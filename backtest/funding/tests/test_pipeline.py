"""De punta a punta sin red: APIs simuladas → universo → series → ejecución → informe."""

from __future__ import annotations

import json
import math
from pathlib import Path

import httpx
import pytest

from backtest.data import DataError, iso
from backtest.funding import cli
from backtest.funding import data as fdata
from backtest.funding.config import DAY_MS, HOUR_MS, WINDOW_A_START_MS
from backtest.funding.download import download_series, download_universe, load_universe
from backtest.funding.report import render_universe

NOW = WINDOW_A_START_MS + 208 * DAY_MS + 10 * HOUR_MS + 30 * 60_000  # 2026-10-10T10:30Z
DATA_START = NOW - 380 * DAY_MS
COINS = {"BTC": ("PF_XBTUSD", 50_000.0), "ETH": ("PF_ETHUSD", 3_000.0),
         "LOW": ("PF_LOWUSD", 1.0)}


def _price(base: str, t: int, venue: str) -> float:
    p0 = COINS[base][1]
    wave = 1.0 + 0.05 * math.sin(t / (9 * DAY_MS))
    basis = {"kraken": 1.0, "hl": 1.0005, "spot": 0.9995}[venue]
    return p0 * wave * basis


def _rate(base: str, t: int, venue: str) -> float:
    """Funding horario: periodos alternos de 6 días a 40 % anual y 6 días a 0."""
    hi = (t // (6 * DAY_MS)) % 2 == 0
    shift = 1 if base == "ETH" else 0
    on = hi if (venue == "kraken") != bool(shift) else not hi
    return (0.40 if on else 0.0) / 8760


def _candles(base: str, venue: str, step: int, frm: int, to: int, limit: int) -> list[int]:
    start = max(-(-frm // step) * step, DATA_START)
    times = list(range(start, to, step))
    return times[-limit:] if venue == "hl" else times[:limit]


def _volume(base: str, step: int) -> float:
    per_day = 1e5 if base == "LOW" else 5e8  # USD
    return per_day / COINS[base][1] * step / DAY_MS


def _kraken(req: httpx.Request, spot_blocked: bool) -> httpx.Response:
    path = req.url.path
    if req.url.host == "api.kraken.com":
        if spot_blocked:
            return httpx.Response(403)
        pairs = {"XXBTZUSD": {"wsname": "XBT/USD"}, "XETHZUSD": {"wsname": "ETH/USD"},
                 "LOWUSD": {"wsname": "LOW/USD"}}
        return httpx.Response(200, json={"error": [], "result": pairs})
    if path.endswith("/instruments"):
        inst = [{"symbol": sym, "type": "flexible_futures", "tradeable": True, "quote": "USD",
                 "base": base, "marginLevels": [{"maintenanceMargin": 0.005}]}
                for base, (sym, _) in COINS.items()]
        return httpx.Response(200, json={"result": "success", "instruments": inst})
    base = next(b for b, (s, _) in COINS.items() if s in str(req.url))
    if path.endswith("/historical-funding-rates"):
        hours = range(DATA_START - DATA_START % HOUR_MS + 10 * DAY_MS, NOW, HOUR_MS)
        rates = [{"timestamp": iso(t), "fundingRate": 0.0,
                  "relativeFundingRate": _rate(base, t, "kraken")} for t in hours]
        return httpx.Response(200, json={"result": "success", "rates": rates})
    _, _, _, _, tick, _sym, res = path.split("/")
    step = HOUR_MS if res == "1h" else DAY_MS
    times = _candles(base, "kraken", step, int(req.url.params["from"]) * 1000,
                     int(req.url.params["to"]) * 1000 + 1, 2000)
    venue = "spot" if tick == "spot" else "kraken"
    candles = []
    for t in times:
        p = _price(base, t, venue)
        candles.append({"time": t, "open": p, "high": p * 1.001, "low": p * 0.999, "close": p,
                        "volume": 0.0 if tick == "spot" else _volume(base, step)})
    more = bool(times) and times[-1] + step < int(req.url.params["to"]) * 1000
    return httpx.Response(200, json={"candles": candles, "more_candles": more})


def _hl(req: httpx.Request) -> httpx.Response:
    body = json.loads(req.content)
    if body["type"] == "metaAndAssetCtxs":
        uni = [{"name": "BTC", "maxLeverage": 40}, {"name": "ETH", "maxLeverage": 25},
               {"name": "LOW", "maxLeverage": 3}, {"name": "OLD", "maxLeverage": 3,
                                                    "isDelisted": True}]
        return httpx.Response(200, json=[{"universe": uni}, []])
    if body["type"] == "candleSnapshot":
        r = body["req"]
        step = HOUR_MS if r["interval"] == "1h" else DAY_MS
        times = _candles(r["coin"], "hl", step, r["startTime"], r["endTime"] + 1, 5000)
        out = []
        for t in times:
            p = _price(r["coin"], t, "hl")
            out.append({"t": t, "o": str(p), "h": str(p * 1.001), "l": str(p * 0.999),
                        "c": str(p), "v": str(_volume(r["coin"], step))})
        return httpx.Response(200, json=out)
    assert body["type"] == "fundingHistory"
    first = -(-body["startTime"] // HOUR_MS) * HOUR_MS
    rows = [{"coin": body["coin"], "fundingRate": str(_rate(body["coin"], t - HOUR_MS, "hl")),
             "time": t + 7} for t in range(first, NOW, HOUR_MS)][:500]
    return httpx.Response(200, json=rows)


def _client(spot_blocked: bool = False) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.hyperliquid.xyz":
            return _hl(req)
        return _kraken(req, spot_blocked)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fdata, "HL_PAUSE_S", 0.0)


@pytest.fixture(scope="module")
def datos(tmp_path_factory: pytest.TempPathFactory) -> Path:
    fdata.HL_PAUSE_S, saved = 0.0, fdata.HL_PAUSE_S
    try:
        d = tmp_path_factory.mktemp("datos")
        with _client() as c:
            download_universe(d, NOW, c)
            download_series(d, NOW, c)
        return d
    finally:
        fdata.HL_PAUSE_S = saved


def test_universe_selection_from_downloaded_daily_volumes(datos: Path) -> None:
    u = load_universe(datos)
    assert sorted(c.base for c in u.selected("A")) == ["BTC", "ETH"]
    assert sorted(c.base for c in u.selected("B")) == ["BTC", "ETH"]
    assert "LOW" in render_universe(u) and "volumen Kraken < 10 M" in render_universe(u)


@pytest.mark.usefixtures("no_pause")
def test_blocked_spot_api_leaves_b_unchecked_and_blocks_series(tmp_path: Path) -> None:
    with _client(spot_blocked=True) as c:
        download_universe(tmp_path, NOW, c)
        u = load_universe(tmp_path)
        assert not u.spot_checked and u.selected("B") == []
        assert "sin comprobar" in render_universe(u)
        with pytest.raises(DataError, match="universo B"):
            download_series(tmp_path, NOW, c)


def test_windows_follow_the_spec(datos: Path) -> None:
    res = cli.run_all(datos, only_dev=True)
    a, b = res
    assert a.window.start >= WINDOW_A_START_MS
    assert a.window.hours <= 208 * 24
    assert b.window.hours == 365 * 24
    assert a.dev.stats.positions > 0 and b.dev.stats.positions > 0
    assert a.dev.stats.transfers >= 1  # la transferencia inicial
    assert b.dev.stats.transfers == 0
    assert b.taker_dev is not None
    assert b.taker_dev.stats.fees > b.dev.stats.fees  # spot taker cuesta más


def test_full_run_report_and_csv(datos: Path, tmp_path: Path) -> None:
    from backtest.funding.config import Account, Costs
    from backtest.funding.report import Meta, write_outputs

    results = cli.run_all(datos, only_dev=False)
    meta = Meta("abc123", "2026-10-10T10:30:00Z", False, Account(), Costs())
    write_outputs(results, meta, tmp_path / "res", tmp_path / "REPORT.md")
    text = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
    for needle in ("Veredicto", "Transferencias", "Coste transferencias", "Funding cobrado",
                   "Resultado por base", "Liquidaciones", "índice spot", "Días para cubrir",
                   "spot taker", "Robustez", "Por activo", "abc123", "3,00 USD por movimiento"):
        assert needle in text, needle
    for r in results:
        assert r.verdict is not None
        csv_text = (tmp_path / "res" / f"posiciones_{r.strategy.value}.csv").read_text()
        assert csv_text.startswith("tramo,activo") and "reservado" in csv_text


def test_cli_ejecutar_solo_desarrollo(datos: Path, tmp_path: Path) -> None:
    rc = cli.main(["ejecutar", "--solo-desarrollo", "--datos", str(datos), "--salida",
                   str(tmp_path / "res"), "--informe", str(tmp_path / "R.md")])
    assert rc == 0
    text = (tmp_path / "R.md").read_text(encoding="utf-8")
    assert "**no ejecutado**" in text and "sin emitir" in text
    assert not (tmp_path / "res" / cli.LOCK).exists()


def test_reserved_guard_runs_once_and_only_from_a_clean_commit(tmp_path: Path) -> None:
    lock = tmp_path / cli.LOCK
    with pytest.raises(cli.FrozenError, match="sin commit"):
        cli.guard_reserved(lock, "abc", dirty=True)
    cli.guard_reserved(lock, "abc", dirty=False)
    lock.write_text(json.dumps({"commit": "abc", "at": "2026-10-10T00:00:00Z"}))
    with pytest.raises(cli.FrozenError, match="ya se ejecutó"):
        cli.guard_reserved(lock, "def", dirty=False)


def test_cli_refuses_a_second_reserved_run(datos: Path, tmp_path: Path) -> None:
    out = tmp_path / "res"
    out.mkdir()
    (out / cli.LOCK).write_text(json.dumps({"commit": "abc", "at": "x"}))
    rc = cli.main(["ejecutar", "--datos", str(datos), "--salida", str(out), "--informe",
                   str(tmp_path / "R.md")])
    assert rc == 2 and not (tmp_path / "R.md").exists()
