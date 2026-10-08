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
    query = [p for _, path, p in env.kraken.calls if "account-log" in path][-1]
    assert query["sort"] == "asc"
    again = await env.live.collect_funding(NOW + timedelta(minutes=12))
    assert again == []  # el cursor avanzó: sin duplicados


async def test_funding_sign_check_alerts_on_mismatch_and_verifies_once(env: Env) -> None:
    start = int(NOW.timestamp() * 1000)
    env.kraken.positions = [{"symbol": BTC, "side": "long", "size": "1", "price": "80000"}]
    await env.live.positions()
    await env.live.collect_funding(NOW)  # fija el cursor
    # Largo con tasa positiva: debe PAGAR. Primero un cobro (incoherente), luego un pago.
    env.kraken.logs = [log_entry(start + 1000, "100", "100.5"),
                       log_entry(start + 2000, "100.5", "100.0")]
    await env.live.collect_funding(NOW + timedelta(minutes=6))
    alerts = env.live.drain_alerts()
    assert len(alerts) == 1 and "SIGNO DEL FUNDING" in alerts[0] and "pago" in alerts[0]
    assert env.state.funding_sign_verified  # el segundo sí cuadró
    assert env.live.drain_alerts() == []


async def test_realized_funding_and_balance_disagreement_alerts(env: Env) -> None:
    start = int(NOW.timestamp() * 1000)
    await env.live.collect_funding(NOW)
    entry = log_entry(start + 1000, "100", "99")
    entry["realized_funding"] = "1"
    env.kraken.logs = [entry]
    await env.live.collect_funding(NOW + timedelta(minutes=6))
    assert any("signos distintos" in a for a in env.live.drain_alerts())


async def test_ledger_records_real_fills_and_fees_but_not_history(env: Env) -> None:
    start = int(NOW.timestamp() * 1000)
    env.kraken.fills = [{"cliOrdId": None, "fillTime": "2026-10-01T00:00:00Z", "fillType": "taker",
                         "fill_id": "viejo", "order_id": "o", "price": "1", "side": "buy",
                         "size": "1", "symbol": "PF_SOLUSD"}]
    await env.live.collect_funding(NOW)  # primer arranque: lo anterior no se registra
    assert env.live.drain_ledger() == ([], [])
    await env.live.send_order(req(cli="c-bot"))
    env.kraken.fills.append({"cliOrdId": "cs-x", "fillTime": "2026-10-08T12:03:00Z",
                             "fillType": "taker", "fill_id": "stop", "order_id": "o2",
                             "price": "80", "side": "sell", "size": "2", "symbol": "PF_SOLUSD"})
    env.kraken.fills.append({"cliOrdId": None, "fillTime": "2026-10-08T12:04:00Z",
                             "fillType": "liquidation", "fill_id": "liq", "order_id": "o3",
                             "price": "70", "side": "sell", "size": "1", "symbol": "PF_SOLUSD"})
    env.kraken.logs = [{"_ms": start + 5000, "date": "2026-10-08T12:00:05+00:00",
                        "info": "futures trade", "contract": "pf_solusd", "fee": "0.1",
                        "collateral": "USD", "booking_uid": "b1"}]
    await env.live.collect_funding(NOW + timedelta(minutes=6))
    fills, fees = env.live.drain_ledger()
    assert [(f["fill_id"], f["origin"]) for f in fills] == [
        ("f1", "bot"), ("stop", "stop_catastrofe"), ("liq", "liquidación")]
    assert [(f["symbol"], f["fee"], f["currency"]) for f in fees] == [(SOL, D("0.1"), "USD")]
    await env.live.collect_funding(NOW + timedelta(minutes=12))
    assert env.live.drain_ledger()[0] == []  # sin duplicados


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
    leader.mids.update(SOL=D(100), ETH=D(100))  # coherentes con las marcas de Kraken
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


# --- A1: respuestas malformadas = ExchangeError, nunca una excepción suelta ---


async def test_malformed_account_log_entries_are_skipped_with_an_alert(env: Env) -> None:
    start = int(NOW.timestamp() * 1000)
    await env.live.collect_funding(NOW)
    ok = log_entry(start + 3000, "100", "99.5")
    env.kraken.logs = [
        {"_ms": start + 1000, "info": "funding rate change", "contract": "pf_xbtusd"},  # sin date
        {"_ms": start + 2000, "info": "funding rate change", "date": "no-es-fecha"},
        ok,
    ]
    events = await env.live.collect_funding(NOW + timedelta(minutes=6))
    assert [e.amount_usd for e in events] == [D("-0.5")]
    alerts = env.live.drain_alerts()
    assert len(alerts) == 2 and all("account-log" in a for a in alerts)
    # el cursor avanzó más allá de las entradas ilegibles: no se atasca
    assert env.state.live_funding_cursor_ms is not None
    assert env.state.live_funding_cursor_ms > start + 3000


