"""Exchange live contra la API privada simulada. Nunca toca la red real."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from copybot.config import ExecutionConfig, RiskConfig
from copybot.exchange.base import ExchangeError, OrderRequest, OrderStatus
from copybot.exchange.kraken_auth import KrakenPrivateClient
from copybot.exchange.live import STOP_PREFIX, LiveExchange
from copybot.executor import ExecutionContext, Executor
from copybot.models import Action, ActionKind, Side
from copybot.records import CsvRecorder
from copybot.risk import CircuitBreaker
from copybot.state import BotState, StateStore
from tests.fake_kraken import CREDS, FakeKraken
from tests.fakes import FakeMarket

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
SOL, BTC = "PF_SOLUSD", "PF_XBTUSD"


class Env:
    def __init__(self, kraken: FakeKraken, live: LiveExchange, state: BotState) -> None:
        self.kraken, self.live, self.state = kraken, live, state


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    kraken = FakeKraken()
    state = BotState()
    with respx.mock(assert_all_called=False) as router:
        kraken.install(router)
        async with httpx.AsyncClient() as http:
            live = LiveExchange(KrakenPrivateClient(http, CREDS), state, now=lambda: NOW)
            yield Env(kraken, live, state)
    assert kraken.bad_signatures == 0  # todas las peticiones iban bien firmadas


def req(side: Side = Side.BUY, size: str = "2", reduce_only: bool = False,
        cli: str = "c1") -> OrderRequest:
    return OrderRequest(cli, SOL, side, D(size), D("100.5"), reduce_only)


async def test_equity_is_margin_equity_with_haircut(env: Env) -> None:
    # Ejemplo de la documentación: portfolioValue 34995.52, marginEquity 34122.66
    env.kraken.flex.update(portfolioValue="34995.52", marginEquity="34122.66",
                           maintenanceMargin="100")
    assert await env.live.equity_usd() == D("34122.66")
    assert await env.live.margin_buffer_usd() == D("34022.66")


async def test_missing_flex_account_is_an_error(env: Env) -> None:
    env.kraken.flex = None  # type: ignore[assignment]
    with pytest.raises(ExchangeError):
        await env.live.equity_usd()


async def test_positions_are_signed(env: Env) -> None:
    env.kraken.positions = [
        {"symbol": "PF_SOLUSD", "side": "long", "size": 2, "price": 100},
        {"symbol": "PF_XBTUSD", "side": "short", "size": "0.01", "price": 80000},
    ]
    assert await env.live.positions() == {SOL: D(2), BTC: D("-0.01")}
    env.kraken.positions = [{"symbol": "PF_SOLUSD", "side": "flat", "size": 1, "price": 1}]
    with pytest.raises(ExchangeError):
        await env.live.positions()


async def test_ioc_order_parameters_and_executions(env: Env) -> None:
    r = await env.live.send_order(req(reduce_only=True))
    sent = env.kraken.sends()[-1]
    assert sent == {"orderType": "ioc", "symbol": SOL, "side": "buy", "size": "2",
                    "limitPrice": "100.5", "cliOrdId": "c1", "reduceOnly": "true"}
    assert (r.status, r.filled_size, r.avg_price) == (OrderStatus.FILLED, D(2), D(101))
    assert r.fee_usd is None  # la comisión real no viene en la respuesta


@pytest.mark.parametrize(
    ("mode", "status"),
    [("none", OrderStatus.NOT_FILLED), ("insufficientAvailableFunds", OrderStatus.REJECTED),
     ("wouldNotReducePosition", OrderStatus.REJECTED), ("marketSuspended", OrderStatus.REJECTED)],
)
async def test_unfilled_and_rejected_orders(env: Env, mode: str, status: OrderStatus) -> None:
    env.kraken.ioc_mode = mode
    r = await env.live.send_order(req())
    assert (r.status, r.filled_size) == (status, D(0))


async def test_duplicate_client_id_forces_reconciliation(env: Env) -> None:
    env.kraken.ioc_mode = "clientOrderIdAlreadyExist"
    with pytest.raises(ExchangeError):
        await env.live.send_order(req())


async def test_find_order_uses_fills_then_recent_status(env: Env) -> None:
    await env.live.send_order(req(cli="vista"))
    found = await env.live.find_order("vista")
    assert found is not None and (found.filled_size, found.avg_price) == (D(2), D(100))
    assert await env.live.find_order("desconocida") is None


async def test_lost_response_is_reconciled_through_fills_without_resending(
    env: Env, tmp_path: Path
) -> None:
    env.kraken.lose_response = True
    state = env.state
    store = StateStore(tmp_path / "state.json")
    market = FakeMarket()
    ex = Executor(exchange=env.live, store=store, state=state,
                  breaker=CircuitBreaker(state, RiskConfig(), clock=lambda: NOW.timestamp()),
                  recorder=CsvRecorder(tmp_path), cfg=ExecutionConfig(), now=lambda: NOW)
    report = await ex.execute(
        [Action(ActionKind.OPEN, SOL, Side.BUY, D(2), False, D(100))],
        markets=market.specs, positions={}, ctx=ExecutionContext(mode="live"))
    assert report.results[0].status is OrderStatus.FILLED
    assert len(env.kraken.sends("ioc")) == 1  # nunca se reenvió
    assert await env.live.positions() == {SOL: D(2)}
    assert state.pending_orders == {}
    row = (tmp_path / "trades.csv").read_text().splitlines()[1].split(",")
    assert row[1] == "live" and row[11] == ""  # comisión desconocida, no inventada


# --- funding real ---


def log_entry(ms: int, amount_old: str, amount_new: str, contract: str = "pf_xbtusd") -> Any:
    return {"_ms": ms, "date": datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(),
            "info": "funding rate change", "asset": "usd", "contract": contract,
            "old_balance": amount_old, "new_balance": amount_new,
            "realized_funding": "0", "funding_rate": "0.5", "booking_uid": "b", "id": 1}


async def test_funding_from_account_log(env: Env) -> None:
    start = int(NOW.timestamp() * 1000)
    assert await env.live.collect_funding(NOW) == []  # primer arranque: sin histórico
    assert env.state.live_funding_cursor_ms == start
    env.kraken.logs = [log_entry(start - 1000, "100", "90"),  # anterior: no se importa
                       log_entry(start + 1000, "100", "99.5"),
                       log_entry(start + 2000, "99.5", "99.7")]
    assert await env.live.collect_funding(NOW + timedelta(seconds=60)) == []  # limitado
    events = await env.live.collect_funding(NOW + timedelta(minutes=6))
    assert [(e.symbol, e.amount_usd) for e in events] == [(BTC, D("-0.5")), (BTC, D("0.2"))]
    params = [p for _, path, p in env.kraken.calls if "account-log" in path][-1]
    assert params["info"] == "funding rate change" and params["sort"] == "asc"
    again = await env.live.collect_funding(NOW + timedelta(minutes=12))
    assert again == []  # el cursor avanzó: sin duplicados


# --- stops de catástrofe ---


async def test_places_one_reduce_only_stop_per_position(env: Env) -> None:
    market = FakeMarket()
    env.kraken.positions = [{"symbol": SOL, "side": "long", "size": "2", "price": "100"},
                            {"symbol": BTC, "side": "short", "size": "0.001", "price": "80000"}]
    warnings = await env.live.sync_catastrophe_stops({SOL: D(2), BTC: D("-0.001")},
                                                     market.specs, D(20))
    assert warnings == []
    stops = {o["symbol"]: o for o in env.kraken.open_orders}
    assert (stops[SOL]["side"], D(stops[SOL]["stopPrice"])) == ("sell", D(80))
    assert (stops[BTC]["side"], D(stops[BTC]["stopPrice"])) == ("buy", D(96000))
    assert all(o["reduceOnly"] and o["triggerSignal"] == "mark" and
               o["cliOrdId"].startswith(STOP_PREFIX) for o in env.kraken.open_orders)
    # Segunda pasada sin cambios: no se toca nada
    n = len(env.kraken.sends("stp"))
    await env.live.sync_catastrophe_stops({SOL: D(2), BTC: D("-0.001")}, market.specs, D(20))
    assert len(env.kraken.sends("stp")) == n and env.kraken.cancels() == []


async def test_stop_is_replaced_when_size_changes_and_cancelled_when_closed(env: Env) -> None:
    market = FakeMarket()
    env.kraken.positions = [{"symbol": SOL, "side": "long", "size": "2", "price": "100"}]
    await env.live.sync_catastrophe_stops({SOL: D(2)}, market.specs, D(20))
    env.kraken.positions[0]["size"] = "3"
    await env.live.sync_catastrophe_stops({SOL: D(3)}, market.specs, D(20))
    assert len(env.kraken.cancels()) == 1
    assert [o["unfilledSize"] for o in env.kraken.open_orders] == ["3"]
    env.kraken.positions = []
    await env.live.sync_catastrophe_stops({}, market.specs, D(20))
    assert env.kraken.open_orders == []


async def test_manual_orders_are_never_cancelled(env: Env) -> None:
    env.kraken.open_orders = [{"cliOrdId": "manual-1", "symbol": SOL, "side": "sell",
                               "orderType": "lmt", "unfilledSize": "1"}]
    await env.live.sync_catastrophe_stops({}, FakeMarket().specs, D(20))
    assert env.kraken.cancels() == []


async def test_stop_is_moved_before_estimated_liquidation(env: Env) -> None:
    market = FakeMarket()
    # Margen libre de 30 USD: un stop al 20 % sobre 200 USD perdería 40 > 0.8 x 30
    env.kraken.flex.update(marginEquity="40", maintenanceMargin="10")
    env.kraken.positions = [{"symbol": SOL, "side": "long", "size": "2", "price": "100"}]
    warnings = await env.live.sync_catastrophe_stops({SOL: D(2)}, market.specs, D(20))
    assert any("liquidación" in w for w in warnings)
    stop = D(env.kraken.open_orders[0]["stopPrice"])
    assert stop >= D(88)  # pérdida en el stop <= 24 USD (0.8 x 30)


# --- ciclo completo en live (API simulada) ---


async def test_full_live_cycle_with_startup_profile_and_stops(env: Env, tmp_path: Path) -> None:
    from copybot.alerts import LogAlerter
    from copybot.config import Config
    from copybot.engine import Engine, Outcome
    from tests.conftest import LEADER
    from tests.fakes import FakeLeader

    cfg = Config.model_validate({"leader_address": LEADER, "mode": "live",
                                 "filters": {"ignore_preexisting": False}})
    market = FakeMarket()
    env.kraken.fill_price = D(100)  # el simulador ejecuta a precio fijo
    leader = FakeLeader("100000", SOL="5000")
    leader.clock = lambda: NOW
    market.set_mark(SOL, "100")
    engine = Engine(cfg=cfg, state=env.state, store=StateStore(tmp_path / "s.json"),
                    leader=leader, market=market, exchange=env.live,
                    recorder=CsvRecorder(tmp_path), alerter=LogAlerter(), kill_dirs=[tmp_path],
                    startup_profile=True, now=lambda: NOW,
                    breaker_clock=lambda: NOW.timestamp())
    assert (await engine.cycle()).outcome is Outcome.OK
    pos = await env.live.positions()
    assert set(pos) == {SOL} and pos[SOL] * 100 <= 100  # perfil de arranque: <= 100 USD
    [ioc] = env.kraken.sends("ioc")
    assert ioc["reduceOnly"] == "false" and ioc["orderType"] == "ioc"
    [stop] = env.kraken.open_orders
    assert stop["side"] == "sell" and stop["unfilledSize"] == str(pos[SOL])
    assert (await engine.cycle()).outcome is Outcome.OK  # idempotente: nada nuevo
    assert len(env.kraken.sends("ioc")) == 1 and len(env.kraken.sends("stp")) == 1
