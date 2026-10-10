"""Orquestación de descargas (única parte con red) y carga local de lo descargado.

Dos pasos, para poder revisar el universo antes de descargar las series horarias:

1. ``descargar-universo``: instrumentos de ambas plataformas, pares spot de Kraken y velas
   diarias de los últimos 90 días → ``datos/universo.json``.
2. ``descargar``: para los activos seleccionados, velas de 1h y funding → ``datos/A`` y
   ``datos/B``, más ``datos/manifest.json``.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from backtest.data import DataError, iso
from backtest.funding.config import (
    DAY_MS,
    HOUR_MS,
    UNIVERSE_DAYS,
    WINDOW_A_START_MS,
    WINDOW_B_DAYS,
)
from backtest.funding.data import (
    Bars,
    HlPerp,
    KrakenPerp,
    Log,
    Rates,
    bars_from_rows,
    fetch_hl_bars,
    fetch_hl_funding,
    fetch_hl_perps,
    fetch_kraken_bars,
    fetch_kraken_funding,
    fetch_kraken_perps,
    fetch_kraken_spot_bases,
    load_bars,
    load_rates,
    read_json,
    save_bars,
    save_rates,
    write_json,
)
from backtest.funding.universe import Candidate, universe_a, universe_b, volume_days

UNIVERSE_FILE = "universo.json"
MANIFEST = "manifest.json"
# Margen previo a cada ventana para la media de 24 h del primer instante de decisión.
WARMUP_MS = 2 * DAY_MS


def _quiet(_: str) -> None:
    pass


def _client(client: httpx.Client | None) -> Any:
    return nullcontext(client) if client is not None else httpx.Client(timeout=60.0)


def _bars_rows(b: Bars) -> list[list[float]]:
    return [[b.t[i], b.o[i], b.h[i], b.l[i], b.c[i], b.v[i]] for i in range(len(b))]


def _bars_from_json(name: str, rows: list[list[float]], now_ms: int) -> Bars:
    data = {int(r[0]): (r[1], r[2], r[3], r[4], r[5]) for r in rows}
    return bars_from_rows(name, DAY_MS, data, now_ms)


# --- paso 1: universo ---------------------------------------------------------------------


def download_universe(
    directory: Path, now_ms: int, client: httpx.Client | None = None, log: Log = _quiet
) -> dict[str, Any]:
    start = volume_days(now_ms)[0]
    with _client(client) as http:
        kraken = fetch_kraken_perps(http)
        hl = fetch_hl_perps(http)
        try:
            spot: list[str] | None = sorted(fetch_kraken_spot_bases(http))
            spot_error = ""
        except (httpx.HTTPError, DataError) as exc:  # p. ej. host bloqueado por la red
            spot, spot_error = None, f"{type(exc).__name__}: {exc}"
            log(f"pares spot de Kraken no disponibles: {spot_error}")
        kraken_bases = {k.base for k in kraken}
        daily_k: dict[str, list[list[float]]] = {}
        for i, k in enumerate(kraken, 1):
            log(f"[{i}/{len(kraken)}] velas diarias {k.symbol}")
            try:
                daily_k[k.symbol] = _bars_rows(fetch_kraken_bars(http, "trade", k.symbol, "1d",
                                                                 start, now_ms))
            except DataError:
                daily_k[k.symbol] = []  # sin velas cerradas en el periodo
        daily_h: dict[str, list[list[float]]] = {}
        matched = [h for h in hl if h.base in kraken_bases]
        for i, h in enumerate(matched, 1):
            log(f"[{i}/{len(matched)}] velas diarias {h.coin} (Hyperliquid)")
            try:
                daily_h[h.coin] = _bars_rows(fetch_hl_bars(http, h, "1d", start, now_ms))
            except DataError:
                daily_h[h.coin] = []
    doc = {
        "downloaded_at": iso(now_ms),
        "now_ms": now_ms,
        "kraken_perps": [asdict(k) for k in kraken],
        "hyperliquid_perps": [asdict(h) for h in hl],
        "kraken_spot_bases": spot,
        "kraken_spot_error": spot_error,
        "daily_kraken": daily_k,
        "daily_hyperliquid": daily_h,
    }
    write_json(directory / UNIVERSE_FILE, doc)
    return doc


@dataclass(frozen=True)
class Universe:
    now_ms: int
    days: list[int]
    a: list[Candidate]
    b: list[Candidate]
    spot_checked: bool
    spot_error: str
    kraken: dict[str, KrakenPerp]
    hyperliquid: dict[str, HlPerp]

    def selected(self, strategy: str) -> list[Candidate]:
        return [c for c in (self.a if strategy == "A" else self.b) if c.selected]


def load_universe(directory: Path) -> Universe:
    doc = read_json(directory / UNIVERSE_FILE)
    now_ms = int(doc["now_ms"])
    kraken = [KrakenPerp(**k) for k in doc["kraken_perps"]]
    hl = [HlPerp(**h) for h in doc["hyperliquid_perps"]]
    daily_k = {s: _bars_from_json(s, r, now_ms) for s, r in doc["daily_kraken"].items() if r}
    daily_h = {s: _bars_from_json(s, r, now_ms) for s, r in doc["daily_hyperliquid"].items() if r}
    spot = set(doc["kraken_spot_bases"]) if doc["kraken_spot_bases"] is not None else None
    days = volume_days(now_ms, UNIVERSE_DAYS)
    return Universe(
        now_ms=now_ms,
        days=days,
        a=universe_a(kraken, hl, daily_k, daily_h, days),
        b=universe_b(kraken, daily_k, spot, days),
        spot_checked=spot is not None,
        spot_error=doc.get("kraken_spot_error", ""),
        kraken={k.symbol: k for k in kraken},
        hyperliquid={h.coin: h for h in hl},
    )


# --- paso 2: series horarias --------------------------------------------------------------


def series_paths(directory: Path, strategy: str, key: str) -> dict[str, Path]:
    d = directory / strategy
    if strategy == "A":
        return {
            "kraken_trade": d / f"{key}_kraken_trade_1h.csv",
            "kraken_funding": d / f"{key}_kraken_funding_1h.csv",
            "hl_trade": d / f"{key}_hyperliquid_trade_1h.csv",
            "hl_funding": d / f"{key}_hyperliquid_funding_1h.csv",
        }
    return {
        "kraken_trade": d / f"{key}_kraken_trade_1h.csv",
        "kraken_spot": d / f"{key}_kraken_spot_1h.csv",
        "kraken_funding": d / f"{key}_kraken_funding_1h.csv",
    }


def download_series(
    directory: Path, now_ms: int, client: httpx.Client | None = None, log: Log = _quiet
) -> dict[str, Any]:
    uni = load_universe(directory)
    if not uni.spot_checked:
        raise DataError("el universo B no está comprobado (faltan los pares spot de Kraken)")
    start_a = WINDOW_A_START_MS - WARMUP_MS
    end_hour = now_ms - now_ms % HOUR_MS
    start_b = end_hour - WINDOW_B_DAYS * DAY_MS - WARMUP_MS
    info: dict[str, Any] = {"downloaded_at": iso(now_ms), "now_ms": now_ms, "A": {}, "B": {}}
    with _client(client) as http:
        for c in uni.selected("A"):
            assert c.hyperliquid is not None
            log(f"A: {c.base}")
            hl = uni.hyperliquid[c.hyperliquid]
            p = series_paths(directory, "A", c.base)
            kt = fetch_kraken_bars(http, "trade", c.kraken, "1h", start_a, now_ms)
            kf = fetch_kraken_funding(http, c.kraken)
            ht = fetch_hl_bars(http, hl, "1h", start_a, now_ms)
            hf = fetch_hl_funding(http, hl, start_a, now_ms)
            for key, obj in (("kraken_trade", kt), ("hl_trade", ht)):
                save_bars(p[key], obj)
            save_rates(p["kraken_funding"], kf)
            save_rates(p["hl_funding"], hf)
            info["A"][c.base] = _series_info(kraken_trade=kt, hl_trade=ht, kraken_funding=kf,
                                             hl_funding=hf)
        for c in uni.selected("B"):
            log(f"B: {c.base}")
            p = series_paths(directory, "B", c.base)
            kt = fetch_kraken_bars(http, "trade", c.kraken, "1h", start_b, now_ms)
            ks = fetch_kraken_bars(http, "spot", c.kraken, "1h", start_b, now_ms)
            kf = fetch_kraken_funding(http, c.kraken)
            save_bars(p["kraken_trade"], kt)
            save_bars(p["kraken_spot"], ks)
            save_rates(p["kraken_funding"], kf)
            info["B"][c.base] = _series_info(kraken_trade=kt, kraken_spot=ks, kraken_funding=kf)
    write_json(directory / MANIFEST, info)
    return info


def _series_info(**series: Bars | Rates) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, s in series.items():
        entry: dict[str, Any] = {"rows": len(s.t), "first": iso(s.t[0]), "last": iso(s.t[-1])}
        if isinstance(s, Bars):
            entry["filled"] = s.filled
        out[key] = entry
    return out


def load_series(directory: Path, strategy: str, key: str) -> dict[str, Bars | Rates]:
    manifest = read_json(directory / MANIFEST)[strategy][key]
    out: dict[str, Bars | Rates] = {}
    for name, path in series_paths(directory, strategy, key).items():
        if name.endswith("funding"):
            out[name] = load_rates(path, f"{name}:{key}")
        else:
            out[name] = load_bars(path, f"{name}:{key}", HOUR_MS, manifest[name]["filled"])
    return out
