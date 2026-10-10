from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from copybot.analysis.leaders import (
    DAY_MS,
    Candidate,
    Criteria,
    collateral_summary,
    daily,
    evaluate,
    flow_adjusted_returns,
    leverage_history,
    main,
    max_drawdown_pct,
    parse_leaderboard,
    percentile,
    pnl_at,
    points,
    ranked,
    read_wallets,
    sharpe,
    table,
    write_csv,
)
from copybot.config import SymbolsConfig
from copybot.sources.hyperliquid_rest import STANDARD_MODES, LeaderDataError

FIX = Path(__file__).parent.parent / "fixtures"
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)
MARKETS = {"PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD"}
ADDR = "0x" + "ab" * 20


def fixture(name: str) -> Any:
    return json.loads((FIX / name).read_text("utf-8"))


def history(days: int, step_hours: int, pnl_steps: list[float], capital: float = 50000,
            flows: dict[int, float] | None = None) -> dict[str, Any]:
    """Serie sintética: PnL por paso (cíclico) y flujos de capital en pasos concretos."""
    avh, pnlh = [], []
    pnl, av = 0.0, capital
    steps = days * 24 // step_hours
    for i in range(steps + 1):
        if i:
            gain = pnl_steps[i % len(pnl_steps)]
            pnl += gain
            av += gain + (flows or {}).get(i, 0.0)
        t = NOW_MS - (steps - i) * step_hours * 3_600_000
        avh.append([t, str(av)])
        pnlh.append([t, str(pnl)])
    return {"accountValueHistory": avh, "pnlHistory": pnlh, "vlm": "0"}


def fill(coin: str, side: str, sz: str, px: str, start: str, hours_ago: float) -> dict[str, Any]:
    return {"coin": coin, "side": side, "sz": sz, "px": px, "startPosition": start,
            "time": NOW_MS - int(hours_ago * 3_600_000)}


class FakeInfo:
    def __init__(self, *, mode: str = "default", age_days: int = 200,
                 month_pnl: list[float] | None = None, total_pnl: list[float] | None = None,
                 capital: float = 50000, n_fills: int = 60, coins: tuple[str, ...] = ("BTC",),
                 positions: list[tuple[str, str, str]] | None = None,
                 fills: list[dict[str, Any]] | None = None, month_flows: dict[int, float]
                 | None = None, perp_pnl: list[float] | None = None) -> None:
        self.mode, self.age, self.capital = mode, age_days, capital
        self.month_pnl = month_pnl if month_pnl is not None else [500, -200, 300, 100, -50]
        self.total_pnl = total_pnl if total_pnl is not None else [2000, -800, 1500, 300]
        self.n_fills, self.coins, self.month_flows = n_fills, coins, month_flows
        self.positions = positions if positions is not None else [("BTC", "0.5", "40000")]
        self.fills = fills
        self.perp_pnl = perp_pnl  # unified: PnL de perpetuos (si no, el mismo que el total)

    async def user_abstraction(self, user: str) -> str:
        return self.mode

    async def clearinghouse_raw(self, user: str) -> dict[str, Any]:
        standard = self.mode in STANDARD_MODES
        return {"marginSummary": {"accountValue": str(self.capital if standard else 0)},
                "assetPositions": [{"type": "oneWay", "position": {
                    "coin": c, "szi": s, "positionValue": v}} for c, s, v in self.positions]}

    async def spot_state(self, user: str) -> dict[str, Any]:
        if self.mode in STANDARD_MODES:
            return {"balances": []}
        return {"balances": [{"coin": "USDC", "total": str(self.capital), "entryNtl": "0"}]}

    async def portfolio(self, user: str) -> dict[str, Any]:
        month = history(30, 16, self.month_pnl, self.capital, self.month_flows)
        total = history(self.age, 7 * 24, self.total_pnl, self.capital)
        if self.mode in STANDARD_MODES:
            return {"perpMonth": month, "perpAllTime": total, "month": month, "allTime": total}
        # unified: las series perp* traen capital 0 y solo el PnL de perpetuos
        perp_m = history(30, 16, self.perp_pnl or self.month_pnl, 0)
        perp_t = history(self.age, 7 * 24, self.perp_pnl or self.total_pnl, 0)
        for h in (perp_m, perp_t):
            h["accountValueHistory"] = [[t, "0.0"] for t, _ in h["accountValueHistory"]]
        return {"perpMonth": perp_m, "perpAllTime": perp_t, "month": month, "allTime": total}

    async def user_fills_by_time(self, user: str, start_ms: int) -> list[dict[str, Any]]:
        if self.fills is not None:
            return self.fills
        return [fill(self.coins[i % len(self.coins)], "B" if i % 2 else "A", "0.01", "100",
                     "0", 24 * 29 * (1 - i / max(self.n_fills, 1)))
                for i in range(self.n_fills)]


