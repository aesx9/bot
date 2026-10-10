"""Parámetros fijos del backtest de arbitraje de funding (no se optimizan) y criterios.

Todos los valores proceden de la especificación y se fijaron antes de ver resultados.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
HOURS_PER_YEAR = 24 * 365

# --- ventanas de datos --------------------------------------------------------------------

# A: 208 días desde 2026-03-16 (las 5000 velas de 1h que sirve candleSnapshot de Hyperliquid).
WINDOW_A_START_MS = 1_773_619_200_000  # 2026-03-16T00:00:00Z
WINDOW_A_DAYS = 208
# B: sus propios 365 días (el funding real de Kraken cubre ≈1 año).
WINDOW_B_DAYS = 365

DEV_FRACTION = 0.70  # 70 % inicial de cada ventana para desarrollo, 30 % final reservado

# --- universo -------------------------------------------------------------------------------

UNIVERSE_DAYS = 90  # volumen diario medio de los últimos 90 días completos
MIN_DAILY_VOLUME_USD = 10_000_000.0
# Comprobación de unidades al emparejar bases entre plataformas (p. ej. «kPEPE» = 1000 PEPE):
# la mediana del cociente de cierres diarios debe estar dentro de este margen.
MAX_PRICE_RATIO_DEV = 0.02


class Strategy(StrEnum):
    A = "A"  # entre plataformas: Kraken Futures vs Hyperliquid
    B = "B"  # cash and carry en Kraken: spot largo + perpetuo corto


@dataclass(frozen=True)
class Thresholds:
    """Umbrales anualizados sobre la media de 24 h (fracción: 0.20 = 20 % anual)."""

    entry: float
    exit: float

    def scaled(self, factor: float) -> Thresholds:
        return replace(self, entry=self.entry * factor, exit=self.exit * factor)


THRESHOLDS: dict[Strategy, Thresholds] = {
    Strategy.A: Thresholds(entry=0.20, exit=0.05),
    Strategy.B: Thresholds(entry=0.10, exit=0.02),
}

MEAN_HOURS = 24  # media móvil del funding liquidado
MAX_POSITIONS = 3  # simultáneas por estrategia, mismo nocional


@dataclass(frozen=True)
class Account:
    initial_capital: float = 550.0
    max_leverage_a: float = 2.0  # por pierna, sobre el margen asignado a esa plataforma


class SpotFee(StrEnum):
    MAKER = "maker"  # escenario evaluado
    TAKER = "taker"  # referencia pesimista


@dataclass(frozen=True)
class Costs:
    kraken_futures_taker: float = 0.0005
    hyperliquid_taker: float = 0.00045  # tier 0, verificado
    kraken_spot_maker: float = 0.0040  # nivel 1 de la cuenta, verificado
    kraken_spot_taker: float = 0.0080
    slippage: float = 0.0005  # 5 pb por pierna, en entrada y salida, siempre en contra
    # A: coste fijo por transferencia entre plataformas (USDC vía Arbitrum). La retirada de
    # Hyperliquid cuesta 1 USDC; la de Kraken depende de la red y no está verificada, así que
    # se usa un supuesto conservador de 3 USD para cualquier sentido.
    transfer_usd: float = 3.0

    def spot_fee(self, which: SpotFee) -> float:
        return self.kraken_spot_maker if which is SpotFee.MAKER else self.kraken_spot_taker


RISK_FREE = 0.03  # referencia: 3 % anual sin riesgo sobre el mismo capital
ROBUSTNESS_PCT = 0.20


@dataclass(frozen=True)
class Criteria:
    """Criterios por estrategia, fijados antes de ver resultados (B se evalúa con spot maker)."""

    min_reserved_annual: float = 0.06  # ≥, el doble de la referencia
    min_dev_annual: float = 0.03  # >
    max_drawdown: float = 0.05  # <
    max_liquidations: int = 0
    min_robust_annual: float = 0.03  # > con cada umbral ±20 %, en desarrollo
    max_asset_share: float = 0.50  # ningún activo > 50 % del beneficio
    min_cycles: int = 10  # ciclos completos en el reservado; si no, «no concluyente»
