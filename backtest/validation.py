"""Validación: división temporal, robustez de parámetros y comparación con entradas aleatorias."""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from backtest.config import (
    DEV_FRACTION,
    PARAM_NAMES,
    Account,
    Costs,
    Params,
    Scenario,
    perturb,
)
from backtest.data import Candles
from backtest.engine import Segment, simulate
from backtest.funding import FundingTable, percentile
from backtest.signals import Indicators, Signal, generate_signals

Tables = Mapping[Scenario, Mapping[str, FundingTable]]


def common_length(candles: Mapping[str, Candles]) -> int:
    """Número de velas de la línea temporal común; todos los activos deben coincidir en tiempos."""
    series = list(candles.values())
    first = series[0]
    for c in series[1:]:
        if c.t != first.t:
            raise ValueError(f"{c.symbol} y {first.symbol} no comparten la misma línea temporal")
    return len(first)


def split_segments(n: int, fraction: float = DEV_FRACTION) -> tuple[Segment, Segment]:
    """Desarrollo = primer ``fraction`` de las velas; reservado = el resto (cronológico)."""
    cut = int(n * fraction)
    return Segment("desarrollo", 0, cut), Segment("reservado", cut, n)


def percentile_rank(sample: Sequence[float], x: float) -> float:
    """Porcentaje de la muestra por debajo de ``x`` (los empates cuentan la mitad), de 0 a 100."""
    below = sum(1 for v in sample if v < x)
    ties = sum(1 for v in sample if v == x)
    return 100.0 * (below + 0.5 * ties) / len(sample)


# --- robustez ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RobustnessRow:
    param: str
    factor: float  # 0.8 o 1.2
    value: float
    net_return: dict[Scenario, float]
    n_trades: dict[Scenario, int]


def robustness_variants(base: Params, pct: float) -> list[tuple[str, float, Params]]:
    """Cada parámetro por separado a ``1 - pct`` y ``1 + pct`` (el resto, en su valor base)."""
    out: list[tuple[str, float, Params]] = []
    for name in PARAM_NAMES:
        for factor in (1.0 - pct, 1.0 + pct):
            out.append((name, factor, perturb(base, name, factor)))
    return out


def run_robustness(
    candles: Mapping[str, Candles],
    tables: Tables,
    seg: Segment,
    base: Params,
    costs: Costs,
    account: Account,
    pct: float,
) -> list[RobustnessRow]:
    if seg.name == "reservado":
        raise ValueError("la robustez no se ejecuta sobre el tramo reservado (una sola pasada)")
    rows: list[RobustnessRow] = []
    for name, factor, params in robustness_variants(base, pct):
        signals = {a: generate_signals(c, params) for a, c in candles.items()}
        net: dict[Scenario, float] = {}
        count: dict[Scenario, int] = {}
        for scenario, fund in tables.items():
            res = simulate(candles, signals, fund, seg, params, costs, account)
            net[scenario] = sum(t.net_pnl for t in res.trades) / account.initial_capital
            count[scenario] = len(res.trades)
        rows.append(RobustnessRow(name, factor, float(getattr(params, name)), net, count))
    return rows


# --- azar --------------------------------------------------------------------------------


@dataclass(frozen=True)
class RandomSummary:
    n_sims: int
    net_returns: list[float]
    mean_trades: float

    @property
    def median(self) -> float:
        return statistics.median(self.net_returns)

    def quantile(self, q: float) -> float:
        return percentile(self.net_returns, q)

    @property
    def share_positive(self) -> float:
        return sum(1 for x in self.net_returns if x > 0.0) / len(self.net_returns)


@dataclass(frozen=True)
class _AssetRates:
    first: int  # primera vela elegible
    count: int  # velas elegibles
    p_long: float
    p_short: float


def _rates(sigs: Sequence[Signal], ind: Indicators, seg: Segment) -> _AssetRates:
    """Frecuencia de señales por vela elegible (todos los indicadores ya definidos)."""
    valid = [
        i
        for i in range(max(seg.start, 1), seg.end - 1)
        if not any(math.isnan(x[i]) for x in (ind.sma, ind.atr, ind.k, ind.d))
        and not math.isnan(ind.k[i - 1] + ind.d[i - 1])
    ]
    if not valid:
        return _AssetRates(seg.start, 0, 0.0, 0.0)
    first, last = valid[0], seg.end - 2
    in_range = [s for s in sigs if first <= s.idx <= last]
    count = last - first + 1
    return _AssetRates(
        first,
        count,
        sum(1 for s in in_range if s.side > 0) / count,
        sum(1 for s in in_range if s.side < 0) / count,
    )


def draw_random_signals(
    rng: random.Random, rates: _AssetRates, atr_values: Sequence[float]
) -> list[Signal]:
    """Entradas aleatorias: cada vela elegible dispara con la frecuencia observada de señales
    (largas y cortas por separado). Salta entre aciertos con una geométrica."""
    p = min(rates.p_long + rates.p_short, 0.999999)
    if p <= 0.0 or rates.count == 0:
        return []
    p_long_given = rates.p_long / (rates.p_long + rates.p_short)
    log_q = math.log1p(-p)
    out: list[Signal] = []
    pos = -1
    while True:
        pos += 1 + int(math.log1p(-rng.random()) / log_q)
        if pos >= rates.count:
            return out
        idx = rates.first + pos
        side = 1 if rng.random() < p_long_given else -1
        out.append(Signal(idx, side, atr_values[idx]))


def run_random(
    candles: Mapping[str, Candles],
    base_signals: Mapping[str, Sequence[Signal]],
    indicators: Mapping[str, Indicators],
    tables: Tables,
    seg: Segment,
    params: Params,
    costs: Costs,
    account: Account,
    n_sims: int,
    seed: int,
) -> dict[Scenario, RandomSummary]:
    """``n_sims`` carteras con entradas aleatorias de la misma frecuencia que la estrategia y las
    mismas reglas de salida, tamaño y costes. Cada simulación se evalúa con todos los escenarios
    de funding sobre las mismas entradas."""
    rng = random.Random(f"{seed}:{seg.name}")  # noqa: S311  (simulación, no criptografía)
    rates = {a: _rates(base_signals[a], indicators[a], seg) for a in candles}
    nets: dict[Scenario, list[float]] = {s: [] for s in tables}
    trades: dict[Scenario, int] = dict.fromkeys(tables, 0)
    for _ in range(n_sims):
        signals = {a: draw_random_signals(rng, rates[a], indicators[a].atr) for a in candles}
        for scenario, fund in tables.items():
            res = simulate(candles, signals, fund, seg, params, costs, account)
            nets[scenario].append(sum(t.net_pnl for t in res.trades) / account.initial_capital)
            trades[scenario] += len(res.trades)
    return {s: RandomSummary(n_sims, nets[s], trades[s] / n_sims) for s in tables}
