"""Motor de simulación de cartera: una posición por activo, capital y apalancamiento comunes.

Reglas de ejecución (todas fijas):

- La señal de la vela ``i`` se genera al cierre; la entrada es la apertura de la vela ``i + 1``.
  Nunca se usan datos de la vela en curso.
- Stop a ``stop_atr`` x ATR y take profit a ``tp_atr`` x ATR *desde el precio de entrada* (con
  slippage), con el ATR de la vela de la señal. Ambos se vigilan desde la propia vela de entrada.
- Si en una vela caben stop y take profit, salta primero el stop (criterio pesimista). Si la vela
  abre más allá del stop (hueco), el stop se ejecuta a la apertura. El take profit se ejecuta
  siempre a su nivel, aunque la vela abra mejor.
- Comisión taker y slippage fijo (en contra) en *cada* ejecución: entrada y salida.
- Tamaño: arriesgar ``risk_per_trade`` del capital realizado hasta el stop; el nocional abierto
  total no supera ``max_leverage`` x capital realizado. Si no cabe entero se reduce; si no cabe
  nada, la operación se descarta. Con varias señales en la misma vela, manda el orden de los
  activos del mapa de entrada.
- Una posición por activo: una señal con posición abierta en ese activo se ignora.
- Al terminar el tramo, las posiciones abiertas se cierran al cierre de su última vela.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from backtest.config import CANDLE_MS, Account, Costs, Params, Scenario
from backtest.data import Candles
from backtest.funding_cost import FundingTable
from backtest.signals import Signal


@dataclass(frozen=True)
class Segment:
    """Tramo ``[start, end)`` de índices de vela."""

    name: str
    start: int
    end: int


@dataclass(frozen=True)
class Trade:
    asset: str
    side: int
    signal_idx: int
    entry_idx: int
    exit_idx: int
    entry_time_ms: int
    exit_time_ms: int  # cierre de la vela de salida
    exit_reason: str  # stop | tp | fin_de_tramo
    atr: float
    entry_ref: float  # apertura de la vela de entrada, sin slippage
    entry_fill: float
    stop: float
    take_profit: float
    exit_ref: float  # precio de salida teórico, sin slippage
    exit_fill: float
    qty: float
    notional: float
    balance_before: float  # capital realizado usado para dimensionar
    gross_pnl: float  # antes de comisiones, slippage y funding
    fee_entry: float
    fee_exit: float
    slippage: float
    funding_real: float
    funding_imputed: float
    funding_hours_real: int
    funding_hours_imputed: int
    net_pnl: float

    @property
    def fees(self) -> float:
        return self.fee_entry + self.fee_exit

    @property
    def funding(self) -> float:
        return self.funding_real + self.funding_imputed


@dataclass(frozen=True)
class RunResult:
    segment: Segment
    scenario: Scenario
    trades: list[Trade]
    raw_signals: int  # señales del tramo antes de aplicar ningún filtro de ejecución
    ignored_open: int  # ignoradas por tener ya posición en ese activo
    skipped_margin: int  # descartadas por falta de margen de apalancamiento
    curve: list[float] | None  # capital marcado a mercado al cierre de cada vela del tramo
    asset_pnl: dict[str, list[float]] | None  # PnL acumulado marcado a mercado por activo


@dataclass
class _Open:
    exit_idx: int
    notional: float
    net_pnl: float


def resolve_exit(
    c: Candles, side: int, entry_idx: int, stop: float, tp: float, last_idx: int
) -> tuple[int, float, str]:
    """Primera vela, desde la de entrada, en la que salta el stop o el take profit.

    Devuelve ``(índice de la vela de salida, precio teórico de salida, motivo)``.
    """
    for j in range(entry_idx, last_idx + 1):
        if side > 0:
            if c.l[j] <= stop:
                return j, min(stop, c.o[j]), "stop"
            if c.h[j] >= tp:
                return j, tp, "tp"
        else:
            if c.h[j] >= stop:
                return j, max(stop, c.o[j]), "stop"
            if c.l[j] <= tp:
                return j, tp, "tp"
    return last_idx, c.c[last_idx], "fin_de_tramo"


def simulate(
    candles: Mapping[str, Candles],
    signals: Mapping[str, Sequence[Signal]],
    funding: Mapping[str, FundingTable],
    seg: Segment,
    params: Params,
    costs: Costs,
    account: Account,
    *,
    curves: bool = False,
) -> RunResult:
    scenario = next(iter(funding.values())).scenario
    order = {asset: n for n, asset in enumerate(candles)}
    events: list[tuple[int, int, str, Signal]] = []
    for asset, sigs in signals.items():
        for s in sigs:
            # La entrada (s.idx + 1) debe caer dentro del tramo.
            if seg.start <= s.idx <= seg.end - 2:
                events.append((s.idx + 1, order[asset], asset, s))
    events.sort(key=lambda e: (e[0], e[1]))

    balance = account.initial_capital
    open_pos: dict[str, _Open] = {}
    trades: list[Trade] = []
    ignored = skipped = 0
    for entry_idx, _, asset, sig in events:
        for a in [a for a, p in open_pos.items() if p.exit_idx < entry_idx]:
            balance += open_pos.pop(a).net_pnl
        if asset in open_pos:
            ignored += 1
            continue
        capacity = account.max_leverage * balance - sum(p.notional for p in open_pos.values())
        if balance <= 0.0 or capacity <= 0.0:
            skipped += 1
            continue
        trade = _enter(
            candles[asset], funding[asset], asset, sig, entry_idx, seg, params, costs, account,
            balance, capacity,
        )
        trades.append(trade)
        open_pos[asset] = _Open(trade.exit_idx, trade.notional, trade.net_pnl)

    curve = asset_pnl = None
    if curves:
        curve, asset_pnl = _build_curves(trades, candles, funding, seg, account.initial_capital)
    return RunResult(seg, scenario, trades, len(events), ignored, skipped, curve, asset_pnl)


def _enter(
    c: Candles,
    fund: FundingTable,
    asset: str,
    sig: Signal,
    entry_idx: int,
    seg: Segment,
    params: Params,
    costs: Costs,
    account: Account,
    balance: float,
    capacity: float,
) -> Trade:
    side = sig.side
    entry_ref = c.o[entry_idx]
    entry_fill = entry_ref * (1.0 + side * costs.slippage)
    stop_dist = params.stop_atr * sig.atr
    qty = account.risk_per_trade * balance / stop_dist
    if qty * entry_fill > capacity:
        qty = capacity / entry_fill
    stop = entry_fill - side * stop_dist
    take_profit = entry_fill + side * params.tp_atr * sig.atr

    exit_idx, exit_ref, reason = resolve_exit(c, side, entry_idx, stop, take_profit, seg.end - 1)
    exit_fill = exit_ref * (1.0 - side * costs.slippage)
    fee_entry = costs.fee * qty * entry_fill
    fee_exit = costs.fee * qty * exit_fill
    gross = side * qty * (exit_ref - entry_ref)
    slippage = qty * costs.slippage * (entry_ref + exit_ref)
    unit_real, unit_imp = fund.cost_per_unit(side, entry_idx, exit_idx)
    hours_real, hours_imp = fund.hours(entry_idx, exit_idx)
    funding_real, funding_imp = qty * unit_real, qty * unit_imp
    net = gross - slippage - fee_entry - fee_exit - funding_real - funding_imp
    return Trade(
        asset=asset,
        side=side,
        signal_idx=sig.idx,
        entry_idx=entry_idx,
        exit_idx=exit_idx,
        entry_time_ms=c.t[entry_idx],
        exit_time_ms=c.t[exit_idx] + CANDLE_MS,
        exit_reason=reason,
        atr=sig.atr,
        entry_ref=entry_ref,
        entry_fill=entry_fill,
        stop=stop,
        take_profit=take_profit,
        exit_ref=exit_ref,
        exit_fill=exit_fill,
        qty=qty,
        notional=qty * entry_fill,
        balance_before=balance,
        gross_pnl=gross,
        fee_entry=fee_entry,
        fee_exit=fee_exit,
        slippage=slippage,
        funding_real=funding_real,
        funding_imputed=funding_imp,
        funding_hours_real=hours_real,
        funding_hours_imputed=hours_imp,
        net_pnl=net,
    )


def _build_curves(
    trades: Sequence[Trade],
    candles: Mapping[str, Candles],
    funding: Mapping[str, FundingTable],
    seg: Segment,
    initial: float,
) -> tuple[list[float], dict[str, list[float]]]:
    """Capital marcado a mercado al cierre de cada vela: realizado + abierto a precio de cierre.

    Una posición abierta suma su PnL latente menos la comisión de entrada y el funding devengado
    hasta ese cierre; la comisión y el slippage de salida solo se descuentan al salir.
    """
    length = seg.end - seg.start
    total_open = [0.0] * length
    total_real = [0.0] * (length + 1)
    by_open = {a: [0.0] * length for a in candles}
    by_real = {a: [0.0] * (length + 1) for a in candles}
    for tr in trades:
        c = candles[tr.asset]
        for j in range(tr.entry_idx, tr.exit_idx):
            real, imp = funding[tr.asset].cost_per_unit(tr.side, tr.entry_idx, j)
            latent = tr.side * tr.qty * (c.c[j] - tr.entry_fill)
            mark = latent - tr.fee_entry - tr.qty * (real + imp)
            total_open[j - seg.start] += mark
            by_open[tr.asset][j - seg.start] += mark
        k = tr.exit_idx - seg.start
        total_open[k] += tr.net_pnl
        by_open[tr.asset][k] += tr.net_pnl
        total_real[k + 1] += tr.net_pnl
        by_real[tr.asset][k + 1] += tr.net_pnl

    def accumulate(open_arr: list[float], real_delta: list[float]) -> list[float]:
        out: list[float] = []
        realized = 0.0
        for j in range(length):
            realized += real_delta[j]
            out.append(realized + open_arr[j])
        return out

    pnl = accumulate(total_open, total_real)
    return [initial + x for x in pnl], {a: accumulate(by_open[a], by_real[a]) for a in candles}
