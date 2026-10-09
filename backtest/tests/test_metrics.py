from __future__ import annotations

import math
import statistics

import pytest

from backtest.config import CANDLE_MS, DAY_MS, Account, Costs, Scenario
from backtest.engine import Segment
from backtest.metrics import (
    buy_and_hold,
    daily_equity,
    max_drawdown,
    profit_factor,
    sharpe_daily,
    summarize,
)
from backtest.signals import Signal
from backtest.tests.helpers import ACCOUNT, ZERO_COSTS, flat, make_candles, no_funding, run


def test_max_drawdown_from_running_peak() -> None:
    assert max_drawdown([100, 120, 90, 130, 65]) == pytest.approx(0.5)
    assert max_drawdown([100, 101, 102]) == 0.0
    assert max_drawdown([100, 80, 100, 90]) == pytest.approx(0.2)


def test_profit_factor() -> None:
    assert profit_factor([10.0, -5.0, 5.0, -5.0]) == pytest.approx(1.5)
    assert profit_factor([3.0, 2.0]) == math.inf
    assert math.isnan(profit_factor([]))
    assert profit_factor([-1.0, -2.0]) == 0.0


def test_daily_equity_takes_the_last_close_of_each_utc_day() -> None:
    day0 = 19_000 * DAY_MS
    times = [day0 + i * CANDLE_MS for i in range(12)]  # dos días de 6 velas
    assert daily_equity([float(i) for i in range(12)], times) == [5.0, 11.0]


def test_sharpe_matches_a_manual_computation() -> None:
    day0 = 19_000 * DAY_MS
    daily = [101.0, 99.0, 102.0, 103.0, 100.0]
    equity = [v for v in daily for _ in range(6)]
    times = [day0 + i * CANDLE_MS for i in range(len(equity))]
    series = [100.0, *daily]
    rets = [b / a - 1 for a, b in zip(series, series[1:], strict=False)]
    expected = statistics.fmean(rets) / statistics.stdev(rets) * math.sqrt(365)
    assert sharpe_daily(equity, times, 100.0) == pytest.approx(expected)


def test_sharpe_is_nan_when_there_is_no_variation_or_too_few_days() -> None:
    day0 = 19_000 * DAY_MS
    flat_eq = [100.0] * 24
    times = [day0 + i * CANDLE_MS for i in range(24)]
    assert math.isnan(sharpe_daily(flat_eq, times, 100.0))
    assert math.isnan(sharpe_daily([100.0, 101.0], times[:2], 100.0))


def test_summarize_counts_wins_and_adds_up_costs() -> None:
    rows = flat(3) + [(100.0, 100.0, 97.0, 99.0)]  # stop
    rows += [(99.0, 99.0, 99.0, 99.0)] * 2 + [(99.0, 103.0, 99.0, 102.0)] + flat(3, 102.0)
    costs = Costs(fee=0.0005, slippage=0.0005)
    res = run({"A": make_candles(rows, "A")}, {"A": [Signal(2, 1, 1.0), Signal(4, 1, 1.0)]},
              costs=costs, curves=True)
    assert len(res.trades) == 2
    s = summarize(res.trades, res.curve, list(range(len(rows))), ACCOUNT.initial_capital)
    assert s.n_trades == 2
    assert s.win_rate == pytest.approx(0.5 if res.trades[1].net_pnl > 0 else 0.0)
    assert s.fees == pytest.approx(sum(t.fee_entry + t.fee_exit for t in res.trades))
    assert s.slippage == pytest.approx(sum(t.slippage for t in res.trades))
    assert s.net_pnl == pytest.approx(
        s.gross_pnl - s.fees - s.slippage - s.funding_real - s.funding_imputed
    )
    assert s.net_return == pytest.approx(s.net_pnl / 1000.0)
    assert 0.0 < s.max_drawdown < 0.05


def test_buy_and_hold_without_costs_is_the_price_return() -> None:
    rows = [(100.0, 100.0, 100.0, 100.0)] * 2 + [(100.0, 125.0, 90.0, 110.0)]
    c = make_candles(rows, "A")
    bh = buy_and_hold(c, Segment("x", 0, 3), no_funding(c, Scenario.CENTRAL), ZERO_COSTS,
                      Account(initial_capital=1000.0))
    assert bh.gross_return == pytest.approx(0.10) and bh.net_return == pytest.approx(0.10)
    assert bh.max_drawdown == 0.0


def test_buy_and_hold_charges_both_sides_and_funding() -> None:
    c = make_candles([(100.0, 100.0, 100.0, 100.0)] * 3, "A")
    costs = Costs(fee=0.001, slippage=0.001)
    bh = buy_and_hold(c, Segment("x", 0, 3), no_funding(c, Scenario.CENTRAL), costs,
                      Account(initial_capital=1000.0))
    assert bh.gross_return == 0.0
    # compra a 100,1 y vende a 99,9 (2 x 0,1 %), más 0,1 % de comisión por cada lado
    qty = 1000.0 / 100.1
    expected = (qty * (99.9 - 100.1) - 0.001 * qty * (100.1 + 99.9)) / 1000.0
    assert bh.net_return == pytest.approx(expected)
