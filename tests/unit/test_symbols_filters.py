from __future__ import annotations

import logging
from decimal import Decimal as D

import pytest

from copybot.config import FiltersConfig
from copybot.filters import eligible_positions, initial_preexisting, is_allowed, update_preexisting
from tests.helpers import mapper


def test_default_mapping_and_btc_alias() -> None:
    m = mapper()
    assert m.symbol_for("BTC") == "PF_XBTUSD"
    assert m.symbol_for("ETH") == "PF_ETHUSD"
    assert m.size_factor("ETH") == 1


def test_override_and_size_factor() -> None:
    m = mapper(overrides={"kPEPE": "PF_PEPEUSD"}, size_factor={"kPEPE": 1000})
    assert m.symbol_for("kPEPE") == "PF_PEPEUSD"
    assert m.size_factor("kPEPE") == 1000


def test_missing_market_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    m = mapper()
    with caplog.at_level(logging.WARNING):
        assert m.symbol_for("HYPE") is None
        assert m.symbol_for("HYPE") is None
    assert sum("HYPE" in r.message for r in caplog.records) == 1


def test_allow_deny() -> None:
    assert is_allowed("BTC", FiltersConfig())
    assert not is_allowed("BTC", FiltersConfig(deny=("BTC",)))
    assert is_allowed("BTC", FiltersConfig(allow=("BTC",)))
    assert not is_allowed("ETH", FiltersConfig(allow=("BTC",)))


def test_preexisting_lifecycle() -> None:
    pre = initial_preexisting({"BTC": D(1), "ETH": D(-2), "SOL": D(0)})
    assert pre == {"BTC": D(1), "ETH": D(-2)}
    cfg = FiltersConfig()
    # Mientras siga abierta en la misma dirección, ignorada aunque cambie de tamaño
    pre = update_preexisting(pre, {"BTC": D(3), "ETH": D(-2)})
    assert eligible_positions({"BTC": D(3), "ETH": D(-2), "SOL": D(1)}, cfg, pre) == {"SOL": D(1)}
    # BTC se cierra -> liberada; ETH da la vuelta -> liberada (posición nueva)
    pre = update_preexisting(pre, {"ETH": D(1)})
    assert pre == {}
    # Si vuelve a abrir BTC, ya se copia
    assert eligible_positions({"BTC": D(1)}, cfg, pre) == {"BTC": D(1)}


def test_preexisting_can_be_disabled() -> None:
    cfg = FiltersConfig(ignore_preexisting=False)
    assert eligible_positions({"BTC": D(1)}, cfg, {"BTC": D(1)}) == {"BTC": D(1)}
