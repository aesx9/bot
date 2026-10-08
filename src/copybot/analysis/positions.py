"""Lectura de los CSV del bot y reconstrucción de posiciones (función pura).

Una posición abre cuando el tamaño pasa de 0 a distinto de 0 y se cierra
cuando vuelve a 0. Un fill que cruza por cero se parte: la parte que cierra
termina la posición y el resto abre otra nueva. El PnL realizado se calcula
con coste medio ponderado: aumentar mueve el precio de entrada medio;
reducir realiza (salida - entrada media) x cantidad x dirección.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ZERO = Decimal(0)


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def dec(v: str | None) -> Decimal | None:
    return None if v in (None, "") else Decimal(v)


def ts(v: str) -> datetime:
    t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError(f"timestamp sin zona horaria: {v}")
    return t


@dataclass(frozen=True)
class Fill:
    timestamp: datetime
    symbol: str
    side: str  # "buy" | "sell"
    size: Decimal  # > 0
    price: Decimal
    origin: str = ""

    @property
    def signed(self) -> Decimal:
        return self.size if self.side == "buy" else -self.size


@dataclass
class Position:
    number: int
    symbol: str
    direction: int  # +1 largo, -1 corto
    opened_at: datetime
    closed_at: datetime | None = None
    size: Decimal = ZERO  # con signo
    entry_avg: Decimal = ZERO
    max_size: Decimal = ZERO
    entry_qty: Decimal = ZERO
    entry_notional: Decimal = ZERO
    exit_qty: Decimal = ZERO
    exit_notional: Decimal = ZERO
    realized_usd: Decimal = ZERO
    fills: int = 0
    origins: set[str] = field(default_factory=set)
    closing_origin: str = ""  # origen del fill que la cerró (bot, stop, liquidación...)

    @property
    def exit_avg(self) -> Decimal | None:
        return self.exit_notional / self.exit_qty if self.exit_qty else None

    @property
    def entry_avg_total(self) -> Decimal:
        return self.entry_notional / self.entry_qty if self.entry_qty else ZERO


def reconstruct(fills: Iterable[Fill]) -> tuple[list[Position], dict[str, Position]]:
    """(posiciones cerradas en orden de cierre, posiciones aún abiertas por símbolo)."""
    open_: dict[str, Position] = {}
    closed: list[Position] = []
    counter = 0
    for f in sorted(fills, key=lambda f: f.timestamp):
        remaining = f.signed
        while remaining != 0:
            pos = open_.get(f.symbol)
            if pos is None:
                counter += 1
                pos = Position(counter, f.symbol, 1 if remaining > 0 else -1, f.timestamp)
                open_[f.symbol] = pos
            pos.fills += 1
            if f.origin:
                pos.origins.add(f.origin)
            if (remaining > 0) == (pos.direction > 0):  # abre o aumenta
                qty = abs(remaining)
                total = abs(pos.size) + qty
                pos.entry_avg = (abs(pos.size) * pos.entry_avg + qty * f.price) / total
                pos.size += remaining
                pos.entry_qty += qty
                pos.entry_notional += qty * f.price
                pos.max_size = max(pos.max_size, abs(pos.size))
                remaining = ZERO
            else:  # reduce o cierra (y quizá cruza por cero)
                qty = min(abs(remaining), abs(pos.size))
                pos.realized_usd += (f.price - pos.entry_avg) * qty * pos.direction
                pos.exit_qty += qty
                pos.exit_notional += qty * f.price
                step = qty if remaining > 0 else -qty
                pos.size += step
                remaining -= step
                if pos.size == 0:
                    pos.closed_at = f.timestamp
                    pos.closing_origin = f.origin
                    closed.append(pos)
                    del open_[f.symbol]
    return closed, open_


def fills_from_rows(rows: Iterable[dict[str, str]], *, price_key: str, size_key: str = "tamano",
                    side_key: str = "lado", origin_key: str | None = None) -> Iterator[Fill]:
    for r in rows:
        price = dec(r.get(price_key))
        size = dec(r.get(size_key))
        if price is None or size is None or size <= 0:
            continue
        yield Fill(ts(r["timestamp_utc"]), r["mercado"], r[side_key], size, price,
                   r.get(origin_key, "") if origin_key else "")
