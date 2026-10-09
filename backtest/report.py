"""Informe Markdown y CSV de operaciones a partir de ``Results``."""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable, Sequence
from pathlib import Path

from backtest.config import CANDLE_MS, Scenario
from backtest.data import iso
from backtest.engine import Trade
from backtest.metrics import Stats
from backtest.runner import DEV, RESERVED, TOTAL, Results, SegmentOutcome

SEGMENT_LABEL = {DEV: "Desarrollo", RESERVED: "Reservado"}
SCENARIO_LABEL = {Scenario.PESIMISTA: "pesimista", Scenario.CENTRAL: "central"}


# --- formato -----------------------------------------------------------------------------


def num(x: float, decimals: int = 2) -> str:
    if math.isnan(x):
        return "n/d"
    if math.isinf(x):
        return "∞"
    return f"{x:.{decimals}f}".replace(".", ",")


def pct(x: float, decimals: int = 2) -> str:
    return "n/d" if math.isnan(x) else num(x * 100.0, decimals) + " %"


def usd(x: float) -> str:
    if math.isnan(x):
        return "n/d"
    return "0,00" if abs(x) < 0.005 else f"{x:+.2f}".replace(".", ",")


def table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def day(ms: int) -> str:
    return iso(ms)[:10]


def mark(passed: bool | None) -> str:
    return "n/e" if passed is None else ("✅ cumple" if passed else "❌ no cumple")


# --- secciones ---------------------------------------------------------------------------


def _verdict(r: Results) -> str:
    sc = r.criteria.judged_scenario
    rows = [(c.name, c.detail, mark(c.passed)) for c in r.checks]
    verdict = r.verdict
    if verdict is None:
        text = "**Sin veredicto**: ejecución parcial (`--solo-desarrollo`)."
    elif verdict:
        text = (
            "**Veredicto: cumple todos los criterios.** Hay indicios de ventaja; no es una prueba "
            "(ver limitaciones), y el siguiente paso sería validarla fuera de muestra con datos "
            "nuevos antes de construir nada."
        )
    else:
        failed = [c.name for c in r.checks if c.passed is False]
        text = (
            "**Veredicto: NO supera los criterios** (fallan: " + "; ".join(failed) + "). "
            "Con estas reglas fijas no se ha demostrado ventaja real."
        )
    return "\n".join(
        [
            "## 1. Veredicto",
            "",
            f"Criterios fijados antes de ver resultados, evaluados con el escenario de funding "
            f"**{SCENARIO_LABEL[sc]}**. El tramo reservado se ejecuta una sola vez con los "
            "parámetros base; la robustez ±20 % se mide solo en desarrollo.",
            "",
            table(["Criterio", "Resultado", "Estado"], rows),
            "",
            text,
        ]
    )


def _data(r: Results) -> str:
    rows = []
    for a, m in r.meta.items():
        rows.append(
            [
                a,
                str(m.n_candles),
                f"{day(m.first_ms)} → {day(m.last_open_ms + CANDLE_MS)}",
                num(m.candle_years),
                f"{m.funding.n_hours}",
                f"{day(m.funding.first_ms)} → {day(m.funding.last_ms)}",
                num(m.funding_years),
            ]
        )
    stats_rows = [
        [
            a,
            f"{m.funding.median_rel * 1e6:+.3f}".replace(".", ","),
            f"{m.funding.p75_rel * 1e6:+.3f}".replace(".", ","),
            f"{abs(m.funding.p75_rel) * 1e6:.3f}".replace(".", ","),
            f"{m.funding.p75_abs_rel * 1e6:.3f}".replace(".", ","),
        ]
        for a, m in r.meta.items()
    ]
    dev = r.segments[DEV]
    lines = [
        "## 2. Datos",
        "",
        "Velas de 4h tipo `trade` y funding horario de la API pública de Kraken Futures "
        "(`api/charts/v1` y `historical-funding-rates`), guardados en `backtest/datos/` para "
        "reproducir sin red. La vela en curso en el momento de la descarga se descartó.",
        "",
        table(
            ["Activo", "Velas 4h", "Rango", "Años de velas", "Horas de funding",
             "Rango de funding", "Años de funding"],
            rows,
        ),
        "",
        f"- Corte 70/30 por tiempo: desarrollo = primeras {dev.end} velas "
        f"({day(r.times_ms[0])} → {day(r.times_ms[dev.end - 1] + CANDLE_MS)}); "
        f"reservado = {len(r.times_ms) - dev.end} velas restantes "
        f"({day(r.times_ms[dev.end])} → {day(r.times_ms[-1] + CANDLE_MS)}).",
        f"- **Funding: Kraken solo publica ≈{num(max(m.funding_years for m in r.meta.values()), 1)}"
        " año; el endpoint no admite rangos ni paginación.** Donde no hay dato se imputa "
        "(ver sección 5) y los dos escenarios se informan por separado.",
        "",
        "Tasas relativas horarias del año real de funding (en millonésimas por hora; "
        "positivo = pagan los largos). El escenario pesimista aplica `|P75|` siempre en contra "
        "de la posición; el central, la mediana con signo.",
        "",
        table(
            ["Activo", "Mediana", "P75 (con signo)", "Abs. del P75 (usado)",
             "P75 de abs. (no usado)"],
            stats_rows,
        ),
        "",
        "El escenario pesimista usa la lectura literal `|P75|` (valor absoluto del percentil 75 de "
        "la tasa con signo). La columna «P75 de abs.» es la lectura alternativa (percentil 75 "
        "de las tasas en valor absoluto), más dura; se muestra solo como referencia.",
    ]
    return "\n".join(lines)


