"""Cliente REST de Hyperliquid contra una API simulada (respx), sin red."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from copybot.sources.hyperliquid_rest import (
    HL_INFO_URL,
    HyperliquidInfo,
    LeaderDataError,
    UnsupportedAccountMode,
    WeightBudget,
)

FIX = Path(__file__).parent.parent / "fixtures"
USER = "0x010461c14e146ac35fe42271bdc1134ee31c703a"


def fixture(name: str) -> Any:
    return json.loads((FIX / name).read_text())


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


class InfoApi:
    """Simula POST /info: una respuesta (o lista de respuestas) por tipo."""

    def __init__(self) -> None:
        self.responses: dict[str, list[Any]] = {
            "clearinghouseState": [fixture("hl_clearinghouse_state.json")],
            "allMids": [fixture("hl_all_mids.json")],
            "userAbstraction": ["default"],
        }
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        queue = self.responses[body["type"]]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c["type"] == kind)


@pytest.fixture
def api() -> InfoApi:
    return InfoApi()


@pytest.fixture
def t() -> FakeTime:
    return FakeTime()


@pytest.fixture
async def info(api: InfoApi, t: FakeTime) -> AsyncIterator[HyperliquidInfo]:
    with respx.mock(assert_all_called=False) as router:
        router.post(HL_INFO_URL).mock(side_effect=api)
        async with httpx.AsyncClient() as http:
            yield HyperliquidInfo(http, sleep=t.sleep, clock=t.clock)


async def test_snapshot_from_real_response(info: HyperliquidInfo, api: InfoApi) -> None:
    snap = await info.leader_snapshot(USER)
    assert snap.equity_usd == D("3189036.4886340001")  # exacto, sin pasar por float
    assert snap.positions == {
        "BTC": D("0.71107"), "ETH": D("13.423"), "SOL": D("334.31"), "kPEPE": D("-2642162.0"),
    }
    assert snap.mids["BTC"] == D("83079.5")
    assert not any(k.startswith(("@", "#")) for k in snap.mids)
    assert {"type": "clearinghouseState", "user": USER} in api.calls


async def test_timestamp_comes_from_server_time(info: HyperliquidInfo, api: InfoApi) -> None:
    chs = fixture("hl_clearinghouse_state.json")
    chs["time"] = 1791454045220
    api.responses["clearinghouseState"] = [chs]
    snap = await info.leader_snapshot(USER)
    assert snap.timestamp == datetime(2026, 10, 8, 10, 7, 25, 220000, tzinfo=UTC)


async def test_zero_size_positions_are_dropped(info: HyperliquidInfo, api: InfoApi) -> None:
    chs = fixture("hl_clearinghouse_state.json")
    chs["assetPositions"][0]["position"]["szi"] = "0.0"
    api.responses["clearinghouseState"] = [chs]
    assert "BTC" not in (await info.leader_snapshot(USER)).positions


@pytest.mark.parametrize("mode", ["unifiedAccount", "portfolioMargin", "dexAbstraction"])
async def test_unsupported_account_modes_are_refused(
    info: HyperliquidInfo, api: InfoApi, mode: str
) -> None:
    api.responses["userAbstraction"] = [mode]
    with pytest.raises(UnsupportedAccountMode):
        await info.leader_snapshot(USER)
    assert api.count("clearinghouseState") == 0


async def test_unknown_account_mode_is_refused(info: HyperliquidInfo, api: InfoApi) -> None:
    api.responses["userAbstraction"] = ["somethingNew"]
    with pytest.raises(LeaderDataError, match="desconocido"):
        await info.leader_snapshot(USER)


async def test_account_mode_is_cached(info: HyperliquidInfo, api: InfoApi, t: FakeTime) -> None:
    await info.leader_snapshot(USER)
    await info.leader_snapshot(USER)
    assert api.count("userAbstraction") == 1
    t.now += 301
    await info.leader_snapshot(USER)
    assert api.count("userAbstraction") == 2


async def test_rate_limit_and_server_errors_are_retried_with_backoff(
    info: HyperliquidInfo, api: InfoApi, t: FakeTime
) -> None:
    api.responses["allMids"] = [
        httpx.Response(429), httpx.Response(502), fixture("hl_all_mids.json"),
    ]
    mids = await info.all_mids()
    assert mids["ETH"] == D("2565.55")
    assert t.sleeps == [0.5, 1.0]


async def test_timeouts_are_retried_then_fail(info: HyperliquidInfo, api: InfoApi,
                                              t: FakeTime) -> None:
    api.responses["allMids"] = [httpx.ReadTimeout("lento")]
    with pytest.raises(LeaderDataError, match="ReadTimeout tras 4 intentos"):
        await info.all_mids()
    assert t.sleeps == [0.5, 1.0, 2.0]


async def test_client_errors_are_not_retried(info: HyperliquidInfo, api: InfoApi,
                                             t: FakeTime) -> None:
    api.responses["allMids"] = [httpx.Response(422, text="bad")]
    with pytest.raises(LeaderDataError, match="HTTP 422"):
        await info.all_mids()
    assert t.sleeps == []


async def test_disconnect_mid_request(info: HyperliquidInfo, api: InfoApi) -> None:
    api.responses["clearinghouseState"] = [httpx.RemoteProtocolError("cortado")]
    with pytest.raises(LeaderDataError):
        await info.leader_snapshot(USER)


def _broken(path: str, value: Any) -> Any:
    chs = fixture("hl_clearinghouse_state.json")
    target = chs
    *parents, last = path.split(".")
    for key in parents:
        target = target[int(key)] if key.isdigit() else target[key]
    if value is KeyError:
        del target[last]
    else:
        target[last] = value
    return chs


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("marginSummary.accountValue", KeyError),
        ("marginSummary.accountValue", "abc"),
        ("marginSummary.accountValue", "NaN"),
        ("assetPositions.0.position.szi", KeyError),
        ("assetPositions.0.position.szi", "Infinity"),
        ("assetPositions.0.position.coin", KeyError),
        ("assetPositions", None),
        ("time", KeyError),
    ],
)
async def test_malformed_clearinghouse_state(info: HyperliquidInfo, api: InfoApi,
                                             path: str, value: Any) -> None:
    api.responses["clearinghouseState"] = [_broken(path, value)]
    with pytest.raises(LeaderDataError):
        await info.leader_snapshot(USER)


async def test_unknown_position_type_and_duplicates(info: HyperliquidInfo, api: InfoApi) -> None:
    api.responses["clearinghouseState"] = [_broken("assetPositions.0.type", "hedge")]
    with pytest.raises(LeaderDataError, match="tipo de posición"):
        await info.leader_snapshot(USER)
    chs = fixture("hl_clearinghouse_state.json")
    chs["assetPositions"].append(chs["assetPositions"][0])
    api.responses["clearinghouseState"] = [chs]
    with pytest.raises(LeaderDataError, match="duplicada"):
        await info.leader_snapshot(USER)


@pytest.mark.parametrize(
    "mids", [[], {"BTC": "abc"}, {"BTC": "0"}, {"BTC": "-1"}, {"BTC": 1.5}, {"BTC": "NaN"}]
)
async def test_malformed_mids(info: HyperliquidInfo, api: InfoApi, mids: Any) -> None:
    api.responses["allMids"] = [mids]
    with pytest.raises(LeaderDataError):
        await info.all_mids()


async def test_invalid_json(info: HyperliquidInfo, api: InfoApi) -> None:
    api.responses["allMids"] = [httpx.Response(200, text="<html>mantenimiento</html>")]
    with pytest.raises(LeaderDataError, match="JSON"):
        await info.all_mids()


async def test_weight_budget_waits_when_exhausted() -> None:
    t = FakeTime()
    budget = WeightBudget(10, clock=t.clock, sleep=t.sleep)
    for _ in range(5):
        await budget.acquire(2)
    assert t.sleeps == []
    await budget.acquire(2)  # el sexto supera 10/min: espera a que caduque el primero
    assert t.sleeps == [60.0]


async def test_snapshot_respects_documented_weights(info: HyperliquidInfo, api: InfoApi,
                                                    t: FakeTime) -> None:
    # Pesos documentados: userAbstraction 20 (en caché), clearinghouseState 2, allMids 2.
    # 100 snapshots = 20 + 100 x 4 = 420 <= 600/min: sin esperas.
    for _ in range(100):
        await info.leader_snapshot(USER)
    assert t.sleeps == []
    assert api.count("userAbstraction") == 1
    # 50 más llevarían a 620 > 600: el cliente espera en vez de pasarse
    for _ in range(50):
        await info.leader_snapshot(USER)
    assert len(t.sleeps) >= 1
