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
    pos_path, fund_path, _, notes = export(data, 2026, data / "out", RATES)
    [p] = rows(pos_path)
    assert (p["mercado"], p["direccion"], p["origen_cierre"], p["origenes"]) == (
        "PF_XBTUSD", "largo", "stop_catastrofe", "bot+stop_catastrofe")
    assert (p["resultado_bruto_usd"], p["comisiones_usd"]) == ("100.00", "0.85")
    assert (p["funding_pagado_usd"], p["funding_cobrado_usd"]) == ("2.33", "0.00")
    assert p["resultado_neto_usd"] == "96.82"
    assert (p["fecha_tipo_bce_cierre"], p["tipo_eurusd_bce_cierre"]) == ("2026-10-02", "1.1650")
    assert p["resultado_bruto_eur"] == str((D(100) / D("1.1650")).quantize(D("0.01")))
    assert p["funding_pagado_eur"] == "2.00"  # 2.33 / 1.165 (tipo del día del pago)
    expected_net = (D(100) / D("1.1650") - D("0.85") / D("1.1650") - D(2)).quantize(D("0.01"))
    assert p["resultado_neto_eur"] == str(expected_net)
    assert "BCE" in p["fuente_tipo_cambio"] and "EXR.D.USD.EUR.SP00.A" in p["fuente_tipo_cambio"]
    assert any("PF_ETHUSD" in n for n in notes)


def test_funding_file_is_live_only_and_converted_on_each_date(data: Path) -> None:
    _, fund_path, _, _ = export(data, 2026, data / "out", RATES)
    f = rows(fund_path)
    assert [(r["pagado_usd"], r["cobrado_usd"]) for r in f] == [("2.33", "0"), ("0", "1.16")]
    assert [(r["fecha_tipo_bce"], r["pagado_eur"], r["cobrado_eur"]) for r in f] == [
        ("2026-10-02", "2.00", "0.00"), ("2026-10-05", "0.00", "1.00")]
    assert all("-500" not in r["importe_usd"] for r in f)  # paper excluido


def test_other_years_are_empty(data: Path) -> None:
    pos_path, fund_path, _, _ = export(data, 2025, data / "out", RATES)
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
    pos, fund, _, notes = export(tmp_path, 2026, tmp_path)  # sin datos: no descarga nada
    assert rows(pos) == [] and rows(fund) == [] and notes == []


def test_each_fee_uses_the_rate_of_its_own_day(tmp_path: Path) -> None:
    rec = CsvRecorder(tmp_path)
    mon = FRI + timedelta(days=3)  # lunes 5: tipo 1.16; viernes 2: 1.165
    fill(rec, FRI, "buy", "1", "100", sym="PF_SOLUSD")
    fill(rec, mon, "sell", "1", "100", sym="PF_SOLUSD")
    rec.fee({"timestamp": FRI, "symbol": "PF_SOLUSD", "fee": D("1.165"), "currency": "USD",
             "info": "futures trade", "booking_uid": "1"})
    rec.fee({"timestamp": mon, "symbol": "PF_SOLUSD", "fee": D("1.16"), "currency": "USD",
             "info": "futures trade", "booking_uid": "2"})
    [p] = rows(export(tmp_path, 2026, tmp_path, RATES)[0])
    assert p["comisiones_eur"] == "2.00"  # 1 EUR + 1 EUR, cada una en su fecha
    assert p["fecha_tipo_bce_cierre"] == "2026-10-05"


def test_summary_by_closing_origin(tmp_path: Path) -> None:
    rec = CsvRecorder(tmp_path)
    t = FRI
    for sym, origin, pnl in [("PF_A", "bot", 10), ("PF_B", "manual", -4),
                             ("PF_C", "liquidación", -50), ("PF_D", "bot", 6)]:
        fill(rec, t, "buy", "1", "100", sym=sym, origin="bot" if origin != "manual" else origin)
        fill(rec, t + timedelta(minutes=1), "sell", "1", str(100 + pnl), sym=sym, origin=origin)
    rec.funding(FundingEvent(t + timedelta(days=3), "PF_Z", D(1), D(1), D("-1.16")), "live")
    summary_path = export(tmp_path, 2026, tmp_path, RATES)[2]
    s = {r["categoria"]: r for r in rows(summary_path)}
    assert (s["bot"]["posiciones"], s["bot"]["resultado_bruto_usd"]) == ("2", "16.00")
    assert s["manual"]["resultado_bruto_usd"] == "-4.00"
    assert s["liquidación"]["resultado_bruto_usd"] == "-50.00"
    assert s["stop_catastrofe"]["posiciones"] == "0"
    assert s["TOTAL posiciones cerradas"]["resultado_bruto_usd"] == "-38.00"
    fy = s["funding total del año (fiscal_funding)"]
    assert (fy["funding_pagado_usd"], fy["funding_pagado_eur"]) == ("1.16", "1.00")
    assert all("BCE" in r["fuente_tipo_cambio"] for r in s.values())


# --- M14: reglas fiscales que las mutaciones dejaron sin cubrir ---

RATES_YEAR_END = ecb.parse_rates(
    "TIME_PERIOD,OBS_VALUE\n2025-12-30,1.1700\n2026-01-02,1.1800\n2026-01-05,1.1900\n", "prueba")


