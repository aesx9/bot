"""Healthcheck externo opcional (tipo healthchecks.io): un "dead man's switch".

El bot es su propio único canal de alerta: si el proceso muere, se cuelga o la VPS cae,
Telegram no recibe nada. Con esto el aviso lo da el servicio externo cuando DEJA de
llegar el ping periódico (o cuando el bot avisa de un fallo explícito en /fail).

La URL va en .env (HEALTHCHECK_URL): contiene un identificador secreto, se registra
para la redacción de logs y nunca se escribe en un mensaje.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx
from pydantic import SecretStr

from copybot.redaction import redact

log = logging.getLogger(__name__)


class Healthcheck(Protocol):
    async def ok(self) -> None: ...

    async def fail(self, reason: str = "") -> None: ...


class HttpHealthcheck:
    def __init__(self, url: SecretStr, http: httpx.AsyncClient, *, timeout: float = 10) -> None:
        self._url = url
        self._http = http
        self._timeout = timeout

    async def ok(self) -> None:
        await self._ping("", "")

    async def fail(self, reason: str = "") -> None:
        await self._ping("/fail", reason)

    async def _ping(self, suffix: str, body: str) -> None:
        url = self._url.get_secret_value().rstrip("/") + suffix
        try:
            response = await self._http.post(url, content=redact(body)[:500].encode("utf-8"),
                                             timeout=self._timeout)
            response.raise_for_status()
        except Exception as exc:  # un fallo del aviso nunca detiene el bot (ni lleva la URL)
            log.warning("healthcheck: no se pudo avisar (%s)", type(exc).__name__)
