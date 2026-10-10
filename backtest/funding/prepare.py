"""Alineación de las series descargadas en la rejilla horaria de cada estrategia.

Las ventanas son fijas y no dependen de los datos descargados:

- A: 208 días desde el 2026-03-16.
- B: los 365 días que terminan a las 00:00 UTC del día de la descarga.

Cada activo lleva ``MEAN_HOURS`` horas previas de calentamiento para la media de 24 h (solo su
funding se usa; los precios de esas horas no se operan).

Regla de datos completos (fijada antes de descargar): un activo entra en una estrategia solo si
cada una de sus series tiene dato real en toda la ventana: una vela por hora operable (las velas
rellenadas en la descarga no cuentan) y funding en cada hora de la ventana y del calentamiento.
Si no, se excluye de esa estrategia con el motivo; nunca se rellena ni se acorta la ventana, y
nunca se sustituyen precios de una plataforma por los de otra.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from backtest.data import DataError, iso
from backtest.funding.config import (
    DAY_MS,
    HOUR_MS,
    MEAN_HOURS,
    WINDOW_A_DAYS,
    WINDOW_A_START_MS,
    WINDOW_B_DAYS,
    Costs,
    SpotFee,
)
from backtest.funding.data import Bars, Rates, read_json
from backtest.funding.download import MANIFEST, Universe, load_series
from backtest.funding.engine import Asset, Leg

VENUE_KRAKEN = "kraken_futures"
VENUE_HL = "hyperliquid"
VENUE_SPOT = "kraken_spot"


@dataclass(frozen=True)
class Window:
    start: int  # primera hora operable (ms)
    end: int  # exclusivo

    @property
    def hours(self) -> int:
        return (self.end - self.start) // HOUR_MS


@dataclass(frozen=True)
class Coverage:
    """Calidad de los datos de un activo dentro de la ventana."""

    asset: str
    flat_bars: dict[str, int]  # velas de la fuente sin operaciones (planas y sin volumen)


@dataclass(frozen=True)
class Exclusion:
    asset: str
    reasons: list[str]


def window_a(downloaded_ms: int) -> Window:
    w = Window(WINDOW_A_START_MS, WINDOW_A_START_MS + WINDOW_A_DAYS * DAY_MS)
    if w.end > downloaded_ms - downloaded_ms % HOUR_MS:
        raise DataError(f"la ventana de A termina el {iso(w.end)}, después de la descarga")
    return w


def window_b(downloaded_ms: int) -> Window:
    end = downloaded_ms - downloaded_ms % DAY_MS
    return Window(end - WINDOW_B_DAYS * DAY_MS, end)


def downloaded_ms(directory: Path) -> int:
    return int(read_json(directory / MANIFEST)["now_ms"])


def _hours_txt(missing: Sequence[int]) -> str:
    n = len(missing)
    first = iso(missing[0])
    return f"{n} hora{'s' if n != 1 else ''} sin dato (primera {first})"


def missing_bars(b: Bars, w: Window) -> list[int]:
    """Horas operables de ``w`` sin vela real en ``b`` (ausentes o rellenadas en la descarga)."""
    have = set(b.t).difference(b.filled)
    return [t for t in range(w.start, w.end, HOUR_MS) if t not in have]


def missing_rates(r: Rates, w: Window) -> list[int]:
    """Horas de ``w`` y de su calentamiento sin tasa de funding."""
    have = set(r.t)
    return [t for t in grid_for(w) if t not in have]


def incomplete(w: Window, bars: Mapping[str, Bars], rates: Mapping[str, Rates]) -> list[str]:
    """Motivos por los que las series de un activo no cubren ``w``; vacío si están completas."""
    out = []
    for key, b in bars.items():
        if gaps := missing_bars(b, w):
            out.append(f"velas {key}: {_hours_txt(gaps)}")
    for key, r in rates.items():
        if gaps := missing_rates(r, w):
            out.append(f"funding {key}: {_hours_txt(gaps)}")
    return out


def _prices(b: Bars, grid: Sequence[int]) -> tuple[list[float], ...]:
    idx = {t: i for i, t in enumerate(b.t)}
    o: list[float] = []
    h: list[float] = []
    lo: list[float] = []
    c: list[float] = []
    for t in grid:
        i = idx.get(t)
        if i is None:
            if t >= grid[MEAN_HOURS]:  # ``incomplete`` lo descarta antes; no se rellena
                raise DataError(f"{b.name}: falta la vela de {iso(t)} en la ventana")
            i = 0  # calentamiento: precio sin uso, solo para tener columnas completas
        o.append(b.o[i])
        h.append(b.h[i])
        lo.append(b.l[i])
        c.append(b.c[i])
    return o, h, lo, c


def _rates(r: Rates | None, grid: Sequence[int]) -> list[float]:
    if r is None:
        return [0.0] * len(grid)
    by_t = dict(zip(r.t, r.rate, strict=True))
    return [by_t.get(t, math.nan) for t in grid]


def make_leg(
    venue: str, bars: Bars, rates: Rates | None, grid: Sequence[int], fee: float,
    mm: float | None,
) -> Leg:
    """``rates`` es ``None`` para el spot (sin funding: tasa 0)."""
    o, h, lo, c = _prices(bars, grid)
    return Leg(venue, o, h, lo, c, _rates(rates, grid), fee, mm)


def grid_for(w: Window) -> list[int]:
    return list(range(w.start - MEAN_HOURS * HOUR_MS, w.end, HOUR_MS))


def _coverage(name: str, w: Window, bars: Mapping[str, Bars]) -> Coverage:
    flat = {}
    for key, b in bars.items():
        flat[key] = sum(
            1 for i, t in enumerate(b.t)
            if w.start <= t < w.end and b.v[i] == 0.0 and b.o[i] == b.h[i] == b.l[i] == b.c[i]
        )
    return Coverage(name, flat)


Built = tuple[list[Asset], Window, list[Coverage], list[Exclusion]]


def build_a(directory: Path, uni: Universe, costs: Costs) -> Built:
    w = window_a(downloaded_ms(directory))
    grid = grid_for(w)
    assets: list[Asset] = []
    cov: list[Coverage] = []
    excluded: list[Exclusion] = []
    for c in uni.selected("A"):
        s = load_series(directory, "A", c.base)
        assert c.hyperliquid is not None
        kt, ht, kf, hf = s["kraken_trade"], s["hl_trade"], s["kraken_funding"], s["hl_funding"]
        assert isinstance(kt, Bars) and isinstance(ht, Bars)
        assert isinstance(kf, Rates) and isinstance(hf, Rates)
        bars = {"Kraken": kt, "Hyperliquid": ht}
        if reasons := incomplete(w, bars, {"Kraken": kf, "Hyperliquid": hf}):
            excluded.append(Exclusion(c.base, reasons))
            continue
        leg1 = make_leg(VENUE_KRAKEN, kt, kf, grid, costs.kraken_futures_taker,
                        uni.kraken[c.kraken].maintenance_margin)
        leg2 = make_leg(VENUE_HL, ht, hf, grid, costs.hyperliquid_taker,
                        uni.hyperliquid[c.hyperliquid].maintenance_margin)
        assets.append(Asset(c.base, grid, leg1, leg2, MEAN_HOURS))
        cov.append(_coverage(c.base, w, bars))
    return assets, w, cov, excluded


def build_b(directory: Path, uni: Universe, costs: Costs, spot_fee: SpotFee) -> Built:
    w = window_b(downloaded_ms(directory))
    grid = grid_for(w)
    assets: list[Asset] = []
    cov: list[Coverage] = []
    excluded: list[Exclusion] = []
    for c in uni.selected("B"):
        s = load_series(directory, "B", c.base)
        kt, ks, kf = s["kraken_trade"], s["kraken_spot"], s["kraken_funding"]
        assert isinstance(kt, Bars) and isinstance(ks, Bars) and isinstance(kf, Rates)
        bars = {"perpetuo": kt, "índice spot": ks}
        if reasons := incomplete(w, bars, {"Kraken": kf}):
            excluded.append(Exclusion(c.base, reasons))
            continue
        leg1 = make_leg(VENUE_SPOT, ks, None, grid, costs.spot_fee(spot_fee), None)
        leg2 = make_leg(VENUE_KRAKEN, kt, kf, grid, costs.kraken_futures_taker,
                        uni.kraken[c.kraken].maintenance_margin)
        assets.append(Asset(c.base, grid, leg1, leg2, MEAN_HOURS))
        cov.append(_coverage(c.base, w, bars))
    return assets, w, cov, excluded
