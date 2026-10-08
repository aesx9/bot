from __future__ import annotations

from decimal import Decimal as D

import pytest

from copybot.config import PlannerConfig
from copybot.models import Action, ActionKind, Side
from copybot.planner import PlannerError, apply_actions, plan
from tests.helpers import specs

P = {"A": D(100), "B": D(10), "C": D(1)}
M = specs(P)
NOFILTER = PlannerConfig(min_order_usd=D(0), rebalance_threshold_pct=D(0))
CFG = PlannerConfig(min_order_usd=D(10), rebalance_threshold_pct=D(5))


def run(t: dict[str, D], c: dict[str, D], cfg: PlannerConfig = CFG,
        managed: set[str] | None = None, step: str | None = None) -> list[Action]:
    m = managed if managed is not None else set(t) | set(c)
    markets = specs(P, step) if step else M
    return plan(targets=t, current=c, managed=m, prices=P, markets=markets, cfg=cfg)


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
        plan(targets={"Z": D(1)}, current={}, managed={"Z"}, prices=P, markets=M, cfg=CFG)


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


# --- Tamaño mínimo y precisión de cada mercado (instruments de Kraken) ---


def test_missing_market_spec_is_an_error() -> None:
    with pytest.raises(PlannerError, match="especificación"):
        plan(targets={"A": D(1)}, current={}, managed={"A"}, prices=P, markets={}, cfg=CFG)


def test_open_is_rounded_down_to_market_step() -> None:
    a = only(run({"A": D("1.27")}, {}, step="0.1"))
    assert (a.kind, a.size) == (ActionKind.OPEN, D("1.2"))
    a = only(run({"A": D("-1.27")}, {}, step="0.1"))
    assert (a.side, a.size) == (Side.SELL, D("1.2"))


def test_open_below_market_minimum_is_skipped_even_without_usd_filter() -> None:
    # Mercado tipo PF_PEPEUSD: contractValueTradePrecision = -3 -> múltiplos de 1000
    assert run({"C": D(999)}, {}, NOFILTER, step="1E+3") == []
    a = only(run({"C": D(1999)}, {}, NOFILTER, step="1E+3"))
    assert a.size == D(1000)


def test_usd_minimum_is_checked_after_rounding() -> None:
    # C vale 1 USD. 10.5 con paso 1 -> 10 unidades = 10 USD: pasa.
    assert only(run({"C": D("10.5")}, {}, step="1")).size == D(10)
    # 10.5 con paso 3 -> 9 unidades = 9 USD < 10: se descarta (supera el mínimo
    # del mercado, así que lo descarta el filtro en USD).
    assert run({"C": D("10.5")}, {}, step="3") == []


def test_increase_rounds_down_and_reduce_rounds_up_never_beyond_target() -> None:
    inc = only(run({"A": D("1.97")}, {"A": D(1)}, step="0.1"))
    assert (inc.kind, inc.size) == (ActionKind.INCREASE, D("0.9"))  # queda en 1.9 <= 1.97
    red = only(run({"A": D("0.43")}, {"A": D(1)}, step="0.1"))
    assert (red.kind, red.size, red.reduce_only) == (ActionKind.REDUCE, D("0.6"), True)  # 0.4


def test_reduce_rounding_up_is_capped_at_current_position() -> None:
    red = only(run({"A": D("0.05")}, {"A": D("0.2")}, NOFILTER, step="0.1"))
    assert (red.kind, red.size, red.reduce_only) == (ActionKind.REDUCE, D("0.2"), True)


def test_remaining_position_below_market_step_can_still_be_reduced() -> None:
    # Posición residual de 500 en un mercado de paso 1000: liquidarla es válido
    red = only(run({"C": D(100)}, {"C": D(500)}, NOFILTER, step="1E+3"))
    assert (red.size, red.reduce_only) == (D(500), True)


def test_rebalance_threshold_uses_rounded_delta() -> None:
    # 10 -> 10.9 con paso 1: el delta redondeado es 0 -> nada que hacer
    assert run({"A": D("10.9")}, {"A": D(10)}, step="1") == []


def test_close_and_flip_close_use_exact_position_size() -> None:
    # La posición actual no es múltiplo del paso: se cierra exacta, sin redondear
    a = only(run({}, {"A": D("0.123456")}, step="0.1"))
    assert (a.kind, a.size) == (ActionKind.CLOSE, D("0.123456"))
    acts = run({"A": D("-1.27")}, {"A": D("0.123456")}, step="0.1")
    assert [(x.kind, x.size) for x in acts] == [
        (ActionKind.FLIP_CLOSE, D("0.123456")), (ActionKind.FLIP_OPEN, D("1.2")),
    ]
