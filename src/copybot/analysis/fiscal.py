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
- fiscal_conciliacion_<año>.csv: por cada foto de las posiciones reales de Kraken
  (positions.csv), el neto de los fills hasta ese instante frente a la posición que
  tenía la cuenta. Si no cuadra falta (o sobra) algún fill en kraken_fills.csv y el
  resultado de las posiciones no es fiable: el export lo avisa.

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
- Sin duplicados: un fill se cuenta una sola vez por fill_id, y una comisión o un
  funding una sola vez por booking_uid, aunque el CSV los traiga repetidos.
Este fichero es una ayuda para la declaración, no asesoramiento fiscal.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from copybot.analysis import ecb
from copybot.analysis.positions import (
    ZERO,
    Fill,
    Position,
    fills_from_rows,
    madrid,
    read_rows,
    reconstruct,
    ts,
)
from copybot.records import fee_key

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
    "moneda_original", "importe_original",
)
CONVERTIBLE = ("USD", "EUR")


class FiscalError(Exception):
    """El export no puede hacerse sin falsear algún importe (p. ej. funding en otra moneda)."""
RECON_HEADER = ("foto_utc", "mercado", "neto_fills", "posicion_kraken", "diferencia", "estado")
WINDOW = timedelta(seconds=2)  # margen de reloj entre fills y apuntes del log
CENT = Decimal("0.01")


def local_date(t: datetime) -> date:
    """Fecha en Madrid: la que cuenta para el tipo de cambio y el año fiscal."""
    return madrid(t).date()


def money(v: Decimal) -> Decimal:
    """Redondeo a céntimos "half-up", el habitual en importes."""
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Funding:
    timestamp: datetime
    symbol: str
    amount: Decimal  # + cobrado, - pagado, en `currency`
    currency: str = "USD"  # USD (funding.csv) o la de funding_moneda.csv
    used: bool = False  # asignado ya a una posición: un funding se cuenta una sola vez

    def usd_eur(self, rates: ecb.RateTable) -> tuple[Decimal, Decimal]:
        """(USD, EUR) con signo, al tipo del BCE del día del pago (en Madrid). El importe en
        su moneda original es exacto; el otro se convierte."""
        if self.currency == "EUR":
            rate, _ = rates.rate_for(local_date(self.timestamp))
            return self.amount * rate, self.amount
        if self.currency != "USD":
            raise FiscalError(f"funding en {self.currency}: no se puede convertir")
        return self.amount, rates.usd_to_eur(self.amount, local_date(self.timestamp))[0]


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


@dataclass
class ReconRow:
    """Una foto de las posiciones de Kraken frente al neto de los fills hasta ese instante."""

    snapshot: datetime
    symbol: str  # "" = cuenta sin posiciones ni neto
    net_fills: Decimal = ZERO
    exchange: Decimal = ZERO

    @property
    def diff(self) -> Decimal:
        return self.net_fills - self.exchange

    @property
    def ok(self) -> bool:
        return self.diff == 0


@dataclass
class LiveData:
    closed: list[Position]
    open_: dict[str, Position]
    fees: list[Fee]
    funding: list[Funding]
    fills: list[Fill]
    snapshots: list[tuple[datetime, dict[str, Decimal]]]
    notes: list[str] = field(default_factory=list)


