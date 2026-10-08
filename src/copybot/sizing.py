"""Cálculo de posiciones objetivo (función pura, sin red).

Pasos:
1. ratio: equity -> (mi capital / capital líder) x multiplier; fixed -> fixed_ratio.
2. objetivo en unidades Kraken = tamaño líder x size_factor x ratio.
3. tope por activo = min(max_asset_usd, % del capital, tope absoluto).
4. tope total = min(apalancamiento config x capital, 3x capital, tope absoluto);
   si se supera, se reducen TODAS las posiciones en la misma proporción.
5. comprobación final contra limits.py (si falla, excepción: no se opera).

Todas las operaciones redondean hacia cero (ROUND_DOWN sobre magnitudes),
así el redondeo nunca puede empujar un tamaño por encima de un tope.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_DOWN, Context, Decimal, localcontext

from copybot import limits
from copybot.config import SizingConfig, SizingMode
from copybot.models import SizingResult
from copybot.symbols import SymbolMapper

# Precisión holgada: con tamaños truncados a 12 decimales, los productos
# tamaño x precio son exactos y el tope se cumple sin errores de redondeo.
_CTX = Context(prec=60, rounding=ROUND_DOWN)
UNITS_QUANTUM = Decimal("1e-12")  # muy por debajo de cualquier lote de mercado
ZERO = Decimal(0)


class SizingError(Exception):
    """Datos insuficientes o incoherentes para dimensionar: no se opera."""


def _signed(magnitude: Decimal, like: Decimal) -> Decimal:
    """Trunca la magnitud (hacia cero) y le aplica el signo de `like`."""
    q = magnitude.quantize(UNITS_QUANTUM, rounding=ROUND_DOWN)
    return q if like > 0 else -q


def per_asset_cap_usd(cfg: SizingConfig, my_equity: Decimal) -> Decimal:
    with localcontext(_CTX):
        return min(
            cfg.max_asset_usd,
            cfg.max_asset_pct_equity * my_equity / 100,
            limits.HARD_MAX_NOTIONAL_PER_ASSET_USD,
        )


def total_cap_usd(cfg: SizingConfig, my_equity: Decimal) -> Decimal:
    with localcontext(_CTX):
        return min(
            cfg.max_total_leverage * my_equity,
            limits.HARD_MAX_LEVERAGE * my_equity,
            limits.HARD_MAX_NOTIONAL_TOTAL_USD,
        )


def compute_targets(
    *,
    leader_equity: Decimal,
    leader_positions: Mapping[str, Decimal],
    my_equity: Decimal,
    prices: Mapping[str, Decimal],
    mapper: SymbolMapper,
    cfg: SizingConfig,
) -> SizingResult:
    """leader_positions ya filtradas (allow/deny/preexistentes).

    prices: precio de referencia Kraken por símbolo, para convertir a USD.
    """
    if leader_equity <= 0:
        raise SizingError(f"capital del líder no positivo: {leader_equity}")
    if my_equity <= 0:
        raise SizingError(f"capital propio no positivo: {my_equity}")

    with localcontext(_CTX):
        if cfg.mode is SizingMode.EQUITY:
            ratio = my_equity / leader_equity * cfg.multiplier
        else:
            ratio = cfg.fixed_ratio

        asset_cap = per_asset_cap_usd(cfg, my_equity)
        targets: dict[str, Decimal] = {}
        capped: set[str] = set()
        origin: dict[str, str] = {}

        for coin, lsize in leader_positions.items():
            if lsize == 0:
                continue
            symbol = mapper.symbol_for(coin)
            if symbol is None:
                continue
            if symbol in origin:
                raise SizingError(f"{coin} y {origin[symbol]} mapean al mismo símbolo {symbol}")
            origin[symbol] = coin
            price = prices.get(symbol)
            if price is None or price <= 0:
                raise SizingError(f"sin precio válido para {symbol}")

            units = abs(lsize) * mapper.size_factor(coin) * ratio
            if units * price > asset_cap:
                units = asset_cap / price
                capped.add(symbol)
            signed = _signed(units, lsize)
            if signed != 0:
                targets[symbol] = signed

        total = sum((abs(u) * prices[s] for s, u in targets.items()), ZERO)
        total_cap = total_cap_usd(cfg, my_equity)
        scale = Decimal(1)
        if total > total_cap:
            scale = total_cap / total
            targets = {s: _signed(abs(u) * scale, u) for s, u in targets.items()}
            targets = {s: u for s, u in targets.items() if u != 0}

    result = SizingResult(targets=targets, scale_applied=scale, capped_assets=frozenset(capped))
    assert_within_hard_limits(result.targets, prices, my_equity)
    return result


def assert_within_hard_limits(
    targets: Mapping[str, Decimal], prices: Mapping[str, Decimal], my_equity: Decimal
) -> None:
    """Defensa final independiente de la config. Lanza HardLimitViolation."""
    with localcontext(_CTX):
        total = ZERO
        for symbol, units in targets.items():
            notional = abs(units) * prices[symbol]
            if notional > limits.HARD_MAX_NOTIONAL_PER_ASSET_USD:
                raise limits.HardLimitViolation(
                    f"{symbol}: nocional {notional} > tope absoluto por activo"
                )
            total += notional
        if total > limits.HARD_MAX_NOTIONAL_TOTAL_USD:
            raise limits.HardLimitViolation(f"nocional total {total} > tope absoluto")
        if my_equity > 0 and total > limits.HARD_MAX_LEVERAGE * my_equity:
            raise limits.HardLimitViolation(f"apalancamiento {total / my_equity} > tope absoluto")
