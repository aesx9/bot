"""Tramos, estadísticas, robustez, criterios y veredicto de cada estrategia."""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from backtest.data import DataError
from backtest.funding.config import (
    COST_BUFFER,
    DEV_FRACTION,
    HOUR_MS,
    HOURS_PER_YEAR,
    MAX_POSITIONS,
    REBALANCE_TRIGGER,
    RISK_FREE,
    ROBUSTNESS_PCT,
    THRESHOLDS,
    TRANSFER_DELAY_HOURS,
    Account,
    Costs,
    Criteria,
    SpotFee,
    Strategy,
    Thresholds,
)
from backtest.funding.engine import Asset, Position, RunResult, Spec, simulate, spread_signal
from backtest.funding.prepare import (
    VENUE_HL,
    VENUE_KRAKEN,
    VENUE_SPOT,
    Coverage,
    Exclusion,
    Window,
)
from backtest.metrics import max_drawdown

DEV = "desarrollo"
RESERVED = "reservado"


def _quiet(_: str) -> None:
    pass


# --- especificación de cada estrategia ----------------------------------------------------


def spec_a(account: Account, costs: Costs) -> Spec:
    """Margen repartido a partes iguales entre plataformas tras la transferencia inicial."""
    alloc = (account.initial_capital - costs.transfer_usd) / 2.0
    lev = account.max_leverage_a
    return Spec(
        two_sided=True,
        initial={VENUE_KRAKEN: alloc, VENUE_HL: alloc},
        leverage={VENUE_KRAKEN: lev, VENUE_HL: lev},
        notional=lev * alloc / MAX_POSITIONS / (1.0 + COST_BUFFER),
        slippage=costs.slippage,
        rebalance=True,
        rebalance_trigger=REBALANCE_TRIGGER,
        transfer_cost=costs.transfer_usd,
        transfer_delay_hours=TRANSFER_DELAY_HOURS,
        initial_transfers=1,
    )


def spec_b(account: Account, costs: Costs) -> Spec:
    """Mitad en spot, mitad como margen del corto (1x). Sin transferencias entre plataformas."""
    half = account.initial_capital / 2.0
    return Spec(
        two_sided=False,
        initial={VENUE_SPOT: half, VENUE_KRAKEN: half},
        leverage={VENUE_SPOT: 1.0, VENUE_KRAKEN: 1.0},
        notional=half / MAX_POSITIONS / (1.0 + COST_BUFFER),
        slippage=costs.slippage,
    )


# --- estadísticas -------------------------------------------------------------------------


@dataclass(frozen=True)
class Stats:
    hours: int
    net_pnl: float
    net_return: float
    annual_return: float  # rentabilidad simple anualizada: neto / capital × 8760 / horas
    max_drawdown: float
    positions: int
    cycles: int  # ciclos completos (entrada y salida por señal)
    funding_received: float
    funding_paid: float
    fees: float
    slippage: float
    basis_pnl: float
    transfers: int
    transfer_cost: float
    liquidations: int
    liquidation_loss: float  # resultado neto de las posiciones liquidadas (incluye penalización)
    liquidation_penalty: float
    avg_hours: float  # duración media de las posiciones
    avg_captured_annual: float  # funding neto capturado, anualizado, ponderado por horas
    per_asset: dict[str, float]  # resultado neto por activo
    skipped_margin: int
    skipped_slots: int
    no_signal_hours: int
    funding_hours_missing: int


