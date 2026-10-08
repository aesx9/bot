"""Disparador por WebSocket: userFills del líder.

Verificado contra la documentación oficial y la API real (2026-10-08):
- URL: wss://api.hyperliquid.xyz/ws
- Suscripción: {"method": "subscribe",
                "subscription": {"type": "userFills", "user": addr}}
- Respuesta: {"channel": "subscriptionResponse", "data": {...}} y después
  {"channel": "userFills", "data": {"user", "fills": [WsFill], "isSnapshot"}}.
  El primer mensaje es un snapshot (isSnapshot: true) y se ignora. En los
  mensajes de streaming la documentación dice isSnapshot: false, pero la API
  real omite el campo: ausente = false.
- El servidor cierra conexiones sin mensajes en 60 s: se envía
  {"method": "ping"} y responde {"channel": "pong"}.
- Las desconexiones pueden ocurrir sin aviso: hay que reconectar.

Los fills solo DISPARAN un ciclo: la verdad sale siempre de clearinghouseState
por REST. Por eso un mensaje de fills malformado también dispara (con lista
vacía): reconciliar de más es inocuo. Tras cada reconexión se avisa para
forzar un ciclo, porque los fills perdidos durante el corte no llegan aquí.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import websockets
from pydantic import BaseModel, ConfigDict, ValidationError

from copybot.config import Dec

log = logging.getLogger(__name__)

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
PING = json.dumps({"method": "ping"})


@dataclass(frozen=True)
class LeaderFill:
    coin: str
    price: Decimal
    size: Decimal
    side: str  # "B" compra, "A" venta
    time: datetime  # UTC
    tid: int  # id único de trade
    start_position: Decimal


class _WsFill(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    coin: str
    px: Dec
    sz: Dec
    side: str
    time: int
    tid: int
    startPosition: Dec


def parse_fills(raw: Any) -> list[LeaderFill]:
    """Lanza ValueError si algún fill no tiene el formato documentado."""
    if not isinstance(raw, list):
        raise ValueError("fills no es una lista")
    fills = []
    for item in raw:
        try:
            f = _WsFill.model_validate(item)
        except ValidationError as exc:
            raise ValueError(f"fill malformado ({exc.error_count()} errores)") from None
        if f.side not in ("A", "B") or f.px <= 0 or f.sz <= 0:
            raise ValueError("fill con lado, precio o tamaño no válidos")
        fills.append(LeaderFill(
            coin=f.coin, price=f.px, size=f.sz, side=f.side,
            time=datetime.fromtimestamp(f.time / 1000, tz=UTC), tid=f.tid,
            start_position=f.startPosition,
        ))
    return fills


class Connection(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


Connect = Callable[[str], Awaitable[Connection]]
OnFills = Callable[[Sequence[LeaderFill]], Awaitable[None]]
OnConnected = Callable[[bool], Awaitable[None]]  # True si es una reconexión


async def default_connect(url: str) -> Connection:
    # max_size holgado: el snapshot inicial puede traer muchos fills
    conn: Connection = await websockets.connect(url, open_timeout=15, max_size=8 * 2**20)
    return conn


class StreamError(Exception):
    """El flujo no es fiable (error del servidor, sin ack, inactivo): reconectar."""


class UserFillsStream:
    def __init__(
        self,
        user: str,
        *,
        on_fills: OnFills,
        on_connected: OnConnected,
        backoff_initial_seconds: float = 1,
        backoff_max_seconds: float = 60,
        ping_interval_seconds: float = 30,
        idle_timeout_seconds: float = 90,
        ack_timeout_seconds: float = 10,
        url: str = HL_WS_URL,
        connect: Connect = default_connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._user = user.lower()
        self._on_fills = on_fills
        self._on_connected = on_connected
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds
        self._ping_interval = ping_interval_seconds
        self._idle_timeout = idle_timeout_seconds
        self._ack_timeout = ack_timeout_seconds
        self._url = url
        self._connect = connect
        self._sleep = sleep
        self._jitter = jitter
        self._stopping = asyncio.Event()
        self._conn: Connection | None = None
        self._ever_connected = False
        self._acked = False
        self.reconnects = 0

    def backoff_delay(self, attempt: int) -> float:
        """Exponencial con tope y jitter (50-100 %) para no reconectar en ráfaga."""
        base: float = min(self._backoff_max, self._backoff_initial * 2.0 ** min(attempt, 30))
        return base * (0.5 + self._jitter() / 2)

    async def stop(self) -> None:
        self._stopping.set()
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()

    async def run(self) -> None:
        attempt = 0
        while not self._stopping.is_set():
            self._acked = False
            try:
                self._conn = await self._connect(self._url)
                await self._session(self._conn)
            except (OSError, TimeoutError, StreamError, websockets.WebSocketException) as exc:
                if self._stopping.is_set():
                    break
                log.warning("WebSocket de Hyperliquid caído: %s", _describe(exc))
            finally:
                if self._conn is not None:
                    with contextlib.suppress(Exception):
                        await self._conn.close()
                    self._conn = None
            if self._stopping.is_set():
                break
            if self._acked:  # la sesión llegó a funcionar: el backoff vuelve a empezar
                attempt = 0
            delay = self.backoff_delay(attempt)
            attempt += 1
            log.info("reconexión al WebSocket en %.1f s (intento %d)", delay, attempt)
            await self._sleep(delay)

    async def _session(self, conn: Connection) -> None:
        """Suscribe y procesa mensajes hasta que la conexión falle o se pare."""
        await conn.send(json.dumps(
            {"method": "subscribe", "subscription": {"type": "userFills", "user": self._user}}
        ))
        loop = asyncio.get_running_loop()
        started = last_rx = loop.time()
        while not self._stopping.is_set():
            if not self._acked and loop.time() - started > self._ack_timeout:
                raise StreamError("sin confirmación de la suscripción")
            if loop.time() - last_rx > self._idle_timeout:
                raise StreamError(f"sin mensajes en {self._idle_timeout:.0f} s")
            try:
                raw = await asyncio.wait_for(conn.recv(), timeout=self._ping_interval)
            except TimeoutError:
                await conn.send(PING)
                continue
            last_rx = loop.time()
            if await self._handle(raw):
                self._acked = True
                reconnect = self._ever_connected
                self._ever_connected = True
                if reconnect:
                    self.reconnects += 1
                log.info("suscrito a userFills del líder%s", " (reconexión)" * reconnect)
                await self._on_connected(reconnect)

    async def _handle(self, raw: str | bytes) -> bool:
        """Procesa un mensaje. Devuelve True si es la confirmación de suscripción."""
        try:
            msg = json.loads(raw)
            channel = msg["channel"]
        except (ValueError, KeyError, TypeError):
            log.warning("mensaje WebSocket no válido: se fuerza un ciclo")
            await self._on_fills(())
            return False

        if channel == "subscriptionResponse":
            sub = (msg.get("data") or {}).get("subscription") or {}
            if sub.get("type") == "userFills" and str(sub.get("user", "")).lower() == self._user:
                return not self._acked
            return False
        if channel == "error":
            raise StreamError(f"error del servidor: {str(msg.get('data'))[:200]}")
        if channel != "userFills":
            return False  # pong y otros canales

        data = msg.get("data")
        if not isinstance(data, dict):
            log.warning("userFills sin datos: se fuerza un ciclo")
            await self._on_fills(())
            return False
        if str(data.get("user", "")).lower() != self._user:
            return False
        if data.get("isSnapshot") is True:
            log.debug("snapshot de userFills ignorado (%d fills)", len(data.get("fills") or []))
            return False
        try:
            fills = parse_fills(data.get("fills"))
        except ValueError as exc:
            log.warning("userFills malformado (%s): se fuerza un ciclo", exc)
            await self._on_fills(())
            return False
        await self._on_fills(fills)
        return False


def _describe(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text[:200]}" if text else type(exc).__name__
