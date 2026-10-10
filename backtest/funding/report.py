"""Informe Markdown del arbitraje de funding (``backtest/funding/REPORT.md``)."""

from __future__ import annotations

import math

from backtest.funding.download import Universe
from backtest.funding.universe import Candidate
from backtest.report import day, num, table


def musd(x: float | None) -> str:
    return "—" if x is None else num(x / 1e6, 1)


def _universe_table(rows: list[Candidate], with_hl: bool) -> str:
    if with_hl:
        header = ["Base", "Kraken", "Hyperliquid", "Vol. Kraken (M USD/día)",
                  "Vol. Hyperliquid (M USD/día)", "Precio HL/Kraken", "Excluido por"]
        body = [
            [c.base, c.kraken, c.hyperliquid or "—", musd(c.vol_kraken), musd(c.vol_hyperliquid),
             "n/d" if c.price_ratio is None or math.isnan(c.price_ratio)
             else num(c.price_ratio, 4), c.reason or "—"]
            for c in rows
        ]
    else:
        header = ["Base", "Kraken", "Vol. perpetuo (M USD/día)", "Par spot USD", "Excluido por"]
        body = [
            [c.base, c.kraken, musd(c.vol_kraken),
             "sin comprobar" if c.has_spot is None else ("sí" if c.has_spot else "no"),
             c.reason or "—"]
            for c in rows
        ]
    return table(header, body)


def render_universe(u: Universe, show_excluded: int = 15) -> str:
    """Universos seleccionados y, como contexto, los primeros excluidos por volumen."""
    period = f"{day(u.days[0])} → {day(u.days[-1])} ({len(u.days)} días completos)"
    out = [f"Volumen diario medio de {period}; umbral 10 M USD.", ""]
    for rows, with_hl in ((u.a, True), (u.b, False)):
        sel = [c for c in rows if c.selected]
        exc = [c for c in rows if not c.selected]
        title = ("A — Kraken Futures y Hyperliquid" if with_hl
                 else "B — Kraken: perpetuo PF_ y par spot")
        out += [f"### Universo {title}: {len(sel)} activos", ""]
        out += [_universe_table(sel, with_hl) if sel else "(vacío)", ""]
        if exc:
            out += [f"Primeros {min(show_excluded, len(exc))} de {len(exc)} excluidos:", ""]
            out += [_universe_table(exc[:show_excluded], with_hl), ""]
    if not u.spot_checked:
        out += [f"⚠️ Pares spot de Kraken sin comprobar: {u.spot_error}", ""]
    return "\n".join(out)
