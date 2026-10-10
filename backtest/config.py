"""Parámetros fijos del backtest (no se optimizan) y criterios de aceptación."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

SYMBOLS: tuple[str, ...] = ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD")

HOUR_MS = 3_600_000
CANDLE_MS = 4 * HOUR_MS
DAY_MS = 24 * HOUR_MS
HOURS_PER_CANDLE = 4

DEV_FRACTION = 0.70  # 70 % inicial para desarrollo, 30 % final reservado
N_RANDOM = 1000
RANDOM_SEED = 20261009


@dataclass(frozen=True)
class Params:
    """Reglas de la estrategia. Valores fijados de antemano; solo se varían en la robustez."""

    sma_len: int = 50
    rsi_len: int = 14
    stoch_len: int = 14
    k_smooth: int = 3
    d_smooth: int = 3
    oversold: float = 20.0
    overbought: float = 80.0
    atr_len: int = 14
    stop_atr: float = 2.0
    tp_atr: float = 3.0


PARAM_NAMES: tuple[str, ...] = (
    "sma_len",
    "rsi_len",
    "stoch_len",
    "k_smooth",
    "d_smooth",
    "oversold",
    "overbought",
    "atr_len",
    "stop_atr",
    "tp_atr",
)


def perturb(params: Params, name: str, factor: float) -> Params:
    """Devuelve `params` con `name` multiplicado por `factor` (enteros redondeados, mínimo 1)."""
    if name not in PARAM_NAMES:
        raise ValueError(f"parámetro desconocido: {name}")
    value = getattr(params, name)
    scaled = value * factor
    new = max(1, int(scaled + 0.5)) if isinstance(value, int) else scaled
    return replace(params, **{name: new})


@dataclass(frozen=True)
class Costs:
    fee: float = 0.0005  # comisión taker 0,05 % por ejecución (entrada y salida)
    slippage: float = 0.0005  # 5 pb por ejecución, siempre en contra


@dataclass(frozen=True)
class Account:
    initial_capital: float = 550.0
    risk_per_trade: float = 0.01  # fracción del capital arriesgada hasta el stop
    max_leverage: float = 2.0  # nocional total abierto / capital


class Scenario(StrEnum):
    """Cómo se rellena el funding donde Kraken no publica histórico."""

    CENTRAL = "central"  # mediana horaria con signo del año real, por activo
    PESIMISTA = "pesimista"  # siempre en contra de la posición, |P75| del año real


@dataclass(frozen=True)
class Criteria:
    """Criterios de aceptación fijados antes de ver resultados."""

    min_trades: int = 50  # en el tramo reservado
    max_drawdown: float = 0.15  # estricto: debe ser menor
    robustness_pct: float = 0.20  # cada parámetro ±20 %
    min_percentile: float = 90.0  # frente a las simulaciones aleatorias
    judged_scenario: Scenario = Scenario.PESIMISTA
