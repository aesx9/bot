"""Tipo de cambio de referencia del BCE (EUR/USD), para el export fiscal.

Serie: EXR.D.USD.EUR.SP00.A (diaria, USD por 1 EUR, tipo de referencia).
Fuentes admitidas:
1. API de datos del BCE (formato SDMX-CSV, columnas TIME_PERIOD y OBS_VALUE):
   https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?format=csvdata
2. Fichero histórico descargado a mano (eurofxref-hist.csv, columnas Date y
   USD; "N/A" en días sin dato) o el mismo SDMX-CSV guardado en disco.

Verificado el 2026-10-08 contra la API real: el SDMX-CSV (32 columnas, campos
entrecomillados con comas, sin "N/A": los días sin publicar no aparecen) se
parsea tal cual (tests/fixtures/ecb_sdmx_real.txt). Un periodo sin datos
responde 200 con cuerpo vacío. El formato eurofxref-hist.csv NO se ha podido
comprobar (www.ecb.europa.eu estaba bloqueado) y sigue según la documentación.

El BCE no publica en fines de semana ni festivos TARGET: se usa el último tipo
publicado anterior a la fecha (como mucho MAX_LOOKBACK_DAYS atrás) y el
fichero fiscal indica siempre la fecha del tipo usado.
"""

from __future__ import annotations

import csv
import io
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

ECB_SERIES = "EXR.D.USD.EUR.SP00.A"
ECB_API_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
SOURCE_TEXT = ("BCE, tipo de cambio de referencia diario EUR/USD "
               f"(serie {ECB_SERIES}, USD por 1 EUR)")
MAX_LOOKBACK_DAYS = 7


class RateError(Exception):
    pass


@dataclass(frozen=True)
class RateTable:
    dates: tuple[date, ...]
    rates: tuple[Decimal, ...]  # USD por 1 EUR
    origin: str  # de dónde salieron (URL o fichero)

    def rate_for(self, day: date) -> tuple[Decimal, date]:
        """(tipo, fecha de publicación usada) para una fecha de liquidación."""
        i = bisect_right(self.dates, day) - 1
        if i < 0 or (day - self.dates[i]) > timedelta(days=MAX_LOOKBACK_DAYS):
            raise RateError(f"sin tipo del BCE para {day} (ni en los {MAX_LOOKBACK_DAYS} "
                            "días anteriores)")
        return self.rates[i], self.dates[i]

    def usd_to_eur(self, usd: Decimal, day: date) -> tuple[Decimal, Decimal, date]:
        rate, used = self.rate_for(day)
        return usd / rate, rate, used


def parse_rates(text: str, origin: str) -> RateTable:
    if not text.strip():  # la API responde 200 y vacío si el periodo no tiene datos
        raise RateError("el fichero no contiene tipos")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = {(f or "").strip(): f for f in (reader.fieldnames or [])}
    if "TIME_PERIOD" in fields and "OBS_VALUE" in fields:
        date_col, value_col = fields["TIME_PERIOD"], fields["OBS_VALUE"]
    elif "Date" in fields and "USD" in fields:
        date_col, value_col = fields["Date"], fields["USD"]
    else:
        raise RateError("formato no reconocido: se esperaba SDMX-CSV del BCE "
                        "(TIME_PERIOD, OBS_VALUE) o eurofxref-hist.csv (Date, USD)")
    pairs: dict[date, Decimal] = {}
    for row in reader:
        raw_date = (row.get(date_col) or "").strip()
        raw_value = (row.get(value_col) or "").strip()
        if not raw_date or raw_value in ("", "N/A", "NaN"):
            continue
        try:
            d = date.fromisoformat(raw_date)
            v = Decimal(raw_value)
        except (ValueError, InvalidOperation):
            raise RateError(f"fila no válida: {raw_date!r}, {raw_value!r}") from None
        if not v.is_finite() or v <= 0:
            raise RateError(f"tipo no válido en {raw_date}: {raw_value}")
        pairs[d] = v
    if not pairs:
        raise RateError("el fichero no contiene tipos")
    ordered = sorted(pairs)
    return RateTable(tuple(ordered), tuple(pairs[d] for d in ordered), origin)


def load_file(path: Path) -> RateTable:
    return parse_rates(path.read_text(encoding="utf-8"), str(path))


def fetch(start: date, end: date, *, timeout: float = 30) -> RateTable:
    """Descarga los tipos del BCE entre start (menos margen) y end."""
    params = {"format": "csvdata",
              "startPeriod": (start - timedelta(days=MAX_LOOKBACK_DAYS)).isoformat(),
              "endPeriod": end.isoformat()}
    try:
        r = httpx.get(ECB_API_URL, params=params, timeout=timeout)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise RateError(f"no se pudo descargar del BCE ({type(exc).__name__}); usa "
                        "--ecb-csv con eurofxref-hist.csv descargado de la web del BCE") from exc
    return parse_rates(r.text, str(r.url))
