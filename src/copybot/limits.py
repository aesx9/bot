"""Topes absolutos de riesgo.

Estos valores están en código a propósito: la configuración solo puede ser
IGUAL O MÁS ESTRICTA. Si config.toml los supera, el bot no arranca.
Cambiarlos exige editar este fichero, pasar los tests y hacer commit.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

# Exposición
HARD_MAX_LEVERAGE: Final = Decimal("3")
HARD_MAX_NOTIONAL_PER_ASSET_USD: Final = Decimal("600")
HARD_MAX_NOTIONAL_TOTAL_USD: Final = Decimal("1500")
HARD_MAX_ASSET_PCT_OF_EQUITY: Final = Decimal("100")

# Ejecución: tope de deslizamiento del límite IOC respecto al precio de referencia
HARD_MAX_SLIPPAGE_PCT: Final = Decimal("0.5")

# Circuit breaker
HARD_MAX_ORDERS_PER_MINUTE: Final = 10
HARD_MAX_NOTIONAL_PER_HOUR_USD: Final = Decimal("2000")
HARD_MAX_CONSECUTIVE_ERRORS: Final = 5

# Sanity del líder: ciclos seguidos con un control fallido antes de detenerse
HARD_MAX_SANITY_FAILURES: Final = 5

# Drawdown máximo desde el pico de capital (%)
HARD_MAX_DRAWDOWN_PCT: Final = Decimal("15")

# Stop de catástrofe: distancia permitida respecto al precio de entrada (%)
HARD_MIN_CATASTROPHE_STOP_PCT: Final = Decimal("5")
HARD_MAX_CATASTROPHE_STOP_PCT: Final = Decimal("50")


class HardLimitViolation(Exception):
    """Un cálculo o la configuración superan un tope absoluto."""
