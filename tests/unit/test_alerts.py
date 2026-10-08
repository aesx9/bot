"""Alertas: redacción de secretos y agrupación de repetidas (Telegram)."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx
from pydantic import SecretStr

from copybot.alerts import Level, TelegramAlerter
from copybot.credentials import TelegramCredentials
from copybot.logging_setup import setup_logging
from copybot.redaction import register_secret

TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKK12"  # pragma: allowlist secret
SEND = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
CREDS = TelegramCredentials(bot_token=SecretStr(TOKEN), chat_id=SecretStr("987654"))


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_telegram_payload_never_carries_a_registered_secret() -> None:
    secret = "clave-super-secreta-123"  # pragma: allowlist secret
    register_secret(secret)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(SEND).respond(200)
        async with httpx.AsyncClient() as http:
            await TelegramAlerter(CREDS, http).alert(Level.CRITICAL, f"fallo con {secret}")
    assert route.called and secret.encode() not in route.calls[0].request.content


async def test_repeated_alerts_are_grouped_for_ten_minutes() -> None:
    clock = Clock()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(SEND).respond(200)
        async with httpx.AsyncClient() as http:
            alerter = TelegramAlerter(CREDS, http, clock=clock)
            await alerter.alert(Level.WARNING, "ciclo con error: X")
            await alerter.alert(Level.WARNING, "ciclo con error: X")
            await alerter.alert(Level.WARNING, "ciclo con error: otro texto")
            assert route.call_count == 2
            clock.t += 601
            await alerter.alert(Level.WARNING, "ciclo con error: X")
            assert route.call_count == 3


def test_noisy_http_loggers_are_silenced_because_their_urls_carry_the_token(tmp_path) -> None:  # type: ignore[no-untyped-def]
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    try:
        setup_logging(tmp_path)
        for name in ("httpx", "httpcore", "websockets"):
            assert logging.getLogger(name).level == logging.WARNING
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in saved[0]:
            root.addHandler(h)
        root.setLevel(saved[1])


@pytest.mark.parametrize("token_in_url", [True])
async def test_a_failing_send_never_logs_the_token(
    token_in_url: bool, caplog: pytest.LogCaptureFixture
) -> None:
    register_secret(TOKEN)
    with respx.mock(assert_all_called=False) as router:
        router.post(SEND).respond(401)
        async with httpx.AsyncClient() as http:
            with caplog.at_level(logging.WARNING):
                await TelegramAlerter(CREDS, http).alert(Level.INFO, "hola")
    assert "AAAbbbCCC" not in "\n".join(r.getMessage() for r in caplog.records)


async def test_critical_alerts_are_not_silenced_for_ten_minutes() -> None:
    """B6: una crítica repetida (p. ej. el cierre sigue sin completarse) se volvía a avisar
    solo pasados 10 minutos, igual que un aviso trivial."""
    clock = Clock()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(SEND).respond(200)
        async with httpx.AsyncClient() as http:
            alerter = TelegramAlerter(CREDS, http, clock=clock)
            await alerter.alert(Level.CRITICAL, "siguen abiertas ['PF_XBTUSD']")
            await alerter.alert(Level.CRITICAL, "siguen abiertas ['PF_XBTUSD']")  # ráfaga
            assert route.call_count == 1
            clock.t += 61  # un minuto después, no diez
            await alerter.alert(Level.CRITICAL, "siguen abiertas ['PF_XBTUSD']")
            assert route.call_count == 2
            # el mismo texto con otro nivel es otra alerta
            await alerter.alert(Level.WARNING, "siguen abiertas ['PF_XBTUSD']")
            assert route.call_count == 3


async def test_any_failure_sending_is_swallowed_including_invalid_urls() -> None:
    class BrokenHttp:
        async def post(self, *a: object, **kw: object) -> httpx.Response:
            raise httpx.InvalidURL("URL inválida")

    await TelegramAlerter(CREDS, BrokenHttp()).alert(Level.CRITICAL, "x")  # type: ignore[arg-type]
