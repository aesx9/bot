"""Ciclos completos: líder y mercado simulados, cuenta paper real, estado en disco."""

from __future__ import annotations

import asyncio
import csv
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from copybot import limits
from copybot.alerts import Level, LogAlerter
from copybot.config import Config
from copybot.engine import Engine, Outcome
from copybot.exchange.kraken_public import FundingRate, KrakenDataError, OrderBook
from copybot.exchange.paper import PaperAccount, PaperExchange, PaperPosition
from copybot.records import CsvRecorder
from copybot.risk import reset_halt
from copybot.sources.hyperliquid_rest import LeaderDataError
from copybot.sources.hyperliquid_ws import LeaderFill
from copybot.state import BotState, StateStore
from tests.conftest import LEADER
from tests.fakes import NOW, FakeLeader, FakeMarket

BTC, ETH, SOL, DOGE = "PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD", "PF_DOGEUSD"


async def _no_sleep(_: float) -> None:
    return None


class World:
    """Todo lo necesario para ejecutar ciclos y simular reinicios."""

    def __init__(self, tmp: Path, leader: FakeLeader, **cfg: Any) -> None:
        self.tmp = tmp
        self.clock = {"now": NOW}
        self.leader = leader
        leader.clock = lambda: self.clock["now"]
        self.market = FakeMarket()
        self.cfg_data = {"leader_address": LEADER,
                         "filters": {"ignore_preexisting": False}} | cfg
        self.cfg = Config.model_validate(self.cfg_data)
        self.store = StateStore(tmp / "state.json")
        self.alerts = LogAlerter()
        self.account = PaperAccount.new(self.cfg.paper)
        self.boot(BotState())

    def boot(self, state: BotState, startup_profile: bool = False) -> None:
        self.state = state
        self.account = (PaperAccount.from_dict(state.paper) if state.paper
                        else self.account)
        self.exchange = PaperExchange(self.account, self.market, self.cfg.paper,
                                      now=lambda: self.clock["now"])
        self.store.before_save = lambda st: setattr(st, "paper", self.account.to_dict())
        self.engine = Engine(
            cfg=self.cfg, state=state, store=self.store, leader=self.leader,
            market=self.market, exchange=self.exchange, recorder=CsvRecorder(self.tmp),
            alerter=self.alerts, kill_dirs=[self.tmp], startup_profile=startup_profile,
            now=lambda: self.clock["now"],
            breaker_clock=lambda: self.clock["now"].timestamp(),
            emergency_pause_seconds=0, sleep=_no_sleep,
        )

    def restart(self, **kw: Any) -> None:
        self.boot(self.store.load(), **kw)

    async def cycle(self, trigger: str = "rest") -> Outcome:
        self.clock["now"] += timedelta(seconds=90)  # salir de la ventana por minuto
        return (await self.engine.cycle(trigger)).outcome

    async def positions(self) -> dict[str, D]:
        return await self.exchange.positions()

    def rows(self, name: str) -> list[dict[str, str]]:
        p = self.tmp / name
        return list(csv.DictReader(p.open())) if p.exists() else []


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path, FakeLeader("100000", BTC="1", ETH="10"))


async def test_copies_leader_and_is_idempotent(world: World) -> None:
    assert await world.cycle() is Outcome.OK
    pos = await world.positions()
    assert set(pos) == {BTC, ETH} and all(v > 0 for v in pos.values())
    n = len(world.rows("trades.csv"))
    assert n == 2
    # Nada cambia: el siguiente ciclo no envía órdenes
    assert await world.cycle() is Outcome.OK
    assert len(world.rows("trades.csv")) == n
    assert world.state.managed_symbols == {BTC, ETH}


async def test_caps_are_respected_in_a_real_cycle(world: World) -> None:
    await world.cycle()
    equity = await world.exchange.equity_usd()
    tickers = await world.market.tickers()
    total = D(0)
    for sym, size in (await world.positions()).items():
        notional = abs(size) * tickers[sym].mark_price
        assert notional <= equity * world.cfg.sizing.max_asset_pct_equity / 100 * D("1.01")
        total += notional
    assert total <= limits.HARD_MAX_LEVERAGE * equity


