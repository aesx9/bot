"""Informe Markdown (``backtest/funding/REPORT.md``) y CSV de posiciones."""

from __future__ import annotations

import csv
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from backtest.data import iso
from backtest.funding.config import (
    COST_BUFFER,
    MAX_POSITIONS,
    MEAN_HOURS,
    REBALANCE_TRIGGER,
    TRANSFER_DELAY_HOURS,
    Account,
    Costs,
    Strategy,
)
from backtest.funding.download import Universe
from backtest.funding.engine import Position
from backtest.funding.runner import (
    Segment,
    StrategyResult,
    days_to_cover,
    positions_of,
)
from backtest.funding.universe import Candidate
from backtest.report import day, mark, num, pct, table, usd

TITLE = {
    Strategy.A: "A — Entre plataformas: Kraken Futures y Hyperliquid",
    Strategy.B: "B — Cash and carry en Kraken: spot largo + perpetuo corto",
}


def musd(x: float | None) -> str:
    return "—" if x is None else num(x / 1e6, 1)


def hours_txt(h: float) -> str:
    return "n/d" if math.isnan(h) else f"{num(h, 1)} h ({num(h / 24.0, 1)} días)"


# --- universo -----------------------------------------------------------------------------


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
    """Universos seleccionados y, como contexto, los primeros excluidos."""
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


# --- informe ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Meta:
    commit: str  # commit congelado desde el que se ejecuta
    downloaded_at: str
    only_dev: bool
    account: Account
    costs: Costs


def _verdict(r: StrategyResult) -> list[str]:
    rows = [(c.name, c.detail, mark(c.passed)) for c in r.checks]
    if r.verdict is None:
        text = ("**Veredicto: sin emitir.** El tramo reservado no se ha ejecutado "
                "(`--solo-desarrollo`).")
    else:
        failed = [c.name for c in r.checks if c.passed is False]
        text = f"**Veredicto: {r.verdict.value.upper()}**"
        if r.verdict.value == "no concluyente":
            text += (" — menos de 10 ciclos completos en el reservado; el resto de criterios se "
                     "informa igual.")
        elif failed:
            text += f" (fallan: {'; '.join(failed)})."
        else:
            text += "."
    judged = " Evaluado con la pierna spot como **maker**." if r.strategy is Strategy.B else ""
    return [table(["Criterio", "Resultado", "Estado"], rows), "", text + judged, ""]


def _segment_line(s: Segment) -> str:
    return f"{day(s.start_t)} → {day(s.end_t)} ({num(s.stats.hours / 24.0, 1)} días)"


def _summary_rows(segs: Sequence[tuple[str, Segment]], rf: float, capital: float) -> list[str]:
    header = ["Tramo", "Periodo", "Neto USD", "Rent. neta", "Anualizada", "Ref. 3 % (USD)",
              "DD máx", "Posiciones", "Ciclos completos", "Duración media",
              "Diferencial capturado"]
    rows = []
    for label, s in segs:
        st = s.stats
        rows.append([
            label, _segment_line(s), usd(st.net_pnl), pct(st.net_return), pct(st.annual_return),
            usd(capital * rf * st.hours / 8760.0), pct(st.max_drawdown), str(st.positions),
            str(st.cycles), hours_txt(st.avg_hours), pct(st.avg_captured_annual),
        ])
    return [table(header, rows), ""]


def _breakdown(segs: Sequence[tuple[str, Segment]]) -> list[str]:
    header = ["Tramo", "Funding cobrado", "Funding pagado", "Comisiones", "Slippage",
              "Resultado por base", "Transferencias", "Coste transferencias", "Liquidaciones",
              "Pérdida por liquidación", "Neto USD"]
    rows = []
    for label, s in segs:
        st = s.stats
        rows.append([
            label, usd(st.funding_received), usd(-st.funding_paid), usd(-st.fees),
            usd(-st.slippage), usd(st.basis_pnl), str(st.transfers), usd(-st.transfer_cost),
            str(st.liquidations), usd(st.liquidation_loss), usd(st.net_pnl),
        ])
    note = ("Signo: positivo suma al resultado. Neto = funding cobrado − pagado − comisiones − "
            "slippage + base − transferencias (la pérdida por liquidación ya está dentro de las "
            "columnas anteriores; incluye el margen de mantenimiento perdido).")
    return [table(header, rows), "", note, ""]


