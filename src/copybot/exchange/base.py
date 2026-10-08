"""Interfaz común de ejecución: paper y live la implementan igual.

No hay (ni habrá) ninguna operación de retiro o transferencia de fondos.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from copybot.models import Side


class ExchangeError(Exception):
    """Fallo de comunicación o respuesta inesperada del exchange."""


class OrderStatus(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"  # IOC: el resto se cancela; se reconcilia el ciclo siguiente
    NOT_FILLED = "not_filled"  # IOC sin liquidez dentro del precio límite
    REJECTED = "rejected"


@dataclass(frozen=True)
class OrderRequest:
    cli_ord_id: str
    symbol: str
    side: Side
    size: Decimal  # > 0, ya redondeado al paso del mercado
    limit_price: Decimal  # IOC: nunca se ejecuta peor que esto
    reduce_only: bool


@dataclass(frozen=True)
class OrderResult:
    cli_ord_id: str
    status: OrderStatus
    filled_size: Decimal
    avg_price: Decimal | None
    fee_usd: Decimal
    reason: str = ""


@dataclass(frozen=True)
class FundingEvent:
    timestamp: datetime  # UTC
    symbol: str
    position: Decimal  # tamaño con signo al aplicarse
    rate: Decimal  # absoluto, USD por unidad
    amount_usd: Decimal  # + cobrado, - pagado


class Exchange(Protocol):
    mode: str  # "paper" | "live"

    async def equity_usd(self) -> Decimal: ...

    async def positions(self) -> dict[str, Decimal]: ...

    async def send_order(self, req: OrderRequest) -> OrderResult: ...

    async def find_order(self, cli_ord_id: str) -> OrderResult | None:
        """Estado de una orden por su cliOrdId, o None si el exchange no la conoce."""
        ...

    async def collect_funding(self, now: datetime) -> list[FundingEvent]: ...
