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

import contextlib
import logging
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from copybot.models import MarketSpec, Side, plain
from copybot.state import BotState

log = logging.getLogger(__name__)
ZERO = Decimal(0)

API = "/derivatives/api/v3"
STOP_PREFIX = "cs-"  # cliOrdId de los stops de catástrofe del bot
LIQUIDATION_SAFETY = Decimal("0.8")  # el stop debe saltar antes del 80 % del margen libre
FUNDING_POLL_SECONDS = 300  # cada cuánto se lee el log de cuenta (funding y comisiones)
FILLS_PAGE = 100  # /fills devuelve como mucho los 100 últimos; más antiguos, con lastFillTime
FILLS_MAX_PAGES = 20
LOG_PAGE = 50  # entradas del account-log por petición
LOG_MAX_PAGES = 40
LOG_INFO = ("funding rate change", "futures trade", "futures liquidation",
            "futures partial liquidation")
SEEN_MEMORY = 500  # ids recordados (fills y entradas del log) para no repetir

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


@contextlib.contextmanager
def malformed(what: str) -> Iterator[None]:
    """Una respuesta con otra forma de la esperada es un error del exchange (ExchangeError),
    nunca una excepción suelta que se escape de los controles de ciclo."""
    try:
        yield
    except ExchangeError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError, ArithmeticError, IndexError) as exc:
        raise ExchangeError(f"{what}: respuesta malformada ({type(exc).__name__})") from None


def _executions(events: Any) -> tuple[Decimal, Decimal | None]:
    filled = notional = ZERO
    for e in events or []:
        if isinstance(e, dict) and e.get("type") == "EXECUTION":
            amount = _dec(e.get("amount"), "ejecución")
            filled += amount
            notional += amount * _dec(e.get("price"), "ejecución")
    return filled, (notional / filled if filled else None)


@dataclass
class _Staged:
    """Lo nuevo del libro, preparado y aún sin confirmar."""

    fills: list[dict[str, Any]] = field(default_factory=list)
    fees: list[dict[str, Any]] = field(default_factory=list)
    fill_ids: list[str] = field(default_factory=list)
    fill_id_set: set[str] = field(default_factory=set)
    log_uids: list[str] = field(default_factory=list)
    log_uid_set: set[str] = field(default_factory=set)
    cursor_ms: int | None = None
    polled_log_at: datetime | None = None


def _parse_ts(value: Any) -> datetime:
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError("fecha sin zona horaria")
    return ts


