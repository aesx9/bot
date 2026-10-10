"""Utilidades de test: activos sintéticos en una rejilla horaria, sin red."""

from __future__ import annotations

from collections.abc import Sequence

from backtest.funding.config import HOUR_MS, HOURS_PER_YEAR, MEAN_HOURS
from backtest.funding.engine import Asset, Leg, Spec

T0 = 1_773_619_200_000  # 2026-03-16T00:00:00Z


def hourly(annual: float) -> float:
    """Tasa horaria equivalente a ``annual`` anualizado."""
    return annual / HOURS_PER_YEAR


def make_leg(
    n: int,
    venue: str,
    *,
    price: float = 100.0,
    prices: Sequence[float] | None = None,
    highs: dict[int, float] | None = None,
    lows: dict[int, float] | None = None,
    rate: float = 0.0,
    rates: Sequence[float] | None = None,
    fee: float = 0.0,
    mm: float | None = 0.01,
) -> Leg:
    """Velas planas (o = h = l = c) salvo los extremos indicados en ``highs``/``lows``."""
    px = list(prices) if prices is not None else [price] * n
    h = [max(p, (highs or {}).get(i, p)) for i, p in enumerate(px)]
    lo = [min(p, (lows or {}).get(i, p)) for i, p in enumerate(px)]
    rr = list(rates) if rates is not None else [rate] * n
    return Leg(venue, list(px), h, lo, list(px), rr, fee, mm)


def make_asset(name: str, leg1: Leg, leg2: Leg) -> Asset:
    n = len(leg1.o)
    return Asset(name, [T0 + i * HOUR_MS for i in range(n)], leg1, leg2, MEAN_HOURS)


def spread_asset(
    name: str,
    spread: Sequence[float],
    *,
    venues: tuple[str, str] = ("v1", "v2"),
    fees: tuple[float, float] = (0.0, 0.0),
    prices2: Sequence[float] | None = None,
    highs2: dict[int, float] | None = None,
    mm: tuple[float | None, float | None] = (0.01, 0.01),
) -> Asset:
    """Activo con funding 0 en ``leg1`` y ``spread`` (tasas horarias) en ``leg2``."""
    n = len(spread)
    leg1 = make_leg(n, venues[0], fee=fees[0], mm=mm[0])
    leg2 = make_leg(n, venues[1], rates=spread, fee=fees[1], prices=prices2, highs=highs2,
                    mm=mm[1])
    return make_asset(name, leg1, leg2)


def make_spec(
    *,
    two_sided: bool = True,
    capital: float = 1000.0,
    leverage: float = 2.0,
    notional: float = 300.0,
    slippage: float = 0.0,
    rebalance: bool = False,
    transfer_cost: float = 0.0,
    initial_transfers: int = 0,
    max_positions: int = 3,
) -> Spec:
    half = capital / 2.0
    return Spec(
        two_sided=two_sided,
        initial={"v1": half, "v2": half},
        leverage={"v1": leverage, "v2": leverage},
        notional=notional,
        slippage=slippage,
        rebalance=rebalance,
        rebalance_trigger=0.5,
        transfer_cost=transfer_cost,
        initial_transfers=initial_transfers,
        max_positions=max_positions,
    )


def step_spread(n: int, annual: float, until: int) -> list[float]:
    """Diferencial ``annual`` en las horas ``< until`` y 0 después."""
    return [hourly(annual) if i < until else 0.0 for i in range(n)]
