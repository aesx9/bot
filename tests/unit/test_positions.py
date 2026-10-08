from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from copybot.analysis.positions import Fill, fills_from_rows, reconstruct

T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)


def f(minute: int, side: str, size: str, price: str, sym: str = "PF_X") -> Fill:
    return Fill(T0 + timedelta(minutes=minute), sym, side, D(size), D(price))


def test_average_cost_and_partial_reductions() -> None:
    closed, open_ = reconstruct([
        f(0, "buy", "1", "100"), f(1, "buy", "1", "110"),  # entrada media 105
        f(2, "sell", "0.5", "120"),  # +7.5
        f(3, "sell", "1.5", "90"),  # -22.5
    ])
    [p] = closed
    assert open_ == {}
    assert (p.direction, p.max_size, p.realized_usd) == (1, D(2), D("-15"))
    assert p.entry_avg_total == D(105) and p.exit_avg == D("97.5")
    assert (p.opened_at, p.closed_at) == (T0, T0 + timedelta(minutes=3))


def test_short_position_profit() -> None:
    [p], _ = reconstruct([f(0, "sell", "2", "100"), f(5, "buy", "2", "80")])
    assert (p.direction, p.realized_usd) == (-1, D(40))


def test_fill_crossing_zero_is_split_into_two_positions() -> None:
    closed, open_ = reconstruct([f(0, "buy", "1", "100"), f(1, "sell", "3", "110")])
    assert [p.realized_usd for p in closed] == [D(10)]
    assert open_["PF_X"].size == D(-2) and open_["PF_X"].entry_avg == D(110)


def test_symbols_are_independent_and_numbered() -> None:
    closed, _ = reconstruct([f(0, "buy", "1", "10", "A"), f(1, "buy", "1", "20", "B"),
                             f(2, "sell", "1", "11", "A"), f(3, "sell", "1", "19", "B")])
    assert [(p.number, p.symbol, p.realized_usd) for p in closed] == [(1, "A", D(1)),
                                                                      (2, "B", D(-1))]


def test_rows_with_missing_price_are_skipped() -> None:
    rows = [{"timestamp_utc": T0.isoformat(), "mercado": "A", "lado": "buy", "tamano": "1",
             "precio": ""},
            {"timestamp_utc": T0.isoformat(), "mercado": "A", "lado": "buy", "tamano": "1",
             "precio": "5", "origen": "bot"}]
    [fill] = fills_from_rows(rows, price_key="precio", origin_key="origen")
    assert fill.origin == "bot"
