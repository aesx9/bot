"""Tipos de dominio. Todo importe y tamaño es Decimal, nunca float."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


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
