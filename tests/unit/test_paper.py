from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest

from copybot.config import PaperConfig
from copybot.exchange.base import OrderRequest, OrderStatus
from copybot.exchange.kraken_public import FundingRate, OrderBook, parse_orderbook
from copybot.exchange.paper import (
    PaperAccount,
    PaperExchange,
    PaperPosition,
    apply_fill,
    walk_book,
)
from copybot.models import Side
from tests.fakes import NOW, FakeMarket, load

SOL = "PF_SOLUSD"


def make(cfg: PaperConfig | None = None) -> tuple[PaperExchange, FakeMarket]:
    cfg = cfg or PaperConfig()
    m = FakeMarket()
    clock = {"now": NOW}
    ex = PaperExchange(PaperAccount.new(cfg), m, cfg, now=lambda: clock["now"])
    return ex, m


def req(side: Side, size: str, limit: str, reduce_only: bool = False,
        cli: str = "") -> OrderRequest:
    return OrderRequest(cli or f"id-{side}-{size}-{limit}-{reduce_only}", SOL, side, D(size),
                        D(limit), reduce_only)


# --- contabilidad de posiciones ---


@pytest.mark.parametrize(
    ("pos", "delta", "price", "new", "realized"),
    [
        (None, D(2), D(100), (D(2), D(100)), D(0)),
        ((D(2), D(100)), D(2), D(110), (D(4), D(105)), D(0)),  # promedia
        ((D(4), D(100)), D(-1), D(110), (D(3), D(100)), D(10)),  # cierre parcial largo
        ((D(-3), D(100)), D(3), D(90), None, D(30)),  # cierre total corto con ganancia
        ((D(2), D(100)), D(-5), D(90), (D(-3), D(90)), D(-20)),  # cruza por cero
    ],
)
def test_apply_fill(pos, delta, price, new, realized) -> None:  # type: ignore[no-untyped-def]
    p = PaperPosition(*pos) if pos else None
    got, pnl = apply_fill(p, delta, price)
    assert (None if got is None else (got.size, got.entry_price)) == new
    assert pnl == realized


def test_walk_book_on_real_orderbook_respects_limit() -> None:
    book = parse_orderbook(load("kraken_orderbook_solusd.json"), SOL)
    # La API real manda los bids en orden ascendente: tras ordenar, el mejor va primero
    assert book.bids[0][0] == D("114.28") and book.asks[0][0] == D("114.29")
    filled, notional = walk_book(book.asks, Side.BUY, D(100), D("114.30"))
    assert filled == D(100)  # 23.02 a 114.29 + 76.98 a 114.30
    assert notional == D("23.02") * D("114.29") + D("76.98") * D("114.30")
    filled, _ = walk_book(book.asks, Side.BUY, D(1000), D("114.30"))
    assert filled == D("23.02") + D("108.48")  # el resto no cabe dentro del límite
    filled, _ = walk_book(book.bids, Side.SELL, D(5), D("114.29"))
    assert filled == 0


# --- órdenes ---


async def test_ioc_buy_fills_against_book_with_taker_fee() -> None:
    ex, m = make()
    m.books[SOL] = OrderBook(SOL, bids=((D(99), D(100)),), asks=((D(100), D(1)), (D(101), D(5))))
    r = await ex.send_order(req(Side.BUY, "3", "101"))
    assert (r.status, r.filled_size, r.avg_price) == (OrderStatus.FILLED, D(3), D(302) / 3)
    assert r.fee_usd == D(302) * D("0.05") / 100
    assert (await ex.positions()) == {SOL: D(3)}
    assert ex.account.usd_balance == -r.fee_usd


async def test_ioc_partial_and_not_filled() -> None:
    ex, m = make()
    m.books[SOL] = OrderBook(SOL, bids=((D(99), D(100)),), asks=((D(100), D(1)), (D(101), D(5))))
    r = await ex.send_order(req(Side.BUY, "3", "100"))
    assert (r.status, r.filled_size) == (OrderStatus.PARTIAL, D(1))
    r2 = await ex.send_order(req(Side.BUY, "3", "99.5"))
    assert (r2.status, r2.filled_size) == (OrderStatus.NOT_FILLED, D(0))
    assert (await ex.positions()) == {SOL: D(1)}


async def test_reduce_only_is_clamped_and_never_opens() -> None:
    ex, m = make()
    m.books[SOL] = OrderBook(SOL, bids=((D(99), D(100)),), asks=((D(100), D(100)),))
    rej = await ex.send_order(req(Side.SELL, "1", "90", reduce_only=True))
    assert (rej.status, rej.reason) == (OrderStatus.REJECTED, "wouldNotReducePosition")
    await ex.send_order(req(Side.BUY, "2", "110"))
    r = await ex.send_order(req(Side.SELL, "5", "90", reduce_only=True))  # pide más de lo que hay
    assert (r.status, r.filled_size) == (OrderStatus.FILLED, D(2))
    assert await ex.positions() == {}
    same = await ex.send_order(req(Side.BUY, "1", "110", reduce_only=True, cli="otra"))
    assert same.status is OrderStatus.REJECTED  # sin posición, reduceOnly no abre nada


