"""Export fiscal (residente en España): resultado por posición cerrada.

Uso:
  python -m copybot.analysis.fiscal --year 2026 [--data-dir data] [--ecb-csv f.csv]

Genera en el directorio de datos (o en --out):
- fiscal_posiciones_<año>.csv: una fila por posición CERRADA en el año, con
  resultado bruto, comisiones, funding pagado y cobrado (columnas separadas)
  y resultado neto, en USD y en EUR.
- fiscal_funding_<año>.csv: cada pago o cobro de funding del año, también de
  posiciones aún abiertas (el funding se liquida en cada pago).

Reglas:
- Solo operaciones REALES: las fuentes son kraken_fills.csv y fees.csv (que
  solo existen en live) y las filas de funding.csv con modo "live". Las
  operaciones paper se excluyen siempre.
- kraken_fills.csv contiene todos los fills reales de la cuenta (bot, stops de
  catástrofe, liquidaciones y operaciones manuales); la columna "origen" lo
  indica.
- Conversión USD -> EUR con el tipo de referencia diario del BCE en la fecha
  de cada liquidación: resultado y comisiones de una posición al tipo del día
  de cierre; cada funding al tipo del día de su pago. Si ese día no hay tipo
  publicado, se usa el último anterior; el fichero indica la fecha usada y la
  fuente en cada fila.
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

POSITIONS_HEADER = (
    "posicion", "mercado", "direccion", "origen", "apertura_utc", "cierre_utc",
    "tamano_maximo", "precio_entrada_medio", "precio_salida_medio",
    "resultado_bruto_usd", "comisiones_usd", "funding_pagado_usd", "funding_cobrado_usd",
    "resultado_neto_usd", "fecha_tipo_bce", "tipo_eurusd_bce",
    "resultado_bruto_eur", "comisiones_eur", "funding_pagado_eur", "funding_cobrado_eur",
    "resultado_neto_eur", "fuente_tipo_cambio", "avisos",
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
    fees_usd: Decimal = ZERO
    fees_eur_native: Decimal = ZERO  # comisiones cobradas directamente en EUR
    funding: list[Funding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
            if f.currency in ("USD", ""):
                row.fees_usd += f.amount
            elif f.currency == "EUR":
                row.fees_eur_native += f.amount
            else:
                row.warnings.append(f"comisión de {f.amount} {f.currency} sin convertir")
        row.funding = [f for f in funding
                       if f.symbol == p.symbol and p.opened_at < f.timestamp <= hi]
        rows.append(row)
    return rows


def _paid(fs: list[Funding]) -> Decimal:
    return sum((-f.amount_usd for f in fs if f.amount_usd < 0), ZERO)


def _received(fs: list[Funding]) -> Decimal:
    return sum((f.amount_usd for f in fs if f.amount_usd > 0), ZERO)


def _eur(fs: list[Funding], rates: ecb.RateTable, sign: int) -> Decimal:
    total = ZERO
    for f in fs:
        if (f.amount_usd > 0) == (sign > 0) and f.amount_usd != 0:
            total += rates.usd_to_eur(abs(f.amount_usd), f.timestamp.date())[0]
    return total


def write_positions(path: Path, rows: list[FiscalRow], rates: ecb.RateTable) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(POSITIONS_HEADER)
        for r in rows:
            p = r.position
            assert p.closed_at is not None
            day = p.closed_at.date()
            rate, rate_day = rates.rate_for(day)
            paid, received = _paid(r.funding), _received(r.funding)
            fees_usd_total = r.fees_usd + r.fees_eur_native * rate
            net_usd = p.realized_usd - fees_usd_total - paid + received
            gross_eur = p.realized_usd / rate
            fees_eur = r.fees_usd / rate + r.fees_eur_native
            paid_eur, received_eur = _eur(r.funding, rates, -1), _eur(r.funding, rates, +1)
            net_eur = gross_eur - fees_eur - paid_eur + received_eur
            w.writerow([
                p.number, p.symbol, "largo" if p.direction > 0 else "corto",
                "+".join(sorted(p.origins)), p.opened_at.isoformat(), p.closed_at.isoformat(),
                p.max_size, p.entry_avg_total, p.exit_avg,
                money(p.realized_usd), money(fees_usd_total),
                money(paid), money(received), money(net_usd),
                rate_day.isoformat(), rate,
                money(gross_eur), money(fees_eur), money(paid_eur),
                money(received_eur), money(net_eur),
                ecb.SOURCE_TEXT + f"; origen de los datos: {rates.origin}",
                "; ".join(r.warnings),
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
                money(received / rate),
                ecb.SOURCE_TEXT + f"; origen de los datos: {rates.origin}",
            ])


def export(data_dir: Path, year: int, out_dir: Path,
           rates_loader: ecb.RateTable | None = None) -> tuple[Path, Path, list[str]]:
    closed, open_, fees, funding = load_live_data(data_dir)
    rows = assign(closed, fees, funding)
    rows = [r for r in rows if r.position.closed_at and r.position.closed_at.year == year]
    year_funding = [f for f in funding if f.timestamp.year == year]
    notes = [f"posición abierta en {p.symbol} desde {p.opened_at.isoformat()}: no se declara "
             "hasta que se cierre" for p in open_.values()]
    days = [r.position.closed_at.date() for r in rows if r.position.closed_at]
    days += [f.timestamp.date() for f in year_funding]
    rates = rates_loader
    if rates is None:  # sin nada que convertir no hace falta descargar
        rates = ecb.fetch(min(days), max(days)) if days else ecb.RateTable((), (), "sin datos")
    out_dir.mkdir(parents=True, exist_ok=True)
    pos_path = out_dir / f"fiscal_posiciones_{year}.csv"
    fund_path = out_dir / f"fiscal_funding_{year}.csv"
    write_positions(pos_path, rows, rates)
    write_funding(fund_path, year_funding, rates)
    return pos_path, fund_path, notes


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
        pos, fund, notes = export(args.data_dir, args.year, args.out or args.data_dir, rates)
    except ecb.RateError as exc:
        print(f"error con los tipos del BCE: {exc}", file=sys.stderr)
        return 1
    print(f"Escrito {pos}\nEscrito {fund}")
    for n in notes:
        print(f"Aviso: {n}")
    print("Solo incluye operaciones reales (live). Revisa los ficheros con tu asesor fiscal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


