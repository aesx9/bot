"""Tipos de dominio. Todo importe y tamaño es Decimal, nunca float."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from enum import StrEnum


def plain(d: Decimal) -> str:
    """Decimal en notación posicional ('5000', nunca '5E+3'; '0.0000009', nunca '9E-7'),
    el único formato que se envía a Kraken o se escribe en los CSV."""
    return format(d, "f")


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @staticmethod
    def for_delta(delta: Decimal) -> Side:
        return Side.BUY if delta > 0 else Side.SELL


class ActionKind(StrEnum):
    OPEN = "open"
    INCREASE = "increase"
    REDUCE = "reduce"
    CLOSE = "close"
    FLIP_CLOSE = "flip_close"  # cierre reduceOnly de un cambio de dirección
    FLIP_OPEN = "flip_open"  # apertura separada en la nueva dirección


REDUCING_KINDS = frozenset({ActionKind.REDUCE, ActionKind.CLOSE, ActionKind.FLIP_CLOSE})


@dataclass(frozen=True)
class LeaderSnapshot:
    """Estado del líder en un instante (tamaños con signo, en unidades del líder)."""

    equity_usd: Decimal
    positions: dict[str, Decimal]  # coin -> tamaño con signo (+ largo, - corto)
    mids: dict[str, Decimal]  # coin -> precio medio
    timestamp: datetime  # UTC


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    symbol: str
    side: Side
    size: Decimal  # siempre > 0, en unidades del contrato Kraken
    reduce_only: bool
    ref_price: Decimal
    reason: str = ""

    def __post_init__(self) -> None:
        if self.size <= 0:
            raise ValueError("Action.size debe ser > 0")
        if self.kind in REDUCING_KINDS and not self.reduce_only:
            raise ValueError(f"{self.kind} debe ser reduceOnly")

    @property
    def notional_usd(self) -> Decimal:
        return self.size * self.ref_price


@dataclass(frozen=True)
class SizingResult:
    targets: dict[str, Decimal]  # símbolo Kraken -> tamaño objetivo con signo
    scale_applied: Decimal = Decimal(1)  # <1 si se redujo por apalancamiento total
    capped_assets: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class MarketSpec:
    """Reglas de tamaño de un mercado Kraken, leídas de /instruments.

    Kraken no publica un tamaño mínimo aparte para los PF_: el tamaño debe ser
    múltiplo de 10^-contractValueTradePrecision, así que el mínimo es un paso.
    """

    symbol: str
    size_step: Decimal  # 10^-contractValueTradePrecision (p.ej. 0.0001 o 1000)
    tick_size: Decimal
    max_position_size: Decimal

    def __post_init__(self) -> None:
        if not (self.size_step > 0 and self.tick_size > 0 and self.max_position_size > 0):
            raise ValueError(f"{self.symbol}: paso, tick y tamaño máximo deben ser > 0")

    @property
    def min_size(self) -> Decimal:
        return self.size_step

    def _round(self, size: Decimal, rounding: str) -> Decimal:
        if size < 0:
            raise ValueError("se redondean magnitudes, no tamaños con signo")
        with localcontext(prec=60):
            steps = (size / self.size_step).to_integral_value(rounding=rounding)
            return steps * self.size_step

    def round_down(self, size: Decimal) -> Decimal:
        """Mayor múltiplo del paso que no supera `size`."""
        return self._round(size, ROUND_FLOOR)

    def round_up(self, size: Decimal) -> Decimal:
        """Menor múltiplo del paso que no queda por debajo de `size`."""
        return self._round(size, ROUND_CEILING)
