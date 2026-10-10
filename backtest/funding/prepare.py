"""Alineación de las series descargadas en la rejilla horaria de cada estrategia.

- A: desde la primera hora en la que todos los activos tienen velas en ambas plataformas (no antes
  del 2026-03-16) hasta 208 días después o la última hora común, lo que llegue antes.
- B: los últimos 365 días que cubren todas las series de Kraken.

Cada activo lleva ``MEAN_HOURS`` horas previas de calentamiento para la media de 24 h (solo su
funding se usa; los precios de esas horas no se operan). Nunca se sustituyen precios de una
plataforma por los de otra: si falta una vela en la rejilla común, es un error.
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
from backtest.funding.data import Bars, Rates
from backtest.funding.download import Universe, load_series
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
    filled_bars: dict[str, int]  # velas rellenadas por la fuente (sin operaciones), por serie
    missing_rates: dict[str, int]  # horas de la ventana (con calentamiento) sin funding


def window_a(bars: Sequence[Bars]) -> Window:
    first = max(max(b.t[0] for b in bars), WINDOW_A_START_MS)
    last_end = min(b.t[-1] for b in bars) + HOUR_MS
    return Window(first, min(first + WINDOW_A_DAYS * DAY_MS, last_end))


def window_b(bars: Sequence[Bars]) -> Window:
    end = min(b.t[-1] for b in bars) + HOUR_MS
    start = end - WINDOW_B_DAYS * DAY_MS
    if max(b.t[0] for b in bars) > start:
        raise DataError(f"las velas de B no cubren 365 días hasta {iso(end)}")
    return Window(start, end)


def _prices(b: Bars, grid: Sequence[int]) -> tuple[list[float], ...]:
    idx = {t: i for i, t in enumerate(b.t)}
    o: list[float] = []
    h: list[float] = []
    lo: list[float] = []
    c: list[float] = []
    for t in grid:
        i = idx.get(t)
        if i is None:
            if t >= grid[MEAN_HOURS]:
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


def _coverage(name: str, grid: Sequence[int], bars: Mapping[str, Bars],
              rates: Mapping[str, Rates]) -> Coverage:
    lo, hi = grid[MEAN_HOURS], grid[-1]
    filled = {}
    for key, b in bars.items():
        # ``filled`` cuenta toda la serie descargada; aquí solo interesa la ventana.
        filled[key] = sum(
            1 for i, t in enumerate(b.t)
            if lo <= t <= hi and b.v[i] == 0.0 and b.o[i] == b.h[i] == b.l[i] == b.c[i]
        )
    missing = {key: sum(1 for x in _rates(r, grid) if math.isnan(x)) for key, r in rates.items()}
    return Coverage(name, filled, missing)


def build_a(directory: Path, uni: Universe, costs: Costs) -> tuple[list[Asset], Window,
                                                                    list[Coverage]]:
    sel = uni.selected("A")
    series = {c.base: load_series(directory, "A", c.base) for c in sel}
    all_bars = [s[k] for s in series.values() for k in ("kraken_trade", "hl_trade")]
    w = window_a([b for b in all_bars if isinstance(b, Bars)])
    grid = grid_for(w)
    assets: list[Asset] = []
    cov: list[Coverage] = []
    for c in sel:
        s = series[c.base]
        assert c.hyperliquid is not None
        kt, ht, kf, hf = s["kraken_trade"], s["hl_trade"], s["kraken_funding"], s["hl_funding"]
        assert isinstance(kt, Bars) and isinstance(ht, Bars)
        assert isinstance(kf, Rates) and isinstance(hf, Rates)
        leg1 = make_leg(VENUE_KRAKEN, kt, kf, grid, costs.kraken_futures_taker,
                        uni.kraken[c.kraken].maintenance_margin)
        leg2 = make_leg(VENUE_HL, ht, hf, grid, costs.hyperliquid_taker,
                        uni.hyperliquid[c.hyperliquid].maintenance_margin)
        assets.append(Asset(c.base, grid, leg1, leg2, MEAN_HOURS))
        cov.append(_coverage(c.base, grid, {"Kraken": kt, "Hyperliquid": ht},
                             {"Kraken": kf, "Hyperliquid": hf}))
    return assets, w, cov


def build_b(
    directory: Path, uni: Universe, costs: Costs, spot_fee: SpotFee
) -> tuple[list[Asset], Window, list[Coverage]]:
    sel = uni.selected("B")
    series = {c.base: load_series(directory, "B", c.base) for c in sel}
    all_bars = [s[k] for s in series.values() for k in ("kraken_trade", "kraken_spot")]
    w = window_b([b for b in all_bars if isinstance(b, Bars)])
    grid = grid_for(w)
    assets: list[Asset] = []
    cov: list[Coverage] = []
    for c in sel:
        s = series[c.base]
        kt, ks, kf = s["kraken_trade"], s["kraken_spot"], s["kraken_funding"]
        assert isinstance(kt, Bars) and isinstance(ks, Bars) and isinstance(kf, Rates)
        leg1 = make_leg(VENUE_SPOT, ks, None, grid, costs.spot_fee(spot_fee), None)
        leg2 = make_leg(VENUE_KRAKEN, kt, kf, grid, costs.kraken_futures_taker,
                        uni.kraken[c.kraken].maintenance_margin)
        assets.append(Asset(c.base, grid, leg1, leg2, MEAN_HOURS))
        cov.append(_coverage(c.base, grid, {"perpetuo": kt, "índice spot": ks},
                             {"Kraken": kf}))
    return assets, w, cov