def _operational(segs: Sequence[tuple[str, Segment]]) -> list[str]:
    header = ["Tramo", "Entradas descartadas (3 posiciones)", "Entradas descartadas (margen)",
              "Horas-activo sin media de 24 h", "Horas de posición sin dato de funding"]
    rows = [[label, str(s.stats.skipped_slots), str(s.stats.skipped_margin),
             str(s.stats.no_signal_hours), str(s.stats.funding_hours_missing)]
            for label, s in segs]
    return [table(header, rows), ""]


def _liquidations(segs: Sequence[tuple[str, Segment]]) -> list[str]:
    rows = [
        [label, iso(x.t), x.venue, ", ".join(x.assets), usd(-x.penalty), usd(x.loss)]
        for label, s in segs for x in s.run.liquidations
    ]
    if not rows:
        return ["Ninguna liquidación simulada en ningún tramo.", ""]
    return [table(["Tramo", "Hora", "Cuenta", "Activos", "Margen perdido", "Resultado neto"],
                  rows), ""]


def _robustness(r: StrategyResult) -> list[str]:
    rows = [
        [v.label, pct(v.thresholds.entry), pct(v.thresholds.exit), pct(v.stats.annual_return),
         pct(v.stats.max_drawdown), str(v.stats.cycles), str(v.stats.liquidations)]
        for v in r.robustness
    ]
    return [
        "Solo en desarrollo; cada umbral por separado y ambos a la vez.", "",
        table(["Variante", "Entrada", "Salida", "Anualizada", "DD máx", "Ciclos",
               "Liquidaciones"], rows), "",
    ]


def _per_asset(segs: Sequence[tuple[str, Segment]]) -> list[str]:
    assets = sorted({p.asset for _, s in segs for p in s.run.positions})
    rows = []
    for a in assets:
        for label, s in segs:
            ps = [p for p in s.run.positions if p.asset == a]
            if not ps:
                continue
            net = sum(p.net_pnl for p in ps)
            fund = sum(p.funding_received - p.funding_paid for p in ps)
            rows.append([a, label, str(len(ps)), str(sum(1 for p in ps if p.complete)),
                         usd(fund), usd(sum(p.basis_pnl for p in ps)),
                         usd(-sum(p.fees + p.slippage for p in ps)), usd(net),
                         hours_txt(statistics.fmean(p.hours for p in ps))])
    if not rows:
        return ["Sin posiciones.", ""]
    return ["Solo informativo.", "",
            table(["Activo", "Tramo", "Posiciones", "Ciclos", "Funding neto", "Base",
                   "Costes", "Neto USD", "Duración media"], rows), ""]


def _cover_b(r: StrategyResult, segs: Sequence[tuple[str, Segment]]) -> list[str]:
    out = ["### Días para cubrir los costes de un ciclo", ""]
    rows = []
    for fee, frac in r.cycle_cost.items():
        rows.append([fee, pct(frac, 3), num(days_to_cover(frac, r.thresholds.entry), 1)]
                    + [num(days_to_cover(frac, s.stats.avg_captured_annual), 1)
                       for _, s in segs])
    out += [table(["Spot", "Coste del ciclo (sobre nocional)",
                   f"Días al umbral de entrada ({pct(r.thresholds.entry, 0)})"]
                  + [f"Días al capturado medio ({label})" for label, _ in segs], rows), ""]
    out += ["Coste del ciclo = 2 × (comisión spot + comisión perpetuo) + 4 × slippage.", ""]
    rows = []
    for asset, eps in r.episodes.items():
        if eps:
            rows.append([asset, str(len(eps)), hours_txt(statistics.fmean(eps)),
                         hours_txt(statistics.median(eps)), hours_txt(float(max(eps)))])
        else:
            rows.append([asset, "0", "—", "—", "—"])
    out += ["Duración real de los periodos de funding alto (media de 24 h por encima del 10 % "
            "hasta que baja del 2 %), en toda la ventana y sin límite de posiciones:", "",
            table(["Activo", "Periodos", "Media", "Mediana", "Máximo"], rows), ""]
    return out


