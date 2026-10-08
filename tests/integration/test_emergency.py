"""A5: el cierre de emergencia (kill switch y drawdown) no se bloquea ni se rinde a la primera."""

from __future__ import annotations

import asyncio
from decimal import Decimal as D
from pathlib import Path

import pytest

from copybot.alerts import Level
from copybot.engine import Outcome
from copybot.exchange.kraken_public import OrderBook
from copybot.state import StateStore
from tests.fakes import FakeLeader
from tests.integration.test_engine import BTC, ETH, World
from tests.integration.test_resilience import IdleStream, fast


def empty_book(w: World, symbol: str = BTC) -> None:
    w.market.books[symbol] = OrderBook(symbol, bids=(), asks=())


async def test_one_unavailable_market_does_not_block_closing_the_others(tmp_path: Path) -> None:
    """PoC I: ETH desaparece de instruments (y el bot no lo conoce): el BTC se cierra igual."""
    w = World(tmp_path, FakeLeader("100000", BTC="1", ETH="10"))
    await w.cycle()
    assert set(await w.positions()) == {BTC, ETH}
    w.market.specs.pop(ETH)
    w.engine._known_markets.clear()  # arranque en frío: sin memoria del mercado
    (w.tmp / "STOP").touch()
    await w.cycle()
    assert set(await w.positions()) == {ETH}  # el BTC sí se cerró
    assert w.state.emergency_close_pending and not w.state.kill_switch_closed
    assert w.state.managed_symbols == {ETH}
    assert any(lvl is Level.CRITICAL and ETH in text for lvl, text in w.alerts.sent)


async def test_vanished_market_is_closed_with_its_last_known_spec(tmp_path: Path) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1", ETH="10"))
    await w.cycle()
    w.market.specs.pop(ETH)  # Kraken lo deja de listar, pero el bot lo conocía
    (w.tmp / "STOP").touch()
    await w.cycle()
    assert await w.positions() == {}
    assert w.state.kill_switch_closed and not w.state.emergency_close_pending


async def test_kill_switch_is_retried_while_stop_exists_and_positions_remain(
    tmp_path: Path,
) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1"))
    await w.cycle()
    empty_book(w)  # sin liquidez: el cierre no puede completarse
    (w.tmp / "STOP").touch()
    await w.cycle()
    assert BTC in await w.positions()
    assert w.state.emergency_close_pending and not w.state.kill_switch_closed
    w.restart()  # el estado pendiente sobrevive a un reinicio
    assert w.state.emergency_close_pending
    w.market.books.pop(BTC)  # vuelve la liquidez
    await w.cycle()
    assert await w.positions() == {}
    assert w.state.kill_switch_closed and not w.state.emergency_close_pending


async def test_halted_bot_retries_an_unfinished_drawdown_close(tmp_path: Path) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1"))
    await w.cycle()
    w.market.set_mark(BTC, w.market.ticker_map[BTC].mark_price * D("0.2"))
    empty_book(w)
    assert (await w.engine.cycle()).outcome.value == "halted"
    assert BTC in await w.positions() and w.state.emergency_close_pending
    w.market.books.pop(BTC)
    await w.cycle()  # sin STOP: reintenta solo porque quedó un cierre pendiente
    assert await w.positions() == {} and not w.state.emergency_close_pending


async def test_process_stays_alive_until_the_emergency_close_completes(tmp_path: Path) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1"))
    await w.cycle()
    fast(w)
    w.engine._emergency_retry_seconds = 0.02
    empty_book(w)
    (w.tmp / "STOP").touch()
    asyncio.get_running_loop().call_later(0.15, w.market.books.pop, BTC)
    report = await asyncio.wait_for(w.engine.run_forever(lambda **cb: IdleStream(**cb)), 5)
    assert report is not None and report.outcome.value == "halted"
    assert await w.positions() == {} and w.state.kill_switch_closed


async def test_emergency_close_is_sent_even_if_the_state_cannot_be_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PoC K: con el disco lleno no sale ninguna orden de cierre."""
    w = World(tmp_path, FakeLeader("100000", BTC="1"))
    await w.cycle()
    (w.tmp / "STOP").touch()

    def boom(self: StateStore, state: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(StateStore, "save", boom)
    report = await w.engine.cycle()  # tampoco debe lanzar al guardar la parada
    assert report.outcome.value == "halted"
    assert await w.positions() == {}


async def test_stop_file_is_honoured_within_seconds_not_at_the_next_cycle(tmp_path: Path) -> None:
    """M9: con reconcile_interval_seconds alto el kill switch tardaba hasta ese intervalo."""
    w = World(tmp_path, FakeLeader("100000", BTC="1"))
    fast(w)
    w.cfg = w.cfg.model_copy(update={"timing": w.cfg.timing.model_copy(
        update={"reconcile_interval_seconds": D(3600), "heartbeat_seconds": D(3600)})})
    w.engine.cfg = w.cfg
    w.engine._kill_poll = 0.05
    loop = asyncio.get_running_loop()
    loop.call_later(0.3, (w.tmp / "STOP").touch)  # tras el primer ciclo REST (ya abrió la posición)
    report = await asyncio.wait_for(w.engine.run_forever(lambda **cb: IdleStream(**cb)), 5)
    assert report is not None and report.outcome.value == "halted"
    assert await w.positions() == {} and w.state.kill_switch_closed


async def test_open_is_skipped_while_a_previous_close_has_not_filled(tmp_path: Path) -> None:
    """M12: el líder cambia de BTC a ETH, el cierre del BTC no se ejecuta (sin liquidez) y la
    apertura de ETH dejaría la exposición real al doble del tope total."""
    w = World(tmp_path, FakeLeader("100000", BTC="1"), sizing={"max_total_leverage": "0.3"})
    await w.cycle()
    assert set(await w.positions()) == {BTC}
    empty_book(w)
    del w.leader.positions["BTC"]
    w.leader.positions["ETH"] = D(10)
    assert await w.cycle() is Outcome.OK
    assert set(await w.positions()) == {BTC}  # ni se cerró el BTC ni se abrió el ETH
    w.market.books.pop(BTC)
    assert await w.cycle() is Outcome.OK
    assert set(await w.positions()) == {ETH}  # con liquidez, cierra y luego abre
