"""Descarga, validación y almacenamiento local de los datos del arbitraje de funding.

Endpoints (públicos, sin autenticación):

- Kraken Futures, instrumentos: ``GET /derivatives/api/v3/instruments`` (``base``, márgenes).
- Kraken Futures, velas: ``GET /api/charts/v1/{trade|spot}/{symbol}/{1h|1d}`` con ``from``/``to``
  en segundos; máximo 2000 velas por respuesta, ``more_candles`` indica que hay más. ``trade`` es el
  precio negociado del perpetuo; ``spot`` es el índice spot de Kraken Futures, que se usa como
  aproximación del precio spot de Kraken (la API de spot no se usa para precios).
- Kraken Futures, funding: ``GET /derivatives/api/v3/historical-funding-rates?symbol=``. Sin rangos:
  devuelve ≈1 año. ``timestamp`` es el inicio de la hora a la que aplica la tasa.
- Kraken spot, pares: ``GET https://api.kraken.com/0/public/AssetPairs`` (solo para el universo B).
- Hyperliquid, ``POST /info``: ``metaAndAssetCtxs`` (universo y apalancamiento máximo),
  ``candleSnapshot`` (solo las 5000 velas más recientes de cada intervalo) y ``fundingHistory``
  (500 registros por respuesta; se pagina con ``startTime``). ``time`` del funding es el momento
  de liquidación, al final de la hora a la que aplica.

Convención interna: toda serie horaria se indexa por el **inicio de la hora** (ms epoch UTC). El
funding de la hora ``t`` se liquida en ``t + 1h`` y solo entonces puede usarse para decidir.
Las tasas de funding son relativas al nocional y horarias (positivo: los largos pagan).
"""

from __future__ import annotations

import csv
import json
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from backtest.data import DataError, _parse_iso, iso
from backtest.funding.config import DAY_MS, HOUR_MS

KRAKEN_FUTURES = "https://futures.kraken.com"
INSTRUMENTS_URL = f"{KRAKEN_FUTURES}/derivatives/api/v3/instruments"
CHARTS_URL = KRAKEN_FUTURES + "/api/charts/v1/{tick_type}/{symbol}/{resolution}"
KRAKEN_FUNDING_URL = f"{KRAKEN_FUTURES}/derivatives/api/v3/historical-funding-rates"
KRAKEN_SPOT_PAIRS_URL = "https://api.kraken.com/0/public/AssetPairs"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"

RESOLUTION_MS = {"1h": HOUR_MS, "1d": DAY_MS}
MAX_PAGES = 1000
HL_FUNDING_PAGE = 500
HL_PAUSE_S = 0.25  # margen frente al límite de peso por minuto de Hyperliquid

# Kraken usa códigos propios para algunas bases (en instrumentos de futuros ya vienen
# normalizados en ``base``; en spot no).
SPOT_BASE_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}


# --- tipos --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Bars:
    """Velas OHLCV contiguas de ``step_ms``; ``t`` es la apertura de cada vela (ms UTC).

    ``filled`` cuenta las velas que faltaban en la fuente y se rellenaron con el cierre anterior
    (sin rango ni volumen)."""

    name: str
    step_ms: int
    t: list[int]
    o: list[float]
    h: list[float]
    l: list[float]  # noqa: E741
    c: list[float]
    v: list[float]
    filled: int = 0

    def __len__(self) -> int:
        return len(self.t)


@dataclass(frozen=True)
class Rates:
    """Funding horario relativo; ``t`` es el inicio de la hora a la que aplica cada tasa."""

    name: str
    t: list[int]
    rate: list[float]


@dataclass(frozen=True)
class KrakenPerp:
    symbol: str  # PF_XBTUSD
    base: str  # BTC
    maintenance_margin: float  # primer tramo (minorista), fracción del nocional


@dataclass(frozen=True)
class HlPerp:
    coin: str  # nombre en Hyperliquid: BTC, kPEPE
    base: str  # base normalizada: BTC, PEPE
    units: float  # unidades de ``base`` por unidad de ``coin`` (kPEPE = 1000 PEPE)
    max_leverage: int

    @property
    def maintenance_margin(self) -> float:
        # Hyperliquid: margen de mantenimiento = la mitad del inicial al apalancamiento máximo.
        return 1.0 / (2.0 * self.max_leverage)


# --- validación ---------------------------------------------------------------------------


def validate_bars(b: Bars) -> None:
    n = len(b)
    if not (len(b.o) == len(b.h) == len(b.l) == len(b.c) == len(b.v) == n):
        raise DataError(f"{b.name}: columnas de longitud distinta")
    if n == 0:
        raise DataError(f"{b.name}: sin velas")
    for i in range(n):
        o, h, lo, cl = b.o[i], b.h[i], b.l[i], b.c[i]
        if not all(math.isfinite(x) and x > 0 for x in (o, h, lo, cl)):
            raise DataError(f"{b.name}: precio no finito o no positivo en {iso(b.t[i])}")
        if h < max(o, cl, lo) or lo > min(o, cl, h):
            raise DataError(f"{b.name}: OHLC incoherente en {iso(b.t[i])}")
        if b.t[i] % b.step_ms:
            raise DataError(f"{b.name}: vela fuera de rejilla en {iso(b.t[i])}")
        if i and b.t[i] - b.t[i - 1] != b.step_ms:
            raise DataError(f"{b.name}: hueco o desorden en {iso(b.t[i - 1])} -> {iso(b.t[i])}")


