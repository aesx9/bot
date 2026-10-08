"""Informe de rendimiento: global y por activo.

Uso: python -m copybot.analysis.report --data-dir data [--mode paper|live] [--json]

Fuentes (del directorio de datos del bot):
- equity.csv: rentabilidad y drawdown máximo sobre el capital registrado.
- trades.csv: PnL realizado (coste medio), slippage, comisiones (paper) y retraso.
- fees.csv: comisiones reales (live, del log de cuenta de Kraken), cada una en su
  moneda: USD suma en "Comisiones (USD)", EUR se muestra aparte (sin convertir) y
  cualquier otra, o sin moneda, no se suma y se avisa. Nada se da por USD a ciegas.
- funding.csv: funding pagado, cobrado y neto (USD).
- funding_moneda.csv: funding live que no es USD, con su moneda; como las comisiones, EUR se
  muestra aparte (sin convertir) y cualquier otra moneda, o sin moneda, no se suma y se avisa.
Los modos no se mezclan nunca: se elige uno (por defecto, live si hay datos live).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

from copybot.analysis.positions import ZERO, dec, fills_from_rows, read_rows, reconstruct


@dataclass
class AssetStats:
    trades: int = 0
    volume_usd: Decimal = ZERO
    realized_pnl_usd: Decimal = ZERO
    fees_usd: Decimal = ZERO
    fees_eur: Decimal = ZERO
    funding_paid_usd: Decimal = ZERO
    funding_received_usd: Decimal = ZERO
    funding_paid_eur: Decimal = ZERO
    funding_received_eur: Decimal = ZERO
    slippage_bps: list[Decimal] = field(default_factory=list)
    delays_s: list[Decimal] = field(default_factory=list)

    @property
    def funding_net_usd(self) -> Decimal:
        return self.funding_received_usd - self.funding_paid_usd

    def summary(self) -> dict[str, object]:
        return {
            "operaciones": self.trades,
            "volumen_usd": _q(self.volume_usd),
            "pnl_realizado_usd": _q(self.realized_pnl_usd),
            "comisiones_usd": _q(self.fees_usd),
            "comisiones_eur": _q(self.fees_eur),
            "funding_pagado_usd": _q(self.funding_paid_usd),
            "funding_cobrado_usd": _q(self.funding_received_usd),
            "funding_neto_usd": _q(self.funding_net_usd),
            "funding_pagado_eur": _q(self.funding_paid_eur),
            "funding_cobrado_eur": _q(self.funding_received_eur),
            "slippage_medio_pb": _mean(self.slippage_bps),
            "slippage_medio_ponderado_pb": None,
            "retraso_medio_s": _mean(self.delays_s),
            "retraso_mediano_s": _median(self.delays_s),
        }


@dataclass
class Report:
    mode: str
    equity_start: Decimal | None
    equity_end: Decimal | None
    return_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    total: dict[str, object]
    by_asset: dict[str, dict[str, object]]
    warnings: list[str] = field(default_factory=list)


def _q(v: Decimal | None, places: str = "0.01") -> str | None:
    return None if v is None else str(v.quantize(Decimal(places)))


def _mean(xs: list[Decimal]) -> str | None:
    return _q(sum(xs, ZERO) / len(xs)) if xs else None


def _median(xs: list[Decimal]) -> str | None:
    return _q(statistics.median(xs)) if xs else None


def max_drawdown_pct(series: Sequence[Decimal]) -> Decimal | None:
    if not series:
        return None
    peak, worst = series[0], ZERO
    for v in series:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak * 100)
    return worst


def available_modes(data_dir: Path) -> set[str]:
    modes: set[str] = set()
    for name in ("trades.csv", "equity.csv", "funding.csv", "funding_moneda.csv"):
        modes |= {r.get("modo", "") for r in read_rows(data_dir / name)}
    return modes - {""}


def build_report(data_dir: Path, mode: str) -> Report:
    trades = [r for r in read_rows(data_dir / "trades.csv") if r["modo"] == mode]
    equity = [r for r in read_rows(data_dir / "equity.csv") if r["modo"] == mode]
    funding = [r for r in read_rows(data_dir / "funding.csv") if r["modo"] == mode]
    other_funding = [r for r in read_rows(data_dir / "funding_moneda.csv") if r["modo"] == mode]
    # fees.csv solo existe en live (comisiones reales del log de cuenta)
    fees = read_rows(data_dir / "fees.csv") if mode == "live" else []

    per: dict[str, AssetStats] = defaultdict(AssetStats)
    weighted: dict[str, tuple[Decimal, Decimal]] = defaultdict(lambda: (ZERO, ZERO))
    for r in trades:
        a = per[r["mercado"]]
        size, price = Decimal(r["tamano"]), Decimal(r["precio_propio"])
        a.trades += 1
        a.volume_usd += size * price
        if (fee := dec(r.get("comision_usd"))) is not None:
            a.fees_usd += fee
        if (slip := dec(r.get("slippage_pb"))) is not None:
            a.slippage_bps.append(slip)
            w_sum, w = weighted[r["mercado"]]
            weighted[r["mercado"]] = (w_sum + slip * size * price, w + size * price)
        if (delay := dec(r.get("retraso_s"))) is not None:
            a.delays_s.append(delay)
    warnings: list[str] = []
    other_fees: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for f in fees:
        currency = (f.get("moneda") or "").upper()
        if currency == "USD":
            per[f["mercado"]].fees_usd += Decimal(f["comision"])
        elif currency == "EUR":
            per[f["mercado"]].fees_eur += Decimal(f["comision"])
        else:
            other_fees[currency or "DESCONOCIDA"] += Decimal(f["comision"])
    if any(a.fees_eur for a in per.values()):
        warnings.append("hay comisiones en EUR: se muestran aparte y NO están en "
                        "'Comisiones (USD)' (conviértelas con el export fiscal)")
    for currency, amount in sorted(other_fees.items()):
        warnings.append(f"comisiones en {currency} ({amount}) sin sumar: moneda distinta de "
                        "USD y EUR; concílalas con el log de Kraken")
    for f in funding:
        a = per[f["mercado"]]
        a.funding_paid_usd += Decimal(f["pagado_usd"])
        a.funding_received_usd += Decimal(f["cobrado_usd"])
    unsummed_funding: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for f in other_funding:
        currency = (f.get("moneda") or "").upper()
        if currency == "EUR":
            a = per[f["mercado"]]
            a.funding_paid_eur += Decimal(f["pagado"])
            a.funding_received_eur += Decimal(f["cobrado"])
        else:
            unsummed_funding[currency or "DESCONOCIDA"] += Decimal(f["importe"])
    if any(a.funding_paid_eur or a.funding_received_eur for a in per.values()):
        warnings.append("hay funding en EUR: se muestra aparte y NO está en el funding en USD "
                        "(conviértelo con el export fiscal)")
    for currency, amount in sorted(unsummed_funding.items()):
        warnings.append(f"funding en {currency} ({amount}) sin sumar: moneda distinta de USD y "
                        "EUR; concílialo con el log de Kraken")
    closed, open_ = reconstruct(fills_from_rows(trades, price_key="precio_propio"))
    for p in [*closed, *open_.values()]:
        per[p.symbol].realized_pnl_usd += p.realized_usd

    total = AssetStats()
    tw = (ZERO, ZERO)
    for sym, a in per.items():
        total.trades += a.trades
        total.volume_usd += a.volume_usd
        total.realized_pnl_usd += a.realized_pnl_usd
        total.fees_usd += a.fees_usd
        total.fees_eur += a.fees_eur
        total.funding_paid_usd += a.funding_paid_usd
        total.funding_received_usd += a.funding_received_usd
        total.funding_paid_eur += a.funding_paid_eur
        total.funding_received_eur += a.funding_received_eur
        total.slippage_bps += a.slippage_bps
        total.delays_s += a.delays_s
        tw = (tw[0] + weighted[sym][0], tw[1] + weighted[sym][1])

    def with_weighted(stats: AssetStats, w: tuple[Decimal, Decimal]) -> dict[str, object]:
        s = stats.summary()
        s["slippage_medio_ponderado_pb"] = _q(w[0] / w[1]) if w[1] else None
        return s

    values = [Decimal(r["capital_propio_usd"]) for r in equity]
    start = values[0] if values else None
    end = values[-1] if values else None
    ret = (end - start) / start * 100 if start and end is not None else None
    return Report(
        mode=mode, equity_start=start, equity_end=end, return_pct=ret,
        max_drawdown_pct=max_drawdown_pct(values),
        total=with_weighted(total, tw),
        by_asset={s: with_weighted(a, weighted[s]) for s, a in sorted(per.items())},
        warnings=warnings,
    )


LABELS = {
    "operaciones": "Operaciones", "volumen_usd": "Volumen (USD)",
    "pnl_realizado_usd": "PnL realizado (USD)", "comisiones_usd": "Comisiones (USD)",
    "comisiones_eur": "Comisiones en EUR (sin convertir)",
    "funding_pagado_usd": "Funding pagado (USD)", "funding_cobrado_usd": "Funding cobrado (USD)",
    "funding_neto_usd": "Funding neto (USD)",
    "funding_pagado_eur": "Funding pagado en EUR (sin convertir)",
    "funding_cobrado_eur": "Funding cobrado en EUR (sin convertir)",
    "slippage_medio_pb": "Slippage medio (pb)",
    "slippage_medio_ponderado_pb": "Slippage ponderado por nocional (pb)",
    "retraso_medio_s": "Retraso medio (s)", "retraso_mediano_s": "Retraso mediano (s)",
}


def format_text(r: Report) -> str:
    def show(v: object) -> str:
        return "-" if v is None else str(v)

    lines = [f"=== Informe ({r.mode}) ===",
             f"Capital: {show(_q(r.equity_start))} -> {show(_q(r.equity_end))} USD "
             f"(rentabilidad {show(_q(r.return_pct))} %; incluye depósitos y retiros)",
             f"Drawdown máximo: {show(_q(r.max_drawdown_pct))} %", "", "-- Global --"]
    lines += [f"  {LABELS[k]}: {show(v)}" for k, v in r.total.items()]
    for sym, stats in r.by_asset.items():
        lines += ["", f"-- {sym} --"]
        lines += [f"  {LABELS[k]}: {show(v)}" for k, v in stats.items()]
    if r.warnings:
        lines += ["", "-- Avisos --"] + [f"  {w}" for w in r.warnings]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="report", description="Informe de rendimiento del bot")
    p.add_argument("--data-dir", type=Path,
                   help="directorio de datos de un modo (por defecto data/live o data/paper)")
    p.add_argument("--mode", choices=("paper", "live"))
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if args.data_dir is None:
        live = Path("data/live")
        args.data_dir = live if live.exists() else Path("data/paper")
    modes = available_modes(args.data_dir)
    mode = args.mode or ("live" if "live" in modes else "paper")
    if mode not in modes:
        print(f"no hay datos en modo {mode} en {args.data_dir}", file=sys.stderr)
        return 1
    report = build_report(args.data_dir, mode)
    if args.json:
        print(json.dumps(asdict(report), default=str, ensure_ascii=False, indent=2))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