async def test_leader_close_is_copied_with_reduce_only(world: World) -> None:
    await world.cycle()
    del world.leader.positions["ETH"]
    assert await world.cycle() is Outcome.OK
    assert ETH not in await world.positions()
    last = world.rows("trades.csv")[-1]
    assert (last["mercado"], last["accion"], last["reduce_only"]) == (ETH, "close", "True")
    assert world.state.managed_symbols == {BTC}


async def test_leader_flip_is_close_then_open(world: World) -> None:
    await world.cycle()
    world.leader.positions["ETH"] = D(-10)
    await world.cycle()
    assert (await world.positions())[ETH] < 0
    actions = [(r["mercado"], r["accion"]) for r in world.rows("trades.csv")[-2:]]
    assert actions == [(ETH, "flip_close"), (ETH, "flip_open")]


async def test_manual_positions_in_other_markets_are_never_touched(world: World) -> None:
    world.account.positions[DOGE] = PaperPosition(D(1000), D("0.08"))
    await world.cycle()
    await world.cycle()
    assert (await world.positions())[DOGE] == D(1000)
    assert DOGE not in world.state.managed_symbols


async def test_preexisting_leader_positions_are_ignored_until_closed(tmp_path: Path) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1"), filters={"ignore_preexisting": True})
    await w.cycle()
    assert await w.positions() == {}
    assert w.state.preexisting == {"BTC": D(1)}
    w.leader.positions["ETH"] = D(10)  # posición nueva: sí se copia
    await w.cycle()
    assert set(await w.positions()) == {ETH}
    del w.leader.positions["BTC"]  # la cierra...
    await w.cycle()
    w.leader.positions["BTC"] = D(1)  # ...y la reabre: ya es nueva
    await w.cycle()
    assert set(await w.positions()) == {ETH, BTC}


async def test_kill_switch_halts_and_survives_restart(world: World) -> None:
    await world.cycle()
    (world.tmp / "STOP").touch()
    assert await world.cycle() is Outcome.HALTED
    assert any(level is Level.CRITICAL for level, _ in world.alerts.sent)
    calls = world.leader.calls
    world.restart()
    (world.tmp / "STOP").unlink()
    assert await world.cycle() is Outcome.HALTED  # la parada persiste sin el fichero
    assert world.leader.calls == calls  # ni siquiera lee al líder
    reset_halt(world.state)
    assert await world.cycle() is Outcome.OK


async def test_drawdown_closes_everything_and_persists(world: World) -> None:
    await world.cycle()
    peak = world.state.peak_equity_usd
    assert peak is not None
    world.restart()  # el pico sobrevive al reinicio
    assert world.state.peak_equity_usd == peak
    # Hundimiento: el BTC cae un 60 %
    mark = world.market.ticker_map[BTC].mark_price
    world.market.set_mark(BTC, mark * D("0.4"))
    world.market.set_mark(ETH, world.market.ticker_map[ETH].mark_price * D("0.4"))
    assert await world.cycle() is Outcome.HALTED
    assert await world.positions() == {}
    assert "drawdown" in world.state.halt_reason
    assert all(r["reduce_only"] == "True" for r in world.rows("trades.csv")[2:])
    world.restart()
    assert world.state.halted


async def test_drawdown_without_close_all_only_stops(tmp_path: Path) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1"), risk={"close_all_on_drawdown": False})
    await w.cycle()
    w.market.set_mark(BTC, w.market.ticker_map[BTC].mark_price * D("0.01"))
    assert await w.cycle() is Outcome.HALTED
    assert BTC in await w.positions()


async def test_sanity_skips_then_halts_after_three(world: World) -> None:
    await world.cycle()
    world.leader.equity = D(400000)  # ¿depósito? +300 %
    outcomes = [await world.cycle() for _ in range(3)]
    assert outcomes == [Outcome.SKIPPED, Outcome.SKIPPED, Outcome.HALTED]
    assert "líder" in world.state.halt_reason
    # Tras revisar y --reset-halt, el nuevo capital pasa a ser la referencia
    reset_halt(world.state)
    assert await world.cycle() is Outcome.OK


async def test_stale_leader_data_is_not_traded(world: World) -> None:
    world.leader.timestamp = NOW - timedelta(minutes=10)
    assert await world.cycle() is Outcome.SKIPPED
    assert await world.positions() == {}


