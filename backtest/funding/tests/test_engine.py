"""Motor: señal sin lookahead, entradas y salidas, funding, costes, base, liquidación y
reequilibrio, con activos sintéticos de precio plano (salvo donde se indica)."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

import pytest

from backtest.funding.config import THRESHOLDS, Strategy, Thresholds
from backtest.funding.engine import Asset, ExitReason, RunResult, Spec, simulate, spread_signal
from backtest.funding.tests.helpers import (
    hourly,
    make_asset,
    make_leg,
    make_spec,
    spread_asset,
    step_spread,
)

TH_A = THRESHOLDS[Strategy.A]  # entrada 20 %, salida 5 %
N = 120


def _run(
    assets: Sequence[Asset],
    spec: Spec | None = None,
    th: Thresholds = TH_A,
    start: int = 24,
    end: int = N,
) -> RunResult:
    return simulate(assets, [spread_signal(a) for a in assets], start, end, th,
                    spec or make_spec())


# --- señal --------------------------------------------------------------------------------


def test_signal_is_mean_of_previous_24_settled_hours_annualised() -> None:
    rng = random.Random(1)  # noqa: S311
    spread = [rng.gauss(0.0, 1e-5) for _ in range(60)]
    a = spread_asset("X", spread)
    sig = spread_signal(a)
    assert all(math.isnan(x) for x in sig[:24])
    for i in range(24, 60):
        assert sig[i] == pytest.approx(sum(spread[i - 24:i]) / 24 * 8760, rel=1e-9, abs=1e-12)


def test_signal_never_uses_the_current_or_future_hours() -> None:
    rng = random.Random(2)  # noqa: S311
    spread = [rng.gauss(0.0, 3e-5) for _ in range(N)]
    base = spread_signal(spread_asset("X", spread))
    k = 70
    altered = spread[:k] + [x * -5.0 + 1e-3 for x in spread[k:]]
    sig = spread_signal(spread_asset("X", altered))
    assert sig[: k + 1] == base[: k + 1]  # la hora k solo se conoce al decidir en k + 1
    assert sig[k + 1] != base[k + 1]


def test_simulation_up_to_an_hour_ignores_everything_after_it() -> None:
    rng = random.Random(3)  # noqa: S311
    spread = [hourly(0.4) + rng.gauss(0.0, 3e-5) for _ in range(N)]
    k = 90
    altered = spread[:k] + [-x for x in spread[k:]]
    r1 = _run([spread_asset("X", spread)], end=k)
    r2 = _run([spread_asset("X", altered)], end=k)
    assert r1 == r2


def test_missing_rate_counts_as_zero_funding_in_the_signal() -> None:
    spread = [hourly(0.3)] * 60
    spread[30] = math.nan
    sig = spread_signal(spread_asset("X", spread))
    assert sig[30] == pytest.approx(0.3)
    assert all(x == pytest.approx(0.3 * 23 / 24) for x in sig[31:55])
    assert sig[55] == pytest.approx(0.3)


def test_missing_rate_pays_nothing_and_is_counted() -> None:
    spread = [hourly(0.4)] * N
    spread[50] = math.nan
    (p,) = _run([spread_asset("X", spread)]).positions
    (q,) = _run([spread_asset("X", [hourly(0.4)] * N)]).positions
    assert p.funding_hours_missing == 1 and q.funding_hours_missing == 0
    assert q.funding_received - p.funding_received == pytest.approx(3.0 * 100.0 * hourly(0.4))


# --- entradas y salidas -------------------------------------------------------------------


def test_enters_above_entry_and_exits_below_exit_threshold_with_exact_funding() -> None:
    a = spread_asset("X", step_spread(N, 0.36, until=60))
    r = _run([a])
    (p,) = r.positions
    # Entra en 24 (media 36 %); sale en 81, primera hora con media < 5 % (3/24 × 36 % = 4,5 %).
    assert (p.entry_t, p.exit_t) == (a.t[24], a.t[81])
    assert p.hours == 57 and p.direction == 1 and p.complete
    assert p.reason is ExitReason.SIGNAL
    assert p.funding_received == pytest.approx(36 * 300.0 * hourly(0.36))
    assert p.funding_paid == 0.0 and p.fees == 0.0 and p.slippage == 0.0
    assert r.equity[-1] - r.initial_capital == pytest.approx(p.net_pnl)


def test_negative_spread_reverses_legs_in_a_and_is_ignored_in_b() -> None:
    a = spread_asset("X", [-x for x in step_spread(N, 0.36, until=60)])
    (p,) = _run([a]).positions
    assert p.direction == -1
    assert p.funding_received == pytest.approx(36 * 300.0 * hourly(0.36))
    assert _run([a], spec=make_spec(two_sided=False)).positions == []


def test_spread_between_thresholds_does_not_enter() -> None:
    a = spread_asset("X", [hourly(0.15)] * N)
    r = _run([a])
    assert r.positions == [] and r.equity[-1] == r.initial_capital


def test_fees_and_slippage_on_both_legs_entry_and_exit() -> None:
    a = spread_asset("X", step_spread(N, 0.36, until=60), fees=(0.001, 0.002))
    r = _run([a], spec=make_spec(slippage=0.0005))
    (p,) = r.positions
    q = 3.0  # 300 USD / 100
    assert p.fees == pytest.approx(q * (0.001 + 0.002) * (100.05 + 99.95))
    assert p.slippage == pytest.approx(4 * q * 100.0 * 0.0005)
    assert p.basis_pnl == pytest.approx(0.0)
    assert p.net_pnl == pytest.approx(p.funding_received - p.fees - p.slippage)
    assert r.equity[-1] - r.initial_capital == pytest.approx(p.net_pnl)


def test_basis_change_between_legs_enters_the_pnl() -> None:
    prices2 = [100.0 if i < 50 else 110.0 for i in range(N)]
    a = spread_asset("X", step_spread(N, 0.36, until=60), prices2=prices2)
    r = _run([a])
    (p,) = r.positions
    assert p.basis_pnl == pytest.approx(-3.0 * 10.0)  # corto en la pierna que sube
    assert r.equity[-1] - r.initial_capital == pytest.approx(p.net_pnl)


def test_at_most_three_positions_choosing_the_largest_spreads() -> None:
    assets = [spread_asset(f"X{k}", [hourly(0.3 + 0.1 * k)] * N) for k in range(5)]
    r = _run(assets)
    assert sorted(p.asset for p in r.positions) == ["X2", "X3", "X4"]
    assert all(p.reason is ExitReason.END and not p.complete for p in r.positions)
    assert r.skipped_slots == 2 * (N - 24)


def test_entry_beyond_max_leverage_is_skipped() -> None:
    a = spread_asset("X", [hourly(0.4)] * N)
    r = _run([a], spec=make_spec(leverage=0.1))
    assert r.positions == [] and r.skipped_margin == N - 24


def test_segment_end_closes_at_last_close_and_is_not_a_cycle() -> None:
    a = spread_asset("X", [hourly(0.4)] * N)
    (p,) = _run([a]).positions
    assert p.reason is ExitReason.END and not p.complete
    assert p.hours == N - 24 and p.exit_t == a.t[-1] + 3_600_000


def test_rejects_unaligned_assets_and_out_of_range_segments() -> None:
    a = spread_asset("X", [0.0] * N)
    b = spread_asset("Y", [0.0] * (N + 1))
    with pytest.raises(ValueError, match="rejilla"):
        _run([a, b])
    with pytest.raises(ValueError, match="tramo"):
        _run([a], start=0)


# --- liquidación y reequilibrio -----------------------------------------------------------


def test_spike_against_the_short_leg_liquidates_its_account() -> None:
    a = spread_asset("X", [hourly(0.4)] * N, highs2={40: 300.0})
    r = _run([a])
    (liq,) = r.liquidations
    assert liq.venue == "v2" and liq.assets == ("X",) and liq.t == a.t[40]
    assert liq.penalty == pytest.approx(0.01 * 3.0 * 300.0)
    p = r.positions[0]
    assert p.reason is ExitReason.LIQUIDATION and not p.complete
    assert p.basis_pnl == pytest.approx(-3.0 * 200.0)
    assert liq.loss == pytest.approx(p.net_pnl)
    # La cuenta queda sin capital: las entradas siguientes se descartan por margen.
    assert len(r.positions) == 1 and r.skipped_margin > 0
    assert r.equity[-1] - r.initial_capital == pytest.approx(p.net_pnl)


def test_spot_leg_without_margin_is_never_liquidated() -> None:
    n = N
    leg1 = make_leg(n, "spot", lows={40: 1.0}, mm=None)  # el spot se desploma en una vela
    leg2 = make_leg(n, "v2", rates=[hourly(0.4)] * n)
    a = make_asset("X", leg1, leg2)
    spec = make_spec(two_sided=False)
    spec = type(spec)(**{**spec.__dict__, "initial": {"spot": 500.0, "v2": 500.0},
                         "leverage": {"spot": 1.0, "v2": 1.0}})
    r = _run([a], spec=spec)
    assert r.liquidations == []


def test_margin_rebalance_moves_half_the_gap_and_pays_the_transfer() -> None:
    prices2 = [100.0 if i < 30 else 220.0 for i in range(N)]
    a = spread_asset("X", [hourly(0.4)] * N, prices2=prices2)
    r = _run([a], spec=make_spec(rebalance=True, transfer_cost=5.0, initial_transfers=1))
    # v2 = 500 − 3 × 120 = 140 < 0,5 × media (320): se transfiere (500 − 140) / 2 = 180.
    assert r.transfers == 2  # la inicial y un reequilibrio
    assert r.transfer_cost == pytest.approx(10.0)
    assert r.liquidations == []
    (p,) = r.positions
    assert r.equity[-1] - r.initial_capital == pytest.approx(p.net_pnl - 10.0)


def _delayed_spec(delay: int) -> Spec:
    spec = make_spec(rebalance=True, transfer_cost=5.0, initial_transfers=1)
    return type(spec)(**{**spec.__dict__, "transfer_delay_hours": delay})


def test_rebalance_transfer_takes_two_hours_and_is_not_margin_meanwhile() -> None:
    prices2 = [100.0 if i < 30 else 220.0 for i in range(N)]
    # Sale de v1 al cierre de la hora 30 y llega a v2 al cierre de la 32. Un máximo de 290 en la
    # pierna corta liquida v2 sin los 175 USD en tránsito (800 − 3·290 < 0,03·290) y no con ellos.
    in_transit = spread_asset("X", [hourly(0.4)] * N, prices2=prices2, highs2={31: 290.0})
    r = _run([in_transit], spec=_delayed_spec(2))
    (liq,) = r.liquidations
    assert liq.venue == "v2" and liq.t == in_transit.t[31]
    assert _run([in_transit], spec=_delayed_spec(0)).liquidations == []  # instantáneo: no
    arrived = spread_asset("X", [hourly(0.4)] * N, prices2=prices2, highs2={33: 290.0})
    r = _run([arrived], spec=_delayed_spec(2))
    assert r.liquidations == []
    # Un solo reequilibrio aunque v2 siga por debajo del umbral mientras el importe viaja; en
    # tránsito sigue contando en el capital total.
    assert r.transfers == 2
    (p,) = r.positions
    assert r.equity[-1] - r.initial_capital == pytest.approx(p.net_pnl - 10.0)
    flat = _run([spread_asset("X", [hourly(0.4)] * N, prices2=prices2)], spec=_delayed_spec(2))
    instant = _run([spread_asset("X", [hourly(0.4)] * N, prices2=prices2)],
                   spec=_delayed_spec(0))
    assert flat.equity == pytest.approx(instant.equity)


def test_transfer_still_in_transit_at_the_end_counts_as_capital() -> None:
    prices2 = [100.0 if i < 30 else 220.0 for i in range(N)]
    a = spread_asset("X", [hourly(0.4)] * N, prices2=prices2)
    r = _run([a], spec=_delayed_spec(2), end=32)  # la transferencia llegaría al cierre de la 32
    assert r.transfers == 2
    (p,) = r.positions
    assert r.equity[-1] - r.initial_capital == pytest.approx(p.net_pnl - 10.0)


def test_no_rebalance_when_disabled() -> None:
    prices2 = [100.0 if i < 30 else 220.0 for i in range(N)]
    a = spread_asset("X", [hourly(0.4)] * N, prices2=prices2)
    assert _run([a], spec=make_spec(transfer_cost=5.0)).transfers == 0


def test_robust_thresholds_object() -> None:
    th = Thresholds(0.2, 0.05).scaled(1.2)
    assert th == Thresholds(pytest.approx(0.24), pytest.approx(0.06))  # type: ignore[arg-type]
