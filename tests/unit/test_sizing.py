from __future__ import annotations

from decimal import Decimal as D

import pytest

from copybot import limits
from copybot.config import SizingConfig
from copybot.sizing import (
    SizingError,
    assert_within_hard_limits,
    compute_targets,
    per_asset_cap_usd,
    total_cap_usd,
)
from tests.helpers import mapper

PRICES = {
    "PF_XBTUSD": D(60000), "PF_ETHUSD": D(3000), "PF_SOLUSD": D(150), "PF_PEPEUSD": D("0.00001"),
}


def run(  # type: ignore[no-untyped-def]
    positions: dict[str, D], *, markets: tuple[str, ...] | None = None,
    leader_eq: D = D(100000), my_eq: D = D(540), cfg: SizingConfig | None = None,
    **mcfg: object,
):
    return compute_targets(
        leader_equity=leader_eq, leader_positions=positions, my_equity=my_eq,
        prices=PRICES, mapper=mapper(markets, **mcfg) if markets else mapper(**mcfg),
        cfg=cfg or SizingConfig(),
    )


def test_equity_proportional_without_caps() -> None:
    # líder 100k con 1 ETH (3k, 3%); yo 540 -> ratio 0.0054 -> 0.0054 ETH = 16.2 USD
    r = run({"ETH": D(1)})
    assert r.targets == {"PF_ETHUSD": D("0.0054")}
    assert r.scale_applied == 1 and not r.capped_assets


def test_sign_is_preserved() -> None:
    assert run({"ETH": D(-1)}).targets["PF_ETHUSD"] == D("-0.0054")


def test_multiplier() -> None:
    r = run({"ETH": D(1)}, cfg=SizingConfig(multiplier=D(2)))
    assert r.targets["PF_ETHUSD"] == D("0.0108")


def test_per_asset_cap_pct_of_equity() -> None:
    # 25% de 540 = 135 USD < 600
    assert per_asset_cap_usd(SizingConfig(), D(540)) == D(135)
    r = run({"BTC": D(10)})  # 600k líder -> 3240 USD sin tope
    assert abs(r.targets["PF_XBTUSD"]) * PRICES["PF_XBTUSD"] <= D(135)
    assert "PF_XBTUSD" in r.capped_assets


def test_per_asset_cap_absolute_with_large_equity() -> None:
    cap = per_asset_cap_usd(SizingConfig(max_asset_pct_equity=D(100)), D(100000))
    assert cap == limits.HARD_MAX_NOTIONAL_PER_ASSET_USD


def test_total_cap() -> None:
    assert total_cap_usd(SizingConfig(), D(540)) == D(1080)  # 2x
    assert total_cap_usd(SizingConfig(max_total_leverage=D(3)), D(540)) == D(1500)  # tope abs.
    assert total_cap_usd(SizingConfig(max_total_leverage=D(3)), D(100)) == D(300)


def test_total_leverage_scales_proportionally() -> None:
    cfg = SizingConfig(max_asset_pct_equity=D(100), max_total_leverage=D(1))
    # 3 activos, cada uno topado a 540 USD -> 1620 total > 540 -> escala 1/3
    r = run({"BTC": D(100), "ETH": D(1000), "SOL": D(-20000)}, cfg=cfg)
    notionals = {s: abs(u) * PRICES[s] for s, u in r.targets.items()}
    assert sum(notionals.values()) <= D(540)
    assert r.scale_applied < 1
    assert r.targets["PF_SOLUSD"] < 0
    # misma proporción entre activos
    vals = list(notionals.values())
    assert max(vals) - min(vals) < D("0.0001")


def test_fixed_mode_ignores_equities() -> None:
    cfg = SizingConfig(mode="fixed", fixed_ratio=D("0.001"))  # type: ignore[arg-type]
    r = run({"ETH": D(10)}, leader_eq=D(1), cfg=cfg)
    assert r.targets["PF_ETHUSD"] == D("0.010")


def test_size_factor_applied() -> None:
    r = run({"kPEPE": D(1000)}, overrides={"kPEPE": "PF_PEPEUSD"}, size_factor={"kPEPE": 1000})
    # 1000 kPEPE = 1e6 PEPE x 0.0054 = 5400 PEPE
    assert r.targets["PF_PEPEUSD"] == D("5400.0000")


def test_asset_without_market_is_ignored() -> None:
    assert run({"HYPE": D(5), "ETH": D(1)}).targets.keys() == {"PF_ETHUSD"}


@pytest.mark.parametrize(("leq", "meq"), [(D(0), D(540)), (D(-1), D(540)), (D(1000), D(0))])
def test_non_positive_equity_refuses(leq: D, meq: D) -> None:
    with pytest.raises(SizingError):
        run({"ETH": D(1)}, leader_eq=leq, my_eq=meq)


def test_missing_price_refuses_instead_of_closing() -> None:
    with pytest.raises(SizingError, match="precio"):
        compute_targets(
            leader_equity=D(1000), leader_positions={"ETH": D(1)}, my_equity=D(500),
            prices={}, mapper=mapper(), cfg=SizingConfig(),
        )


def test_two_coins_same_symbol_refuses() -> None:
    with pytest.raises(SizingError, match="mismo símbolo"):
        run({"BTC": D(1), "XBT": D(1)}, markets=("PF_XBTUSD",))


def test_hard_limit_assertion() -> None:
    with pytest.raises(limits.HardLimitViolation):
        assert_within_hard_limits({"PF_ETHUSD": D("0.2001")}, PRICES, D(10000))  # 600.3 USD
    with pytest.raises(limits.HardLimitViolation):
        assert_within_hard_limits(
            {"PF_ETHUSD": D("0.19"), "PF_XBTUSD": D("0.009"), "PF_SOLUSD": D(3.9)},
            PRICES, D(10000),
        )  # 570 + 540 + 585 = 1695 > 1500
    with pytest.raises(limits.HardLimitViolation):
        assert_within_hard_limits({"PF_ETHUSD": D("0.1")}, PRICES, D(99))  # 300 > 3x99
