from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from copybot.config import PlannerConfig
from copybot.models import REDUCING_KINDS, ActionKind, Side
from copybot.planner import apply_actions, plan
from tests.helpers import specs
from tests.property.strategies import STEPS, SYMBOLS, positions, price_maps

NOFILTER = PlannerConfig(min_order_usd=Decimal(0), rebalance_threshold_pct=Decimal(0))
FINE = specs(SYMBOLS)  # paso 0.0001: los tamaños generados (4 decimales) son exactos
market_maps = st.fixed_dictionaries(
    {s: st.sampled_from(STEPS).map(lambda step, s=s: specs([s], step)[s]) for s in SYMBOLS}
)
thresholds = st.builds(
    PlannerConfig,
    min_order_usd=st.decimals(min_value=0, max_value=10**6, places=2),
    rebalance_threshold_pct=st.decimals(min_value=0, max_value=100, places=2),
)


@given(positions, positions, price_maps)
def test_without_filters_reaches_target(t, c, p):  # type: ignore[no-untyped-def]
    acts = plan(targets=t, current=c, managed=set(SYMBOLS), prices=p, markets=FINE, cfg=NOFILTER)
    assert apply_actions(c, acts) == t


@given(positions, positions, price_maps, thresholds, market_maps)
def test_every_reduction_is_reduce_only_and_never_overshoots(t, c, p, cfg, m):  # type: ignore[no-untyped-def]
    acts = plan(targets=t, current=c, managed=set(SYMBOLS), prices=p, markets=m, cfg=cfg)
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


@given(positions, positions, price_maps, thresholds, market_maps)
def test_full_closes_are_never_filtered(t, c, p, cfg, m):  # type: ignore[no-untyped-def]
    acts = plan(targets=t, current=c, managed=set(SYMBOLS), prices=p, markets=m, cfg=cfg)
    for s, size in c.items():
        tgt = t.get(s, Decimal(0))
        if tgt == 0 or (tgt > 0) != (size > 0):
            closes = [a for a in acts if a.symbol == s and a.kind in
                      (ActionKind.CLOSE, ActionKind.FLIP_CLOSE)]
            assert len(closes) == 1 and closes[0].size == abs(size)


@given(positions, positions, price_maps, thresholds,
       st.sets(st.sampled_from(SYMBOLS)), market_maps)
def test_only_managed_symbols_are_touched(t, c, p, cfg, managed, m):  # type: ignore[no-untyped-def]
    t = {s: v for s, v in t.items() if s in managed}
    acts = plan(targets=t, current=c, managed=managed, prices=p, markets=m, cfg=cfg)
    assert {a.symbol for a in acts} <= managed


@given(positions, positions, price_maps, thresholds, market_maps)
def test_reductions_precede_additions(t, c, p, cfg, m):  # type: ignore[no-untyped-def]
    acts = plan(targets=t, current=c, managed=set(SYMBOLS), prices=p, markets=m, cfg=cfg)
    flags = [a.reduce_only for a in acts]
    assert flags == sorted(flags, reverse=True)


@given(positions, positions, price_maps, thresholds, market_maps)
def test_trades_respect_market_step_except_exact_liquidations(t, c, p, cfg, m):  # type: ignore[no-untyped-def]
    acts = plan(targets=t, current=c, managed=set(SYMBOLS), prices=p, markets=m, cfg=cfg)
    state = dict(c)
    for a in acts:
        cur = state.get(a.symbol, Decimal(0))
        step = m[a.symbol].size_step
        on_step = a.size % step == 0 and a.size >= step
        if a.reduce_only:
            assert on_step or a.size == abs(cur)  # o liquida exactamente lo que hay
        else:
            assert on_step
        state[a.symbol] = cur + (a.size if a.side is Side.BUY else -a.size)


@given(positions, positions, price_maps, market_maps)
def test_rounding_never_leaves_position_beyond_target(t, c, p, m):  # type: ignore[no-untyped-def]
    acts = plan(targets=t, current=c, managed=set(SYMBOLS), prices=p, markets=m, cfg=NOFILTER)
    final = apply_actions(c, acts)
    for s in SYMBOLS:
        tgt, got, step = t.get(s, Decimal(0)), final.get(s, Decimal(0)), m[s].size_step
        if tgt == 0:
            assert got == 0
            continue
        assert got == 0 or (got > 0) == (tgt > 0)
        assert abs(got) <= abs(tgt)  # nunca por encima del objetivo (que respeta los topes)
        assert abs(tgt) - abs(got) < step  # y a menos de un paso de él
