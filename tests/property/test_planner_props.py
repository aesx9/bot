from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from copybot.config import PlannerConfig
from copybot.models import REDUCING_KINDS, ActionKind, Side
from copybot.planner import apply_actions, plan
from tests.property.strategies import SYMBOLS, positions, price_maps

NOFILTER = PlannerConfig(min_order_usd=Decimal(0), rebalance_threshold_pct=Decimal(0))
thresholds = st.builds(
    PlannerConfig,
    min_order_usd=st.decimals(min_value=0, max_value=10**6, places=2),
    rebalance_threshold_pct=st.decimals(min_value=0, max_value=100, places=2),
)


@given(positions, positions, price_maps)
def test_without_filters_reaches_target(t, c, p):  # type: ignore[no-untyped-def]
    acts = plan(targets=t, current=c, managed=set(SYMBOLS), prices=p, cfg=NOFILTER)
    assert apply_actions(c, acts) == t


@given(positions, positions, price_maps, thresholds)
def test_every_reduction_is_reduce_only_and_never_overshoots(t, c, p, cfg):  # type: ignore[no-untyped-def]
    acts = plan(targets=t, current=c, managed=set(SYMBOLS), prices=p, cfg=cfg)
    state = dict(c)  # se aplican en orden, como hará el ejecutor
    for a in acts:
        cur = state.get(a.symbol, Decimal(0))
        delta = a.size if a.side is Side.BUY else -a.size
        after = cur + delta
        if abs(after) < abs(cur) or a.kind in REDUCING_KINDS:
            assert a.reduce_only
            assert abs(after) <= abs(cur)
            assert after == 0 or (after > 0) == (cur > 0)  # nunca cruza por cero
        else:
            assert not a.reduce_only
            assert cur == 0 or (after > 0) == (cur > 0)  # una apertura nunca invierte
        state[a.symbol] = after


@given(positions, positions, price_maps, thresholds)
def test_full_closes_are_never_filtered(t, c, p, cfg):  # type: ignore[no-untyped-def]
    acts = plan(targets=t, current=c, managed=set(SYMBOLS), prices=p, cfg=cfg)
    for s, size in c.items():
        tgt = t.get(s, Decimal(0))
        if tgt == 0 or (tgt > 0) != (size > 0):
            closes = [a for a in acts if a.symbol == s and a.kind in
                      (ActionKind.CLOSE, ActionKind.FLIP_CLOSE)]
            assert len(closes) == 1 and closes[0].size == abs(size)


@given(positions, positions, price_maps, thresholds,
       st.sets(st.sampled_from(SYMBOLS)))
def test_only_managed_symbols_are_touched(t, c, p, cfg, managed):  # type: ignore[no-untyped-def]
    t = {s: v for s, v in t.items() if s in managed}
    acts = plan(targets=t, current=c, managed=managed, prices=p, cfg=cfg)
    assert {a.symbol for a in acts} <= managed


@given(positions, positions, price_maps, thresholds)
def test_reductions_precede_additions(t, c, p, cfg):  # type: ignore[no-untyped-def]
    acts = plan(targets=t, current=c, managed=set(SYMBOLS), prices=p, cfg=cfg)
    flags = [a.reduce_only for a in acts]
    assert flags == sorted(flags, reverse=True)
