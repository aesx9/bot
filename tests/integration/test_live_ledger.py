"""M3: el libro fiscal live no pierde ni duplica fills, comisiones ni funding.

Regresiones de la auditoría (PoC D): /fills solo devuelve los 100 últimos, el cursor del
account-log avanzaba antes de escribir los CSV y las entradas del mismo milisegundo se
perdían o se repetían."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from copybot.exchange import live
from copybot.exchange.base import FundingEvent
from copybot.records import CsvRecorder
from tests.integration.test_live_exchange import BTC, NOW, SOL, Env, env  # noqa: F401

START_MS = int(NOW.timestamp() * 1000)


def fill_at(ms_after_start: int, fid: str, side: str = "buy", size: str = "1") -> dict[str, Any]:
    t = datetime.fromtimestamp((START_MS + ms_after_start) / 1000, tz=UTC)
    return {"cliOrdId": None, "fillTime": t.isoformat(timespec="milliseconds"),
            "fillType": "taker", "fill_id": fid, "order_id": "o", "price": "100",
            "side": side, "size": size, "symbol": SOL}


def fee_at(ms_after_start: int, uid: str, fee: str = "0.1") -> dict[str, Any]:
    ms = START_MS + ms_after_start
    return {"_ms": ms, "date": datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(),
            "info": "futures trade", "contract": "pf_solusd", "fee": fee,
            "collateral": "USD", "booking_uid": uid}


def rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(path.open())) if path.exists() else []


async def started(env: Env) -> None:  # noqa: F811
    await env.live.prepare_ledger(NOW)  # línea base: nada anterior se importa


# --- /fills: paginación ---


@pytest.mark.parametrize("inclusive", [False, True])
async def test_fills_beyond_the_first_page_are_fetched(
        env: Env, monkeypatch: pytest.MonkeyPatch, inclusive: bool) -> None:  # noqa: F811
    """PoC D: /fills da solo los 100 más recientes; un hueco de más de 100 fills entre dos
    sondeos (reinicio largo, ráfaga de stops) perdía los más antiguos para siempre."""
    monkeypatch.setattr(live, "FILLS_PAGE", 3)
    env.kraken.fills_page, env.kraken.fills_inclusive = 3, inclusive
    await started(env)
    env.kraken.fills = [fill_at(1000 * i, f"f{i}") for i in range(1, 8)]
    await env.live.collect_funding(NOW + timedelta(minutes=6))
    fills, _ = env.live.drain_ledger()
    assert sorted(f["fill_id"] for f in fills) == [f"f{i}" for i in range(1, 8)]


@pytest.mark.parametrize("inclusive", [False, True])
async def test_fills_sharing_a_millisecond_at_the_page_edge_are_not_lost(
        env: Env, monkeypatch: pytest.MonkeyPatch, inclusive: bool) -> None:  # noqa: F811
    """El borde de página cae entre dos fills del MISMO milisegundo: pedir "antes del más
    antiguo" dejaba fuera al hermano que no cupo en la página."""
    monkeypatch.setattr(live, "FILLS_PAGE", 3)
    env.kraken.fills_page, env.kraken.fills_inclusive = 3, inclusive
    await started(env)
    env.kraken.fills = [fill_at(1000, "a"), fill_at(2000, "b"), fill_at(2000, "c"),
                        fill_at(3000, "d"), fill_at(4000, "e")]
    await env.live.collect_funding(NOW + timedelta(minutes=6))
    fills, _ = env.live.drain_ledger()
    assert sorted(f["fill_id"] for f in fills) == ["a", "b", "c", "d", "e"]
    assert len({f["fill_id"] for f in fills}) == len(fills)  # y ninguno repetido


