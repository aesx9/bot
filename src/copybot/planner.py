"""Planificador: compara objetivo vs actual y produce acciones (función pura).

Reglas:
- Solo considera símbolos gestionados (los del líder o registrados en el estado).
  Las posiciones manuales en otros símbolos no se tocan nunca.
- Toda acción que reduce |posición| es reduceOnly.
- Cambio de dirección = cierre reduceOnly + apertura separada.
- Los cierres totales (objetivo 0) nunca se filtran por umbrales.
- Un objetivo nunca supera el máximo de posición del mercado (MarketSpec.max_position_size).
- Los tamaños se redondean al paso del mercado Kraken (MarketSpec):
  aperturas y aumentos hacia abajo, reducciones parciales hacia arriba (sin
  pasar de la posición actual). Así ninguna acción deja la posición por
  encima del objetivo, que ya respeta los topes.
- Aperturas/aumentos/reducciones parciales se descartan si, ya redondeadas,
  quedan bajo el mínimo del mercado o bajo min_order_usd; aumentos y
  reducciones además por rebalance_threshold_pct.
- Orden de salida (importa porque el circuit breaker limita las órdenes por
  minuto y las que no caben se aplazan): primero cierres (totales y de cambio
  de dirección), después reducciones y por último aperturas y aumentos; dentro
  de cada grupo, de mayor a menor nocional.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from decimal import Decimal

from copybot.config import PlannerConfig
from copybot.models import Action, ActionKind, MarketSpec, Side

ZERO = Decimal(0)


class PlannerError(Exception):
    pass


def plan(
    *,
    targets: Mapping[str, Decimal],
    current: Mapping[str, Decimal],
    managed: Collection[str],
    prices: Mapping[str, Decimal],
    markets: Mapping[str, MarketSpec],
    cfg: PlannerConfig,
) -> list[Action]:
    unmanaged_targets = set(targets) - set(managed)
    if unmanaged_targets:
        raise PlannerError(f"objetivos fuera del conjunto gestionado: {sorted(unmanaged_targets)}")

    reducing: list[Action] = []
    adding: list[Action] = []

    for symbol in sorted(managed):
        t = targets.get(symbol, ZERO)
        c = current.get(symbol, ZERO)
        if t == c:
            continue
        price = prices.get(symbol)
        if price is None or price <= 0:
            raise PlannerError(f"sin precio válido para {symbol}")
        spec = markets.get(symbol)
        if spec is None:
            raise PlannerError(f"sin especificación de mercado para {symbol}")
        # Nunca se pide una posición mayor que el máximo del mercado (maxPositionSize)
        if abs(t) > spec.max_position_size:
            t = spec.max_position_size if t > 0 else -spec.max_position_size
            if t == c:
                continue

        def act(
            kind: ActionKind, delta: Decimal, reduce_only: bool, reason: str,
            symbol: str = symbol, price: Decimal = price,
        ) -> Action:
            return Action(
                kind=kind, symbol=symbol, side=Side.for_delta(delta), size=abs(delta),
                reduce_only=reduce_only, ref_price=price, reason=reason,
            )

        def tradeable(
            size: Decimal, spec: MarketSpec = spec, price: Decimal = price, c: Decimal = c
        ) -> bool:
            # Liquidar todo lo que queda siempre es un tamaño válido para el exchange
            valid_size = size >= spec.min_size or (size > 0 and size == abs(c))
            return valid_size and size * price >= cfg.min_order_usd

        if t == 0:
            # Cierre total: tamaño exacto de la posición, sin redondeos ni filtros
            reducing.append(act(ActionKind.CLOSE, -c, True, "objetivo 0"))
            continue

        sign = Decimal(1) if t > 0 else Decimal(-1)

        if c == 0:
            size = spec.round_down(abs(t))
            if tradeable(size):
                adding.append(act(ActionKind.OPEN, sign * size, False, "nueva posición"))
            continue

        if (t > 0) != (c > 0):
            # Cambio de dirección: el cierre nunca se filtra; la apertura sí
            reducing.append(act(ActionKind.FLIP_CLOSE, -c, True, "cambio de dirección"))
            size = spec.round_down(abs(t))
            if tradeable(size):
                adding.append(act(ActionKind.FLIP_OPEN, sign * size, False, "cambio de dirección"))
            continue

        increasing = abs(t) > abs(c)
        if increasing:
            size = spec.round_down(abs(t) - abs(c))
        else:
            size = min(spec.round_up(abs(c) - abs(t)), abs(c))
        if not tradeable(size):
            continue
        if size / abs(c) * 100 < cfg.rebalance_threshold_pct:
            continue
        if increasing:
            adding.append(act(ActionKind.INCREASE, sign * size, False, "aumentar"))
        else:
            reducing.append(act(ActionKind.REDUCE, -sign * size, True, "reducir"))

    def by_notional(acts: list[Action]) -> list[Action]:
        return sorted(acts, key=lambda a: (-a.notional_usd, a.symbol))

    closes = [a for a in reducing if a.kind is not ActionKind.REDUCE]
    reductions = [a for a in reducing if a.kind is ActionKind.REDUCE]
    return by_notional(closes) + by_notional(reductions) + by_notional(adding)


def apply_actions(current: Mapping[str, Decimal], actions: list[Action]) -> dict[str, Decimal]:
    """Simula el resultado de ejecutar las acciones completas (útil en tests y paper)."""
    result = dict(current)
    for a in actions:
        sign = Decimal(1) if a.side is Side.BUY else Decimal(-1)
        result[a.symbol] = result.get(a.symbol, ZERO) + sign * a.size
        if result[a.symbol] == 0:
            del result[a.symbol]
    return result
