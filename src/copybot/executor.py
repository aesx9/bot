"""Ejecución de acciones del planificador.

Garantías:
- Toda orden es límite IOC: compra como mucho a ref x (1 + tope) y vende como
  poco a ref x (1 - tope), redondeado al tick en la dirección conservadora.
- reduceOnly en toda reducción (viene del planificador y se respeta tal cual).
- Idempotencia: cada orden lleva un cliOrdId único que se guarda en el estado
  ANTES de enviarla. Si el envío falla o expira, se pregunta al exchange por
  ese cliOrdId antes de hacer nada más; si no se puede saber, la orden queda
  pendiente y el ciclo se aborta: nunca se reenvía a ciegas. El siguiente
  ciclo reconcilia las pendientes antes de planificar.
- Un cambio de dirección solo abre la nueva posición si el cierre se completó.
- Circuit breaker: el límite de órdenes por minuto nunca se supera; las
  acciones que no caben se aplazan al ciclo siguiente (vienen priorizadas
  del planificador). Superar el nocional por hora detiene el bot.
- Cierres de emergencia (drawdown, kill switch): solo reduceOnly y sin
  límite del circuit breaker.
- Tope absoluto por activo comprobado antes de cada envío.
- Las ejecuciones parciales no se reintentan aquí: el ciclo siguiente parte de
  la posición real y planifica el resto.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from copybot import limits
from copybot.config import ExecutionConfig
from copybot.exchange.base import (
    Exchange,
    ExchangeError,
    OrderRequest,
    OrderResult,
    OrderStatus,
)
from copybot.models import Action, ActionKind, MarketSpec, Side, plain
from copybot.records import CsvRecorder, TradeRecord
from copybot.risk import CircuitBreaker
from copybot.state import BotState, StateStore

log = logging.getLogger(__name__)
ZERO = Decimal(0)


class CircuitBreakerTripped(Exception):
    pass


class OrderUncertain(Exception):
    """No se sabe si una orden llegó al exchange: no operar hasta reconciliar."""


@dataclass(frozen=True)
class ExecutionReport:
    results: list[OrderResult]
    deferred: int = 0  # acciones aplazadas por el límite de órdenes/min
    skipped: int = 0  # aperturas/aumentos omitidos por superar la exposición real permitida


@dataclass(frozen=True)
class ExposureLimits:
    """Topes sobre la exposición REAL (posiciones actuales + la orden), no sobre el objetivo.

    El objetivo respeta los topes, pero con cierres parciales o que no se ejecutan, con
    precios que se mueven bajo el umbral de reajuste, o con el perfil de arranque, las
    posiciones reales pueden estar por encima de él: una orden que AUMENTA riesgo no se
    envía si dejaría la cuenta por encima."""

    prices: Mapping[str, Decimal]  # precio mark por símbolo, para valorar lo ya abierto
    max_asset_usd: Decimal
    max_total_usd: Decimal

@dataclass(frozen=True)
class ExecutionContext:
    mode: str
    leader_prices: Mapping[str, Decimal] = field(default_factory=dict)  # por símbolo Kraken
    leader_time: datetime | None = None  # momento del cambio del líder (retraso)


def limit_price(side: Side, ref: Decimal, cap_pct: Decimal, tick: Decimal) -> Decimal:
    if side is Side.BUY:
        raw, rounding = ref * (1 + cap_pct / 100), ROUND_FLOOR
    else:
        raw, rounding = ref * (1 - cap_pct / 100), ROUND_CEILING
    return (raw / tick).to_integral_value(rounding=rounding) * tick


class Executor:
    def __init__(
        self,
        *,
        exchange: Exchange,
        store: StateStore,
        state: BotState,
        breaker: CircuitBreaker,
        recorder: CsvRecorder,
        cfg: ExecutionConfig,
        send_timeout_seconds: float = 10,
        pending_grace_seconds: float = 60,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ex = exchange
        self._store = store
        self._state = state
        self._breaker = breaker
        self._rec = recorder
        self._cap = min(cfg.slippage_cap_pct, limits.HARD_MAX_SLIPPAGE_PCT)
        self._timeout = send_timeout_seconds
        self._grace = pending_grace_seconds
        self._now = now

    # --- reconciliación ---

    async def reconcile_pending(self) -> None:
        """Resuelve las órdenes pendientes de ciclos anteriores. Lanza si alguna sigue dudosa."""
        for cli, info in list(self._state.pending_orders.items()):
            result = await self._ex.find_order(cli)
            if result is None:
                age = (self._now() - datetime.fromisoformat(info["created_at"])).total_seconds()
                if age < self._grace:
                    raise OrderUncertain(f"orden {cli} sin confirmar ({age:.0f} s)")
                log.warning("orden %s no consta en el exchange tras %.0f s: no se envió",
                            cli, age)
            else:
                self._record(info, result)
            del self._state.pending_orders[cli]
            self._store.save(self._state)

    # --- ejecución ---

    async def execute(
        self,
        actions: list[Action],
        *,
        markets: Mapping[str, MarketSpec],
        positions: Mapping[str, Decimal],
        ctx: ExecutionContext,
        emergency: bool = False,
        exposure: ExposureLimits | None = None,
    ) -> ExecutionReport:
        """emergency=True (cierre por drawdown o kill switch): solo admite órdenes
        reduceOnly y no las frena el circuit breaker, que existe para impedir
        AUMENTAR riesgo."""
        pos = dict(positions)
        results: list[OrderResult] = []
        incomplete_flip: set[str] = set()
        skipped = 0

        for i, a in enumerate(actions):
            if not emergency and self._breaker.minute_limit_reached():
                deferred = len(actions) - i
                log.warning("límite de órdenes por minuto alcanzado: %d acciones aplazadas",
                            deferred)
                return ExecutionReport(results, deferred, skipped)
            if a.kind is ActionKind.FLIP_OPEN and a.symbol in incomplete_flip:
                log.warning("%s: el cierre del cambio de dirección no se completó; "
                            "la apertura queda para el ciclo siguiente", a.symbol)
                continue

            cur = pos.get(a.symbol, ZERO)
            delta = a.size if a.side is Side.BUY else -a.size
            if not a.reduce_only:
                projected = abs(cur + delta) * a.ref_price
                if projected > limits.HARD_MAX_NOTIONAL_PER_ASSET_USD:
                    raise limits.HardLimitViolation(
                        f"{a.symbol}: la orden dejaría {projected} USD > tope absoluto"
                    )
            if emergency:
                if not a.reduce_only:
                    raise limits.HardLimitViolation("cierre de emergencia con orden no reduceOnly")
            else:
                if not a.reduce_only and exposure is not None:
                    over = _exposure_excess(a, cur, delta, pos, exposure)
                    if over:
                        log.warning("%s: orden omitida, superaría la exposición permitida (%s)",
                                    a.symbol, over)
                        skipped += 1
                        continue
                reason = self._breaker.check_notional(a.notional_usd)
                if reason:
                    raise CircuitBreakerTripped(reason)

            spec = markets[a.symbol]
            req = OrderRequest(
                cli_ord_id=uuid.uuid4().hex,
                symbol=a.symbol, side=a.side, size=a.size,
                limit_price=limit_price(a.side, a.ref_price, self._cap, spec.tick_size),
                reduce_only=a.reduce_only,
            )
            info: dict[str, Any] = {
                "symbol": a.symbol, "side": a.side.value, "size": plain(a.size),
                "limit_price": plain(req.limit_price), "reduce_only": a.reduce_only,
                "ref_price": plain(a.ref_price), "action": a.kind.value,
                "leader_price": _opt(ctx.leader_prices.get(a.symbol)),
                "leader_time": None if ctx.leader_time is None else ctx.leader_time.isoformat(),
                "mode": ctx.mode, "created_at": self._now().isoformat(),
            }
            # Primero se persiste la intención; después se envía. El símbolo pasa a
            # gestionado AHORA: si el ciclo se aborta después de este envío, la posición
            # abierta ya consta (kill switch, drawdown y stops la cubren).
            self._state.pending_orders[req.cli_ord_id] = info
            self._state.managed_symbols.add(a.symbol)
            self._breaker.record(a.notional_usd)
            if emergency:
                # Un cierre reduceOnly de emergencia nunca puede quedar bloqueado por no
                # poder escribir en disco (disco lleno, sistema de ficheros de solo lectura).
                self._best_effort_save()
            else:
                self._store.save(self._state)

            result = await self._send(req)
            del self._state.pending_orders[req.cli_ord_id]
            if emergency:
                try:
                    self._record(info, result)
                except Exception:
                    log.exception("cierre de emergencia: no se pudo registrar la operación")
                self._best_effort_save()
            else:
                self._record(info, result)
                self._store.save(self._state)
            results.append(result)

            if result.filled_size > 0:
                signed = result.filled_size if a.side is Side.BUY else -result.filled_size
                pos[a.symbol] = cur + signed
            if result.status is OrderStatus.REJECTED:
                log.warning("%s: orden rechazada (%s)", a.symbol, result.reason)
            if a.kind is ActionKind.FLIP_CLOSE and result.status is not OrderStatus.FILLED:
                incomplete_flip.add(a.symbol)
        return ExecutionReport(results, skipped=skipped)

    def _best_effort_save(self) -> None:
        try:
            self._store.save(self._state)
        except Exception:
            log.exception("cierre de emergencia: no se pudo guardar el estado; se sigue")

    async def _send(self, req: OrderRequest) -> OrderResult:
        try:
            return await asyncio.wait_for(self._ex.send_order(req), timeout=self._timeout)
        except (ExchangeError, TimeoutError, OSError) as exc:
            log.warning("envío de %s incierto (%s): se consulta al exchange",
                        req.cli_ord_id, type(exc).__name__)
        try:
            found = await self._ex.find_order(req.cli_ord_id)
        except (ExchangeError, TimeoutError, OSError) as exc:
            raise OrderUncertain(
                f"orden {req.cli_ord_id}: ni envío ni consulta ({type(exc).__name__})"
            ) from exc
        if found is None:
            raise OrderUncertain(f"orden {req.cli_ord_id} sin confirmar tras el error")
        if found.status is OrderStatus.FILLED and found.filled_size < req.size:
            found = replace(found, status=OrderStatus.PARTIAL)
        return found

    def _record(self, info: Mapping[str, Any], result: OrderResult) -> None:
        if result.filled_size <= 0 or result.avg_price is None:
            return
        now = self._now()
        leader_time = info.get("leader_time")
        delay = None
        if isinstance(leader_time, str):
            delay = Decimal(str(round((now - datetime.fromisoformat(leader_time))
                                      .total_seconds(), 3)))
        leader_price = info.get("leader_price")
        self._rec.trade(TradeRecord(
            timestamp=now, mode=str(info["mode"]), symbol=str(info["symbol"]),
            action=str(info["action"]), side=str(info["side"]), size=result.filled_size,
            reduce_only=bool(info["reduce_only"]),
            leader_price=None if leader_price is None else Decimal(str(leader_price)),
            ref_price=Decimal(str(info["ref_price"])), fill_price=result.avg_price,
            fee_usd=result.fee_usd, delay_seconds=delay, cli_ord_id=result.cli_ord_id,
            status=result.status.value,
        ))


def _exposure_excess(
    a: Action, cur: Decimal, delta: Decimal, pos: Mapping[str, Decimal], exposure: ExposureLimits
) -> str:
    """Motivo si esta orden dejaría la exposición real por encima de los topes; '' si no."""
    asset = abs(cur + delta) * a.ref_price
    if asset > exposure.max_asset_usd:
        return f"{asset:.2f} USD en el activo > {exposure.max_asset_usd:.2f}"
    others = sum((abs(p) * exposure.prices.get(sym, a.ref_price)
                  for sym, p in pos.items() if sym != a.symbol), ZERO)
    if asset + others > exposure.max_total_usd:
        return f"{asset + others:.2f} USD en total > {exposure.max_total_usd:.2f}"
    return ""


def _opt(v: Decimal | None) -> str | None:
    return None if v is None else plain(v)
