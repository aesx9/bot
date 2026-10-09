"""Descarga, validación y almacenamiento local de velas 4h y funding de Kraken Futures.

Endpoints (verificados en docs.kraken.com):

- Velas: ``GET https://futures.kraken.com/api/charts/v1/{tick_type}/{symbol}/{resolution}``
  con ``from``/``to`` en segundos epoch (opcionales) y ``count``. Cada vela trae ``time`` en
  milisegundos; ``more_candles`` indica que hay más velas en el rango.
- Funding: ``GET https://futures.kraken.com/derivatives/api/v3/historical-funding-rates?symbol=``.
  ``timestamp`` es el *inicio* del periodo horario al que aplica la tasa, ``fundingRate`` es la tasa
  absoluta (USD por unidad de contrato y hora) y ``relativeFundingRate`` la relativa al precio.
  El endpoint no admite rangos ni paginación: solo devuelve la última ventana (~1 año).
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from backtest.config import CANDLE_MS, HOUR_MS

CHARTS_URL = "https://futures.kraken.com/api/charts/v1/trade/{symbol}/4h"
FUNDING_URL = "https://futures.kraken.com/derivatives/api/v3/historical-funding-rates"
MAX_PAGES = 1000
MANIFEST = "manifest.json"


class DataError(Exception):
    """Datos descargados o cargados que no cumplen lo esperado."""


@dataclass(frozen=True)
class Candles:
    """Velas OHLCV ordenadas; ``t`` es la apertura de cada vela en ms epoch (UTC)."""

    symbol: str
    t: list[int]
    o: list[float]
    h: list[float]
    l: list[float]  # noqa: E741
    c: list[float]
    v: list[float]

    def __len__(self) -> int:
        return len(self.t)


@dataclass(frozen=True)
class FundingSeries:
    """Funding horario; ``t`` es el inicio de la hora a la que aplica cada tasa (ms epoch)."""

    symbol: str
    t: list[int]
    rate_abs: list[float]  # USD por unidad de contrato y hora (positivo: pagan los largos)
    rate_rel: list[float]  # relativa al precio


def validate_candles(c: Candles) -> None:
    """Orden estricto, sin huecos de 4h y OHLC coherente. Lanza ``DataError`` si falla."""
    n = len(c)
    if not (len(c.o) == len(c.h) == len(c.l) == len(c.c) == len(c.v) == n):
        raise DataError(f"{c.symbol}: columnas de longitud distinta")
    if n == 0:
        raise DataError(f"{c.symbol}: sin velas")
    for i in range(n):
        o, h, lo, cl = c.o[i], c.h[i], c.l[i], c.c[i]
        if not all(math.isfinite(x) and x > 0 for x in (o, h, lo, cl)):
            raise DataError(f"{c.symbol}: precio no finito o no positivo en la vela {i}")
        if h < max(o, cl, lo) or lo > min(o, cl, h):
            raise DataError(f"{c.symbol}: OHLC incoherente en la vela {i}")
        if i and c.t[i] - c.t[i - 1] != CANDLE_MS:
            raise DataError(
                f"{c.symbol}: hueco o desorden entre las velas {i - 1} y {i} "
                f"({iso(c.t[i - 1])} -> {iso(c.t[i])})"
            )


def validate_funding(f: FundingSeries) -> None:
    if not (len(f.t) == len(f.rate_abs) == len(f.rate_rel)):
        raise DataError(f"{f.symbol}: columnas de funding de longitud distinta")
    if not f.t:
        raise DataError(f"{f.symbol}: sin funding")
    for i, ts in enumerate(f.t):
        if ts % HOUR_MS:
            raise DataError(f"{f.symbol}: funding fuera de hora en punto ({iso(ts)})")
        if i and ts <= f.t[i - 1]:
            raise DataError(f"{f.symbol}: funding no estrictamente creciente en {iso(ts)}")
        if not (math.isfinite(f.rate_abs[i]) and math.isfinite(f.rate_rel[i])):
            raise DataError(f"{f.symbol}: tasa de funding no finita en {iso(ts)}")


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


# --- descarga ---------------------------------------------------------------------------


def fetch_candles(client: httpx.Client, symbol: str, now_ms: int) -> Candles:
    """Todo el histórico de velas 4h de tipo ``trade``, sin la vela en curso."""
    rows: dict[int, tuple[float, float, float, float, float]] = {}
    cursor_s = 0
    for _ in range(MAX_PAGES):
        resp = client.get(
            CHARTS_URL.format(symbol=symbol), params={"from": cursor_s, "to": now_ms // 1000}
        )
        resp.raise_for_status()
        body = resp.json()
        page = body["candles"]
        if not page:
            break
        for k in page:
            rows[int(k["time"])] = (
                float(k["open"]),
                float(k["high"]),
                float(k["low"]),
                float(k["close"]),
                float(k["volume"]),
            )
        last_s = int(page[-1]["time"]) // 1000
        if not body.get("more_candles") or last_s + 1 <= cursor_s:
            break
        cursor_s = last_s + 1
    else:
        raise DataError(f"{symbol}: demasiadas páginas de velas")
    # Una vela cuyo cierre aún no ha ocurrido está en curso: nunca se usa.
    times = [t for t in sorted(rows) if t + CANDLE_MS <= now_ms]
    candles = Candles(
        symbol,
        times,
        [rows[t][0] for t in times],
        [rows[t][1] for t in times],
        [rows[t][2] for t in times],
        [rows[t][3] for t in times],
        [rows[t][4] for t in times],
    )
    validate_candles(candles)
    return candles


def fetch_funding(client: httpx.Client, symbol: str) -> FundingSeries:
    resp = client.get(FUNDING_URL, params={"symbol": symbol})
    resp.raise_for_status()
    body = resp.json()
    if body.get("result") != "success":
        raise DataError(f"{symbol}: respuesta de funding no exitosa: {body}")
    rows = {_parse_iso(r["timestamp"]): r for r in body["rates"]}
    times = sorted(rows)
    series = FundingSeries(
        symbol,
        times,
        [float(rows[t]["fundingRate"]) for t in times],
        [float(rows[t]["relativeFundingRate"]) for t in times],
    )
    validate_funding(series)
    return series


# --- almacenamiento local ----------------------------------------------------------------


def candles_path(directory: Path, symbol: str) -> Path:
    return directory / f"{symbol}_4h_velas.csv"


def funding_path(directory: Path, symbol: str) -> Path:
    return directory / f"{symbol}_funding_1h.csv"


def save_candles(directory: Path, c: Candles) -> Path:
    path = candles_path(directory, c.symbol)
    directory.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["time_ms", "open", "high", "low", "close", "volume"])
        for i in range(len(c)):
            ohlcv = (c.o[i], c.h[i], c.l[i], c.c[i], c.v[i])
            w.writerow([c.t[i], *(repr(x) for x in ohlcv)])
    return path


def save_funding(directory: Path, f: FundingSeries) -> Path:
    path = funding_path(directory, f.symbol)
    directory.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["time_ms", "funding_rate", "relative_funding_rate"])
        for i in range(len(f.t)):
            w.writerow([f.t[i], repr(f.rate_abs[i]), repr(f.rate_rel[i])])
    return path


def load_candles(directory: Path, symbol: str) -> Candles:
    t: list[int] = []
    cols: tuple[list[float], ...] = ([], [], [], [], [])
    with candles_path(directory, symbol).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            t.append(int(row["time_ms"]))
            for col, key in zip(cols, ("open", "high", "low", "close", "volume"), strict=True):
                col.append(float(row[key]))
    candles = Candles(symbol, t, *cols)
    validate_candles(candles)
    return candles


def load_funding(directory: Path, symbol: str) -> FundingSeries:
    t: list[int] = []
    rate_abs: list[float] = []
    rate_rel: list[float] = []
    with funding_path(directory, symbol).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            t.append(int(row["time_ms"]))
            rate_abs.append(float(row["funding_rate"]))
            rate_rel.append(float(row["relative_funding_rate"]))
    series = FundingSeries(symbol, t, rate_abs, rate_rel)
    validate_funding(series)
    return series


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download_all(directory: Path, symbols: tuple[str, ...], now_ms: int) -> dict[str, object]:
    """Descarga velas y funding de cada símbolo, los guarda y escribe el manifiesto."""
    symbols_info: dict[str, object] = {}
    manifest: dict[str, object] = {"downloaded_at": iso(now_ms), "symbols": symbols_info}
    with httpx.Client(timeout=60.0) as client:
        for symbol in symbols:
            candles = fetch_candles(client, symbol, now_ms)
            funding = fetch_funding(client, symbol)
            cp = save_candles(directory, candles)
            fp = save_funding(directory, funding)
            info = {
                "candles": {
                    "rows": len(candles),
                    "first": iso(candles.t[0]),
                    "last_open": iso(candles.t[-1]),
                    "sha256": _sha256(cp),
                },
                "funding": {
                    "rows": len(funding.t),
                    "first": iso(funding.t[0]),
                    "last": iso(funding.t[-1]),
                    "sha256": _sha256(fp),
                },
            }
            symbols_info[symbol] = info
    (directory / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest
