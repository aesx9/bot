from __future__ import annotations

import base64
import hashlib
import hmac
import re
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
import respx
from pydantic import SecretStr

from copybot.credentials import KrakenCredentials
from copybot.exchange.base import ExchangeError
from copybot.exchange.kraken_auth import (
    ALLOWED_ENDPOINTS,
    KRAKEN_FUTURES_URL,
    EndpointNotAllowed,
    KrakenApiError,
    KrakenPrivateClient,
    endpoint_path,
    sign,
)

TEST_SECRET = base64.b64encode(b"secreto-de-prueba-no-real").decode()
CREDS = KrakenCredentials(api_key=SecretStr("clave-publica-prueba"),
                          api_secret=SecretStr(TEST_SECRET))


def reference_sign(secret: str, post_data: str, nonce: str, path: str) -> str:
    """Implementación independiente, paso a paso según la documentación."""
    step1 = post_data + nonce + path
    step2 = hashlib.sha256(step1.encode()).digest()
    step3 = base64.b64decode(secret)
    step4 = hmac.new(step3, step2, hashlib.sha512).digest()
    return base64.b64encode(step4).decode()


def test_sign_matches_documented_algorithm() -> None:
    # (El secreto de ejemplo de la documentación no es base64 válido: se usa uno propio)
    args = ("orderType=ioc&symbol=PF_XBTUSD&side=buy&size=0.001&limitPrice=60000",
            "1415957147987", "/api/v3/sendorder")
    assert sign(TEST_SECRET, *args) == reference_sign(TEST_SECRET, *args)
    # Vector fijo de regresión: cualquier cambio en la firma lo rompe
    assert sign(TEST_SECRET, "symbol=fi_xbtusd_180615", "1415957147987",
                "/api/v3/orderbook") == (
        "+RAgmcuHRaBs9fn6NEETlLwgIoNkRQlBW1yCpCnyyEYkPeXFMfSgeDf2T+NFA73wgmoS3iXfx+Vq4dJrmDGQxQ=="  # noqa: E501  # pragma: allowlist secret
    )


def test_endpoint_path_strips_only_derivatives_prefix() -> None:
    assert endpoint_path("/derivatives/api/v3/sendorder") == "/api/v3/sendorder"
    assert endpoint_path("/api/history/v3/account-log") == "/api/history/v3/account-log"
    assert endpoint_path("/api/auth/v1/api-keys/v3/check") == "/api/auth/v1/api-keys/v3/check"


def test_no_endpoint_can_move_funds() -> None:
    for _, path in ALLOWED_ENDPOINTS:
        assert not re.search(r"withdraw|transfer|wallet|subaccount", path, re.I), path


def test_source_has_no_withdrawal_or_transfer_calls() -> None:
    src = Path(__file__).resolve().parents[2] / "src"
    pattern = re.compile(r"""["'](/[^"']*(withdraw|transfer)[^"']*)["']""", re.I)
    hits = [(f.name, m.group(1)) for f in src.rglob("*.py")
            for m in pattern.finditer(f.read_text())]
    assert hits == []


def _verify(request: httpx.Request, signed_data: str) -> None:
    nonce = request.headers["Nonce"]
    expected = reference_sign(TEST_SECRET, signed_data, nonce, endpoint_path(request.url.path))
    assert request.headers["APIKey"] == "clave-publica-prueba"  # pragma: allowlist secret
    assert request.headers["Authent"] == expected


@respx.mock
async def test_get_signs_the_query_and_post_signs_the_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": "success", "fills": []})

    respx.get(KRAKEN_FUTURES_URL + "/api/history/v3/account-log").mock(side_effect=handler)
    respx.post(KRAKEN_FUTURES_URL + "/derivatives/api/v3/sendorder").mock(side_effect=handler)
    async with httpx.AsyncClient() as http:
        c = KrakenPrivateClient(http, CREDS)
        await c.request("GET", "/api/history/v3/account-log",
                        [("since", "1"), ("info", "funding rate change")])
        await c.request("POST", "/derivatives/api/v3/sendorder",
                        [("orderType", "ioc"), ("reduceOnly", "true")])
    get, post = seen
    _verify(get, get.url.query.decode())
    assert dict(parse_qsl(get.url.query.decode())) == {"since": "1",
                                                       "info": "funding rate change"}
    _verify(post, post.content.decode())
    assert post.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert int(post.headers["Nonce"]) > int(get.headers["Nonce"])


async def test_endpoints_outside_whitelist_are_never_called() -> None:
    async with httpx.AsyncClient() as http:
        c = KrakenPrivateClient(http, CREDS)
        with respx.mock(assert_all_called=False) as router:
            route = router.route()
            for method, path in [("POST", "/derivatives/api/v3/withdrawal"),
                                 ("POST", "/derivatives/api/v3/transfer"),
                                 ("GET", "/derivatives/api/v3/sendorder")]:
                with pytest.raises(EndpointNotAllowed):
                    await c.request(method, path)
            assert not route.called


@respx.mock
@pytest.mark.parametrize(
    ("response", "exc"),
    [
        (httpx.Response(200, json={"result": "error", "error": "apiLimitExceeded"}),
         KrakenApiError),
        (httpx.Response(401, json={"error": "authenticationError"}), KrakenApiError),
        (httpx.Response(502, text="<html>"), ExchangeError),
        (httpx.ConnectTimeout("t"), ExchangeError),
    ],
)
async def test_errors_are_exchange_errors(response: object, exc: type[Exception]) -> None:
    route = respx.get(KRAKEN_FUTURES_URL + "/derivatives/api/v3/accounts")
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(exc):
            await KrakenPrivateClient(http, CREDS).request("GET", "/derivatives/api/v3/accounts")


@respx.mock
async def test_secret_never_appears_in_errors() -> None:
    respx.get(KRAKEN_FUTURES_URL + "/derivatives/api/v3/accounts").mock(
        return_value=httpx.Response(200, json={"result": "error", "error": "authenticationError"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(KrakenApiError) as info:
            await KrakenPrivateClient(http, CREDS).request("GET", "/derivatives/api/v3/accounts")
    assert TEST_SECRET not in str(info.value) and "clave-publica" not in str(info.value)