def summarize(run: RunResult) -> Stats:
    ps = run.positions
    hours = len(run.times)
    net = run.net_pnl
    ret = net / run.initial_capital
    per_asset: dict[str, float] = {}
    for p in ps:
        per_asset[p.asset] = per_asset.get(p.asset, 0.0) + p.net_pnl
    held = sum(p.hours * p.notional for p in ps)
    captured = (
        sum(p.funding_received - p.funding_paid for p in ps) / held * HOURS_PER_YEAR
        if held > 0.0 else math.nan
    )
    return Stats(
        hours=hours,
        net_pnl=net,
        net_return=ret,
        annual_return=ret * HOURS_PER_YEAR / hours if hours else math.nan,
        max_drawdown=max_drawdown([run.initial_capital, *run.equity]),
        positions=len(ps),
        cycles=sum(1 for p in ps if p.complete),
        funding_received=sum(p.funding_received for p in ps),
        funding_paid=sum(p.funding_paid for p in ps),
        fees=sum(p.fees for p in ps),
        slippage=sum(p.slippage for p in ps),
        basis_pnl=sum(p.basis_pnl for p in ps),
        transfers=run.transfers,
        transfer_cost=run.transfer_cost,
        liquidations=len(run.liquidations),
        liquidation_loss=sum(x.loss for x in run.liquidations),
        liquidation_penalty=sum(x.penalty for x in run.liquidations),
        avg_hours=statistics.fmean(p.hours for p in ps) if ps else math.nan,
        avg_captured_annual=captured,
        per_asset=per_asset,
        skipped_margin=run.skipped_margin,
        skipped_slots=run.skipped_slots,
        no_signal_hours=run.no_signal_hours,
        funding_hours_missing=sum(p.funding_hours_missing for p in ps),
    )


def max_asset_share(per_asset: dict[str, float]) -> tuple[str, float]:
    """(activo, fracción del beneficio total) del activo que más aporta. NaN si el total ≤ 0."""
    total = sum(per_asset.values())
    if not per_asset or total <= 0.0:
        return "", math.nan
    name = max(per_asset, key=lambda k: per_asset[k])
    return name, per_asset[name] / total


# --- tramos y robustez --------------------------------------------------------------------


def split(first: int, n: int, fraction: float = DEV_FRACTION) -> tuple[range, range]:
    """Desarrollo = primer ``fraction`` de las horas operables; reservado = el resto."""
    cut = first + int((n - first) * fraction)
    return range(first, cut), range(cut, n)


@dataclass(frozen=True)
class Segment:
    name: str
    start_t: int
    end_t: int
    run: RunResult
    stats: Stats


def run_segment(
    name: str,
    assets: Sequence[Asset],
    signals: Sequence[Sequence[float]],
    rng: range,
    thresholds: Thresholds,
    spec: Spec,
) -> Segment:
    run = simulate(assets, signals, rng.start, rng.stop, thresholds, spec)
    t = assets[0].t
    return Segment(name, t[rng.start], t[rng.stop - 1] + HOUR_MS, run, summarize(run))


@dataclass(frozen=True)
class Variant:
    label: str
    thresholds: Thresholds
    stats: Stats


def robustness_variants(base: Thresholds, pct: float) -> list[tuple[str, Thresholds]]:
    """Cada umbral por separado y ambos a la vez, a ``1 - pct`` y ``1 + pct``."""
    out: list[tuple[str, Thresholds]] = []
    for f in (1.0 - pct, 1.0 + pct):
        sign = "-" if f < 1.0 else "+"
        p = f"{sign}{round(pct * 100)} %"
        out.append((f"entrada {p}", Thresholds(base.entry * f, base.exit)))
        out.append((f"salida {p}", Thresholds(base.entry, base.exit * f)))
        out.append((f"ambos {p}", base.scaled(f)))
    return out


def run_robustness(
    assets: Sequence[Asset],
    signals: Sequence[Sequence[float]],
    dev: range,
    base: Thresholds,
    spec: Spec,
    pct: float = ROBUSTNESS_PCT,
) -> list[Variant]:
    """Solo sobre el tramo de desarrollo: nunca recibe el reservado."""
    return [
        Variant(label, th, run_segment(DEV, assets, signals, dev, th, spec).stats)
        for label, th in robustness_variants(base, pct)
    ]


# --- episodios de funding alto (B) --------------------------------------------------------


