"""Controles de cordura sobre los datos del líder (función pura).

Ante la duda, no se opera:
- Cualquier control fallido -> se salta el ciclo (SKIP) y se alerta.
- N ciclos seguidos con algún fallo -> el bot se detiene sin operar (HALT).

Controles:
- capital del líder <= 0;
- datos obsoletos (más antiguos que leader_stale_seconds) o "del futuro";
- capital que cambia más de max_equity_jump_pct respecto al último dato
  aceptado (un depósito o retiro también lo dispara: es intencionado);
- posición YA ABIERTA que crece más de max_position_jump_factor veces respecto
  al último dato aceptado. Las aperturas desde 0 no se comprueban, ni las
  reducciones o cierres (bajan el riesgo).

La referencia es siempre el último dato ACEPTADO, no el último visto: así un
salto que persiste sigue contando como fallo y acaba deteniendo el bot, en vez
de "normalizarse" al ciclo siguiente. Tras revisar la causa, --reset-halt
empieza con SanityState() vacío y el siguiente dato válido pasa a ser la
referencia.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from copybot.config import SanityConfig
from copybot.models import LeaderSnapshot

log = logging.getLogger(__name__)


class Verdict(StrEnum):
    OK = "ok"
    SKIP = "skip"  # no operar en este ciclo y alertar
    HALT = "halt"  # detener el bot sin operar y alertar


@dataclass(frozen=True)
class SanityState:
    """Lo que hay que persistir entre ciclos y reinicios (state.json)."""

    baseline_equity: Decimal | None = None
    baseline_positions: Mapping[str, Decimal] = field(
        default_factory=lambda: MappingProxyType({})
    )
    consecutive_failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_equity": None if self.baseline_equity is None else str(self.baseline_equity),
            "baseline_positions": {c: str(s) for c, s in self.baseline_positions.items()},
            "consecutive_failures": self.consecutive_failures,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SanityState:
        eq = data.get("baseline_equity")
        return cls(
            baseline_equity=None if eq is None else Decimal(eq),
            baseline_positions=MappingProxyType(
                {c: Decimal(s) for c, s in data.get("baseline_positions", {}).items()}
            ),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
        )


@dataclass(frozen=True)
class SanityResult:
    verdict: Verdict
    reasons: tuple[str, ...]
    state: SanityState  # estado nuevo a persistir

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.OK


def _failures(
    snap: LeaderSnapshot,
    state: SanityState,
    cfg: SanityConfig,
    stale_seconds: Decimal,
    now: datetime,
) -> list[str]:
    reasons: list[str] = []

    if snap.timestamp.tzinfo is None or now.tzinfo is None:
        return ["timestamp sin zona horaria: no se puede comprobar la antigüedad"]
    age = now - snap.timestamp
    if age > timedelta(seconds=float(stale_seconds)):
        reasons.append(f"datos del líder obsoletos ({age.total_seconds():.0f} s)")
    elif -age > timedelta(seconds=float(cfg.max_clock_skew_seconds)):
        reasons.append(f"datos del líder con fecha futura ({-age.total_seconds():.0f} s)")

    if snap.equity_usd <= 0:
        reasons.append(f"capital del líder no positivo ({snap.equity_usd})")
    elif state.baseline_equity is not None:
        base = state.baseline_equity
        change_pct = abs(snap.equity_usd - base) / base * 100
        if change_pct > cfg.max_equity_jump_pct:
            reasons.append(
                f"capital del líder {base} -> {snap.equity_usd} "
                f"({change_pct:.1f} % > {cfg.max_equity_jump_pct} %); "
                "¿depósito o retiro?"
            )

    for coin in sorted(snap.positions):
        new = snap.positions[coin]
        old = state.baseline_positions.get(coin, Decimal(0))
        if old != 0 and new != 0 and abs(new) > cfg.max_position_jump_factor * abs(old):
            reasons.append(
                f"posición del líder en {coin} salta de {old} a {new} "
                f"(más de {cfg.max_position_jump_factor} veces)"
            )
    return reasons


def check_leader(
    snap: LeaderSnapshot,
    state: SanityState,
    cfg: SanityConfig,
    *,
    stale_seconds: Decimal,
    now: datetime,
) -> SanityResult:
    reasons = _failures(snap, state, cfg, stale_seconds, now)
    if not reasons:
        accepted = SanityState(
            baseline_equity=snap.equity_usd,
            baseline_positions=MappingProxyType({c: s for c, s in snap.positions.items() if s}),
            consecutive_failures=0,
        )
        return SanityResult(Verdict.OK, (), accepted)

    failures = state.consecutive_failures + 1
    kept = SanityState(state.baseline_equity, state.baseline_positions, failures)
    if failures >= cfg.halt_after_consecutive_failures:
        log.error("sanity: %d ciclos seguidos con fallos, se detiene: %s",
                  failures, "; ".join(reasons))
        return SanityResult(Verdict.HALT, tuple(reasons), kept)
    log.warning("sanity: ciclo saltado (%d/%d): %s",
                failures, cfg.halt_after_consecutive_failures, "; ".join(reasons))
    return SanityResult(Verdict.SKIP, tuple(reasons), kept)
