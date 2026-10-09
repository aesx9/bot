"""Utilidades de test: velas sintéticas y tablas de funding sin red."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from backtest.config import CANDLE_MS, HOUR_MS, Account, Costs, Params, Scenario
from backtest.data import Candles, FundingSeries
from backtest.engine import RunResult, Segment, simulate
from backtest.funding import FundingStats, FundingTable
from backtest.signals import Signal

T0 = 1_700_000_000_000 - (1_700_000_000_000 % CANDLE_MS)  # múltiplo de 4h

ZERO_COSTS = Costs(fee=0.0, slippage=0.0)
ACCOUNT = Account(initial_capital=1000.0, risk_per_trade=0.01, max_leverage=2.0)
PARAMS = Params()  # stop 2 ATR, take profit 3 ATR


def make_candles(
    ohlc: Sequence[tuple[float, float, float, float]], symbol: str = "PF_TEST"
) -> Candles:
    return Candles(
        symbol,
        [T0 + i * CANDLE_MS for i in range(len(ohlc))],
        [x[0] for x in ohlc],
        [x[1] for x in ohlc],
        [x[2] for x in ohlc],
        [x[3] for x in ohlc],
        [1.0] * len(ohlc),
    )


def flat(n: int, price: float = 100.0) -> list[tuple[float, float, float, float]]:
    """``n`` velas planas sin rango (nunca tocan stop ni take profit)."""
    return [(price, price, price, price)] * n


def random_walk(n: int, seed: int, symbol: str = "PF_TEST", vol: float = 0.01) -> Candles:
    rng = random.Random(seed)  # noqa: S311
    price, rows = 100.0, []
    for _ in range(n):
        o = price
        c = o * (1.0 + rng.gauss(0.0, vol))
        h = max(o, c) * (1.0 + abs(rng.gauss(0.0, vol / 2)))
        lo = min(o, c) * (1.0 - abs(rng.gauss(0.0, vol / 2)))
        rows.append((o, h, lo, c))
        price = c
    return make_candles(rows, symbol)


def no_funding(candles: Candles, scenario: Scenario = Scenario.CENTRAL) -> FundingTable:
    """Tabla sin dato real y con tasa imputada cero."""
    series = FundingSeries(candles.symbol, [T0 - HOUR_MS], [0.0], [0.0])
    stats = FundingStats(1, T0 - HOUR_MS, T0 - HOUR_MS, 0.0, 0.0)
    return FundingTable(candles, series, stats, scenario)


def run(
    candles: Mapping[str, Candles],
    signals: Mapping[str, Sequence[Signal]],
    *,
    seg: Segment | None = None,
    costs: Costs = ZERO_COSTS,
    account: Account = ACCOUNT,
    params: Params = PARAMS,
    funding: Mapping[str, FundingTable] | None = None,
    curves: bool = False,
) -> RunResult:
    first = next(iter(candles.values()))
    seg = seg or Segment("desarrollo", 0, len(first))
    funding = funding or {a: no_funding(c) for a, c in candles.items()}
    return simulate(candles, signals, funding, seg, params, costs, account, curves=curves)


def synthetic_dataset(
    n: int = 1500, symbols: tuple[str, ...] = ("PF_A", "PF_B", "PF_C"), seed: int = 7
) -> tuple[dict[str, Candles], dict[str, FundingSeries]]:
    """Tres paseos aleatorios con funding real solo en las últimas ~600 horas."""
    candles = {s: random_walk(n, seed + i, s, vol=0.02) for i, s in enumerate(symbols)}
    funding: dict[str, FundingSeries] = {}
    first_hour = n * 4 - 600
    for i, s in enumerate(symbols):
        hours = [h for h in range(first_hour, n * 4) if h % 97 != 5]  # con algún hueco horario
        rel = [1e-5 * (1.0 + 0.5 * ((h + i) % 5 - 2)) for h in hours]
        funding[s] = FundingSeries(
            s, [T0 + h * HOUR_MS for h in hours], [r * 100.0 for r in rel], rel
        )
    return candles, funding