def validate_rates(r: Rates) -> None:
    if len(r.t) != len(r.rate):
        raise DataError(f"{r.name}: columnas de funding de longitud distinta")
    if not r.t:
        raise DataError(f"{r.name}: sin funding")
    for i, ts in enumerate(r.t):
        if ts % HOUR_MS:
            raise DataError(f"{r.name}: funding fuera de hora en punto ({iso(ts)})")
        if i and ts <= r.t[i - 1]:
            raise DataError(f"{r.name}: funding no estrictamente creciente en {iso(ts)}")
        if not math.isfinite(r.rate[i]):
            raise DataError(f"{r.name}: tasa no finita en {iso(ts)}")


def bars_from_rows(
    name: str,
    step_ms: int,
    rows: dict[int, tuple[float, float, float, float, float]],
    now_ms: int,
    scale: float = 1.0,
) -> Bars:
    """Ordena, descarta la vela en curso y rellena huecos internos con el cierre anterior.

    ``scale`` convierte precios por ``coin`` a precios por unidad de base (kPEPE: 1/1000) y el
    volumen a unidades de base."""
    times = [t for t in sorted(rows) if t % step_ms == 0 and t + step_ms <= now_ms]
    if not times:
        raise DataError(f"{name}: sin velas cerradas")
    t_out: list[int] = []
    cols: tuple[list[float], ...] = ([], [], [], [], [])
    filled = 0
    for t in range(times[0], times[-1] + step_ms, step_ms):
        row = rows.get(t)
        if row is None:
            prev = cols[3][-1]
            row = (prev, prev, prev, prev, 0.0)
            filled += 1
        else:
            o, h, lo, c, v = row
            row = (o * scale, h * scale, lo * scale, c * scale, v / scale)
        t_out.append(t)
        for col, x in zip(cols, row, strict=True):
            col.append(x)
    co, ch, cl, cc, cv = cols
    bars = Bars(name, step_ms, t_out, co, ch, cl, cc, cv, filled=filled)
    validate_bars(bars)
    return bars


# --- descarga: Kraken Futures -------------------------------------------------------------


def _get(client: httpx.Client, url: str, **params: Any) -> Any:
    resp = client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def fetch_kraken_perps(client: httpx.Client) -> list[KrakenPerp]:
    body = _get(client, INSTRUMENTS_URL)
    if body.get("result") != "success":
        raise DataError(f"instrumentos de Kraken: respuesta no exitosa: {body}")
    out: list[KrakenPerp] = []
    for x in body["instruments"]:
        if not str(x["symbol"]).startswith("PF_") or x.get("type") != "flexible_futures":
            continue
        if not x.get("tradeable") or x.get("quote") != "USD":
            continue
        levels = x.get("retailMarginLevels") or x["marginLevels"]
        out.append(KrakenPerp(x["symbol"], x["base"], float(levels[0]["maintenanceMargin"])))
    return sorted(out, key=lambda p: p.symbol)