def high_funding_episodes(signal: Sequence[float], th: Thresholds, first: int) -> list[int]:
    """Duración en horas de cada periodo con la señal por encima de la entrada hasta que baja de
    la salida (sin límite de posiciones). Descriptivo: se calcula sobre toda la ventana."""
    out: list[int] = []
    start: int | None = None
    for i in range(first, len(signal)):
        x = signal[i]
        if math.isnan(x):
            continue
        if start is None and x > th.entry:
            start = i
        elif start is not None and x < th.exit:
            out.append(i - start)
            start = None
    if start is not None:
        out.append(len(signal) - start)
    return out


def cycle_cost_fraction(costs: Costs, spot_fee: SpotFee) -> float:
    """Comisiones y slippage de un ciclo de B (4 ejecuciones) como fracción del nocional."""
    return 2.0 * (costs.spot_fee(spot_fee) + costs.kraken_futures_taker) + 4.0 * costs.slippage


def days_to_cover(cost_fraction: float, annual_rate: float) -> float:
    if not annual_rate > 0.0:
        return math.inf
    return cost_fraction / (annual_rate / 365.0)


# --- criterios ----------------------------------------------------------------------------


class Verdict(StrEnum):
    APPROVED = "aprobado"
    REJECTED = "no supera"
    INCONCLUSIVE = "no concluyente"


@dataclass(frozen=True)
class Check:
    name: str
    detail: str
    passed: bool | None  # None: no evaluado (reservado sin ejecutar)


def _pct(x: float) -> str:
    return "n/d" if math.isnan(x) else f"{x * 100:.2f} %".replace(".", ",")


def evaluate(
    dev: Stats, reserved: Stats | None, robustness: Sequence[Variant], crit: Criteria
) -> list[Check]:
    r = reserved
    checks: list[Check] = []
    checks.append(Check(
        "Rentabilidad anualizada en el reservado ≥ 6 %",
        "sin ejecutar" if r is None else _pct(r.annual_return),
        None if r is None else r.annual_return >= crit.min_reserved_annual,
    ))
    checks.append(Check(
        "Rentabilidad anualizada en desarrollo > 3 %",
        _pct(dev.annual_return),
        dev.annual_return > crit.min_dev_annual,
    ))
    dds = [("desarrollo", dev.max_drawdown)] + ([] if r is None else [("reservado",
                                                                      r.max_drawdown)])
    checks.append(Check(
        "Drawdown máximo < 5 % (en cada tramo)",
        "; ".join(f"{k} {_pct(v)}" for k, v in dds),
        None if r is None and dev.max_drawdown < crit.max_drawdown
        else all(v < crit.max_drawdown for _, v in dds),
    ))
    liq = [("desarrollo", dev.liquidations)] + ([] if r is None else [("reservado",
                                                                      r.liquidations)])
    checks.append(Check(
        "Ninguna liquidación simulada",
        "; ".join(f"{k} {v}" for k, v in liq),
        None if r is None and dev.liquidations == 0
        else all(v <= crit.max_liquidations for _, v in liq),
    ))
    worst = min(robustness, key=lambda v: v.stats.annual_return) if robustness else None
    checks.append(Check(
        "Con los umbrales ±20 % sigue > 3 % en desarrollo",
        "sin variantes" if worst is None else
        f"{sum(1 for v in robustness if v.stats.annual_return > crit.min_robust_annual)}"
        f"/{len(robustness)} variantes; peor {worst.label}: {_pct(worst.stats.annual_return)}",
        bool(robustness) and all(v.stats.annual_return > crit.min_robust_annual
                                 for v in robustness),
    ))
    if r is None:
        checks.append(Check("Ningún activo aporta más del 50 % del beneficio (reservado)",
                            "sin ejecutar", None))
    else:
        name, share = max_asset_share(r.per_asset)
        checks.append(Check(
            "Ningún activo aporta más del 50 % del beneficio (reservado)",
            "sin beneficio total" if math.isnan(share) else f"{name}: {_pct(share)}",
            not math.isnan(share) and share <= crit.max_asset_share,
        ))
    checks.append(Check(
        "Al menos 10 ciclos completos en el reservado",
        "sin ejecutar" if r is None else f"{r.cycles} ciclos",
        None if r is None else r.cycles >= crit.min_cycles,
    ))
    return checks


