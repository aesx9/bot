from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from copybot.exchange.kraken_public import (
    INSTRUMENTS_PATH,
    KRAKEN_FUTURES_URL,
    KrakenDataError,
    fetch_instruments,
    parse_instruments,
)
from copybot.models import MarketSpec

FIXTURE = Path(__file__).parent.parent / "fixtures" / "kraken_instruments.json"


def payload() -> Any:
    # Recorte de la respuesta real de /instruments (2026-10-08)
    return json.loads(FIXTURE.read_text(), parse_float=D)


def spec(step: str) -> MarketSpec:
    return MarketSpec("PF_XUSD", D(step), D("0.01"), D(1000))


@pytest.mark.parametrize(
    ("step", "size", "down", "up"),
    [
        ("0.0001", D("0.12345"), D("0.1234"), D("0.1235")),
        ("1", D("7"), D("7"), D("7")),
        ("1E+3", D("1999"), D("1000"), D("2000")),
        ("1E+3", D("0"), D("0"), D("0")),
    ],
)
def test_market_spec_rounding(step: str, size: D, down: D, up: D) -> None:
    s = spec(step)
    assert s.round_down(size) == down
    assert s.round_up(size) == up
    assert s.min_size == D(step)


def test_market_spec_rejects_signed_sizes_and_bad_specs() -> None:
    with pytest.raises(ValueError):
        spec("0.1").round_down(D("-1"))
    with pytest.raises(ValueError):
        MarketSpec("PF_XUSD", D(0), D("0.01"), D(1))


def test_parse_real_instruments_fixture() -> None:
    specs = parse_instruments(payload())
    assert set(specs) == {"PF_XBTUSD", "PF_ETHUSD", "PF_PEPEUSD", "PF_SOLUSD", "PF_DOGEUSD"}
    xbt = specs["PF_XBTUSD"]
    assert (xbt.size_step, xbt.tick_size, xbt.max_position_size) == (D("0.0001"), D(1), D(1200))
    assert specs["PF_PEPEUSD"].size_step == D(1000)  # contractValueTradePrecision = -3
    assert specs["PF_PEPEUSD"].tick_size == D("1E-10")
    assert "PI_XBTUSD" not in specs  # solo perpetuos lineales PF_


def _with(symbol: str, **changes: Any) -> Any:
    data = payload()
    for ins in data["instruments"]:
        if ins["symbol"] == symbol:
            ins.update(changes)
    return data


@pytest.mark.parametrize(
    "changes",
    [
        {"tradeable": False},
        {"isExpired": True},
        {"contractSize": 10},
        {"contractValueTradePrecision": "x"},
        {"type": "futures_inverse"},
    ],
)
def test_non_tradeable_or_odd_markets_are_dropped(changes: dict[str, Any]) -> None:
    specs = parse_instruments(_with("PF_ETHUSD", **changes))
    assert "PF_ETHUSD" not in specs
    assert "PF_XBTUSD" in specs


@pytest.mark.parametrize(
    "bad",
    [
        None, [], {"result": "error"}, {"result": "success"},
        {"result": "success", "instruments": []},
    ],
)
def test_malformed_response_raises(bad: Any) -> None:
    with pytest.raises(KrakenDataError):
        parse_instruments(bad)


@respx.mock
async def test_fetch_instruments_parses_decimals_exactly() -> None:
    respx.get(KRAKEN_FUTURES_URL + INSTRUMENTS_PATH).mock(
        return_value=httpx.Response(200, text=FIXTURE.read_text())
    )
    async with httpx.AsyncClient() as http:
        specs = await fetch_instruments(http)
    assert specs["PF_PEPEUSD"].tick_size == D("1E-10")


@respx.mock
@pytest.mark.parametrize(
    "response",
    [httpx.Response(503), httpx.Response(200, text="<html>"), httpx.ConnectTimeout("t")],
)
async def test_fetch_instruments_errors(response: Any) -> None:
    route = respx.get(KRAKEN_FUTURES_URL + INSTRUMENTS_PATH)
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(KrakenDataError):
            await fetch_instruments(http)
