"""Simulación horaria de una estrategia de arbitraje de funding neutral al precio.

Cada activo tiene dos piernas alineadas en la misma rejilla horaria:

- A: ``leg1`` = perpetuo de Kraken Futures, ``leg2`` = perpetuo de Hyperliquid.
- B: ``leg1`` = spot de Kraken (índice spot de Kraken Futures como aproximación, sin funding),
  ``leg2`` = perpetuo de Kraken Futures.

Diferencial horario ``s = tasa2 - tasa1``. Dirección ``+1`` = largo ``leg1`` y corto ``leg2``
(cobra ``s`` por nocional y hora); ``-1`` = al revés. B solo admite ``+1`` (no se vende spot).

Orden de cada hora ``i`` (``t[i]`` es su inicio):

1. Decisión a la apertura con la media de ``s`` de las horas ``i-24 … i-1``, todas ya liquidadas
   (el funding de la hora ``k`` se liquida al final de ``k``). Primero salidas, luego entradas.
   Ejecución a la apertura de la vela ``i`` de cada pierna, con slippage en contra.
2. Comprobación de liquidación de cada cuenta con margen, con los extremos adversos de la vela
   (largos al mínimo, cortos al máximo, todos a la vez: peor caso).
3. Funding de la hora ``i`` sobre las posiciones que siguen abiertas (precio de referencia: la
   apertura de la hora).
4. Valoración al cierre (curva de capital) y, en A, reequilibrio de margen entre plataformas.
   El importe transferido llega ``transfer_delay_hours`` horas después (al cierre de esa hora);
   en tránsito no es margen de ninguna cuenta, pero sí forma parte del capital total.

Ambas piernas tienen la misma cantidad de activo base, de modo que el resultado por precio es
exactamente la variación de la base entre las dos piernas (``resultado por base``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from backtest.funding.config import HOUR_MS, HOURS_PER_YEAR, MAX_POSITIONS, MEAN_HOURS, Thresholds


@dataclass(frozen=True)
class Leg:
    """Serie de un mercado en la rejilla del activo. ``rate`` es NaN donde no hay dato."""

    venue: str  # cuenta en la que vive la pierna
    o: list[float]
    h: list[float]
    l: list[float]  # noqa: E741
    c: list[float]
    rate: list[float]  # funding horario relativo de la hora (positivo: pagan los largos)
    fee: float  # comisión por ejecución
    mm: float | None  # margen de mantenimiento (fracción del nocional); None = sin margen (spot)


@dataclass(frozen=True)
class Asset:
    name: str
    t: list[int]  # inicio de cada hora; incluye ``first`` horas previas de calentamiento
    leg1: Leg
    leg2: Leg
    first: int = MEAN_HOURS  # índice de la primera hora operable

    def __len__(self) -> int:
        return len(self.t)


@dataclass(frozen=True)
class Spec:
    """Cómo se reparte el capital y qué se permite en cada estrategia."""

    two_sided: bool  # A: sí; B: solo largo spot / corto perpetuo
    initial: dict[str, float]  # capital inicial por cuenta (ya neto de transferencias iniciales)
    leverage: dict[str, float]  # nocional abierto máximo / capital, por cuenta
    notional: float  # por pierna y posición
    slippage: float
    rebalance: bool = False
    rebalance_trigger: float = 0.5
    transfer_cost: float = 0.0
    transfer_delay_hours: int = 0  # horas que tarda en llegar un reequilibrio (0 = instantáneo)
    initial_transfers: int = 0
    max_positions: int = MAX_POSITIONS


class ExitReason(StrEnum):
    SIGNAL = "señal"
    END = "fin de tramo"
    LIQUIDATION = "liquidación"


@dataclass(frozen=True)
class Position:
    """Ciclo cerrado. Importes en USD; costes y pagos en positivo."""

    asset: str
    direction: int
    entry_t: int
    exit_t: int  # apertura de la hora de salida (o cierre de la última hora del tramo)
    hours: int  # horas con la posición abierta
    notional: float  # por pierna, a la entrada
    entry_signal: float  # media de 24 h anualizada del diferencial al entrar (con signo)
    basis_pnl: float  # variación de la base sobre precios de referencia
    funding_received: float
    funding_paid: float
    fees: float
    slippage: float
    liquidation_penalty: float
    reason: ExitReason
    funding_hours_missing: int

    @property
    def net_pnl(self) -> float:
        return (
            self.basis_pnl
            + self.funding_received
            - self.funding_paid
            - self.fees
            - self.slippage
            - self.liquidation_penalty
        )

    @property
    def complete(self) -> bool:
        """Ciclo completo: entrada y salida por señal dentro del tramo."""
        return self.reason is ExitReason.SIGNAL

    @property
    def captured_annual(self) -> float:
        """Funding neto capturado, anualizado sobre el nocional."""
        if self.hours == 0:
            return math.nan
        net = self.funding_received - self.funding_paid
        return net / (self.notional * self.hours) * HOURS_PER_YEAR


@dataclass(frozen=True)
class Liquidation:
    t: int
    venue: str
    assets: tuple[str, ...]
    penalty: float  # margen de mantenimiento perdido
    loss: float  # resultado neto de las posiciones cerradas por el evento


@dataclass(frozen=True)
class RunResult:
    positions: list[Position]
    equity: list[float]  # capital total al cierre de cada hora del tramo
    times: list[int]  # inicio de cada hora del tramo
    initial_capital: float
    transfers: int
    transfer_cost: float
    liquidations: list[Liquidation]
    skipped_margin: int  # entradas descartadas por el límite de apalancamiento
    skipped_slots: int  # entradas descartadas por tener ya 3 posiciones
    no_signal_hours: int  # horas-activo sin media de 24 h completa (sin decisión)

    @property
    def net_pnl(self) -> float:
        return self.equity[-1] - self.initial_capital if self.equity else 0.0


# --- señal --------------------------------------------------------------------------------


def spread_signal(asset: Asset, hours: int = MEAN_HOURS) -> list[float]:
    """Media de ``tasa2 - tasa1`` de las ``hours`` horas anteriores a cada ``i``, anualizada.

    ``signal[i]`` solo usa funding de horas ``< i`` (liquidado antes de decidir en ``i``). Una tasa
    sin dato (NaN) cuenta como funding cero. NaN solo si no hay historia suficiente."""
    n = len(asset)
    s = [_zero_if_nan(b) - _zero_if_nan(a)
         for a, b in zip(asset.leg1.rate, asset.leg2.rate, strict=True)]
    out = [math.nan] * n
    total = 0.0
    for i in range(n):
        if i >= hours:
            out[i] = total / hours * HOURS_PER_YEAR
            total -= s[i - hours]
        total += s[i]
    return out


def _zero_if_nan(x: float) -> float:
    return 0.0 if math.isnan(x) else x


# --- simulación ---------------------------------------------------------------------------


@dataclass
class _Open:
    k: int  # índice del activo
    direction: int
    q: float  # cantidad de base en cada pierna
    entry_i: int
    entry_signal: float
    ref1: float
    ref2: float
    fill1: float
    fill2: float
    fees: float = 0.0
    slippage: float = 0.0
    received: float = 0.0
    paid: float = 0.0
    missing: int = 0

    def sides(self) -> tuple[int, int]:
        return self.direction, -self.direction


@dataclass
class _State:
    cash: dict[str, float]
    open: list[_Open] = field(default_factory=list)
    closed: list[Position] = field(default_factory=list)
    liquidations: list[Liquidation] = field(default_factory=list)
    in_transit: list[tuple[int, str, float]] = field(default_factory=list)  # (llega en i, a, USD)


def _legs(a: Asset) -> tuple[Leg, Leg]:
    return a.leg1, a.leg2


def _venue_equity(st: _State, assets: Sequence[Asset], venue: str, price: str, i: int) -> float:
    eq = st.cash[venue]
    for p in st.open:
        for leg, side, fill in zip(_legs(assets[p.k]), p.sides(), (p.fill1, p.fill2), strict=True):
            if leg.venue == venue:
                eq += side * p.q * (getattr(leg, price)[i] - fill)
    return eq


def _venue_notional(st: _State, assets: Sequence[Asset], venue: str, i: int) -> float:
    total = 0.0
    for p in st.open:
        for leg in _legs(assets[p.k]):
            if leg.venue == venue:
                total += p.q * leg.o[i]
    return total


def _close(
    st: _State,
    assets: Sequence[Asset],
    p: _Open,
    i_exit: int,
    exit_t: int,
    refs: tuple[float, float],
    slip: tuple[float, float],
    reason: ExitReason,
    penalty: float = 0.0,
) -> Position:
    """Cierra ``p`` con precios de referencia ``refs`` y slippage relativo ``slip`` por pierna."""
    a = assets[p.k]
    basis = 0.0
    for leg, side, ref_in, ref_out, fill_in, s in zip(
        _legs(a), p.sides(), (p.ref1, p.ref2), refs, (p.fill1, p.fill2), slip, strict=True
    ):
        fill_out = ref_out * (1.0 - side * s)  # vender (largo) más barato, recomprar más caro
        cost_slip = p.q * ref_out * s
        fee = leg.fee * p.q * fill_out
        st.cash[leg.venue] += side * p.q * (fill_out - fill_in) - fee
        p.fees += fee
        p.slippage += cost_slip
        basis += side * p.q * (ref_out - ref_in)
    pos = Position(
        asset=a.name,
        direction=p.direction,
        entry_t=a.t[p.entry_i],
        exit_t=exit_t,
        hours=i_exit - p.entry_i,
        notional=p.q * p.ref1,
        entry_signal=p.entry_signal,
        basis_pnl=basis,
        funding_received=p.received,
        funding_paid=p.paid,
        fees=p.fees,
        slippage=p.slippage,
        liquidation_penalty=penalty,
        reason=reason,
        funding_hours_missing=p.missing,
    )
    st.closed.append(pos)
    return pos


def _check_liquidations(st: _State, assets: Sequence[Asset], spec: Spec, i: int) -> None:
    venues = sorted({leg.venue for a in assets for leg in _legs(a) if leg.mm is not None})
    for venue in venues:
        involved = [
            p for p in st.open if any(leg.venue == venue for leg in _legs(assets[p.k]))
        ]
        if not involved:
            continue
        eq = st.cash[venue]
        req = 0.0
        for p in involved:
            for leg, side, fill in zip(
                _legs(assets[p.k]), p.sides(), (p.fill1, p.fill2), strict=True
            ):
                if leg.venue != venue:
                    continue
                ext = leg.l[i] if side > 0 else leg.h[i]
                eq += side * p.q * (ext - fill)
                req += (leg.mm or 0.0) * p.q * ext
        if eq >= req:
            continue
        # Liquidación de toda la cuenta (margen cruzado): sus piernas se cierran al extremo
        # adverso y se pierde el margen de mantenimiento; las piernas de cobertura en la otra
        # cuenta se cierran al cierre de la vela, con slippage y comisión.
        names: list[str] = []
        loss = 0.0
        penalty_total = 0.0
        for p in involved:
            a = assets[p.k]
            refs: list[float] = []
            slips: list[float] = []
            penalty = 0.0
            for leg, side in zip(_legs(a), p.sides(), strict=True):
                if leg.venue == venue:
                    ext = leg.l[i] if side > 0 else leg.h[i]
                    refs.append(ext)
                    slips.append(0.0)
                    penalty += (leg.mm or 0.0) * p.q * ext
                else:
                    refs.append(leg.c[i])
                    slips.append(spec.slippage)
            st.cash[venue] -= penalty
            pos = _close(st, assets, p, i + 1, a.t[i], (refs[0], refs[1]), (slips[0], slips[1]),
                         ExitReason.LIQUIDATION, penalty)
            st.open.remove(p)
            names.append(a.name)
            loss += pos.net_pnl
            penalty_total += penalty
        st.liquidations.append(Liquidation(assets[0].t[i], venue, tuple(names), penalty_total,
                                           loss))


def simulate(
    assets: Sequence[Asset],
    signals: Sequence[Sequence[float]],
    start: int,
    end: int,
    thresholds: Thresholds,
    spec: Spec,
) -> RunResult:
    """Simula las horas ``[start, end)`` (índices comunes a todos los activos).

    ``signals[k][i]`` es la media de 24 h anualizada del diferencial de ``assets[k]`` disponible
    al decidir en ``i`` (ver ``spread_signal``). Cada tramo empieza con el capital de ``spec``."""
    if not assets:
        raise ValueError("sin activos")
    times = assets[0].t
    for a in assets:
        if a.t != times:
            raise ValueError(f"{a.name} no comparte la rejilla horaria")
    if not (assets[0].first <= start < end <= len(times)):
        raise ValueError("tramo fuera de la rejilla operable")
    st = _State(cash=dict(spec.initial))
    initial_capital = sum(spec.initial.values()) + spec.initial_transfers * spec.transfer_cost
    transfers = spec.initial_transfers
    transfer_cost = spec.initial_transfers * spec.transfer_cost
    skipped_margin = skipped_slots = no_signal = 0
    equity: list[float] = []

    for i in range(start, end):
        # 1. salidas
        for p in list(st.open):
            sig = signals[p.k][i]
            if math.isnan(sig):
                continue
            if p.direction * sig < thresholds.exit:
                a = assets[p.k]
                _close(st, assets, p, i, a.t[i], (a.leg1.o[i], a.leg2.o[i]),
                       (spec.slippage, spec.slippage), ExitReason.SIGNAL)
                st.open.remove(p)
        # 1. entradas, de mayor a menor diferencial
        held = {p.k for p in st.open}
        candidates: list[tuple[float, str, int, int]] = []
        for k, a in enumerate(assets):
            sig = signals[k][i]
            if math.isnan(sig):
                no_signal += 1
                continue
            if k in held:
                continue
            if sig > thresholds.entry:
                candidates.append((-sig, a.name, k, 1))
            elif spec.two_sided and -sig > thresholds.entry:
                candidates.append((sig, a.name, k, -1))
        for neg_abs, _name, k, direction in sorted(candidates):
            if len(st.open) >= spec.max_positions:
                skipped_slots += 1
                continue
            a = assets[k]
            q = spec.notional / a.leg1.o[i]
            fits = True
            for venue, lev in spec.leverage.items():
                extra = sum(q * leg.o[i] for leg in _legs(a) if leg.venue == venue)
                if extra == 0.0:
                    continue
                eq = _venue_equity(st, assets, venue, "o", i)
                if _venue_notional(st, assets, venue, i) + extra > lev * eq:
                    fits = False
            if not fits:
                skipped_margin += 1
                continue
            p = _Open(k, direction, q, i, -neg_abs if direction > 0 else neg_abs,
                      a.leg1.o[i], a.leg2.o[i], 0.0, 0.0)
            fills = []
            for leg, side, ref in zip(_legs(a), p.sides(), (p.ref1, p.ref2), strict=True):
                fill = ref * (1.0 + side * spec.slippage)  # comprar más caro, vender más barato
                fee = leg.fee * q * fill
                st.cash[leg.venue] -= fee
                p.fees += fee
                p.slippage += q * ref * spec.slippage
                fills.append(fill)
            p.fill1, p.fill2 = fills
            st.open.append(p)
        # 2. liquidaciones con los extremos de la vela
        _check_liquidations(st, assets, spec, i)
        # 3. funding de la hora
        for p in st.open:
            for leg, side in zip(_legs(assets[p.k]), p.sides(), strict=True):
                rate = leg.rate[i]
                if math.isnan(rate):  # hora sin dato de funding: cuenta como cero y se anota
                    p.missing += 1
                    continue
                pay = side * p.q * leg.o[i] * rate
                st.cash[leg.venue] -= pay
                if pay > 0.0:
                    p.paid += pay
                else:
                    p.received -= pay
        # 4. llegada de transferencias, valoración al cierre y reequilibrio
        for tr in [tr for tr in st.in_transit if tr[0] <= i]:
            st.cash[tr[1]] += tr[2]
            st.in_transit.remove(tr)
        per_venue = {v: _venue_equity(st, assets, v, "c", i) for v in st.cash}
        if spec.rebalance and len(per_venue) == 2 and not st.in_transit:
            (v_lo, e_lo), (v_hi, e_hi) = sorted(per_venue.items(), key=lambda kv: kv[1])
            mean = (e_lo + e_hi) / 2.0
            if mean > 0.0 and e_lo < spec.rebalance_trigger * mean:
                amount = (e_hi - e_lo) / 2.0
                st.cash[v_hi] -= amount
                per_venue[v_hi] -= amount
                transfers += 1
                transfer_cost += spec.transfer_cost
                arrival = amount - spec.transfer_cost
                if spec.transfer_delay_hours > 0:
                    st.in_transit.append((i + spec.transfer_delay_hours, v_lo, arrival))
                else:
                    st.cash[v_lo] += arrival
                    per_venue[v_lo] += arrival
        pending = sum(tr[2] for tr in st.in_transit)
        equity.append(sum(per_venue.values()) + pending)

    # Cierre forzoso al final del tramo, al cierre de la última vela (no es un ciclo completo).
    last = end - 1
    for p in list(st.open):
        a = assets[p.k]
        _close(st, assets, p, end, a.t[last] + HOUR_MS, (a.leg1.c[last], a.leg2.c[last]),
               (spec.slippage, spec.slippage), ExitReason.END)
        st.open.remove(p)
    if equity:  # una transferencia aún en tránsito sigue siendo capital
        equity[-1] = sum(st.cash.values()) + sum(tr[2] for tr in st.in_transit)

    return RunResult(
        positions=st.closed,
        equity=equity,
        times=list(times[start:end]),
        initial_capital=initial_capital,
        transfers=transfers,
        transfer_cost=transfer_cost,
        liquidations=st.liquidations,
        skipped_margin=skipped_margin,
        skipped_slots=skipped_slots,
        no_signal_hours=no_signal,
    )
