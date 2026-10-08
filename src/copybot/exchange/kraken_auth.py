"""Cliente privado de Kraken Futures: firma y lista blanca de endpoints.

Firma (docs.kraken.com/exchange/guides/futures/rest, contrastada con el SDK
oficial krakenfx/api-go):
    Authent = base64(HMAC-SHA512(base64decode(secret),
                                 SHA256(postData + nonce + endpointPath)))
- postData: el cuerpo form-urlencoded (POST) o la query (GET), exactamente
  como se envía (ya codificado).
- endpointPath: la ruta de la URL sin el prefijo "/derivatives".
- Cabeceras: APIKey, Authent y Nonce.

SEGURIDAD: solo se pueden llamar los endpoints de ALLOWED_ENDPOINTS. Ninguno
mueve fondos (ni retiros ni transferencias); cualquier otra ruta se rechaza
antes de firmar nada.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from copybot.credentials import KrakenCredentials
from copybot.exchange.base import ExchangeError

log = logging.getLogger(__name__)

KRAKEN_FUTURES_URL = "https://futures.kraken.com"

# (método, ruta) permitidos. Cualquier otro se rechaza.
ALLOWED_ENDPOINTS: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/derivatives/api/v3/accounts"),
    ("GET", "/derivatives/api/v3/openpositions"),
    ("GET", "/derivatives/api/v3/openorders"),
    ("GET", "/derivatives/api/v3/fills"),
    ("POST", "/derivatives/api/v3/sendorder"),
    ("POST", "/derivatives/api/v3/cancelorder"),
    ("POST", "/derivatives/api/v3/orders/status"),
    ("GET", "/api/history/v3/account-log"),
    ("GET", "/api/auth/v1/api-keys/v3/check"),
})

Params = Sequence[tuple[str, str]]


class EndpointNotAllowed(Exception):
    """Intento de llamar a un endpoint fuera de la lista blanca."""


class KrakenApiError(ExchangeError):
    def __init__(self, path: str, error: str) -> None:
        super().__init__(f"{path}: {error}")
        self.error = error


def sign(secret_b64: str, post_data: str, nonce: str, endpoint_path: str) -> str:
    digest = hashlib.sha256((post_data + nonce + endpoint_path).encode("utf-8")).digest()
    key = base64.b64decode(secret_b64)
    return base64.b64encode(hmac.new(key, digest, hashlib.sha512).digest()).decode("ascii")


def endpoint_path(path: str) -> str:
    return path.removeprefix("/derivatives")


def encode(params: Params | Mapping[str, str] | None) -> str:
    if not params:
        return ""
    items = list(params.items()) if isinstance(params, Mapping) else list(params)
    return urlencode(items)


class KrakenPrivateClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        creds: KrakenCredentials,
        *,
        base_url: str = KRAKEN_FUTURES_URL,
        nonce: Callable[[], str] | None = None,
    ) -> None:
        self._http = http
        self._creds = creds
        self._base = base_url
        self._last_nonce = 0
        self._nonce = nonce or self._monotonic_nonce

    def _monotonic_nonce(self) -> str:
        n = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = n
        return str(n)

    async def request(
        self, method: str, path: str, params: Params | Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        if (method, path) not in ALLOWED_ENDPOINTS:
            raise EndpointNotAllowed(f"{method} {path} no está en la lista blanca")
        data = encode(params)
        nonce = self._nonce()
        headers = {
            "APIKey": self._creds.api_key.get_secret_value(),
            "Nonce": nonce,
            "Authent": sign(self._creds.api_secret.get_secret_value(), data, nonce,
                            endpoint_path(path)),
        }
        url = self._base + path
        try:
            if method == "GET":
                response = await self._http.get(url + (f"?{data}" if data else ""),
                                                headers=headers)
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                response = await self._http.post(url, content=data, headers=headers)
        except httpx.TransportError as exc:
            raise ExchangeError(f"{path}: {type(exc).__name__}") from exc
        try:
            payload = json.loads(response.text, parse_float=Decimal)
        except ValueError:
            raise ExchangeError(f"{path}: HTTP {response.status_code}, JSON no válido") from None
        if not isinstance(payload, dict):
            raise ExchangeError(f"{path}: respuesta inesperada")
        if response.status_code >= 400 or payload.get("result") == "error":
            error = payload.get("error") or payload.get("message") or f"HTTP {response.status_code}"
            raise KrakenApiError(path, str(error))
        return payload
