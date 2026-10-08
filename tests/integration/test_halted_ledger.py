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


# --- N5: un stop de catástrofe o una liquidación detienen el bot ---


# kind -> (cliOrdId: "stop" = el del stop cs-, fillType, texto esperado en el motivo)
PROTECTIVE_KINDS: dict[str, tuple[str | None, str, str]] = {
    "stop": ("stop", "taker", "stop_catastrofe"),
    "liquidation": (None, "liquidation", "liquidación"),
    "assignor": (None, "assignor", "liquidación"),
    "unwindBankrupt": (None, "unwindBankrupt", "desapalancamiento"),
    "unwindCounterparty": (None, "unwindCounterparty", "desapalancamiento"),
    # el stop salta pero Kraken no conserva el cliOrdId cs- (o lo cambia): fill ajeno
    "stop-sin-cliOrdId": (None, "taker", "fill ajeno"),
    "stop-con-otro-cliOrdId": ("otro", "taker", "fill ajeno"),
}


def _protective_fill(env: Env, kind: str) -> None:  # noqa: F811
    """Kraken ejecuta el stop (o liquida): la posición desaparece y aparece el fill."""
    cli, fill_type, _ = PROTECTIVE_KINDS[kind]
    [stop] = env.kraken.open_orders
    env.kraken.open_orders = []
    env.kraken.positions = []
    env.kraken.fills.append({
        "cliOrdId": stop["cliOrdId"] if cli == "stop" else cli,
        "fillTime": "2026-10-08T12:00:30.000Z",
        "fillType": fill_type, "fill_id": f"{kind}-fill",
        "order_id": "o", "price": "85", "side": "sell", "size": stop["unfilledSize"],
        "symbol": SOL})


@pytest.mark.parametrize("kind", list(PROTECTIVE_KINDS))
async def test_a_catastrophe_stop_or_liquidation_halts_instead_of_reopening(
        env: Env, tmp_path: Path, kind: str) -> None:  # noqa: F811
    """N5 (PoC P5): tras saltar el stop, el ciclo siguiente reabría la posición del líder; con
    el stop al 5-7,5 % la volatilidad normal lo repetía hasta cortar el drawdown. Refuerzo: lo
    mismo con assignor y unwind*, y con un fill ajeno en un símbolo gestionado (un stop que
    salta sin conservar el cliOrdId cs-), para no depender de que Kraken lo conserve."""
    clock = {"now": NOW}
    engine = live_engine(env, tmp_path, clock)
    assert (await engine.cycle()).outcome is Outcome.OK
    _protective_fill(env, kind)
    clock["now"] = NOW + timedelta(minutes=1)
    sends = len(env.kraken.sends("ioc"))
    report = await engine.cycle()
    assert report.outcome is Outcome.HALTED
    assert len(env.kraken.sends("ioc")) == sends  # no reabre
    origin = PROTECTIVE_KINDS[kind][2]
    assert origin in env.state.halt_reason
    assert any(origin in n for n in env.state.protective_fills_unreviewed)
    alerter = engine._alert
    assert isinstance(alerter, LogAlerter)
    assert any(level.value == "CRÍTICO" and origin in text for level, text in alerter.sent)
    assert f"{kind}-fill" in {r["fill_id"] for r in rows(tmp_path / "kraken_fills.csv")}


async def test_bot_and_manual_fills_do_not_halt(env: Env, tmp_path: Path) -> None:  # noqa: F811
    engine = live_engine(env, tmp_path)
    assert (await engine.cycle()).outcome is Outcome.OK
    env.kraken.fills.append({"cliOrdId": None, "fillTime": "2026-10-08T12:00:30.000Z",
                             "fillType": "taker", "fill_id": "manual", "order_id": "o",
                             "price": "100", "side": "buy", "size": "1", "symbol": "PF_XBTUSD"})
    assert (await engine.cycle()).outcome is Outcome.OK


async def test_a_foreign_fill_while_trading_in_a_managed_symbol_halts_after_the_cycle(
        env: Env, tmp_path: Path) -> None:  # noqa: F811
    """El fill ajeno puede aparecer entre la lectura previa y el final del ciclo: también
    detiene (tras operar), con los símbolos gestionados de antes y de después del ciclo."""
    clock = {"now": NOW}
    engine = live_engine(env, tmp_path, clock)
    leader = engine._leader
    assert isinstance(leader, FakeLeader)
    leader.clock = lambda: clock["now"]
    assert (await engine.cycle()).outcome is Outcome.OK
    real_positions = env.live.positions

    async def positions_then_foreign_fill() -> dict[str, D]:
        out = await real_positions()
        if not any(f["fill_id"] == "ajeno" for f in env.kraken.fills):
            env.kraken.fills.append({
                "cliOrdId": None, "fillTime": "2026-10-08T12:00:40.000Z", "fillType": "taker",
                "fill_id": "ajeno", "order_id": "o", "price": "100", "side": "buy", "size": "1",
                "symbol": SOL})
        return out

    env.live.positions = positions_then_foreign_fill  # type: ignore[method-assign]
    clock["now"] = NOW + timedelta(minutes=1)
    report = await engine.cycle()
    assert report.outcome is Outcome.HALTED and "fill ajeno" in report.detail


