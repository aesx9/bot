from __future__ import annotations

import csv
import stat
from decimal import Decimal as D
from pathlib import Path

from copybot.exchange.base import FundingEvent
from copybot.records import FUNDING_HEADER, TRADES_HEADER, CsvRecorder, TradeRecord
from copybot.redaction import register_secret
from tests.fakes import NOW


def trade(side: str, fill: str, cli: str = "abc") -> TradeRecord:
    return TradeRecord(NOW, "paper", "PF_SOLUSD", "open", side, D(1), False, D(114), D(100),
                       D(fill), D("0.05"), D("1.5"), cli, "filled")


def test_slippage_sign_is_cost() -> None:
    assert trade("buy", "100.5").slippage_bps == D("50.00")  # compró más caro
    assert trade("sell", "99.5").slippage_bps == D("50.00")  # vendió más barato
    assert trade("buy", "99.9").slippage_bps == D("-10.00")  # mejora


def test_csv_files_header_once_private_and_utc(tmp_path: Path) -> None:
    rec = CsvRecorder(tmp_path)
    rec.trade(trade("buy", "100.5"))
    rec.trade(trade("sell", "99"))
    path = tmp_path / "trades.csv"
    rows = list(csv.reader(path.open()))
    assert tuple(rows[0]) == TRADES_HEADER and len(rows) == 3
    assert rows[1][0].endswith("+00:00")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_funding_separates_paid_and_received(tmp_path: Path) -> None:
    rec = CsvRecorder(tmp_path)
    rec.funding(FundingEvent(NOW, "PF_XBTUSD", D(1), D("0.5"), D("-0.5")), "paper")
    rec.funding(FundingEvent(NOW, "PF_XBTUSD", D(-1), D("0.5"), D("0.5")), "paper")
    rows = list(csv.DictReader((tmp_path / "funding.csv").open()))
    assert tuple(rows[0]) == FUNDING_HEADER
    assert (rows[0]["pagado_usd"], rows[0]["cobrado_usd"]) == ("0.5", "0")
    assert (rows[1]["pagado_usd"], rows[1]["cobrado_usd"]) == ("0", "0.5")


def test_secrets_never_reach_csv(tmp_path: Path) -> None:
    secret = "s3cr3t-value-123"  # pragma: allowlist secret
    register_secret(secret)
    CsvRecorder(tmp_path).trade(trade("buy", "100", cli=secret))
    assert secret not in (tmp_path / "trades.csv").read_text()


def test_decimals_are_written_without_scientific_notation(tmp_path: Path) -> None:
    rec = TradeRecord(NOW, "live", "PF_PEPEUSD", "open", "buy", D("5E+3"), False, None,
                      D("9E-7"), D("9E-7"), None, None, "abc", "filled")
    CsvRecorder(tmp_path).trade(rec)
    row = list(csv.DictReader((tmp_path / "trades.csv").open()))[0]
    assert (row["tamano"], row["precio_referencia"]) == ("5000", "0.0000009")
