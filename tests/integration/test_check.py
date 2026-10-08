from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from copybot.checks import (
    WITHDRAW_CONFIRM_PHRASE,
    CheckReport,
    evaluate_key,
    live_check_valid,
    run_check,
)
from copybot.config import Config
from copybot.credentials import KrakenCredentials
from copybot.exchange.kraken_auth import KrakenPrivateClient
from copybot.sources.hyperliquid_rest import UnsupportedAccountMode
from copybot.state import BotState
from tests.conftest import LEADER
from tests.fake_kraken import CREDS, FakeKraken
from tests.fakes import FakeLeader, FakeMarket

LIVE = Config.model_validate({"leader_address": LEADER, "mode": "live"})


class Harness:
    def __init__(self, kraken: FakeKraken, http: httpx.AsyncClient) -> None:
        self.kraken, self.http = kraken, http
        self.state = BotState()
        self.leader = FakeLeader("100000", BTC="1")
        self.prompts: list[str] = []
        self.answer = ""

    async def run(self, cfg: Config = LIVE) -> CheckReport:
        def prompt(text: str) -> str:
            self.prompts.append(text)
            return self.answer

        return await run_check(cfg=cfg, creds=CREDS, client=KrakenPrivateClient(self.http, CREDS),
                               state=self.state, leader=self.leader, market=FakeMarket(),
                               prompt=prompt)


@pytest.fixture
async def h() -> AsyncIterator[Harness]:
    kraken = FakeKraken()
    with respx.mock(assert_all_called=False) as router:
        kraken.install(router)
        async with httpx.AsyncClient() as http:
            yield Harness(kraken, http)
    # --check nunca envía ni cancela órdenes
    assert kraken.sends() == [] and kraken.cancels() == []
    assert all(m == "GET" for m, _, _ in kraken.calls)


def failed(report: CheckReport) -> list[str]:
    return [i.name for i in report.items if i.fatal and not i.ok]


async def test_check_passes_and_records_config_and_key(h: Harness) -> None:
    report = await h.run()
    assert report.passed, report.text()
    assert h.state.live_check is not None
    assert live_check_valid(h.state, LIVE, CREDS) is None
    assert h.prompts == []


@pytest.mark.parametrize(
    ("perms", "reason"),
    [
        ({"general": "FULL_ACCESS", "transfer": "FULL_ACCESS"}, "transferencia/retiro"),
        ({"general": "FULL_ACCESS", "transfer": "READ_ONLY"}, "transferencia/retiro"),
        ({"general": "READ_ONLY", "transfer": "NO_ACCESS"}, "lectura y trading"),
    ],
)
async def test_withdrawal_or_missing_trading_permission_aborts(
    h: Harness, perms: dict[str, str], reason: str
) -> None:
    h.kraken.key_check["permissions"] = perms
    report = await h.run()
    assert not report.passed and "permisos de la clave" in failed(report)
    assert reason in report.text() and h.state.live_check is None


@pytest.mark.parametrize("perms", [{}, None, {"general": "FULL_ACCESS"},
                                   {"general": "FULL_ACCESS", "transfer": "SOMETHING_NEW"}])
async def test_unverifiable_permissions_without_ip_restriction_abort(
    h: Harness, perms: Any
) -> None:
    h.kraken.key_check["permissions"] = perms
    h.kraken.key_check["allowedCidrBlocks"] = []
    h.answer = WITHDRAW_CONFIRM_PHRASE  # ni con confirmación
    report = await h.run()
    assert not report.passed and h.prompts == []


async def test_unverifiable_with_ip_restriction_needs_written_confirmation(h: Harness) -> None:
    h.kraken.key_check["permissions"] = {}
    h.answer = "si"
    assert not (await h.run()).passed
    h.answer = WITHDRAW_CONFIRM_PHRASE
    report = await h.run()
    assert report.passed and "203.0.113.7/32" in h.prompts[-1]


async def test_no_ip_restriction_is_only_a_warning_when_verifiable(h: Harness) -> None:
    h.kraken.key_check["allowedCidrBlocks"] = []
    report = await h.run()
    assert report.passed and "AVISO" in report.text()