def test_a_position_belongs_to_the_year_it_was_closed_not_opened(tmp_path: Path) -> None:
    rec = CsvRecorder(tmp_path)
    fill(rec, datetime(2025, 12, 30, 12, tzinfo=UTC), "buy", "1", "100", sym="PF_SOLUSD")
    fill(rec, datetime(2026, 1, 2, 12, tzinfo=UTC), "sell", "1", "110", sym="PF_SOLUSD")
    assert len(rows(export(tmp_path, 2026, tmp_path / "a", RATES_YEAR_END)[0])) == 1
    assert rows(export(tmp_path, 2025, tmp_path / "b", RATES_YEAR_END)[0]) == []


def test_each_funding_in_a_position_uses_the_rate_of_its_payment_day(tmp_path: Path) -> None:
    """Pagado el viernes 2 (1,165) con la posición cerrada el lunes 5 (1,16)."""
    rec = CsvRecorder(tmp_path)
    fill(rec, FRI, "buy", "1", "100", sym="PF_SOLUSD")
    fill(rec, FRI + timedelta(days=3), "sell", "1", "100", sym="PF_SOLUSD")
    rec.funding(FundingEvent(FRI + timedelta(hours=1), "PF_SOLUSD", D(1), D(1), D("-2.33")),
                "live")
    [p] = rows(export(tmp_path, 2026, tmp_path, RATES)[0])
    assert p["funding_pagado_eur"] == "2.00"  # 2.33 / 1.165; con el tipo del cierre sería 2.01
    assert p["fecha_tipo_bce_cierre"] == "2026-10-05"


# --- B3: fechas en hora de Madrid ---


def test_madrid_time_matches_the_tz_database_for_every_hour_of_several_years() -> None:
    from zoneinfo import ZoneInfo

    from copybot.analysis.positions import madrid

    zone, t = ZoneInfo("Europe/Madrid"), datetime(2024, 1, 1, tzinfo=UTC)
    while t.year < 2032:
        expected = t.astimezone(zone)
        got = madrid(t)
        assert (got.year, got.month, got.day, got.hour) == (
            expected.year, expected.month, expected.day, expected.hour), t
        t += timedelta(minutes=30)


def test_position_closed_at_year_end_utc_belongs_to_the_next_year_in_spain(tmp_path: Path) -> None:
    """31/12/2025 23:30 UTC = 1/1/2026 00:30 en Madrid: año 2026 y tipo del 1 de enero."""
    rec = CsvRecorder(tmp_path)
    fill(rec, datetime(2025, 12, 30, 12, tzinfo=UTC), "buy", "1", "100", sym="PF_SOLUSD")
    fill(rec, datetime(2025, 12, 31, 23, 30, tzinfo=UTC), "sell", "1", "110", sym="PF_SOLUSD")
    rates = ecb.parse_rates(
        "TIME_PERIOD,OBS_VALUE\n2025-12-30,1.1700\n2025-12-31,1.1750\n2026-01-02,1.1800\n", "p")
    assert rows(export(tmp_path, 2025, tmp_path / "a", rates)[0]) == []
    [p] = rows(export(tmp_path, 2026, tmp_path / "b", rates)[0])
    # 1 de enero no es hábil para el BCE: se usa el último tipo anterior (31/12)
    assert (p["fecha_tipo_bce_cierre"], p["tipo_eurusd_bce_cierre"]) == ("2025-12-31", "1.1750")
    assert p["cierre_utc"].startswith("2025-12-31T23:30")  # los CSV siguen en UTC


# --- B4: export atómico y funding contado una vez ---


def test_failed_rate_lookup_leaves_no_partial_files_and_keeps_the_previous_export(
    tmp_path: Path, data: Path
) -> None:
    out = data / "out"
    export(data, 2026, out, RATES)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    assert len(before) == 3
    old_rates = ecb.parse_rates("TIME_PERIOD,OBS_VALUE\n2026-09-01,1.1650\n", "demasiado vieja")
    with pytest.raises(ecb.RateError):
        export(data, 2026, out, old_rates)  # no hay tipo en los 7 días anteriores
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before  # ni parciales ni mezcla


def test_export_files_are_private(data: Path) -> None:
    import stat

    for p in export(data, 2026, data / "out", RATES)[:3]:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_funding_at_a_direction_change_is_counted_once_and_by_the_open_position(
    tmp_path: Path,
) -> None:
    rec = CsvRecorder(tmp_path)
    t1 = FRI + timedelta(hours=1)
    fill(rec, FRI, "buy", "1", "100", sym="PF_SOLUSD")
    fill(rec, t1, "sell", "2", "100", sym="PF_SOLUSD")  # cierra el largo y abre un corto de 1
    fill(rec, FRI + timedelta(hours=3), "buy", "1", "100", sym="PF_SOLUSD")
    rec.funding(FundingEvent(t1 + timedelta(seconds=1), "PF_SOLUSD", D(-1), D(1), D("-3.00")),
                "live")
    pos, _, _, _ = export(tmp_path, 2026, tmp_path / "out", RATES)
    paid = {r["direccion"]: D(r["funding_pagado_usd"]) for r in rows(pos)}
    # una sola vez, y en el corto, que es la posición abierta cuando se paga
    assert paid == {"largo": D("0.00"), "corto": D("3.00")}
