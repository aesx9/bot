from __future__ import annotations

import csv
import json
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from copybot import limits
from copybot.config import ExecutionConfig, PaperConfig, RiskConfig
from copybot.exchange.base import ExchangeError, OrderRequest, OrderResult, OrderStatus
from copybot.exchange.kraken_public import OrderBook
from copybot.exchange.paper import PaperAccount, PaperExchange
from copybot.executor import (
    CircuitBreakerTripped,
    ExecutionContext,
    ExecutionReport,
    Executor,
    OrderUncertain,
    limit_price,
)
from copybot.models import Action, ActionKind, Side
from copybot.records import CsvRecorder
from copybot.risk import CircuitBreaker
from copybot.state import BotState, StateStore
from tests.fakes import NOW, FakeMarket

SOL = "PF_SOLUSD"
CTX = ExecutionContext(mode="paper", leader_prices={SOL: D(114)}, leader_time=NOW)


class FlakyExchange(PaperExchange):
    """Paper con fallos de red inyectables en el envío y la consulta."""

    def __init__(self, *a: Any, store_path: Path, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.store_path = store_path
        self.send_mode = "ok"  # ok | timeout_after | timeout_before
        self.find_fails = False
        self.sent: list[str] = []
        self.pending_seen_on_disk: list[bool] = []

    async def send_order(self, req: OrderRequest) -> OrderResult:
        on_disk = json.loads(self.store_path.read_text())["pending_orders"]
        self.pending_seen_on_disk.append(req.cli_ord_id in on_disk)
        self.sent.append(req.cli_ord_id)
        if self.send_mode == "timeout_before":
            raise TimeoutError
        result = await super().send_order(req)
        if self.send_mode == "timeout_after":
            raise ExchangeError("respuesta perdida")  # llegó al exchange, la respuesta no
        return result

    async def find_order(self, cli_ord_id: str) -> OrderResult | None:
        if self.find_fails:
            raise ExchangeError("sin conexión")
        return await super().find_order(cli_ord_id)


def setup(tmp_path: Path, **risk: Any) -> tuple[Executor, FlakyExchange, BotState, StateStore,
                                                 FakeMarket, dict[str, Any]]:
    market = FakeMarket()
    market.set_mark(SOL, "100")
    clock = {"now": NOW}
    cfg = PaperConfig(taker_fee_pct=D(0))
    ex = FlakyExchange(PaperAccount.new(cfg), market, cfg, now=lambda: clock["now"],
                       store_path=tmp_path / "state.json")
    state = BotState()
    store = StateStore(tmp_path / "state.json",
                       before_save=lambda st: setattr(st, "paper", ex.account.to_dict()))
    store.save(state)
    breaker = CircuitBreaker(state, RiskConfig(**risk), clock=lambda: clock["now"].timestamp())
    execu = Executor(exchange=ex, store=store, state=state, breaker=breaker,
                     recorder=CsvRecorder(tmp_path), cfg=ExecutionConfig(),
                     now=lambda: clock["now"], pending_grace_seconds=60)
    return execu, ex, state, store, market, clock


def act(kind: ActionKind, side: Side, size: str, reduce_only: bool = False,
        ref: str = "100") -> Action:
    return Action(kind, SOL, side, D(size), reduce_only, D(ref))


async def run_report(execu: Executor, market: FakeMarket, actions: list[Action],
                     positions: dict[str, D] | None = None, **kw: Any) -> ExecutionReport:
    return await execu.execute(actions, markets=market.specs, positions=positions or {},
                               ctx=CTX, **kw)


async def run(execu: Executor, market: FakeMarket, actions: list[Action],
              positions: dict[str, D] | None = None, **kw: Any) -> list[OrderResult]:
    return (await run_report(execu, market, actions, positions, **kw)).results


def trades(tmp_path: Path) -> list[dict[str, str]]:
    p = tmp_path / "trades.csv"
    return list(csv.DictReader(p.open())) if p.exists() else []


@pytest.mark.parametrize(
    ("side", "ref", "cap", "tick", "expected"),
    [
        (Side.BUY, "100", "0.5", "0.01", "100.50"),
        (Side.SELL, "100", "0.5", "0.01", "99.50"),
        (Side.BUY, "82588.89", "0.5", "1", "83001"),  # 83001.83 -> hacia abajo
        (Side.SELL, "82588.89", "0.5", "1", "82176"),  # 82175.94 -> hacia arriba
    ],
)
def test_limit_price_is_conservative(side: Side, ref: str, cap: str, tick: str,
                                     expected: str) -> None:
    assert limit_price(side, D(ref), D(cap), D(tick)) == D(expected)


async def test_order_is_persisted_before_sending_and_cleared_after(tmp_path: Path) -> None:
    execu, ex, state, _, market, _ = setup(tmp_path)
    [r] = await run(execu, market, [act(ActionKind.OPEN, Side.BUY, "2")])
    assert ex.pending_seen_on_disk == [True]  # el cliOrdId ya estaba en disco al enviar
    assert r.status is OrderStatus.FILLED and state.pending_orders == {}
    rows = trades(tmp_path)
    assert len(rows) == 1 and rows[0]["cli_ord_id"] == r.cli_ord_id
    assert rows[0]["precio_lider"] == "114" and rows[0]["modo"] == "paper"


async def test_client_order_ids_are_unique(tmp_path: Path) -> None:
    execu, ex, _, _, market, _ = setup(tmp_path)
    await run(execu, market, [act(ActionKind.OPEN, Side.BUY, "1")])
    await run(execu, market, [act(ActionKind.INCREASE, Side.BUY, "1")], {SOL: D(1)})
    assert len(set(ex.sent)) == 2


async def test_lost_response_is_reconciled_not_resent(tmp_path: Path) -> None:
    execu, ex, state, _, market, _ = setup(tmp_path)
    ex.send_mode = "timeout_after"
    [r] = await run(execu, market, [act(ActionKind.OPEN, Side.BUY, "2")])
    assert r.status is OrderStatus.FILLED  # se supo por find_order
    assert len(ex.sent) == 1  # nunca se reenvió
    assert await ex.positions() == {SOL: D(2)}
    assert len(trades(tmp_path)) == 1 and state.pending_orders == {}


async def test_unknown_outcome_blocks_and_next_cycle_never_duplicates(tmp_path: Path) -> None:
    execu, ex, state, store, market, clock = setup(tmp_path)
    ex.send_mode, ex.find_fails = "timeout_after", True
    with pytest.raises(OrderUncertain):
        await run(execu, market, [act(ActionKind.OPEN, Side.BUY, "2")])
    assert len(store.load().pending_orders) == 1  # queda anotada en disco
    # Siguiente ciclo: la red vuelve; la reconciliación encuentra la orden ejecutada
    ex.send_mode, ex.find_fails = "ok", False
    await execu.reconcile_pending()
    assert state.pending_orders == {}
    assert await ex.positions() == {SOL: D(2)}
    assert len(ex.sent) == 1 and len(trades(tmp_path)) == 1


async def test_order_that_never_arrived_is_dropped_only_after_grace(tmp_path: Path) -> None:
    execu, ex, state, _, market, clock = setup(tmp_path)
    ex.send_mode = "timeout_before"
    with pytest.raises(OrderUncertain):
        await run(execu, market, [act(ActionKind.OPEN, Side.BUY, "2")])
    ex.send_mode = "ok"
    with pytest.raises(OrderUncertain):  # aún dentro del margen: no operar
        await execu.reconcile_pending()
    clock["now"] = NOW + timedelta(seconds=61)
    await execu.reconcile_pending()
    assert state.pending_orders == {} and await ex.positions() == {}


async def test_reduce_only_flags_are_sent_as_planned(tmp_path: Path) -> None:
    execu, ex, _, _, market, _ = setup(tmp_path)
    await run(execu, market, [act(ActionKind.OPEN, Side.BUY, "3")])
    sent: list[OrderRequest] = []
    original = ex.send_order

    async def spy(req: OrderRequest) -> OrderResult:
        sent.append(req)
        return await original(req)

    ex.send_order = spy  # type: ignore[method-assign]
    await run(execu, market, [act(ActionKind.REDUCE, Side.SELL, "1", True),
                              act(ActionKind.CLOSE, Side.SELL, "2", True)], {SOL: D(3)})
    assert [s.reduce_only for s in sent] == [True, True]
    assert await ex.positions() == {}


async def test_flip_open_waits_for_complete_close(tmp_path: Path) -> None:
    execu, ex, _, _, market, _ = setup(tmp_path)
    await run(execu, market, [act(ActionKind.OPEN, Side.BUY, "3")])
    # Libro con poca liquidez en bids: el cierre solo se ejecuta en parte
    market.books[SOL] = OrderBook(SOL, bids=((D(100), D(1)),), asks=((D("100.1"), D(100)),))
    results = await run(execu, market, [act(ActionKind.FLIP_CLOSE, Side.SELL, "3", True),
                                        act(ActionKind.FLIP_OPEN, Side.SELL, "2")], {SOL: D(3)})
    assert len(results) == 1 and results[0].status is OrderStatus.PARTIAL
    assert await ex.positions() == {SOL: D(2)}  # nunca abre el corto con el largo vivo


async def test_order_limit_defers_instead_of_exceeding(tmp_path: Path) -> None:
    execu, ex, _, _, market, _ = setup(tmp_path, max_orders_per_minute=2)
    acts = [act(ActionKind.OPEN, Side.BUY, "0.2"), act(ActionKind.OPEN, Side.BUY, "0.2"),
            act(ActionKind.OPEN, Side.BUY, "0.2")]
    report = await run_report(execu, market, acts)
    assert len(ex.sent) == 2 and report.deferred == 1  # nunca más de 2 en el minuto


async def test_notional_per_hour_still_stops(tmp_path: Path) -> None:
    execu, ex, _, _, market, _ = setup(tmp_path, max_notional_per_hour_usd=D(150))
    with pytest.raises(CircuitBreakerTripped):
        await run(execu, market, [act(ActionKind.OPEN, Side.BUY, "1"),
                                  act(ActionKind.INCREASE, Side.BUY, "1")])
    assert len(ex.sent) == 1


async def test_emergency_close_bypasses_breaker_but_only_reduce_only(tmp_path: Path) -> None:
    execu, ex, _, _, market, _ = setup(tmp_path, max_orders_per_minute=1)
    await run(execu, market, [act(ActionKind.OPEN, Side.BUY, "1")])
    await run(execu, market, [act(ActionKind.CLOSE, Side.SELL, "1", True)], {SOL: D(1)},
              emergency=True)
    assert await ex.positions() == {}
    with pytest.raises(limits.HardLimitViolation):
        await run(execu, market, [act(ActionKind.OPEN, Side.BUY, "1")], emergency=True)


async def test_hard_cap_defence_in_executor(tmp_path: Path) -> None:
    execu, ex, _, _, market, _ = setup(tmp_path)
    too_big = limits.HARD_MAX_NOTIONAL_PER_ASSET_USD / 100 + 1  # unidades a 100 USD
    with pytest.raises(limits.HardLimitViolation):
        await run(execu, market, [act(ActionKind.OPEN, Side.BUY, str(too_big))])
    assert ex.sent == []


async def test_rejection_is_reported_not_raised(tmp_path: Path) -> None:
    execu, ex, _, _, market, _ = setup(tmp_path)
    [r] = await run(execu, market, [act(ActionKind.CLOSE, Side.SELL, "1", True)], {SOL: D(1)})
    assert r.status is OrderStatus.REJECTED and trades(tmp_path) == []


# --- M12: topes sobre la exposición real ---


def limits_for(max_asset: str, max_total: str) -> Any:
    from copybot.executor import ExposureLimits

    return ExposureLimits(prices={SOL: D(100)}, max_asset_usd=D(max_asset),
                          max_total_usd=D(max_total))


async def test_increase_above_the_per_asset_cap_is_skipped_not_sent(tmp_path: Path) -> None:
    """El perfil de arranque (100 USD/activo) también rige la exposición REAL."""
    execu, ex, _, _, market, _ = setup(tmp_path)
    report = await run_report(execu, market, [act(ActionKind.OPEN, Side.BUY, "1.5")],
                              exposure=limits_for("100", "1000"))
    assert (report.results, report.skipped, ex.sent) == ([], 1, [])
    report = await run_report(execu, market, [act(ActionKind.OPEN, Side.BUY, "1")],
                              exposure=limits_for("100", "1000"))
    assert report.skipped == 0 and len(report.results) == 1  # justo en el tope: pasa


async def test_open_is_skipped_if_real_positions_already_use_the_total(tmp_path: Path) -> None:
    from copybot.executor import ExposureLimits

    execu, ex, _, _, market, _ = setup(tmp_path)
    market.set_mark("PF_ETHUSD", "100")
    exposure = ExposureLimits(prices={SOL: D(100), "PF_ETHUSD": D(100)},
                              max_asset_usd=D(500), max_total_usd=D(250))
    open_eth = Action(ActionKind.OPEN, "PF_ETHUSD", Side.BUY, D(1), False, D(100))
    # SOL ya ocupa 200 USD reales y la orden añadiría 100: 300 > 250
    report = await run_report(execu, market, [open_eth], {SOL: D(2)}, exposure=exposure)
    assert (report.skipped, report.results, ex.sent) == (1, [], [])
    roomy = ExposureLimits(prices=exposure.prices, max_asset_usd=D(500), max_total_usd=D(300))
    report = await run_report(execu, market, [open_eth], {SOL: D(2)}, exposure=roomy)
    assert report.skipped == 0 and len(report.results) == 1  # 300 <= 300: pasa


async def test_reductions_and_closes_are_never_blocked_by_the_guard(tmp_path: Path) -> None:
    execu, ex, _, _, market, _ = setup(tmp_path)
    await run(execu, market, [act(ActionKind.OPEN, Side.BUY, "3")])
    report = await run_report(
        execu, market,
        [act(ActionKind.REDUCE, Side.SELL, "1", True), act(ActionKind.CLOSE, Side.SELL, "2", True)],
        {SOL: D(3)}, exposure=limits_for("1", "1"))
    assert report.skipped == 0 and await ex.positions() == {}