def _rules(r: Results) -> str:
    p, k, a = r.params, r.costs, r.account
    return "\n".join(
        [
            "## 3. Reglas y supuestos",
            "",
            f"- **Régimen**: cierre > SMA{p.sma_len} → solo largos; cierre < SMA{p.sma_len} → "
            "solo cortos.",
            f"- **Entrada**: RSI estocástico ({p.rsi_len}, {p.stoch_len}, {p.k_smooth}, "
            f"{p.d_smooth}) con RSI de Wilder. Largo si %K cruza por encima de %D y %K de la vela "
            f"anterior estaba por debajo de {num(p.oversold, 0)}; corto si %K cruza por debajo de "
            f"%D y %K de la vela anterior estaba por encima de {num(p.overbought, 0)}. "
            "(Interpretación de «habiendo estado por debajo/encima»: la vela inmediatamente "
            "anterior al cruce.)",
            "- **Ejecución**: señal al cierre de la vela, entrada a la apertura de la siguiente; "
            "nunca se usan datos de la vela en curso.",
            f"- **Salidas**: stop a {num(p.stop_atr, 1)}×ATR({p.atr_len}) y take profit a "
            f"{num(p.tp_atr, 1)}×ATR desde el precio de entrada (ATR de la vela de la señal). "
            "Stop y TP en la misma vela → salta el stop. Hueco más allá del stop → se ejecuta "
            "a la apertura. Sin salida por tiempo; al final del tramo se cierra al cierre.",
            f"- **Tamaño**: se arriesga el {pct(a.risk_per_trade, 0)} del capital realizado hasta "
            f"el stop; nocional total abierto ≤ {num(a.max_leverage, 0)}× el capital (compartido "
            "entre los tres activos, que comparten una cartera única); una posición por activo. "
            f"Capital inicial {num(a.initial_capital, 0)} USD en cada tramo.",
            f"- **Costes**: comisión taker {pct(k.fee)} y slippage {num(k.slippage * 1e4, 0)} pb "
            "en contra, en cada ejecución (entrada y salida, también en TP). Funding horario "
            "mientras la posición está abierta, con la granularidad de la vela de 4h.",
        ]
    )


STATS_HEADER = ["Activo", "Rent. neta", "Neto USD", "Ops", "Acierto", "Profit factor",
                "DD máx", "Sharpe"]
COST_HEADER = ["Activo", "Bruto USD", "Comisiones", "Slippage", "Funding real",
               "Funding imputado", "Neto USD"]


def _stats_row(label: str, s: Stats) -> list[str]:
    return [
        label, pct(s.net_return), usd(s.net_pnl), str(s.n_trades), pct(s.win_rate, 1),
        num(s.profit_factor), pct(s.max_drawdown), num(s.sharpe),
    ]


def _cost_row(label: str, s: Stats) -> list[str]:
    return [
        label, usd(s.gross_pnl), usd(-s.fees), usd(-s.slippage), usd(-s.funding_real),
        usd(-s.funding_imputed), usd(s.net_pnl),
    ]


def _segment_tables(o: SegmentOutcome) -> list[str]:
    order = [*o.stats.keys()]
    order.remove(TOTAL)
    order.append(TOTAL)
    return [
        table(STATS_HEADER, [_stats_row(a, o.stats[a]) for a in order]),
        "",
        "Desglose de costes (negativo = coste; el bruto es el movimiento de precio sin costes):",
        "",
        table(COST_HEADER, [_cost_row(a, o.stats[a]) for a in order]),
    ]


def _results(r: Results) -> str:
    lines = [
        "## 4. Resultados por tramo y activo",
        "",
        "Los tres activos operan en una cartera única; las filas por activo son su contribución "
        "(PnL de sus operaciones sobre el capital inicial). Drawdown y Sharpe (diario, "
        "anualizado ×√365, sin tasa libre de riesgo) salen del capital marcado a mercado al "
        "cierre de cada vela de 4h.",
    ]
    for sc in (Scenario.PESIMISTA, Scenario.CENTRAL):
        suffix = " — escenario que se evalúa" if sc is r.criteria.judged_scenario else ""
        lines += ["", f"### Funding {SCENARIO_LABEL[sc]}{suffix}"]
        for name in (DEV, RESERVED):
            lines += ["", f"#### {SEGMENT_LABEL[name]}", ""]
            outcome = r.outcomes[sc].get(name)
            if outcome is None:
                lines.append("_No ejecutado (`--solo-desarrollo`)._")
                continue
            seg = outcome.segment
            run = outcome.run
            lines.append(
                f"{day(r.times_ms[seg.start])} → {day(r.times_ms[seg.end - 1] + CANDLE_MS)}. "
                f"Señales con entrada posible: {run.raw_signals}; ignoradas por posición "
                f"abierta: {run.ignored_open}; descartadas por margen: {run.skipped_margin}."
            )
            lines += [""] + _segment_tables(outcome)
    return "\n".join(lines)


def _real_share(trades: Sequence[Trade]) -> str:
    """Parte del importe de funding (suma de valores absolutos por operación) que es real."""
    real = sum(abs(t.funding_real) for t in trades)
    total = real + sum(abs(t.funding_imputed) for t in trades)
    return pct(real / total, 1) if total > 0 else "n/d"


def _funding_split(r: Results) -> str:
    rows = []
    for name in (DEV, RESERVED):
        for a in r.meta:
            cells = [SEGMENT_LABEL[name], a]
            outcome = r.outcomes[Scenario.PESIMISTA].get(name)
            if outcome is None:
                continue
            s = outcome.stats[a]
            hours = s.funding_hours_real + s.funding_hours_imputed
            share_h = s.funding_hours_real / hours if hours else math.nan
            cells += [pct(outcome.funding_coverage[a], 1), pct(share_h, 1)]
            for sc in (Scenario.PESIMISTA, Scenario.CENTRAL):
                trades = [t for t in r.outcomes[sc][name].run.trades if t.asset == a]
                cells.append(_real_share(trades))
            rows.append(cells)
        outcome = r.outcomes[Scenario.PESIMISTA].get(name)
        if outcome is None:
            continue
        cells = [SEGMENT_LABEL[name], TOTAL]
        real_h = sum(outcome.stats[a].funding_hours_real for a in r.meta)
        all_h = real_h + sum(outcome.stats[a].funding_hours_imputed for a in r.meta)
        cov = sum(outcome.funding_coverage.values()) / len(outcome.funding_coverage)
        cells += [pct(cov, 1), pct(real_h / all_h, 1) if all_h else "n/d"]
        for sc in (Scenario.PESIMISTA, Scenario.CENTRAL):
            cells.append(_real_share(r.outcomes[sc][name].run.trades))
        rows.append(cells)
    return "\n".join(
        [
            "## 5. Funding: real frente a imputado",
            "",
            "- *Cobertura del tramo*: % de las horas del tramo con funding real publicado.",
            "- *Horas en posición con dato real*: % de las horas con posición abierta cubiertas "
            "por funding real.",
            "- *% del coste real*: parte del importe de funding (suma de valores absolutos por "
            "operación, sin compensar signos) que viene de funding real, en cada escenario.",
            "",
            table(
                ["Tramo", "Activo", "Cobertura del tramo", "Horas en posición con dato real",
                 "% coste real (pesimista)", "% coste real (central)"],
                rows,
            ),
        ]
    )


def _buy_hold(r: Results) -> str:
    rows = []
    for name in (DEV, RESERVED):
        for a in r.meta:
            o_p = r.outcomes[Scenario.PESIMISTA].get(name)
            o_c = r.outcomes[Scenario.CENTRAL].get(name)
            if o_p is None or o_c is None:
                continue
            bh_p, bh_c = o_p.buy_hold[a], o_c.buy_hold[a]
            rows.append(
                [SEGMENT_LABEL[name], a, pct(bh_p.gross_return), pct(bh_c.net_return),
                 pct(bh_p.net_return), pct(bh_p.max_drawdown)]
            )
    return "\n".join(
        [
            "## 6. Buy & hold (contexto)",
            "",
            "Largo 1× con todo el capital en un solo activo durante el mismo tramo, con las mismas "
            "comisiones, slippage y funding que la estrategia.",
            "",
            table(
                ["Tramo", "Activo", "Rent. bruta", "Neta (funding central)",
                 "Neta (funding pesimista)", "DD máx (cierres)"],
                rows,
            ),
        ]
    )


def _robustness(r: Results) -> str:
    pct_var = int(round(r.criteria.robustness_pct * 100))
    rows = []
    for row in r.robustness:
        sign = "−" if row.factor < 1.0 else "+"
        rows.append(
            [
                row.param,
                f"{sign}{pct_var} %",
                num(row.value, 2 if row.value != int(row.value) else 0),
                pct(row.net_return[Scenario.PESIMISTA]),
                str(row.n_trades[Scenario.PESIMISTA]),
                pct(row.net_return[Scenario.CENTRAL]),
                str(row.n_trades[Scenario.CENTRAL]),
            ]
        )
    sc = r.criteria.judged_scenario
    base = r.outcomes[sc][DEV].stats[TOTAL]
    base_c = r.outcomes[Scenario.CENTRAL][DEV].stats[TOTAL]
    return "\n".join(
        [
            f"## 7. Robustez ±{pct_var} % (solo desarrollo)",
            "",
            "Cada parámetro se varía por separado (el resto, en su valor base); los enteros se "
            "redondean. Rentabilidad neta de la cartera en el tramo de desarrollo. **No se ejecuta "
            "sobre el reservado**, que solo se evalúa una vez con los parámetros base.",
            "",
            table(
                ["Parámetro", "Variación", "Valor", "Rent. neta (pesimista)", "Ops",
                 "Rent. neta (central)", "Ops"],
                [
                    ["*base*", "—", "—", pct(base.net_return), str(base.n_trades),
                     pct(base_c.net_return), str(base_c.n_trades)],
                    *rows,
                ],
            ),
        ]
    )


def _random(r: Results) -> str:
    rows = []
    for name in (DEV, RESERVED):
        if name not in r.random:
            continue
        for sc in (Scenario.PESIMISTA, Scenario.CENTRAL):
            rnd = r.random[name][sc]
            strat = r.outcomes[sc][name].stats[TOTAL]
            rows.append(
                [
                    SEGMENT_LABEL[name], SCENARIO_LABEL[sc], pct(strat.net_return),
                    f"**{num(r.percentiles[(sc, name)], 1)}**", pct(rnd.median),
                    pct(rnd.quantile(5.0)), pct(rnd.quantile(95.0)), pct(rnd.share_positive, 1),
                    f"{strat.n_trades} / {num(rnd.mean_trades, 1)}",
                ]
            )
    return "\n".join(
        [
            "## 8. Comparación con el azar",
            "",
            f"{r.n_random} simulaciones (semilla {r.seed}) con entradas aleatorias: cada vela "
            "elegible dispara, por activo, con la misma frecuencia de señales largas y cortas que "
            "la estrategia en ese tramo, sin filtro de régimen ni de oscilador. Mismas salidas "
            "(stop y TP en ATR, pesimista), mismo tamaño, apalancamiento, comisiones, slippage y "
            "funding. El percentil es el % de simulaciones por debajo de la estrategia "
            "(rentabilidad neta).",
            "",
            table(
                ["Tramo", "Funding", "Estrategia", "Percentil", "Mediana azar", "P5 azar",
                 "P95 azar", "Azar > 0", "Ops estrategia / azar (media)"],
                rows,
            ),
        ]
    )


def _limits(r: Results) -> str:
    return "\n".join(
        [
            "## 9. Limitaciones",
            "",
            "- **Funding imputado**: gran parte del periodo no tiene funding publicado; los "
            "resultados del tramo de desarrollo y de parte del reservado dependen de la "
            "imputación (sección 5). Por eso el veredicto usa el escenario pesimista.",
            "- Las estadísticas del funding imputado salen del último año, que incluye parte "
            "del tramo reservado (afecta solo a costes, no a señales ni parámetros).",
            "- El drawdown se mide con el capital a precios de cierre de las velas de 4h, no "
            "intravela; el real puede ser algo mayor. El orden de los extremos dentro de una "
            "vela es desconocido: de ahí el criterio pesimista del stop.",
            "- Funding con granularidad de vela: una salida dentro de la vela paga el funding de "
            "toda la vela.",
            "- El capital que dimensiona cada operación es el realizado (sin PnL latente de otras "
            "posiciones). Con señales simultáneas, el margen lo consume el activo que va antes en "
            "el orden BTC, ETH, SOL.",
            "- Ejecución idealizada: sin profundidad de libro, sin rechazos de órdenes, sin "
            "liquidación (a ≤ 2× está lejos) y con cantidades continuas.",
            "- Tres activos muy correlacionados: las operaciones no son independientes, así que "
            "el número efectivo de observaciones es menor que el de operaciones.",
        ]
    )


def render_report(r: Results) -> str:
    head = [
        "# Backtest 4h: tendencia (SMA) + RSI estocástico en perpetuos de Kraken Futures",
        "",
        "Informe generado por `python -m backtest ejecutar`; no editar a mano. "
        "Operaciones en `backtest/resultados/`.",
        "",
    ]
    sections = [
        _verdict(r), _data(r), _rules(r), _results(r), _funding_split(r), _buy_hold(r),
        _robustness(r), _random(r), _limits(r),
    ]
    return "\n".join(head) + "\n" + "\n\n".join(sections) + "\n"


# --- CSV ---------------------------------------------------------------------------------

CSV_COLUMNS = [
    "activo", "lado", "entrada", "salida", "motivo", "atr", "entrada_ref",
    "entrada_fill", "stop", "take_profit", "salida_ref", "salida_fill", "cantidad", "nocional",
    "capital_previo", "bruto", "comision_entrada", "comision_salida", "slippage",
    "funding_real", "funding_imputado", "horas_funding_real", "horas_funding_imputado", "neto",
]


def trade_row(t: Trade) -> list[str]:
    return [
        t.asset, "largo" if t.side > 0 else "corto", iso(t.entry_time_ms),
        iso(t.exit_time_ms), t.exit_reason, repr(t.atr), repr(t.entry_ref), repr(t.entry_fill),
        repr(t.stop), repr(t.take_profit), repr(t.exit_ref), repr(t.exit_fill), repr(t.qty),
        repr(t.notional), repr(t.balance_before), repr(t.gross_pnl), repr(t.fee_entry),
        repr(t.fee_exit), repr(t.slippage), repr(t.funding_real), repr(t.funding_imputed),
        str(t.funding_hours_real), str(t.funding_hours_imputed), repr(t.net_pnl),
    ]


def write_trades_csv(path: Path, trades: Sequence[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(CSV_COLUMNS)
        for t in sorted(trades, key=lambda x: (x.entry_idx, x.asset)):
            w.writerow(trade_row(t))


def write_outputs(r: Results, out_dir: Path, report_path: Path) -> None:
    for sc, by_segment in r.outcomes.items():
        for name, outcome in by_segment.items():
            write_trades_csv(out_dir / f"operaciones_{name}_{sc.value}.csv", outcome.run.trades)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(r), encoding="utf-8")

