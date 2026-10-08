"""Lectura del líder por REST: POST https://api.hyperliquid.xyz/info

Verificado contra la documentación oficial y la API real (2026-10-08):
- {"type": "clearinghouseState", "user": addr} -> assetPositions[].position
  {coin, szi (con signo), ...}, marginSummary.accountValue y time (ms).
  Los números vienen como cadenas; time es un entero.
- {"type": "allMids"} -> {coin: "precio"}. Incluye spot ("@1") y otros
  mercados ("#14720"), que se descartan.
- {"type": "userAbstraction", "user": addr} -> "unifiedAccount" |
  "portfolioMargin" | "disabled" | "default" | "dexAbstraction".
  Con unified account o portfolio margin el capital NO está en
  clearinghouseState (está en el estado spot): esos modos no se operan.
- Límite por IP: peso agregado 1200/min. clearinghouseState y allMids pesan
  2; el resto de peticiones info documentadas, 20.

Todo error (red, HTTP, JSON o estructura) acaba en LeaderDataError:
el ciclo no opera.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from copybot.config import Dec
from copybot.models import LeaderSnapshot

log = logging.getLogger(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"

IP_WEIGHT_PER_MINUTE = 1200
REQUEST_WEIGHTS = {"clearinghouseState": 2, "allMids": 2}
DEFAULT_WEIGHT = 20

# Modos en los que clearinghouseState refleja capital y posiciones del perp dex
# principal. "default" verificado con la API real (cuenta con posiciones).
STANDARD_MODES = frozenset({"default", "disabled"})
UNSUPPORTED_MODES = frozenset({"unifiedAccount", "portfolioMargin", "dexAbstraction"})

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class LeaderDataError(Exception):
    """Datos del líder no disponibles o no fiables: no se opera."""


class UnsupportedAccountMode(LeaderDataError):
    """El líder usa un modo de cuenta cuyo capital no está en clearinghouseState."""


# --- Modelos de respuesta (se ignoran campos extra; los usados son obligatorios) ---


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Position(_Model):
    coin: str
    szi: Dec


class _AssetPosition(_Model):
    type: str
    position: _Position


class _MarginSummary(_Model):
    accountValue: Dec


class _ClearinghouseState(_Model):
    assetPositions: list[_AssetPosition]
    marginSummary: _MarginSummary
    time: int


class WeightBudget:
    """Ventana deslizante de 60 s sobre el peso de las peticiones.

    Por defecto usa la mitad del límite por IP: deja margen para otros procesos
    en la misma máquina (p. ej. rank_leaders) y para la deriva del reloj.
    """

    def __init__(
        self,
        per_minute: int = IP_WEIGHT_PER_MINUTE // 2,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not 0 < per_minute <= IP_WEIGHT_PER_MINUTE:
            raise ValueError("presupuesto de peso fuera de rango")
        self._limit = per_minute
        self._clock = clock
        self._sleep = sleep
        self._spent: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    def _used(self, now: float) -> int:
        while self._spent and now - self._spent[0][0] >= 60:
            self._spent.popleft()
        return sum(w for _, w in self._spent)

    async def acquire(self, weight: int) -> None:
        async with self._lock:
            while self._used(self._clock()) + weight > self._limit:
                wait = 60 - (self._clock() - self._spent[0][0])
                log.warning("presupuesto de peso de Hyperliquid agotado: espera %.1f s", wait)
                await self._sleep(max(wait, 0.05))
            self._spent.append((self._clock(), weight))


class HyperliquidInfo:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        url: str = HL_INFO_URL,
        budget: WeightBudget | None = None,
        max_retries: int = 3,
        backoff_initial_seconds: float = 0.5,
        abstraction_ttl_seconds: float = 300,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self._http = http
        self._url = url
        self._budget = budget or WeightBudget(sleep=sleep, clock=clock)
        self._max_retries = max_retries
        self._backoff = backoff_initial_seconds
        self._abstraction_ttl = abstraction_ttl_seconds
        self._sleep = sleep
        self._clock = clock
        self._abstraction_cache: dict[str, tuple[float, str]] = {}

    async def _post(self, body: dict[str, str]) -> Any:
        kind = body["type"]
        for attempt in range(self._max_retries + 1):
            await self._budget.acquire(REQUEST_WEIGHTS.get(kind, DEFAULT_WEIGHT))
            try:
                response = await self._http.post(self._url, json=body)
            except httpx.TransportError as exc:  # timeouts, conexión, etc.
                problem = f"{type(exc).__name__}"
            else:
                status = response.status_code
                if status == 200:
                    try:
                        return json.loads(response.text, parse_float=Decimal)
                    except ValueError as exc:
                        raise LeaderDataError(f"{kind}: JSON no válido") from exc
                if status != 429 and status < 500:
                    raise LeaderDataError(f"{kind}: HTTP {status}")
                problem = f"HTTP {status}"
            if attempt == self._max_retries:
                raise LeaderDataError(f"{kind}: {problem} tras {attempt + 1} intentos")
            delay = self._backoff * 2**attempt
            log.warning("%s: %s, reintento en %.1f s", kind, problem, delay)
            await self._sleep(delay)
        raise AssertionError("inalcanzable")

    async def clearinghouse_state(self, user: str) -> _ClearinghouseState:
        raw = await self._post({"type": "clearinghouseState", "user": user})
        try:
            return _ClearinghouseState.model_validate(raw)
        except ValidationError as exc:
            raise LeaderDataError(
                f"clearinghouseState malformado ({exc.error_count()} errores)"
            ) from None

    async def all_mids(self) -> dict[str, Decimal]:
        raw = await self._post({"type": "allMids"})
        if not isinstance(raw, dict):
            raise LeaderDataError("allMids: se esperaba un objeto")
        mids: dict[str, Decimal] = {}
        for coin, px in raw.items():
            if coin.startswith(("@", "#")):  # spot y otros mercados: no son perps
                continue
            try:
                value = Decimal(px) if isinstance(px, str) else None
            except ArithmeticError:
                value = None
            if value is None or not value.is_finite() or value <= 0:
                raise LeaderDataError(f"allMids: precio no válido para {coin}")
            mids[coin] = value
        return mids

    async def user_abstraction(self, user: str) -> str:
        now = self._clock()
        cached = self._abstraction_cache.get(user)
        if cached and now - cached[0] < self._abstraction_ttl:
            return cached[1]
        mode = await self._post({"type": "userAbstraction", "user": user})
        if not isinstance(mode, str):
            raise LeaderDataError("userAbstraction: se esperaba una cadena")
        self._abstraction_cache[user] = (now, mode)
        return mode

    async def leader_snapshot(self, user: str) -> LeaderSnapshot:
        mode = await self.user_abstraction(user)
        if mode in UNSUPPORTED_MODES:
            raise UnsupportedAccountMode(
                f"el líder usa el modo de cuenta {mode!r}: su capital no está en "
                "clearinghouseState, no se puede dimensionar"
            )
        if mode not in STANDARD_MODES:
            raise LeaderDataError(f"modo de cuenta desconocido: {mode!r}")

        state, mids = await asyncio.gather(self.clearinghouse_state(user), self.all_mids())

        positions: dict[str, Decimal] = {}
        seen: set[str] = set()
        for ap in state.assetPositions:
            if ap.type != "oneWay":
                raise LeaderDataError(f"tipo de posición desconocido: {ap.type!r}")
            coin = ap.position.coin
            if coin in seen:
                raise LeaderDataError(f"posición duplicada para {coin}")
            seen.add(coin)
            if ap.position.szi != 0:
                positions[coin] = ap.position.szi

        return LeaderSnapshot(
            equity_usd=state.marginSummary.accountValue,
            positions=positions,
            mids=mids,
            timestamp=datetime.fromtimestamp(state.time / 1000, tz=UTC),
        )