@pytest.mark.parametrize("breakage", ["no_symbol", "accounts_list", "fills_not_list"])
async def test_malformed_payloads_raise_exchange_error(env: Env, breakage: str) -> None:
    if breakage == "no_symbol":
        env.kraken.positions = [{"side": "long", "size": "1", "price": "1"}]
        with pytest.raises(ExchangeError):
            await env.live.positions()
    elif breakage == "accounts_list":
        env.kraken.flex = []  # type: ignore[assignment]
        with pytest.raises(ExchangeError):
            await env.live.equity_usd()
    else:
        env.kraken.fills = {"x": 1}  # type: ignore[assignment]
        with pytest.raises(ExchangeError):
            await env.live.find_order("c1")


async def test_stop_is_placed_even_when_the_cycle_aborts_mid_execution(
    env: Env, tmp_path: Path
) -> None:
    """A2: lo abierto antes de que salte el breaker lleva stop de catástrofe y consta como
    gestionado en disco."""
    from copybot.alerts import LogAlerter
    from copybot.config import Config
    from copybot.engine import Engine, Outcome
    from tests.conftest import LEADER
    from tests.fakes import FakeLeader

    cfg = Config.model_validate({"leader_address": LEADER, "mode": "live",
                                 "filters": {"ignore_preexisting": False},
                                 "risk": {"max_notional_per_hour_usd": 150}})
    market = FakeMarket()
    for sym in (SOL, "PF_ETHUSD"):
        market.set_mark(sym, "100")
    env.kraken.fill_price = D(100)
    leader = FakeLeader("100000", SOL="5000", ETH="5000")
    leader.clock = lambda: NOW
    leader.mids.update(SOL=D(100), ETH=D(100))  # coherentes con las marcas de Kraken
    store = StateStore(tmp_path / "s.json")
    engine = Engine(cfg=cfg, state=env.state, store=store, leader=leader, market=market,
                    exchange=env.live, recorder=CsvRecorder(tmp_path), alerter=LogAlerter(),
                    kill_dirs=[tmp_path], startup_profile=True, now=lambda: NOW,
                    breaker_clock=lambda: NOW.timestamp())
    assert (await engine.cycle()).outcome is Outcome.HALTED  # 2.ª orden: 100 + 100 > 150
    opened = set(await env.live.positions())
    assert len(opened) == 1
    assert store.load().managed_symbols == opened
    [stop] = env.kraken.open_orders
    assert stop["symbol"] in opened and stop["reduceOnly"] is True


async def test_first_cycle_fills_reach_the_fiscal_ledger(env: Env, tmp_path: Path) -> None:
    """A4 (PoC A): el primer ciclo live opera ANTES de la primera llamada a collect_funding;
    sus fills y comisiones no pueden descartarse como "anteriores al primer arranque"."""
    import csv

    from copybot.alerts import LogAlerter
    from copybot.config import Config
    from copybot.engine import Engine, Outcome
    from tests.conftest import LEADER
    from tests.fakes import FakeLeader

    env.kraken.fills = [{"cliOrdId": None, "fillTime": "2026-10-01T00:00:00Z",
                         "fillType": "taker", "fill_id": "viejo", "order_id": "o",
                         "price": "1", "side": "buy", "size": "1", "symbol": SOL}]
    cfg = Config.model_validate({"leader_address": LEADER, "mode": "live",
                                 "filters": {"ignore_preexisting": False}})
    market = FakeMarket()
    market.set_mark(SOL, "100")
    env.kraken.fill_price = D(100)
    leader = FakeLeader("100000", SOL="5000")
    leader.clock = lambda: NOW
    leader.mids.update(SOL=D(100), ETH=D(100))  # coherentes con las marcas de Kraken
    engine = Engine(cfg=cfg, state=env.state, store=StateStore(tmp_path / "s.json"),
                    leader=leader, market=market, exchange=env.live,
                    recorder=CsvRecorder(tmp_path), alerter=LogAlerter(), kill_dirs=[tmp_path],
                    startup_profile=True, now=lambda: NOW,
                    breaker_clock=lambda: NOW.timestamp())
    assert (await engine.cycle()).outcome is Outcome.OK
    assert len(env.kraken.sends("ioc")) == 1  # el primer ciclo sí operó
    rows = list(csv.DictReader((tmp_path / "kraken_fills.csv").open()))
    assert [(r["fill_id"], r["origen"], r["lado"]) for r in rows] == [("f1", "bot", "buy")]
    assert "viejo" in env.state.fills_seen  # lo anterior al bot solo se marca como visto


async def test_prepare_ledger_is_idempotent_and_runs_once(env: Env) -> None:
    await env.live.prepare_ledger(NOW)
    first = env.state.live_funding_cursor_ms
    assert first == int(NOW.timestamp() * 1000)
    n = len([c for c in env.kraken.calls if c[1].endswith("/fills")])
    await env.live.prepare_ledger(NOW + timedelta(hours=1))
    assert env.state.live_funding_cursor_ms == first
    assert len([c for c in env.kraken.calls if c[1].endswith("/fills")]) == n


# --- M1: nunca notación científica en lo que se envía a Kraken ---

POSITIONAL = __import__("re").compile(r"^\d+(\.\d+)?$")


async def test_negative_precision_market_sends_positional_decimals(env: Env) -> None:
    """PoC (formato): PF_PEPEUSD (precisión -3, tick 1E-10) enviaba size=5E+3."""
    from copybot.executor import limit_price

    spec = FakeMarket().specs["PF_PEPEUSD"]
    size = spec.round_down(D("5200"))
    assert str(size) == "5E+3"  # el Decimal real tiene exponente: la serialización lo arregla
    price = limit_price(Side.BUY, D("0.0000009"), D("0.5"), spec.tick_size)
    assert "E" in str(price)
    await env.live.send_order(OrderRequest("c1", "PF_PEPEUSD", Side.BUY, size, price, False))
    sent = env.kraken.sends()[-1]
    assert (sent["size"], sent["limitPrice"]) == ("5000", "0.0000009045")
    assert POSITIONAL.match(sent["size"]) and POSITIONAL.match(sent["limitPrice"])


async def test_stop_prices_and_sizes_are_positional(env: Env) -> None:
    spec = FakeMarket().specs["PF_PEPEUSD"]
    env.kraken.positions = [{"symbol": "PF_PEPEUSD", "side": "long", "size": "5000",
                             "price": "0.0000009"}]
    await env.live.sync_catastrophe_stops({"PF_PEPEUSD": D("5E+3")}, {"PF_PEPEUSD": spec}, D(20))
    [stop] = env.kraken.sends("stp")
    assert POSITIONAL.match(stop["size"]) and POSITIONAL.match(stop["stopPrice"])
    assert stop["size"] == "5000"


# --- M2: stops de catástrofe ---


async def test_failed_replacement_keeps_the_old_stop(env: Env) -> None:
    """PoC G: antes se cancelaba el stop y luego se colocaba el nuevo; si el alta fallaba,
    la posición se quedaba sin ninguno."""
    market = FakeMarket()
    env.kraken.positions = [{"symbol": SOL, "side": "long", "size": "2", "price": "100"}]
    await env.live.sync_catastrophe_stops({SOL: D(2)}, market.specs, D(20))
    [old] = env.kraken.open_orders
    env.kraken.positions[0]["size"] = "3"
    original = env.kraken._send

    def flaky(p: dict[str, str]) -> httpx.Response:
        if p["orderType"] == "stp":
            return httpx.Response(503, json={"result": "error", "error": "unavailable"})
        return original(p)

    env.kraken._send = flaky  # type: ignore[method-assign]
    with pytest.raises(ExchangeError):
        await env.live.sync_catastrophe_stops({SOL: D(3)}, market.specs, D(20))
    assert env.kraken.open_orders == [old] and env.kraken.cancels() == []


async def test_new_stop_is_placed_before_the_old_one_is_cancelled(env: Env) -> None:
    market = FakeMarket()
    env.kraken.positions = [{"symbol": SOL, "side": "long", "size": "2", "price": "100"}]
    await env.live.sync_catastrophe_stops({SOL: D(2)}, market.specs, D(20))
    env.kraken.positions[0]["size"] = "3"
    mark = len(env.kraken.calls)
    await env.live.sync_catastrophe_stops({SOL: D(3)}, market.specs, D(20))
    order = [p.rsplit("/", 1)[1] for _, p, _ in env.kraken.calls[mark:]
             if p.endswith(("/sendorder", "/cancelorder"))]
    assert order == ["sendorder", "cancelorder"]
    assert [o["unfilledSize"] for o in env.kraken.open_orders] == ["3"]


async def test_replacement_falls_back_when_the_exchange_allows_one_stop_per_symbol(
    env: Env,
) -> None:
    market = FakeMarket()
    env.kraken.one_stop_per_symbol = True
    env.kraken.positions = [{"symbol": SOL, "side": "long", "size": "2", "price": "100"}]
    await env.live.sync_catastrophe_stops({SOL: D(2)}, market.specs, D(20))
    env.kraken.positions[0]["size"] = "3"
    warnings = await env.live.sync_catastrophe_stops({SOL: D(3)}, market.specs, D(20))
    assert warnings == []
    assert [o["unfilledSize"] for o in env.kraken.open_orders] == ["3"]