def fetch_kraken_bars(
    client: httpx.Client,
    tick_type: str,
    symbol: str,
    resolution: str,
    start_ms: int,
    now_ms: int,
) -> Bars:
    """Velas cerradas desde ``start_ms`` (incluida) hasta ``now_ms``."""
    rows: dict[int, tuple[float, float, float, float, float]] = {}
    cursor_s = start_ms // 1000
    url = CHARTS_URL.format(tick_type=tick_type, symbol=symbol, resolution=resolution)
    for _ in range(MAX_PAGES):
        body = _get(client, url, **{"from": cursor_s, "to": now_ms // 1000})
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
    rows = {t: r for t, r in rows.items() if t >= start_ms}
    return bars_from_rows(f"kraken:{tick_type}:{symbol}", RESOLUTION_MS[resolution], rows, now_ms)


def fetch_kraken_funding(client: httpx.Client, symbol: str) -> Rates:
    body = _get(client, KRAKEN_FUNDING_URL, symbol=symbol)
    if body.get("result") != "success":
        raise DataError(f"{symbol}: respuesta de funding no exitosa: {body}")
    rows = {_parse_iso(r["timestamp"]): float(r["relativeFundingRate"]) for r in body["rates"]}
    times = sorted(rows)
    rates = Rates(f"kraken:{symbol}", times, [rows[t] for t in times])
    validate_rates(rates)
    return rates


def fetch_kraken_spot_bases(client: httpx.Client) -> set[str]:
    """Bases normalizadas con un par spot ``BASE/USD`` en línea en Kraken."""
    body = _get(client, KRAKEN_SPOT_PAIRS_URL)
    if body.get("error"):
        raise DataError(f"pares spot de Kraken: {body['error']}")
    return spot_bases_from_pairs(body["result"].values())


def spot_bases_from_pairs(pairs: Iterable[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for p in pairs:
        ws = p.get("wsname")
        if not ws or "/" not in ws or p.get("status", "online") != "online":
            continue
        base, quote = ws.split("/", 1)
        if quote == "USD":
            out.add(SPOT_BASE_ALIASES.get(base, base))
    return out


# --- descarga: Hyperliquid ----------------------------------------------------------------


def _post(client: httpx.Client, payload: dict[str, Any]) -> Any:
    resp = client.post(HL_INFO_URL, json=payload)
    resp.raise_for_status()
    time.sleep(HL_PAUSE_S)
    return resp.json()


def hl_base(coin: str) -> tuple[str, float]:
    """(base normalizada, unidades de base por unidad de ``coin``): ``kPEPE`` -> (PEPE, 1000)."""
    if len(coin) > 1 and coin[0] == "k" and coin[1:].isupper():
        return coin[1:], 1000.0
    return coin, 1.0


def fetch_hl_perps(client: httpx.Client) -> list[HlPerp]:
    meta, _ctx = _post(client, {"type": "metaAndAssetCtxs"})
    out: list[HlPerp] = []
    for u in meta["universe"]:
        if u.get("isDelisted"):
            continue
        base, units = hl_base(u["name"])
        out.append(HlPerp(u["name"], base, units, int(u["maxLeverage"])))
    return sorted(out, key=lambda p: p.coin)


def fetch_hl_bars(
    client: httpx.Client, perp: HlPerp, interval: str, start_ms: int, now_ms: int
) -> Bars:
    """Velas cerradas (candleSnapshot solo sirve las 5000 más recientes), en unidades de base."""
    body = _post(
        client,
        {
            "type": "candleSnapshot",
            "req": {"coin": perp.coin, "interval": interval, "startTime": start_ms,
                    "endTime": now_ms},
        },
    )
    rows = {
        int(k["t"]): (float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"]))
        for k in body
        if int(k["t"]) >= start_ms
    }
    return bars_from_rows(
        f"hyperliquid:{perp.coin}", RESOLUTION_MS[interval], rows, now_ms, 1.0 / perp.units
    )


def hl_rates_from_rows(name: str, rows: Iterable[dict[str, Any]]) -> Rates:
    """``time`` es la liquidación (hora en punto + unos ms): la tasa aplica a la hora anterior."""
    by_hour: dict[int, float] = {}
    for r in rows:
        settled = int(r["time"])
        hour_end = settled - settled % HOUR_MS
        by_hour[hour_end - HOUR_MS] = float(r["fundingRate"])
    times = sorted(by_hour)
    rates = Rates(name, times, [by_hour[t] for t in times])
    validate_rates(rates)
    return rates


def fetch_hl_funding(client: httpx.Client, perp: HlPerp, start_ms: int, now_ms: int) -> Rates:
    rows: list[dict[str, Any]] = []
    cursor = start_ms
    for _ in range(MAX_PAGES):
        page = _post(
            client,
            {"type": "fundingHistory", "coin": perp.coin, "startTime": cursor, "endTime": now_ms},
        )
        if not page:
            break
        rows.extend(page)
        last = int(page[-1]["time"])
        if len(page) < HL_FUNDING_PAGE or last + 1 <= cursor:
            break
        cursor = last + 1
    else:
        raise DataError(f"{perp.coin}: demasiadas páginas de funding")
    return hl_rates_from_rows(f"hyperliquid:{perp.coin}", rows)


# --- almacenamiento local -----------------------------------------------------------------


def save_bars(path: Path, b: Bars) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["time_ms", "open", "high", "low", "close", "volume"])
        for i in range(len(b)):
            w.writerow([b.t[i], *(repr(x) for x in (b.o[i], b.h[i], b.l[i], b.c[i], b.v[i]))])


def load_bars(path: Path, name: str, step_ms: int, filled: int = 0) -> Bars:
    t: list[int] = []
    cols: tuple[list[float], ...] = ([], [], [], [], [])
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            t.append(int(row["time_ms"]))
            for col, key in zip(cols, ("open", "high", "low", "close", "volume"), strict=True):
                col.append(float(row[key]))
    o, h, lo, c, v = cols
    bars = Bars(name, step_ms, t, o, h, lo, c, v, filled=filled)
    validate_bars(bars)
    return bars


def save_rates(path: Path, r: Rates) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["time_ms", "rate"])
        for ts, x in zip(r.t, r.rate, strict=True):
            w.writerow([ts, repr(x)])


def load_rates(path: Path, name: str) -> Rates:
    t: list[int] = []
    rate: list[float] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            t.append(int(row["time_ms"]))
            rate.append(float(row["rate"]))
    rates = Rates(name, t, rate)
    validate_rates(rates)
    return rates


def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


Log = Callable[[str], None]
