"""Datos: validación, almacenamiento local reproducible y descarga (sin red)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from backtest.config import CANDLE_MS, HOUR_MS
from backtest.data import (
    DataError,
    FundingSeries,
    download_all,
    fetch_candles,
    fetch_funding,
    load_candles,
    load_funding,
    save_candles,
    save_funding,
    validate_candles,
    validate_funding,
)
from backtest.tests.helpers import T0, make_candles, random_walk


def test_candles_roundtrip_is_exact(tmp_path: Path) -> None:
    c = random_walk(300, seed=5, symbol="PF_X")
    save_candles(tmp_path, c)
    assert load_candles(tmp_path, "PF_X") == c


def test_funding_roundtrip_is_exact(tmp_path: Path) -> None:
    f = FundingSeries("PF_X", [T0 + i * HOUR_MS for i in range(5)], [0.1, -2.5e-4, 0.0, 3.0, 1.0],
                      [1e-5, -2e-6, 0.0, 3e-5, 1.5e-5])
    save_funding(tmp_path, f)
    assert load_funding(tmp_path, "PF_X") == f


def test_validate_candles_rejects_gaps_disorder_and_bad_ohlc() -> None:
    good = make_candles([(10, 11, 9, 10), (10, 11, 9, 10), (10, 11, 9, 10)])
    validate_candles(good)
    with pytest.raises(DataError, match="hueco"):
        validate_candles(replace(good, t=[good.t[0], good.t[1], good.t[2] + CANDLE_MS]))
    with pytest.raises(DataError, match="hueco"):
        validate_candles(replace(good, t=[good.t[1], good.t[0], good.t[2]]))
    with pytest.raises(DataError, match="OHLC"):
        validate_candles(replace(good, h=[11, 8, 11]))
    with pytest.raises(DataError, match="no positivo"):
        validate_candles(replace(good, o=[10, 0, 10], l=[9, 0, 9]))


def test_validate_funding_rejects_off_hour_and_unsorted() -> None:
    ok = FundingSeries("X", [T0, T0 + HOUR_MS], [1.0, 1.0], [1e-5, 1e-5])
    validate_funding(ok)
    with pytest.raises(DataError, match="hora en punto"):
        validate_funding(replace(ok, t=[T0 + 1, T0 + HOUR_MS]))
    with pytest.raises(DataError, match="creciente"):
        validate_funding(replace(ok, t=[T0 + HOUR_MS, T0]))


def _charts_handler(n_total: int, page: int) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        start_s = int(request.url.params["from"])
        rows = [
            {"time": T0 + i * CANDLE_MS, "open": "100", "high": "101", "low": "99",
             "close": "100.5", "volume": "3"}
            for i in range(n_total)
            if (T0 + i * CANDLE_MS) // 1000 >= start_s
        ]
        return httpx.Response(200, json={"candles": rows[:page], "more_candles": len(rows) > page})

    return httpx.MockTransport(handler)


def test_fetch_candles_paginates_and_drops_the_candle_in_progress() -> None:
    now_ms = T0 + 9 * CANDLE_MS + CANDLE_MS // 2  # la vela 9 aún no ha cerrado
    with httpx.Client(transport=_charts_handler(n_total=10, page=4)) as client:
        c = fetch_candles(client, "PF_X", now_ms)
    assert c.t == [T0 + i * CANDLE_MS for i in range(9)]  # velas 0-8; la 9 (en curso) fuera
    assert c.o[0] == 100.0 and c.v[0] == 3.0


def test_fetch_candles_keeps_a_candle_that_has_just_closed() -> None:
    with httpx.Client(transport=_charts_handler(n_total=3, page=10)) as client:
        c = fetch_candles(client, "PF_X", T0 + 3 * CANDLE_MS)
    assert len(c) == 3


def test_fetch_funding_parses_iso_timestamps_and_checks_result() -> None:
    body = {"result": "success", "rates": [
        {"timestamp": "2025-10-06T08:00:00Z", "fundingRate": 1.5, "relativeFundingRate": 1.2e-5},
        {"timestamp": "2025-10-06T09:00:00Z", "fundingRate": -0.5, "relativeFundingRate": -4e-6},
    ]}
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as c:
        f = fetch_funding(c, "PF_X")
    assert f.t == [1759737600000, 1759737600000 + HOUR_MS]
    assert f.rate_abs == [1.5, -0.5] and f.rate_rel == [1.2e-5, -4e-6]
    bad = {"result": "error", "rates": []}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=bad))
    with httpx.Client(transport=transport) as c, pytest.raises(DataError):
        fetch_funding(c, "PF_X")


def test_download_all_saves_files_and_manifest_that_load_back(tmp_path: Path) -> None:
    funding_body = {"result": "success", "rates": [
        {"timestamp": "2025-10-06T08:00:00Z", "fundingRate": 1.5, "relativeFundingRate": 1.2e-5},
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        if "historical-funding-rates" in request.url.path:
            return httpx.Response(200, json=funding_body)
        return _charts_handler(n_total=6, page=4).handle_request(request)

    now_ms = T0 + 6 * CANDLE_MS
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        manifest = download_all(tmp_path, ("PF_X",), now_ms, client)
    assert load_candles(tmp_path, "PF_X").t == [T0 + i * CANDLE_MS for i in range(6)]
    assert load_funding(tmp_path, "PF_X").rate_abs == [1.5]
    symbols = manifest["symbols"]
    assert isinstance(symbols, dict) and symbols["PF_X"]["candles"]["rows"] == 6
    assert (tmp_path / "manifest.json").read_text(encoding="utf-8").endswith("}\n")
