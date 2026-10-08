"""Carga de credenciales desde .env.

Reglas:
- Solo se leen del fichero .env (python-dotenv), sin volcarlas a os.environ,
  para que procesos hijo no las hereden.
- El fichero debe tener permisos 600 (ni grupo ni otros); si no, se rechaza.
- Los valores se guardan como SecretStr: repr/str muestran '**********'.
- Cada secreto cargado se registra en el filtro de logs para redactarlo.
- En modo paper las claves de Kraken NO se cargan nunca.
"""

from __future__ import annotations

import stat
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, SecretStr

from copybot.redaction import register_secret


class CredentialsError(Exception):
    """Problema con el fichero .env o con las credenciales (sin incluir valores)."""


class KrakenCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    api_key: SecretStr
    api_secret: SecretStr


class TelegramCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    bot_token: SecretStr
    chat_id: SecretStr


def _read_env(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        raise CredentialsError(f"no existe {env_path}")
    mode = stat.S_IMODE(env_path.stat().st_mode)
    if mode & 0o077:
        raise CredentialsError(
            f"{env_path} tiene permisos {oct(mode)}; ejecuta: chmod 600 {env_path}"
        )
    values = dotenv_values(env_path)
    return {k: v for k, v in values.items() if v}


def _secret(values: dict[str, str], name: str) -> SecretStr:
    value = values.get(name, "").strip()
    if not value:
        raise CredentialsError(f"falta {name} en .env")
    register_secret(value)
    return SecretStr(value)


def load_kraken_credentials(env_path: Path) -> KrakenCredentials:
    values = _read_env(env_path)
    return KrakenCredentials(
        api_key=_secret(values, "KRAKEN_FUTURES_API_KEY"),
        api_secret=_secret(values, "KRAKEN_FUTURES_API_SECRET"),
    )


def load_telegram_credentials(env_path: Path) -> TelegramCredentials:
    values = _read_env(env_path)
    return TelegramCredentials(
        bot_token=_secret(values, "TELEGRAM_BOT_TOKEN"),
        chat_id=_secret(values, "TELEGRAM_CHAT_ID"),
    )


def load_healthcheck_url(env_path: Path) -> SecretStr:
    values = _read_env(env_path)
    url = _secret(values, "HEALTHCHECK_URL")
    if not url.get_secret_value().startswith("https://"):
        raise CredentialsError("HEALTHCHECK_URL debe empezar por https://")
    return url