async def ev(info: FakeInfo, crit: Criteria | None = None) -> Candidate:
    return await evaluate(info, ADDR, "manual", MARKETS, SymbolsConfig(),  # type: ignore[arg-type]
                          crit or Criteria(), NOW)


# --- rentabilidades descontando depósitos y retiros ---


def pts(*rows: tuple[float, float]) -> list[tuple[int, D, D]]:
    return [(i * DAY_MS, D(str(av)), D(str(p))) for i, (av, p) in enumerate(rows)]


def test_deposits_and_withdrawals_are_not_returns() -> None:
    # +100 de PnL y un depósito de 9.900 el mismo día: la rentabilidad no es +100 %
    s = flow_adjusted_returns(pts((10000, 0), (20000, 100)), D(1000))
    assert s.returns == [D(100) / (D(10000) + D(9900) / 2)]
    # Retiro de casi todo con PnL positivo: rentabilidad positiva, no un desplome
    s = flow_adjusted_returns(pts((10000, 0), (1000, 50)), D(500))
    assert s.returns[0] > 0


def test_periods_with_too_little_capital_are_skipped() -> None:
    # Caso real (0x1687...): capital de 1.236 a 37 USD con PnL subiendo y bajando
    rows = ((116, 0), (556, 99), (742, 285), (1236, 1531), (438, 1555), (127, 1124), (37, 1033))
    s = flow_adjusted_returns(pts(*rows), D(1000))
    # Ningún periodo tiene una base de 1.000 USD: no se mide nada (antes daba un DD de
    # 805.085 %); la candidata se descarta por serie no medible
    assert s.returns == [] and s.skipped == 6 and s.skipped_share == 1


def test_losses_are_capped_at_minus_100_percent_and_drawdown_is_bounded() -> None:
    s = flow_adjusted_returns(pts((10000, 0), (100, -20000)), D(1))
    assert s.returns == [D(-1)] and s.max_drawdown_pct == D(100)


def test_window_statistics() -> None:
    s = flow_adjusted_returns(pts((100, 0), (110, 10), (99, -1), (108.9, 8.9)), D(1))
    assert s.period_days == 1
    assert s.total_return_pct.quantize(D("0.01")) == D("8.90")
    assert s.max_drawdown_pct.quantize(D("0.01")) == D("10.00")
    assert sharpe([D("0.01"), D("0.03")], D(365)) == pytest.approx(  # type: ignore[arg-type]
        D("0.02") / D("0.0002").sqrt() * D(365).sqrt())
    assert sharpe([D("0.01"), D("0.01")]) is None and sharpe([D(1)]) is None
    assert max_drawdown_pct([D("0.1"), D("-0.5"), D("0.2")]) == D(50)


def test_real_standard_portfolio_series() -> None:
    port = dict(fixture("hl_portfolio_standard.json"))
    month = points(port["perpMonth"])
    assert month == points(port["month"])  # cuenta estándar: perp y total coinciden
    s = flow_adjusted_returns(daily(month), D(1000))
    assert len(s.returns) >= 25 and s.skipped == 0
    assert D(0) < s.period_days <= D(2)
    total = flow_adjusted_returns(points(port["perpAllTime"]), D(1000))
    assert total.period_days > 3  # la serie total es mucho más espaciada