def _iso_ms(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _log_ms(e: Mapping[str, Any]) -> int | None:
    try:
        return int(_parse_ts(e["date"]).timestamp() * 1000)
    except (KeyError, TypeError, ValueError):
        return None


def _log_uid(e: Mapping[str, Any]) -> str:
    """Identificador de una entrada del log: booking_uid o, si falta, una clave compuesta."""
    uid = str(e.get("booking_uid") or "")
    if uid:
        return uid
    return "|".join(str(e.get(k)) for k in ("date", "info", "contract", "fee", "new_balance"))


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
        self._last_log_poll: datetime | None = None
        self._positions: dict[str, Decimal] = {}  # última lectura, para el signo del funding
        self.alerts: list[str] = []  # avisos para el motor (se vacían con drain_alerts)
        # Libro en dos fases: collect_funding() PREPARA lo nuevo sin tocar el estado; el motor
        # lo escribe en los CSV y solo entonces commit_ledger() avanza cursores e ids vistos.
        self._staged: _Staged | None = None

    # --- cuenta ---

    async def _flex(self) -> dict[str, Any]:
        payload = await self._c.request("GET", f"{API}/accounts")
        accounts = payload.get("accounts") or {}
        flex = accounts.get("flex") if isinstance(accounts, dict) else None
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
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ExchangeError("openpositions: falta la lista")
        return rows

    async def positions(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for p in await self._open_positions():
            with malformed("openpositions"):
                size = _dec(p.get("size"), "posición")
                if p.get("side") not in ("long", "short"):
                    raise ExchangeError("openpositions: lado desconocido")
                if size:
                    out[str(p["symbol"]).upper()] = size if p["side"] == "long" else -size
        self._positions = dict(out)
        return out

    def drain_alerts(self) -> list[str]:
        out, self.alerts = self.alerts, []
        return out

    def drain_ledger(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Fills y comisiones preparados (NO se vacían: hasta commit_ledger() siguen
        pendientes, de modo que un fallo al escribir el CSV no pierde nada)."""
        st = self._staged
        return (list(st.fills), list(st.fees)) if st else ([], [])

    # --- órdenes ---

    async def send_order(self, req: OrderRequest) -> OrderResult:
        with malformed("sendorder"):
            return await self._send_order(req)

    async def _send_order(self, req: OrderRequest) -> OrderResult:
        payload = await self._c.request("POST", f"{API}/sendorder", [
            ("orderType", "ioc"), ("symbol", req.symbol), ("side", req.side.value),
            ("size", plain(req.size)), ("limitPrice", plain(req.limit_price)),
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
        with malformed("find_order"):
            return await self._find_order(cli_ord_id)

    async def _find_order(self, cli_ord_id: str) -> OrderResult | None:
        payload = await self._c.request("GET", f"{API}/fills")
        all_fills = payload.get("fills") or []
        if not isinstance(all_fills, list):
            raise ExchangeError("fills: se esperaba una lista")
        fills = [f for f in all_fills if isinstance(f, dict) and f.get("cliOrdId") == cli_ord_id]
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

    # --- funding, comisiones y fills reales (libro para el export fiscal) ---

    async def prepare_ledger(self, now: datetime) -> None:
        """Línea base del libro fiscal: debe ejecutarse ANTES de enviar la primera orden.

        Lo que ya hay en /fills y en el log de cuenta es anterior al bot y no se importa.
        Si la línea base se fijara después (en collect_funding, tras operar), se
        descartarían como "anteriores" los fills de las primeras órdenes del propio bot."""
        if self._state.live_funding_cursor_ms is not None:
            return
        staged = _Staged()
        await self._stage_fills(staged, baseline=True)
        staged.cursor_ms = int(now.timestamp() * 1000)
        self._staged = staged
        self.commit_ledger()

    async def collect_funding(self, now: datetime) -> list[FundingEvent]:
        """Prepara lo nuevo del libro: fills (en cada llamada, con paginación) y, cada
        FUNDING_POLL_SECONDS, funding y comisiones del log de cuenta.

        NO avanza cursores ni ids vistos: el motor escribe primero los CSV (fills y
        comisiones con drain_ledger(), eventos de funding con el valor devuelto) y después
        llama a commit_ledger(). Si algo falla en medio, la siguiente llamada vuelve a
        encontrar lo mismo y los CSV no duplican (son idempotentes por id)."""
        if self._state.live_funding_cursor_ms is None:
            await self.prepare_ledger(now)
        staged = _Staged()
        await self._stage_fills(staged, baseline=False)
        events: list[FundingEvent] = []
        if (self._last_log_poll is None
                or (now - self._last_log_poll).total_seconds() >= FUNDING_POLL_SECONDS):
            events = await self._stage_account_log(staged)
            staged.polled_log_at = now
        self._staged = staged
        return events

    def commit_ledger(self) -> None:
        """Confirma lo preparado, DESPUÉS de haberlo escrito en los CSV."""
        st = self._staged
        if st is None:
            return
        self._state.fills_seen = (self._state.fills_seen + st.fill_ids)[-SEEN_MEMORY:]
        self._state.log_seen = (self._state.log_seen + st.log_uids)[-SEEN_MEMORY:]
        if st.cursor_ms is not None:
            current = self._state.live_funding_cursor_ms
            self._state.live_funding_cursor_ms = (
                st.cursor_ms if current is None else max(current, st.cursor_ms))
        if st.polled_log_at is not None:
            self._last_log_poll = st.polled_log_at
        self._staged = None

    # -- account-log (funding y comisiones) --

    async def _stage_account_log(self, staged: _Staged) -> list[FundingEvent]:
        cursor = self._state.live_funding_cursor_ms
        assert cursor is not None
        seen = set(self._state.log_seen)
        events: list[FundingEvent] = []
        # since-1: sea inclusivo o exclusivo en el exchange, se vuelven a pedir las entradas
        # del milisegundo del cursor (las ya vistas se descartan por booking_uid); así no se
        # pierde ningún evento que comparta milisegundo con el último procesado
        since = cursor - 1
        for _ in range(LOG_MAX_PAGES):
            payload = await self._c.request("GET", "/api/history/v3/account-log", [
                ("since", str(since)), ("sort", "asc"), ("count", str(LOG_PAGE)),
                *(("info", i) for i in LOG_INFO),
            ])
            logs = payload.get("logs") or []
            if not isinstance(logs, list):
                raise ExchangeError("account-log: se esperaba una lista")
            last_ms: int | None = None
            for e in logs:
                if not isinstance(e, dict):
                    continue
                uid = _log_uid(e)
                ms = _log_ms(e)
                if ms is not None:
                    last_ms = ms if last_ms is None else max(last_ms, ms)
                if uid in seen or uid in staged.log_uid_set:
                    continue
                staged.log_uid_set.add(uid)
                staged.log_uids.append(uid)
                if ms is not None:
                    staged.cursor_ms = ms if staged.cursor_ms is None else max(
                        staged.cursor_ms, ms)
                try:
                    self._process_log_entry(e, staged, events)
                except (KeyError, TypeError, AttributeError, ValueError, ArithmeticError,
                        ExchangeError) as exc:
                    # Se salta esa entrada (si no, el cursor no avanzaría nunca) y se avisa:
                    # el libro fiscal tendría un hueco que hay que revisar a mano.
                    self.alerts.append(
                        f"account-log: entrada ilegible ({type(exc).__name__}) "
                        f"[date={e.get('date')!r}, info={e.get('info')!r}, "
                        f"booking_uid={e.get('booking_uid')!r}]: no se importa; revísala a mano")
            if len(logs) < LOG_PAGE or last_ms is None:
                return events
            # Siguiente página: desde (último ms - 1), de modo que lo que comparta milisegundo
            # con la última entrada y no cupo vuelva a salir (las ya vistas se descartan).
            # Si así no se avanza (una página entera en el mismo milisegundo) no queda más
            # remedio que saltar: se prueba con el propio ms y después con el siguiente, y se
            # avisa de que puede faltar alguna entrada en vez de repetir la misma página.
            nxt = last_ms - 1
            if nxt <= since:
                nxt = last_ms if last_ms > since else last_ms + 1
                self.alerts.append(
                    f"account-log: {LOG_PAGE} entradas o más con el mismo milisegundo "
                    f"({last_ms}): puede faltar alguna; revisa fees.csv y funding.csv")
            since = nxt
        self.alerts.append(f"account-log: más de {LOG_MAX_PAGES} páginas pendientes; "
                           "el resto se importará en el siguiente sondeo")
        return events

    def _process_log_entry(self, e: Mapping[str, Any], staged: _Staged,
                           events: list[FundingEvent]) -> None:
        ts = datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            raise ValueError("fecha sin zona horaria")
        symbol = str(e.get("contract") or "").upper()
        if e.get("info") == "funding rate change":
            events.append(self._funding_event(e, ts, symbol))
        elif e.get("fee") is not None:
            staged.fees.append({
                "timestamp": ts, "symbol": symbol, "fee": _dec(e["fee"], "comisión"),
                "currency": str(e.get("collateral") or e.get("asset") or "").upper(),
                "info": str(e.get("info")), "booking_uid": str(e.get("booking_uid") or ""),
            })

    def _funding_event(self, e: Mapping[str, Any], ts: datetime, symbol: str) -> FundingEvent:
        rate = _dec(e.get("funding_rate") or 0, "funding")
        realized = _dec(e.get("realized_funding") or 0, "funding")
        if e.get("old_balance") is not None and e.get("new_balance") is not None:
            amount = _dec(e["new_balance"], "funding") - _dec(e["old_balance"], "funding")
        else:
            amount = realized
        position = self._positions.get(symbol, ZERO)
        self._check_funding_sign(symbol, position, rate, amount, realized)
        return FundingEvent(ts, symbol, position, rate, amount, booking_uid=_log_uid(e))

    def _check_funding_sign(self, symbol: str, position: Decimal, rate: Decimal,
                            amount: Decimal, realized: Decimal) -> None:
        """Con tasa positiva pagan los largos. Si el dato real no cuadra, alerta:
        el registro fiscal de funding pagado/cobrado dependería de ello."""
        problems = []
        if position and rate and amount:
            expected_paid = (position > 0) == (rate > 0)
            if expected_paid != (amount < 0):
                problems.append(
                    f"{symbol}: funding {amount} USD con posición {position} y tasa {rate}; "
                    f"se esperaba {'pago' if expected_paid else 'cobro'}"
                )
        if realized and amount and (realized > 0) != (amount > 0):
            problems.append(f"{symbol}: realized_funding ({realized}) y la variación de saldo "
                            f"({amount}) tienen signos distintos")
        if problems:
            self.alerts.extend("SIGNO DEL FUNDING: " + p + ". Revisa funding.csv" for p in problems)
        elif position and rate and amount and not self._state.funding_sign_verified:
            self._state.funding_sign_verified = True
            log.warning("signo del funding real verificado con %s (tasa %s, importe %s)",
                        symbol, rate, amount)

    # -- /fills --

    async def _fetch_fills(self, last_fill_time: str | None) -> list[dict[str, Any]]:
        payload = await self._c.request(
            "GET", f"{API}/fills", [("lastFillTime", last_fill_time)] if last_fill_time else None)
        fills = payload.get("fills") or []
        if not isinstance(fills, list):
            raise ExchangeError("fills: se esperaba una lista")
        return [f for f in fills if isinstance(f, dict)]

    async def _stage_fills(self, staged: _Staged, *, baseline: bool) -> None:
        """Fills nuevos, de los más recientes a los más antiguos hasta topar con uno ya visto.

        /fills devuelve como mucho FILLS_PAGE: si una página llega llena y toda es nueva,
        hay más antiguos y se pide la siguiente con lastFillTime = (el más antiguo + 1 ms),
        de modo que los fills del mismo milisegundo en el borde de página vuelven a salir
        (se descartan por fill_id). baseline=True solo marca como vistos los existentes."""
        seen = set(self._state.fills_seen)
        param: str | None = None
        for _ in range(FILLS_MAX_PAGES):
            page = await self._fetch_fills(param)
            page_has_seen = False
            times: list[datetime] = []
            for f in sorted(page, key=lambda f: str(f.get("fillTime"))):
                fid = str(f.get("fill_id") or "")
                if not fid:
                    continue
                with contextlib.suppress(KeyError, TypeError, ValueError, AttributeError):
                    times.append(_parse_ts(f["fillTime"]))
                if fid in seen:
                    page_has_seen = True
                    continue
                if fid in staged.fill_id_set:
                    continue  # ya salió en la página anterior (solapamiento del borde)
                if baseline:  # fills anteriores al primer arranque live: no son del bot
                    staged.fill_id_set.add(fid)
                    staged.fill_ids.append(fid)
                    continue
                row = self._fill_row(f, fid)
                if row is None:
                    continue  # sin marcarlo como visto: se reintenta en el siguiente sondeo
                staged.fill_id_set.add(fid)
                staged.fill_ids.append(fid)
                staged.fills.append(row)
            if baseline or len(page) < FILLS_PAGE or page_has_seen or not times:
                return
            nxt = _iso_ms(min(times) + timedelta(milliseconds=1))
            if nxt == param:
                self.alerts.append(
                    f"fills: {FILLS_PAGE} fills o más en el mismo milisegundo ({nxt}): puede "
                    "faltar alguno en kraken_fills.csv; concilia con la web de Kraken")
                return
            param = nxt
        self.alerts.append(f"fills: más de {FILLS_MAX_PAGES} páginas pendientes; el resto se "
                           "importará en el siguiente sondeo")

    def _fill_row(self, f: Mapping[str, Any], fid: str) -> dict[str, Any] | None:
        cli = str(f.get("cliOrdId") or "")
        origin = ("liquidación" if "iquidation" in str(f.get("fillType"))
                  else "stop_catastrofe" if cli.startswith(STOP_PREFIX)
                  else "bot" if cli else "manual")
        try:
            return {
                "timestamp": _parse_ts(f["fillTime"]),
                "symbol": str(f.get("symbol")).upper(), "side": str(f.get("side")),
                "size": _dec(f.get("size"), "fill"), "price": _dec(f.get("price"), "fill"),
                "fill_type": str(f.get("fillType")), "cli_ord_id": cli, "fill_id": fid,
                "order_id": str(f.get("order_id") or ""), "origin": origin,
            }
        except (KeyError, TypeError, AttributeError, ValueError, ArithmeticError,
                ExchangeError) as exc:
            self.alerts.append(
                f"fills: fill ilegible ({type(exc).__name__}) [fill_id={fid!r}]: no se "
                "importa al libro fiscal; revísalo a mano")
            return None

    # --- stops de catástrofe ---

    async def sync_catastrophe_stops(
        self, managed: Mapping[str, Decimal], markets: Mapping[str, MarketSpec], pct: Decimal
    ) -> list[str]:
        """Un stop reduceOnly (a mercado al saltar, señal mark) por posición gestionada,
        a `pct` % del precio de entrada y siempre antes de la liquidación estimada."""
        with malformed("stops de catástrofe"):
            return await self._sync_catastrophe_stops(managed, markets, pct)

    async def _sync_catastrophe_stops(
        self, managed: Mapping[str, Decimal], markets: Mapping[str, MarketSpec], pct: Decimal
    ) -> list[str]:
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
            # Primero se coloca el nuevo y solo entonces se retira el antiguo: la posición
            # no queda ni un instante sin protección. Si el exchange no admite dos stops
            # reduceOnly a la vez, se retira el antiguo y se reintenta; si el alta lanza
            # una excepción, el antiguo sigue en su sitio.
            status = await self._place_stop(symbol, side, size, stop)
            if status == "placed":
                for o in current:
                    await self._cancel(o)
                continue
            for o in current:
                await self._cancel(o)
            status = await self._place_stop(symbol, side, size, stop)
            if status != "placed":
                warnings.append(f"{symbol}: no se pudo colocar el stop de catástrofe ({status})")

        for o in ours:  # stops de posiciones que ya no existen
            if str(o.get("symbol")).upper() not in managed:
                await self._cancel(o)
        if total_loss > buffer * LIQUIDATION_SAFETY:
            warnings.append("si saltaran todos los stops a la vez, la pérdida se acercaría "
                            "a la liquidación: reduce el apalancamiento")
        return warnings

    async def _place_stop(self, symbol: str, side: Side, size: Decimal, stop: Decimal) -> Any:
        result = await self._c.request("POST", f"{API}/sendorder", [
            ("orderType", "stp"), ("symbol", symbol), ("side", side.value),
            ("size", plain(abs(size))), ("stopPrice", plain(stop)), ("triggerSignal", "mark"),
            ("reduceOnly", "true"), ("cliOrdId", STOP_PREFIX + uuid.uuid4().hex),
        ])
        return (result.get("sendStatus") or {}).get("status")

    async def _cancel(self, order: Mapping[str, Any]) -> None:
        await self._c.request("POST", f"{API}/cancelorder",
                              [("cliOrdId", str(order.get("cliOrdId")))])
