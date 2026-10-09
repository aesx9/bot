from __future__ import annotations

import math

import pytest

from backtest.indicators import atr, rsi, sma, stoch_rsi
from backtest.tests.helpers import random_walk


def test_sma_and_nan_warmup() -> None:
    out = sma([1.0, 2.0, 3.0, 4.0], 2)
    assert math.isnan(out[0])
    assert out[1:] == [1.5, 2.5, 3.5]


def test_sma_window_with_nan_is_nan() -> None:
    out = sma([math.nan, 1.0, 2.0, 3.0], 2)
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert out[2:] == [1.5, 2.5]


def test_rsi_hand_computed_wilder() -> None:
    # n=2, cierres 1,2,1,2,3: cambios +1,-1 -> media ganancia 0,5 y pérdida 0,5 -> RSI 50
    # siguiente cambio +1: ganancia (0,5*1+1)/2=0,75; pérdida 0,25 -> RS 3 -> RSI 75
    # siguiente cambio +1: ganancia 0,875; pérdida 0,125 -> RS 7 -> RSI 87,5
    out = rsi([1.0, 2.0, 1.0, 2.0, 3.0], 2)
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert out[2] == pytest.approx(50.0)
    assert out[3] == pytest.approx(75.0)
    assert out[4] == pytest.approx(87.5)


def test_rsi_extremes() -> None:
    assert rsi([1.0, 2.0, 3.0, 4.0], 2)[-1] == 100.0
    assert rsi([4.0, 3.0, 2.0, 1.0], 2)[-1] == 0.0


def test_atr_hand_computed_with_gap() -> None:
    # TR0 = 2; TR1 = max(2, |14-9|, |12-9|) = 5; TR2 = max(2, |13-13|, |11-13|) = 2
    high, low, close = [10.0, 14.0, 13.0], [8.0, 12.0, 11.0], [9.0, 13.0, 12.0]
    out = atr(high, low, close, 2)
    assert math.isnan(out[0])
    assert out[1] == pytest.approx(3.5)
    assert out[2] == pytest.approx(2.75)


def test_stoch_rsi_warmup_and_bounds() -> None:
    c = random_walk(200, seed=1)
    k, d = stoch_rsi(c.c, 14, 14, 3, 3)
    first_k = 14 + 14 - 1 + 3 - 1  # RSI desde 14, ventana 14 y suavizado 3
    first_d = first_k + 3 - 1
    assert all(math.isnan(x) for x in k[:first_k]) and not math.isnan(k[first_k])
    assert all(math.isnan(x) for x in d[:first_d]) and not math.isnan(d[first_d])
    assert all(0.0 <= x <= 100.0 for x in k if not math.isnan(x))
    assert all(0.0 <= x <= 100.0 for x in d if not math.isnan(x))


def test_stoch_rsi_matches_direct_formula() -> None:
    c = random_walk(120, seed=2)
    r = rsi(c.c, 14)
    k, _ = stoch_rsi(c.c, 14, 14, 1, 1)  # sin suavizado: %K = estocástico crudo
    for i in range(27, 120):
        window = r[i - 13 : i + 1]
        expected = 100.0 * (r[i] - min(window)) / (max(window) - min(window))
        assert k[i] == pytest.approx(expected)


def test_indicators_are_causal() -> None:
    c = random_walk(150, seed=3)
    cut = 100
    full = (sma(c.c, 50), rsi(c.c, 14), atr(c.h, c.l, c.c, 14), *stoch_rsi(c.c, 14, 14, 3, 3))
    head = (
        sma(c.c[: cut + 1], 50),
        rsi(c.c[: cut + 1], 14),
        atr(c.h[: cut + 1], c.l[: cut + 1], c.c[: cut + 1], 14),
        *stoch_rsi(c.c[: cut + 1], 14, 14, 3, 3),
    )
    for a, b in zip(full, head, strict=True):
        for x, y in zip(a[: cut + 1], b, strict=True):
            assert (math.isnan(x) and math.isnan(y)) or x == y
