from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from copybot.config import SymbolsConfig
from copybot.models import MarketSpec
from copybot.symbols import SymbolMapper

MARKETS = ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD", "PF_PEPEUSD", "PF_DOGEUSD")


def mapper(markets: Iterable[str] = MARKETS, **cfg: object) -> SymbolMapper:
    return SymbolMapper(SymbolsConfig.model_validate(cfg), markets)


def specs(symbols: Iterable[str], step: Decimal | str = "0.0001") -> dict[str, MarketSpec]:
    return {
        s: MarketSpec(s, Decimal(step), tick_size=Decimal("0.01"), max_position_size=Decimal(10**9))
        for s in symbols
    }