def _data(r: StrategyResult) -> list[str]:
    w = r.window
    out = [
        f"Ventana: {day(w.start)} → {day(w.end)} ({num(w.hours / 24.0, 1)} días, {w.hours} h). "
        f"Activos: {', '.join(r.assets)}.",
        "",
    ]
    if r.strategy is Strategy.A:
        out += ["Velas de 1h `trade` de Kraken Futures y `candleSnapshot` de Hyperliquid, funding "
                "real horario de ambas plataformas. Solo datos reales: ningún precio de "
                "Hyperliquid se sustituye por el de Kraken.", ""]
    else:
        out += ["Perpetuo: velas de 1h `trade` de Kraken Futures y funding real horario. **Spot: "
                "índice spot de la API de gráficos de Kraken Futures (`/api/charts/v1/spot/PF_*/"
                "1h`) como aproximación del precio spot de Kraken**; el índice agrega varias "
                "plataformas y no es el libro de órdenes spot de Kraken.", ""]
    out += ["**Regla de datos completos** (fijada antes de descargar): un activo entra solo si "
            "todas sus series tienen dato real en toda la ventana (una vela por hora y funding "
            "en cada hora, también en las 24 h de calentamiento). Si no, se excluye de la "
            "estrategia; nunca se rellena ni se acorta la ventana.", ""]
    if r.excluded:
        out += [table(["Activo excluido", "Motivo"],
                      [[e.asset, "; ".join(e.reasons)] for e in r.excluded]), ""]
    else:
        out += ["Ningún activo del universo excluido por datos incompletos.", ""]
    rows = [[c.asset, ", ".join(f"{k}: {v}" for k, v in c.flat_bars.items())]
            for c in r.coverage]
    out += [table(["Activo", "Velas de la fuente sin operaciones en la ventana"], rows), ""]
    return out