async def test_consecutive_errors_halt(world: World) -> None:
    world.leader.fail = LeaderDataError("API caída")
    outcomes = [await world.cycle() for _ in range(5)]
    assert outcomes == [Outcome.ERROR] * 4 + [Outcome.HALTED]
    world.leader.fail = None
    world.restart()
    assert await world.cycle() is Outcome.HALTED


async def test_one_success_resets_error_count(world: World) -> None:
    world.market.fail = KrakenDataError("timeout")
    for _ in range(4):
        assert await world.cycle() is Outcome.ERROR
    world.market.fail = None
    assert await world.cycle() is Outcome.OK
    assert world.state.consecutive_errors == 0


async def test_initial_sync_is_paced_not_halted(tmp_path: Path) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1", ETH="10", SOL="200", DOGE="100000"),
              risk={"max_orders_per_minute": 3})
    report = await w.engine.cycle()
    w.clock["now"] += timedelta(seconds=90)
    assert report.outcome is Outcome.OK and "1 aplazadas" in report.detail
    assert len(await w.positions()) == 3
    # Prioridad: las aperturas de mayor nocional entraron primero
    assert await w.cycle() is Outcome.OK
    assert len(await w.positions()) == 4 and w.state.paced_streak == 0


async def test_pacing_outside_initial_sync_halts_after_n_cycles(tmp_path: Path) -> None:
    # Ratio fijo pequeño: ningún tope recorta, así cada cambio del líder genera órdenes
    w = World(tmp_path, FakeLeader("100000", BTC="1"),
              risk={"max_orders_per_minute": 2},
              sizing={"mode": "fixed", "fixed_ratio": "0.0002"})
    assert await w.cycle() is Outcome.OK  # sincronización inicial completa, sin aplazar
    outcomes = []
    for k in range(2, 6):  # el líder aumenta tres activos por ciclo: solo caben dos órdenes
        w.leader.positions = {"BTC": D(k), "ETH": D(30 * k), "SOL": D(600 * k)}
        outcomes.append(await w.cycle())
    assert outcomes == [Outcome.OK, Outcome.OK, Outcome.OK, Outcome.HALTED]
    assert "seguidos" in w.state.halt_reason
    assert sum(1 for lvl, _ in w.alerts.sent if lvl is Level.WARNING) >= 3


async def test_kill_switch_closes_managed_and_spares_manual(world: World) -> None:
    world.account.positions[DOGE] = PaperPosition(D(1000), D("0.08"))
    await world.cycle()
    (world.tmp / "STOP").touch()
    assert await world.cycle() is Outcome.HALTED
    assert await world.positions() == {DOGE: D(1000)}
    assert world.state.kill_switch_closed and "cerradas" in world.state.halt_reason
    assert all(r["reduce_only"] == "True" for r in world.rows("trades.csv")[2:])


async def test_kill_switch_close_can_be_disabled(tmp_path: Path) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1"), risk={"close_all_on_kill_switch": False})
    await w.cycle()
    (w.tmp / "STOP").touch()
    assert await w.cycle() is Outcome.HALTED
    assert BTC in await w.positions()


async def test_kill_switch_on_already_halted_bot_still_closes(world: World) -> None:
    await world.cycle()
    world.leader.fail = LeaderDataError("caída")
    for _ in range(5):
        await world.cycle()
    assert world.state.halted and await world.positions()
    (world.tmp / "STOP").touch()
    await world.cycle()
    assert await world.positions() == {}


async def test_emergency_close_retries_until_flat_ignoring_order_limit(tmp_path: Path) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1"), risk={"max_orders_per_minute": 1})
    await w.cycle()
    size = (await w.positions())[BTC]
    mark = w.market.ticker_map[BTC].mark_price
    # Libro casi vacío: cada ronda solo cierra 0.0005 BTC
    w.market.books[BTC] = OrderBook(BTC, bids=((mark, D("0.0005")),),
                                    asks=((mark + 1, D(1)),))
    (w.tmp / "STOP").touch()
    assert await w.cycle() is Outcome.HALTED
    assert await w.positions() == {}
    closes = [r for r in w.rows("trades.csv") if r["accion"] == "close"]
    assert len(closes) >= 2 and sum(D(r["tamano"]) for r in closes) == size


async def test_emergency_close_failure_is_reported(tmp_path: Path) -> None:
    w = World(tmp_path, FakeLeader("100000", BTC="1"))
    await w.cycle()
    w.market.books[BTC] = OrderBook(BTC, bids=(), asks=())
    (w.tmp / "STOP").touch()
    assert await w.cycle() is Outcome.HALTED
    assert "CIÉRRALAS A MANO" in w.state.halt_reason
    assert w.state.managed_symbols == {BTC}


