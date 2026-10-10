"""Sin lookahead: alterar velas futuras no cambia señales ni indicadores pasados."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from backtest.config import Params
from backtest.data import Candles
from backtest.signals import compute_indicators, generate_signals
from backtest.tests.helpers import random_walk


def _alter_future(c: Candles, k: int, factor: float) -> Candles:
    """Misma serie hasta la vela ``k`` incluida; después, precios completamente distintos."""
    def scale(xs: list[float], mult: float) -> list[float]:
        future = [x * mult * (1.0 + 0.05 * ((i % 7) - 3)) for i, x in enumerate(xs[k + 1 :])]
        return xs[: k + 1] + future

    return replace(
        c,
        o=scale(c.o, factor),
        h=scale(c.h, factor * 1.3),
        l=scale(c.l, factor * 0.7),
        c=scale(c.c, factor),
    )


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("k", [120, 200, 301])
def test_future_candles_do_not_change_past_signals_or_indicators(seed: int, k: int) -> None:
    c = random_walk(400, seed=seed, vol=0.02)
    params = Params()
    base_ind, alt = compute_indicators(c, params), _alter_future(c, k, 1.7)
    alt_ind = compute_indicators(alt, params)
    for name in ("sma", "k", "d", "atr"):
        before, after = getattr(base_ind, name)[: k + 1], getattr(alt_ind, name)[: k + 1]
        for x, y in zip(before, after, strict=True):
            assert (math.isnan(x) and math.isnan(y)) or x == y, name
    base_signals, alt_signals = generate_signals(c, params), generate_signals(alt, params)
    assert [s for s in base_signals if s.idx <= k] == [s for s in alt_signals if s.idx <= k]
    # La alteración es material: el futuro sí cambia (si no, el test no probaría nada).
    assert [s for s in base_signals if s.idx > k] != [s for s in alt_signals if s.idx > k]


def test_the_signal_of_the_last_closed_candle_ignores_the_next_one() -> None:
    """Una señal en la vela k se decide con velas <= k: cambiar solo la k+1 no la toca."""
    c = random_walk(600, seed=11, vol=0.03)
    signals = generate_signals(c, Params())
    assert signals, "la serie de prueba debe generar señales"
    for s in signals[:25]:
        k = s.idx
        o, h, lo, cl = (list(x) for x in (c.o, c.h, c.l, c.c))
        o[k + 1], h[k + 1], lo[k + 1], cl[k + 1] = 1.0, 1e6, 0.5, 777.0
        mutated = replace(c, o=o, h=h, l=lo, c=cl)
        same = [x for x in generate_signals(mutated, Params()) if x.idx == k]
        assert same == [s]
