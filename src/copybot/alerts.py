"""Alertas: siempre al log; opcionalmente a Telegram.

Un fallo al enviar una alerta nunca detiene el bot. Las alertas repetidas
(mismo nivel y texto) se agrupan durante `dedupe_seconds` para no inundar el móvil
cuando un control salta ciclo tras ciclo; las CRÍTICAS solo durante un minuto.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

import httpx

from copybot.credentials import TelegramCredentials
from copybot.redaction import redact

log = logging.getLogger("copybot.alerts")

CRITICAL_DEDUPE_SECONDS = 60


class Level(StrEnum):
    INFO = "info"
    WARNING = "aviso"
    CRITICAL = "CRÍTICO"


class Alerter(Protocol):
    async def alert(self, level: Level, text: str) -> None: ...


class LogAlerter:
    def __init__(self) -> None:
        self.sent: list[tuple[Level, str]] = []

    async def alert(self, level: Level, text: str) -> None:
        self.sent.append((level, text))
        py_level = {Level.INFO: logging.INFO, Level.WARNING: logging.WARNING,
                    Level.CRITICAL: logging.ERROR}[level]
        log.log(py_level, "ALERTA %s: %s", level.value, text)


class TelegramAlerter:
    def __init__(
        self,
        creds: TelegramCredentials,
        http: httpx.AsyncClient,
        *,
        dedupe_seconds: float = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._creds = creds
        self._http = http
        self._dedupe = dedupe_seconds
        self._clock = clock
        self._last: dict[str, float] = {}
        self._log = LogAlerter()

    async def alert(self, level: Level, text: str) -> None:
        await self._log.alert(level, text)
        now = self._clock()
        # Las críticas (parada, posiciones abiertas, cierre fallido) no se silencian 10
        # minutos: solo se agrupan ráfagas de menos de CRITICAL_DEDUPE_SECONDS
        window = (min(self._dedupe, CRITICAL_DEDUPE_SECONDS) if level is Level.CRITICAL
                  else self._dedupe)
        key = f"{level.value}:{text}"
        if now - self._last.get(key, -1e18) < window:
            return
        self._last[key] = now
        url = f"https://api.telegram.org/bot{self._creds.bot_token.get_secret_value()}/sendMessage"
        try:
            r = await self._http.post(url, json={
                "chat_id": self._creds.chat_id.get_secret_value(),
                "text": redact(f"[copybot] {level.value}: {text}"),
            }, timeout=10)
            r.raise_for_status()
        except Exception as exc:  # httpx.InvalidURL, por ejemplo, no es un HTTPError
            log.warning("no se pudo enviar la alerta a Telegram (%s)", type(exc).__name__)
