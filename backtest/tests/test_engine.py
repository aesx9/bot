"""Motor: entrada en la apertura siguiente, stop/TP pesimista, costes en ambos lados, tamaño."""

from __future__ import annotations

import pytest

from backtest.config import CANDLE_MS, Costs, Params
from backtest.engine import Segment, Trade
from backtest.signals import Signal, generate_signals
from backtest.tests.helpers import T0, ZERO_COSTS, flat, make_candles, random_walk, run

Candle = tuple[float, float, float, float]


def one_trade(
    after: list[Candle], side: int = 1, costs: Costs = ZERO_COSTS, atr: float = 1.0
) -> Trade:
    """Señal en la vela 2 (planas 0-2); la vela 3 es la de entrada, con apertura 100."""
    candles = {"A": make_candles(flat(3) + after, "A")}
    res = run(candles, {"A": [Signal(2, side, atr)]}, costs=costs)
    assert len(res.trades) == 1, res
    return res.trades[0]


# --- ejecución: entrada en la apertura de la vela siguiente --------------------------------


def test_entry_is_the_next_candle_open_not_the_signal_close() -> None:
    rows: list[Candle] = [*flat(2), (100.0, 100.0, 100.0, 105.0), (107.0, 107.0, 107.0, 107.0)]
    res = run({"A": make_candles(rows, "A")}, {"A": [Signal(2, 1, 1.0)]})
    t = res.trades[0]
    assert (t.signal_idx, t.entry_idx) == (2, 3)
    assert t.entry_ref == 107.0  # apertura de la vela 3, no el cierre (105) de la señal
    assert t.entry_time_ms == T0 + 3 * CANDLE_MS  # = cierre de la vela de la señal


def test_stop_and_take_profit_are_measured_from_the_entry_with_atr_of_the_signal_candle() -> None:
    t = one_trade([(100.0, 100.5, 99.5, 100.0)], atr=1.5)
    assert t.stop == pytest.approx(100.0 - 2 * 1.5)
    assert t.take_profit == pytest.approx(100.0 + 3 * 1.5)
    s = one_trade([(100.0, 100.5, 99.5, 100.0)], side=-1, atr=1.5)
    assert s.stop == pytest.approx(103.0) and s.take_profit == pytest.approx(95.5)


def test_signals_outside_the_segment_do_not_open_trades() -> None:
    c = {"A": make_candles(flat(12), "A")}
    sigs = {"A": [Signal(i, 1, 1.0) for i in (3, 4, 10, 11)]}
    res = run(c, sigs, seg=Segment("reservado", 5, 12))
    # idx 3 y 4 son anteriores al tramo; idx 11 es la última vela y no tiene vela de entrada
    assert [t.signal_idx for t in res.trades] == [10]
    assert res.raw_signals == 1


# --- stop y take profit --------------------------------------------------------------------


def test_long_take_profit() -> None:
    t = one_trade([(100.0, 100.0, 100.0, 100.0), (100.0, 103.5, 99.0, 103.0)])
    assert (t.exit_reason, t.exit_idx, t.exit_ref) == ("tp", 4, 103.0)
    assert t.qty == pytest.approx(5.0)  # 1 % de 1000 USD / (2 x ATR)
    assert t.net_pnl == pytest.approx(15.0)


def test_long_stop_loses_exactly_the_risked_fraction() -> None:
    t = one_trade([(100.0, 100.0, 100.0, 100.0), (100.0, 101.0, 97.5, 98.0)])
    assert (t.exit_reason, t.exit_ref) == ("stop", 98.0)
    assert t.net_pnl == pytest.approx(-10.0)  # 1 % de 1000


def test_short_take_profit_and_stop() -> None:
    tp = one_trade([(100.0, 100.0, 100.0, 100.0), (100.0, 101.0, 96.5, 97.0)], side=-1)
    assert (tp.exit_reason, tp.exit_ref) == ("tp", 97.0)
    assert tp.net_pnl == pytest.approx(15.0)
    st = one_trade([(100.0, 100.0, 100.0, 100.0), (100.0, 102.5, 99.0, 102.0)], side=-1)
    assert (st.exit_reason, st.exit_ref) == ("stop", 102.0)
    assert st.net_pnl == pytest.approx(-10.0)