async def test_a_full_page_in_one_millisecond_alerts_instead_of_looping(
        env: Env, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(live, "FILLS_PAGE", 2)
    env.kraken.fills_page = 2
    await started(env)
    env.kraken.fills = [fill_at(1000, "a"), fill_at(1000, "b"), fill_at(1000, "c")]
    await env.live.collect_funding(NOW + timedelta(minutes=6))
    assert any("mismo milisegundo" in a for a in env.live.drain_alerts())


# --- el cursor solo avanza cuando el CSV ya está escrito ---


async def test_nothing_is_lost_if_the_csv_write_fails_before_commit(env: Env) -> None:  # noqa: F811
    """PoC D: el cursor y los ids vistos avanzaban al LEER; si escribir el CSV fallaba
    (disco lleno), esos fills y comisiones no volvían a salir jamás."""
    await started(env)
    env.kraken.fills = [fill_at(1000, "f1")]
    env.kraken.logs = [fee_at(1000, "u1"),
                       {**fee_at(2000, "u2"), "info": "funding rate change", "fee": None,
                        "funding_rate": "0.5", "old_balance": "10", "new_balance": "9",
                        "realized_funding": "0"}]
    t = NOW + timedelta(minutes=6)
    first = await env.live.collect_funding(t)
    assert len(first) == 1 and len(env.live.drain_ledger()[0]) == 1
    cursor = env.state.live_funding_cursor_ms
    # (aquí el motor intenta escribir los CSV y falla: no se llama a commit_ledger)
    again = await env.live.collect_funding(t + timedelta(seconds=30))
    fills, fees = env.live.drain_ledger()
    assert [e.booking_uid for e in again] == [e.booking_uid for e in first]
    assert [f["fill_id"] for f in fills] == ["f1"] and [f["booking_uid"] for f in fees] == ["u1"]
    assert env.state.live_funding_cursor_ms == cursor and env.state.fills_seen == []
    env.live.commit_ledger()
    assert env.state.fills_seen == ["f1"]
    assert await env.live.collect_funding(t + timedelta(minutes=6)) == []
    assert env.live.drain_ledger() == ([], [])


async def test_engine_writes_the_csv_before_advancing_the_cursor(
        env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """El motor: si CsvRecorder falla, el cursor no se confirma y el siguiente ciclo lo
    escribe; sin duplicados en el CSV."""
    from copybot.alerts import LogAlerter
    from copybot.config import Config
    from copybot.engine import Engine
    from copybot.state import StateStore
    from tests.conftest import LEADER
    from tests.fakes import FakeLeader, FakeMarket

    rec = CsvRecorder(tmp_path)
    cfg = Config.model_validate({"leader_address": LEADER, "mode": "live"})
    leader = FakeLeader("100000", SOL="0")
    engine = Engine(cfg=cfg, state=env.state, store=StateStore(tmp_path / "s.json"),
                    leader=leader, market=FakeMarket(), exchange=env.live, recorder=rec,
                    alerter=LogAlerter(), kill_dirs=[tmp_path], startup_profile=True,
                    now=lambda: NOW + timedelta(minutes=6))
    await env.live.prepare_ledger(NOW)
    env.kraken.fills = [fill_at(1000, "f1")]
    env.kraken.logs = [fee_at(1000, "u1")]
    real = rec.kraken_fill
    calls = {"n": 0}

    def flaky(f: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        real(f)

    monkeypatch.setattr(rec, "kraken_fill", flaky)
    with pytest.raises(OSError):
        await engine._after_trading(D(100000), {}, NOW)
    assert env.state.fills_seen == []  # no se confirmó nada
    await engine._after_trading(D(100000), {}, NOW)
    assert env.state.fills_seen == ["f1"]
    rows = list(csv.DictReader((tmp_path / "kraken_fills.csv").open()))
    assert [r["fill_id"] for r in rows] == ["f1"]
    fees = list(csv.DictReader((tmp_path / "fees.csv").open()))
    assert [r["booking_uid"] for r in fees] == ["u1"]


# --- account-log: paginación y mismo milisegundo ---


@pytest.mark.parametrize("inclusive", [False, True])
async def test_account_log_is_paged_and_same_millisecond_entries_are_kept(
        env: Env, monkeypatch: pytest.MonkeyPatch, inclusive: bool) -> None:  # noqa: F811
    """PoC D: el cursor pasaba a (último + 1 ms) y solo se pedía una página de 50: lo que
    compartía milisegundo con la última entrada, o iba después de la página, se perdía."""
    monkeypatch.setattr(live, "LOG_PAGE", 3)
    env.kraken.log_since_inclusive = inclusive
    await started(env)
    env.kraken.logs = [fee_at(1000, "u1"), fee_at(2000, "u2"), fee_at(2000, "u3"),
                       fee_at(3000, "u4"), fee_at(3000, "u5"), fee_at(4000, "u6")]
    await env.live.collect_funding(NOW + timedelta(minutes=6))
    _, fees = env.live.drain_ledger()
    assert sorted(f["booking_uid"] for f in fees) == ["u1", "u2", "u3", "u4", "u5", "u6"]
    assert env.live.drain_alerts() == []
    env.live.commit_ledger()
    # llega tarde otra entrada del MISMO milisegundo que la última ya procesada
    env.kraken.logs.append(fee_at(4000, "u7"))
    await env.live.collect_funding(NOW + timedelta(minutes=12))
    _, fees = env.live.drain_ledger()
    assert [f["booking_uid"] for f in fees] == ["u7"]  # solo la nueva, sin repetir u6


@pytest.mark.parametrize("inclusive", [False, True])
async def test_a_whole_log_page_in_one_millisecond_alerts_and_terminates(
        env: Env, monkeypatch: pytest.MonkeyPatch, inclusive: bool) -> None:  # noqa: F811
    monkeypatch.setattr(live, "LOG_PAGE", 2)
    env.kraken.log_since_inclusive = inclusive
    await started(env)
    env.kraken.logs = [fee_at(2000, "u1"), fee_at(2000, "u2"), fee_at(2000, "u3"),
                       fee_at(3000, "u4")]
    await env.live.collect_funding(NOW + timedelta(minutes=6))
    assert any("mismo milisegundo" in a for a in env.live.drain_alerts())
    assert len([c for c in env.kraken.calls if "account-log" in c[1]]) < live.LOG_MAX_PAGES


# --- idempotencia de los CSV ---


def test_ledger_csvs_do_not_duplicate_after_a_crash_between_write_and_commit(
        tmp_path: Path) -> None:
    """Si el proceso cae tras escribir y antes de guardar el estado, la siguiente lectura
    trae lo mismo: un fill duplicado impediría cerrar la posición en el export fiscal."""
    fill = {"timestamp": NOW, "symbol": SOL, "side": "buy", "size": D(1), "price": D(100),
            "fill_type": "taker", "origin": "bot", "cli_ord_id": "c", "fill_id": "f1",
            "order_id": "o"}
    fee = {"timestamp": NOW, "symbol": SOL, "fee": D("0.1"), "currency": "USD",
           "info": "futures trade", "booking_uid": "u1"}
    fee_no_uid = {**fee, "booking_uid": ""}
    event = FundingEvent(NOW, SOL, D(1), D(1), D("-0.5"), booking_uid="u2")
    for _ in range(2):  # el segundo CsvRecorder es el de después del reinicio
        rec = CsvRecorder(tmp_path)
        rec.kraken_fill(fill)
        rec.fee(fee)
        rec.fee(fee_no_uid)
        rec.funding(event, "live")
        rec.kraken_fill(fill)  # y repetido en la misma ejecución
    for name, expected in (("kraken_fills.csv", 1), ("fees.csv", 2), ("funding.csv", 1)):
        assert len((tmp_path / name).read_text().splitlines()) == expected + 1, name


# --- M4: moneda y signo de las entradas del log ---


def funding_entry(ms: int, uid: str, **extra: Any) -> dict[str, Any]:
    e = {"_ms": START_MS + ms,
         "date": datetime.fromtimestamp((START_MS + ms) / 1000, tz=UTC).isoformat(),
         "info": "funding rate change", "contract": "pf_xbtusd", "funding_rate": "0.5",
         "old_balance": "100", "new_balance": "99.5", "realized_funding": "0",
         "booking_uid": uid}
    e.update(extra)
    return {k: v for k, v in e.items() if v is not None}


async def test_funding_in_another_currency_is_kept_with_its_currency(
        env: Env, tmp_path: Path) -> None:  # noqa: F811
    """N4 (PoC P1): el funding que no era USD solo quedaba en una alerta (y en el log, que
    rota); su booking_uid se marcaba visto y nunca volvía. Ahora va a funding_moneda.csv con
    su moneda; avisa si no es USD ni EUR (el export no podría convertirlo)."""
    from copybot.engine import update_ledger

    await started(env)
    env.kraken.logs = [
        funding_entry(1000, "usd", asset="usd"),
        funding_entry(2000, "xbt", asset="xbt", old_balance="0.01", new_balance="0.009"),
        funding_entry(3000, "eur", collateral="EUR", asset="eur"),
        funding_entry(4000, "none"),  # sin moneda: no se supone USD
    ]
    rec = CsvRecorder(tmp_path)
    update = await update_ledger(env.live, rec, NOW + timedelta(minutes=6))
    assert update.funding == 4
    usd = rows(tmp_path / "funding.csv")
    other = rows(tmp_path / "funding_moneda.csv")
    assert [r["booking_uid"] for r in usd] == ["usd"]
    assert {r["booking_uid"]: (r["moneda"], r["importe"]) for r in other} == {
        "xbt": ("XBT", "-0.001"), "eur": ("EUR", "-0.5"), "none": ("DESCONOCIDA", "-0.5")}
    assert [a for a in update.alerts if "MONEDA DEL FUNDING" in a] and not any(
        "booking_uid='eur'" in a for a in update.alerts)  # EUR lo convierte el export
    assert any("XBT" in a for a in update.alerts) and any("DESCONOCIDA" in a
                                                          for a in update.alerts)
    # idempotente por booking_uid, también en el CSV nuevo
    rec.funding(FundingEvent(NOW, "PF_XBTUSD", D(0), D(0), D("-0.5"), "eur", "EUR"), "live")
    assert len(rows(tmp_path / "funding_moneda.csv")) == 3


async def test_collateral_and_asset_disagreeing_is_unknown_and_alerts(
        env: Env) -> None:  # noqa: F811
    """N4: con `collateral` y `asset` distintos se elegía `collateral` en silencio; si era el
    equivocado, las comisiones en USD se convertían como EUR sin ningún aviso."""
    await started(env)
    env.kraken.logs = [funding_entry(1000, "f", asset="usd", collateral="EUR"),
                       {**fee_at(2000, "c"), "collateral": "EUR", "asset": "usd"}]
    events = await env.live.collect_funding(NOW + timedelta(minutes=6))
    _, fees = env.live.drain_ledger()
    assert [e.currency for e in events] == ["DESCONOCIDA"]
    assert [f["currency"] for f in fees] == ["DESCONOCIDA"]
    alerts = env.live.drain_alerts()
    assert sum("DISCREPAN" in a and "EUR" in a and "USD" in a for a in alerts) == 2


async def test_fee_currency_is_recorded_and_never_assumed_usd(env: Env) -> None:  # noqa: F811
    await started(env)
    env.kraken.logs = [
        {**fee_at(1000, "usd"), "collateral": "USD"},
        {**fee_at(2000, "eur"), "collateral": "EUR"},
        {**fee_at(3000, "xbt"), "collateral": None, "asset": "xbt"},
        {**fee_at(4000, "none"), "collateral": None},
    ]
    await env.live.collect_funding(NOW + timedelta(minutes=6))
    _, fees = env.live.drain_ledger()
    assert {f["booking_uid"]: f["currency"] for f in fees} == {
        "usd": "USD", "eur": "EUR", "xbt": "XBT", "none": "DESCONOCIDA"}
    alerts = env.live.drain_alerts()
    # EUR lo convierte el export fiscal: sin alerta; XBT y desconocida sí la llevan
    assert len(alerts) == 2 and all("MONEDA DE LA COMISIÓN" in a for a in alerts)
    assert any("XBT" in a for a in alerts) and any("DESCONOCIDA" in a for a in alerts)


async def test_negative_or_inverted_fee_sign_alerts_and_the_value_is_kept(
        env: Env) -> None:  # noqa: F811
    await started(env)
    env.kraken.logs = [
        {**fee_at(1000, "ok"), "old_balance": "10", "new_balance": "9.9"},  # baja lo que cobra
        {**fee_at(2000, "neg"), "fee": "-0.1"},  # comisión negativa
        {**fee_at(3000, "inv"), "old_balance": "10", "new_balance": "10.1"},  # el saldo sube
        {**fee_at(4000, "pnl"), "old_balance": "10", "new_balance": "10.4",
         "realized_pnl": "0.5"},  # 10.4 = 10 + 0.5 - 0.1: correcto
    ]
    await env.live.collect_funding(NOW + timedelta(minutes=6))
    alerts = env.live.drain_alerts()
    assert len(alerts) == 2 and all("SIGNO DE LA COMISIÓN" in a for a in alerts)
    assert any("'neg'" in a for a in alerts) and any("'inv'" in a for a in alerts)
    _, fees = env.live.drain_ledger()
    assert {f["booking_uid"]: f["fee"] for f in fees}["neg"] == D("-0.1")  # tal cual llega


async def test_fills_seen_keeps_the_most_recent_ids_when_trimmed(
        env: Env, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """N6: las páginas de /fills van de las más recientes a las más antiguas; al recortar
    fills_seen se quedaban los ids ANTIGUOS y cada sondeo volvía a paginar los recientes."""
    monkeypatch.setattr(live, "FILLS_PAGE", 3)
    monkeypatch.setattr(live, "SEEN_MEMORY", 4)
    env.kraken.fills_page = 3
    await started(env)
    env.kraken.fills = [fill_at(1000 * i, f"f{i}") for i in range(1, 8)]
    await env.live.collect_funding(NOW + timedelta(minutes=6))
    env.live.commit_ledger()
    assert env.state.fills_seen == ["f4", "f5", "f6", "f7"]  # los 4 más recientes, en orden
    calls = len(env.kraken.calls)
    await env.live.collect_funding(NOW + timedelta(minutes=7))
    assert env.live.drain_ledger() == ([], [])
    assert sum(path.endswith("/fills") for _, path, _ in env.kraken.calls[calls:]) == 1


async def test_liquidation_fee_is_recorded_and_deducted(env: Env, tmp_path: Path) -> None:  # noqa: F811
    """N7: la comisión de una liquidación viene en `liquidation_fee` (no en `fee`) y se
    ignoraba: el export no la deducía."""
    from copybot.analysis import ecb, fiscal
    from copybot.engine import update_ledger

    await started(env)
    env.kraken.fills = [fill_at(1000, "open", "buy", "1"),
                        {**fill_at(5000, "liq", "sell", "1"), "fillType": "liquidation",
                         "price": "90"}]
    env.kraken.logs = [
        {**fee_at(1000, "t1", fee="0.05"), "collateral": "USD"},
        {**fee_at(5000, "l1", fee="0"), "info": "futures liquidation", "collateral": "USD",
         "liquidation_fee": "1.50"},
    ]
    rec = CsvRecorder(tmp_path)
    await update_ledger(env.live, rec, NOW + timedelta(minutes=6))
    fees = {r["booking_uid"]: (r["comision"], r["concepto"]) for r in rows(tmp_path / "fees.csv")}
    assert fees["l1:liquidation_fee"] == ("1.50", "futures liquidation (liquidation_fee)")
    days = tuple(NOW.date() + timedelta(days=i) for i in range(-3, 3))
    rates = ecb.RateTable(days, tuple(D("1.25") for _ in days), "test")
    pos_path, *_ = fiscal.export(tmp_path, 2026, tmp_path / "out", rates)
    [p] = rows(pos_path)
    assert (p["origen_cierre"], p["comisiones_usd"]) == ("liquidación", "1.55")  # 0.05 + 1.50
    # idempotente: el apunte de liquidación no se duplica en un segundo sondeo
    await update_ledger(env.live, rec, NOW + timedelta(minutes=12))
    assert len(rows(tmp_path / "fees.csv")) == 3