def verdict(checks: Sequence[Check], reserved_run: bool) -> Verdict | None:
    """Sin ciclos suficientes, «no concluyente» aunque fallen o pasen los demás criterios."""
    if not reserved_run:
        return None
    if checks[-1].passed is False:
        return Verdict.INCONCLUSIVE
    return Verdict.APPROVED if all(c.passed for c in checks) else Verdict.REJECTED


# --- ejecución completa -------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyResult:
    strategy: Strategy
    window: Window
    assets: list[str]
    coverage: list[Coverage]
    excluded: list[Exclusion]  # activos del universo sin datos completos en la ventana
    thresholds: Thresholds
    spec: Spec
    dev: Segment
    reserved: Segment | None
    robustness: list[Variant]
    checks: list[Check]
    verdict: Verdict | None
    risk_free: float
    # Solo B: referencia pesimista con la pierna spot como taker (no se evalúa).
    taker_dev: Segment | None = None
    taker_reserved: Segment | None = None
    episodes: dict[str, list[int]] = field(default_factory=dict)
    cycle_cost: dict[str, float] = field(default_factory=dict)


Log = Callable[[str], None]


def run_strategy(
    strategy: Strategy,
    assets: Sequence[Asset],
    window: Window,
    coverage: list[Coverage],
    excluded: list[Exclusion],
    spec: Spec,
    *,
    only_dev: bool,
    taker_assets: Sequence[Asset] | None = None,
    taker_spec: Spec | None = None,
    costs: Costs | None = None,
    criteria: Criteria | None = None,
    log: Log = _quiet,
) -> StrategyResult:
    if not assets:
        raise DataError(f"{strategy}: ningún activo tiene datos completos en la ventana")
    crit = criteria or Criteria()
    th = THRESHOLDS[strategy]
    signals = [spread_signal(a) for a in assets]
    dev_rng, res_rng = split(assets[0].first, len(assets[0]))
    log(f"{strategy}: desarrollo")
    dev = run_segment(DEV, assets, signals, dev_rng, th, spec)
    log(f"{strategy}: robustez (solo desarrollo)")
    rob = run_robustness(assets, signals, dev_rng, th, spec)
    reserved = None
    if not only_dev:
        log(f"{strategy}: reservado (única pasada)")
        reserved = run_segment(RESERVED, assets, signals, res_rng, th, spec)
    checks = evaluate(dev.stats, None if reserved is None else reserved.stats, rob, crit)
    taker_dev = taker_res = None
    episodes: dict[str, list[int]] = {}
    cycle_cost: dict[str, float] = {}
    if taker_assets is not None and taker_spec is not None:
        t_signals = [spread_signal(a) for a in taker_assets]
        taker_dev = run_segment(DEV, taker_assets, t_signals, dev_rng, th, taker_spec)
        if not only_dev:
            taker_res = run_segment(RESERVED, taker_assets, t_signals, res_rng, th, taker_spec)
    if strategy is Strategy.B:
        episodes = {a.name: high_funding_episodes(s, th, a.first)
                    for a, s in zip(assets, signals, strict=True)}
        c = costs or Costs()
        cycle_cost = {f.value: cycle_cost_fraction(c, f) for f in SpotFee}
    return StrategyResult(
        strategy=strategy,
        window=window,
        assets=[a.name for a in assets],
        coverage=coverage,
        excluded=excluded,
        thresholds=th,
        spec=spec,
        dev=dev,
        reserved=reserved,
        robustness=rob,
        checks=checks,
        verdict=verdict(checks, reserved is not None),
        risk_free=RISK_FREE,
        taker_dev=taker_dev,
        taker_reserved=taker_res,
        episodes=episodes,
        cycle_cost=cycle_cost,
    )


def positions_of(r: StrategyResult) -> list[tuple[str, Position]]:
    out = [(DEV, p) for p in r.dev.run.positions]
    if r.reserved is not None:
        out += [(RESERVED, p) for p in r.reserved.run.positions]
    return out
