from __future__ import annotations

from collections.abc import Iterable

from copybot.config import SymbolsConfig
from copybot.symbols import SymbolMapper

MARKETS = ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD", "PF_PEPEUSD", "PF_DOGEUSD")


def mapper(markets: Iterable[str] = MARKETS, **cfg: object) -> SymbolMapper:
    return SymbolMapper(SymbolsConfig.model_validate(cfg), markets)