@pytest.mark.parametrize("side", [1, -1])
def test_stop_and_take_profit_in_the_same_candle_the_stop_wins(side: int) -> None:
    """Criterio pesimista: si la vela toca ambos niveles, se asume que saltó antes el stop."""
    wide = (100.0, 110.0, 90.0, 100.0)  # alcanza stop (98/102) y take profit (103/97)
    t = one_trade([wide], side=side)
    assert t.exit_reason == "stop"
    assert t.exit_ref == (98.0 if side > 0 else 102.0)
    assert t.net_pnl == pytest.approx(-10.0)


def test_stop_can_trigger_in_the_entry_candle() -> None:
    t = one_trade([(100.0, 100.0, 97.0, 99.0)])
    assert t.exit_idx == t.entry_idx == 3 and t.exit_reason == "stop"


def test_gap_beyond_the_stop_fills_at_the_open() -> None:
    long = one_trade([(100.0, 100.0, 100.0, 100.0), (95.0, 96.0, 94.0, 95.0)])
    assert (long.exit_reason, long.exit_ref) == ("stop", 95.0)  # peor que el stop de 98
    short = one_trade([(100.0, 100.0, 100.0, 100.0), (105.0, 106.0, 104.0, 105.0)], side=-1)
    assert (short.exit_reason, short.exit_ref) == ("stop", 105.0)


def test_gap_beyond_the_take_profit_fills_at_the_take_profit_level() -> None:
    t = one_trade([(100.0, 100.0, 100.0, 100.0), (110.0, 111.0, 109.0, 110.0)])
    assert (t.exit_reason, t.exit_ref) == ("tp", 103.0)  # no se aprovecha la mejora del hueco


def test_open_position_is_closed_at_the_last_close_of_the_segment() -> None:
    t = one_trade(flat(3))
    assert (t.exit_reason, t.exit_idx, t.exit_ref) == ("fin_de_tramo", 5, 100.0)


# --- costes en ambos lados -----------------------------------------------------------------

COSTS = Costs(fee=0.0005, slippage=0.0005)


def test_long_costs_are_charged_on_entry_and_exit() -> None:
    t = one_trade([(100.0, 104.0, 100.0, 103.0)], costs=COSTS)
    entry_fill = 100.0 * 1.0005
    assert t.entry_fill == pytest.approx(entry_fill)
    assert t.take_profit == pytest.approx(entry_fill + 3.0)
    assert t.exit_reason == "tp" and t.exit_ref == pytest.approx(entry_fill + 3.0)
    assert t.exit_fill == pytest.approx(t.exit_ref * 0.9995)  # vende por debajo
    qty = 0.01 * 1000.0 / 2.0
    assert t.qty == pytest.approx(qty)
    assert t.fee_entry == pytest.approx(0.0005 * qty * t.entry_fill) and t.fee_entry > 0
    assert t.fee_exit == pytest.approx(0.0005 * qty * t.exit_fill) and t.fee_exit > 0
    assert t.slippage == pytest.approx(qty * 0.0005 * (t.entry_ref + t.exit_ref))
    assert t.gross_pnl == pytest.approx(qty * (t.exit_ref - t.entry_ref))
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.slippage - t.fee_entry - t.fee_exit)
    # y es lo mismo que liquidar a los precios ejecutados menos comisiones
    fills = qty * (t.exit_fill - t.entry_fill) - t.fee_entry - t.fee_exit
    assert t.net_pnl == pytest.approx(fills)


def test_short_costs_are_charged_on_entry_and_exit() -> None:
    t = one_trade([(100.0, 100.5, 96.0, 97.0)], side=-1, costs=COSTS)
    assert t.entry_fill == pytest.approx(100.0 * 0.9995)  # vende por debajo
    assert t.exit_fill == pytest.approx(t.exit_ref * 1.0005)  # recompra por encima
    qty = t.qty
    assert t.fee_entry > 0 and t.fee_exit > 0 and t.slippage > 0
    fills = -qty * (t.exit_fill - t.entry_fill) - t.fee_entry - t.fee_exit
    assert t.net_pnl == pytest.approx(fills)
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.slippage - t.fee_entry - t.fee_exit)


def test_costs_are_also_charged_on_a_stop_and_make_a_flat_trade_lose() -> None:
    free = one_trade(flat(3), costs=ZERO_COSTS)
    paid = one_trade(flat(3), costs=COSTS)
    assert free.net_pnl == pytest.approx(0.0)
    assert paid.net_pnl < 0
    assert paid.fee_entry > 0 and paid.fee_exit > 0 and paid.slippage > 0


# --- tamaño, apalancamiento y una posición por activo ---------------------------------------