def _unique(rows: list[dict[str, str]], key: Callable[[dict[str, str]], str | None], what: str,
            notes: list[str]) -> list[dict[str, str]]:
    """Filas sin repetir la clave (la primera gana); avisa de cuántas sobraban."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    dups = 0
    for r in rows:
        k = key(r)
        if k:
            if k in seen:
                dups += 1
                continue
            seen.add(k)
        out.append(r)
    if dups:
        notes.append(f"{what}: {dups} fila(s) repetida(s) ignorada(s); cada una cuenta una vez")
    return out


def load_live_data(data_dir: Path) -> LiveData:
    notes: list[str] = []
    fill_rows = _unique(read_rows(data_dir / "kraken_fills.csv"), lambda r: r.get("fill_id"),
                        "kraken_fills.csv (por fill_id)", notes)
    fills = list(fills_from_rows(fill_rows, price_key="precio", origin_key="origen"))
    closed, open_ = reconstruct(fills)
    fee_rows = _unique(
        read_rows(data_dir / "fees.csv"),
        lambda r: fee_key(r.get("timestamp_utc"), r.get("mercado"), r.get("comision"),
                          r.get("concepto"), r.get("booking_uid")),
        "fees.csv (por booking_uid)", notes)
    fees = [Fee(ts(r["timestamp_utc"]), r["mercado"], Decimal(r["comision"]),
                (r.get("moneda") or "").upper()) for r in fee_rows]
    live_funding = _unique([r for r in read_rows(data_dir / "funding.csv")
                            if r.get("modo") == "live"], lambda r: r.get("booking_uid"),
                           "funding.csv (por booking_uid)", notes)
    funding = [Funding(ts(r["timestamp_utc"]), r["mercado"], Decimal(r["importe_usd"]))
               for r in live_funding]
    other_funding = _unique([r for r in read_rows(data_dir / "funding_moneda.csv")
                             if r.get("modo") == "live"], lambda r: r.get("booking_uid"),
                            "funding_moneda.csv (por booking_uid)", notes)
    funding += [Funding(ts(r["timestamp_utc"]), r["mercado"], Decimal(r["importe"]),
                        (r.get("moneda") or "").upper()) for r in other_funding]
    by_time: dict[str, dict[str, Decimal]] = {}
    for r in read_rows(data_dir / "positions.csv"):
        if r.get("modo") != "live":
            continue
        snap = by_time.setdefault(r["timestamp_utc"], {})
        if r.get("mercado"):
            snap[r["mercado"]] = Decimal(r["tamano"])
    snapshots = sorted(((ts(t), snap) for t, snap in by_time.items()), key=lambda s: s[0])
    return LiveData(closed, open_, fees, funding, fills, snapshots, notes)


def reconcile(fills: list[Fill], snapshots: list[tuple[datetime, dict[str, Decimal]]]
              ) -> list[ReconRow]:
    """Neto de los fills (acumulado desde el primero) frente a cada foto de posiciones."""
    ordered = sorted(fills, key=lambda f: f.timestamp)
    net: dict[str, Decimal] = {}
    out: list[ReconRow] = []
    i = 0
    for when, positions in snapshots:
        while i < len(ordered) and ordered[i].timestamp <= when:
            net[ordered[i].symbol] = net.get(ordered[i].symbol, ZERO) + ordered[i].signed
            i += 1
        symbols = sorted(set(positions) | {s for s, v in net.items() if v})
        if not symbols:
            out.append(ReconRow(when, ""))
        out += [ReconRow(when, s, net.get(s, ZERO), positions.get(s, ZERO)) for s in symbols]
    return out


def reconciliation_notes(rows: list[ReconRow], year: int, has_fills: bool) -> list[str]:
    if not rows:
        return ([f"CONCILIACIÓN: no hay fotos de las posiciones de Kraken de {year} "
                 "(positions.csv): no se puede comprobar que kraken_fills.csv esté completo"]
                if has_fills else [])
    last = max(r.snapshot for r in rows)
    notes = [
        f"CONCILIACIÓN: NO CUADRA en {r.symbol}: neto de fills {r.net_fills}, posición en "
        f"Kraken {r.exchange} (foto {r.snapshot.isoformat()}); falta o sobra algún fill en "
        "kraken_fills.csv y el resultado de las posiciones de ese mercado no es fiable"
        for r in rows if r.snapshot == last and not r.ok]
    earlier = sorted({r.snapshot for r in rows if r.snapshot != last and not r.ok})
    if earlier:
        notes.append(f"CONCILIACIÓN: {len(earlier)} foto(s) anterior(es) no cuadraban (la "
                     f"primera, {earlier[0].isoformat()}): revisa fiscal_conciliacion_{year}.csv")
    return notes


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
            if f.currency in ("USD", "EUR"):
                row.fees.append(f)
            else:  # sin moneda o distinta de USD/EUR: nunca se supone USD
                row.warnings.append(
                    f"comisión de {f.amount} {f.currency or 'moneda desconocida'} sin "
                    "convertir: no está en las comisiones de esta fila")
        # Un funding pertenece a la posición ABIERTA cuando se paga (sin margen de reloj:
        # a diferencia de las comisiones, no depende de casar un fill con su apunte) y solo
        # a una: en un cambio de dirección el cierre y la apertura comparten instante.
        row.funding = [f for f in funding if not f.used and f.symbol == p.symbol
                       and p.opened_at < f.timestamp <= p.closed_at]
        for fund in row.funding:
            fund.used = True
        rows.append(row)
    return rows


def amounts(r: FiscalRow, rates: ecb.RateTable) -> Amounts:
    """Cada flujo al tipo del BCE de su propia fecha."""
    p = r.position
    assert p.closed_at is not None
    a = Amounts(count=1, gross_usd=p.realized_usd)
    a.gross_eur = rates.usd_to_eur(p.realized_usd, local_date(p.closed_at))[0]
    for fee in r.fees:
        rate, _ = rates.rate_for(local_date(fee.timestamp))
        if fee.currency == "EUR":
            a.fees_eur += fee.amount
            a.fees_usd += fee.amount * rate
        else:
            a.fees_usd += fee.amount
            a.fees_eur += fee.amount / rate
    for fund in r.funding:
        _add_funding(a, fund, rates)
    return a


def _add_funding(a: Amounts, fund: Funding, rates: ecb.RateTable) -> None:
    if fund.amount == 0:
        return
    usd, eur = fund.usd_eur(rates)
    if fund.amount < 0:
        a.paid_usd += -usd
        a.paid_eur += -eur
    else:
        a.received_usd += usd
        a.received_eur += eur


def _source(rates: ecb.RateTable) -> str:
    return ecb.SOURCE_TEXT + f"; origen de los datos: {rates.origin}"


def write_positions(path: Path, rows: list[FiscalRow], rates: ecb.RateTable) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(POSITIONS_HEADER)
        for r in rows:
            p = r.position
            assert p.closed_at is not None
            rate, rate_day = rates.rate_for(local_date(p.closed_at))
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
        _add_funding(year_funding, f, rates)
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
            rate, rate_day = rates.rate_for(local_date(f.timestamp))
            usd, eur = f.usd_eur(rates)
            w.writerow([
                f.timestamp.isoformat(), f.symbol, usd, max(-usd, ZERO), max(usd, ZERO),
                rate_day.isoformat(), rate, money(max(-eur, ZERO)), money(max(eur, ZERO)),
                _source(rates), f.currency, f.amount,
            ])


def write_reconciliation(path: Path, rows: list[ReconRow]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(RECON_HEADER)
        for r in rows:
            w.writerow([r.snapshot.isoformat(), r.symbol, r.net_fills, r.exchange, r.diff,
                        "cuadra" if r.ok else "NO CUADRA"])


def export(data_dir: Path, year: int, out_dir: Path,
           rates_loader: ecb.RateTable | None = None) -> tuple[Path, Path, Path, list[str]]:
    data = load_live_data(data_dir)
    funding = data.funding
    rows = assign(data.closed, data.fees, funding)
    rows = [r for r in rows if r.position.closed_at and madrid(r.position.closed_at).year == year]
    year_funding = [f for f in funding if madrid(f.timestamp).year == year]
    # Funding en una moneda que no se sabe convertir (ni USD ni EUR, o desconocida): el export
    # se bloquea antes de escribir nada; declarar sin él falsearía el funding pagado/cobrado
    blocked = sorted({(f.timestamp.isoformat(), f.symbol, f.currency or "DESCONOCIDA",
                       str(f.amount))
                      for f in year_funding + [f for r in rows for f in r.funding]
                      if f.currency not in CONVERTIBLE})
    if blocked:
        raise FiscalError(
            "funding en una moneda que el export no sabe convertir (funding_moneda.csv): "
            + "; ".join(f"{t} {s} {amt} {cur}" for t, s, cur, amt in blocked)
            + ". Concílialo con el log de Kraken y corrige la fila (importe y moneda USD o "
            "EUR) antes de exportar")
    notes = list(data.notes)
    notes += [f"posición abierta en {p.symbol} desde {p.opened_at.isoformat()}: no se declara "
              "hasta que se cierre" for p in data.open_.values()]
    recon = reconcile(data.fills, [s for s in data.snapshots if madrid(s[0]).year == year])
    notes += reconciliation_notes(recon, year, bool(data.fills))
    days = [local_date(r.position.closed_at) for r in rows if r.position.closed_at]
    days += [local_date(f.timestamp) for f in year_funding]
    days += [local_date(f.timestamp) for r in rows for f in r.fees]
    rates = rates_loader
    if rates is None:  # sin nada que convertir no hace falta descargar
        rates = ecb.fetch(min(days), max(days)) if days else ecb.RateTable((), (), "sin datos")
    out_dir.mkdir(parents=True, exist_ok=True)
    pos_path = out_dir / f"fiscal_posiciones_{year}.csv"
    fund_path = out_dir / f"fiscal_funding_{year}.csv"
    sum_path = out_dir / f"fiscal_resumen_{year}.csv"
    recon_path = out_dir / f"fiscal_conciliacion_{year}.csv"
    # Los tres ficheros se generan primero aparte: si falta un tipo del BCE a mitad de camino
    # no queda ningún fichero a medias ni una mezcla de ficheros nuevos y viejos.
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".fiscal.") as tmp:
        staging = Path(tmp)
        write_positions(staging / pos_path.name, rows, rates)
        write_funding(staging / fund_path.name, year_funding, rates)
        write_summary(staging / sum_path.name, summarize(rows, year_funding, rates), rates)
        write_reconciliation(staging / recon_path.name, recon)
        for final in (pos_path, fund_path, sum_path, recon_path):
            (staging / final.name).chmod(0o600)  # datos personales de tributación
            os.replace(staging / final.name, final)
    return pos_path, fund_path, sum_path, notes


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="export_fiscal",
                                description="Export fiscal en EUR (solo operaciones reales)")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--data-dir", type=Path, default=Path("data/live"),
                   help="directorio de datos del modo live (por defecto data/live)")
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
    except FiscalError as exc:
        print(f"export fiscal BLOQUEADO: {exc}", file=sys.stderr)
        return 1
    print(f"Escrito {pos}\nEscrito {fund}\nEscrito {summary}")
    print(f"Escrito {summary.with_name(f'fiscal_conciliacion_{args.year}.csv')}")
    for n in notes:
        print(f"Aviso: {n}")
    print("Solo incluye operaciones reales (live). Revisa los ficheros con tu asesor fiscal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


