"""Filtros de activos del líder: allow/deny y posiciones preexistentes.

Posición preexistente: la que el líder ya tenía abierta cuando empezamos a
seguirlo. No la copiamos porque entraríamos a un precio distinto y sin saber
su plan. Se libera cuando el líder la cierra (tamaño 0) o le da la vuelta
(cambio de signo = posición nueva).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from copybot.config import FiltersConfig


def is_allowed(coin: str, cfg: FiltersConfig) -> bool:
    if coin in cfg.deny:
        return False
    return not cfg.allow or coin in cfg.allow


def initial_preexisting(leader_positions: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Al empezar a seguir al líder: todo lo abierto queda marcado (con su signo)."""
    return {c: s for c, s in leader_positions.items() if s != 0}


def update_preexisting(
    preexisting: Mapping[str, Decimal], leader_positions: Mapping[str, Decimal]
) -> dict[str, Decimal]:
    """Devuelve las que siguen siendo preexistentes tras observar el líder."""
    still: dict[str, Decimal] = {}
    for coin, orig in preexisting.items():
        now = leader_positions.get(coin, Decimal(0))
        if now != 0 and (now > 0) == (orig > 0):
            still[coin] = orig
    return still


def eligible_positions(
    leader_positions: Mapping[str, Decimal],
    cfg: FiltersConfig,
    preexisting: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    return {
        coin: size
        for coin, size in leader_positions.items()
        if size != 0
        and is_allowed(coin, cfg)
        and not (cfg.ignore_preexisting and coin in preexisting)
    }
