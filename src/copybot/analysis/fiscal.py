"""Export fiscal (residente en España): resultado por posición cerrada.

Uso:
  python -m copybot.analysis.fiscal --year 2026 [--data-dir data] [--ecb-csv f.csv]

Genera en el directorio de datos (o en --out):
- fiscal_posiciones_<año>.csv: una fila por posición CERRADA en el año, con
  resultado bruto, comisiones, funding pagado y cobrado (columnas separadas)
  y resultado neto, en USD y en EUR.
- fiscal_funding_<año>.csv: cada pago o cobro de funding del año, también de
  posiciones aún abiertas (el funding se liquida en cada pago).
- fiscal_resumen_<año>.csv: subtotales por origen de cierre de la posición
  (bot, stop_catastrofe, liquidación, manual) y total; más el funding total del
  año según fiscal_funding.

Reglas:
- Solo operaciones REALES: las fuentes son kraken_fills.csv y fees.csv (que
  solo existen en live) y las filas de funding.csv con modo "live". Las
  operaciones paper se excluyen siempre.
- kraken_fills.csv contiene todos los fills reales de la cuenta (bot, stops de
  catástrofe, liquidaciones y operaciones manuales); la columna "origen" lo
  indica.
- Conversión USD -> EUR con el tipo de referencia diario del BCE, cada flujo
  en su fecha: el resultado de la posición al tipo del día de cierre, cada
  comisión al tipo del día en que se cobra y cada funding al del día de su
  pago. Si ese día no hay tipo publicado, se usa el último anterior; el
  fichero indica la fecha del tipo del cierre y la fuente en cada fila.
- Origen: cada posición se clasifica por el origen del fill que la cerró
  (una posición del bot cerrada por un stop cuenta como stop_catastrofe); la
  columna "origenes" lista todos los que intervinieron.
- Comisiones: las del log de cuenta de Kraken del mismo mercado entre la
  apertura y el cierre de la posición.
Este fichero es una ayuda para la declaración, no asesoramiento fiscal.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from copybot.analysis import ecb
from copybot.analysis.positions import (
    ZERO,
    Position,
    fills_from_rows,
    read_rows,
    reconstruct,
    ts,
)

ORIGINS = ("bot", "stop_catastrofe", "liquidación", "manual")
POSITIONS_HEADER = (
    "posicion", "mercado", "direccion", "origen_cierre", "origenes", "apertura_utc",
    "cierre_utc",
    "tamano_maximo", "precio_entrada_medio", "precio_salida_medio",
    "resultado_bruto_usd", "comisiones_usd", "funding_pagado_usd", "funding_cobrado_usd",
    "resultado_neto_usd", "fecha_tipo_bce_cierre", "tipo_eurusd_bce_cierre",
    "resultado_bruto_eur", "comisiones_eur", "funding_pagado_eur", "funding_cobrado_eur",
    "resultado_neto_eur", "fuente_tipo_cambio", "avisos",
)
SUMMARY_HEADER = (
    "categoria", "posiciones", "resultado_bruto_usd", "comisiones_usd", "funding_pagado_usd",
    "funding_cobrado_usd", "resultado_neto_usd", "resultado_bruto_eur", "comisiones_eur",
    "funding_pagado_eur", "funding_cobrado_eur", "resultado_neto_eur", "fuente_tipo_cambio",
)
FUNDING_HEADER = (
    "timestamp_utc", "mercado", "importe_usd", "pagado_usd", "cobrado_usd",
    "fecha_tipo_bce", "tipo_eurusd_bce", "pagado_eur", "cobrado_eur", "fuente_tipo_cambio",
)
WINDOW = timedelta(seconds=2)  # margen de reloj entre fills y apuntes del log
CENT = Decimal("0.01")


def money(v: Decimal) -> Decimal:
    """Redondeo a céntimos "half-up", el habitual en importes."""
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Funding:
    timestamp: datetime
    symbol: str
    amount_usd: Decimal  # + cobrado, - pagado


@dataclass
class Fee:
    timestamp: datetime
    symbol: str
    amount: Decimal
    currency: str
    used: bool = False


@dataclass
class FiscalRow:
    position: Position
    fees: list[Fee] = field(default_factory=list)
    funding: list[Funding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Amounts:
    """Importes de una posición (o subtotal) en USD y EUR."""

    gross_usd: Decimal = ZERO
    fees_usd: Decimal = ZERO
    paid_usd: Decimal = ZERO
    received_usd: Decimal = ZERO
    gross_eur: Decimal = ZERO
    fees_eur: Decimal = ZERO
    paid_eur: Decimal = ZERO
    received_eur: Decimal = ZERO
    count: int = 0

    @property
    def net_usd(self) -> Decimal:
        return self.gross_usd - self.fees_usd - self.paid_usd + self.received_usd

    @property
    def net_eur(self) -> Decimal:
        return self.gross_eur - self.fees_eur - self.paid_eur + self.received_eur

    def add(self, o: Amounts) -> None:
        for name in ("gross_usd", "fees_usd", "paid_usd", "received_usd", "gross_eur",
                     "fees_eur", "paid_eur", "received_eur", "count"):
            setattr(self, name, getattr(self, name) + getattr(o, name))


def load_live_data(data_dir: Path) -> tuple[list[Position], dict[str, Position],
                                              list[Fee], list[Funding]]:
    fills = fills_from_rows(read_rows(data_dir / "kraken_fills.csv"), price_key="precio",
                            origin_key="origen")
    closed, open_ = reconstruct(fills)
    fees = [Fee(ts(r["timestamp_utc"]), r["mercado"], Decimal(r["comision"]),
                (r.get("moneda") or "USD").upper())
            for r in read_rows(data_dir / "fees.csv")]
    funding = [Funding(ts(r["timestamp_utc"]), r["mercado"], Decimal(r["importe_usd"]))
               for r in read_rows(data_dir / "funding.csv") if r.get("modo") == "live"]
    return closed, open_, fees, funding


def assign(closed: list[Position], fees: list[Fee], funding: list[Funding]) -> list[FiscalRow]:
    rows = []
    for p in closed:
        assert p.closed_at is not None
        row = FiscalRow(p)
        lo, hi = p.opened_at - WINDOW, p.closed_at + WINDOW
        for f in fees:
            if f.used or f.symbol != p.symbol or not lo <= f.timestamp <= hi:
                continue
            f.used = True
            if f.currency in ("USD", "", "EUR"):
                row.fees.append(f)
            else:
                row.warnings.append(f"comisión de {f.amount} {f.currency} sin convertir")
        row.funding = [f for f in funding
                       if f.symbol == p.symbol and p.opened_at < f.timestamp <= hi]
        rows.append(row)
    return rows


def amounts(r: FiscalRow, rates: ecb.RateTable) -> Amounts:
    """Cada flujo al tipo del BCE de su propia fecha."""
    p = r.position
    assert p.closed_at is not None
    a = Amounts(count=1, gross_usd=p.realized_usd)
    a.gross_eur = rates.usd_to_eur(p.realized_usd, p.closed_at.date())[0]
    for fee in r.fees:
        rate, _ = rates.rate_for(fee.timestamp.date())
        if fee.currency == "EUR":
            a.fees_eur += fee.amount
            a.fees_usd += fee.amount * rate
        else:
            a.fees_usd += fee.amount
            a.fees_eur += fee.amount / rate
    for fund in r.funding:
        if fund.amount_usd == 0:
            continue
        eur = rates.usd_to_eur(abs(fund.amount_usd), fund.timestamp.date())[0]
        if fund.amount_usd < 0:
            a.paid_usd += -fund.amount_usd
            a.paid_eur += eur
        else:
            a.received_usd += fund.amount_usd
            a.received_eur += eur
    return a


def _source(rates: ecb.RateTable) -> str:
    return ecb.SOURCE_TEXT + f"; origen de los datos: {rates.origin}"


def write_positions(path: Path, rows: list[FiscalRow], rates: ecb.RateTable) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(POSITIONS_HEADER)
        for r in rows:
            p = r.position
            assert p.closed_at is not None
            rate, rate_day = rates.rate_for(p.closed_at.date())
            a = amounts(r, rates)
            w.writerow([
                p.number, p.symbol, "largo" if p.direction > 0 else "corto",
                p.closing_origin, "+".join(sorted(p.origins)),
                p.opened_at.isoformat(), p.closed_at.isoformat(),
                p.max_size, p.entry_avg_total, p.exit_avg,
                money(a.gross_usd), money(a.fees_usd), money(a.paid_usd),
                money(a.received_usd), money(a.net_usd),
                rate_day.isoformat(), rate,
                money(a.gross_eur), money(a.fees_eur), money(a.paid_eur),
                money(a.received_eur), money(a.net_eur),
                _source(rates), "; ".join(r.warnings),
            ])


def summarize(rows: list[FiscalRow], funding: list[Funding],
              rates: ecb.RateTable) -> dict[str, Amounts]:
    by: dict[str, Amounts] = {o: Amounts() for o in ORIGINS}
    for r in rows:
        by.setdefault(r.position.closing_origin or "sin_origen", Amounts()).add(amounts(r, rates))
    total = Amounts()
    for a in by.values():
        total.add(a)
    by["TOTAL posiciones cerradas"] = total
    year_funding = Amounts()
    for f in funding:
        if f.amount_usd == 0:
            continue
        eur = rates.usd_to_eur(abs(f.amount_usd), f.timestamp.date())[0]
        if f.amount_usd < 0:
            year_funding.paid_usd += -f.amount_usd
            year_funding.paid_eur += eur
        else:
            year_funding.received_usd += f.amount_usd
            year_funding.received_eur += eur
    by["funding total del año (fiscal_funding)"] = year_funding
    return by


def write_summary(path: Path, summary: dict[str, Amounts], rates: ecb.RateTable) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SUMMARY_HEADER)
        for name, a in summary.items():
            w.writerow([
                name, a.count, money(a.gross_usd), money(a.fees_usd), money(a.paid_usd),
                money(a.received_usd), money(a.net_usd), money(a.gross_eur),
                money(a.fees_eur), money(a.paid_eur), money(a.received_eur),
                money(a.net_eur), _source(rates),
            ])


def write_funding(path: Path, funding: list[Funding], rates: ecb.RateTable) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(FUNDING_HEADER)
        for f in sorted(funding, key=lambda f: f.timestamp):
            rate, rate_day = rates.rate_for(f.timestamp.date())
            paid = -f.amount_usd if f.amount_usd < 0 else ZERO
            received = f.amount_usd if f.amount_usd > 0 else ZERO
            w.writerow([
                f.timestamp.isoformat(), f.symbol, f.amount_usd, paid, received,
                rate_day.isoformat(), rate, money(paid / rate),
                money(received / rate), _source(rates),
            ])


def export(data_dir: Path, year: int, out_dir: Path,
           rates_loader: ecb.RateTable | None = None) -> tuple[Path, Path, Path, list[str]]:
    closed, open_, fees, funding = load_live_data(data_dir)
    rows = assign(closed, fees, funding)
    rows = [r for r in rows if r.position.closed_at and r.position.closed_at.year == year]
    year_funding = [f for f in funding if f.timestamp.year == year]
    notes = [f"posición abierta en {p.symbol} desde {p.opened_at.isoformat()}: no se declara "
             "hasta que se cierre" for p in open_.values()]
    days = [r.position.closed_at.date() for r in rows if r.position.closed_at]
    days += [f.timestamp.date() for f in year_funding]
    days += [f.timestamp.date() for r in rows for f in r.fees]
    rates = rates_loader
    if rates is None:  # sin nada que convertir no hace falta descargar
        rates = ecb.fetch(min(days), max(days)) if days else ecb.RateTable((), (), "sin datos")
    out_dir.mkdir(parents=True, exist_ok=True)
    pos_path = out_dir / f"fiscal_posiciones_{year}.csv"
    fund_path = out_dir / f"fiscal_funding_{year}.csv"
    sum_path = out_dir / f"fiscal_resumen_{year}.csv"
    write_positions(pos_path, rows, rates)
    write_funding(fund_path, year_funding, rates)
    write_summary(sum_path, summarize(rows, year_funding, rates), rates)
    return pos_path, fund_path, sum_path, notes


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="export_fiscal",
                                description="Export fiscal en EUR (solo operaciones reales)")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--out", type=Path)
    p.add_argument("--ecb-csv", type=Path,
                   help="tipos del BCE en local (eurofxref-hist.csv o SDMX-CSV)")
    args = p.parse_args(argv)
    try:
        rates = ecb.load_file(args.ecb_csv) if args.ecb_csv else None
        pos, fund, summary, notes = export(args.data_dir, args.year, args.out or args.data_dir,
                                           rates)
    except ecb.RateError as exc:
        print(f"error con los tipos del BCE: {exc}", file=sys.stderr)
        return 1
    print(f"Escrito {pos}\nEscrito {fund}\nEscrito {summary}")
    for n in notes:
        print(f"Aviso: {n}")
    print("Solo incluye operaciones reales (live). Revisa los ficheros con tu asesor fiscal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


