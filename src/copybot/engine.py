"""Orquestación: un ciclo de copia y el bucle principal.

Un ciclo, en orden (cualquier duda -> no operar):
1. Kill switch (fichero STOP) -> parada persistente.
2. Si el bot está detenido, no hace nada.
3. Reconciliar órdenes pendientes de ciclos anteriores.
4. Leer el líder y pasar los controles de cordura (SKIP / HALT).
5. Filtros: preexistentes, allow/deny.
6. Datos de Kraken: instrumentos y precios mark.
7. Capital propio y drawdown (si salta: cerrar lo gestionado y detener).
8. Sizing -> planificador -> ejecución.
9. Funding, registro de capital y guardado del estado.

Los errores cuentan como ciclo fallido; N seguidos detienen el bot. Cortar
el circuit breaker o un tope absoluto detiene el bot al momento.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import httpx

from copybot import limits
from copybot.alerts import Alerter, Level
from copybot.config import Config
from copybot.exchange.base import Exchange, ExchangeError
from copybot.exchange.kraken_public import KrakenDataError, Ticker
from copybot.executor import CircuitBreakerTripped, ExecutionContext, Executor, OrderUncertain
from copybot.filters import eligible_positions, initial_preexisting, update_preexisting
from copybot.models import LeaderSnapshot, MarketSpec
from copybot.planner import PlannerError, plan
from copybot.records import CsvRecorder
from copybot.risk import (
    CircuitBreaker,
    drawdown_tripped,
    effective_sizing,
    halt,
    kill_switch_active,
    record_cycle_error,
    record_cycle_ok,
)
from copybot.sizing import SizingError, compute_targets
from copybot.sources.debounce import Debouncer
from copybot.sources.hyperliquid_rest import LeaderDataError
from copybot.sources.hyperliquid_ws import LeaderFill
from copybot.sources.sanity import Verdict, check_leader
from copybot.state import BotState, StateStore
from copybot.symbols import SymbolMapper

log = logging.getLogger(__name__)

CYCLE_ERRORS = (
    LeaderDataError, KrakenDataError, ExchangeError, SizingError, PlannerError,
    OrderUncertain, httpx.HTTPError, TimeoutError, OSError,
)


class Outcome(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"  # sanity: no se opera este ciclo
    ERROR = "error"
    HALTED = "halted"


class LeaderSource(Protocol):
    async def leader_snapshot(self, user: str) -> LeaderSnapshot: ...


class Stream(Protocol):
    async def run(self) -> None: ...
    async def stop(self) -> None: ...


class MarketSource(Protocol):
    async def instruments(self) -> dict[str, MarketSpec]: ...
    async def tickers(self) -> dict[str, Ticker]: ...


@dataclass
class CycleReport:
    outcome: Outcome
    detail: str = ""
    orders: int = 0


class Engine:
    def __init__(
        self,
        *,
        cfg: Config,
        state: BotState,
        store: StateStore,
        leader: LeaderSource,
        market: MarketSource,
        exchange: Exchange,
        recorder: CsvRecorder,
        alerter: Alerter,
        kill_dirs: Sequence[Path],
        startup_profile: bool = False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        breaker_clock: Callable[[], float] | None = None,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self._store = store
        self._leader = leader
        self._market = market
        self._ex = exchange
        self._rec = recorder
        self._alert = alerter
        self._kill_dirs = kill_dirs
        self._sizing_cfg = effective_sizing(cfg.sizing, startup_profile=startup_profile)
        self._now = now
        breaker = (CircuitBreaker(state, cfg.risk, clock=breaker_clock) if breaker_clock
                   else CircuitBreaker(state, cfg.risk))
        self._executor = Executor(
            exchange=exchange, store=store, state=state, breaker=breaker,
            recorder=recorder, cfg=cfg.execution, now=now,
        )
        self._mapper: SymbolMapper | None = None
        self._mapper_markets: set[str] = set()
        self._lock = asyncio.Lock()
        self.last_report: CycleReport | None = None

    # --- utilidades ---

    def _save(self) -> None:
        self._store.save(self.state)

    async def _halt(self, reason: str) -> CycleReport:
        halt(self.state, reason, self._now())
        self._save()
        await self._alert.alert(Level.CRITICAL, f"bot detenido: {reason}")
        return CycleReport(Outcome.HALTED, reason)

    def _mapper_for(self, markets: dict[str, MarketSpec]) -> SymbolMapper:
        if self._mapper is None or self._mapper_markets != set(markets):
            self._mapper = SymbolMapper(self.cfg.symbols, markets)
            self._mapper_markets = set(markets)
        return self._mapper

    # --- ciclo ---

    async def cycle(self, trigger: str = "rest", leader_time: datetime | None = None
                    ) -> CycleReport:
        async with self._lock:
            report = await self._cycle(trigger, leader_time)
            self.last_report = report
            log.info("ciclo (%s): %s %s", trigger, report.outcome.value, report.detail)
            return report

    async def _cycle(self, trigger: str, leader_time: datetime | None) -> CycleReport:
        stop = kill_switch_active(self._kill_dirs)
        if stop is not None:
            if self.state.halted:
                return CycleReport(Outcome.HALTED, self.state.halt_reason)
            return await self._halt(f"kill switch: existe {stop}")
        if self.state.halted:
            return CycleReport(Outcome.HALTED, self.state.halt_reason)

        try:
            return await self._trade(leader_time)
        except (CircuitBreakerTripped, limits.HardLimitViolation) as exc:
            return await self._halt(str(exc))
        except CYCLE_ERRORS as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if record_cycle_error(self.state, self.cfg.risk):
                return await self._halt(
                    f"{self.state.consecutive_errors} ciclos seguidos con error ({detail})"
                )
            self._save()
            await self._alert.alert(Level.WARNING, f"ciclo con error: {detail}")
            return CycleReport(Outcome.ERROR, detail)

    async def _trade(self, leader_time: datetime | None) -> CycleReport:
        cfg, state = self.cfg, self.state
        await self._executor.reconcile_pending()

        snap = await self._leader.leader_snapshot(cfg.leader_address)
        sanity = check_leader(snap, state.sanity, cfg.sanity,
                              stale_seconds=cfg.timing.leader_stale_seconds, now=self._now())
        state.sanity = sanity.state
        if sanity.verdict is Verdict.HALT:
            return await self._halt("controles del líder: " + "; ".join(sanity.reasons))
        if sanity.verdict is Verdict.SKIP:
            self._save()
            text = "; ".join(sanity.reasons)
            await self._alert.alert(Level.WARNING, f"ciclo saltado: {text}")
            return CycleReport(Outcome.SKIPPED, text)

        if not state.preexisting_initialized:
            state.preexisting = (initial_preexisting(snap.positions)
                                 if cfg.filters.ignore_preexisting else {})
            state.preexisting_initialized = True
            if state.preexisting:
                log.info("posiciones preexistentes del líder (no se copian): %s",
                         ", ".join(sorted(state.preexisting)))
        else:
            state.preexisting = update_preexisting(state.preexisting, snap.positions)
        eligible = eligible_positions(snap.positions, cfg.filters, state.preexisting)

        markets = await self._market.instruments()
        tickers = await self._market.tickers()
        mapper = self._mapper_for(markets)
        coin_for: dict[str, str] = {}
        for coin in eligible:
            sym = mapper.symbol_for(coin)
            if sym is not None:
                coin_for[sym] = coin
        managed = set(coin_for) | state.managed_symbols

        prices: dict[str, Decimal] = {}
        for sym in managed:
            t = tickers.get(sym)
            if t is None or t.suspended:
                raise KrakenDataError(f"{sym}: sin precio o mercado suspendido")
            prices[sym] = t.mark_price
        missing = managed - set(markets)
        if missing:
            raise KrakenDataError(f"mercados gestionados no disponibles: {sorted(missing)}")

        equity = await self._ex.equity_usd()
        all_positions = await self._ex.positions()
        current = {s: all_positions[s] for s in managed if all_positions.get(s)}

        dd = drawdown_tripped(state, equity, cfg.risk)
        if dd is not None:
            return await self._drawdown_stop(dd, current, managed, prices, markets)

        sized = compute_targets(
            leader_equity=snap.equity_usd,
            leader_positions={coin_for[s]: eligible[coin_for[s]] for s in coin_for},
            my_equity=equity, prices=prices, mapper=mapper, cfg=self._sizing_cfg,
        )
        actions = plan(targets=sized.targets, current=current, managed=managed,
                       prices=prices, markets=markets, cfg=cfg.planner)
        ctx = ExecutionContext(
            mode=self._ex.mode,
            leader_prices={s: snap.mids[c] for s, c in coin_for.items() if c in snap.mids},
            leader_time=leader_time,
        )
        results = await self._executor.execute(
            actions, markets=markets, positions=current, ctx=ctx)

        after = await self._ex.positions()
        state.managed_symbols = {s for s in managed if after.get(s) or s in sized.targets}
        await self._after_trading(snap.equity_usd)

        rejected = [r for r in results if r.status.value == "rejected"]
        if rejected:
            raise ExchangeError(
                "órdenes rechazadas: " + ", ".join(f"{r.cli_ord_id} ({r.reason})" for r in rejected)
            )
        record_cycle_ok(state)
        self._save()
        return CycleReport(Outcome.OK, f"{len(actions)} acciones", orders=len(results))

    async def _drawdown_stop(
        self, dd: Decimal, current: dict[str, Decimal], managed: set[str],
        prices: dict[str, Decimal], markets: dict[str, MarketSpec],
    ) -> CycleReport:
        reason = f"drawdown del {dd:.2f} % desde el máximo ({self.state.peak_equity_usd} USD)"
        if self.cfg.risk.close_all_on_drawdown and current:
            actions = plan(targets={}, current=current, managed=managed, prices=prices,
                           markets=markets, cfg=self.cfg.planner)
            try:
                await self._executor.execute(
                    actions, markets=markets, positions=current,
                    ctx=ExecutionContext(mode=self._ex.mode), emergency=True)
                reason += "; posiciones gestionadas cerradas"
            except CYCLE_ERRORS as exc:
                reason += f"; ERROR al cerrar posiciones ({type(exc).__name__}): revisa a mano"
        return await self._halt(reason)

    async def _after_trading(self, leader_equity: Decimal) -> None:
        now = self._now()
        for event in await self._ex.collect_funding(now):
            self._rec.funding(event, self._ex.mode)
        last = self.state.last_equity_record_at
        if last is None or now.timestamp() - last >= float(self.cfg.timing.equity_snapshot_seconds):
            self._rec.equity(now, self._ex.mode, await self._ex.equity_usd(), leader_equity)
            self.state.last_equity_record_at = now.timestamp()

    # --- bucle principal ---

    async def run_forever(self, stream_factory: Callable[..., Stream]) -> CycleReport | None:
        """WebSocket (con debounce) + ciclo REST de respaldo + heartbeat.

        Termina cuando el bot queda detenido. stream_factory recibe on_fills y
        on_connected y devuelve un objeto con run() y stop() (UserFillsStream).
        """
        timing = self.cfg.timing
        latest_fill: list[datetime] = []
        stopped = asyncio.Event()

        async def run_cycle(trigger: str, when: datetime | None = None) -> None:
            report = await self.cycle(trigger, when)
            if report.outcome is Outcome.HALTED:
                stopped.set()

        async def debounced() -> None:
            when = max(latest_fill) if latest_fill else None
            latest_fill.clear()
            await run_cycle("websocket", when)

        debouncer = Debouncer(float(timing.debounce_seconds), debounced)

        async def on_fills(fills: Sequence[LeaderFill]) -> None:
            latest_fill.extend(f.time for f in fills)
            debouncer.trigger()

        async def on_connected(reconnect: bool) -> None:
            if reconnect:  # los fills perdidos durante el corte no llegan: ciclo forzado
                await run_cycle("reconexión")

        stream = stream_factory(on_fills=on_fills, on_connected=on_connected)

        async def reconcile_loop() -> None:
            while not stopped.is_set():
                await run_cycle("rest")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stopped.wait(), float(timing.reconcile_interval_seconds))

        async def heartbeat() -> None:
            while not stopped.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stopped.wait(), float(timing.heartbeat_seconds))
                r = self.last_report
                log.info("heartbeat: último ciclo %s, gestionados %s, errores seguidos %d",
                         r.outcome.value if r else "-", sorted(self.state.managed_symbols),
                         self.state.consecutive_errors)

        tasks = [asyncio.create_task(stream.run()),
                 asyncio.create_task(reconcile_loop()),
                 asyncio.create_task(heartbeat())]
        try:
            await stopped.wait()
        finally:
            await stream.stop()
            await debouncer.aclose()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return self.last_report
