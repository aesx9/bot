"""N2: el libro fiscal live también se actualiza con el bot detenido.

Regresiones de la segunda auditoría (PoC P3): los fills de un cierre de emergencia (STOP,
drawdown) no llegaban a kraken_fills.csv mientras durase la parada; el export creía abierta la
posición y la conciliación decía "cuadra" porque tampoco había foto de posiciones posterior."""

from __future__ import annotations

import csv
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
import respx

from copybot.alerts import LogAlerter
from copybot.analysis import ecb, fiscal
from copybot.config import Config
from copybot.engine import Engine, Outcome
from copybot.main import EXIT_OK, EXIT_USAGE, main
from copybot.records import CsvRecorder
from copybot.state import BotState, StateStore
from tests.conftest import LEADER
from tests.fakes import FakeLeader, FakeMarket
from tests.integration.test_live_exchange import NOW, SOL, Env, env  # noqa: F401
from tests.integration.test_main import live_args, write_env


def rates() -> ecb.RateTable:
    days = tuple(NOW.date() + timedelta(days=i) for i in range(-10, 400))
    return ecb.RateTable(days, tuple(D("1.10") for _ in days), "test")


def rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(path.open())) if path.exists() else []


def live_engine(env: Env, tmp_path: Path, clock: dict[str, Any] | None = None  # noqa: F811
                ) -> Engine:
    clock = clock if clock is not None else {"now": NOW}
    cfg = Config.model_validate({"leader_address": LEADER, "mode": "live",
                                 "filters": {"ignore_preexisting": False}})
    market = FakeMarket()
    market.set_mark(SOL, "100")
    env.kraken.fill_price = D(100)
    leader = FakeLeader("100000", SOL="5000")
    leader.clock = lambda: NOW
    leader.mids.update(SOL=D(100), ETH=D(100))
    return Engine(cfg=cfg, state=env.state, store=StateStore(tmp_path / "s.json"),
                  leader=leader, market=market, exchange=env.live,
                  recorder=CsvRecorder(tmp_path), alerter=LogAlerter(), kill_dirs=[tmp_path],
                  startup_profile=True, now=lambda: clock["now"],
                  breaker_clock=lambda: clock["now"].timestamp(), emergency_pause_seconds=0)


async def test_emergency_close_fills_reach_the_ledger_and_the_export(
        env: Env, tmp_path: Path) -> None:  # noqa: F811
    clock = {"now": NOW}
    engine = live_engine(env, tmp_path, clock)
    assert (await engine.cycle()).outcome is Outcome.OK
    clock["now"] = NOW + timedelta(minutes=1)
    (tmp_path / "STOP").touch()
    assert (await engine.cycle()).outcome is Outcome.HALTED
    assert await env.live.positions() == {}
    ids = {r["fill_id"] for r in rows(tmp_path / "kraken_fills.csv")}
    assert ids == {f["fill_id"] for f in env.kraken.fills} and len(ids) == 2
    last = max(r["timestamp_utc"] for r in rows(tmp_path / "positions.csv"))
    assert [r["mercado"] for r in rows(tmp_path / "positions.csv")
            if r["timestamp_utc"] == last] == [""]  # foto posterior: cuenta sin posiciones
    _, _, _, notes = fiscal.export(tmp_path, 2026, tmp_path / "out", rates())
    assert not any("posición abierta" in n for n in notes)
    assert len(rows(tmp_path / "out" / "fiscal_posiciones_2026.csv")) == 1


async def test_halted_cycles_keep_importing_stop_and_liquidation_fills(
        env: Env, tmp_path: Path) -> None:  # noqa: F811
    """Detenido con un cierre de emergencia pendiente, el proceso sigue vivo: lo que pase en
    la cuenta (un stop que salta, una liquidación) entra en el libro en esos ciclos."""
    engine = live_engine(env, tmp_path)
    assert (await engine.cycle()).outcome is Outcome.OK
    env.state.halted, env.state.halt_reason = True, "prueba"
    env.state.emergency_close_pending = True
    env.kraken.fail_paths.add("/derivatives/api/v3/sendorder")  # el cierre no sale
    env.kraken.fills.append({"cliOrdId": None, "fillTime": "2026-10-08T12:00:30.000Z",
                             "fillType": "liquidation", "fill_id": "liq1", "order_id": "o",
                             "price": "90", "side": "sell", "size": "0.5", "symbol": SOL})
    assert (await engine.cycle()).outcome is Outcome.HALTED
    assert "liq1" in {r["fill_id"] for r in rows(tmp_path / "kraken_fills.csv")}


def _live_state_with_ledger(tmp_path: Path) -> StateStore:
    st = StateStore(tmp_path / "data" / "live" / "state.json")
    st.save(BotState(mode="live", halted=True, halt_reason="drawdown",
                     live_funding_cursor_ms=int(NOW.timestamp() * 1000) - 1000))
    return st


def test_sync_ledger_imports_without_sending_anything(tmp_path: Path) -> None:
    from tests.fake_kraken import FakeKraken

    write_env(tmp_path)
    st = _live_state_with_ledger(tmp_path)
    kraken = FakeKraken()
    kraken.fills = [{"cliOrdId": "abc", "fillTime": "2026-10-08T12:00:00.000Z",
                     "fillType": "taker", "fill_id": "f1", "order_id": "o", "price": "100",
                     "side": "sell", "size": "1", "symbol": SOL}]
    with respx.mock(assert_all_called=False) as router:
        kraken.install(router)
        assert main(live_args(tmp_path, "--sync-ledger")) == EXIT_OK
    assert {r["fill_id"] for r in rows(tmp_path / "data" / "live" / "kraken_fills.csv")} == {"f1"}
    assert kraken.sends() == [] and kraken.cancels() == []  # solo lectura en el exchange
    assert all(method == "GET" for method, _, _ in kraken.calls)
    after = st.load()
    assert after.fills_seen == ["f1"] and after.halted  # la parada sigue
    assert rows(tmp_path / "data" / "live" / "positions.csv")


def test_sync_ledger_refuses_without_a_previous_live_start(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Sin línea base no hay nada que sincronizar; fijarla aquí además desactivaría el control
    de posiciones ajenas del primer arranque live (M10)."""
    from tests.fake_kraken import FakeKraken

    write_env(tmp_path)
    kraken = FakeKraken()
    with respx.mock(assert_all_called=False) as router:
        kraken.install(router)
        assert main(live_args(tmp_path, "--sync-ledger")) == EXIT_USAGE
    assert kraken.calls == []
    assert "nunca ha operado en live" in capsys.readouterr().err
    path = tmp_path / "data" / "live" / "state.json"
    assert not path.exists() or StateStore(path).load().live_funding_cursor_ms is None


def test_sync_ledger_is_only_for_live(tmp_path: Path, capsys: Any) -> None:
    from tests.integration.test_main import write_config

    assert main(["--config", str(write_config(tmp_path)), "--sync-ledger"]) == EXIT_USAGE
