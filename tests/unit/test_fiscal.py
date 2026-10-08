from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from copybot.analysis import ecb
from copybot.analysis.fiscal import export, main
from copybot.exchange.base import FundingEvent
from copybot.records import CsvRecorder, TradeRecord
from tests.unit.test_ecb import SDMX

FRI = datetime(2026, 10, 2, 15, tzinfo=UTC)  # viernes
RATES = ecb.parse_rates(SDMX, "prueba")  # 2 oct: 1.1650; 5 oct: 1.16


def fill(rec: CsvRecorder, t: datetime, side: str, size: str, price: str, origin: str = "bot",
         sym: str = "PF_XBTUSD", fid: str = "") -> None:
    rec.kraken_fill({"timestamp": t, "symbol": sym, "side": side, "size": D(size),
                     "price": D(price), "fill_type": "taker", "origin": origin,
                     "cli_ord_id": "", "fill_id": fid or f"{t.timestamp()}{side}",
                     "order_id": "o"})


@pytest.fixture
def data(tmp_path: Path) -> Path:
    rec = CsvRecorder(tmp_path)
    # Posición 1: largo cerrado el sábado 3 (tipo del viernes 2): +100 USD bruto
    fill(rec, FRI, "buy", "0.01", "80000")
    fill(rec, FRI + timedelta(days=1), "sell", "0.01", "90000", origin="stop_catastrofe")
    rec.fee({"timestamp": FRI, "symbol": "PF_XBTUSD", "fee": D("0.40"), "currency": "USD",
             "info": "futures trade", "booking_uid": "1"})
    rec.fee({"timestamp": FRI + timedelta(days=1), "symbol": "PF_XBTUSD", "fee": D("0.45"),
             "currency": "USD", "info": "futures trade", "booking_uid": "2"})
    rec.funding(FundingEvent(FRI + timedelta(hours=1), "PF_XBTUSD", D("0.01"), D(1),
                             D("-2.33")), "live")  # pagado el viernes
    rec.funding(FundingEvent(FRI + timedelta(days=3), "PF_XBTUSD", D(0), D(1), D("1.16")),
                "live")  # cobrado el lunes 5, fuera de la posición
    # Operaciones paper: deben ignorarse siempre
    rec.funding(FundingEvent(FRI + timedelta(hours=2), "PF_XBTUSD", D(1), D(1), D("-500")),
                "paper")
    rec.trade(TradeRecord(FRI, "paper", "PF_ETHUSD", "open", "buy", D(1), False, None, D(1),
                          D(1), D(0), None, "p", "filled"))
    # Posición 2: corto abierto aún (no se declara)
    fill(rec, FRI + timedelta(days=3), "sell", "1", "2500", sym="PF_ETHUSD")
    return tmp_path


def rows(path: Path) -> list[dict[str, Any]]:
    return list(csv.DictReader(path.open(encoding="utf-8")))


def test_positions_file(data: Path) -> None:
    pos_path, fund_path, notes = export(data, 2026, data / "out", RATES)
    [p] = rows(pos_path)
    assert (p["mercado"], p["direccion"], p["origen"]) == ("PF_XBTUSD", "largo",
                                                          "bot+stop_catastrofe")
    assert (p["resultado_bruto_usd"], p["comisiones_usd"]) == ("100.00", "0.85")
    assert (p["funding_pagado_usd"], p["funding_cobrado_usd"]) == ("2.33", "0.00")
    assert p["resultado_neto_usd"] == "96.82"
    assert (p["fecha_tipo_bce"], p["tipo_eurusd_bce"]) == ("2026-10-02", "1.1650")
    assert p["resultado_bruto_eur"] == str((D(100) / D("1.1650")).quantize(D("0.01")))
    assert p["funding_pagado_eur"] == "2.00"  # 2.33 / 1.165 (tipo del día del pago)
    expected_net = (D(100) / D("1.1650") - D("0.85") / D("1.1650") - D(2)).quantize(D("0.01"))
    assert p["resultado_neto_eur"] == str(expected_net)
    assert "BCE" in p["fuente_tipo_cambio"] and "EXR.D.USD.EUR.SP00.A" in p["fuente_tipo_cambio"]
    assert any("PF_ETHUSD" in n for n in notes)


def test_funding_file_is_live_only_and_converted_on_each_date(data: Path) -> None:
    _, fund_path, _ = export(data, 2026, data / "out", RATES)
    f = rows(fund_path)
    assert [(r["pagado_usd"], r["cobrado_usd"]) for r in f] == [("2.33", "0"), ("0", "1.16")]
    assert [(r["fecha_tipo_bce"], r["pagado_eur"], r["cobrado_eur"]) for r in f] == [
        ("2026-10-02", "2.00", "0.00"), ("2026-10-05", "0.00", "1.00")]
    assert all("-500" not in r["importe_usd"] for r in f)  # paper excluido


def test_other_years_are_empty(data: Path) -> None:
    pos_path, fund_path, _ = export(data, 2025, data / "out", RATES)
    assert rows(pos_path) == [] and rows(fund_path) == []


def test_eur_denominated_fees_are_not_converted_twice(tmp_path: Path) -> None:
    rec = CsvRecorder(tmp_path)
    fill(rec, FRI, "buy", "1", "100", sym="PF_SOLUSD")
    fill(rec, FRI + timedelta(minutes=5), "sell", "1", "100", sym="PF_SOLUSD")
    rec.fee({"timestamp": FRI, "symbol": "PF_SOLUSD", "fee": D("1"), "currency": "EUR",
             "info": "futures trade", "booking_uid": "1"})
    [p] = rows(export(tmp_path, 2026, tmp_path, RATES)[0])
    assert (p["comisiones_eur"], p["comisiones_usd"]) == ("1.00", "1.17")


def test_cli_with_local_ecb_file(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ecb_file = data / "eurofxref-hist.csv"
    ecb_file.write_text(SDMX)
    assert main(["--year", "2026", "--data-dir", str(data), "--ecb-csv", str(ecb_file)]) == 0
    out = capsys.readouterr().out
    assert "fiscal_posiciones_2026.csv" in out and "Solo incluye operaciones reales" in out
    bad = data / "malo.csv"
    bad.write_text("x,y\n")
    assert main(["--year", "2026", "--data-dir", str(data), "--ecb-csv", str(bad)]) == 1


def test_no_live_data_needs_no_rates(tmp_path: Path) -> None:
    pos, fund, notes = export(tmp_path, 2026, tmp_path)  # sin datos: no descarga nada
    assert rows(pos) == [] and rows(fund) == [] and notes == []