def _rules(r: StrategyResult, meta: Meta) -> list[str]:
    th, sp, co = r.thresholds, r.spec, meta.costs
    if r.strategy is Strategy.A:
        rule = (f"Diferencial = funding de Hyperliquid − funding de Kraken. Entra si la media de "
                f"{MEAN_HOURS} h anualizada supera {pct(th.entry, 0)} en valor absoluto (corto "
                f"donde se paga más, largo donde se paga menos); sale si baja de "
                f"{pct(th.exit, 0)} o cambia de signo.")
        alloc = (f"Capital {num(meta.account.initial_capital)} USD: una transferencia inicial "
                 f"({num(co.transfer_usd)} USD) y el resto a partes iguales entre plataformas; "
                 f"apalancamiento por pierna ≤ {num(meta.account.max_leverage_a, 0)}x. "
                 f"Nocional por pierna y posición: {num(sp.notional)} USD.")
        costs = (f"Kraken Futures taker {pct(co.kraken_futures_taker)}, Hyperliquid taker "
                 f"{pct(co.hyperliquid_taker, 3)} (tier 0), slippage {num(co.slippage * 1e4, 0)}"
                 f" pb por pierna en entrada y salida. **Transferencias: {num(co.transfer_usd)} "
                 f"USD por movimiento (supuesto conservador documentado: la retirada de "
                 f"Hyperliquid cuesta 1 USDC; la de Kraken no está verificada)**, al empezar "
                 f"cada tramo y en cada reequilibrio. Reequilibrio: si al cierre de una hora "
                 f"una plataforma tiene menos del {pct(REBALANCE_TRIGGER, 0)} de la media de "
                 f"las dos, se transfiere la mitad de la diferencia; **el importe tarda "
                 f"{TRANSFER_DELAY_HOURS} h en llegar y mientras tanto no cuenta como margen en "
                 f"ninguna plataforma** (no se lanza otro reequilibrio hasta que llega).")
    else:
        rule = (f"Entra si la media de {MEAN_HOURS} h del funding de Kraken, anualizada, supera "
                f"{pct(th.entry, 0)} (los largos pagan): largo spot + corto perpetuo. Sale si "
                f"baja de {pct(th.exit, 0)} o se vuelve negativa.")
        alloc = (f"Capital {num(meta.account.initial_capital)} USD: mitad para comprar spot y "
                 f"mitad como margen del corto (1x). Nocional por pierna y posición: "
                 f"{num(sp.notional)} USD.")
        costs = (f"Kraken spot maker {pct(co.kraken_spot_maker)} (evaluado) y taker "
                 f"{pct(co.kraken_spot_taker)} (referencia pesimista); perpetuo taker "
                 f"{pct(co.kraken_futures_taker)}; slippage {num(co.slippage * 1e4, 0)} pb por "
                 f"pierna en entrada y salida (también en la pierna maker). Sin transferencias "
                 f"entre plataformas (0 movimientos).")
    return [
        f"- **Reglas**: {rule}",
        f"- **Decisión** cada hora, a la apertura, con funding ya liquidado (las horas "
        f"anteriores completas); ejecución a la apertura de la vela de 1h de cada pierna. Sin "
        f"media completa de {MEAN_HOURS} h no hay decisión.",
        f"- **Posiciones**: máximo {MAX_POSITIONS} simultáneas, mismo nocional, misma cantidad de "
        f"base en las dos piernas. Si hay más candidatos que huecos, entran los de mayor "
        f"diferencial.",
        f"- **Capital**: {alloc} Cada cuenta reserva un {pct(COST_BUFFER, 0)} para comisiones y "
        f"funding (el nocional se divide entre {num(1 + COST_BUFFER, 2)}). Una entrada que "
        f"superase el apalancamiento máximo de su cuenta se descarta.",
        f"- **Costes**: {costs}",
        "- **Funding**: tasa relativa horaria × cantidad × apertura de la hora; sin dato en una "
        "hora, no se aplica y se cuenta.",
        "- **Liquidación**: cada hora, con los mínimos (largos) y máximos (cortos) de la vela a "
        "la vez, se compara el capital de cada cuenta con margen con su margen de mantenimiento "
        "(Kraken: primer tramo minorista del instrumento; Hyperliquid: 1 / (2 × apalancamiento "
        "máximo)). Si cae por debajo, todas las piernas de esa cuenta se cierran al extremo y se "
        "pierde el margen de mantenimiento; la cobertura se cierra al cierre de la vela.",
        "- **Tramos**: 70 % desarrollo / 30 % reservado por tiempo; cada uno empieza con el "
        "capital inicial y cierra al final lo abierto (no cuenta como ciclo completo).",
        "- **Rentabilidad anualizada** = neto / capital × 8760 / horas del tramo (simple). "
        "Referencia: 3 % anual sin riesgo sobre el mismo capital.",
        "",
    ]


