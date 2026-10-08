"""Mapeo de activos Hyperliquid -> perpetuos PF_*USD de Kraken Futures."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from decimal import Decimal

from copybot.config import SymbolsConfig

log = logging.getLogger(__name__)

# Kraken denomina XBT a bitcoin (p.ej. PF_XBTUSD). Otros alias se añaden por
# config en [symbols.overrides], y en ejecución todo se contrasta con la lista
# real de instrumentos del exchange.
_DEFAULT_ALIASES: Mapping[str, str] = {"BTC": "XBT"}


class SymbolMapper:
    def __init__(self, cfg: SymbolsConfig, available_markets: Iterable[str]) -> None:
        self._overrides = dict(cfg.overrides)
        self._factors = dict(cfg.size_factor)
        self._available = frozenset(available_markets)
        self._warned: set[str] = set()

    @staticmethod
    def default_symbol(coin: str) -> str:
        base = _DEFAULT_ALIASES.get(coin, coin).upper()
        return f"PF_{base}USD"

    def symbol_for(self, coin: str) -> str | None:
        """Símbolo Kraken o None si no hay mercado (avisa una sola vez por activo)."""
        sym = self._overrides.get(coin) or self.default_symbol(coin)
        if sym in self._available:
            return sym
        if coin not in self._warned:
            self._warned.add(coin)
            log.warning("activo %s sin mercado %s en Kraken: se ignora", coin, sym)
        return None

    def size_factor(self, coin: str) -> Decimal:
        return self._factors.get(coin, Decimal(1))