def test_real_unified_portfolio_has_no_perp_capital() -> None:
    port = dict(fixture("hl_portfolio_unified.json"))
    perp = flow_adjusted_returns(daily(points(port["perpMonth"])), D(1000))
    assert perp.returns == [] and perp.skipped > 0  # capital de perpetuos 0: no medible
    total = flow_adjusted_returns(daily(points(port["month"])), D(1000))
    assert len(total.returns) >= 25 and total.skipped == 0


def test_unified_returns_use_perp_pnl_on_total_capital() -> None:
    total = pts((10000, 0), (11000, 1000), (16000, 1000), (16800, 1800))  # +5000 depósito
    perp = [(0, D(0), D(0)), (DAY_MS, D(0), D(-200)), (3 * DAY_MS, D(0), D(300))]
    s = flow_adjusted_returns(total, D(1000), perp)
    # PnL de perpetuos por periodo: -200, 0 (sin punto nuevo), +500; base = cuenta total
    assert s.returns == [D(-200) / 10000, D(0), D(500) / 16000]
    assert pnl_at(perp, 2 * DAY_MS) == D(-200) and pnl_at(perp, -1) == D(0)


def test_real_unified_total_return_comes_from_spot_not_perps() -> None:
    port = dict(fixture("hl_portfolio_unified.json"))
    total, perp = points(port["allTime"]), points(port["perpAllTime"])
    assert perp[-1][2] < 0 < total[-1][2]  # PnL total +9,2 M; de perpetuos -0,7 M
    whole = flow_adjusted_returns(total, D(1000))
    perps = flow_adjusted_returns(total, D(1000), perp)
    assert perps.total_return_pct < whole.total_return_pct
    assert perps.skipped == whole.skipped  # misma base de capital y mismos flujos


def test_collateral_from_real_unified_spot_state() -> None:
    text = collateral_summary(fixture("hl_spot_state_unified.json"), D(0))
    assert text.startswith("USDC 100 %")
    assert collateral_summary({"balances": []}, D(5000)) == "USDC (cuenta de perpetuos) 100 %"
    assert collateral_summary({"balances": []}, D(0)) == "sin saldo"


def test_leverage_history_reconstructs_positions() -> None:
    capital = [(NOW_MS - 10 * DAY_MS, D(10000), D(0))]
    fills = [fill("BTC", "B", "1", "50000", "0", 48),  # 5x
             fill("ETH", "B", "10", "3000", "0", 24),  # 5x + 3x = 8x, 2 posiciones
             fill("BTC", "A", "1", "50000", "1", 12),  # cierra BTC: 3x
             fill("@107", "B", "100", "10", "0", 6)]  # spot: no cuenta
    samples, max_open = leverage_history(fills, capital)
    assert samples == [D(5), D(8), D(3)] and max_open == 2
    assert percentile(samples, D("0.9")) == D(8) and percentile([], D("0.9")) is None


# --- evaluación ---


async def test_good_standard_candidate() -> None:
    c = await ev(FakeInfo())
    assert c.discarded == "", c.discarded
    assert c.bot_compatible and c.capital_source == "perpetuos" and c.capital_usd == 50000
    assert c.history_days is not None and c.history_days >= 190  # pasos de 7 días
    assert c.sharpe_30d and c.sharpe_total and c.consistency == min(c.sharpe_30d,
                                                                     c.sharpe_total)
    assert c.leverage_now == D("0.8") and c.positions_now == 1


