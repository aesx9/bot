"""Ejecución real en Kraken Futures. NUNCA se ha probado con dinero real.

Endpoints (verificados contra la documentación oficial, 2026-10-08):
- GET  /derivatives/api/v3/accounts -> accounts.flex.marginEquity:
  "[Balance Value in USD * (1-Haircut)] + unrealised PnL as margin". Es el
  capital propio: ya incluye haircut, conversión y PnL no realizado. (El campo
  portfolioValue NO incluye el haircut según la documentación.)
- GET  /derivatives/api/v3/openpositions -> openPositions[{symbol, side
  long|short, size, price}].
- POST /derivatives/api/v3/sendorder (orderType=ioc | stp, cliOrdId,
  reduceOnly, triggerSignal=mark) -> sendStatus{status, orderEvents[]}; las
  ejecuciones llegan como eventos EXECUTION{price, amount}.
- GET  /derivatives/api/v3/fills -> últimos 100 fills, con cliOrdId: fuente
  para reconciliar una orden cuya respuesta se perdió.
- POST /derivatives/api/v3/orders/status -> solo órdenes abiertas o cerradas
  en los ÚLTIMOS 5 SEGUNDOS: complemento, nunca la única fuente.
- GET  /derivatives/api/v3/openorders y POST /cancelorder: stops de catástrofe.
- GET  /api/history/v3/account-log?info=funding rate change: funding real.

La comisión real no viene en la respuesta de la orden: queda vacía en
trades.csv (el export fiscal la tomará del log de cuenta de Kraken).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from copybot import limits
from copybot.exchange.base import (
    ExchangeError,
    FundingEvent,
    OrderRequest,
    OrderResult,
    OrderStatus,
)
from copybot.exchange.kraken_auth import KrakenPrivateClient
from copybot.models import MarketSpec, Side
from copybot.state import BotState

log = logging.getLogger(__name__)
ZERO = Decimal(0)

API = "/derivatives/api/v3"
STOP_PREFIX = "cs-"  # cliOrdId de los stops de catástrofe del bot
LIQUIDATION_SAFETY = Decimal("0.8")  # el stop debe saltar antes del 80 % del margen libre
FUNDING_POLL_SECONDS = 300

# sendStatus.status que significan "no ejecutada, sin error del exchange"
_NOT_FILLED = {"placed", "cancelled", "iocWouldNotExecute"}
_FILLED = {"filled", "partiallyFilled"}


def _dec(v: Any, what: str) -> Decimal:
    try:
        d = Decimal(str(v))
    except (ArithmeticError, ValueError):
        raise ExchangeError(f"{what}: número no válido") from None
    if not d.is_finite():
        raise ExchangeError(f"{what}: número no finito")
    return d


def _executions(events: Any) -> tuple[Decimal, Decimal | None]:
    filled = notional = ZERO
    for e in events or []:
        if isinstance(e, dict) and e.get("type") == "EXECUTION":
            amount = _dec(e.get("amount"), "ejecución")
            filled += amount
            notional += amount * _dec(e.get("price"), "ejecución")
    return filled, (notional / filled if filled else None)


class LiveExchange:
    mode = "live"

    def __init__(
        self,
        client: KrakenPrivateClient,
        state: BotState,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._c = client
        self._state = state
        self._now = now
        self._last_funding_poll: datetime | None = None

    # --- cuenta ---

    async def _flex(self) -> dict[str, Any]:
        payload = await self._c.request("GET", f"{API}/accounts")
        flex = (payload.get("accounts") or {}).get("flex")
        if not isinstance(flex, dict):
            raise ExchangeError("accounts: falta la cuenta multi-colateral (flex)")
        return flex

    async def equity_usd(self) -> Decimal:
        return _dec((await self._flex()).get("marginEquity"), "marginEquity")

    async def margin_buffer_usd(self) -> Decimal:
        """Margen hasta liquidación: marginEquity - margen de mantenimiento."""
        flex = await self._flex()
        return (_dec(flex.get("marginEquity"), "marginEquity")
                - _dec(flex.get("maintenanceMargin"), "maintenanceMargin"))

    async def _open_positions(self) -> list[dict[str, Any]]:
        payload = await self._c.request("GET", f"{API}/openpositions")
        rows = payload.get("openPositions")
        if not isinstance(rows, list):
            raise ExchangeError("openpositions: falta la lista")
        return rows

    async def positions(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for p in await self._open_positions():
            size = _dec(p.get("size"), "posición")
            if p.get("side") not in ("long", "short"):
                raise ExchangeError("openpositions: lado desconocido")
            if size:
                out[str(p["symbol"]).upper()] = size if p["side"] == "long" else -size
        return out

    # --- órdenes ---

    async def send_order(self, req: OrderRequest) -> OrderResult:
        payload = await self._c.request("POST", f"{API}/sendorder", [
            ("orderType", "ioc"), ("symbol", req.symbol), ("side", req.side.value),
            ("size", str(req.size)), ("limitPrice", str(req.limit_price)),
            ("cliOrdId", req.cli_ord_id), ("reduceOnly", "true" if req.reduce_only else "false"),
        ])
        send = payload.get("sendStatus")
        if not isinstance(send, dict):
            raise ExchangeError("sendorder: falta sendStatus")
        status = str(send.get("status"))
        if status == "clientOrderIdAlreadyExist":
            # La orden ya había llegado: el ejecutor reconcilia por cliOrdId
            raise ExchangeError("sendorder: cliOrdId ya existente")
        filled, avg = _executions(send.get("orderEvents"))
        if filled > 0:
            st = OrderStatus.FILLED if filled >= req.size else OrderStatus.PARTIAL
            return OrderResult(req.cli_ord_id, st, filled, avg, None, status)
        if status in _NOT_FILLED or status in _FILLED:
            return OrderResult(req.cli_ord_id, OrderStatus.NOT_FILLED, ZERO, None, ZERO, status)
        return OrderResult(req.cli_ord_id, OrderStatus.REJECTED, ZERO, None, ZERO, status)

    async def find_order(self, cli_ord_id: str) -> OrderResult | None:
        payload = await self._c.request("GET", f"{API}/fills")
        fills = [f for f in payload.get("fills") or []
                 if isinstance(f, dict) and f.get("cliOrdId") == cli_ord_id]
        if fills:
            filled = sum((_dec(f.get("size"), "fill") for f in fills), ZERO)
            notional = sum((_dec(f.get("size"), "fill") * _dec(f.get("price"), "fill")
                            for f in fills), ZERO)
            return OrderResult(cli_ord_id, OrderStatus.FILLED, filled, notional / filled, None,
                               "reconciliada por fills")
        payload = await self._c.request("POST", f"{API}/orders/status",
                                        [("cliOrdIds", cli_ord_id)])
        for o in payload.get("orders") or []:
            order = o.get("order") if isinstance(o, dict) else None
            if not isinstance(order, dict) or order.get("cliOrdId") != cli_ord_id:
                continue
            st = o.get("status")
            done = _dec(order.get("filled") or 0, "filled")
            if st in ("REJECTED", "CANCELLED") and done == 0:
                kind = OrderStatus.REJECTED if st == "REJECTED" else OrderStatus.NOT_FILLED
                return OrderResult(cli_ord_id, kind, ZERO, None, ZERO, str(st))
            return None  # ejecutada pero sin fills visibles todavía, o aún viva: esperar
        return None

    # --- funding real ---

    async def collect_funding(self, now: datetime) -> list[FundingEvent]:
        if (self._last_funding_poll is not None
                and (now - self._last_funding_poll).total_seconds() < FUNDING_POLL_SECONDS):
            return []
        self._last_funding_poll = now
        cursor = self._state.live_funding_cursor_ms
        if cursor is None:  # primer arranque: no se importa el histórico anterior
            self._state.live_funding_cursor_ms = int(now.timestamp() * 1000)
            return []
        payload = await self._c.request("GET", "/api/history/v3/account-log", [
            ("since", str(cursor)), ("sort", "asc"), ("count", "25"),
            ("info", "funding rate change"),
        ])
        events: list[FundingEvent] = []
        for e in payload.get("logs") or []:
            if not isinstance(e, dict) or e.get("info") != "funding rate change":
                continue
            ts = datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00"))
            if e.get("old_balance") is not None and e.get("new_balance") is not None:
                amount = _dec(e["new_balance"], "funding") - _dec(e["old_balance"], "funding")
            else:
                amount = _dec(e.get("realized_funding") or 0, "funding")
            rate = _dec(e.get("funding_rate") or 0, "funding")
            events.append(FundingEvent(ts, str(e.get("contract") or "").upper(), ZERO, rate,
                                       amount))
            cursor = max(cursor, int(ts.timestamp() * 1000) + 1)
        self._state.live_funding_cursor_ms = cursor
        return events

    # --- stops de catástrofe ---

    async def sync_catastrophe_stops(
        self, managed: Mapping[str, Decimal], markets: Mapping[str, MarketSpec], pct: Decimal
    ) -> list[str]:
        """Un stop reduceOnly (a mercado al saltar, señal mark) por posición gestionada,
        a `pct` % del precio de entrada y siempre antes de la liquidación estimada."""
        warnings: list[str] = []
        entries = {str(p["symbol"]).upper(): _dec(p.get("price"), "entrada")
                   for p in await self._open_positions()}
        buffer = await self.margin_buffer_usd()
        payload = await self._c.request("GET", f"{API}/openorders")
        ours = [o for o in payload.get("openOrders") or []
                if isinstance(o, dict) and str(o.get("cliOrdId") or "").startswith(STOP_PREFIX)]

        total_loss = ZERO
        for symbol, size in managed.items():
            entry = entries.get(symbol)
            spec = markets.get(symbol)
            if entry is None or spec is None or size == 0:
                continue
            eff = pct
            max_loss = buffer * LIQUIDATION_SAFETY
            loss = abs(size) * entry * eff / 100
            if loss > max_loss:
                eff = max(max_loss, ZERO) / (abs(size) * entry) * 100
                warnings.append(
                    f"{symbol}: el stop al {pct} % quedaría tras la liquidación estimada; "
                    f"se acerca al {eff:.2f} %"
                )
                if eff < limits.HARD_MIN_CATASTROPHE_STOP_PCT:
                    warnings.append(f"{symbol}: posición demasiado grande para su margen libre")
                loss = abs(size) * entry * eff / 100
            total_loss += loss
            side = Side.SELL if size > 0 else Side.BUY
            raw = entry * (1 - eff / 100) if size > 0 else entry * (1 + eff / 100)
            rounding = ROUND_CEILING if size > 0 else ROUND_FLOOR  # hacia la entrada
            stop = (raw / spec.tick_size).to_integral_value(rounding=rounding) * spec.tick_size

            current = [o for o in ours if str(o.get("symbol")).upper() == symbol]
            keep = [o for o in current
                    if o.get("side") == side.value
                    and _dec(o.get("unfilledSize"), "stop") == abs(size)
                    and abs(_dec(o.get("stopPrice"), "stop") - stop) <= spec.tick_size]
            if keep and len(current) == 1:
                continue
            for o in current:
                await self._cancel(o)
            result = await self._c.request("POST", f"{API}/sendorder", [
                ("orderType", "stp"), ("symbol", symbol), ("side", side.value),
                ("size", str(abs(size))), ("stopPrice", str(stop)), ("triggerSignal", "mark"),
                ("reduceOnly", "true"), ("cliOrdId", STOP_PREFIX + uuid.uuid4().hex),
            ])
            status = (result.get("sendStatus") or {}).get("status")
            if status != "placed":
                warnings.append(f"{symbol}: no se pudo colocar el stop de catástrofe ({status})")

        for o in ours:  # stops de posiciones que ya no existen
            if str(o.get("symbol")).upper() not in managed:
                await self._cancel(o)
        if total_loss > buffer * LIQUIDATION_SAFETY:
            warnings.append("si saltaran todos los stops a la vez, la pérdida se acercaría "
                            "a la liquidación: reduce el apalancamiento")
        return warnings

    async def _cancel(self, order: Mapping[str, Any]) -> None:
        await self._c.request("POST", f"{API}/cancelorder",
                              [("cliOrdId", str(order.get("cliOrdId")))])
