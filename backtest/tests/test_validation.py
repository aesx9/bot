"""Validación: corte 70/30, robustez ±20 %, azar reproducible y ausencia de fuga del reservado."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from backtest.config import Account, Costs, Params, Scenario, perturb
from backtest.data import Candles
from backtest.engine import Segment, simulate
from backtest.funding import FundingTable, funding_stats
from backtest.runner import DEV, RESERVED, run_all
from backtest.signals import Indicators, Signal, compute_indicators, generate_signals
from backtest.tests.helpers import ACCOUNT, PARAMS, ZERO_COSTS, random_walk, synthetic_dataset
from backtest.validation import (
    _rates,
    common_length,
    draw_random_signals,
    percentile_rank,
    robustness_variants,
    run_random,
    run_robustness,
    split_segments,
)


def test_split_is_chronological_contiguous_and_70_30() -> None:
    dev, res = split_segments(1000)
    assert (dev.start, dev.end, res.start, res.end) == (0, 700, 700, 1000)
    dev, res = split_segments(9966)
    assert dev.end == res.start == 6976 and res.end == 9966
    assert dev.name == "desarrollo" and res.name == "reservado"


def test_common_timeline_must_match() -> None:
    a, b = random_walk(50, 1, "A"), random_walk(50, 2, "B")
    assert common_length({"A": a, "B": b}) == 50
    with pytest.raises(ValueError, match="línea temporal"):
        common_length({"A": a, "B": random_walk(49, 3, "B")})


@pytest.mark.parametrize(
    ("name", "factor", "expected"),
    [
        ("sma_len", 0.8, 40), ("sma_len", 1.2, 60), ("rsi_len", 0.8, 11), ("rsi_len", 1.2, 17),
        ("stoch_len", 0.8, 11), ("stoch_len", 1.2, 17), ("k_smooth", 0.8, 2), ("k_smooth", 1.2, 4),
        ("d_smooth", 0.8, 2), ("d_smooth", 1.2, 4), ("atr_len", 0.8, 11), ("atr_len", 1.2, 17),
        ("oversold", 0.8, 16.0), ("oversold", 1.2, 24.0), ("overbought", 0.8, 64.0),
        ("overbought", 1.2, 96.0), ("stop_atr", 0.8, 1.6), ("stop_atr", 1.2, 2.4),
        ("tp_atr", 0.8, 2.4), ("tp_atr", 1.2, 3.6),
    ],
)
def test_perturb_plus_minus_20_percent(name: str, factor: float, expected: float) -> None:
    assert getattr(perturb(Params(), name, factor), name) == pytest.approx(expected)


def test_robustness_varies_one_parameter_at_a_time() -> None:
    variants = robustness_variants(Params(), 0.20)
    assert len(variants) == 20
    for name, _, params in variants:
        base = Params()
        changed = [f for f in Params.__dataclass_fields__ if getattr(params, f) != getattr(base, f)]
        assert changed == [name]


def test_perturb_rejects_unknown_parameter_and_keeps_integers_at_least_one() -> None:
    with pytest.raises(ValueError):
        perturb(Params(), "nope", 1.2)
    assert perturb(Params(k_smooth=1), "k_smooth", 0.8).k_smooth == 1


def test_percentile_rank_counts_ties_as_half() -> None:
    sample = [1.0, 2.0, 3.0, 4.0]
    assert percentile_rank(sample, 2.5) == 50.0
    assert percentile_rank(sample, 0.0) == 0.0 and percentile_rank(sample, 9.0) == 100.0
    assert percentile_rank(sample, 2.0) == 37.5  # 1 por debajo + media de 1 empate


# --- azar --------------------------------------------------------------------------------


Setup = tuple[
    dict[str, Candles],
    dict[Scenario, dict[str, FundingTable]],
    dict[str, Indicators],
    dict[str, list[Signal]],
]


def _setup(n: int = 900) -> Setup:
    candles, funding = synthetic_dataset(n=n)
    stats = {a: funding_stats(f) for a, f in funding.items()}
    tables = {
        sc: {a: FundingTable(candles[a], funding[a], stats[a], sc) for a in candles}
        for sc in Scenario
    }
    ind = {a: compute_indicators(c, PARAMS) for a, c in candles.items()}
    sigs = {a: generate_signals(c, PARAMS, ind[a]) for a, c in candles.items()}
    return candles, tables, ind, sigs


def test_random_signals_match_the_observed_frequency_and_are_reproducible() -> None:
    _, _, ind, sigs = _setup()
    seg = Segment("desarrollo", 0, 900)
    rates = _rates(sigs["PF_A"], ind["PF_A"], seg)
    assert rates.count > 0 and 0 < rates.p_long + rates.p_short < 1
    atr = ind["PF_A"].atr
    # promedio de muchas muestras ~ frecuencia esperada
    total = sum(len(draw_random_signals(random.Random(i), rates, atr)) for i in range(300))  # noqa: S311
    expected = (rates.p_long + rates.p_short) * rates.count
    assert total / 300 == pytest.approx(expected, rel=0.15)
    one = draw_random_signals(random.Random(1), rates, atr)  # noqa: S311
    assert one == draw_random_signals(random.Random(1), rates, atr)  # noqa: S311
    assert all(rates.first <= s.idx < rates.first + rates.count for s in one)
    assert all(s.atr == atr[s.idx] for s in one)


def test_run_random_is_reproducible_by_seed_and_uses_all_scenarios() -> None:
    candles, tables, ind, sigs = _setup()
    seg = Segment("desarrollo", 0, 900)
    args = (candles, sigs, ind, tables, seg, PARAMS, Costs(), ACCOUNT, 12)
    a = run_random(*args, seed=5)
    b = run_random(*args, seed=5)
    c = run_random(*args, seed=6)
    assert a == b and a != c
    assert set(a) == set(Scenario) and a[Scenario.PESIMISTA].n_sims == 12
    # mismas entradas, solo cambia el funding: el pesimista nunca mejora al central en media
    assert sum(a[Scenario.PESIMISTA].net_returns) <= sum(a[Scenario.CENTRAL].net_returns) + 1e-9


def test_random_trades_use_the_same_exits_and_costs_as_the_strategy() -> None:
    candles, tables, ind, sigs = _setup()
    seg = Segment("desarrollo", 0, 900)
    rates = {a: _rates(sigs[a], ind[a], seg) for a in candles}
    rng = random.Random(3)  # noqa: S311
    signals = {a: draw_random_signals(rng, rates[a], ind[a].atr) for a in candles}
    res = simulate(candles, signals, tables[Scenario.PESIMISTA], seg, PARAMS, Costs(), ACCOUNT)
    assert res.trades
    for t in res.trades:
        assert t.stop == pytest.approx(t.entry_fill - t.side * PARAMS.stop_atr * t.atr)
        assert t.take_profit == pytest.approx(t.entry_fill + t.side * PARAMS.tp_atr * t.atr)
        assert t.fee_entry > 0 and t.fee_exit > 0 and t.slippage > 0


# --- ni el reservado ni el futuro contaminan el desarrollo ------------------------------------


def _trash_reserved(candles: dict[str, Candles], start: int) -> dict[str, Candles]:
    out = {}
    for a, c in candles.items():
        junk = random_walk(len(c) - start, seed=99, symbol=a, vol=0.08)
        out[a] = replace(
            c,
            o=c.o[:start] + [x * 3 for x in junk.o],
            h=c.h[:start] + [x * 3 for x in junk.h],
            l=c.l[:start] + [x * 3 for x in junk.l],
            c=c.c[:start] + [x * 3 for x in junk.c],
        )
    return out


def test_development_results_do_not_depend_on_the_reserved_segment() -> None:
    candles, funding = synthetic_dataset(n=1200)
    cut = split_segments(1200)[0].end
    original = run_all(candles, funding, n_random=15)
    trashed = run_all(_trash_reserved(candles, cut), funding, n_random=15)
    for sc in Scenario:
        a, b = original.outcomes[sc][DEV], trashed.outcomes[sc][DEV]
        assert a.run.trades == b.run.trades
        assert a.stats == b.stats
    assert original.robustness == trashed.robustness  # la robustez solo mira desarrollo
    assert original.random[DEV] == trashed.random[DEV]
    # ...y el reservado sí cambia, así que la alteración es material
    assert (original.outcomes[Scenario.PESIMISTA][RESERVED].run.trades
            != trashed.outcomes[Scenario.PESIMISTA][RESERVED].run.trades)


def test_robustness_refuses_to_run_on_the_reserved_segment() -> None:
    candles, tables, _, _ = _setup(400)
    with pytest.raises(ValueError, match="reservado"):
        run_robustness(candles, tables, Segment("reservado", 280, 400), PARAMS, ZERO_COSTS,
                       Account(), 0.2)


def test_only_development_never_touches_the_reserved_segment() -> None:
    candles, funding = synthetic_dataset(n=900)
    res = run_all(candles, funding, only_dev=True, n_random=5)
    assert set(res.segments) == {DEV}
    assert all(set(by_seg) == {DEV} for by_seg in res.outcomes.values())
    assert set(res.random) == {DEV}
    assert res.verdict is not True  # incompleto: a lo sumo puede estar ya refutado
    reserved_checks = [c for c in res.checks if "reservado" in c.name and "±" not in c.name]
    assert len(reserved_checks) == 4 and all(c.passed is None for c in reserved_checks)
