from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from copybot import limits
from copybot.config import Config, ConfigError, Mode, load_config
from tests.conftest import LEADER

ROOT = Path(__file__).resolve().parents[2]


def make(**overrides: Any) -> Config:
    data: dict[str, Any] = {"leader_address": LEADER}
    data.update(overrides)
    return Config.model_validate(data)


def test_hard_limits_are_the_agreed_values() -> None:
    # Si alguien cambia un tope absoluto, este test obliga a hacerlo a conciencia.
    assert Decimal("3") == limits.HARD_MAX_LEVERAGE
    assert Decimal("600") == limits.HARD_MAX_NOTIONAL_PER_ASSET_USD
    assert Decimal("1500") == limits.HARD_MAX_NOTIONAL_TOTAL_USD
    assert Decimal("0.5") == limits.HARD_MAX_SLIPPAGE_PCT
    assert limits.HARD_MAX_ORDERS_PER_MINUTE == 10
    assert Decimal("2000") == limits.HARD_MAX_NOTIONAL_PER_HOUR_USD
    assert limits.HARD_MAX_CONSECUTIVE_ERRORS == 5
    assert Decimal("15") == limits.HARD_MAX_DRAWDOWN_PCT


def test_defaults_are_paper_and_agreed_values() -> None:
    cfg = make()
    assert cfg.mode is Mode.PAPER
    assert cfg.sizing.multiplier == 1
    assert cfg.sizing.max_total_leverage == 2
    assert cfg.sizing.max_asset_pct_equity == 25
    assert cfg.risk.max_drawdown_pct == 15
    assert cfg.risk.close_all_on_drawdown is True
    assert cfg.risk.catastrophe_stop_pct == 20
    assert cfg.execution.slippage_cap_pct == Decimal("0.5")


def test_example_config_loads(tmp_path: Path) -> None:
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.mode is Mode.PAPER


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("sizing", "max_total_leverage", "3.01"),
        ("sizing", "max_asset_usd", "600.01"),
        ("execution", "slippage_cap_pct", "0.51"),
        ("risk", "max_drawdown_pct", "15.1"),
        ("risk", "max_orders_per_minute", 11),
        ("risk", "max_notional_per_hour_usd", "2000.01"),
        ("risk", "max_consecutive_errors", 6),
        ("risk", "catastrophe_stop_pct", "4.9"),
        ("risk", "catastrophe_stop_pct", "50.1"),
    ],
)
def test_exceeding_hard_limits_refuses_to_start(section: str, key: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        make(**{section: {key: value}})


def test_at_hard_limits_is_accepted() -> None:
    make(
        sizing={"max_total_leverage": 3, "max_asset_usd": 600},
        risk={"max_orders_per_minute": 10, "max_notional_per_hour_usd": 2000},
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"leader_address": "0x123"},
        {"mode": "real"},
        {"unknown_key": 1},
        {"sizing": {"multiplier": 0}},
        {"sizing": {"multiplier": True}},
        {"sizing": {"multiplier": "nan"}},
        {"filters": {"allow": ["BTC"], "deny": ["BTC"]}},
        {"symbols": {"overrides": {"BTC": "PI_XBTUSD"}}},
        {"symbols": {"overrides": {"A": "PF_XUSD", "B": "PF_XUSD"}}},
        {"symbols": {"size_factor": {"kPEPE": 0}}},
        {"timing": {"debounce_seconds": 60, "reconcile_interval_seconds": 30}},
        {"timing": {"ws_backoff_initial_seconds": 100, "ws_backoff_max_seconds": 10}},
    ],
)
def test_invalid_configs(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        make(**overrides)


def test_floats_from_toml_become_exact_decimals(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(f'leader_address = "{LEADER}"\n[planner]\nrebalance_threshold_pct = 0.1\n')
    cfg = load_config(p)
    assert cfg.planner.rebalance_threshold_pct == Decimal("0.1")


def test_address_is_normalised_lowercase() -> None:
    assert make(leader_address="0x" + "AB" * 20).leader_address == LEADER


def test_load_errors_are_readable_and_do_not_echo_values(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text('leader_address = "0xSECRETVALUE12345"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert "leader_address" in str(exc.value)
    assert "SECRETVALUE" not in str(exc.value)


def test_missing_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no existe"):
        load_config(tmp_path / "nope.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("mode = = 1")
    with pytest.raises(ConfigError, match="TOML"):
        load_config(bad)


def test_config_is_immutable() -> None:
    cfg = make()
    with pytest.raises(ValidationError):
        cfg.mode = Mode.LIVE  # type: ignore[misc]
