"""M8: healthcheck externo y aviso de fallo desde fuera del bot."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from copybot.credentials import CredentialsError, load_healthcheck_url
from copybot.healthcheck import HttpHealthcheck
from copybot.notify import main as notify_main
from copybot.notify import notify

URL = "https://hc-ping.com/aaaa1111-bbbb-2222-cccc-3333dddd4444"  # pragma: allowlist secret
TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKK12"  # pragma: allowlist secret


def write_env(tmp: Path, body: str, mode: int = 0o600) -> Path:
    env = tmp / ".env"
    env.write_text(body)
    env.chmod(mode)
    return env


async def test_ok_and_fail_pings_never_leak_the_url(caplog: pytest.LogCaptureFixture) -> None:
    from copybot.logging_setup import RedactingFormatter
    from copybot.redaction import register_secret

    register_secret(URL)
    with respx.mock(assert_all_called=False) as router:
        ok = router.post(URL).respond(200)
        fail = router.post(URL + "/fail").respond(200)
        async with httpx.AsyncClient() as http:
            hc = HttpHealthcheck(SecretStr(URL), http)
            await hc.ok()
            await hc.fail("bot detenido: drawdown")
    assert ok.called and fail.called
    assert b"drawdown" in fail.calls[0].request.content

    with respx.mock(assert_all_called=False) as router:  # un fallo no lanza ni deja la URL
        router.post(URL).mock(side_effect=httpx.ConnectError("sin red"))
        async with httpx.AsyncClient() as http:
            with caplog.at_level(logging.WARNING):
                await HttpHealthcheck(SecretStr(URL), http).ok()
    fmt = RedactingFormatter("%(message)s")
    text = "\n".join(fmt.format(r) for r in caplog.records)
    assert "ConnectError" in text and "aaaa1111" not in text


def test_url_comes_from_env_with_private_permissions(tmp_path: Path) -> None:
    assert load_healthcheck_url(write_env(tmp_path, f"HEALTHCHECK_URL={URL}\n")
                                ).get_secret_value() == URL
    with pytest.raises(CredentialsError, match="https"):
        load_healthcheck_url(write_env(tmp_path, "HEALTHCHECK_URL=http://x.example/ping\n"))
    with pytest.raises(CredentialsError, match="chmod 600"):
        load_healthcheck_url(write_env(tmp_path, f"HEALTHCHECK_URL={URL}\n", 0o644))
    with pytest.raises(CredentialsError, match="HEALTHCHECK_URL"):
        load_healthcheck_url(write_env(tmp_path, "OTRA=1\n"))


def test_notify_reaches_telegram_and_healthcheck_from_outside_the_bot(tmp_path: Path) -> None:
    env = write_env(tmp_path, f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=987654\n"
                              f"HEALTHCHECK_URL={URL}\n")
    with respx.mock(assert_all_called=False) as router:
        tg = router.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").respond(200)
        hc = router.post(URL + "/fail").respond(200)
        sent = notify(env, "el servicio copybot ha fallado")
    assert sent == ["telegram", "healthcheck"]
    assert b"ha fallado" in tg.calls[0].request.content and hc.called


def test_notify_never_fails_and_never_prints_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = write_env(tmp_path, f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=987654\n"
                              f"HEALTHCHECK_URL={URL}\n")
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith="https://api.telegram.org/").mock(
            side_effect=httpx.ConnectError("sin red"))
        router.post(URL + "/fail").respond(500)
        assert notify_main(["--env", str(env), "--message", "fallo"]) == 0
    out = capsys.readouterr()
    assert "ningún canal" in out.out
    assert TOKEN not in out.out + out.err and "aaaa1111" not in out.out + out.err
    # sin .env tampoco falla
    assert notify_main(["--env", str(tmp_path / "no-existe"), "--message", "x"]) == 0
