from __future__ import annotations

import json
import os
import stat
from decimal import Decimal as D
from pathlib import Path

import pytest

from copybot import limits
from copybot.config import RiskConfig, SizingConfig
from copybot.risk import (
    CircuitBreaker,
    activate_startup_profile_on_first_live,
    drawdown_tripped,
    effective_sizing,
    halt,
    kill_switch_active,
    record_cycle_error,
    record_cycle_ok,
    release_startup_profile,
    reset_halt,
)
from copybot.sources.sanity import SanityState
from copybot.state import AlreadyRunning, BotState, InstanceLock, StateError, StateStore


def full_state() -> BotState:
    return BotState(
        halted=True, halt_reason="x", halted_at="2026-10-08T00:00:00+00:00",
        peak_equity_usd=D("612.5"), consecutive_errors=2, preexisting_initialized=True,
        preexisting={"BTC": D("-1.5")}, managed_symbols={"PF_ETHUSD"},
        sanity=SanityState(D(1000), {"ETH": D(2)}, 1),  # type: ignore[arg-type]
        pending_orders={"abc": {"symbol": "PF_ETHUSD"}},
        breaker_log=[(1.5, D("10.25"))], live_startup_profile=True,
        last_equity_record_at=123.0, paper={"eur_collateral": "500"},
    )


def test_state_roundtrip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    st = full_state()
    store.save(st)
    loaded = store.load()
    assert loaded.to_dict() == st.to_dict()
    assert loaded.peak_equity_usd == D("612.5")  # Decimal exacto, no float


def test_state_is_bound_to_the_mode_that_created_it(tmp_path: Path) -> None:
    st = BotState()
    st.bind_mode("paper")
    st.bind_mode("paper")  # idempotente
    store = StateStore(tmp_path / "state.json")
    store.save(st)
    loaded = store.load()
    assert loaded.mode == "paper"
    with pytest.raises(StateError, match="no comparten estado"):
        loaded.bind_mode("live")


def test_missing_state_is_a_fresh_state(tmp_path: Path) -> None:
    assert StateStore(tmp_path / "state.json").load() == BotState()


def test_atomic_save_leaves_no_temp_files_and_private_dir(tmp_path: Path) -> None:
    d = tmp_path / "data"
    store = StateStore(d / "state.json")
    for i in range(5):
        store.save(BotState(consecutive_errors=i))
    assert [p.name for p in d.iterdir()] == ["state.json"]
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert store.load().consecutive_errors == 4


def test_failed_write_keeps_previous_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(BotState(consecutive_errors=1))

    def boom(*a: object) -> None:
        raise OSError("disco lleno")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.save(BotState(consecutive_errors=2))
    monkeypatch.undo()
    assert store.load().consecutive_errors == 1
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


@pytest.mark.parametrize("content", ["{no json", '{"version": 99}', '{"version": 1}', "[]"])
def test_unreadable_state_is_never_silently_reset(tmp_path: Path, content: str) -> None:
    p = tmp_path / "state.json"
    p.write_text(content)
    with pytest.raises(StateError):
        StateStore(p).load()


def test_before_save_hook(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.json",
                       before_save=lambda st: setattr(st, "paper", {"x": 1}))
    st = BotState()
    store.save(st)
    assert json.loads((tmp_path / "s.json").read_text())["paper"] == {"x": 1}


def test_instance_lock_blocks_second_instance(tmp_path: Path) -> None:
    lock = tmp_path / "copybot.lock"
    with InstanceLock(lock), pytest.raises(AlreadyRunning):
        InstanceLock(lock).acquire()
    with InstanceLock(lock):  # liberado: se puede volver a tomar
        pass


