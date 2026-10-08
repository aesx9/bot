"""Protecciones de riesgo. Todo el estado que importa vive en BotState.

- Kill switch: si existe un fichero STOP, no se opera y el bot se detiene.
- Parada (halt): persistente entre reinicios; solo --reset-halt la quita.
- Drawdown desde el pico de capital (pico persistido).
- Circuit breaker: órdenes por minuto y nocional enviado por hora, con el
  registro persistido para que un reinicio no lo ponga a cero.
- Parada tras N ciclos consecutivos con error.
- Perfil de arranque live (1x, 100 USD por activo).
Los valores de config se combinan siempre con los topes de limits.py.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from copybot import limits
from copybot.config import RiskConfig, SizingConfig
from copybot.sources.sanity import SanityState
from copybot.state import BotState

log = logging.getLogger(__name__)

STOP_FILENAME = "STOP"


def kill_switch_active(directories: Iterable[Path]) -> Path | None:
    for d in directories:
        f = d / STOP_FILENAME
        if f.exists():
            return f
    return None


def halt(state: BotState, reason: str, now: datetime | None = None) -> None:
    if not state.halted:
        log.error("BOT DETENIDO: %s", reason)
        state.halted = True
        state.halt_reason = reason
        state.halted_at = (now or datetime.now(UTC)).isoformat()
    state.live_confirmation = None  # tras cualquier parada hay que volver a confirmar


def reset_halt(state: BotState) -> None:
    """Quitar la parada tras revisar la causa. La referencia del sanity se reinicia."""
    state.halted = False
    state.halt_reason = ""
    state.halted_at = None
    state.consecutive_errors = 0
    state.paced_streak = 0
    state.kill_switch_closed = False
    state.emergency_close_pending = False
    state.live_confirmation = None
    state.sanity = SanityState()


def record_cycle_error(state: BotState, cfg: RiskConfig) -> bool:
    """Cuenta un ciclo con error. Devuelve True si toca detenerse."""
    state.consecutive_errors += 1
    limit = min(cfg.max_consecutive_errors, limits.HARD_MAX_CONSECUTIVE_ERRORS)
    return state.consecutive_errors >= limit


def record_cycle_ok(state: BotState) -> None:
    state.consecutive_errors = 0


def drawdown_pct(state: BotState, equity: Decimal) -> Decimal:
    """Actualiza el pico persistido y devuelve el drawdown actual en %."""
    if equity <= 0:
        return Decimal(100)
    if state.peak_equity_usd is None or equity > state.peak_equity_usd:
        state.peak_equity_usd = equity
    peak = state.peak_equity_usd
    return (peak - equity) / peak * 100


def drawdown_tripped(state: BotState, equity: Decimal, cfg: RiskConfig) -> Decimal | None:
    dd = drawdown_pct(state, equity)
    limit = min(cfg.max_drawdown_pct, limits.HARD_MAX_DRAWDOWN_PCT)
    return dd if dd >= limit else None


class CircuitBreaker:
    def __init__(
        self, state: BotState, cfg: RiskConfig, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._state = state
        self._max_orders = min(cfg.max_orders_per_minute, limits.HARD_MAX_ORDERS_PER_MINUTE)
        self._max_notional = min(
            cfg.max_notional_per_hour_usd, limits.HARD_MAX_NOTIONAL_PER_HOUR_USD
        )
        self._clock = clock

    def _prune(self, now: float) -> None:
        self._state.breaker_log = [(t, n) for t, n in self._state.breaker_log if now - t < 3600]

    def minute_limit_reached(self) -> bool:
        """El límite de órdenes/min no se supera nunca: las órdenes que no caben
        se aplazan al ciclo siguiente (el planificador ya las priorizó)."""
        now = self._clock()
        self._prune(now)
        last_minute = sum(1 for t, _ in self._state.breaker_log if now - t < 60)
        return last_minute >= self._max_orders

    def check_notional(self, notional_usd: Decimal) -> str | None:
        """Motivo para DETENER el bot si esta orden supera el nocional por hora."""
        now = self._clock()
        self._prune(now)
        last_hour = sum((n for _, n in self._state.breaker_log), Decimal(0))
        if last_hour + notional_usd > self._max_notional:
            return (
                f"circuit breaker: el nocional enviado en una hora superaría "
                f"{self._max_notional} USD ({last_hour} + {notional_usd})"
            )
        return None

    def record(self, notional_usd: Decimal) -> None:
        self._state.breaker_log.append((self._clock(), notional_usd))


def catastrophe_stop_pct(risk: RiskConfig, effective_leverage: Decimal) -> Decimal:
    """Distancia (%) del stop de catástrofe al precio de entrada.

    Con apalancamiento L, una caída de d % del precio es una pérdida de d x L % del capital:
    el stop se pone donde esa pérdida iguala al límite de drawdown (max_drawdown_pct / L),
    de modo que el exchange protege con el mismo criterio que el propio bot, incluso con el
    bot caído. La config solo puede acercarlo; siempre dentro de los límites absolutos."""
    drawdown = min(risk.max_drawdown_pct, limits.HARD_MAX_DRAWDOWN_PCT)
    pct = drawdown / effective_leverage
    if risk.catastrophe_stop_pct is not None:
        pct = min(pct, risk.catastrophe_stop_pct)
    return min(max(pct, limits.HARD_MIN_CATASTROPHE_STOP_PCT),
               limits.HARD_MAX_CATASTROPHE_STOP_PCT)


# --- Perfil de arranque live ---


def activate_startup_profile_on_first_live(state: BotState) -> bool:
    """Al arrancar en live: si es la primera vez, activa el perfil. Devuelve si está activo."""
    if state.live_startup_profile is None:
        state.live_startup_profile = True
        log.warning("primer arranque en live: perfil de arranque activado (1x, 100 USD/activo)")
    return state.live_startup_profile


def release_startup_profile(state: BotState) -> None:
    """Solo desde el comando explícito con confirmación escrita."""
    state.live_startup_profile = False


def effective_sizing(cfg: SizingConfig, *, startup_profile: bool) -> SizingConfig:
    if not startup_profile:
        return cfg
    lev = min(cfg.max_total_leverage, limits.STARTUP_PROFILE_MAX_LEVERAGE)
    return cfg.model_copy(update={
        "max_total_leverage": lev,
        "max_asset_usd": min(cfg.max_asset_usd, limits.STARTUP_PROFILE_MAX_ASSET_USD),
        "max_asset_pct_equity": min(cfg.max_asset_pct_equity, lev * 100),
    })
