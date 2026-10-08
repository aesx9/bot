"""Datos públicos de Kraken Futures (sin claves).

Ver también SPEC.md (verificación de APIs). Endpoints usados:
- /instruments: reglas de tamaño de cada mercado.
- /tickers: precio mark, bid/ask, suspensión y funding actual.
- /orderbook?symbol=: libro completo (los bids llegan en orden ascendente
  en la API real, así que se ordenan siempre).
- /historical-funding-rates?symbol=: tasas pasadas, ordenadas por tiempo.

Verificado contra la documentación oficial y la API real (2026-10-08):
GET https://futures.kraken.com/derivatives/api/v3/instruments
-> {"result": "success", "serverTime": ..., "instruments": [...]}

Campos usados de cada instrumento: symbol, type, tradeable, isExpired,
contractSize, tickSize, maxPositionSize y contractValueTradePrecision.
Los PF_ no traen un tamaño mínimo aparte: el tamaño debe ser múltiplo de
10^-contractValueTradePrecision (la precisión puede ser negativa).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from copybot.models import MarketSpec

log = logging.getLogger(__name__)

KRAKEN_FUTURES_URL = "https://futures.kraken.com"
INSTRUMENTS_PATH = "/derivatives/api/v3/instruments"
TICKERS_PATH = "/derivatives/api/v3/tickers"
ORDERBOOK_PATH = "/derivatives/api/v3/orderbook"
FUNDING_PATH = "/derivatives/api/v3/historical-funding-rates"
EURUSD_SYMBOL = "PF_EURUSD"

_PERP_SYMBOL = re.compile(r"^PF_[A-Z0-9]+USD$")


class KrakenDataError(Exception):
    """Respuesta de Kraken ausente, malformada o con error: no se opera."""


class _Instrument(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    symbol: str
    type: str
    tradeable: bool
    isExpired: bool = False
    contractSize: Decimal
    tickSize: Decimal
    maxPositionSize: Decimal
    contractValueTradePrecision: int


def parse_instruments(payload: Any) -> dict[str, MarketSpec]:
    """Perpetuos lineales PF_*USD operables, como MarketSpec por símbolo."""
    if not isinstance(payload, dict) or payload.get("result") != "success":
        raise KrakenDataError("instruments: respuesta sin result=success")
    raw = payload.get("instruments")
    if not isinstance(raw, list):
        raise KrakenDataError("instruments: falta la lista de instrumentos")

    specs: dict[str, MarketSpec] = {}
    for item in raw:
        symbol = item.get("symbol") if isinstance(item, dict) else None
        if not isinstance(symbol, str) or not _PERP_SYMBOL.match(symbol):
            continue
        try:
            ins = _Instrument.model_validate(item)
            if ins.type != "flexible_futures" or not ins.tradeable or ins.isExpired:
                continue
            if ins.contractSize != 1:
                # Todo el sizing trabaja en unidades del activo: un contrato distinto
                # de 1 cambiaría el significado del tamaño. Mejor no operarlo.
                log.error("%s: contractSize=%s inesperado, mercado descartado",
                          symbol, ins.contractSize)
                continue
            specs[symbol] = MarketSpec(
                symbol=symbol,
                size_step=Decimal(1).scaleb(-ins.contractValueTradePrecision),
                tick_size=ins.tickSize,
                max_position_size=ins.maxPositionSize,
            )
        except (ValidationError, ValueError) as exc:
            log.error("%s: instrumento malformado, mercado descartado (%s)",
                      symbol, type(exc).__name__)
    if not specs:
        raise KrakenDataError("instruments: ningún perpetuo PF_ operable")
    return specs


def decode_json(response: httpx.Response) -> Any:
    """JSON con decimales exactos (nunca float)."""
    try:
        return json.loads(response.text, parse_float=Decimal)
    except ValueError as exc:
        raise KrakenDataError(f"{response.request.url.path}: JSON no válido") from exc


async def fetch_instruments(
    http: httpx.AsyncClient, base_url: str = KRAKEN_FUTURES_URL
) -> dict[str, MarketSpec]:
    try:
        response = await http.get(base_url + INSTRUMENTS_PATH)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise KrakenDataError(f"instruments: error HTTP ({type(exc).__name__})") from exc
    return parse_instruments(decode_json(response))


# --- Tickers, libro y funding ---


@dataclass(frozen=True)
class Ticker:
    symbol: str
    mark_price: Decimal
    index_price: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    suspended: bool
    funding_rate: Decimal | None  # absoluto: USD por unidad y periodo


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    bids: tuple[tuple[Decimal, Decimal], ...]  # (precio, tamaño), mejor primero
    asks: tuple[tuple[Decimal, Decimal], ...]


@dataclass(frozen=True)
class FundingRate:
    timestamp: datetime
    rate: Decimal  # absoluto: USD por unidad; positivo = pagan los largos


class _TickerJson(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    symbol: str
    markPrice: Decimal
    indexPrice: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    suspended: bool = False
    fundingRate: Decimal | None = None


class _FundingJson(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    timestamp: datetime
    fundingRate: Decimal


def _check_success(payload: Any, what: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("result") != "success":
        raise KrakenDataError(f"{what}: respuesta sin result=success")
    return payload


def parse_tickers(payload: Any) -> dict[str, Ticker]:
    raw = _check_success(payload, "tickers").get("tickers")
    if not isinstance(raw, list):
        raise KrakenDataError("tickers: falta la lista")
    out: dict[str, Ticker] = {}
    for item in raw:
        symbol = item.get("symbol") if isinstance(item, dict) else None
        if not isinstance(symbol, str) or not _PERP_SYMBOL.match(symbol):
            continue
        try:
            t = _TickerJson.model_validate(item)
        except ValidationError:
            log.error("%s: ticker malformado, se ignora", symbol)
            continue
        if not t.markPrice.is_finite() or t.markPrice <= 0:
            log.error("%s: precio mark no válido, se ignora", symbol)
            continue
        out[symbol] = Ticker(
            symbol=symbol, mark_price=t.markPrice, index_price=t.indexPrice,
            bid=t.bid, ask=t.ask, suspended=t.suspended, funding_rate=t.fundingRate,
        )
    return out


def parse_orderbook(payload: Any, symbol: str) -> OrderBook:
    book = _check_success(payload, "orderbook").get("orderBook")
    if not isinstance(book, dict):
        raise KrakenDataError(f"orderbook {symbol}: falta orderBook")

    def side(name: str, best_first_descending: bool) -> tuple[tuple[Decimal, Decimal], ...]:
        levels = book.get(name)
        if not isinstance(levels, list):
            raise KrakenDataError(f"orderbook {symbol}: falta {name}")
        parsed = []
        for lvl in levels:
            try:
                px, qty = Decimal(str(lvl[0])), Decimal(str(lvl[1]))
            except (TypeError, IndexError, ArithmeticError):
                raise KrakenDataError(f"orderbook {symbol}: nivel malformado") from None
            if not (px.is_finite() and qty.is_finite()) or px <= 0 or qty < 0:
                raise KrakenDataError(f"orderbook {symbol}: nivel no válido")
            if qty > 0:
                parsed.append((px, qty))
        return tuple(sorted(parsed, key=lambda lv: lv[0], reverse=best_first_descending))

    ob = OrderBook(symbol, bids=side("bids", True), asks=side("asks", False))
    if ob.bids and ob.asks and ob.bids[0][0] >= ob.asks[0][0]:
        raise KrakenDataError(f"orderbook {symbol}: libro cruzado")
    return ob


def parse_funding_rates(payload: Any, symbol: str) -> list[FundingRate]:
    raw = _check_success(payload, "historical-funding-rates").get("rates")
    if not isinstance(raw, list):
        raise KrakenDataError(f"funding {symbol}: falta la lista de tasas")
    try:
        rates = [_FundingJson.model_validate(r) for r in raw]
    except ValidationError:
        raise KrakenDataError(f"funding {symbol}: tasa malformada") from None
    out = [FundingRate(r.timestamp, r.fundingRate) for r in rates]
    if any(r.timestamp.tzinfo is None or not r.rate.is_finite() for r in out):
        raise KrakenDataError(f"funding {symbol}: tasa o fecha no válidas")
    return sorted(out, key=lambda r: r.timestamp)


class KrakenMarketData:
    """Acceso a datos públicos con caché de instrumentos y de funding."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str = KRAKEN_FUTURES_URL,
        instruments_ttl_seconds: float = 3600,
        funding_ttl_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._base = base_url
        self._instruments_ttl = instruments_ttl_seconds
        self._funding_ttl = funding_ttl_seconds
        self._clock = clock
        self._instruments: tuple[float, dict[str, MarketSpec]] | None = None
        self._funding: dict[str, tuple[float, list[FundingRate]]] = {}

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        try:
            response = await self._http.get(self._base + path, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise KrakenDataError(f"{path}: error HTTP ({type(exc).__name__})") from exc
        return decode_json(response)

    async def instruments(self) -> dict[str, MarketSpec]:
        now = self._clock()
        if self._instruments and now - self._instruments[0] < self._instruments_ttl:
            return self._instruments[1]
        specs = parse_instruments(await self._get(INSTRUMENTS_PATH))
        self._instruments = (now, specs)
        return specs

    async def tickers(self) -> dict[str, Ticker]:
        return parse_tickers(await self._get(TICKERS_PATH))

    async def orderbook(self, symbol: str) -> OrderBook:
        return parse_orderbook(await self._get(ORDERBOOK_PATH, {"symbol": symbol}), symbol)

    async def funding_rates(self, symbol: str) -> list[FundingRate]:
        now = self._clock()
        cached = self._funding.get(symbol)
        if cached and now - cached[0] < self._funding_ttl:
            return cached[1]
        rates = parse_funding_rates(await self._get(FUNDING_PATH, {"symbol": symbol}), symbol)
        self._funding[symbol] = (now, rates)
        return rates

    async def eur_usd(self) -> Decimal:
        """EUR/USD de Kraken: índice del perpetuo PF_EURUSD."""
        t = (await self.tickers()).get(EURUSD_SYMBOL)
        price = t.index_price if t else None
        if price is None or not price.is_finite() or price <= 0:
            raise KrakenDataError("sin precio EUR/USD válido en PF_EURUSD")
        return price
