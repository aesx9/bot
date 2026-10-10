"""Prueba diferencial: un simulador ingenuo de un solo activo debe coincidir con el motor."""

from __future__ import annotations

import pytest

from backtest.config import Account, Costs, Params
from backtest.data import Candles
from backtest.engine import Trade
from backtest.signals import Signal, generate_signals
from backtest.tests.helpers import random_walk, run


def reference(
    c: Candles, signals: list[Signal], p: Params, k: Costs, acc: Account
) -> list[tuple[int, int, str, float]]:
    """Recorre vela a vela con un estado explícito. Sin funding. Devuelve
    ``(entrada, salida, motivo, neto)`` por operación."""
    by_entry = {s.idx + 1: s for s in signals}
    balance = acc.initial_capital
    pos: dict[str, float] | None = None
    out: list[tuple[int, int, str, float]] = []
    last = len(c) - 1
    for j in range(len(c)):
        if pos is None and j in by_entry:
            s = by_entry[j]
            fill = c.o[j] * (1 + s.side * k.slippage)
            qty = acc.risk_per_trade * balance / (p.stop_atr * s.atr)
            if qty * fill > acc.max_leverage * balance:
                qty = acc.max_leverage * balance / fill
            pos = {
                "side": s.side, "qty": qty, "ref": c.o[j], "fill": fill, "entry": j,
                "stop": fill - s.side * p.stop_atr * s.atr,
                "tp": fill + s.side * p.tp_atr * s.atr,
            }
        if pos is None:
            continue
        side = pos["side"]
        stop_hit = c.l[j] <= pos["stop"] if side > 0 else c.h[j] >= pos["stop"]
        tp_hit = c.h[j] >= pos["tp"] if side > 0 else c.l[j] <= pos["tp"]
        ref = reason = None
        if stop_hit:  # el stop manda aunque en la misma vela se alcance el take profit
            gap = c.o[j]
            ref = min(pos["stop"], gap) if side > 0 else max(pos["stop"], gap)
            reason = "stop"
        elif tp_hit:
            ref, reason = pos["tp"], "tp"
        elif j == last:
            ref, reason = c.c[j], "fin_de_tramo"
        if ref is None:
            continue
        exit_fill = ref * (1 - side * k.slippage)
        qty = pos["qty"]
        pnl = side * qty * (exit_fill - pos["fill"]) - k.fee * qty * (pos["fill"] + exit_fill)
        balance += pnl
        out.append((int(pos["entry"]), j, str(reason), pnl))
        pos = None
    return out


def _engine(trades: list[Trade]) -> list[tuple[int, int, str, float]]:
    return [(t.entry_idx, t.exit_idx, t.exit_reason, t.net_pnl) for t in trades]


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("costs", [Costs(fee=0.0, slippage=0.0), Costs()])
def test_engine_matches_the_naive_reference(seed: int, costs: Costs) -> None:
    c = random_walk(1500, seed=seed, symbol="A", vol=0.015)
    p, acc = Params(), Account()
    sigs = generate_signals(c, p)
    expected = reference(c, sigs, p, costs, acc)
    got = _engine(run({"A": c}, {"A": sigs}, costs=costs, account=acc, params=p).trades)
    assert len(expected) > 5, "la serie debe producir operaciones"
    assert [e[:3] for e in got] == [e[:3] for e in expected]
    assert [e[3] for e in got] == pytest.approx([e[3] for e in expected])
