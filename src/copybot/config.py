"""Configuración validada con pydantic.

- Se lee de un TOML (sin secretos: los secretos van en .env, ver credentials.py).
- Tipos, rangos y coherencia se validan al cargar.
- Cualquier valor por encima de los topes de limits.py hace fallar la carga.
"""

from __future__ import annotations

import re
import tomllib
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from copybot import limits


class ConfigError(Exception):
    """La configuración no es válida o supera un tope absoluto."""


def _to_decimal(value: Any) -> Decimal:
    # TOML entrega floats para 0.5; se convierten vía str para no arrastrar
    # errores binarios (Decimal(0.1) != Decimal("0.1")).
    if isinstance(value, bool):
        raise ValueError("se esperaba un número, no un booleano")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int | float | str):
        try:
            result = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"número no válido: {value!r}") from exc
    else:
        raise ValueError(f"número no válido: {value!r}")
    if not result.is_finite():
        raise ValueError("el número debe ser finito")
    return result


Dec = Annotated[Decimal, BeforeValidator(_to_decimal)]

_HL_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_KRAKEN_SYMBOL = re.compile(r"^PF_[A-Z0-9]+USD$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Mode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class SizingMode(StrEnum):
    EQUITY = "equity"  # proporción de capitales x multiplier
    FIXED = "fixed"  # ratio fijo sobre el tamaño del líder


class SizingConfig(_Strict):
    mode: SizingMode = SizingMode.EQUITY
    multiplier: Dec = Field(default=Decimal("1.0"), gt=0, le=10)
    fixed_ratio: Dec = Field(default=Decimal("0.01"), gt=0, le=1)
    max_asset_usd: Dec = Field(default=Decimal("600"), gt=0)
    max_asset_pct_equity: Dec = Field(default=Decimal("25"), gt=0, le=100)
    max_total_leverage: Dec = Field(default=Decimal("2"), gt=0)

    @model_validator(mode="after")
    def _within_hard_limits(self) -> Self:
        if self.max_asset_usd > limits.HARD_MAX_NOTIONAL_PER_ASSET_USD:
            raise ValueError(
                f"max_asset_usd={self.max_asset_usd} supera el tope absoluto "
                f"{limits.HARD_MAX_NOTIONAL_PER_ASSET_USD}"
            )
        if self.max_asset_pct_equity > limits.HARD_MAX_ASSET_PCT_OF_EQUITY:
            raise ValueError("max_asset_pct_equity supera el tope absoluto")
        if self.max_total_leverage > limits.HARD_MAX_LEVERAGE:
            raise ValueError(
                f"max_total_leverage={self.max_total_leverage} supera el tope absoluto "
                f"{limits.HARD_MAX_LEVERAGE}x"
            )
        return self


class PlannerConfig(_Strict):
    # Órdenes por debajo de este nocional se descartan (salvo cierres totales)
    min_order_usd: Dec = Field(default=Decimal("10"), ge=0)
    # Ajustes menores que este % de la posición actual se ignoran
    rebalance_threshold_pct: Dec = Field(default=Decimal("5"), ge=0, le=100)


class ExecutionConfig(_Strict):
    # Precio límite de la orden IOC = referencia ± este %
    slippage_cap_pct: Dec = Field(default=Decimal("0.5"), gt=0)

    @field_validator("slippage_cap_pct")
    @classmethod
    def _cap(cls, v: Decimal) -> Decimal:
        if v > limits.HARD_MAX_SLIPPAGE_PCT:
            raise ValueError(
                f"slippage_cap_pct supera el tope absoluto {limits.HARD_MAX_SLIPPAGE_PCT}"
            )
        return v


class FiltersConfig(_Strict):
    allow: tuple[str, ...] = ()  # vacío = todos
    deny: tuple[str, ...] = ()
    ignore_preexisting: bool = True

    @model_validator(mode="after")
    def _no_overlap(self) -> Self:
        both = set(self.allow) & set(self.deny)
        if both:
            raise ValueError(f"activos en allow y deny a la vez: {sorted(both)}")
        return self


class SymbolsConfig(_Strict):
    # coin de Hyperliquid -> símbolo Kraken, p.ej. {"kPEPE" = "PF_PEPEUSD"}
    overrides: dict[str, str] = Field(default_factory=dict)
    # unidades Kraken por unidad del líder, p.ej. {"kPEPE" = 1000}
    size_factor: dict[str, Dec] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def _symbols(cls, v: dict[str, str]) -> dict[str, str]:
        for coin, sym in v.items():
            if not _KRAKEN_SYMBOL.match(sym):
                raise ValueError(f"override {coin}->{sym}: solo se admiten perpetuos PF_*USD")
        if len(set(v.values())) != len(v):
            raise ValueError("dos activos no pueden mapear al mismo símbolo Kraken")
        return v

    @field_validator("size_factor")
    @classmethod
    def _factors(cls, v: dict[str, Decimal]) -> dict[str, Decimal]:
        for coin, f in v.items():
            if f <= 0:
                raise ValueError(f"size_factor de {coin} debe ser > 0")
        return v


class RiskConfig(_Strict):
    max_drawdown_pct: Dec = Field(default=Decimal("15"), gt=0)
    close_all_on_drawdown: bool = True
    max_orders_per_minute: int = Field(default=10, gt=0)
    max_notional_per_hour_usd: Dec = Field(default=Decimal("2000"), gt=0)
    max_consecutive_errors: int = Field(default=5, gt=0)
    # Ciclos seguidos con órdenes aplazadas por el límite/min (fuera de la
    # sincronización inicial); al superarse, el bot se detiene
    max_consecutive_paced_cycles: int = Field(default=3, gt=0)
    close_all_on_kill_switch: bool = True
    catastrophe_stop_enabled: bool = True  # solo aplica en live
    catastrophe_stop_pct: Dec = Field(default=Decimal("20"))

    @model_validator(mode="after")
    def _within_hard_limits(self) -> Self:
        if self.max_drawdown_pct > limits.HARD_MAX_DRAWDOWN_PCT:
            raise ValueError("max_drawdown_pct supera el tope absoluto")
        if self.max_orders_per_minute > limits.HARD_MAX_ORDERS_PER_MINUTE:
            raise ValueError("max_orders_per_minute supera el tope absoluto")
        if self.max_notional_per_hour_usd > limits.HARD_MAX_NOTIONAL_PER_HOUR_USD:
            raise ValueError("max_notional_per_hour_usd supera el tope absoluto")
        if self.max_consecutive_errors > limits.HARD_MAX_CONSECUTIVE_ERRORS:
            raise ValueError("max_consecutive_errors supera el tope absoluto")
        if self.max_consecutive_paced_cycles > limits.HARD_MAX_PACED_CYCLES:
            raise ValueError("max_consecutive_paced_cycles supera el tope absoluto")
        if not (
            limits.HARD_MIN_CATASTROPHE_STOP_PCT
            <= self.catastrophe_stop_pct
            <= limits.HARD_MAX_CATASTROPHE_STOP_PCT
        ):
            raise ValueError(
                "catastrophe_stop_pct fuera de rango "
                f"[{limits.HARD_MIN_CATASTROPHE_STOP_PCT}, {limits.HARD_MAX_CATASTROPHE_STOP_PCT}]"
            )
        return self


class TimingConfig(_Strict):
    debounce_seconds: Dec = Field(default=Decimal("2"), ge=0, le=60)
    reconcile_interval_seconds: Dec = Field(default=Decimal("60"), ge=5, le=3600)
    leader_stale_seconds: Dec = Field(default=Decimal("30"), gt=0, le=600)
    heartbeat_seconds: Dec = Field(default=Decimal("300"), ge=10)
    equity_snapshot_seconds: Dec = Field(default=Decimal("900"), ge=60)
    ws_backoff_initial_seconds: Dec = Field(default=Decimal("1"), gt=0)
    ws_backoff_max_seconds: Dec = Field(default=Decimal("60"), gt=0, le=600)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.ws_backoff_initial_seconds > self.ws_backoff_max_seconds:
            raise ValueError("ws_backoff_initial_seconds > ws_backoff_max_seconds")
        if self.debounce_seconds >= self.reconcile_interval_seconds:
            raise ValueError("debounce_seconds debe ser menor que reconcile_interval_seconds")
        return self


class SanityConfig(_Strict):
    # Si una posición YA ABIERTA del líder crece más de este factor respecto al
    # último dato aceptado, no operar (las aperturas desde 0 no se comprueban)
    max_position_jump_factor: Dec = Field(default=Decimal("20"), gt=1)
    # Si el capital del líder cambia más de este % respecto al último dato
    # aceptado, no operar (puede ser un depósito o retiro: se alerta)
    max_equity_jump_pct: Dec = Field(default=Decimal("50"), gt=0)
    # Un activo cuyo precio en Hyperliquid (dividido por su size_factor) difiere más de este %
    # del mark de Kraken no se opera: casi seguro es otro activo o otra unidad (p. ej. kPEPE
    # sin size_factor) y el tamaño saldría desproporcionado
    max_price_divergence_pct: Dec = Field(default=Decimal("5"), gt=0, le=50)
    # Ciclos seguidos con algún control fallido antes de detener el bot
    halt_after_consecutive_failures: int = Field(default=3, ge=1)
    # Margen para relojes desincronizados: datos "del futuro" más allá de esto
    max_clock_skew_seconds: Dec = Field(default=Decimal("5"), ge=0, le=60)

    @field_validator("halt_after_consecutive_failures")
    @classmethod
    def _failures_cap(cls, v: int) -> int:
        if v > limits.HARD_MAX_SANITY_FAILURES:
            raise ValueError(
                f"halt_after_consecutive_failures supera el tope absoluto "
                f"{limits.HARD_MAX_SANITY_FAILURES}"
            )
        return v


class PaperConfig(_Strict):
    # Colateral simulado en EUR (el real será EUR); se valora con EUR/USD de Kraken
    initial_collateral_eur: Dec = Field(default=Decimal("500"), gt=0)
    # Haircut del colateral EUR: 2,2 % por prudencia (el ejemplo de la documentación
    # de /accounts da 4999,14 de valor frente a 4886,91 de colateral en EUR)
    eur_haircut_pct: Dec = Field(default=Decimal("2.2"), ge=0, lt=100)
    # Comisión taker en %: 0,05 % verificado en la cuenta del usuario (nivel 1)
    taker_fee_pct: Dec = Field(default=Decimal("0.05"), ge=0, le=1)
    simulate_funding: bool = True
    simulate_orderbook_slippage: bool = True


class TelegramConfig(_Strict):
    enabled: bool = False


class HealthcheckConfig(_Strict):
    # URL en .env (HEALTHCHECK_URL): aviso externo si el bot deja de dar señales
    enabled: bool = False


class PathsConfig(_Strict):
    data_dir: Path = Path("data")


class LoggingConfig(_Strict):
    # true en el VPS: rota logrotate (el bot reabre el fichero al rotarse)
    external_rotation: bool = False


class Config(_Strict):
    mode: Mode = Mode.PAPER
    leader_address: str
    sizing: SizingConfig = SizingConfig()
    planner: PlannerConfig = PlannerConfig()
    execution: ExecutionConfig = ExecutionConfig()
    filters: FiltersConfig = FiltersConfig()
    symbols: SymbolsConfig = SymbolsConfig()
    risk: RiskConfig = RiskConfig()
    timing: TimingConfig = TimingConfig()
    sanity: SanityConfig = SanityConfig()
    paper: PaperConfig = PaperConfig()
    telegram: TelegramConfig = TelegramConfig()
    healthcheck: HealthcheckConfig = HealthcheckConfig()
    paths: PathsConfig = PathsConfig()
    logging: LoggingConfig = LoggingConfig()

    @property
    def run_dir(self) -> Path:
        """Directorio de ejecución: paper y live NUNCA comparten estado, bloqueo, logs ni CSV."""
        return self.paths.data_dir / self.mode.value

    @field_validator("leader_address")
    @classmethod
    def _address(cls, v: str) -> str:
        if not _HL_ADDRESS.match(v):
            raise ValueError("leader_address debe ser 0x seguido de 40 caracteres hex")
        return v.lower()

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.sizing.max_asset_usd > limits.HARD_MAX_NOTIONAL_TOTAL_USD:
            raise ValueError("max_asset_usd no puede superar el tope total")
        # El tope por activo no puede permitir más que el apalancamiento total
        if self.sizing.max_asset_pct_equity > self.sizing.max_total_leverage * 100:
            raise ValueError("max_asset_pct_equity supera max_total_leverage")
        return self


def load_config(path: Path) -> Config:
    """Carga y valida la configuración. Lanza ConfigError con un mensaje legible."""
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"no existe el fichero de configuración: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML no válido en {path}: {exc}") from exc
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        # include_input=False: no volcamos valores al mensaje (la config no
        # debería tener secretos, pero por si alguien los pega ahí).
        lines = [
            f"  {'.'.join(str(p) for p in err['loc']) or '(raíz)'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        ]
        raise ConfigError("configuración no válida:\n" + "\n".join(lines)) from None
