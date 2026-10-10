"""Selección del universo de cada estrategia con una regla fija (nada a mano).

- A: perpetuo en Kraken Futures (``PF_``) y en Hyperliquid con la misma base, y volumen diario
  medio ≥ 10 M USD en cada plataforma en los últimos 90 días completos.
- B: perpetuo ``PF_`` con par spot ``BASE/USD`` en Kraken y el mismo umbral en el perpetuo.

Volumen diario en USD = volumen de la vela diaria (unidades de base) × cierre. Los días sin vela
cuentan como volumen cero (un activo listado hace menos de 90 días no llega al umbral por poco
volumen que le falte). Para emparejar bases entre plataformas se exige además que la mediana del
cociente de cierres diarios esté a menos del 2 % de 1 (detecta unidades distintas).
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from backtest.funding.config import DAY_MS, MAX_PRICE_RATIO_DEV, MIN_DAILY_VOLUME_USD, UNIVERSE_DAYS
from backtest.funding.data import Bars, HlPerp, KrakenPerp


def volume_days(now_ms: int, days: int = UNIVERSE_DAYS) -> list[int]:
    """Aperturas de los ``days`` últimos días UTC completos antes de ``now_ms``."""
    today = now_ms - now_ms % DAY_MS
    return [today - (days - i) * DAY_MS for i in range(days)]


def avg_daily_volume_usd(bars: Bars | None, days: Sequence[int]) -> float:
    if bars is None:
        return 0.0
    by_day = {t: bars.v[i] * bars.c[i] for i, t in enumerate(bars.t)}
    return sum(by_day.get(d, 0.0) for d in days) / len(days)


def price_ratio(a: Bars, b: Bars, days: Sequence[int]) -> float:
    """Mediana de ``cierre b / cierre a`` en los días comunes (NaN si no hay ninguno)."""
    ca = {t: a.c[i] for i, t in enumerate(a.t)}
    cb = {t: b.c[i] for i, t in enumerate(b.t)}
    ratios = [cb[d] / ca[d] for d in days if d in ca and d in cb]
    return statistics.median(ratios) if ratios else float("nan")


@dataclass(frozen=True)
class Candidate:
    base: str
    kraken: str  # PF_...
    hyperliquid: str | None  # coin de Hyperliquid (solo A)
    vol_kraken: float  # USD/día, media de 90 días
    vol_hyperliquid: float | None
    price_ratio: float | None  # Hyperliquid / Kraken (solo A)
    has_spot: bool | None  # par BASE/USD en Kraken spot (solo B)
    selected: bool
    reason: str  # por qué se excluye ("" si entra)


def universe_a(
    kraken: Sequence[KrakenPerp],
    hl: Sequence[HlPerp],
    kraken_daily: Mapping[str, Bars],
    hl_daily: Mapping[str, Bars],
    days: Sequence[int],
) -> list[Candidate]:
    hl_by_base = {p.base: p for p in hl}
    out: list[Candidate] = []
    for k in kraken:
        h = hl_by_base.get(k.base)
        if h is None:
            continue
        vk = avg_daily_volume_usd(kraken_daily.get(k.symbol), days)
        vh = avg_daily_volume_usd(hl_daily.get(h.coin), days)
        kb, hb = kraken_daily.get(k.symbol), hl_daily.get(h.coin)
        ratio = price_ratio(kb, hb, days) if kb is not None and hb is not None else float("nan")
        reasons = []
        if vk < MIN_DAILY_VOLUME_USD:
            reasons.append("volumen Kraken < 10 M")
        if vh < MIN_DAILY_VOLUME_USD:
            reasons.append("volumen Hyperliquid < 10 M")
        if not abs(ratio - 1.0) < MAX_PRICE_RATIO_DEV:
            reasons.append("precios no comparables")
        out.append(
            Candidate(
                k.base, k.symbol, h.coin, vk, vh, ratio, None, not reasons, "; ".join(reasons)
            )
        )
    return sorted(out, key=lambda c: (not c.selected, -min(c.vol_kraken, c.vol_hyperliquid or 0)))


def universe_b(
    kraken: Sequence[KrakenPerp],
    kraken_daily: Mapping[str, Bars],
    spot_bases: set[str] | None,
    days: Sequence[int],
) -> list[Candidate]:
    """``spot_bases`` es ``None`` si no se pudo consultar la lista de pares spot."""
    out: list[Candidate] = []
    for k in kraken:
        vk = avg_daily_volume_usd(kraken_daily.get(k.symbol), days)
        has_spot = None if spot_bases is None else k.base in spot_bases
        reasons = []
        if vk < MIN_DAILY_VOLUME_USD:
            reasons.append("volumen Kraken < 10 M")
        if has_spot is False:
            reasons.append("sin par spot BASE/USD")
        elif has_spot is None:
            reasons.append("par spot sin comprobar")
        out.append(Candidate(k.base, k.symbol, None, vk, None, None, has_spot, not reasons,
                             "; ".join(reasons)))
    return sorted(out, key=lambda c: (not c.selected, -c.vol_kraken))
