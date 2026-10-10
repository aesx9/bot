"""Datos: normalización, convenciones de tiempo, paginación y almacenamiento (sin red)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import httpx
import pytest

from backtest.data import DataError, iso
from backtest.funding import data as fdata
from backtest.funding.config import DAY_MS, HOUR_MS
from backtest.funding.data import (
    Bars,
    HlPerp,
    Rates,
    bars_from_rows,
    fetch_hl_bars,
    fetch_hl_funding,
    fetch_kraken_bars,
    fetch_kraken_funding,
    fetch_kraken_perps,
    hl_base,
    hl_rates_from_rows,
    load_bars,
    load_rates,
    save_bars,
    save_rates,
    spot_bases_from_pairs,
    validate_bars,
    validate_rates,
)
from backtest.funding.tests.helpers import T0

Row = tuple[float, float, float, float, float]


def _rows(n: int, start: int = T0, step: int = HOUR_MS) -> dict[int, Row]:
    return {start + i * step: (10.0 + i, 11.0 + i, 9.0 + i, 10.5 + i, 2.0) for i in range(n)}


def test_bars_drop_the_candle_in_progress_and_fill_internal_gaps() -> None:
    rows = _rows(6)
    del rows[T0 + 2 * HOUR_MS]
    now = T0 + 5 * HOUR_MS + 1  # la vela de las T0+5h sigue abierta
    b = bars_from_rows("x", HOUR_MS, rows, now)
    assert b.t == [T0 + i * HOUR_MS for i in range(5)]
    assert b.filled == (T0 + 2 * HOUR_MS,)
    assert (b.o[2], b.h[2], b.l[2], b.c[2], b.v[2]) == (11.5, 11.5, 11.5, 11.5, 0.0)


def test_bars_scale_prices_and_volume_to_base_units() -> None:
    b = bars_from_rows("kPEPE", HOUR_MS, _rows(2), T0 + 10 * HOUR_MS, scale=1 / 1000)
    assert b.c[0] == pytest.approx(10.5 / 1000) and b.v[0] == pytest.approx(2000.0)


def test_validate_bars_rejects_bad_ohlc_and_off_grid() -> None:
    b = bars_from_rows("x", HOUR_MS, _rows(3), T0 + 10 * HOUR_MS)
    bad = Bars("x", HOUR_MS, b.t, b.o, [1.0, 1.0, 1.0], b.l, b.c, b.v)
    with pytest.raises(DataError, match="OHLC"):
        validate_bars(bad)
    off = Bars("x", HOUR_MS, [t + 1 for t in b.t], b.o, b.h, b.l, b.c, b.v)
    with pytest.raises(DataError, match="rejilla"):
        validate_bars(off)
    with pytest.raises(DataError, match="sin velas cerradas"):
        bars_from_rows("x", HOUR_MS, _rows(1), T0)


def test_hyperliquid_funding_time_is_settlement_so_rate_belongs_to_previous_hour() -> None:
    r = hl_rates_from_rows("hl", [
        {"time": T0 + HOUR_MS + 8, "fundingRate": "0.00001"},
        {"time": T0 + 2 * HOUR_MS + 40, "fundingRate": "-0.00002"},
    ])
    assert r.t == [T0, T0 + HOUR_MS] and r.rate == [1e-5, -2e-5]


def test_validate_rates_rejects_off_hour_unsorted_and_nan() -> None:
    with pytest.raises(DataError, match="hora en punto"):
        validate_rates(Rates("x", [T0 + 1], [0.0]))
    with pytest.raises(DataError, match="creciente"):
        validate_rates(Rates("x", [T0 + HOUR_MS, T0], [0.0, 0.0]))
    with pytest.raises(DataError, match="no finita"):
        validate_rates(Rates("x", [T0], [math.nan]))


def test_hl_base_maps_thousand_prefix() -> None:
    assert hl_base("kPEPE") == ("PEPE", 1000.0)
    assert hl_base("BTC") == ("BTC", 1.0)
    assert hl_base("kaito") == ("kaito", 1.0)


def test_spot_bases_normalise_kraken_codes_and_keep_only_usd_online() -> None:
    pairs = [
        {"wsname": "XBT/USD", "status": "online"},
        {"wsname": "XDG/USD"},
        {"wsname": "ETH/EUR"},
        {"wsname": "SOL/USD", "status": "cancel_only"},
        {"altname": "sin wsname"},
    ]
    assert spot_bases_from_pairs(pairs) == {"BTC", "DOGE"}


def test_bars_and_rates_roundtrip_exactly(tmp_path: Path) -> None:
    b = bars_from_rows("x", HOUR_MS, _rows(4), T0 + 10 * HOUR_MS)
    save_bars(tmp_path / "b.csv", b)
    assert load_bars(tmp_path / "b.csv", "x", HOUR_MS) == b
    r = Rates("r", [T0, T0 + HOUR_MS], [1.2345678901234e-5, -3e-7])
    save_rates(tmp_path / "r.csv", r)
    assert load_rates(tmp_path / "r.csv", "r") == r


# --- descarga con transporte simulado -----------------------------------------------------


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_kraken_bars_paginate_until_more_candles_is_false() -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        frm = int(req.url.params["from"])
        calls.append(frm)
        start = max(-(-frm * 1000 // HOUR_MS) * HOUR_MS, T0)  # primera hora en punto ≥ from
        times = [start + i * HOUR_MS for i in range(3)]
        more = len(calls) < 3
        candles = [{"time": t, "open": "1", "high": "2", "low": "0.5", "close": "1.5",
                    "volume": "3"} for t in times]
        return httpx.Response(200, json={"candles": candles, "more_candles": more})

    with _client(handler) as c:
        b = fetch_kraken_bars(c, "trade", "PF_X", "1h", T0, T0 + 100 * HOUR_MS)
    assert len(calls) == 3 and len(b) == 9 and b.filled == ()
    assert calls[1] == (T0 + 2 * HOUR_MS) // 1000 + 1


def test_kraken_funding_uses_relative_rate_at_hour_start() -> None:
    body = {"result": "success", "rates": [
        {"timestamp": "2026-03-16T01:00:00Z", "fundingRate": 9.9, "relativeFundingRate": 2e-6},
        {"timestamp": "2026-03-16T00:00:00Z", "fundingRate": 9.9, "relativeFundingRate": 1e-6},
    ]}
    with _client(lambda req: httpx.Response(200, json=body)) as c:
        r = fetch_kraken_funding(c, "PF_X")
    assert r.t == [T0, T0 + HOUR_MS] and r.rate == [1e-6, 2e-6]


def test_kraken_perps_keep_tradeable_usd_perpetuals_with_first_margin_tier() -> None:
    def inst(sym: str, **kw: Any) -> dict[str, Any]:
        base = {"symbol": sym, "type": "flexible_futures", "tradeable": True, "quote": "USD",
                "base": sym[3:-3], "marginLevels": [{"maintenanceMargin": 0.02}],
                "retailMarginLevels": [{"maintenanceMargin": 0.005}]}
        return {**base, **kw}

    body = {"result": "success", "instruments": [
        inst("PF_XBTUSD", base="BTC"), inst("PF_OLDUSD", tradeable=False),
        inst("FI_XBTUSD_260327", type="futures_inverse"), inst("PF_ETHUSD"),
    ]}
    with _client(lambda req: httpx.Response(200, json=body)) as c:
        perps = fetch_kraken_perps(c)
    assert [(p.symbol, p.base, p.maintenance_margin) for p in perps] == [
        ("PF_ETHUSD", "ETH", 0.005), ("PF_XBTUSD", "BTC", 0.005)]


def test_hyperliquid_funding_paginates_by_start_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fdata, "HL_PAUSE_S", 0.0)
    total = 1200

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        start = payload["startTime"]
        rows: list[dict[str, Any]] = []
        for i in range(total):
            t = T0 + (i + 1) * HOUR_MS + 5
            if t >= start and len(rows) < 500:
                rows.append({"coin": "BTC", "fundingRate": str(i * 1e-8), "time": t})
        return httpx.Response(200, json=rows)

    perp = HlPerp("BTC", "BTC", 1.0, 40)
    with _client(handler) as c:
        r = fetch_hl_funding(c, perp, T0, T0 + 2000 * HOUR_MS)
    assert len(r.t) == total and r.t[0] == T0 and r.t[-1] == T0 + (total - 1) * HOUR_MS


def test_hyperliquid_bars_drop_in_progress_and_scale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fdata, "HL_PAUSE_S", 0.0)
    now = T0 + 2 * DAY_MS + 5
    rows = [{"t": T0 + i * DAY_MS, "o": "1000", "h": "2000", "l": "500", "c": "1500",
             "v": "4"} for i in range(3)]
    with _client(lambda req: httpx.Response(200, json=rows)) as c:
        b = fetch_hl_bars(c, HlPerp("kPEPE", "PEPE", 1000.0, 10), "1d", T0, now)
    assert len(b) == 2 and b.c[0] == pytest.approx(1.5) and b.v[0] == pytest.approx(4000.0)
    assert HlPerp("X", "X", 1.0, 10).maintenance_margin == pytest.approx(0.05)


def test_rate_limited_requests_wait_and_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fdata, "HL_PAUSE_S", 0.0)
    waits: list[float] = []
    monkeypatch.setattr("backtest.funding.data.time.sleep", waits.append)
    calls = iter([429, 429, 200])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(next(calls), json=[])

    with _client(handler) as c:
        assert fdata._post(c, {"type": "x"}) == []
    assert waits == [5.0, 10.0, 0.0]
    monkeypatch.setattr(fdata, "RATE_LIMIT_WAITS_S", (1.0,))
    with _client(lambda req: httpx.Response(429, json=[])) as c, \
            pytest.raises(httpx.HTTPStatusError):
        fdata._post(c, {"type": "x"})


# --- regla de datos completos -------------------------------------------------------------


def test_filled_or_absent_hours_make_a_series_incomplete() -> None:
    from backtest.funding.prepare import Window, incomplete, missing_bars, missing_rates

    w = Window(T0 + 30 * HOUR_MS, T0 + 40 * HOUR_MS)
    rows = _rows(50)
    del rows[T0 + 33 * HOUR_MS]  # se rellena en la descarga: no es dato
    b = bars_from_rows("x", HOUR_MS, rows, T0 + 60 * HOUR_MS)
    assert missing_bars(b, w) == [T0 + 33 * HOUR_MS]
    short = bars_from_rows("y", HOUR_MS, _rows(38), T0 + 60 * HOUR_MS)  # acaba antes
    assert missing_bars(short, w) == [T0 + 38 * HOUR_MS, T0 + 39 * HOUR_MS]
    # El funding también debe cubrir las 24 h de calentamiento.
    r = Rates("f", [T0 + i * HOUR_MS for i in range(7, 40)], [0.0] * 33)
    assert missing_rates(r, w) == [T0 + 6 * HOUR_MS]
    assert incomplete(w, {"Kraken": b}, {}) == [
        f"velas Kraken: 1 hora sin dato (primera {iso(T0 + 33 * HOUR_MS)})"]


def test_missing_funding_is_tolerated_up_to_half_a_percent_of_the_window() -> None:
    from backtest.funding.prepare import Window, incomplete, max_missing_funding

    w = Window(T0 + 24 * HOUR_MS, T0 + 1024 * HOUR_MS)  # 1000 h: se admiten 5 horas
    assert max_missing_funding(w) == 5
    hours = range(0, 1024)

    def rates(gaps: set[int]) -> Rates:
        t = [T0 + i * HOUR_MS for i in hours if i not in gaps]
        return Rates("f", t, [0.0] * len(t))

    # Las horas se cuentan una vez aunque falten en las dos plataformas, también en el
    # calentamiento: 5 distintas se admiten, 6 excluyen.
    assert incomplete(w, {}, {"K": rates({3, 100, 200}), "H": rates({100, 300, 400})}) == []
    assert incomplete(w, {}, {"K": rates({3, 100, 200}), "H": rates({300, 400, 500})}) == [
        f"funding: 6 horas sin dato (primera {iso(T0 + 3 * HOUR_MS)}) en alguna plataforma, "
        "más del 0,50 % de 1000 h (máximo 5)"]


def test_windows_are_fixed_and_never_shortened() -> None:
    from backtest.funding.config import WINDOW_A_START_MS
    from backtest.funding.prepare import window_a, window_b

    end_a = WINDOW_A_START_MS + 207 * DAY_MS  # 2026-10-10
    assert window_a(end_a + 5 * HOUR_MS).end == end_a
    with pytest.raises(DataError, match="después de la descarga"):
        window_a(end_a - HOUR_MS)
    w = window_b(end_a + 5 * HOUR_MS + 7)
    assert (w.start, w.end) == (end_a - 365 * DAY_MS, end_a)