def test_leverage_cap_reduces_the_size() -> None:
    t = one_trade([(100.0, 100.0, 100.0, 100.0)], atr=0.01)  # riesgo pediría 50.000 USD
    assert t.notional == pytest.approx(2000.0)  # 2x el capital de 1000


def test_leverage_cap_is_shared_by_all_assets() -> None:
    c = {a: make_candles(flat(6), a) for a in ("A", "B", "C")}
    sigs = {"A": [Signal(2, 1, 0.5)], "B": [Signal(2, 1, 0.25)], "C": [Signal(2, 1, 0.01)]}
    res = run(c, sigs)
    by = {t.asset: t for t in res.trades}
    assert by["A"].notional == pytest.approx(1000.0)  # 1 % de 1000 / (2 x 0,5) x 100
    assert by["B"].notional == pytest.approx(1000.0)  # pedía 2000; solo cabe lo que queda
    assert "C" not in by and res.skipped_margin == 1  # sin margen


def test_one_position_per_asset() -> None:
    c = {"A": make_candles(flat(8), "A")}
    res = run(c, {"A": [Signal(2, 1, 1.0), Signal(4, -1, 1.0)]})
    assert len(res.trades) == 1 and res.ignored_open == 1


def test_a_position_that_exits_in_the_entry_candle_still_counts_as_open() -> None:
    rows = flat(3) + [(100.0, 100.0, 100.0, 100.0), (100.0, 100.0, 100.0, 100.0)]
    rows += [(100.0, 101.0, 97.0, 98.0), (98.0, 98.0, 98.0, 98.0), (98.0, 98.0, 98.0, 98.0)]
    # Posición abierta en la 3; su stop salta en la vela 5. Una señal en la 4 entraría en la 5.
    res = run({"A": make_candles(rows, "A")}, {"A": [Signal(2, 1, 1.0), Signal(4, 1, 1.0)]})
    assert [t.exit_idx for t in res.trades] == [5] and res.ignored_open == 1


def test_a_new_position_can_open_after_the_previous_one_closed() -> None:
    rows = flat(3) + [(100.0, 100.0, 97.0, 99.0)] + [(99.0, 99.0, 99.0, 99.0)] * 4
    res = run({"A": make_candles(rows, "A")}, {"A": [Signal(2, 1, 1.0), Signal(4, 1, 1.0)]})
    first, second = res.trades
    assert first.exit_idx == 3 and second.entry_idx == 5
    assert first.net_pnl == pytest.approx(-10.0)
    assert second.balance_before == pytest.approx(990.0)  # el riesgo se calcula sobre el capital
    assert second.qty == pytest.approx(0.01 * 990.0 / 2.0)


# --- curva de capital -----------------------------------------------------------------------


def test_equity_curve_is_marked_to_market_and_ends_at_the_net_result() -> None:
    rows = flat(3) + [(100.0, 101.0, 99.5, 101.0), (101.0, 104.0, 100.0, 103.0)] + flat(2, 103.0)
    res = run({"A": make_candles(rows, "A")}, {"A": [Signal(2, 1, 1.0)]}, curves=True)
    assert res.curve is not None and res.asset_pnl is not None
    assert len(res.curve) == len(rows)
    assert res.curve[:3] == [1000.0] * 3
    assert res.curve[3] == pytest.approx(1005.0)  # 5 unidades x (101 - 100) latente
    assert res.curve[4] == pytest.approx(1015.0)  # take profit en 103
    assert res.curve[-1] == pytest.approx(1000.0 + res.trades[0].net_pnl)
    assert res.asset_pnl["A"][-1] == pytest.approx(res.trades[0].net_pnl)


def test_multi_asset_equity_curve_adds_up_to_the_sum_of_net_results() -> None:
    candles = {a: random_walk(900, seed=i, symbol=a, vol=0.02) for i, a in enumerate("ABC")}
    sigs = {a: generate_signals(c, Params()) for a, c in candles.items()}
    res = run(candles, sigs, costs=Costs(), curves=True)
    assert len(res.trades) > 20 and res.curve is not None and res.asset_pnl is not None
    assert res.curve[-1] == pytest.approx(1000.0 + sum(t.net_pnl for t in res.trades))
    for a in candles:
        assert res.asset_pnl[a][-1] == pytest.approx(
            sum(t.net_pnl for t in res.trades if t.asset == a)
        )
    for j in (100, 500, 899):  # la cartera es la suma de sus partes en cada vela
        assert sum(res.asset_pnl[a][j] for a in candles) == pytest.approx(res.curve[j] - 1000.0)
