"""Orquestación: carga datos, ejecuta tramos y escenarios, validación y veredicto."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from backtest.config import (
    CANDLE_MS,
    HOUR_MS,
    N_RANDOM,
    RANDOM_SEED,
    SYMBOLS,
    Account,
    Costs,
    Criteria,
    Params,
    Scenario,
)
from backtest.data import Candles, FundingSeries, load_candles, load_funding
from backtest.engine import RunResult, Segment, simulate
from backtest.funding import FundingStats, FundingTable, funding_stats
from backtest.metrics import BuyHold, Stats, buy_and_hold, summarize
from backtest.signals import Indicators, compute_indicators, generate_signals
from backtest.validation import (
    RandomSummary,
    RobustnessRow,
    common_length,
    percentile_rank,
    run_random,
    run_robustness,
    split_segments,
)

TOTAL = "TOTAL"
DEV = "desarrollo"
RESERVED = "reservado"
YEAR_MS = 365.25 * 86_400_000


@dataclass(frozen=True)
class AssetMeta:
    symbol: str
    n_candles: int
    first_ms: int
    last_open_ms: int
    candle_years: float
    funding: FundingStats
    funding_years: float


@dataclass(frozen=True)
class SegmentOutcome:
    segment: Segment
    run: RunResult
    stats: dict[str, Stats]  # por activo y TOTAL
    buy_hold: dict[str, BuyHold]
    funding_coverage: dict[str, float]  # fracción de horas del tramo con funding real


@dataclass(frozen=True)
class Check:
    name: str
    detail: str
    passed: bool | None  # None: no evaluado (p. ej. con --solo-desarrollo)


@dataclass
class Results:
    params: Params
    costs: Costs
    account: Account
    criteria: Criteria
    meta: dict[str, AssetMeta]
    segments: dict[str, Segment]
    times_ms: list[int]
    outcomes: dict[Scenario, dict[str, SegmentOutcome]]
    robustness: list[RobustnessRow]
    random: dict[str, dict[Scenario, RandomSummary]]
    percentiles: dict[tuple[Scenario, str], float]
    checks: list[Check] = field(default_factory=list)
    only_dev: bool = False
    n_random: int = 0
    seed: int = 0

    @property
    def verdict(self) -> bool | None:
        """True si todos los criterios pasan; False si alguno falla; None si falta alguno."""
        if any(c.passed is False for c in self.checks):
            return False
        if any(c.passed is None for c in self.checks):
            return None
        return True


Log = Callable[[str], None]


def _quiet(_: str) -> None:
    return None


def load_data(
    data_dir: Path, symbols: tuple[str, ...] = SYMBOLS
) -> tuple[dict[str, Candles], dict[str, FundingSeries]]:
    candles = {s: load_candles(data_dir, s) for s in symbols}
    funding = {s: load_funding(data_dir, s) for s in symbols}
    return candles, funding


def run_all(
    candles: Mapping[str, Candles],
    funding: Mapping[str, FundingSeries],
    *,
    params: Params | None = None,
    costs: Costs | None = None,
    account: Account | None = None,
    criteria: Criteria | None = None,
    only_dev: bool = False,
    n_random: int = N_RANDOM,
    seed: int = RANDOM_SEED,
    log: Log = _quiet,
) -> Results:
    params, costs = params or Params(), costs or Costs()
    account, criteria = account or Account(), criteria or Criteria()
    n = common_length(candles)
    dev, reserved = split_segments(n)
    segments = {DEV: dev} if only_dev else {DEV: dev, RESERVED: reserved}
    times = next(iter(candles.values())).t

    stats = {a: funding_stats(f) for a, f in funding.items()}
    meta = {
        a: AssetMeta(
            a,
            len(c),
            c.t[0],
            c.t[-1],
            (c.t[-1] + CANDLE_MS - c.t[0]) / YEAR_MS,
            stats[a],
            (stats[a].last_ms + HOUR_MS - stats[a].first_ms) / YEAR_MS,
        )
        for a, c in candles.items()
    }
    tables = {
        sc: {a: FundingTable(candles[a], funding[a], stats[a], sc) for a in candles}
        for sc in Scenario
    }
    indicators: dict[str, Indicators] = {
        a: compute_indicators(c, params) for a, c in candles.items()
    }
    signals = {a: generate_signals(c, params, indicators[a]) for a, c in candles.items()}

    outcomes: dict[Scenario, dict[str, SegmentOutcome]] = {sc: {} for sc in Scenario}
    for name, seg in segments.items():
        for sc in Scenario:
            log(f"estrategia base: tramo {name}, funding {sc.value}")
            run = simulate(
                candles, signals, tables[sc], seg, params, costs, account, curves=True
            )
            outcomes[sc][name] = _outcome(candles, tables[sc], seg, run, costs, account, times)

    log("robustez ±20 % (solo tramo de desarrollo)")
    robustness = run_robustness(
        candles, tables, dev, params, costs, account, criteria.robustness_pct
    )

    random_results: dict[str, dict[Scenario, RandomSummary]] = {}
    percentiles: dict[tuple[Scenario, str], float] = {}
    for name, seg in segments.items():
        log(f"azar: {n_random} simulaciones en {name}")
        random_results[name] = run_random(
            candles, signals, indicators, tables, seg, params, costs, account, n_random, seed
        )
        for sc in Scenario:
            net = outcomes[sc][name].stats[TOTAL].net_return
            percentiles[(sc, name)] = percentile_rank(random_results[name][sc].net_returns, net)

    results = Results(
        params, costs, account, criteria, meta, segments, list(times), outcomes, robustness,
        random_results, percentiles, only_dev=only_dev, n_random=n_random, seed=seed,
    )
    results.checks = evaluate_criteria(results)
    return results


def _outcome(
    candles: Mapping[str, Candles],
    tables: Mapping[str, FundingTable],
    seg: Segment,
    run: RunResult,
    costs: Costs,
    account: Account,
    times: list[int],
) -> SegmentOutcome:
    assert run.curve is not None and run.asset_pnl is not None  # noqa: S101
    seg_times = times[seg.start : seg.end]
    stats = {
        a: summarize(
            [t for t in run.trades if t.asset == a],
            [account.initial_capital + x for x in run.asset_pnl[a]],
            seg_times,
            account.initial_capital,
        )
        for a in candles
    }
    stats[TOTAL] = summarize(run.trades, run.curve, seg_times, account.initial_capital)
    bh = {a: buy_and_hold(c, seg, tables[a], costs, account) for a, c in candles.items()}
    coverage = {}
    for a in candles:
        real, imp = tables[a].hours(seg.start, seg.end - 1)
        coverage[a] = real / (real + imp)
    return SegmentOutcome(seg, run, stats, bh, coverage)


def evaluate_criteria(r: Results) -> list[Check]:
    """Criterios de aceptación, evaluados con el escenario de funding pesimista."""
    c, sc = r.criteria, r.criteria.judged_scenario
    checks: list[Check] = []

    reserved = r.outcomes[sc].get(RESERVED)
    if reserved is None:
        for name in ("Neto positivo en el reservado", "Operaciones en el reservado",
                     "Drawdown máximo en el reservado"):
            checks.append(Check(name, "no evaluado (solo desarrollo)", None))
    else:
        tot = reserved.stats[TOTAL]
        checks.append(Check(
            "Neto positivo en el reservado",
            f"rentabilidad neta {_pct(tot.net_return)} ({_comma(f'{tot.net_pnl:+.2f}')} USD)",
            tot.net_pnl > 0.0,
        ))
        checks.append(Check(
            "Operaciones en el reservado",
            f"{tot.n_trades} operaciones (mínimo {c.min_trades})",
            tot.n_trades >= c.min_trades,
        ))
        checks.append(Check(
            "Drawdown máximo en el reservado",
            f"{_pct(tot.max_drawdown)} (debe ser < {_pct(c.max_drawdown)})",
            tot.max_drawdown < c.max_drawdown,
        ))

    pct = int(round(c.robustness_pct * 100))
    returns = [row.net_return[sc] for row in r.robustness]
    worst = min(returns)
    positive = sum(1 for x in returns if x > 0.0)
    checks.append(Check(
        f"Positivo con los parámetros ±{pct} % (desarrollo)",
        f"{positive}/{len(returns)} variantes con neto positivo; peor {_pct(worst)}",
        positive == len(returns),
    ))

    if reserved is None:
        checks.append(
            Check("Percentil frente al azar (reservado)", "no evaluado (solo desarrollo)", None)
        )
    else:
        p = r.percentiles[(sc, RESERVED)]
        rnd = r.random[RESERVED][sc]
        checks.append(Check(
            "Percentil frente al azar (reservado)",
            f"percentil {_comma(f'{p:.1f}')} de {rnd.n_sims} simulaciones "
            f"(mínimo {c.min_percentile:.0f}); "
            f"mediana del azar {_pct(rnd.median)}",
            p >= c.min_percentile,
        ))
    return checks


def _comma(text: str) -> str:
    return text.replace(".", ",")


def _pct(x: float) -> str:
    return "n/d" if math.isnan(x) else _comma(f"{x * 100:.2f} %")
