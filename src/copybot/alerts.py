"""Alertas: siempre al log; opcionalmente a Telegram.

Un fallo al enviar una alerta nunca detiene el bot. Las alertas repetidas
(mismo texto) se agrupan durante `dedupe_seconds` para no inundar el móvil
cuando un control salta ciclo tras ciclo.
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
        if now - self._last.get(text, -1e18) < self._dedupe:
            return
        self._last[text] = now
        url = f"https://api.telegram.org/bot{self._creds.bot_token.get_secret_value()}/sendMessage"
        try:
            r = await self._http.post(url, json={
                "chat_id": self._creds.chat_id.get_secret_value(),
                "text": redact(f"[copybot] {level.value}: {text}"),
            }, timeout=10)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("no se pudo enviar la alerta a Telegram (%s)", type(exc).__name__)
