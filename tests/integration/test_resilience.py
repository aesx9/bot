"""A1: ninguna excepción inesperada puede matar el bucle principal en silencio."""

from __future__ import annotations

import asyncio
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from copybot.engine import LoopTaskDied, Outcome
from copybot.state import StateStore
from tests.fakes import FakeLeader
from tests.integration.test_engine import World


class IdleStream:
    def __init__(self, on_fills: Any, on_connected: Any) -> None: ...

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def stop(self) -> None: ...


def fast(world: World) -> None:
    """Intervalos de milisegundos (se salta la validación del rango a propósito)."""
    timing = world.cfg.timing.model_copy(update={
        "reconcile_interval_seconds": D("0.02"), "debounce_seconds": D("0.01"),
        "heartbeat_seconds": D("0.05")})
    world.cfg = world.cfg.model_copy(update={"timing": timing})
    world.engine.cfg = world.cfg


@pytest.mark.parametrize("exc", [KeyError("campo"), ValueError("fecha"), AttributeError("x")])
async def test_unexpected_exception_is_a_cycle_error_and_five_halt(
    tmp_path: Path, exc: Exception
) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1"))
    w.leader.fail = exc
    outcomes = [await w.cycle() for _ in range(5)]
    assert outcomes == [Outcome.ERROR] * 4 + [Outcome.HALTED]
    assert w.state.halted and w.state.consecutive_errors == 5


async def test_failure_while_handling_the_error_does_not_escape_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guardar el estado dentro del tratamiento del error también puede fallar (disco lleno)."""
    w = World(tmp_path, FakeLeader("100000", BTC="1"))
    w.leader.fail = KeyError("x")

    def boom(self: StateStore, state: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(StateStore, "save", boom)
    outcomes = [await w.cycle() for _ in range(5)]
    assert outcomes[:4] == [Outcome.ERROR] * 4 and outcomes[4] is Outcome.HALTED


async def test_run_forever_keeps_cycling_and_halts_after_unexpected_exceptions(
    tmp_path: Path,
) -> None:
    """PoC B: con KeyError el bucle REST moría tras 1 consulta, sin errores ni parada."""
    w = World(tmp_path, FakeLeader("100000", BTC="1"))
    fast(w)
    w.leader.fail = KeyError("campo inesperado")
    report = await asyncio.wait_for(w.engine.run_forever(lambda **cb: IdleStream(**cb)), 5)
    assert report is not None and report.outcome is Outcome.HALTED
    assert w.leader.calls >= 5 and w.state.halted


async def test_a_dead_loop_task_makes_run_forever_fail_loudly(tmp_path: Path) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1"))
    fast(w)

    class DyingStream(IdleStream):
        async def run(self) -> None:
            raise RuntimeError("tarea rota")

    with pytest.raises(LoopTaskDied, match="websocket"):
        await asyncio.wait_for(w.engine.run_forever(lambda **cb: DyingStream(**cb)), 5)


async def test_a_task_that_returns_early_also_fails_loudly(tmp_path: Path) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1"))
    fast(w)

    class QuietStream(IdleStream):
        async def run(self) -> None:
            return None

    with pytest.raises(LoopTaskDied):
        await asyncio.wait_for(w.engine.run_forever(lambda **cb: QuietStream(**cb)), 5)
