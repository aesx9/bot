"""Runner: tramos, robustez solo en desarrollo, criterios, veredicto y episodios de B."""

from __future__ import annotations

from dataclasses import replace

import pytest

from backtest.funding.config import (
    THRESHOLDS,
    Account,
    Costs,
    Criteria,
    SpotFee,
    Strategy,
    Thresholds,
)
from backtest.funding.engine import RunResult, spread_signal
from backtest.funding.prepare import Window
from backtest.funding.runner import (
    Stats,
    Variant,
    Verdict,
    cycle_cost_fraction,
    days_to_cover,
    evaluate,
    high_funding_episodes,
    max_asset_share,
    robustness_variants,
    run_robustness,
    run_strategy,
    spec_a,
    spec_b,
    split,
    summarize,
    verdict,
)
from backtest.funding.tests.helpers import T0, hourly, make_spec, spread_asset

GOOD = replace(
    summarize(RunResult([], [550.0], [T0], 550.0, 0, 0.0, [], 0, 0, 0)),
    annual_return=0.08, max_drawdown=0.01, cycles=12, per_asset={"A": 3.0, "B": 3.0},
)


def _variants(annual: float) -> list[Variant]:
    return [Variant(label, th, replace(GOOD, annual_return=annual))
            for label, th in robustness_variants(THRESHOLDS[Strategy.A], 0.2)]


def _verdict(dev: Stats, res: Stats | None, rob: list[Variant]) -> Verdict | None:
    return verdict(evaluate(dev, res, rob, Criteria()), res is not None)


# --- configuración de cada estrategia -----------------------------------------------------


def test_spec_a_splits_margin_after_the_initial_transfer() -> None:
    s = spec_a(Account(), Costs())
    assert s.initial == {"kraken_futures": 273.5, "hyperliquid": 273.5}
    assert s.notional == pytest.approx(2.0 * 273.5 / 3 / 1.02)
    assert s.initial_transfers == 1 and s.transfer_cost == 3.0 and s.rebalance and s.two_sided
    assert s.transfer_delay_hours == 2


def test_spec_b_half_spot_half_margin_one_sided() -> None:
    s = spec_b(Account(), Costs())
    assert s.initial == {"kraken_spot": 275.0, "kraken_futures": 275.0}
    assert s.leverage == {"kraken_spot": 1.0, "kraken_futures": 1.0}
    assert not s.two_sided and s.initial_transfers == 0 and not s.rebalance


def test_split_is_chronological_70_30_after_warmup() -> None:
    dev, res = split(24, 1024)
    assert (dev.start, dev.stop, res.start, res.stop) == (24, 724, 724, 1024)


def test_robustness_varies_each_threshold_and_both_by_20_pct() -> None:
    v = dict(robustness_variants(Thresholds(0.20, 0.05), 0.2))
    assert len(v) == 6
    assert v["entrada -20 %"] == Thresholds(pytest.approx(0.16), 0.05)  # type: ignore[arg-type]
    assert v["salida +20 %"] == Thresholds(0.20, pytest.approx(0.06))  # type: ignore[arg-type]
    assert v["ambos +20 %"] == Thresholds(pytest.approx(0.24), pytest.approx(0.06))  # type: ignore[arg-type]


# --- criterios y veredicto ----------------------------------------------------------------


def test_all_criteria_met_is_approved() -> None:
    assert _verdict(GOOD, GOOD, _variants(0.05)) is Verdict.APPROVED


def test_fewer_than_10_cycles_is_inconclusive_never_approved() -> None:
    res = replace(GOOD, cycles=9)
    assert _verdict(GOOD, res, _variants(0.05)) is Verdict.INCONCLUSIVE
    bad = replace(res, annual_return=-0.5)
    assert _verdict(GOOD, bad, _variants(0.05)) is Verdict.INCONCLUSIVE
    checks = evaluate(GOOD, bad, _variants(0.05), Criteria())
    assert checks[0].passed is False  # el resto de criterios se informa igual


