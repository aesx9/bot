"""Registros CSV: trades.csv, funding.csv y equity.csv.

- Los CSV del libro son IDEMPOTENTES por identificador: trades.csv por cliOrdId,
  kraken_fills.csv por fill_id, fees.csv y funding.csv por booking_uid. Si el proceso cae
  entre escribir una fila y confirmar el cursor, la siguiente lectura vuelve a traerla y no
  se duplica (un fill duplicado impediría cerrar una posición en el export fiscal).

- Timestamps en UTC (ISO 8601). Importes como Decimal en texto exacto.
- Cada fila lleva el modo (paper/live): los informes fiscales excluyen paper.
- funding.csv separa pagado y cobrado (tratamiento fiscal distinto en España).
- Cada escritura hace flush + fsync: una caída no deja filas a medias.
- Todo texto pasa por la redacción de secretos.
"""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from copybot.exchange.base import FundingEvent
from copybot.models import plain
from copybot.redaction import redact

TRADES_HEADER = (
    "timestamp_utc", "modo", "mercado", "accion", "lado", "tamano", "reduce_only",
    "precio_lider", "precio_referencia", "precio_propio", "slippage_pb", "comision_usd",
    "retraso_s", "cli_ord_id", "estado",
)
FUNDING_HEADER = (
    "timestamp_utc", "modo", "mercado", "posicion", "tasa_usd_por_unidad",
    "importe_usd", "pagado_usd", "cobrado_usd", "booking_uid",
)
KRAKEN_FILLS_HEADER = (
    "timestamp_utc", "mercado", "lado", "tamano", "precio", "tipo_fill", "origen",
    "cli_ord_id", "fill_id", "order_id",
)
FEES_HEADER = ("timestamp_utc", "mercado", "comision", "moneda", "concepto", "booking_uid")
EQUITY_HEADER = ("timestamp_utc", "modo", "capital_propio_usd", "capital_lider_usd")
# Posiciones reales del exchange en un instante (solo live): el export fiscal las concilia
# con el neto de los fills. Cuenta sin posiciones = una fila con mercado vacío y tamaño 0.
POSITIONS_HEADER = ("timestamp_utc", "modo", "mercado", "tamano")


POSITIONS_REFRESH_SECONDS = 3600  # sin cambios, la foto se repite como mucho cada hora


def fee_key(timestamp: object, symbol: object, fee: object, info: object, uid: object) -> str:
    """Identidad de una comisión: su booking_uid o, si el log no lo trae, una clave compuesta."""
    return str(uid) if uid else f"{timestamp}|{symbol}|{fee}|{info}"


@dataclass(frozen=True)
class TradeRecord:
    timestamp: datetime
    mode: str
    symbol: str
    action: str
    side: str
    size: Decimal
    reduce_only: bool
    leader_price: Decimal | None
    ref_price: Decimal
    fill_price: Decimal
    fee_usd: Decimal | None
    delay_seconds: Decimal | None
    cli_ord_id: str
    status: str

    @property
    def slippage_bps(self) -> Decimal:
        """Positivo = peor que la referencia (pagamos más o cobramos menos)."""
        sign = 1 if self.side == "buy" else -1
        return (sign * (self.fill_price - self.ref_price) / self.ref_price * 10000).quantize(
            Decimal("0.01")
        )


