"""Funding: real donde existe, imputado según escenario donde no, signo por lado."""

from __future__ import annotations

import pytest

from backtest.config import CANDLE_MS, HOUR_MS, Scenario
from backtest.data import FundingSeries
from backtest.funding_cost import FundingStats, FundingTable, funding_stats, percentile
from backtest.signals import Signal
from backtest.tests.helpers import T0, flat, make_candles, no_funding, run

OPEN = 100.0


def table(
    n_candles: int,
    hours: dict[int, float],
    scenario: Scenario,
    median: float = 0.0,
    p75: float = 0.0,
) -> FundingTable:
    """Tabla sobre velas planas a 100 USD. ``hours`` mapea hora absoluta -> fundingRate real."""
    c = make_candles(flat(n_candles, OPEN), "A")
    ts = sorted(hours)
    series = FundingSeries("A", [T0 + h * HOUR_MS for h in ts], [hours[h] for h in ts],
                           [hours[h] / OPEN for h in ts])
    stats = FundingStats(len(ts), series.t[0], series.t[-1], median, p75)
    return FundingTable(c, series, stats, scenario)


def test_percentile_linear_interpolation() -> None:
    xs = [4.0, 1.0, 3.0, 2.0]
    assert percentile(xs, 0) == 1.0 and percentile(xs, 100) == 4.0
    assert percentile(xs, 50) == pytest.approx(2.5)
    assert percentile(xs, 75) == pytest.approx(3.25)
    assert percentile([7.0], 75) == 7.0
    with pytest.raises(ValueError):
        percentile([], 50)


def test_funding_stats_use_signed_relative_rates() -> None:
    rel = [-2e-5, 1e-5, 3e-5, 5e-5, 9e-5]
    s = funding_stats(FundingSeries("A", [T0 + i * HOUR_MS for i in range(5)], [0.0] * 5, rel))
    assert s.median_rel == pytest.approx(3e-5) and s.p75_rel == pytest.approx(5e-5)


def test_real_funding_long_pays_short_receives_when_positive() -> None:
    t = table(3, {h: 0.5 for h in range(12)}, Scenario.CENTRAL)  # 3 velas x 4 h, todas reales
    assert t.cost_per_unit(1, 0, 0) == pytest.approx((2.0, 0.0))  # 4 h x 0,5 USD
    assert t.cost_per_unit(-1, 0, 0) == pytest.approx((-2.0, 0.0))
    assert t.cost_per_unit(1, 0, 2) == pytest.approx((6.0, 0.0))
    assert t.hours(0, 2) == (12, 0)


def test_real_funding_negative_rate_reverses_the_sides() -> None:
    t = table(1, {h: -0.25 for h in range(4)}, Scenario.PESIMISTA)
    assert t.cost_per_unit(1, 0, 0)[0] == pytest.approx(-1.0)  # el largo cobra
    assert t.cost_per_unit(-1, 0, 0)[0] == pytest.approx(1.0)  # el corto paga


def test_central_scenario_imputes_signed_median_on_notional() -> None:
    c = make_candles(flat(2, OPEN), "A")
    far = FundingSeries("A", [T0 - 100 * HOUR_MS], [1.0], [1e-5])  # fuera del rango de velas
    stats = FundingStats(1, far.t[0], far.t[0], median_rel=1e-5, p75_rel=2e-5)
    t = FundingTable(c, far, stats, Scenario.CENTRAL)
    long_real, long_imp = t.cost_per_unit(1, 0, 1)
    short_real, short_imp = t.cost_per_unit(-1, 0, 1)
    assert long_real == short_real == 0.0
    assert long_imp == pytest.approx(1e-5 * 8 * OPEN)  # 8 horas x 100 USD
    assert short_imp == pytest.approx(-1e-5 * 8 * OPEN)  # el corto cobra
    assert t.hours(0, 1) == (0, 8)


@pytest.mark.parametrize("p75", [3e-5, -3e-5])
def test_pessimistic_scenario_is_always_against_the_position_with_abs_p75(p75: float) -> None:
    c = make_candles(flat(2, OPEN), "A")
    far = FundingSeries("A", [T0 - 100 * HOUR_MS], [1.0], [1e-5])
    stats = FundingStats(1, far.t[0], far.t[0], median_rel=-9e-5, p75_rel=p75)
    t = FundingTable(c, far, stats, Scenario.PESIMISTA)
    expected = 3e-5 * 8 * OPEN
    assert t.cost_per_unit(1, 0, 1)[1] == pytest.approx(expected)
    assert t.cost_per_unit(-1, 0, 1)[1] == pytest.approx(expected)  # el corto también paga


def test_partial_coverage_mixes_real_and_imputed_hours() -> None:
    # Velas 0-2. Funding real solo en la vela 1 (horas 4-7) salvo una hora que falta (la 6).
    hours = {4: 1.0, 5: 1.0, 7: 1.0}
    t = table(3, hours, Scenario.CENTRAL, median=1e-5)
    assert t.hours(0, 0) == (0, 4)
    assert t.hours(1, 1) == (3, 1)
    assert t.hours(2, 2) == (0, 4)
    assert t.hours(0, 2) == (3, 9)
    real, imp = t.cost_per_unit(1, 0, 2)
    assert real == pytest.approx(3.0)
    assert imp == pytest.approx(1e-5 * 9 * OPEN)


def test_engine_charges_funding_for_every_hour_of_every_open_candle() -> None:
    # Entrada en la vela 3 (apertura) y salida en la vela 4: 2 velas x 4 h = 8 h.
    rows = flat(3) + [(100.0, 100.0, 100.0, 100.0), (100.0, 104.0, 100.0, 103.0)] + flat(2, 103.0)
    c = make_candles(rows, "A")
    hours = {h: 0.1 for h in range(3 * 4, 5 * 4)}  # reales justo en las velas 3 y 4
    far = FundingSeries("A", [T0 + h * HOUR_MS for h in hours], list(hours.values()),
                        [0.001] * len(hours))
    stats = FundingStats(len(hours), far.t[0], far.t[-1], 0.0, 0.0)
    fund = {"A": FundingTable(c, far, stats, Scenario.PESIMISTA)}
    sig = {"A": [Signal(2, 1, 1.0)]}
    base = run({"A": c}, sig, funding={"A": no_funding(c)}).trades[0]
    with_funding = run({"A": c}, sig, funding=fund).trades[0]
    qty = with_funding.qty
    assert with_funding.funding_hours_real == 8 and with_funding.funding_hours_imputed == 0
    assert with_funding.funding_real == pytest.approx(qty * 0.1 * 8)
    assert with_funding.funding_imputed == 0.0
    assert with_funding.net_pnl == pytest.approx(base.net_pnl - qty * 0.1 * 8)


def test_engine_short_receives_positive_real_funding() -> None:
    rows = flat(3) + [(100.0, 100.0, 96.0, 97.0)] + flat(2, 97.0)
    c = make_candles(rows, "A")
    hours = {h: 0.2 for h in range(12, 16)}
    times = [T0 + h * HOUR_MS for h in hours]
    series = FundingSeries("A", times, list(hours.values()), [0.002] * 4)
    stats = FundingStats(4, times[0], times[-1], 0.0, 0.0)
    fund = {"A": FundingTable(c, series, stats, Scenario.CENTRAL)}
    t = run({"A": c}, {"A": [Signal(2, -1, 1.0)]}, funding=fund).trades[0]
    assert t.funding_real == pytest.approx(-t.qty * 0.2 * 4)  # negativo = ingreso
    assert t.exit_time_ms == T0 + 4 * CANDLE_MS
