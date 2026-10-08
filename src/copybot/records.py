"""Registros CSV: trades.csv, funding.csv y equity.csv.

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
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from copybot.exchange.base import FundingEvent
from copybot.redaction import redact

TRADES_HEADER = (
    "timestamp_utc", "modo", "mercado", "accion", "lado", "tamano", "reduce_only",
    "precio_lider", "precio_referencia", "precio_propio", "slippage_pb", "comision_usd",
    "retraso_s", "cli_ord_id", "estado",
)
FUNDING_HEADER = (
    "timestamp_utc", "modo", "mercado", "posicion", "tasa_usd_por_unidad",
    "importe_usd", "pagado_usd", "cobrado_usd",
)
EQUITY_HEADER = ("timestamp_utc", "modo", "capital_propio_usd", "capital_lider_usd")


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
    fee_usd: Decimal
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
    return str(v)


class CsvRecorder:
    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def _append(self, name: str, header: tuple[str, ...], row: tuple[object, ...]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.dir / name
        new = not path.exists()
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        if new:
            writer.writerow(header)
        writer.writerow([redact(_fmt(v)) for v in row])
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, buf.getvalue().encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def trade(self, r: TradeRecord) -> None:
        self._append("trades.csv", TRADES_HEADER, (
            r.timestamp, r.mode, r.symbol, r.action, r.side, r.size, r.reduce_only,
            r.leader_price, r.ref_price, r.fill_price, r.slippage_bps, r.fee_usd,
            r.delay_seconds, r.cli_ord_id, r.status,
        ))

    def funding(self, e: FundingEvent, mode: str) -> None:
        paid = -e.amount_usd if e.amount_usd < 0 else Decimal(0)
        received = e.amount_usd if e.amount_usd > 0 else Decimal(0)
        self._append("funding.csv", FUNDING_HEADER, (
            e.timestamp, mode, e.symbol, e.position, e.rate, e.amount_usd, paid, received,
        ))

    def equity(self, ts: datetime, mode: str, mine: Decimal, leader: Decimal | None) -> None:
        self._append("equity.csv", EQUITY_HEADER, (ts, mode, mine, leader))