async def test_error_after_trading_does_not_skip_the_stop_sync(env: Env, tmp_path: Path) -> None:
    """PoC G2: un 503 en /fills (libro fiscal) tras abrir dejaba la posición sin stop."""
    from copybot.alerts import LogAlerter
    from copybot.config import Config
    from copybot.engine import Engine
    from tests.conftest import LEADER
    from tests.fakes import FakeLeader

    # El log de cuenta (funding/comisiones) falla DESPUÉS de operar; /fills sí responde
    # porque la línea base del libro se lee antes de enviar nada (A4)
    env.kraken.fail_paths.add("/api/history/v3/account-log")
    cfg = Config.model_validate({"leader_address": LEADER, "mode": "live",
                                 "filters": {"ignore_preexisting": False}})
    market = FakeMarket()
    market.set_mark(SOL, "100")
    env.kraken.fill_price = D(100)
    leader = FakeLeader("100000", SOL="5000")
    leader.clock = lambda: NOW
    leader.mids.update(SOL=D(100), ETH=D(100))  # coherentes con las marcas de Kraken
    engine = Engine(cfg=cfg, state=env.state, store=StateStore(tmp_path / "s.json"),
                    leader=leader, market=market, exchange=env.live,
                    recorder=CsvRecorder(tmp_path), alerter=LogAlerter(), kill_dirs=[tmp_path],
                    startup_profile=True, now=lambda: NOW,
                    breaker_clock=lambda: NOW.timestamp())
    assert (await engine.cycle()).outcome.value == "error"  # _after_trading falló...
    assert await env.live.positions()
    assert len(env.kraken.open_orders) == 1  # ...pero el stop ya estaba colocado


async def test_kill_switch_cancels_the_catastrophe_stops(env: Env, tmp_path: Path) -> None:
    """PoC L: tras el cierre de emergencia el stop `cs-` seguía en el exchange."""
    from copybot.alerts import LogAlerter
    from copybot.config import Config
    from copybot.engine import Engine
    from tests.conftest import LEADER
    from tests.fakes import FakeLeader

    cfg = Config.model_validate({"leader_address": LEADER, "mode": "live",
                                 "filters": {"ignore_preexisting": False}})
    market = FakeMarket()
    market.set_mark(SOL, "100")
    env.kraken.fill_price = D(100)
    leader = FakeLeader("100000", SOL="5000")
    leader.clock = lambda: NOW
    leader.mids.update(SOL=D(100), ETH=D(100))  # coherentes con las marcas de Kraken
    engine = Engine(cfg=cfg, state=env.state, store=StateStore(tmp_path / "s.json"),
                    leader=leader, market=market, exchange=env.live,
                    recorder=CsvRecorder(tmp_path), alerter=LogAlerter(), kill_dirs=[tmp_path],
                    startup_profile=True, now=lambda: NOW,
                    breaker_clock=lambda: NOW.timestamp(), emergency_pause_seconds=0)
    await engine.cycle()
    assert len(env.kraken.open_orders) == 1
    (tmp_path / "STOP").touch()
    assert (await engine.cycle()).outcome.value == "halted"
    assert await env.live.positions() == {}
    assert env.kraken.open_orders == []


async def test_catastrophe_stop_distance_comes_from_drawdown_and_leverage(
    env: Env, tmp_path: Path
) -> None:
    """M13: con el perfil de arranque (1x) y drawdown 15 %, el stop de un largo a 100 va a 85."""
    from copybot.alerts import LogAlerter
    from copybot.config import Config
    from copybot.engine import Engine
    from tests.conftest import LEADER
    from tests.fakes import FakeLeader

    cfg = Config.model_validate({"leader_address": LEADER, "mode": "live",
                                 "filters": {"ignore_preexisting": False}})
    market = FakeMarket()
    market.set_mark(SOL, "100")
    env.kraken.fill_price = D(100)
    leader = FakeLeader("100000", SOL="5000")
    leader.clock = lambda: NOW
    leader.mids.update(SOL=D(100))
    engine = Engine(cfg=cfg, state=env.state, store=StateStore(tmp_path / "s.json"),
                    leader=leader, market=market, exchange=env.live,
                    recorder=CsvRecorder(tmp_path), alerter=LogAlerter(), kill_dirs=[tmp_path],
                    startup_profile=True, now=lambda: NOW,
                    breaker_clock=lambda: NOW.timestamp())
    await engine.cycle()
    [stop] = env.kraken.open_orders
    assert D(stop["stopPrice"]) == D(85)


async def test_partial_ioc_execution_is_reported_as_partial(env: Env) -> None:
    """Una ejecución parcial marcada como FILLED haría que un cambio de dirección abriera el
    lado nuevo con el viejo a medio cerrar."""
    env.kraken.fill_fraction = D("0.4")
    r = await env.live.send_order(req(size="5"))
    assert (r.status, r.filled_size) == (OrderStatus.PARTIAL, D(2))
