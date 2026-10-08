from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from copybot.credentials import CredentialsError, load_kraken_credentials, load_telegram_credentials
from copybot.logging_setup import setup_logging
from copybot.redaction import REDACTED, redact

FAKE_KEY = "fake-key-for-tests-0001"
FAKE_SECRET = "ZmFrZS1zZWNyZXQtZm9yLXRlc3Rz"  # pragma: allowlist secret


def write_env(tmp_path: Path, mode: int = 0o600) -> Path:
    p = tmp_path / ".env"
    p.write_text(f"KRAKEN_FUTURES_API_KEY={FAKE_KEY}\nKRAKEN_FUTURES_API_SECRET={FAKE_SECRET}\n")
    p.chmod(mode)
    return p


def test_missing_env_file(tmp_path: Path) -> None:
    with pytest.raises(CredentialsError, match="no existe"):
        load_kraken_credentials(tmp_path / ".env")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_env_with_loose_permissions_is_rejected(tmp_path: Path, mode: int) -> None:
    with pytest.raises(CredentialsError, match="chmod 600") as exc:
        load_kraken_credentials(write_env(tmp_path, mode))
    assert FAKE_SECRET not in str(exc.value)


def test_loads_hidden_and_not_into_environ(tmp_path: Path) -> None:
    creds = load_kraken_credentials(write_env(tmp_path))
    assert creds.api_secret.get_secret_value() == FAKE_SECRET
    for text in (repr(creds), str(creds), str(creds.model_dump())):
        assert FAKE_SECRET not in text and FAKE_KEY not in text
    assert "KRAKEN_FUTURES_API_SECRET" not in os.environ


def test_missing_variable(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("KRAKEN_FUTURES_API_KEY=abcdefgh\n")
    p.chmod(0o600)
    with pytest.raises(CredentialsError, match="KRAKEN_FUTURES_API_SECRET"):
        load_kraken_credentials(p)
    with pytest.raises(CredentialsError, match="TELEGRAM_BOT_TOKEN"):
        load_telegram_credentials(p)


def test_loaded_secrets_are_redacted_everywhere_in_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    load_kraken_credentials(write_env(tmp_path))
    setup_logging(tmp_path / "logs")
    log = logging.getLogger("test")
    log.info("clave %s", FAKE_KEY)
    log.warning(f"secreto {FAKE_SECRET}")
    try:
        raise RuntimeError(f"fallo con {FAKE_SECRET}")
    except RuntimeError:
        log.exception("error")
    for h in logging.getLogger().handlers:
        h.flush()
    written = (tmp_path / "logs" / "copybot.log").read_text() + capsys.readouterr().err
    assert FAKE_SECRET not in written
    assert FAKE_KEY not in written
    assert written.count(REDACTED) >= 4  # mensaje, mensaje, excepción, traza


def test_log_timestamps_are_utc(tmp_path: Path) -> None:
    setup_logging(tmp_path)
    logging.getLogger("t").info("hola")
    for h in logging.getLogger().handlers:
        h.flush()
    line = (tmp_path / "copybot.log").read_text().splitlines()[0]
    assert line[23] == "Z"


@pytest.mark.parametrize(
    "text",
    [
        "headers={'APIKey': 'abcdef123456', 'Authent': 'c2lnbmF0dXJl'}",  # pragma: allowlist secret
        'APIKey: abcdef123456',
        "api_secret=abcdef123456",
        "https://api.telegram.org/bot123456:AAAbbbCCCdddEEEfffGGGhhh/sendMessage",
    ],
)
def test_generic_patterns_are_redacted(text: str) -> None:
    out = redact(text)
    assert "abcdef123456" not in out  # pragma: allowlist secret
    assert "c2lnbmF0dXJl" not in out
    assert "AAAbbbCCCdddEEEfffGGGhhh" not in out


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abcdef1234567890SECRET",
        'headers={"Authorization": "Bearer abcdef1234567890SECRET"}',
        "authorization=Basic abcdef1234567890SECRET",
        "Authorization: Token abcdef1234567890SECRET",
        "Authorization: abcdef1234567890SECRET",
    ],
)
def test_authorization_schemes_hide_the_whole_token(text: str) -> None:
    """B2: solo se enmascaraba la palabra Bearer y el token quedaba en claro."""
    out = redact(text)
    assert "abcdef1234567890SECRET" not in out and REDACTED in out