async def test_duplicate_client_order_id_is_rejected_and_not_executed() -> None:
    ex, _ = make()
    first = await ex.send_order(req(Side.BUY, "1", "200", cli="dup"))
    again = await ex.send_order(req(Side.BUY, "1", "200", cli="dup"))
    assert first.status is OrderStatus.FILLED
    assert (again.status, again.reason) == (OrderStatus.REJECTED, "clientOrderIdAlreadyExist")
    assert await ex.positions() == {SOL: D(1)}
    assert await ex.find_order("dup") == first
    assert await ex.find_order("nunca-enviada") is None


async def test_suspended_market_rejects() -> None:
    ex, m = make()
    t = m.ticker_map[SOL]
    m.ticker_map[SOL] = t.__class__(**{**t.__dict__, "suspended": True})
    r = await ex.send_order(req(Side.BUY, "1", "200"))
    assert (r.status, r.reason) == (OrderStatus.REJECTED, "marketSuspended")


async def test_without_book_simulation_uses_best_bid_ask() -> None:
    ex, m = make(PaperConfig(simulate_orderbook_slippage=False))
    m.set_mark(SOL, "114")
    r = await ex.send_order(req(Side.BUY, "1000", "115"))
    assert (r.filled_size, r.avg_price) == (D(1000), D(114))


# --- capital y funding ---


async def test_equity_eur_collateral_with_haircut_and_unrealized_pnl() -> None:
    ex, m = make(PaperConfig(initial_collateral_eur=D(500), eur_haircut_pct=D(10),
                             taker_fee_pct=D(0)))
    m.eurusd = D("1.20")
    assert await ex.equity_usd() == D(500) * D("1.20") * D("0.9")  # 540
    m.books[SOL] = OrderBook(SOL, bids=((D(99), D(100)),), asks=((D(100), D(100)),))
    await ex.send_order(req(Side.BUY, "2", "100"))
    m.set_mark(SOL, "110")
    assert await ex.equity_usd() == D(540) + D(20)


async def test_funding_follows_real_timestamps_and_sign() -> None:
    ex, m = make(PaperConfig(taker_fee_pct=D(0)))
    await ex.send_order(req(Side.BUY, "10", "200"))  # largo 10 SOL abierto en NOW
    m.funding[SOL] = [
        FundingRate(NOW - timedelta(hours=1), D("0.5")),  # anterior a la apertura: no aplica
        FundingRate(NOW + timedelta(hours=1), D("0.01")),  # positivo: el largo paga
        FundingRate(NOW + timedelta(hours=2), D("-0.02")),  # negativo: el largo cobra
        FundingRate(NOW + timedelta(hours=3), D("0.03")),  # aún no ha llegado
    ]
    balance0 = ex.account.usd_balance
    events = await ex.collect_funding(NOW + timedelta(hours=2, minutes=5))
    assert [e.amount_usd for e in events] == [D("-0.10"), D("0.20")]
    assert ex.account.usd_balance - balance0 == D("0.10")
    # Sin cobros dobles al volver a consultar
    assert await ex.collect_funding(NOW + timedelta(hours=2, minutes=30)) == []
    later = await ex.collect_funding(NOW + timedelta(hours=3))
    assert [e.amount_usd for e in later] == [D("-0.30")]


async def test_short_receives_positive_funding_and_disabled_funding() -> None:
    ex, m = make(PaperConfig(taker_fee_pct=D(0)))
    await ex.send_order(req(Side.SELL, "10", "1"))
    m.funding[SOL] = [FundingRate(NOW + timedelta(hours=1), D("0.01"))]
    events = await ex.collect_funding(NOW + timedelta(hours=1))
    assert [e.amount_usd for e in events] == [D("0.10")]
    ex2, m2 = make(PaperConfig(simulate_funding=False))
    await ex2.send_order(req(Side.BUY, "1", "200"))
    m2.funding[SOL] = m.funding[SOL]
    assert await ex2.collect_funding(NOW + timedelta(hours=1)) == []


async def test_account_roundtrip() -> None:
    ex, _ = make()
    await ex.send_order(req(Side.BUY, "2", "200", cli="a"))
    acc = PaperAccount.from_dict(ex.account.to_dict())
    assert acc.to_dict() == ex.account.to_dict()
    assert acc.orders["a"].status is OrderStatus.FILLED