async def test_drawdown_close_is_not_limited_by_order_rate(world: World) -> None:
    await world.cycle()
    now = world.clock["now"].timestamp() + 90
    world.state.breaker_log += [(now - 1, D(1))] * 20  # límite por minuto agotado
    for sym in (BTC, ETH):
        world.market.set_mark(sym, world.market.ticker_map[sym].mark_price * D("0.4"))
    assert await world.cycle() is Outcome.HALTED
    assert await world.positions() == {}


async def test_startup_profile_caps_live_exposure(world: World) -> None:
    world.restart(startup_profile=True)
    await world.cycle()
    tickers = await world.market.tickers()
    equity = await world.exchange.equity_usd()
    total = D(0)
    for sym, size in (await world.positions()).items():
        n = abs(size) * tickers[sym].mark_price
        assert n <= limits.STARTUP_PROFILE_MAX_ASSET_USD
        total += n
    assert total <= equity * limits.STARTUP_PROFILE_MAX_LEVERAGE


async def test_funding_and_equity_are_recorded(world: World) -> None:
    await world.cycle()
    world.market.funding[BTC] = [FundingRate(world.clock["now"] + timedelta(minutes=30),
                                             D("1.5"))]
    world.clock["now"] += timedelta(hours=1)
    await world.cycle()
    [f] = world.rows("funding.csv")
    assert f["mercado"] == BTC and D(f["pagado_usd"]) > 0 and f["cobrado_usd"] == "0"
    eq = world.rows("equity.csv")
    assert eq and eq[0]["capital_lider_usd"] == "100000" and eq[0]["modo"] == "paper"


async def test_unknown_order_blocks_trading_until_reconciled(world: World) -> None:
    world.state.pending_orders["zzz"] = {"created_at": world.clock["now"].isoformat(),
                                         "mode": "paper", "symbol": BTC}
    world.clock["now"] -= timedelta(seconds=80)  # el ciclo suma 90 s: 10 s de antigüedad
    assert await world.cycle() is Outcome.ERROR
    assert await world.positions() == {}
    assert await world.cycle() is Outcome.OK  # pasado el margen se descarta y se opera
    assert set(await world.positions()) == {BTC, ETH}


async def test_state_and_paper_account_survive_restart(world: World) -> None:
    await world.cycle()
    before = await world.positions()
    world.restart()
    assert await world.positions() == before
    assert await world.cycle() is Outcome.OK
    assert len(world.rows("trades.csv")) == 2  # sin órdenes duplicadas tras reiniciar


async def test_run_forever_ws_trigger_reconnect_and_stop(world: World) -> None:
    triggers: list[str] = []
    original = world.engine.cycle

    async def spy(trigger: str = "rest", leader_time: Any = None) -> Any:
        triggers.append(trigger)
        return await original(trigger, leader_time)

    world.engine.cycle = spy  # type: ignore[method-assign]

    class FakeStream:
        def __init__(self, on_fills: Any, on_connected: Any) -> None:
            self.on_fills, self.on_connected = on_fills, on_connected

        async def run(self) -> None:
            await self.on_connected(False)
            fill = LeaderFill("BTC", D(82500), D(1), "B", NOW, 1, D(0))
            for _ in range(3):  # ráfaga: un solo ciclo tras el debounce
                await self.on_fills([fill])
            await asyncio.sleep(0.1)
            await self.on_connected(True)  # reconexión: ciclo forzado
            (world.tmp / "STOP").touch()
            await self.on_connected(True)
            await asyncio.Event().wait()

        async def stop(self) -> None:
            pass

    def factory(**cb: Any) -> FakeStream:
        return FakeStream(**cb)

    world.cfg = world.cfg.model_copy(update={"timing": world.cfg.timing.model_copy(
        update={"debounce_seconds": D("0.02")})})
    world.engine.cfg = world.cfg
    report = await asyncio.wait_for(world.engine.run_forever(factory), timeout=5)
    assert report is not None and report.outcome is Outcome.HALTED
    assert triggers.count("websocket") == 1
    assert triggers.count("reconexión") == 2
    assert "rest" in triggers