@pytest.mark.parametrize(
    ("info", "reason"),
    [
        (FakeInfo(mode="unifiedAccount"), "modo de cuenta"),
        (FakeInfo(capital=68), "capital en perpetuos 68 USD"),
        (FakeInfo(age_days=60), "historial de"),
        (FakeInfo(n_fills=2000), "scalper: o más"),
        (FakeInfo(n_fills=1500), "scalper"),
        (FakeInfo(n_fills=2), "inactivo"),
        (FakeInfo(coins=("BTC", "NOEXISTE")), "sin mercado en Kraken"),
        (FakeInfo(coins=("@107",)), "volumen en perpetuos 0.0 %"),
        (FakeInfo(coins=("BTC", "@107", "PURR/USDC")), "volumen en perpetuos 33.3 %"),
        (FakeInfo(positions=[(f"C{i}", "1", "100") for i in range(9)]), "posiciones simultáneas"),
        (FakeInfo(positions=[("BTC", "10", "600000")]), "apalancamiento efectivo 12.0x"),
        (FakeInfo(month_pnl=[0], total_pnl=[0]), "Sharpe no calculable"),
    ],
)
async def test_discard_rules(info: FakeInfo, reason: str) -> None:
    assert reason in (await ev(info)).discarded


async def test_historical_leverage_filter_uses_p90_of_30_days() -> None:
    fills = [fill("BTC", "B", "12", "50000", "0", 24 * (20 - i)) for i in range(10)]
    fills = [dict(f, startPosition="0") for f in fills]  # 12 BTC: unas 12x del capital
    c = await ev(FakeInfo(fills=fills))
    assert "apalancamiento efectivo 11." in c.discarded and "p90" in c.discarded
    c2 = await ev(FakeInfo(fills=fills), Criteria(max_leverage=D(15)))
    assert c2.discarded == "" and c2.leverage_p90_30d is not None
    assert D(11) < c2.leverage_p90_30d <= D(12)


async def test_capital_floor_discards_unmeasurable_series() -> None:
    # Retiros que dejan la cuenta casi vacía buena parte del mes
    flows = {i: -9000.0 for i in range(1, 8)}
    c = await ev(FakeInfo(capital=60000, month_flows=flows))
    assert "no medible" in c.discarded


async def test_unified_account_is_only_informative() -> None:
    c = await ev(FakeInfo(mode="unifiedAccount"), Criteria(ignore_account_mode=True))
    assert c.discarded == "" and c.bot_compatible is False
    assert c.capital_source == "cuenta_total" and c.capital_usd is not None
    assert c.capital_usd > 50000  # capital total de la cuenta (perpetuos = 0 en unified)
    assert c.collateral == "USDC 100 %" and c.pnl_source == "perpetuos_sobre_cuenta_total"
    assert "NO COMPATIBLE CON EL BOT" in table([c])


async def test_unified_performance_ignores_spot_gains() -> None:
    crit = Criteria(ignore_account_mode=True)
    whole = await ev(FakeInfo(mode="unifiedAccount"), crit)
    perps = await ev(FakeInfo(mode="unifiedAccount", perp_pnl=[100, -300, 50, -120]), crit)
    assert whole.discarded == perps.discarded == ""
    assert whole.return_30d_pct and whole.return_30d_pct > 0  # PnL de perpetuos = total
    assert perps.return_30d_pct is not None and perps.return_30d_pct < 0  # el spot ya no suma
    assert perps.capital_usd == whole.capital_usd  # misma base: la cuenta total


async def test_perp_volume_share_threshold() -> None:
    mixed = FakeInfo(coins=("BTC", "@107"))
    c = await ev(mixed)
    assert c.discarded == "" and c.perp_volume_pct == D(50)
    assert "volumen en perpetuos" in (await ev(mixed, Criteria(min_perp_volume_pct=D(60)))
                                      ).discarded


async def test_consistency_ranks_steady_above_lucky_month() -> None:
    steady = await ev(FakeInfo(month_pnl=[300, 250, 280, 310, -50],
                               total_pnl=[1500, 1200, -300, 1400]))
    lucky = await ev(FakeInfo(month_pnl=[5000, -200, 4000, 100, -300],
                              total_pnl=[3000, -6000, 2500, -4000, 1000]))
    assert steady.discarded == lucky.discarded == ""
    assert lucky.return_30d_pct and steady.return_30d_pct
    assert lucky.return_30d_pct > steady.return_30d_pct  # el mes de suerte gana en ROI...
    order = [c.address for c in ranked([lucky, steady])]
    assert order[0] is steady.address  # ...pero la consistencia ordena primero la estable
    assert steady.consistency and lucky.consistency and steady.consistency > lucky.consistency


