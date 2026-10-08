from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from copybot.analysis.leaders import (
    Candidate,
    Criteria,
    daily_returns,
    daily_series,
    evaluate,
    max_drawdown_pct,
    parse_leaderboard,
    ranked,
    read_wallets,
    sharpe,
    write_csv,
)
from copybot.config import SymbolsConfig
from copybot.models import LeaderSnapshot
from copybot.sources.hyperliquid_rest import STANDARD_MODES

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
MARKETS = {"PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD"}
ADDR = "0x" + "ab" * 20


def history(days: int, pnl_per_day: list[float] | None = None,
            av: float = 10000, points_per_day: int = 2) -> dict[str, Any]:
    avh, pnlh, pnl = [], [], 0.0
    start = NOW - timedelta(days=days)
    for d in range(days + 1):
        if pnl_per_day:
            pnl += pnl_per_day[d % len(pnl_per_day)]
        for k in range(points_per_day):
            t = int((start + timedelta(days=d, hours=6 * k)).timestamp() * 1000)
            avh.append([t, str(av + pnl)])
            pnlh.append([t, str(pnl)])
    return {"accountValueHistory": avh, "pnlHistory": pnlh, "vlm": "0"}


class FakeInfo:
    def __init__(self, mode: str = "default", age_days: int = 200,
                 pnl: list[float] | None = None, fills: int = 60, coins: tuple[str, ...] = ("BTC",),
                 positions: int = 3) -> None:
        self.mode, self.age, self.fills_n = mode, age_days, fills
        self.coins, self.n_pos = coins, positions
        self.pnl = pnl if pnl is not None else [50, -20, 30, 10, -5]

    async def user_abstraction(self, user: str) -> str:
        return self.mode

    async def portfolio(self, user: str) -> dict[str, Any]:
        return {"perpAllTime": history(self.age, [0], points_per_day=1),
                "perpMonth": history(30, self.pnl)}

    async def user_fills_by_time(self, user: str, start_ms: int) -> list[dict[str, Any]]:
        return [{"coin": self.coins[i % len(self.coins)], "px": "100", "sz": "1"}
                for i in range(self.fills_n)]

    async def leader_snapshot(self, user: str) -> LeaderSnapshot:
        return LeaderSnapshot(D(10000), {f"C{i}": D(1) for i in range(self.n_pos)}, {}, NOW)


async def ev(info: FakeInfo, crit: Criteria | None = None) -> Candidate:
    return await evaluate(info, ADDR, "manual", MARKETS, SymbolsConfig(),  # type: ignore[arg-type]
                          crit or Criteria(), NOW)


def test_series_returns_and_metrics() -> None:
    s = daily_series(history(3, [100, -50]))
    assert len(s) == 4  # un punto por día (el último del día)
    r = daily_returns(s)
    assert r[0] == D(-50) / D(10100)
    assert sharpe([D("0.01"), D("0.03")]) == pytest.approx(
        D("0.02") / D("0.0002").sqrt() * D(365).sqrt())  # type: ignore[arg-type]
    assert sharpe([D("0.01"), D("0.01")]) is None and sharpe([D(1)]) is None
    assert max_drawdown_pct([D("0.1"), D("-0.5"), D("0.2")]) == D(50)


async def test_good_candidate_is_ranked() -> None:
    c = await ev(FakeInfo())
    assert c.discarded == "" and c.sharpe_30d is not None and c.sharpe_30d > 0
    assert c.positions_now == 3 and c.fills_per_day == D(2) and c.unmapped_volume_pct == 0


@pytest.mark.parametrize(
    ("info", "reason"),
    [
        (FakeInfo(mode="unifiedAccount"), "modo de cuenta"),
        (FakeInfo(mode="portfolioMargin"), "modo de cuenta"),
        (FakeInfo(age_days=20), "historial"),
        (FakeInfo(fills=2000), "scalper: o más"),
        (FakeInfo(fills=1500), "scalper"),  # 50 fills/día > 40
        (FakeInfo(fills=2), "inactivo"),
        (FakeInfo(coins=("BTC", "NOEXISTE")), "sin mercado en Kraken"),
        (FakeInfo(positions=9), "posiciones simultáneas"),
        (FakeInfo(pnl=[0]), "Sharpe no calculable"),
    ],
)
async def test_discard_rules(info: FakeInfo, reason: str) -> None:
    assert reason in (await ev(info)).discarded


async def test_spot_fills_do_not_count_as_unmapped_and_x8_is_allowed() -> None:
    c = await ev(FakeInfo(coins=("BTC", "@107", "PURR/USDC"), positions=8))
    assert c.discarded == "" and c.unmapped_volume_pct == 0
    assert "default" in STANDARD_MODES


def test_leaderboard_parsing_is_defensive() -> None:
    payload = {"leaderboardRows": [
        {"ethAddress": "0xA", "accountValue": "50000",
         "windowPerformances": [["month", {"pnl": "100", "roi": "0.10", "vlm": "1"}]]},
        {"ethAddress": "0xB", "accountValue": "50000",
         "windowPerformances": [["month", {"pnl": "100", "roi": "0.30", "vlm": "1"}]]},
        {"ethAddress": "0xC", "accountValue": "500",  # demasiado pequeño
         "windowPerformances": [["month", {"pnl": "100", "roi": "0.90", "vlm": "1"}]]},
        {"ethAddress": "0xD", "accountValue": "50000",  # pierde
         "windowPerformances": [["month", {"pnl": "-1", "roi": "-0.1", "vlm": "1"}]]},
        {"roto": True},
    ]}
    assert parse_leaderboard(payload, min_account_value=D(10000), top=5) == ["0xb", "0xa"]
    assert parse_leaderboard(payload, min_account_value=D(10000), top=1) == ["0xb"]
    from copybot.sources.hyperliquid_rest import LeaderDataError
    with pytest.raises(LeaderDataError):
        parse_leaderboard({"otra": 1}, min_account_value=D(0), top=5)


def test_ranking_csv_and_wallet_file(tmp_path: Path) -> None:
    cands = [Candidate("0x1", "manual", sharpe_30d=D(1)),
             Candidate("0x2", "manual", discarded="scalper"),
             Candidate("0x3", "leaderboard_no_oficial", sharpe_30d=D(2))]
    order = [c.address for c in ranked(cands)]
    assert order == ["0x3", "0x1", "0x2"]
    out = tmp_path / "r.csv"
    write_csv(out, ranked(cands))
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["source"] == "leaderboard_no_oficial" and rows[2]["discarded"] == "scalper"
    w = tmp_path / "w.txt"
    w.write_text("# lista\n0xAB  # comentario\n\n0xcd\n")
    assert read_wallets(w) == ["0xab", "0xcd"]