# --- R1: --sync-ledger y --reset-halt con fills protectores ---


def _sync_state(tmp_path: Path, halted: bool) -> StateStore:
    st = StateStore(tmp_path / "data" / "live" / "state.json")
    st.save(BotState(mode="live", halted=halted, halt_reason="controles del líder" if halted
                     else "", managed_symbols={SOL},
                     live_funding_cursor_ms=int(NOW.timestamp() * 1000) - 1000))
    return st


def _sync_with_stop_fill(tmp_path: Path, cli: str | None = "cs-abc") -> None:
    from tests.fake_kraken import FakeKraken

    kraken = FakeKraken()
    kraken.fills = [{"cliOrdId": cli, "fillTime": "2026-10-08T12:00:00.000Z",
                     "fillType": "taker", "fill_id": "stop1", "order_id": "o", "price": "85",
                     "side": "sell", "size": "1", "symbol": SOL}]
    with respx.mock(assert_all_called=False) as router:
        kraken.install(router)
        assert main(live_args(tmp_path, "--sync-ledger")) == EXIT_OK
    assert kraken.sends() == [] and kraken.cancels() == []


@pytest.mark.parametrize("halted", [True, False])
def test_sync_ledger_shows_and_keeps_protective_fills_and_reset_requires_review(
        tmp_path: Path, capsys: pytest.CaptureFixture[str], halted: bool) -> None:
    """R1 (revisión de la segunda auditoría): --sync-ledger importaba el fill del stop y solo
    decía "1 fills"; al marcarlo como visto, tras --reset-halt el bot reabría sin aviso.
    Ahora lo muestra, lo guarda en el estado (y detiene el bot si no lo estaba) y
    --reset-halt exige confirmar que se ha revisado."""
    from tests.integration.test_main import answer

    write_env(tmp_path)
    st = _sync_state(tmp_path, halted)
    _sync_with_stop_fill(tmp_path)
    out = capsys.readouterr().out
    assert "ATENCIÓN" in out and "stop_catastrofe en PF_SOLUSD" in out and "stop1" in out
    after = st.load()
    assert after.halted  # detenido aunque no lo estuviera: no reabre al arrancar
    assert len(after.protective_fills_unreviewed) == 1
    assert "stop1" in after.protective_fills_unreviewed[0]

    # REANUDAR solo no basta: hay que confirmar la revisión de los fills
    assert main(live_args(tmp_path, "--reset-halt"), prompt=answer("REANUDAR")) == EXIT_USAGE
    assert "FILLS PROTECTORES SIN REVISAR" in capsys.readouterr().out
    assert st.load().halted and st.load().protective_fills_unreviewed

    phrases = iter(["HE REVISADO LOS FILLS", "REANUDAR"])
    assert main(live_args(tmp_path, "--reset-halt"),
                prompt=lambda _p: next(phrases)) == EXIT_OK
    final = st.load()
    assert not final.halted and final.protective_fills_unreviewed == []


def test_sync_ledger_flags_a_foreign_fill_in_a_managed_symbol(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Un stop disparado sin su cliOrdId cs- llega como fill ajeno: también cuenta."""
    write_env(tmp_path)
    st = _sync_state(tmp_path, halted=True)
    _sync_with_stop_fill(tmp_path, cli=None)
    assert "fill ajeno en PF_SOLUSD" in capsys.readouterr().out
    assert st.load().protective_fills_unreviewed


def test_sync_ledger_without_protective_fills_changes_nothing_else(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_env(tmp_path)
    st = StateStore(tmp_path / "data" / "live" / "state.json")
    st.save(BotState(mode="live", managed_symbols={SOL}, sent_orders=["c-bot"],
                     live_funding_cursor_ms=int(NOW.timestamp() * 1000) - 1000))
    _sync_with_stop_fill(tmp_path, cli="c-bot")
    assert "ATENCIÓN" not in capsys.readouterr().out
    after = st.load()
    assert not after.halted and after.protective_fills_unreviewed == []


async def test_halted_ledger_keeps_protective_fills_for_review(
        env: Env, tmp_path: Path) -> None:  # noqa: F811
    """Con el bot detenido el libro también los guarda para --reset-halt (no solo alerta)."""
    engine = live_engine(env, tmp_path)
    assert (await engine.cycle()).outcome is Outcome.OK
    env.state.halted, env.state.halt_reason = True, "prueba"
    _protective_fill(env, "stop")
    assert (await engine.cycle()).outcome is Outcome.HALTED
    assert any("stop_catastrofe" in n for n in env.state.protective_fills_unreviewed)
