"""Cuenta simulada con precios reales de Kraken (sin claves).

Simulación:
- Colateral en EUR valorado con EUR/USD de Kraken (índice de PF_EURUSD) en
  cada consulta, menos el haircut configurado. PnL realizado, comisiones y
  funding se acumulan en un saldo USD aparte, como en la cuenta
  multi-colateral real.
- Órdenes límite IOC: se recorre el libro real de Kraken en el lado contrario
  mientras el precio respete el límite; lo que no cabe queda sin ejecutar
  (ejecución parcial). Sin simulación de libro: un único nivel al mejor
  bid/ask del ticker.
- reduceOnly como en el exchange: se recorta a la posición existente y se
  rechaza si no reduce nada.
- Comisión taker sobre el nocional ejecutado.
- Funding: en cada marca de tiempo del histórico real de Kraken (intervalo
  real del mercado, horario en los PF_ verificados) se cobra o paga
  tamaño x tasa absoluta; tasa positiva = pagan los largos. Se usa el tamaño
  de la posición en el momento de cobrarlo (aproximación: Kraken lo devenga
  de forma continua).
- cliOrdId duplicado: rechazado, como en Kraken (clientOrderIdAlreadyExist).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from copybot.config import PaperConfig
from copybot.exchange.base import (
    ExchangeError,
    FundingEvent,
    OrderRequest,
    OrderResult,
    OrderStatus,
)
from copybot.exchange.kraken_public import FundingRate, OrderBook, Ticker
from copybot.models import Side

log = logging.getLogger(__name__)
ZERO = Decimal(0)


class MarketData(Protocol):
    async def tickers(self) -> dict[str, Ticker]: ...
    async def orderbook(self, symbol: str) -> OrderBook: ...
    async def funding_rates(self, symbol: str) -> list[FundingRate]: ...
    async def eur_usd(self) -> Decimal: ...


@dataclass
class PaperPosition:
    size: Decimal  # con signo
    entry_price: Decimal


@dataclass
class PaperAccount:
    eur_collateral: Decimal
    usd_balance: Decimal = ZERO
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    funding_cursor: dict[str, datetime] = field(default_factory=dict)
    orders: dict[str, OrderResult] = field(default_factory=dict)

    @classmethod
    def new(cls, cfg: PaperConfig) -> PaperAccount:
        return cls(eur_collateral=cfg.initial_collateral_eur)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eur_collateral": str(self.eur_collateral),
            "usd_balance": str(self.usd_balance),
            "positions": {
                s: {"size": str(p.size), "entry_price": str(p.entry_price)}
                for s, p in self.positions.items()
            },
            "funding_cursor": {s: t.isoformat() for s, t in self.funding_cursor.items()},
            "orders": {
                k: {
                    "status": o.status.value, "filled_size": str(o.filled_size),
                    "avg_price": None if o.avg_price is None else str(o.avg_price),
                    "fee_usd": str(o.fee_usd), "reason": o.reason,
                }
                for k, o in self.orders.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PaperAccount:
        return cls(
            eur_collateral=Decimal(d["eur_collateral"]),
            usd_balance=Decimal(d["usd_balance"]),
            positions={
                s: PaperPosition(Decimal(p["size"]), Decimal(p["entry_price"]))
                for s, p in d.get("positions", {}).items()
            },
            funding_cursor={
                s: datetime.fromisoformat(t) for s, t in d.get("funding_cursor", {}).items()
            },
            orders={
                k: OrderResult(
                    cli_ord_id=k, status=OrderStatus(o["status"]),
                    filled_size=Decimal(o["filled_size"]),
                    avg_price=None if o["avg_price"] is None else Decimal(o["avg_price"]),
                    fee_usd=Decimal(o["fee_usd"]), reason=o.get("reason", ""),
                )
                for k, o in d.get("orders", {}).items()
            },
        )


def apply_fill(
    pos: PaperPosition | None, delta: Decimal, price: Decimal
) -> tuple[PaperPosition | None, Decimal]:
    """Nueva posición y PnL realizado tras ejecutar `delta` (con signo) a `price`."""
    if pos is None or pos.size == 0:
        return PaperPosition(delta, price), ZERO
    s, e = pos.size, pos.entry_price
    if (s > 0) == (delta > 0):
        total = s + delta
        return PaperPosition(total, (s * e + delta * price) / total), ZERO
    closing = min(abs(delta), abs(s))
    realized = closing * (price - e) * (1 if s > 0 else -1)
    remaining = s + delta
    if remaining == 0:
        return None, realized
    if (remaining > 0) == (s > 0):
        return PaperPosition(remaining, e), realized
    return PaperPosition(remaining, price), realized  # cruzó por cero


def walk_book(
    levels: tuple[tuple[Decimal, Decimal], ...], side: Side, size: Decimal, limit: Decimal
) -> tuple[Decimal, Decimal]:
    """(cantidad ejecutada, nocional) recorriendo niveles dentro del límite."""
    filled = notional = ZERO
    for px, qty in levels:
        if (side is Side.BUY and px > limit) or (side is Side.SELL and px < limit):
            break
        take = min(qty, size - filled)
        filled += take
        notional += take * px
        if filled == size:
            break
    return filled, notional


class PaperExchange:
    mode = "paper"

    def __init__(
        self,
        account: PaperAccount,
        market: MarketData,
        cfg: PaperConfig,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.account = account
        self._market = market
        self._cfg = cfg
        self._now = now

    async def equity_usd(self) -> Decimal:
        eurusd = await self._market.eur_usd()
        collateral = self.account.eur_collateral * eurusd * (1 - self._cfg.eur_haircut_pct / 100)
        unrealized = ZERO
        if self.account.positions:
            tickers = await self._market.tickers()
            for sym, p in self.account.positions.items():
                t = tickers.get(sym)
                if t is None:
                    raise ExchangeError(f"sin precio mark para {sym}")
                unrealized += p.size * (t.mark_price - p.entry_price)
        return collateral + self.account.usd_balance + unrealized

    async def positions(self) -> dict[str, Decimal]:
        return {s: p.size for s, p in self.account.positions.items()}

    async def find_order(self, cli_ord_id: str) -> OrderResult | None:
        return self.account.orders.get(cli_ord_id)

    def _reject(self, req: OrderRequest, reason: str, store: bool = True) -> OrderResult:
        result = OrderResult(req.cli_ord_id, OrderStatus.REJECTED, ZERO, None, ZERO, reason)
        if store:
            self.account.orders[req.cli_ord_id] = result
        return result

    async def send_order(self, req: OrderRequest) -> OrderResult:
        if req.cli_ord_id in self.account.orders:
            return self._reject(req, "clientOrderIdAlreadyExist", store=False)
        if req.size <= 0 or req.limit_price <= 0:
            return self._reject(req, "invalidSize")

        ticker = (await self._market.tickers()).get(req.symbol)
        if ticker is None or ticker.suspended:
            return self._reject(req, "marketSuspended")

        pos = self.account.positions.get(req.symbol)
        size = req.size
        if req.reduce_only:
            cur = pos.size if pos else ZERO
            opposite = (cur > 0 and req.side is Side.SELL) or (cur < 0 and req.side is Side.BUY)
            if not opposite:
                return self._reject(req, "wouldNotReducePosition")
            size = min(size, abs(cur))

        if self._cfg.simulate_orderbook_slippage:
            book = await self._market.orderbook(req.symbol)
            levels = book.asks if req.side is Side.BUY else book.bids
        else:
            best = ticker.ask if req.side is Side.BUY else ticker.bid
            levels = ((best, size),) if best else ()
        filled, notional = walk_book(levels, req.side, size, req.limit_price)

        if filled == 0:
            result = OrderResult(req.cli_ord_id, OrderStatus.NOT_FILLED, ZERO, None, ZERO,
                                 "iocWouldNotExecute")
            self.account.orders[req.cli_ord_id] = result
            return result

        avg = notional / filled
        fee = notional * self._cfg.taker_fee_pct / 100
        delta = filled if req.side is Side.BUY else -filled
        new_pos, realized = apply_fill(pos, delta, avg)
        if new_pos is None:
            self.account.positions.pop(req.symbol, None)
            self.account.funding_cursor.pop(req.symbol, None)
        else:
            if pos is None:
                self.account.funding_cursor[req.symbol] = self._now()
            self.account.positions[req.symbol] = new_pos
        self.account.usd_balance += realized - fee

        status = OrderStatus.FILLED if filled == size else OrderStatus.PARTIAL
        result = OrderResult(req.cli_ord_id, status, filled, avg, fee)
        self.account.orders[req.cli_ord_id] = result
        return result

    async def collect_funding(self, now: datetime) -> list[FundingEvent]:
        if not self._cfg.simulate_funding:
            return []
        events: list[FundingEvent] = []
        for sym, pos in list(self.account.positions.items()):
            since = self.account.funding_cursor.get(sym, now)
            rates = await self._market.funding_rates(sym)
            for r in rates:
                if since < r.timestamp <= now:
                    amount = -pos.size * r.rate  # tasa positiva: pagan los largos
                    self.account.usd_balance += amount
                    events.append(FundingEvent(r.timestamp, sym, pos.size, r.rate, amount))
                    since = r.timestamp
            self.account.funding_cursor[sym] = since
        return events
