"""Indicadores causales: el valor en la vela ``i`` solo usa velas ``<= i``.

Convenciones (las de TradingView):
- SMA simple.
- RSI de Wilder (media móvil RMA), sembrado con la media simple de los primeros ``n`` cambios.
- RSI estocástico: ``stoch = 100 * (rsi - min(rsi, n)) / (max(rsi, n) - min(rsi, n))``;
  ``%K = SMA(stoch, k)`` y ``%D = SMA(%K, d)``. Con rango cero el estocástico no está definido (NaN)
  y no produce señales.
- ATR de Wilder (RMA del rango verdadero), sembrado con la media simple de los primeros ``n`` TR.
Los valores no definidos (calentamiento) son NaN.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

NAN = float("nan")


def sma(values: Sequence[float], n: int) -> list[float]:
    """Media simple de ``n`` valores; NaN si la ventana contiene algún NaN o no está completa."""
    out = [NAN] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1 : i + 1]
        if any(math.isnan(x) for x in window):
            continue
        out[i] = sum(window) / n
    return out


def rsi(close: Sequence[float], n: int) -> list[float]:
    out = [NAN] * len(close)
    if len(close) <= n:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = close[i] - close[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    avg_gain, avg_loss = gain / n, loss / n
    out[n] = _rsi_value(avg_gain, avg_loss)
    for i in range(n + 1, len(close)):
        d = close[i] - close[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0.0)) / n
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def stoch_rsi(
    close: Sequence[float], rsi_len: int, stoch_len: int, k_smooth: int, d_smooth: int
) -> tuple[list[float], list[float]]:
    """Devuelve ``(%K, %D)`` del RSI estocástico en escala 0-100."""
    r = rsi(close, rsi_len)
    raw = [NAN] * len(close)
    for i in range(len(close)):
        window = r[i - stoch_len + 1 : i + 1] if i >= stoch_len - 1 else []
        if len(window) < stoch_len or any(math.isnan(x) for x in window):
            continue
        lo, hi = min(window), max(window)
        if hi > lo:
            raw[i] = 100.0 * (r[i] - lo) / (hi - lo)
    k = sma(raw, k_smooth)
    d = sma(k, d_smooth)
    return k, d


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int) -> list[float]:
    size = len(close)
    out = [NAN] * size
    if size < n:
        return out
    tr = [high[0] - low[0]]
    for i in range(1, size):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    value = sum(tr[:n]) / n
    out[n - 1] = value
    for i in range(n, size):
        value = (value * (n - 1) + tr[i]) / n
        out[i] = value
    return out
