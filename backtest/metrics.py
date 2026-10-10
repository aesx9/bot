"""Métricas de resultados: rentabilidad, acierto, profit factor, drawdown, Sharpe y costes."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from backtest.config import DAY_MS, Account, Costs
from backtest.data import Candles
from backtest.engine import Segment, Trade
from backtest.funding import FundingTable

TRADING_DAYS = 365  # perpetuos cripto: 24/7


@dataclass(frozen=True)
class Stats:
    net_pnl: float
    net_return: float  # sobre el capital inicial de la cuenta
    n_trades: int
    win_rate: float  # NaN sin operaciones
    profit_factor: float  # NaN sin operaciones; inf sin pérdidas
    max_drawdown: float  # fracción positiva (0.10 = -10 %)
    sharpe: float  # anualizado sobre rendimientos diarios; NaN si no es calculable
    gross_pnl: float
    fees: float
    slippage: float
    funding_real: float
    funding_imputed: float
    funding_hours_real: int
    funding_hours_imputed: int


def max_drawdown(equity: Sequence[float]) -> float:
    """Mayor caída relativa desde un máximo previo. ``equity`` incluye el valor inicial."""
    peak = -math.inf
    worst = 0.0
    for x in equity:
        peak = max(peak, x)
        if peak > 0.0:
            worst = max(worst, (peak - x) / peak)
    return worst


def profit_factor(pnls: Sequence[float]) -> float:
    if not pnls:
        return math.nan
    wins = sum(x for x in pnls if x > 0.0)
    losses = -sum(x for x in pnls if x < 0.0)
    if losses == 0.0:
        return math.inf if wins > 0.0 else math.nan
    return wins / losses


def daily_equity(equity: Sequence[float], times_ms: Sequence[int]) -> list[float]:
    """Último valor de cada día UTC (el cierre de la última vela del día)."""
    out: list[float] = []
    last_day: int | None = None
    for x, t in zip(equity, times_ms, strict=True):
        day = t // DAY_MS
        if day == last_day:
            out[-1] = x
        else:
            out.append(x)
            last_day = day
    return out


def sharpe_daily(equity: Sequence[float], times_ms: Sequence[int], initial: float) -> float:
    """Sharpe anualizado (sin tasa libre de riesgo) de los rendimientos diarios del capital."""
    series = [initial, *daily_equity(equity, times_ms)]
    returns = [b / a - 1.0 for a, b in zip(series, series[1:], strict=False) if a > 0.0]
    if len(returns) < 3:
        return math.nan
    sd = statistics.stdev(returns)
    if sd == 0.0:
        return math.nan
    return statistics.fmean(returns) / sd * math.sqrt(TRADING_DAYS)


def summarize(
    trades: Sequence[Trade],
    curve: Sequence[float] | None,
    times_ms: Sequence[int],
    initial: float,
) -> Stats:
    """Estadísticos de un conjunto de operaciones. ``curve`` es el capital al cierre de cada vela
    (``None`` si no se calculó: drawdown y Sharpe salen NaN)."""
    pnls = [t.net_pnl for t in trades]
    net = sum(pnls)
    return Stats(
        net_pnl=net,
        net_return=net / initial,
        n_trades=len(trades),
        win_rate=(sum(1 for x in pnls if x > 0.0) / len(pnls)) if pnls else math.nan,
        profit_factor=profit_factor(pnls),
        max_drawdown=max_drawdown([initial, *curve]) if curve is not None else math.nan,
        sharpe=sharpe_daily(curve, times_ms, initial) if curve is not None else math.nan,
        gross_pnl=sum(t.gross_pnl for t in trades),
        fees=sum(t.fees for t in trades),
        slippage=sum(t.slippage for t in trades),
        funding_real=sum(t.funding_real for t in trades),
        funding_imputed=sum(t.funding_imputed for t in trades),
        funding_hours_real=sum(t.funding_hours_real for t in trades),
        funding_hours_imputed=sum(t.funding_hours_imputed for t in trades),
    )


@dataclass(frozen=True)
class BuyHold:
    gross_return: float  # solo movimiento de precio
    net_return: float  # tras comisión y slippage de entrada y salida, y funding de un largo 1x
    max_drawdown: float  # sobre el precio de cierre, sin costes


def buy_and_hold(
    c: Candles, seg: Segment, fund: FundingTable, costs: Costs, account: Account
) -> BuyHold:
    """Largo 1x con todo el capital, de la apertura del tramo al cierre de su última vela."""
    entry_ref, exit_ref = c.o[seg.start], c.c[seg.end - 1]
    entry_fill = entry_ref * (1.0 + costs.slippage)
    exit_fill = exit_ref * (1.0 - costs.slippage)
    qty = account.initial_capital / entry_fill
    real, imp = fund.cost_per_unit(1, seg.start, seg.end - 1)
    net = qty * (exit_fill - entry_fill) - costs.fee * qty * (entry_fill + exit_fill)
    net -= qty * (real + imp)
    equity = [account.initial_capital * c.c[j] / entry_ref for j in range(seg.start, seg.end)]
    return BuyHold(
        gross_return=exit_ref / entry_ref - 1.0,
        net_return=net / account.initial_capital,
        max_drawdown=max_drawdown([account.initial_capital, *equity]),
    )
