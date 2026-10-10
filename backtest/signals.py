"""Señales de la estrategia, evaluadas siempre sobre velas ya cerradas.

Largo (régimen alcista, ``cierre > SMA``): %K cruza por encima de %D en la vela ``i`` habiendo
estado %K por debajo de ``oversold`` en la vela anterior. Corto (régimen bajista, ``cierre < SMA``):
%K cruza por debajo de %D habiendo estado %K por encima de ``overbought`` en la vela anterior.
Cruce estricto: ``K[i-1] <= D[i-1]`` y ``K[i] > D[i]`` (a la inversa para cortos).

La señal de la vela ``i`` usa solo datos hasta el cierre de ``i``; la entrada es la apertura de
``i + 1`` (la resuelve el motor).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from backtest.config import Params
from backtest.data import Candles
from backtest.indicators import atr, sma, stoch_rsi


@dataclass(frozen=True)
class Indicators:
    sma: list[float]
    k: list[float]
    d: list[float]
    atr: list[float]


@dataclass(frozen=True)
class Signal:
    idx: int  # vela (cerrada) en cuyo cierre se genera la señal
    side: int  # +1 largo, -1 corto
    atr: float  # ATR de esa vela, usado para stop y take profit


def compute_indicators(c: Candles, p: Params) -> Indicators:
    k, d = stoch_rsi(c.c, p.rsi_len, p.stoch_len, p.k_smooth, p.d_smooth)
    return Indicators(sma(c.c, p.sma_len), k, d, atr(c.h, c.l, c.c, p.atr_len))


def generate_signals(c: Candles, p: Params, ind: Indicators | None = None) -> list[Signal]:
    ind = ind or compute_indicators(c, p)
    out: list[Signal] = []
    for i in range(1, len(c)):
        values = (ind.sma[i], ind.atr[i], ind.k[i], ind.d[i], ind.k[i - 1], ind.d[i - 1])
        if any(math.isnan(x) for x in values) or ind.atr[i] <= 0.0:
            continue
        k0, d0, k1, d1 = ind.k[i - 1], ind.d[i - 1], ind.k[i], ind.d[i]
        close = c.c[i]
        if close > ind.sma[i] and k0 <= d0 and k1 > d1 and k0 < p.oversold:
            out.append(Signal(i, 1, ind.atr[i]))
        elif close < ind.sma[i] and k0 >= d0 and k1 < d1 and k0 > p.overbought:
            out.append(Signal(i, -1, ind.atr[i]))
    return out