def _strategy(r: StrategyResult, meta: Meta) -> list[str]:
    segs: list[tuple[str, Segment]] = [("Desarrollo", r.dev)]
    if r.reserved is not None:
        segs.append(("Reservado", r.reserved))
    capital = meta.account.initial_capital
    out = [f"## {TITLE[r.strategy]}", "", "### Veredicto", ""]
    out += _verdict(r)
    out += ["### Datos", ""] + _data(r)
    out += ["### Reglas y supuestos", ""] + _rules(r, meta)
    out += ["### Resultados", ""] + _summary_rows(segs, r.risk_free, capital)
    out += ["### Desglose", ""] + _breakdown(segs)
    out += _operational(segs)
    out += ["### Liquidaciones", ""] + _liquidations(segs)
    if r.strategy is Strategy.B and r.taker_dev is not None:
        taker: list[tuple[str, Segment]] = [("Desarrollo (spot taker)", r.taker_dev)]
        if r.taker_reserved is not None:
            taker.append(("Reservado (spot taker)", r.taker_reserved))
        out += ["### Referencia pesimista: pierna spot como taker (no se evalúa)", ""]
        out += _summary_rows(taker, r.risk_free, capital) + _breakdown(taker)
    if r.strategy is Strategy.B:
        out += _cover_b(r, segs)
    out += ["### Robustez ±20 % de los umbrales", ""] + _robustness(r)
    out += ["### Por activo", ""] + _per_asset(segs)
    return out


def render_report(results: Sequence[StrategyResult], meta: Meta) -> str:
    out = [
        "# Backtest de arbitraje de funding neutral al precio",
        "",
        "Informe generado por `python -m backtest.funding ejecutar`; no editar a mano. "
        "Posiciones en `backtest/funding/resultados/`.",
        "",
        f"- Commit congelado: `{meta.commit}`. Datos descargados: {meta.downloaded_at}.",
        "- Tramo reservado: " + ("**no ejecutado** (`--solo-desarrollo`)." if meta.only_dev
                                 else "ejecutado una sola vez."),
        "",
        "| Estrategia | Veredicto |",
        "|---|---|",
    ]
    for r in results:
        out.append(f"| {TITLE[r.strategy]} | "
                   f"{'sin emitir' if r.verdict is None else r.verdict.value} |")
    out.append("")
    for r in results:
        out += _strategy(r, meta)
    out += [
        "## Limitaciones",
        "",
        "- Universo elegido con el volumen de los 90 días previos a la descarga, que se solapan "
        "con el final de las ventanas (sesgo de selección asumido por la regla fija).",
        "- Ejecución a la apertura de la vela con slippage fijo; sin profundidad de libro.",
        "- Transferencias y reequilibrios instantáneos con coste fijo supuesto.",
        "- Liquidación en el peor caso simultáneo de todas las piernas de una cuenta.",
        "- B usa el índice spot de Kraken Futures como aproximación del spot de Kraken.",
        "",
    ]
    return "\n".join(out)


# --- CSV ----------------------------------------------------------------------------------


POSITION_HEADER = ["tramo", "activo", "direccion", "entrada", "salida", "horas", "nocional",
                   "senal_entrada", "base", "funding_cobrado", "funding_pagado", "comisiones",
                   "slippage", "penalizacion_liquidacion", "neto", "motivo_salida",
                   "horas_sin_funding"]


def position_row(segment: str, p: Position) -> list[str]:
    return [segment, p.asset, str(p.direction), iso(p.entry_t), iso(p.exit_t), str(p.hours),
            f"{p.notional:.6f}", f"{p.entry_signal:.6f}", f"{p.basis_pnl:.6f}",
            f"{p.funding_received:.6f}", f"{p.funding_paid:.6f}", f"{p.fees:.6f}",
            f"{p.slippage:.6f}", f"{p.liquidation_penalty:.6f}", f"{p.net_pnl:.6f}",
            p.reason.value, str(p.funding_hours_missing)]


def write_positions_csv(path: Path, r: StrategyResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(POSITION_HEADER)
        for seg, p in positions_of(r):
            w.writerow(position_row(seg, p))


def write_outputs(results: Sequence[StrategyResult], meta: Meta, out_dir: Path,
                  report_path: Path) -> None:
    for r in results:
        write_positions_csv(out_dir / f"posiciones_{r.strategy.value}.csv", r)
    report_path.write_text(render_report(results, meta), encoding="utf-8")