# --- leaderboard, ranking y salida ---


def lb_row(addr: str, roi_m: str, pnl_m: str, roi_t: str, pnl_t: str,
           av: str = "50000") -> dict[str, Any]:
    return {"ethAddress": addr, "accountValue": av, "windowPerformances": [
        ["month", {"pnl": pnl_m, "roi": roi_m, "vlm": "1"}],
        ["allTime", {"pnl": pnl_t, "roi": roi_t, "vlm": "1"}]]}


def test_leaderboard_preselection_by_consistency() -> None:
    payload = {"leaderboardRows": [
        lb_row("0xA", "0.90", "100", "0.05", "10"),  # mejor mes, peor total
        lb_row("0xB", "0.40", "100", "2.00", "100"),  # bien en las dos
        lb_row("0xC", "0.30", "100", "1.50", "100"),  # bien en las dos
        lb_row("0xD", "0.80", "100", "-0.2", "-10"),  # pierde en el total
        lb_row("0xE", "1.00", "-1", "3.00", "100"),  # pierde en el mes
        lb_row("0xF", "0.10", "100", "1.00", "100", av="0.0"),  # capital del leaderboard 0
        {"roto": True},
    ]}
    assert parse_leaderboard(payload, top=10) == ["0xb", "0xc", "0xa", "0xf"]
    assert parse_leaderboard(payload, top=2) == ["0xb", "0xc"]
    # El capital del leaderboard ya no filtra por defecto (se mide en perpetuos)
    assert "0xf" in parse_leaderboard(payload, top=10)
    assert "0xf" not in parse_leaderboard(payload, top=10, min_account_value=D(1))
    with pytest.raises(LeaderDataError):
        parse_leaderboard({"otra": 1}, top=5)


def test_real_leaderboard_sample() -> None:
    """Muestra real del leaderboard (2026-10-08): importes como cadenas y
    "windowPerformances" como lista de pares day/week/month/allTime."""
    sample = fixture("hl_leaderboard.json")
    rows = sample["leaderboardRows"]
    assert [w for w, _ in rows[0]["windowPerformances"]] == ["day", "week", "month", "allTime"]
    kept = parse_leaderboard(sample, top=10)
    for r in rows:
        perf = dict(r["windowPerformances"])
        positive = D(perf["month"]["pnl"]) > 0 and D(perf["allTime"]["pnl"]) > 0
        assert (r["ethAddress"].lower() in kept) == positive


def test_ranking_csv_and_wallet_file(tmp_path: Path) -> None:
    cands = [Candidate("0x1", "manual", consistency=D(1)),
             Candidate("0x2", "manual", discarded="scalper"),
             Candidate("0x3", "leaderboard_no_oficial", consistency=D(2))]
    assert [c.address for c in ranked(cands)] == ["0x3", "0x1", "0x2"]
    out = tmp_path / "r.csv"
    write_csv(out, ranked(cands))
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["source"] == "leaderboard_no_oficial" and rows[2]["discarded"] == "scalper"
    assert "bot_compatible" in rows[0] and "collateral" in rows[0]
    w = tmp_path / "w.txt"
    w.write_text("# lista\n0xAB  # comentario\n\n0xcd\n")
    assert read_wallets(w) == ["0xab", "0xcd"]


def test_cli_requires_a_source(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main([])
    assert "--wallets o --leaderboard" in capsys.readouterr().err


def test_defaults_match_the_agreed_criteria() -> None:
    c = Criteria()
    assert (c.min_history_days, c.max_leverage, c.min_perp_capital, c.max_positions,
            c.min_perp_volume_pct) == (90, D(10), D(10000), 8, D(50))
    _ = timedelta  # noqa: F841