@pytest.mark.parametrize(
    ("dev", "res", "rob"),
    [
        (GOOD, replace(GOOD, annual_return=0.059), 0.05),  # reservado < 6 %
        (replace(GOOD, annual_return=0.03), GOOD, 0.05),  # desarrollo no > 3 %
        (GOOD, replace(GOOD, max_drawdown=0.05), 0.05),  # DD no < 5 %
        (replace(GOOD, max_drawdown=0.06), GOOD, 0.05),  # DD de desarrollo
        (GOOD, replace(GOOD, liquidations=1), 0.05),
        (GOOD, GOOD, 0.03),  # robustez no > 3 %
        (GOOD, replace(GOOD, per_asset={"A": 5.1, "B": 4.9}), 0.05),  # un activo > 50 %
        (GOOD, replace(GOOD, per_asset={"A": -1.0}), 0.05),  # sin beneficio
    ],
)
def test_any_failed_criterion_rejects(dev: Stats, res: Stats, rob: float) -> None:
    assert _verdict(dev, res, _variants(rob)) is Verdict.REJECTED


def test_without_reserved_there_is_no_verdict() -> None:
    checks = evaluate(GOOD, None, _variants(0.05), Criteria())
    assert verdict(checks, False) is None
    assert checks[0].passed is None and checks[-1].passed is None


def test_max_asset_share() -> None:
    assert max_asset_share({"A": 3.0, "B": 1.0}) == ("A", 0.75)
    name, share = max_asset_share({"A": 3.0, "B": -4.0})
    assert name == "" and share != share  # NaN


# --- aislamiento del reservado ------------------------------------------------------------


def _assets(flip_after: int | None = None) -> list:  # type: ignore[type-arg]
    n = 1024
    out = []
    for k in range(2):
        spread = [hourly(0.4) if (i // (60 + 7 * k)) % 2 == 0 else 0.0 for i in range(n)]
        if flip_after is not None:
            spread = spread[:flip_after] + [-x for x in spread[flip_after:]]
        out.append(spread_asset(f"X{k}", spread))
    return out


def test_reserved_data_never_changes_development_or_robustness() -> None:
    w = Window(T0, T0 + 1000 * 3_600_000)
    spec = make_spec()
    a = run_strategy(Strategy.A, _assets(), w, [], spec, only_dev=False)
    b = run_strategy(Strategy.A, _assets(flip_after=724), w, [], spec, only_dev=False)
    c = run_strategy(Strategy.A, _assets(), w, [], spec, only_dev=True)
    assert a.dev == b.dev == c.dev
    assert a.robustness == b.robustness == c.robustness
    assert a.reserved != b.reserved and c.reserved is None and c.verdict is None


def test_robustness_only_simulates_the_development_hours() -> None:
    assets = _assets()
    dev, _ = split(24, len(assets[0]))
    rob = run_robustness(assets, [spread_signal(x) for x in assets], dev,
                         THRESHOLDS[Strategy.A], make_spec())
    assert {v.stats.hours for v in rob} == {len(dev)}


# --- episodios y costes de B --------------------------------------------------------------


def test_high_funding_episodes_from_entry_until_exit() -> None:
    th = Thresholds(0.10, 0.02)
    nan = float("nan")
    sig = [nan, 0.05, 0.12, 0.08, 0.03, 0.01, 0.2, nan, 0.15, 0.019, 0.5, 0.5]
    assert high_funding_episodes(sig, th, 0) == [3, 3, 2]


def test_cycle_cost_and_days_to_cover() -> None:
    c = Costs()
    assert cycle_cost_fraction(c, SpotFee.MAKER) == pytest.approx(2 * (0.004 + 0.0005) + 0.002)
    assert cycle_cost_fraction(c, SpotFee.TAKER) == pytest.approx(2 * (0.008 + 0.0005) + 0.002)
    assert days_to_cover(0.011, 0.10) == pytest.approx(40.15)
    assert days_to_cover(0.011, 0.0) == float("inf")
