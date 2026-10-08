"""WebSocket userFills contra un servidor simulado: desconexiones, reconexión
con backoff, mensajes malformados, ping e inactividad. Sin red."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
import websockets

from copybot.sources.debounce import Debouncer
from copybot.sources.hyperliquid_ws import LeaderFill, UserFillsStream, parse_fills

FIX = json.loads((Path(__file__).parent.parent / "fixtures" / "hl_ws_userfills.json").read_text())
ACK, SNAPSHOT, PONG, STREAM = (json.dumps(m) for m in FIX)  # mensajes reales, en orden
USER = FIX[0]["data"]["subscription"]["user"]
SUBSCRIBE = {"method": "subscribe", "subscription": {"type": "userFills", "user": USER}}


def stream_msg(**data_changes: Any) -> str:
    msg = copy.deepcopy(FIX[3])
    msg["data"].update(data_changes)
    return json.dumps(msg)


class FakeConn:
    """Entrega los mensajes en orden; al acabarlos, cierra (o se queda colgada)."""

    def __init__(self, incoming: Sequence[str | BaseException], *, hang: bool = False) -> None:
        self.incoming = list(incoming)
        self.hang = hang
        self.sent: list[Any] = []
        self.closed = False
        self._closed = asyncio.Event()

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        if self.incoming:
            item = self.incoming.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        if self.hang:  # como un socket real: recv() despierta al cerrarse
            await self._closed.wait()
            raise websockets.ConnectionClosedOK(None, None)
        raise websockets.ConnectionClosedError(None, None)

    async def close(self) -> None:
        self.closed = True
        self._closed.set()


class Harness:
    def __init__(self, *conns: FakeConn | BaseException, **kw: Any) -> None:
        self.conns = list(conns)
        self.fills: list[Sequence[LeaderFill]] = []
        self.connected: list[bool] = []
        self.sleeps: list[float] = []
        params = {"ping_interval_seconds": 5, "idle_timeout_seconds": 60,
                  "ack_timeout_seconds": 5} | kw
        self.stream = UserFillsStream(
            USER, on_fills=self._on_fills, on_connected=self._on_connected,
            connect=self._connect, sleep=self._sleep, jitter=lambda: 1.0, **params,
        )

    async def _on_fills(self, fills: Sequence[LeaderFill]) -> None:
        self.fills.append(fills)

    async def _on_connected(self, reconnect: bool) -> None:
        self.connected.append(reconnect)

    async def _connect(self, url: str) -> FakeConn:
        assert url == "wss://api.hyperliquid.xyz/ws"
        if not self.conns:  # guion agotado: fin de la prueba
            await self.stream.stop()
            raise OSError("fin del guion")
        item = self.conns.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def _sleep(self, s: float) -> None:
        self.sleeps.append(s)
        await asyncio.sleep(0)

    async def run(self) -> None:
        await asyncio.wait_for(self.stream.run(), timeout=5)


async def test_real_message_sequence() -> None:
    conn = FakeConn([ACK, SNAPSHOT, PONG, STREAM])
    h = Harness(conn)
    await h.run()
    assert conn.sent[0] == SUBSCRIBE
    assert h.connected == [False]
    assert len(h.fills) == 1  # el snapshot se ignora; el streaming sin isSnapshot no
    fill = h.fills[0][0]
    raw = FIX[3]["data"]["fills"][0]
    assert (fill.coin, fill.price, fill.size, fill.side, fill.tid) == (
        raw["coin"], D(raw["px"]), D(raw["sz"]), raw["side"], raw["tid"])
    assert fill.time == datetime.fromtimestamp(raw["time"] / 1000, tz=UTC)
    assert conn.closed


async def test_explicit_is_snapshot_false_is_forwarded() -> None:
    h = Harness(FakeConn([ACK, stream_msg(isSnapshot=False)]))
    await h.run()
    assert len(h.fills) == 1 and len(h.fills[0]) == 1


async def test_reconnects_with_exponential_backoff_and_forces_cycle() -> None:
    h = Harness(
        FakeConn([ACK, SNAPSHOT]),  # se cae tras suscribirse
        OSError("connection refused"),
        TimeoutError(),
        FakeConn([ACK, SNAPSHOT, STREAM]),
        backoff_initial_seconds=1, backoff_max_seconds=60,
    )
    await h.run()
    # 1 s tras una sesión sana; 2 y 4 s tras fallos seguidos; vuelve a 1 s al reconectar
    assert h.sleeps == [1, 2, 4, 1]
    assert h.connected == [False, True]  # True = reconexión: el motor fuerza un ciclo
    assert h.stream.reconnects == 1
    assert len(h.fills) == 1  # el snapshot de la reconexión también se ignora


def test_backoff_is_capped_and_jittered() -> None:
    s = UserFillsStream(USER, on_fills=None, on_connected=None,  # type: ignore[arg-type]
                        backoff_initial_seconds=1, backoff_max_seconds=60, jitter=lambda: 1.0)
    assert [s.backoff_delay(a) for a in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert s.backoff_delay(10_000) == 60  # sin desbordamiento
    s2 = UserFillsStream(USER, on_fills=None, on_connected=None,  # type: ignore[arg-type]
                         backoff_initial_seconds=1, backoff_max_seconds=60, jitter=lambda: 0.0)
    assert s2.backoff_delay(3) == 4  # jitter mínimo: la mitad


async def test_server_error_channel_forces_reconnect() -> None:
    err = json.dumps({"channel": "error", "data": "Invalid subscription"})
    h = Harness(FakeConn([ACK, err, STREAM]), FakeConn([ACK]))
    await h.run()
    assert h.connected == [False, True]
    assert h.fills == []  # lo que venía tras el error en esa conexión no se procesa


async def test_missing_ack_forces_reconnect() -> None:
    conn = FakeConn([SNAPSHOT], hang=True)
    h = Harness(conn, ping_interval_seconds=0.01, ack_timeout_seconds=0.05)
    await h.run()
    assert h.connected == []
    assert h.sleeps  # se reintentó
    assert conn.closed


async def test_ack_for_another_user_is_not_an_ack() -> None:
    other = json.loads(ACK)
    other["data"]["subscription"]["user"] = "0x" + "11" * 20
    h = Harness(FakeConn([json.dumps(other)], hang=True),
                ping_interval_seconds=0.01, ack_timeout_seconds=0.05)
    await h.run()
    assert h.connected == []


@pytest.mark.parametrize(
    "bad",
    [
        "no es json",
        json.dumps({"sin": "canal"}),
        json.dumps({"channel": "userFills", "data": None}),
        stream_msg(fills="x"),
        stream_msg(fills=[{"coin": "BTC"}]),
        stream_msg(fills=[dict(FIX[3]["data"]["fills"][0], px="NaN")]),
        stream_msg(fills=[dict(FIX[3]["data"]["fills"][0], side="X")]),
        stream_msg(fills=[dict(FIX[3]["data"]["fills"][0], sz="-1")]),
    ],
)
async def test_malformed_messages_trigger_a_safe_reconcile(bad: str) -> None:
    h = Harness(FakeConn([ACK, bad, STREAM]))
    await h.run()
    assert h.fills[0] == ()  # malformado: ciclo de reconciliación sin fills
    assert len(h.fills[1]) == 1  # y el flujo sigue funcionando


async def test_fills_of_another_user_are_ignored() -> None:
    h = Harness(FakeConn([ACK, stream_msg(user="0x" + "22" * 20)]))
    await h.run()
    assert h.fills == []


async def test_ping_when_idle_and_reconnect_when_silent() -> None:
    conn = FakeConn([ACK], hang=True)
    h = Harness(conn, ping_interval_seconds=0.01, idle_timeout_seconds=0.05)
    await h.run()
    assert {"method": "ping"} in conn.sent
    assert h.connected == [False]
    assert conn.closed and h.sleeps  # inactividad = conexión muerta: reconectar


async def test_stop_ends_a_live_session() -> None:
    conn = FakeConn([ACK], hang=True)
    h = Harness(conn)
    task = asyncio.create_task(h.stream.run())
    while not h.connected:
        await asyncio.sleep(0.001)
    await h.stream.stop()
    await asyncio.wait_for(task, timeout=1)
    assert conn.closed


def test_parse_fills_rejects_non_list() -> None:
    with pytest.raises(ValueError):
        parse_fills({"coin": "BTC"})


# --- Debounce ---


async def test_debounce_collapses_bursts() -> None:
    runs: list[float] = []

    async def action() -> None:
        runs.append(asyncio.get_running_loop().time())

    d = Debouncer(0.03, action)
    for _ in range(5):
        d.trigger()
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.08)
    assert len(runs) == 1


async def test_debounce_max_wait_bounds_the_delay() -> None:
    runs: list[float] = []

    async def action() -> None:
        runs.append(asyncio.get_running_loop().time())

    d = Debouncer(0.03, action, max_wait_seconds=0.06)
    start = asyncio.get_running_loop().time()
    for _ in range(20):  # goteo continuo cada 10 ms durante 200 ms
        d.trigger()
        await asyncio.sleep(0.01)
    await d.aclose()
    assert runs and runs[0] - start < 0.12  # no se aplaza hasta el final del goteo


async def test_debounce_runs_never_overlap_and_late_triggers_are_kept() -> None:
    active = 0
    peak = 0

    async def action() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        active -= 1

    d = Debouncer(0.005, action)
    d.trigger()
    await asyncio.sleep(0.015)  # el primer ciclo está en marcha
    d.trigger()  # llega un fill durante el ciclo: debe haber otro ciclo después
    await asyncio.sleep(0.1)
    assert d.runs == 2 and peak == 1


async def test_debounce_close_cancels_pending_cycle() -> None:
    called = False

    async def action() -> None:
        nonlocal called
        called = True

    d = Debouncer(0.05, action)
    d.trigger()
    await d.aclose()
    await asyncio.sleep(0.07)
    assert not called and not d.pending


async def test_unexpected_callback_failure_reconnects_instead_of_killing_the_stream() -> None:
    """A1 (PoC B): un fallo inesperado en un callback no puede matar la tarea del stream."""
    h = Harness(FakeConn([ACK, SNAPSHOT]), FakeConn([ACK, SNAPSHOT, STREAM]))
    calls = {"n": 0}
    original = h._on_connected

    async def flaky(reconnect: bool) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("fallo inesperado en el callback")
        await original(reconnect)

    h.stream._on_connected = flaky  # type: ignore[assignment]
    await h.run()
    assert h.connected == [True]  # la segunda sesión se estableció: el stream siguió vivo
    assert len(h.fills) == 1
