from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from copybot.config import ConfigError, SanityConfig, load_config
from copybot.models import LeaderSnapshot
from copybot.sources.sanity import SanityState, Verdict, check_leader
from tests.conftest import LEADER

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
CFG = SanityConfig()  # factor 20, capital 50 %, N = 3


def snap(equity: str = "10000", age_s: float = 1, **positions: str) -> LeaderSnapshot:
    return LeaderSnapshot(
        equity_usd=D(equity),
        positions={c: D(s) for c, s in positions.items()},
        mids={},
        timestamp=NOW - timedelta(seconds=age_s),
    )


def check(s: LeaderSnapshot, state: SanityState | None = None, cfg: SanityConfig = CFG):  # type: ignore[no-untyped-def]
    return check_leader(s, state or SanityState(), cfg, stale_seconds=D(30), now=NOW)


def accepted(**kw: str) -> SanityState:
    r = check(snap(**kw))
    assert r.verdict is Verdict.OK
    return r.state


def test_valid_snapshot_becomes_baseline() -> None:
    r = check(snap(BTC="1.5", ETH="-2"))
    assert r.ok and r.reasons == ()
    assert r.state.baseline_equity == D(10000)
    assert dict(r.state.baseline_positions) == {"BTC": D("1.5"), "ETH": D(-2)}


@pytest.mark.parametrize("equity", ["0", "-5"])
def test_non_positive_equity_skips(equity: str) -> None:
    r = check(snap(equity=equity))
    assert r.verdict is Verdict.SKIP and "no positivo" in r.reasons[0]


def test_stale_data_skips() -> None:
    assert check(snap(age_s=31)).verdict is Verdict.SKIP
    assert check(snap(age_s=29)).ok


def test_future_data_beyond_clock_skew_skips() -> None:
    assert check(snap(age_s=-4)).ok
    r = check(snap(age_s=-6))
    assert r.verdict is Verdict.SKIP and "futura" in r.reasons[0]


def test_naive_timestamp_is_rejected() -> None:
    s = LeaderSnapshot(D(1), {}, {}, datetime(2026, 10, 8, 12, 0))
    assert check(s).verdict is Verdict.SKIP


def test_position_jump_only_for_already_open_positions() -> None:
    base = accepted(BTC="0.1")
    # Apertura desde 0, por grande que sea: no se comprueba
    assert check(snap(BTC="0.1", ETH="1000"), base).ok
    # x20 justo: se acepta; más de x20: se salta
    assert check(snap(BTC="2"), base).ok
    r = check(snap(BTC="2.0001"), base)
    assert r.verdict is Verdict.SKIP and "BTC" in r.reasons[0]
    # Un cambio de dirección cuenta por magnitud
    assert check(snap(BTC="-3"), base).verdict is Verdict.SKIP


def test_reductions_and_closes_are_not_jumps() -> None:
    base = accepted(BTC="10")
    assert check(snap(BTC="0.01"), base).ok
    assert check(snap(), base).ok


def test_equity_jump_skips_both_ways() -> None:
    base = accepted(equity="10000")
    assert check(snap(equity="15000"), base).ok  # 50 % justo
    assert check(snap(equity="15001"), base).verdict is Verdict.SKIP  # ¿depósito?
    assert check(snap(equity="4999"), base).verdict is Verdict.SKIP  # ¿retiro?


def test_persistent_failure_halts_after_n_cycles() -> None:
    state = accepted(equity="10000")
    verdicts = []
    for _ in range(3):
        r = check(snap(equity="30000"), state)  # p.ej. un depósito grande
        verdicts.append(r.verdict)
        state = r.state
    assert verdicts == [Verdict.SKIP, Verdict.SKIP, Verdict.HALT]
    # La referencia sigue siendo el último dato aceptado, no el visto
    assert state.baseline_equity == D(10000)


def test_one_good_cycle_resets_the_counter() -> None:
    state = accepted(equity="10000")
    state = check(snap(equity="30000"), state).state
    state = check(snap(equity="30000"), state).state
    assert state.consecutive_failures == 2
    r = check(snap(equity="10100"), state)
    assert r.ok and r.state.consecutive_failures == 0
    assert check(snap(equity="30000"), r.state).verdict is Verdict.SKIP


def test_reset_starts_a_new_baseline() -> None:
    # --reset-halt tras revisar un depósito: el siguiente dato válido es la referencia
    r = check(snap(equity="30000"), SanityState())
    assert r.ok and r.state.baseline_equity == D(30000)


def test_halt_after_one_failure_when_configured() -> None:
    cfg = SanityConfig(halt_after_consecutive_failures=1)
    assert check(snap(equity="0"), cfg=cfg).verdict is Verdict.HALT


def test_state_roundtrip_for_persistence() -> None:
    state = check(snap(BTC="1.5"), accepted(BTC="1")).state
    failed = check(snap(equity="0"), state).state
    assert SanityState.from_dict(failed.to_dict()) == failed
    assert SanityState.from_dict(SanityState().to_dict()) == SanityState()


def test_halt_threshold_respects_hard_limit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "c.toml"
    p.write_text(f'leader_address = "{LEADER}"\n[sanity]\nhalt_after_consecutive_failures = 6\n')
    with pytest.raises(ConfigError, match="tope absoluto"):
        load_config(p)
