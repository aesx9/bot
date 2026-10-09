"""Extremo a extremo: informe, CSV y CLI funcionan con datos locales y sin red."""

from __future__ import annotations

import csv
from pathlib import Path

import httpx
import pytest

from backtest.cli import main
from backtest.config import SYMBOLS, Criteria, Scenario
from backtest.data import save_candles, save_funding
from backtest.report import CSV_COLUMNS, render_report, write_outputs
from backtest.runner import DEV, RESERVED, TOTAL, Check, run_all
from backtest.tests.helpers import synthetic_dataset


def test_report_has_every_required_section_and_metric() -> None:
    candles, funding = synthetic_dataset(n=1200)
    r = run_all(candles, funding, n_random=15)
    text = render_report(r)
    for heading in ("## 1. Veredicto", "## 2. Datos", "## 3. Reglas", "## 4. Resultados",
                    "## 5. Funding", "## 6. Buy & hold", "## 7. Robustez", "## 8. Comparación",
                    "## 9. Limitaciones"):
        assert heading in text
    for metric in ("Rent. neta", "Ops", "Acierto", "Profit factor", "DD máx", "Sharpe",
                   "Comisiones", "Slippage", "Funding real", "Funding imputado", "Percentil"):
        assert metric in text
    for symbol in candles:
        assert symbol in text
    assert text.count("#### Desarrollo") == 2 and text.count("#### Reservado") == 2


def test_criteria_are_evaluated_with_the_pessimistic_scenario() -> None:
    candles, funding = synthetic_dataset(n=1200)
    r = run_all(candles, funding, n_random=15, criteria=Criteria(min_trades=10_000))
    by_name = {c.name: c for c in r.checks}
    trades = by_name["Operaciones en el reservado"]
    pess = r.outcomes[Scenario.PESIMISTA][RESERVED].stats[TOTAL]
    assert trades.passed is False and f"{pess.n_trades} operaciones" in trades.detail
    assert r.verdict is False
    assert len(r.checks) == 5  # las cinco condiciones de aceptación
    p = r.percentiles[(Scenario.PESIMISTA, RESERVED)]
    assert f"percentil {p:.1f}".replace(".", ",") in by_name[
        "Percentil frente al azar (reservado)"
    ].detail


@pytest.mark.parametrize(
    ("states", "expected"),
    [([True, True], True), ([True, False], False), ([True, None], None), ([None, False], False)],
)
def test_verdict_requires_every_criterion(states: list[bool | None], expected: bool | None) -> None:
    candles, funding = synthetic_dataset(n=900)
    r = run_all(candles, funding, n_random=3)
    r.checks = [Check(f"c{i}", "", s) for i, s in enumerate(states)]
    assert r.verdict is expected


def test_trade_csv_matches_the_trades(tmp_path: Path) -> None:
    candles, funding = synthetic_dataset(n=1200)
    r = run_all(candles, funding, n_random=5)
    write_outputs(r, tmp_path / "res", tmp_path / "REPORT.md")
    for sc in Scenario:
        for name in (DEV, RESERVED):
            path = tmp_path / "res" / f"operaciones_{name}_{sc.value}.csv"
            with path.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            trades = r.outcomes[sc][name].run.trades
            assert len(rows) == len(trades)
            assert list(rows[0]) == CSV_COLUMNS if rows else True
            assert sum(float(x["neto"]) for x in rows) == pytest.approx(
                sum(t.net_pnl for t in trades)
            )
    assert (tmp_path / "REPORT.md").read_text(encoding="utf-8").startswith("# Backtest 4h")


def test_cli_runs_from_local_files_without_any_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candles, funding = synthetic_dataset(n=900, symbols=SYMBOLS)
    data = tmp_path / "datos"
    for s in candles:
        save_candles(data, candles[s])
        save_funding(data, funding[s])

    def no_network(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("el backtest no debe usar la red")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    out, report = tmp_path / "out", tmp_path / "REPORT.md"
    code = main(["ejecutar", "--datos", str(data), "--salida", str(out), "--informe", str(report),
                 "--n-azar", "5"])
    assert code == 0 and report.exists()
    assert len(list(out.glob("operaciones_*.csv"))) == 4
    # dos ejecuciones con los mismos datos y la misma semilla dan el mismo informe
    again = tmp_path / "REPORT2.md"
    main(["ejecutar", "--datos", str(data), "--salida", str(out), "--informe", str(again),
          "--n-azar", "5"])
    assert report.read_text(encoding="utf-8") == again.read_text(encoding="utf-8")


def test_cli_solo_desarrollo_writes_no_reserved_files(tmp_path: Path) -> None:
    candles, funding = synthetic_dataset(n=900, symbols=SYMBOLS)
    data = tmp_path / "datos"
    for s in candles:
        save_candles(data, candles[s])
        save_funding(data, funding[s])
    out = tmp_path / "out"
    main(["ejecutar", "--datos", str(data), "--salida", str(out), "--informe",
          str(tmp_path / "R.md"), "--n-azar", "3", "--solo-desarrollo"])
    assert not list(out.glob("*reservado*"))
    assert "No ejecutado" in (tmp_path / "R.md").read_text(encoding="utf-8")


def test_every_markdown_table_row_has_the_same_number_of_columns_as_its_header() -> None:
    """Un `|` dentro de una celda rompe la tabla; esto lo detecta en todo el informe."""
    candles, funding = synthetic_dataset(n=1200)
    lines = render_report(run_all(candles, funding, n_random=5)).splitlines()
    columns = None
    for line in lines:
        if not line.startswith("|"):
            columns = None
            continue
        n = len(line.strip().strip("|").split("|"))
        columns = columns or n
        assert n == columns, line


def test_real_funding_share_does_not_net_opposite_signs() -> None:
    from backtest.report import _real_share

    candles, funding = synthetic_dataset(n=1200)
    r = run_all(candles, funding, n_random=3)
    trades = r.outcomes[Scenario.PESIMISTA][RESERVED].run.trades
    assert any(t.funding_real > 0 for t in trades)
    parts = [_real_share([t for t in trades if t.asset == a]) for a in candles]
    total = _real_share(trades)
    values = [float(x.rstrip(" %").replace(",", ".")) for x in (*parts, total)]
    assert min(values[:-1]) <= values[-1] <= max(values[:-1])  # el total queda entre sus partes
