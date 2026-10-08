from __future__ import annotations

from decimal import Decimal, localcontext

from hypothesis import given
from hypothesis import strategies as st

from copybot import limits
from copybot.config import SizingConfig
from copybot.sizing import compute_targets, per_asset_cap_usd, total_cap_usd
from tests.helpers import mapper

COINS = ("BTC", "ETH", "SOL", "DOGE")
SYMS = {"BTC": "PF_XBTUSD", "ETH": "PF_ETHUSD", "SOL": "PF_SOLUSD", "DOGE": "PF_DOGEUSD"}

dec = lambda lo, hi, pl=4: st.decimals(min_value=Decimal(lo), max_value=Decimal(hi),  # noqa: E731
                                       places=pl, allow_nan=False, allow_infinity=False)
leader_pos = st.dictionaries(st.sampled_from(COINS), dec(-10**6, 10**6).filter(lambda d: d != 0))
price_map = st.fixed_dictionaries({s: dec("0.0001", 200000) for s in SYMS.values()})
cfgs = st.builds(
    SizingConfig,
    mode=st.sampled_from(["equity", "fixed"]),
    multiplier=dec("0.01", 10, 2),
    fixed_ratio=dec("0.0001", 1),
    max_asset_usd=dec(1, 600, 2),
    max_asset_pct_equity=dec("0.1", 100, 1),
    max_total_leverage=dec("0.1", 3, 2),
)


@given(leader_pos, price_map, dec("0.01", 10**9, 2), dec("0.01", 10**6, 2), cfgs)
def test_caps_and_signs_always_hold(lp, prices, leq, meq, cfg):  # type: ignore[no-untyped-def]
    r = compute_targets(leader_equity=leq, leader_positions=lp, my_equity=meq,
                        prices=prices, mapper=mapper(), cfg=cfg)
    for coin, size in lp.items():
        sym = SYMS[coin]
        if sym in r.targets:
            assert (r.targets[sym] > 0) == (size > 0)
    assert Decimal(0) < r.scale_applied <= 1
    with localcontext(prec=100):  # comprobación con aritmética exacta
        asset_cap = per_asset_cap_usd(cfg, meq)
        total = Decimal(0)
        for sym, units in r.targets.items():
            n = abs(units) * prices[sym]
            assert n <= asset_cap <= limits.HARD_MAX_NOTIONAL_PER_ASSET_USD
            total += n
        assert total <= total_cap_usd(cfg, meq)
        assert total <= limits.HARD_MAX_NOTIONAL_TOTAL_USD
        assert total <= limits.HARD_MAX_LEVERAGE * meq


@given(price_map, dec(1, 10**9, 2), dec(1, 10**6, 2), cfgs)
def test_flat_leader_means_flat_targets(prices, leq, meq, cfg):  # type: ignore[no-untyped-def]
    r = compute_targets(leader_equity=leq, leader_positions={}, my_equity=meq,
                        prices=prices, mapper=mapper(), cfg=cfg)
    assert r.targets == {}
