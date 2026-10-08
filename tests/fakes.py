"""Dobles de prueba: mercado Kraken y líder simulados, sin red."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from copybot.exchange.kraken_public import (
    FundingRate,
    KrakenDataError,
    OrderBook,
    Ticker,
    parse_instruments,
    parse_tickers,
)
from copybot.models import LeaderSnapshot, MarketSpec

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text(), parse_float=Decimal)


def deep_book(mark: Decimal, *, levels: int = 20, qty: Decimal = Decimal(10**6),
              step_pct: Decimal = Decimal("0.01")) -> OrderBook:
    """Libro sintético simétrico alrededor de `mark` con mucha liquidez."""
    step = mark * step_pct / 100
    return OrderBook(
        "X",
        bids=tuple((mark - step * (i + 1), qty) for i in range(levels)),
        asks=tuple((mark + step * (i + 1), qty) for i in range(levels)),
    )


class FakeMarket:
    """Instrumentos y tickers reales (fixtures); libros y funding configurables."""

    def __init__(self) -> None:
        self.specs: dict[str, MarketSpec] = parse_instruments(load("kraken_instruments.json"))
        self.ticker_map: dict[str, Ticker] = parse_tickers(load("kraken_tickers.json"))
        self.books: dict[str, OrderBook] = {}
        self.funding: dict[str, list[FundingRate]] = {}
        self.eurusd = Decimal("1.17")
        self.fail: Exception | None = None

    def set_mark(self, symbol: str, price: Decimal | str) -> None:
        t = self.ticker_map[symbol]
        p = Decimal(price)
        self.ticker_map[symbol] = Ticker(symbol, p, t.index_price, p, p, t.suspended,
                                         t.funding_rate)
        self.books.pop(symbol, None)

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def instruments(self) -> dict[str, MarketSpec]:
        self._check()
        return self.specs

    async def tickers(self) -> dict[str, Ticker]:
        self._check()
        return dict(self.ticker_map)

    async def orderbook(self, symbol: str) -> OrderBook:
        self._check()
        if symbol in self.books:
            return self.books[symbol]
        t = self.ticker_map.get(symbol)
        if t is None:
            raise KrakenDataError(f"sin libro para {symbol}")
        return deep_book(t.mark_price)

    async def funding_rates(self, symbol: str) -> list[FundingRate]:
        self._check()
        return self.funding.get(symbol, [])

    async def eur_usd(self) -> Decimal:
        self._check()
        return self.eurusd


class FakeLeader:
    def __init__(self, equity: str = "100000", **positions: str) -> None:
        self.equity = Decimal(equity)
        self.positions = {c: Decimal(s) for c, s in positions.items()}
        self.mids: dict[str, Decimal] = {"BTC": Decimal(82500), "ETH": Decimal(2560),
                                         "SOL": Decimal(114), "DOGE": Decimal("0.088")}
        self.timestamp: datetime | None = None  # None = "ahora" del reloj del test
        self.clock = lambda: NOW
        self.fail: Exception | None = None
        self.calls = 0

    async def leader_snapshot(self, user: str) -> LeaderSnapshot:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return LeaderSnapshot(
            equity_usd=self.equity, positions=dict(self.positions), mids=dict(self.mids),
            timestamp=self.timestamp or self.clock(),
        )