def _fmt(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return plain(v)
    return str(v)


class CsvRecorder:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self._keys: dict[str, set[str]] = {}  # claves ya escritas por fichero (carga perezosa)
        self._last_positions: tuple[float, dict[str, Decimal]] | None = None

    def _known(self, name: str, key_of: Callable[[dict[str, str]], str]) -> set[str]:
        """Claves ya escritas en `name` (el fichero se lee una sola vez)."""
        if name not in self._keys:
            keys: set[str] = set()
            path = self.dir / name
            if path.exists():
                with path.open(newline="", encoding="utf-8") as fh:
                    keys = {key_of(r) for r in csv.DictReader(fh)}
            self._keys[name] = keys
        return self._keys[name]

    def _append(self, name: str, header: tuple[str, ...], row: tuple[object, ...]) -> None:
        self._append_rows(name, header, [row])

    def _append_rows(self, name: str, header: tuple[str, ...],
                     rows: list[tuple[object, ...]]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.dir / name
        new = not path.exists()
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        if new:
            writer.writerow(header)
        for row in rows:  # una sola escritura: o están todas las filas o ninguna
            writer.writerow([redact(_fmt(v)) for v in row])
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, buf.getvalue().encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def trade(self, r: TradeRecord) -> None:
        known = self._known("trades.csv", lambda r: r.get("cli_ord_id", ""))
        if r.cli_ord_id in known:
            return  # idempotente por cliOrdId
        self._append("trades.csv", TRADES_HEADER, (
            r.timestamp, r.mode, r.symbol, r.action, r.side, r.size, r.reduce_only,
            r.leader_price, r.ref_price, r.fill_price, r.slippage_bps, r.fee_usd,
            r.delay_seconds, r.cli_ord_id, r.status,
        ))
        known.add(r.cli_ord_id)

    def funding(self, e: FundingEvent, mode: str) -> None:
        known = self._known("funding.csv", lambda r: r.get("booking_uid", ""))
        if e.booking_uid and e.booking_uid in known:
            return  # idempotente por booking_uid (el funding de paper no lo trae)
        paid = -e.amount_usd if e.amount_usd < 0 else Decimal(0)
        received = e.amount_usd if e.amount_usd > 0 else Decimal(0)
        self._append("funding.csv", FUNDING_HEADER, (
            e.timestamp, mode, e.symbol, e.position, e.rate, e.amount_usd, paid, received,
            e.booking_uid,
        ))
        if e.booking_uid:
            known.add(e.booking_uid)

    def kraken_fill(self, f: dict[str, Any]) -> None:
        """Fill REAL de Kraken (solo live): base del export fiscal. Incluye stops de
        catástrofe, liquidaciones y operaciones manuales, marcados en 'origen'."""
        known = self._known("kraken_fills.csv", lambda r: r.get("fill_id", ""))
        if f["fill_id"] in known:
            return  # idempotente por fill_id
        self._append("kraken_fills.csv", KRAKEN_FILLS_HEADER, (
            f["timestamp"], f["symbol"], f["side"], f["size"], f["price"], f["fill_type"],
            f["origin"], f["cli_ord_id"], f["fill_id"], f["order_id"],
        ))
        known.add(f["fill_id"])

    def fee(self, f: dict[str, Any]) -> None:
        """Comisión real del log de cuenta de Kraken (solo live)."""
        known = self._known("fees.csv", lambda r: fee_key(
            r.get("timestamp_utc"), r.get("mercado"), r.get("comision"), r.get("concepto"),
            r.get("booking_uid")))
        key = fee_key(_fmt(f["timestamp"]), f["symbol"], _fmt(f["fee"]), f["info"],
                      f["booking_uid"])
        if key in known:
            return  # idempotente por booking_uid
        self._append("fees.csv", FEES_HEADER, (
            f["timestamp"], f["symbol"], f["fee"], f["currency"], f["info"], f["booking_uid"],
        ))
        known.add(key)

    def equity(self, ts: datetime, mode: str, mine: Decimal, leader: Decimal | None) -> None:
        self._append("equity.csv", EQUITY_HEADER, (ts, mode, mine, leader))

    def positions_snapshot(self, ts: datetime, mode: str, positions: dict[str, Decimal]) -> None:
        """Posiciones reales del exchange en `ts` (todas las de la cuenta, con signo). Sin
        cambios respecto a la última foto escrita, solo se repite cada hora: basta para que
        el export detecte un fill que falte en el libro sin crecer sin límite."""
        clean = {symbol: size for symbol, size in positions.items() if size}
        last = self._last_positions
        if last and last[1] == clean and ts.timestamp() - last[0] < POSITIONS_REFRESH_SECONDS:
            return
        rows: list[tuple[object, ...]] = [
            (ts, mode, symbol, size) for symbol, size in sorted(clean.items())]
        self._append_rows("positions.csv", POSITIONS_HEADER,
                          rows or [(ts, mode, "", Decimal(0))])
        self._last_positions = (ts.timestamp(), clean)
