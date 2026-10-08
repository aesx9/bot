"""Planificador: compara objetivo vs actual y produce acciones (función pura).

Reglas:
- Solo considera símbolos gestionados (los del líder o registrados en el estado).
  Las posiciones manuales en otros símbolos no se tocan nunca.
- Toda acción que reduce |posición| es reduceOnly.
- Cambio de dirección = cierre reduceOnly + apertura separada.
- Los cierres totales (objetivo 0) nunca se filtran por umbrales.
- Aperturas/aumentos/reducciones parciales se filtran por min_order_usd;
  aumentos/reducciones además por rebalance_threshold_pct.
- Orden de salida: primero lo que libera margen (cierres y reducciones),
  después lo que lo consume.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from decimal import Decimal

from copybot.config import PlannerConfig
from copybot.models import Action, ActionKind, Side

ZERO = Decimal(0)


class PlannerError(Exception):
    pass


def plan(
    *,
    targets: Mapping[str, Decimal],
    current: Mapping[str, Decimal],
    managed: Collection[str],
    prices: Mapping[str, Decimal],
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

        def act(
            kind: ActionKind, delta: Decimal, reduce_only: bool, reason: str,
            symbol: str = symbol, price: Decimal = price,
        ) -> Action:
            return Action(
                kind=kind, symbol=symbol, side=Side.for_delta(delta), size=abs(delta),
                reduce_only=reduce_only, ref_price=price, reason=reason,
            )

        if t == 0:
            reducing.append(act(ActionKind.CLOSE, -c, True, "objetivo 0"))
            continue

        if c == 0:
            if abs(t) * price >= cfg.min_order_usd:
                adding.append(act(ActionKind.OPEN, t, False, "nueva posición"))
            continue

        if (t > 0) != (c > 0):
            # Cambio de dirección: el cierre nunca se filtra; la apertura sí
            reducing.append(act(ActionKind.FLIP_CLOSE, -c, True, "cambio de dirección"))
            if abs(t) * price >= cfg.min_order_usd:
                adding.append(act(ActionKind.FLIP_OPEN, t, False, "cambio de dirección"))
            continue

        delta = t - c
        if abs(delta) * price < cfg.min_order_usd:
            continue
        if abs(delta) / abs(c) * 100 < cfg.rebalance_threshold_pct:
            continue
        if abs(t) > abs(c):
            adding.append(act(ActionKind.INCREASE, delta, False, "aumentar"))
        else:
            reducing.append(act(ActionKind.REDUCE, delta, True, "reducir"))

    return reducing + adding


def apply_actions(current: Mapping[str, Decimal], actions: list[Action]) -> dict[str, Decimal]:
    """Simula el resultado de ejecutar las acciones completas (útil en tests y paper)."""
    result = dict(current)
    for a in actions:
        sign = Decimal(1) if a.side is Side.BUY else Decimal(-1)
        result[a.symbol] = result.get(a.symbol, ZERO) + sign * a.size
        if result[a.symbol] == 0:
            del result[a.symbol]
    return result
