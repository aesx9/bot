from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from copybot.analysis.report import build_report, format_text, main, max_drawdown_pct
from copybot.exchange.base import FundingEvent
from copybot.records import CsvRecorder, TradeRecord

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def trade(rec: CsvRecorder, mode: str, minute: int, sym: str, side: str, size: str,
          ref: str, fill: str, fee: str | None, delay: str | None = "2") -> None:
    rec.trade(TradeRecord(T0 + timedelta(minutes=minute), mode, sym, "open", side, D(size),
                          False, None, D(ref), D(fill), None if fee is None else D(fee),
                          None if delay is None else D(delay), f"id{minute}", "filled"))


@pytest.fixture
def data(tmp_path: Path) -> Path:
    rec = CsvRecorder(tmp_path)
    trade(rec, "paper", 0, "PF_A", "buy", "2", "100", "100.1", "0.1")
    trade(rec, "paper", 1, "PF_A", "sell", "2", "110", "110", "0.11", delay="4")
    trade(rec, "paper", 2, "PF_B", "sell", "1", "50", "49.9", "0.02")
    trade(rec, "live", 3, "PF_A", "buy", "5", "100", "100", None)  # nunca se mezcla
    rec.funding(FundingEvent(T0, "PF_A", D(2), D("0.5"), D("-1")), "paper")
    rec.funding(FundingEvent(T0, "PF_B", D(-1), D("0.5"), D("0.5")), "paper")
    rec.funding(FundingEvent(T0, "PF_A", D(5), D("0.5"), D("-99")), "live")
    for i, v in enumerate(["500", "550", "440", "520"]):
        rec.equity(T0 + timedelta(hours=i), "paper", D(v), D(1000))
    rec.equity(T0, "live", D(10), None)
    rec.fee({"timestamp": T0, "symbol": "PF_A", "fee": D("0.7"), "currency": "USD",
             "info": "futures trade", "booking_uid": "b"})
    return tmp_path


def test_paper_report_global_and_per_asset(data: Path) -> None:
    r = build_report(data, "paper")
    assert (r.equity_start, r.equity_end, r.return_pct) == (D(500), D(520), D(4))
    assert r.max_drawdown_pct == D(20)  # 550 -> 440
    t = r.total
    assert t["operaciones"] == 3 and t["comisiones_usd"] == "0.23"
    assert t["pnl_realizado_usd"] == "19.80"  # PF_A: (110 - 100.1) x 2
    assert (t["funding_pagado_usd"], t["funding_cobrado_usd"], t["funding_neto_usd"]) == (
        "1.00", "0.50", "-0.50")
    assert t["retraso_medio_s"] == "2.67"
    a = r.by_asset["PF_A"]
    assert a["slippage_medio_pb"] == "5.00"  # +10 pb al comprar, 0 al vender
    assert set(r.by_asset) == {"PF_A", "PF_B"}


def test_live_report_uses_real_fees_and_never_paper_rows(data: Path) -> None:
    r = build_report(data, "live")
    assert r.total["operaciones"] == 1 and r.total["comisiones_usd"] == "0.70"
    assert r.total["funding_pagado_usd"] == "99.00"


def test_max_drawdown() -> None:
    assert max_drawdown_pct([D(100), D(120), D(60), D(130)]) == D(50)
    assert max_drawdown_pct([]) is None


def test_cli(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--data-dir", str(data), "--mode", "paper"]) == 0
    out = capsys.readouterr().out
    assert "Informe (paper)" in out and "-- PF_B --" in out
    assert main(["--data-dir", str(data)]) == 0  # por defecto live si hay datos live
    assert "Informe (live)" in capsys.readouterr().out
    assert main(["--data-dir", str(data / "vacio")]) == 1
    assert main(["--data-dir", str(data), "--mode", "paper", "--json"]) == 0
    assert '"mode": "paper"' in capsys.readouterr().out
    assert "Drawdown" in format_text(build_report(data, "paper"))
