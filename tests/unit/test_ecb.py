from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import httpx
import pytest
import respx

from copybot.analysis import ecb

# Formato SDMX-CSV de la API de datos del BCE (cabecera recortada)
SDMX = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-01,1.1712,A\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-02,1.1650,A\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-05,1.1600,A\n"
)
# Formato eurofxref-hist.csv (más reciente primero, BOM y "N/A")
HIST = ("﻿Date,USD,JPY,\n2026-10-05,1.1600,170.1,\n2026-10-03,N/A,N/A,\n"
        "2026-10-02,1.1650,169.9,\n2026-10-01,1.1712,169.5,\n")


@pytest.mark.parametrize("text", [SDMX, HIST])
def test_both_official_formats(text: str) -> None:
    t = ecb.parse_rates(text, "f")
    assert t.rate_for(date(2026, 10, 2)) == (D("1.1650"), date(2026, 10, 2))
    # Sábado y domingo: último tipo publicado (viernes)
    assert t.rate_for(date(2026, 10, 4)) == (D("1.1650"), date(2026, 10, 2))


def test_usd_to_eur_and_lookback_limit() -> None:
    t = ecb.parse_rates(SDMX, "f")
    eur, rate, used = t.usd_to_eur(D("116.50"), date(2026, 10, 3))
    assert (eur, rate, used) == (D(100), D("1.1650"), date(2026, 10, 2))
    with pytest.raises(ecb.RateError):
        t.rate_for(date(2026, 9, 30))  # anterior a la serie
    with pytest.raises(ecb.RateError):
        t.rate_for(date(2026, 10, 20))  # más de 7 días sin tipo


@pytest.mark.parametrize("bad", ["a,b\n1,2\n", "Date,USD\n", "Date,USD\nayer,1.1\n",
                                 "Date,USD\n2026-01-01,-1\n"])
def test_bad_files(bad: str) -> None:
    with pytest.raises(ecb.RateError):
        ecb.parse_rates(bad, "f")


@respx.mock
def test_fetch_from_ecb_api() -> None:
    route = respx.get(ecb.ECB_API_URL).mock(return_value=httpx.Response(200, text=SDMX))
    t = ecb.fetch(date(2026, 10, 2), date(2026, 10, 5))
    params = route.calls.last.request.url.params
    assert params["format"] == "csvdata" and params["startPeriod"] == "2026-09-25"
    assert t.rate_for(date(2026, 10, 5))[0] == D("1.16") and "data-api.ecb" in t.origin
    route.mock(return_value=httpx.Response(503))
    with pytest.raises(ecb.RateError, match="--ecb-csv"):
        ecb.fetch(date(2026, 10, 2), date(2026, 10, 5))
