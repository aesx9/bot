"""Universo: regla fija de volumen, emparejamiento de bases y par spot."""

from __future__ import annotations

from backtest.funding.config import DAY_MS
from backtest.funding.data import Bars, HlPerp, KrakenPerp
from backtest.funding.tests.helpers import T0
from backtest.funding.universe import (
    avg_daily_volume_usd,
    price_ratio,
    universe_a,
    universe_b,
    volume_days,
)

NOW = T0 + 100 * DAY_MS + 123
DAYS = volume_days(NOW)


def daily(name: str, usd_per_day: float, price: float = 10.0, days: int = 90) -> Bars:
    t = DAYS[-days:]
    n = len(t)
    return Bars(name, DAY_MS, list(t), [price] * n, [price] * n, [price] * n, [price] * n,
                [usd_per_day / price] * n)


def test_volume_days_are_the_last_90_complete_utc_days() -> None:
    assert len(DAYS) == 90
    assert DAYS[-1] == NOW - NOW % DAY_MS - DAY_MS
    assert all(b - a == DAY_MS for a, b in zip(DAYS, DAYS[1:], strict=False))


def test_missing_days_count_as_zero_volume() -> None:
    assert avg_daily_volume_usd(daily("x", 20e6, days=45), DAYS) == 10e6
    assert avg_daily_volume_usd(None, DAYS) == 0.0


def test_universe_a_requires_volume_on_both_venues_and_comparable_prices() -> None:
    kraken = [KrakenPerp(f"PF_{b}USD", b, 0.01) for b in ("BTC", "ETH", "SOL", "PEPE", "ONLY")]
    hl = [HlPerp("BTC", "BTC", 1, 40), HlPerp("ETH", "ETH", 1, 25), HlPerp("SOL", "SOL", 1, 20),
          HlPerp("kPEPE", "PEPE", 1000, 10)]
    kd = {"PF_BTCUSD": daily("k", 50e6), "PF_ETHUSD": daily("k", 50e6),
          "PF_SOLUSD": daily("k", 9.9e6), "PF_PEPEUSD": daily("k", 30e6, price=1e-5),
          "PF_ONLYUSD": daily("k", 99e6)}
    hd = {"BTC": daily("h", 80e6), "ETH": daily("h", 80e6, price=10.5),
          "SOL": daily("h", 80e6), "kPEPE": daily("h", 30e6, price=1.0001e-5)}
    rows = {c.base: c for c in universe_a(kraken, hl, kd, hd, DAYS)}
    assert "ONLY" not in rows  # sin perpetuo en Hyperliquid
    assert rows["BTC"].selected and rows["PEPE"].selected
    assert not rows["ETH"].selected and "precios" in rows["ETH"].reason
    assert not rows["SOL"].selected and "Kraken" in rows["SOL"].reason
    assert price_ratio(kd["PF_ETHUSD"], hd["ETH"], DAYS) == 1.05


def test_universe_b_requires_spot_pair_and_perp_volume() -> None:
    kraken = [KrakenPerp("PF_XBTUSD", "BTC", 0.01), KrakenPerp("PF_ABCUSD", "ABC", 0.01),
              KrakenPerp("PF_LOWUSD", "LOW", 0.01)]
    kd = {"PF_XBTUSD": daily("k", 50e6), "PF_ABCUSD": daily("k", 50e6),
          "PF_LOWUSD": daily("k", 1e6)}
    rows = {c.base: c for c in universe_b(kraken, kd, {"BTC", "LOW"}, DAYS)}
    assert rows["BTC"].selected
    assert not rows["ABC"].selected and rows["ABC"].has_spot is False
    assert not rows["LOW"].selected
    unchecked = universe_b(kraken, kd, None, DAYS)
    assert not any(c.selected for c in unchecked)
    assert all("sin comprobar" in c.reason for c in unchecked)
