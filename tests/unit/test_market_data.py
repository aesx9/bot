from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from copybot.exchange.kraken_public import (
    FUNDING_PATH,
    INSTRUMENTS_PATH,
    KRAKEN_FUTURES_URL,
    ORDERBOOK_PATH,
    TICKERS_PATH,
    KrakenDataError,
    KrakenMarketData,
    parse_funding_rates,
    parse_orderbook,
    parse_tickers,
)
from tests.fakes import load

FIX = Path(__file__).parent.parent / "fixtures"


def test_parse_real_tickers() -> None:
    t = parse_tickers(load("kraken_tickers.json"))
    assert "PI_XBTUSD" not in t
    xbt = t["PF_XBTUSD"]
    assert xbt.mark_price == D("82588.89051844827") and not xbt.suspended
    assert xbt.funding_rate == D("0.977731259677764")
    assert t["PF_EURUSD"].index_price == D("1.118")


def test_malformed_ticker_is_dropped_not_fatal() -> None:
    data = load("kraken_tickers.json")
    data["tickers"][0]["markPrice"] = "abc"
    sym = data["tickers"][0]["symbol"]
    assert sym not in parse_tickers(data)


def test_orderbook_is_sorted_best_first() -> None:
    raw = load("kraken_orderbook_solusd.json")
    assert raw["orderBook"]["bids"][0][0] < raw["orderBook"]["bids"][-1][0]  # así viene
    ob = parse_orderbook(raw, "PF_SOLUSD")
    assert [p for p, _ in ob.bids] == sorted((p for p, _ in ob.bids), reverse=True)
    assert [p for p, _ in ob.asks] == sorted(p for p, _ in ob.asks)
    assert ob.bids[0][0] < ob.asks[0][0]


@pytest.mark.parametrize(
    "book",
    [
        {"bids": [[101, 1]], "asks": [[100, 1]]},  # cruzado
        {"bids": [["x", 1]], "asks": []},
        {"bids": [[100]], "asks": []},
        {"bids": [[-1, 1]], "asks": []},
        {"asks": []},
    ],
)
def test_bad_orderbooks(book: dict[str, Any]) -> None:
    with pytest.raises(KrakenDataError):
        parse_orderbook({"result": "success", "orderBook": book}, "PF_X")


def test_funding_rates_parse_sorted_utc() -> None:
    data = load("kraken_funding_xbtusd.json")
    data["rates"].reverse()
    rates = parse_funding_rates(data, "PF_XBTUSD")
    assert rates[-1].timestamp == datetime(2026, 10, 8, 10, tzinfo=UTC)
    assert rates[-1].rate == D("0.977731259677764")
    assert all(b.timestamp > a.timestamp for a, b in zip(rates, rates[1:], strict=False))
    deltas = {(b.timestamp - a.timestamp).total_seconds() for a, b in zip(rates, rates[1:],
                                                                          strict=False)}
    assert deltas == {3600.0}  # intervalo real: horario


@pytest.mark.parametrize("bad", [{"result": "error"}, {"result": "success"},
                                 {"result": "success", "rates": [{"timestamp": "x"}]}])
def test_bad_funding(bad: Any) -> None:
    with pytest.raises(KrakenDataError):
        parse_funding_rates(bad, "PF_X")


class Clock:
    t = 0.0

    def __call__(self) -> float:
        return self.t


@respx.mock
async def test_market_data_client_caches_and_reads_eurusd() -> None:
    respx.get(KRAKEN_FUTURES_URL + TICKERS_PATH).mock(
        return_value=httpx.Response(200, text=(FIX / "kraken_tickers.json").read_text()))
    inst = respx.get(KRAKEN_FUTURES_URL + INSTRUMENTS_PATH).mock(
        return_value=httpx.Response(200, text=(FIX / "kraken_instruments.json").read_text()))
    fund = respx.get(KRAKEN_FUTURES_URL + FUNDING_PATH, params={"symbol": "PF_XBTUSD"}).mock(
        return_value=httpx.Response(200, text=(FIX / "kraken_funding_xbtusd.json").read_text()))
    respx.get(KRAKEN_FUTURES_URL + ORDERBOOK_PATH, params={"symbol": "PF_SOLUSD"}).mock(
        return_value=httpx.Response(
            200, text=(FIX / "kraken_orderbook_solusd.json").read_text()))
    clock = Clock()
    async with httpx.AsyncClient() as http:
        md = KrakenMarketData(http, clock=clock)
        assert await md.eur_usd() == D("1.118")
        await md.instruments()
        await md.instruments()
        assert inst.call_count == 1
        clock.t += 3601
        await md.instruments()
        assert inst.call_count == 2
        await md.funding_rates("PF_XBTUSD")
        await md.funding_rates("PF_XBTUSD")
        assert fund.call_count == 1
        assert (await md.orderbook("PF_SOLUSD")).asks[0][0] == D("114.29")


@respx.mock
async def test_missing_eurusd_or_http_errors() -> None:
    data = json.loads((FIX / "kraken_tickers.json").read_text())
    data["tickers"] = [t for t in data["tickers"] if t["symbol"] != "PF_EURUSD"]
    route = respx.get(KRAKEN_FUTURES_URL + TICKERS_PATH)
    route.mock(return_value=httpx.Response(200, json=data))
    async with httpx.AsyncClient() as http:
        md = KrakenMarketData(http)
        with pytest.raises(KrakenDataError, match="EUR/USD"):
            await md.eur_usd()
        route.mock(side_effect=httpx.ReadTimeout("lento"))
        with pytest.raises(KrakenDataError):
            await md.tickers()
