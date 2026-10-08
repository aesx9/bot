"""Datos públicos de Kraken Futures (sin claves).

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
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from copybot.models import MarketSpec

log = logging.getLogger(__name__)

KRAKEN_FUTURES_URL = "https://futures.kraken.com"
INSTRUMENTS_PATH = "/derivatives/api/v3/instruments"

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