def test_kill_switch(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert kill_switch_active([a, b]) is None
    (b / "STOP").touch()
    assert kill_switch_active([a, b]) == b / "STOP"


def test_halt_is_persistent_and_reset_clears_counters(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.json")
    st = BotState(consecutive_errors=3, sanity=SanityState(D(5), {}, 2))  # type: ignore[arg-type]
    halt(st, "prueba")
    halt(st, "otro motivo")  # el primer motivo se conserva
    store.save(st)
    reloaded = store.load()
    assert reloaded.halted and reloaded.halt_reason == "prueba"
    reset_halt(reloaded)
    assert not reloaded.halted and reloaded.consecutive_errors == 0
    assert reloaded.paced_streak == 0 and not reloaded.kill_switch_closed
    assert reloaded.sanity == SanityState()


def test_consecutive_errors_halt_after_limit() -> None:
    st, cfg = BotState(), RiskConfig(max_consecutive_errors=3)
    assert [record_cycle_error(st, cfg) for _ in range(3)] == [False, False, True]
    record_cycle_ok(st)
    assert st.consecutive_errors == 0


def test_drawdown_peak_persists_across_restarts(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.json")
    cfg = RiskConfig(max_drawdown_pct=D(15))
    st = BotState()
    assert drawdown_tripped(st, D(500), cfg) is None
    assert drawdown_tripped(st, D(600), cfg) is None  # nuevo pico
    store.save(st)
    st2 = store.load()  # reinicio: el pico NO se pierde
    assert st2.peak_equity_usd == D(600)
    assert drawdown_tripped(st2, D(511), cfg) is None  # 14.83 %
    assert drawdown_tripped(st2, D(510), cfg) == D(15)
    assert drawdown_tripped(st2, D(0), cfg) == D(100)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def test_circuit_breaker_orders_per_minute_is_a_pace_not_a_stop() -> None:
    st, clock = BotState(), Clock()
    br = CircuitBreaker(st, RiskConfig(max_orders_per_minute=3), clock=clock)
    for _ in range(3):
        assert not br.minute_limit_reached()
        br.record(D(1))
    assert br.minute_limit_reached()
    clock.t += 61
    assert not br.minute_limit_reached()


def test_circuit_breaker_notional_per_hour_survives_restart(tmp_path: Path) -> None:
    st, clock = BotState(), Clock()
    cfg = RiskConfig(max_notional_per_hour_usd=D(1000))
    br = CircuitBreaker(st, cfg, clock=clock)
    br.record(D(900))
    store = StateStore(tmp_path / "s.json")
    store.save(st)
    br2 = CircuitBreaker(store.load(), cfg, clock=clock)  # tras reinicio
    assert br2.check_notional(D(100)) is None
    assert "nocional" in (br2.check_notional(D("100.01")) or "")
    clock.t += 3600
    assert br2.check_notional(D(1000)) is None  # la ventana de una hora caduca


def test_breaker_uses_hard_limits_even_if_config_was_bypassed() -> None:
    cfg = RiskConfig.model_construct(  # saltándose la validación a propósito
        max_orders_per_minute=999, max_notional_per_hour_usd=D(10**9),
        max_consecutive_errors=5, max_drawdown_pct=D(15),
    )
    st, clock = BotState(), Clock()
    br = CircuitBreaker(st, cfg, clock=clock)
    assert br.check_notional(limits.HARD_MAX_NOTIONAL_PER_HOUR_USD + 1) is not None
    for _ in range(limits.HARD_MAX_ORDERS_PER_MINUTE):
        br.record(D(1))
    assert br.minute_limit_reached()


def test_paced_cycles_limit_respects_hard_cap(tmp_path: Path) -> None:
    from copybot.config import ConfigError, load_config
    from tests.conftest import LEADER

    p = tmp_path / "c.toml"
    p.write_text(f'leader_address = "{LEADER}"\n[risk]\nmax_consecutive_paced_cycles = 6\n')
    with pytest.raises(ConfigError, match="tope absoluto"):
        load_config(p)


def test_startup_profile_activates_once_and_only_explicit_release(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.json")
    st = BotState()
    assert st.live_startup_profile is None
    assert activate_startup_profile_on_first_live(st) is True
    store.save(st)
    st = store.load()
    assert activate_startup_profile_on_first_live(st) is True  # sigue activo tras reiniciar
    release_startup_profile(st)
    assert activate_startup_profile_on_first_live(st) is False  # no se reactiva solo


def test_effective_sizing_with_startup_profile() -> None:
    cfg = SizingConfig(max_asset_usd=D(600), max_total_leverage=D(2), max_asset_pct_equity=D(25))
    eff = effective_sizing(cfg, startup_profile=True)
    assert eff.max_total_leverage == limits.STARTUP_PROFILE_MAX_LEVERAGE == D(1)
    assert eff.max_asset_usd == limits.STARTUP_PROFILE_MAX_ASSET_USD == D(100)
    assert eff.max_asset_pct_equity == D(25)
    assert effective_sizing(cfg, startup_profile=False) is cfg
    # Una config ya más estricta que el perfil se respeta
    strict = SizingConfig(max_asset_usd=D(50), max_total_leverage=D("0.5"))
    eff2 = effective_sizing(strict, startup_profile=True)
    assert (eff2.max_asset_usd, eff2.max_total_leverage) == (D(50), D("0.5"))
    assert eff2.max_asset_pct_equity <= 50


# --- M13: stop de catástrofe = drawdown / apalancamiento efectivo ---


@pytest.mark.parametrize(
    ("leverage", "explicit", "expected"),
    [
        ("1", None, "15"),  # perfil de arranque: 1x -> 15 %
        ("2", None, "7.5"),  # 2x -> 7,5 %
        ("3", None, "5"),  # 3x -> 5 % (mínimo absoluto)
        ("0.5", None, "30"),
        ("0.1", None, "50"),  # 150 % -> máximo absoluto
        ("1", "10", "10"),  # la config puede acercarlo...
        ("2", "20", "7.5"),  # ...pero nunca alejarlo del criterio de drawdown
    ],
)
def test_catastrophe_stop_distance_follows_drawdown_over_leverage(
    leverage: str, explicit: str | None, expected: str
) -> None:
    from copybot.risk import catastrophe_stop_pct

    cfg = RiskConfig(max_drawdown_pct=D(15), catastrophe_stop_pct=None if explicit is None
                     else D(explicit))
    assert catastrophe_stop_pct(cfg, D(leverage)) == D(expected)


def test_stop_loss_at_the_stop_equals_the_drawdown_limit() -> None:
    from copybot.risk import catastrophe_stop_pct

    cfg = RiskConfig(max_drawdown_pct=D(12))
    for leverage in (D(1), D(2), D(3)):
        pct = catastrophe_stop_pct(cfg, leverage)
        assert pct * leverage <= D(12) or pct == limits.HARD_MIN_CATASTROPHE_STOP_PCT