async def test_unsupported_leader_or_paper_config_fails(h: Harness) -> None:
    h.leader.fail = UnsupportedAccountMode("unifiedAccount")
    assert "líder" in failed(await h.run())
    h.leader.fail = None
    paper = Config.model_validate({"leader_address": LEADER})
    assert "modo" in failed(await h.run(paper))


async def test_bad_override_and_zero_equity_fail(h: Harness) -> None:
    cfg = Config.model_validate({"leader_address": LEADER, "mode": "live",
                                 "symbols": {"overrides": {"FOO": "PF_NOEXISTEUSD"}}})
    h.kraken.flex["marginEquity"] = 0
    report = await h.run(cfg)
    assert {"overrides de símbolos", "cuenta"} <= set(failed(report))


async def test_failed_check_invalidates_a_previous_pass(h: Harness) -> None:
    await h.run()
    h.kraken.key_check["permissions"]["transfer"] = "FULL_ACCESS"
    await h.run()
    assert h.state.live_check is None


def test_check_is_bound_to_config_and_key() -> None:
    state = BotState(live_check=None)
    assert live_check_valid(state, LIVE, CREDS) == "no hay un --check superado"
    from copybot.checks import config_hash, key_fingerprint

    state.live_check = {"config_hash": config_hash(LIVE), "key_fingerprint": key_fingerprint(CREDS)}
    assert live_check_valid(state, LIVE, CREDS) is None
    other_cfg = LIVE.model_copy(update={"leader_address": "0x" + "cd" * 20})
    assert "configuración" in (live_check_valid(state, other_cfg, CREDS) or "")
    other_key = KrakenCredentials(api_key=SecretStr("otra"), api_secret=CREDS.api_secret)
    assert "clave" in (live_check_valid(state, LIVE, other_key) or "")


def test_evaluate_key_reads_both_cidr_fields() -> None:
    verdict, _, cidrs = evaluate_key({"permissions": {"general": "FULL_ACCESS",
                                                      "transfer": "NO_ACCESS"},
                                      "allowedCidrBlock": "10.0.0.1/32",
                                      "allowedCidrBlocks": []})
    assert verdict == "ok" and cidrs == ["10.0.0.1/32"]
    assert evaluate_key("basura")[0] == "unverifiable"


# --- M5: la puerta del --check ---


async def test_failed_recheck_invalidates_the_previous_pass(h: Harness) -> None:
    """PoC M: un --check que falla a medias dejaba vigente el anterior."""
    assert (await h.run()).passed and live_check_valid(h.state, LIVE, CREDS) is None
    h.kraken.key_check["permissions"]["transfer"] = "FULL_ACCESS"  # cambian en Kraken
    h.kraken.fail_paths.add("/api/auth/v1/api-keys/v3/check")  # y el endpoint falla
    report = await h.run()
    assert not report.passed
    assert h.state.live_check is None
    assert live_check_valid(h.state, LIVE, CREDS) is not None


async def test_key_is_revalidated_before_every_live_start(h: Harness) -> None:
    from copybot.checks import key_problem
    from copybot.exchange.base import ExchangeError

    client = KrakenPrivateClient(h.http, CREDS)
    assert await key_problem(client) is None
    h.kraken.key_check["permissions"]["transfer"] = "READ_ONLY"
    assert "transferencia/retiro" in (await key_problem(client) or "")
    h.kraken.key_check["permissions"] = {"general": "FULL_ACCESS", "transfer": "NO_ACCESS"}
    h.kraken.key_check["allowedCidrBlocks"] = []
    assert await key_problem(client) is None  # verificable: la IP no es obligatoria
    h.kraken.key_check["permissions"] = {}
    assert "restricción de IP" in (await key_problem(client) or "")  # no verificable, sin IP
    h.kraken.fail_paths.add("/api/auth/v1/api-keys/v3/check")
    with pytest.raises(ExchangeError):
        await key_problem(client)  # no se pudo consultar: no se da por bueno
