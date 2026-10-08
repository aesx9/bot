from __future__ import annotations

from decimal import Decimal as D

import pytest

from copybot.config import PlannerConfig
from copybot.models import Action, ActionKind, Side
from copybot.planner import PlannerError, apply_actions, plan

P = {"A": D(100), "B": D(10), "C": D(1)}
NOFILTER = PlannerConfig(min_order_usd=D(0), rebalance_threshold_pct=D(0))
CFG = PlannerConfig(min_order_usd=D(10), rebalance_threshold_pct=D(5))


def run(t: dict[str, D], c: dict[str, D], cfg: PlannerConfig = CFG,
        managed: set[str] | None = None) -> list[Action]:
    m = managed if managed is not None else set(t) | set(c)
    return plan(targets=t, current=c, managed=m, prices=P, cfg=cfg)


def only(actions: list[Action]) -> Action:
    assert len(actions) == 1
    return actions[0]


def test_open() -> None:
    a = only(run({"A": D(1)}, {}))
    assert (a.kind, a.side, a.size, a.reduce_only) == (ActionKind.OPEN, Side.BUY, D(1), False)


def test_open_short() -> None:
    a = only(run({"A": D(-1)}, {}))
    assert (a.kind, a.side) == (ActionKind.OPEN, Side.SELL)


def test_increase() -> None:
    a = only(run({"A": D(2)}, {"A": D(1)}))
    assert (a.kind, a.side, a.size, a.reduce_only) == (ActionKind.INCREASE, Side.BUY, D(1), False)


def test_reduce_is_reduce_only() -> None:
    a = only(run({"A": D(-1)}, {"A": D(-2)}))
    assert (a.kind, a.side, a.size, a.reduce_only) == (ActionKind.REDUCE, Side.BUY, D(1), True)


def test_close_is_reduce_only() -> None:
    a = only(run({}, {"A": D("0.5")}))
    assert (a.kind, a.side, a.size, a.reduce_only) == (ActionKind.CLOSE, Side.SELL, D("0.5"), True)


def test_flip_is_close_plus_separate_open() -> None:
    acts = run({"A": D(-1)}, {"A": D(2)})
    assert [a.kind for a in acts] == [ActionKind.FLIP_CLOSE, ActionKind.FLIP_OPEN]
    close, open_ = acts
    assert (close.side, close.size, close.reduce_only) == (Side.SELL, D(2), True)
    assert (open_.side, open_.size, open_.reduce_only) == (Side.SELL, D(1), False)


def test_min_order_filters_open_and_flip_open_but_never_closes() -> None:
    assert run({"C": D(5)}, {}) == []  # 5 USD < 10
    # cierre total de 1 USD: NO se filtra
    assert only(run({}, {"C": D(1)})).kind is ActionKind.CLOSE
    # flip con apertura pequeña: cierre sí, apertura no
    acts = run({"C": D(-5)}, {"C": D(1)})
    assert [a.kind for a in acts] == [ActionKind.FLIP_CLOSE]


def test_closes_never_filtered_even_with_huge_thresholds() -> None:
    cfg = PlannerConfig(min_order_usd=D(10**9), rebalance_threshold_pct=D(100))
    assert only(run({}, {"C": D("0.0001")}, cfg)).kind is ActionKind.CLOSE


def test_rebalance_threshold() -> None:
    # 1 -> 1.04 (4% < 5%), nocional 4 USD también < 10
    assert run({"A": D("1.04")}, {"A": D(1)}) == []
    # 10 -> 10.4 en A: 4% < 5% aunque el nocional (40) supere el mínimo
    assert run({"A": D("10.4")}, {"A": D(10)}) == []
    assert only(run({"A": D("10.6")}, {"A": D(10)})).kind is ActionKind.INCREASE


def test_unmanaged_positions_are_never_touched() -> None:
    acts = run({"A": D(1)}, {"A": D(1), "B": D(5)}, managed={"A"})
    assert acts == []


def test_target_outside_managed_is_an_error() -> None:
    with pytest.raises(PlannerError):
        run({"B": D(1)}, {}, managed={"A"})


def test_missing_price_is_an_error() -> None:
    with pytest.raises(PlannerError):
        plan(targets={"Z": D(1)}, current={}, managed={"Z"}, prices=P, cfg=CFG)


def test_reductions_come_before_additions() -> None:
    acts = run({"A": D(3), "B": D(-10)}, {"B": D(5)}, NOFILTER)
    kinds = [a.kind for a in acts]
    assert kinds == [ActionKind.FLIP_CLOSE, ActionKind.OPEN, ActionKind.FLIP_OPEN]


def test_action_rejects_non_reduce_only_reduction() -> None:
    with pytest.raises(ValueError, match="reduceOnly"):
        Action(ActionKind.CLOSE, "A", Side.SELL, D(1), reduce_only=False, ref_price=D(1))


def test_apply_actions_reaches_target() -> None:
    t, c = {"A": D(-1), "B": D(3)}, {"A": D(2), "C": D(1)}
    assert apply_actions(c, run(t, c, NOFILTER)) == t
