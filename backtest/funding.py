"""Funding horario aplicado a una posición abierta, con relleno de los tramos sin histórico.

Kraken solo publica ~1 año de funding. Donde hay dato se usa el real; donde no, se imputa según
el escenario (cobertura y reparto real/imputado se reportan por separado):

- ``CENTRAL``: mediana horaria *con signo* de ``relativeFundingRate`` del año real, por activo.
  Un largo paga si es positiva y cobra si es negativa; un corto, al revés.
- ``PESIMISTA``: siempre en contra de la posición, con valor ``|P75|`` de la tasa relativa del año
  real, por activo (el signo de la posición no importa: largos y cortos pagan).

Funding real: una posición larga abonada con ``fundingRate`` (USD por unidad y hora) multiplicado
por la cantidad, que es la liquidación exacta de Kraken. Funding imputado: tasa relativa por
nocional, usando la apertura de la vela de 4h como precio de referencia de cada hora.

La granularidad del cálculo es la vela de 4h: una posición paga el funding de todas las horas de
cada vela en la que está abierta (de la apertura de la vela de entrada al cierre de la de salida).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from backtest.config import HOUR_MS, HOURS_PER_CANDLE, Scenario
from backtest.data import Candles, FundingSeries


def percentile(values: Sequence[float], q: float) -> float:
    """Percentil ``q`` (0-100) por interpolación lineal (el método por defecto de numpy)."""
    if not values:
        raise ValueError("percentil de una serie vacía")
    if not 0.0 <= q <= 100.0:
        raise ValueError("q debe estar en [0, 100]")
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


@dataclass(frozen=True)
class FundingStats:
    """Estadísticos del año real de funding de un activo (tasas relativas horarias)."""

    n_hours: int
    first_ms: int
    last_ms: int
    median_rel: float  # con signo
    p75_rel: float  # con signo; el escenario pesimista usa su valor absoluto
    p75_abs_rel: float = 0.0  # P75 del valor absoluto: lectura alternativa, solo informativa


def funding_stats(series: FundingSeries) -> FundingStats:
    return FundingStats(
        n_hours=len(series.t),
        first_ms=series.t[0],
        last_ms=series.t[-1],
        median_rel=percentile(series.rate_rel, 50.0),
        p75_rel=percentile(series.rate_rel, 75.0),
        p75_abs_rel=percentile([abs(x) for x in series.rate_rel], 75.0),
    )


class FundingTable:
    """Coste de funding por unidad de contrato en un rango de velas, en O(1) (sumas prefijo)."""

    def __init__(
        self,
        candles: Candles,
        series: FundingSeries,
        stats: FundingStats,
        scenario: Scenario,
    ) -> None:
        by_hour = dict(zip(series.t, series.rate_abs, strict=True))
        n = len(candles)
        self.scenario = scenario
        # Prefijos de longitud n + 1: el rango [j0, j1] vale P[j1 + 1] - P[j0].
        self._real_hours = [0] * (n + 1)
        self._real_abs = [0.0] * (n + 1)  # suma de fundingRate real (coste de 1 largo)
        self._imp_hours = [0] * (n + 1)
        self._imp_long = [0.0] * (n + 1)  # coste imputado de 1 largo
        self._imp_short = [0.0] * (n + 1)
        if scenario is Scenario.CENTRAL:
            rate_long, rate_short = stats.median_rel, -stats.median_rel
        else:
            rate_long = rate_short = abs(stats.p75_rel)
        for j in range(n):
            t0 = candles.t[j]
            real_abs = 0.0
            real_h = 0
            for k in range(HOURS_PER_CANDLE):
                rate = by_hour.get(t0 + k * HOUR_MS)
                if rate is not None:
                    real_abs += rate
                    real_h += 1
            imp_h = HOURS_PER_CANDLE - real_h
            weight = imp_h * candles.o[j]  # USD de precio-hora sin dato real
            self._real_hours[j + 1] = self._real_hours[j] + real_h
            self._real_abs[j + 1] = self._real_abs[j] + real_abs
            self._imp_hours[j + 1] = self._imp_hours[j] + imp_h
            self._imp_long[j + 1] = self._imp_long[j] + rate_long * weight
            self._imp_short[j + 1] = self._imp_short[j] + rate_short * weight

    def cost_per_unit(self, side: int, j0: int, j1: int) -> tuple[float, float]:
        """(real, imputado) en USD por unidad de contrato desde la apertura de ``j0`` al cierre de
        ``j1``. Positivo = paga la posición; negativo = cobra."""
        real = (self._real_abs[j1 + 1] - self._real_abs[j0]) * side
        imp_prefix = self._imp_long if side > 0 else self._imp_short
        return real, imp_prefix[j1 + 1] - imp_prefix[j0]

    def hours(self, j0: int, j1: int) -> tuple[int, int]:
        """(horas con funding real, horas imputadas) del rango de velas ``[j0, j1]``."""
        return (
            self._real_hours[j1 + 1] - self._real_hours[j0],
            self._imp_hours[j1 + 1] - self._imp_hours[j0],
        )
