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
from collections.abc import Awaitable, Callable, Sequence
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
from copybot.healthcheck import Healthcheck
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


class LoopTaskDied(RuntimeError):
    """Una de las tareas del bucle principal terminó sin que el bot estuviera detenido."""


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
        emergency_rounds: int = 5,
        emergency_pause_seconds: float = 2,
        emergency_retry_seconds: float = 15,
        healthcheck: Healthcheck | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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
        self._initial_sync = True  # hasta el primer ciclo sin órdenes aplazadas
        self._sync_cycles = 0
        self._emergency_rounds = emergency_rounds
        self._emergency_pause = emergency_pause_seconds
        self._emergency_retry_seconds = emergency_retry_seconds
        self._health = healthcheck
        self._stop_requested = False
        self._stopped: asyncio.Event | None = None  # el de run_forever, para request_stop()
        self._sleep = sleep
        self._mapper_markets: set[str] = set()
        # Último mercado y precio conocidos: permiten cerrar en emergencia aunque Kraken
        # deje de listar un mercado o falle la lectura pública
        self._known_markets: dict[str, MarketSpec] = {}
        self._last_prices: dict[str, Decimal] = {}
        self._lock = asyncio.Lock()
        self.last_report: CycleReport | None = None
        self.last_cycle_at: datetime | None = None

    # --- utilidades ---

    def _save(self) -> None:
        self._store.save(self.state)

    async def _best_effort(self, level: Level | None = None, text: str = "") -> None:
        """Guardar el estado y avisar al tratar un error: si esto falla (disco lleno, URL
        de Telegram rota) no puede convertirse en otro fallo del ciclo."""
        try:
            self._save()
        except Exception:
            log.exception("no se pudo guardar el estado")
        if level is not None:
            try:
                await self._alert.alert(level, text)
            except Exception:
                log.exception("no se pudo enviar la alerta")

    async def _halt(self, reason: str) -> CycleReport:
        halt(self.state, reason, self._now())
        await self._best_effort(Level.CRITICAL, f"bot detenido: {reason}")
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
            if self._stop_requested:  # parada ordenada en curso: no empieza ningún ciclo nuevo
                return CycleReport(Outcome.SKIPPED, "parada solicitada")
            try:
                report = await self._cycle(trigger, leader_time)
            except Exception as exc:  # el bucle principal no puede morir por un fallo del ciclo
                report = await self._last_resort(exc)
            self.last_report = report
            self.last_cycle_at = self._now()
            log.info("ciclo (%s): %s %s", trigger, report.outcome.value, report.detail)
            return report

    # --- parada ordenada (SIGTERM / SIGINT) ---

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    def request_stop(self) -> None:
        """Parada ordenada: termina el ciclo en curso (si lo hay), no empieza otro y sale.
        Las posiciones y los stops de catástrofe se quedan como están."""
        self._stop_requested = True
        if self._stopped is not None:
            self._stopped.set()

    # --- vigilancia externa ---

    async def _report_health(self) -> None:
        """Ping al healthcheck externo si el bot funciona; /fail y alerta si no.

        Sano = el último ciclo terminó bien (o saltado por cordura) y hace menos de
        3 intervalos de reconciliación: así también se detecta un ciclo colgado."""
        if self._health is None or self._stop_requested:
            return
        limit = 3 * float(self.cfg.timing.reconcile_interval_seconds) + 30
        report, at = self.last_report, self.last_cycle_at
        age = None if at is None else (self._now() - at).total_seconds()
        if self.state.halted:
            problem = f"bot detenido: {self.state.halt_reason}"
        elif age is not None and age > limit:
            problem = f"sin completar un ciclo desde hace {age:.0f} s (límite {limit:.0f} s)"
            await self._best_effort(Level.CRITICAL, f"el bot lleva más de {limit:.0f} s sin "
                                                    "completar un ciclo: ¿colgado?")
        elif report is not None and report.outcome not in (Outcome.OK, Outcome.SKIPPED):
            problem = f"último ciclo: {report.outcome.value} {report.detail}"
        else:
            problem = ""
        try:
            if problem:
                await self._health.fail(problem)
            else:
                await self._health.ok()
        except Exception:
            log.exception("healthcheck")

    async def _last_resort(self, exc: Exception) -> CycleReport:
        """Un fallo dentro del propio tratamiento de errores (guardar el estado, alertar):
        se cuenta como ciclo con error y, al llegar al límite, se detiene en memoria."""
        detail = f"{type(exc).__name__}: {exc}"
        log.exception("fallo no tratado en el ciclo")
        try:
            if record_cycle_error(self.state, self.cfg.risk):
                reason = f"{self.state.consecutive_errors} ciclos seguidos con error ({detail})"
                halt(self.state, reason, self._now())
                return CycleReport(Outcome.HALTED, reason)
        except Exception:
            log.exception("no se pudo registrar el error del ciclo")
        return CycleReport(Outcome.ERROR, detail)

    async def _cycle(self, trigger: str, leader_time: datetime | None) -> CycleReport:
        stop = kill_switch_active(self._kill_dirs)
        if stop is not None:
            return await self._kill_switch(stop)
        if self.state.halted:
            if self.state.emergency_close_pending:
                return await self._retry_emergency_close()
            return CycleReport(Outcome.HALTED, self.state.halt_reason)

        try:
            return await self._trade(leader_time)
        except (CircuitBreakerTripped, limits.HardLimitViolation) as exc:
            return await self._halt(str(exc))
        except Exception as exc:
            if not isinstance(exc, CYCLE_ERRORS):
                log.exception("excepción no prevista en el ciclo")
            detail = f"{type(exc).__name__}: {exc}"
            if record_cycle_error(self.state, self.cfg.risk):
                return await self._halt(
                    f"{self.state.consecutive_errors} ciclos seguidos con error ({detail})"
                )
            await self._best_effort(Level.WARNING, f"ciclo con error: {detail}")
            return CycleReport(Outcome.ERROR, detail)

    async def _trade(self, leader_time: datetime | None) -> CycleReport:
        cfg, state = self.cfg, self.state
        await self._executor.reconcile_pending()
        prepare_ledger = getattr(self._ex, "prepare_ledger", None)
        if prepare_ledger is not None:  # live: línea base del libro ANTES de la primera orden
            await prepare_ledger(self._now())

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
        self._known_markets.update(markets)
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
        self._last_prices.update(prices)
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
        try:
            execution = await self._executor.execute(
                actions, markets=markets, positions=current, ctx=ctx)
        except Exception:
            # El ciclo se aborta (breaker, límite duro, orden incierta, fallo inesperado):
            # lo que ya se abrió en este ciclo no puede quedarse sin stop.
            await self._protect_after_abort(markets)
            raise
        results = execution.results

        after = await self._ex.positions()
        state.managed_symbols = {s for s in managed if after.get(s) or s in sized.targets}
        # Los stops van PRIMERO: un fallo posterior (funding, libro, capital) no puede dejar
        # una posición recién abierta sin protección
        await self._sync_protective_stops(after, markets)
        await self._after_trading(snap.equity_usd)

        paced = await self._track_pacing(execution.deferred)
        if paced is not None:
            return paced

        rejected = [r for r in results if r.status.value == "rejected"]
        if rejected:
            raise ExchangeError(
                "órdenes rechazadas: " + ", ".join(f"{r.cli_ord_id} ({r.reason})" for r in rejected)
            )
        record_cycle_ok(state)
        self._save()
        detail = f"{len(actions)} acciones"
        if execution.deferred:
            detail += f", {execution.deferred} aplazadas por el límite de órdenes/min"
        return CycleReport(Outcome.OK, detail, orders=len(results))

    async def _protect_after_abort(self, markets: dict[str, MarketSpec]) -> None:
        """Mejor esfuerzo: guardar lo gestionado y colocar los stops de catástrofe."""
        try:
            after = await self._ex.positions()
            await self._sync_protective_stops(after, markets)
        except Exception:
            log.exception("no se pudieron proteger las posiciones tras abortar el ciclo")
        try:
            self._save()
        except Exception:
            log.exception("no se pudo guardar el estado tras abortar el ciclo")

    async def _track_pacing(self, deferred: int) -> CycleReport | None:
        """Aplazar órdenes es normal en la sincronización inicial (primer reparto
        tras arrancar el proceso); fuera de ella, N ciclos seguidos detienen el bot."""
        state, risk = self.state, self.cfg.risk
        if not deferred:
            self._initial_sync = False
            state.paced_streak = 0
            return None
        if self._initial_sync:
            self._sync_cycles += 1
            if self._sync_cycles > limits.INITIAL_SYNC_MAX_CYCLES:
                return await self._halt(
                    f"la sincronización inicial sigue aplazando órdenes tras "
                    f"{limits.INITIAL_SYNC_MAX_CYCLES} ciclos"
                )
            return None
        state.paced_streak += 1
        limit = min(risk.max_consecutive_paced_cycles, limits.HARD_MAX_PACED_CYCLES)
        if state.paced_streak > limit:
            return await self._halt(
                f"límite de órdenes por minuto alcanzado en {state.paced_streak} ciclos "
                "seguidos fuera de la sincronización inicial"
            )
        await self._alert.alert(
            Level.WARNING,
            f"límite de órdenes/min alcanzado ({state.paced_streak}/{limit} ciclos seguidos): "
            f"{deferred} órdenes aplazadas",
        )
        return None

    async def _drawdown_stop(
        self, dd: Decimal, current: dict[str, Decimal], managed: set[str],
        prices: dict[str, Decimal], markets: dict[str, MarketSpec],
    ) -> CycleReport:
        reason = f"drawdown del {dd:.2f} % desde el máximo ({self.state.peak_equity_usd} USD)"
        if self.cfg.risk.close_all_on_drawdown and current:
            message, complete = await self._close_all_managed()
            self.state.emergency_close_pending = not complete
            reason += "; " + message
        return await self._halt(reason)

    async def _kill_switch(self, stop: Path) -> CycleReport:
        state = self.state
        reason = f"kill switch: existe {stop}"
        closed_now = False
        if self.cfg.risk.close_all_on_kill_switch and not state.kill_switch_closed:
            message, complete = await self._close_all_managed()
            reason += "; " + message
            # Solo se da por cerrado si no queda nada abierto: si no, se reintenta en
            # cada ciclo mientras exista STOP (y el proceso sigue vivo para hacerlo)
            state.kill_switch_closed = complete
            state.emergency_close_pending = not complete
            closed_now = True
        if state.halted:
            await self._best_effort(Level.CRITICAL if closed_now else None, reason)
            return CycleReport(Outcome.HALTED, state.halt_reason)
        return await self._halt(reason)

    async def _retry_emergency_close(self) -> CycleReport:
        """Bot detenido con un cierre de emergencia sin terminar: se reintenta."""
        message, complete = await self._close_all_managed()
        self.state.emergency_close_pending = not complete
        await self._best_effort(
            Level.WARNING if complete else Level.CRITICAL,
            f"reintento del cierre de emergencia: {message}")
        return CycleReport(Outcome.HALTED, f"{self.state.halt_reason}; {message}")

    async def _public_or_known(self) -> tuple[dict[str, MarketSpec], dict[str, Decimal]]:
        """Mercados y precios mark para cerrar: los de ahora y, si fallan o faltan, los
        últimos conocidos (cerrar con un precio algo viejo es mejor que no cerrar)."""
        markets = dict(self._known_markets)
        prices = dict(self._last_prices)
        try:
            markets.update(await self._market.instruments())
        except Exception as exc:
            log.error("cierre de emergencia: sin instrumentos frescos (%s)", type(exc).__name__)
        try:
            prices.update({s: t.mark_price for s, t in (await self._market.tickers()).items()})
        except Exception as exc:
            log.error("cierre de emergencia: sin precios frescos (%s)", type(exc).__name__)
        return markets, prices

    async def _close_all_managed(self) -> tuple[str, bool]:
        """Cierra lo gestionado y deja los stops de catástrofe coherentes con lo que quede:
        sin posiciones no sobrevive ningún stop `cs-` (podría cerrar una posición manual
        futura en ese mercado)."""
        message, complete = await self._close_rounds()
        try:
            markets, _ = await self._public_or_known()
            await self._sync_protective_stops(await self._ex.positions(), markets)
        except Exception:
            log.exception("cierre de emergencia: no se pudieron actualizar los stops")
        return message, complete

    async def _close_rounds(self) -> tuple[str, bool]:
        """Cierre de emergencia: rondas de órdenes reduceOnly sin límite del circuit breaker.

        Cada símbolo se trata por separado: un mercado sin especificación, sin precio o
        con una orden incierta no impide cerrar los demás. Devuelve (mensaje, completo)."""
        last_error = ""
        for attempt in range(self._emergency_rounds):
            if attempt:
                await self._sleep(self._emergency_pause)
            try:
                positions = await self._ex.positions()
                current = {s: p for s, p in positions.items()
                           if p and s in self.state.managed_symbols}
                if not current:
                    self.state.managed_symbols = set()
                    return "posiciones gestionadas cerradas", True
                markets, prices = await self._public_or_known()
                for sym, size in sorted(current.items()):
                    try:
                        spec, price = markets.get(sym), prices.get(sym)
                        if spec is None or price is None:
                            raise KrakenDataError(f"{sym}: sin especificación o precio para cerrar")
                        actions = plan(targets={}, current={sym: size}, managed={sym},
                                       prices={sym: price}, markets={sym: spec},
                                       cfg=self.cfg.planner)
                        await self._executor.execute(
                            actions, markets=markets, positions={sym: size},
                            ctx=ExecutionContext(mode=self._ex.mode), emergency=True)
                    except Exception as exc:
                        last_error = f"{sym}: {type(exc).__name__}: {exc}"
                        log.error("cierre de emergencia, intento %d: %s", attempt + 1, last_error)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.error("cierre de emergencia, intento %d: %s", attempt + 1, last_error)
        try:
            left = {s: p for s, p in (await self._ex.positions()).items()
                    if p and s in self.state.managed_symbols}
        except Exception:
            left = {s: Decimal(0) for s in self.state.managed_symbols}
        if not left:
            self.state.managed_symbols = set()
            return "posiciones gestionadas cerradas", True
        self.state.managed_symbols = set(left)
        return (f"ERROR: siguen abiertas {sorted(left)} tras {self._emergency_rounds} "
                f"intentos ({last_error}); se reintenta mientras el bot siga en marcha; "
                "si no, CIÉRRALAS A MANO"), False

    async def _sync_protective_stops(
        self, positions: dict[str, Decimal], markets: dict[str, MarketSpec]
    ) -> None:
        """Stops de catástrofe en el exchange (solo live, si el exchange los soporta)."""
        sync = getattr(self._ex, "sync_catastrophe_stops", None)
        if sync is None or not self.cfg.risk.catastrophe_stop_enabled:
            return
        managed = {s: p for s, p in positions.items() if p and s in self.state.managed_symbols}
        for warning in await sync(managed, markets, self.cfg.risk.catastrophe_stop_pct):
            await self._alert.alert(Level.CRITICAL, warning)

    async def _after_trading(self, leader_equity: Decimal) -> None:
        now = self._now()
        for event in await self._ex.collect_funding(now):
            self._rec.funding(event, self._ex.mode)
        drain_ledger = getattr(self._ex, "drain_ledger", None)
        if drain_ledger is not None:
            fills, fees = drain_ledger()
            for f in fills:
                self._rec.kraken_fill(f)
            for fee in fees:
                self._rec.fee(fee)
        drain_alerts = getattr(self._ex, "drain_alerts", None)
        if drain_alerts is not None:
            for text in drain_alerts():
                await self._alert.alert(Level.CRITICAL, text)
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
        stopped = self._stopped = asyncio.Event()
        if self._stop_requested:
            stopped.set()

        async def run_cycle(trigger: str, when: datetime | None = None) -> None:
            report = await self.cycle(trigger, when)
            if report.outcome is Outcome.HALTED and not self.state.emergency_close_pending:
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
                wait = float(timing.reconcile_interval_seconds)
                if self.state.emergency_close_pending:  # cierre sin terminar: reintentar pronto
                    wait = min(wait, self._emergency_retry_seconds)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stopped.wait(), wait)

        async def heartbeat() -> None:
            while not stopped.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stopped.wait(), float(timing.heartbeat_seconds))
                r = self.last_report
                log.info("heartbeat: último ciclo %s, gestionados %s, errores seguidos %d",
                         r.outcome.value if r else "-", sorted(self.state.managed_symbols),
                         self.state.consecutive_errors)
                await self._report_health()

        tasks = [asyncio.create_task(stream.run(), name="websocket"),
                 asyncio.create_task(reconcile_loop(), name="reconciliación"),
                 asyncio.create_task(heartbeat(), name="heartbeat")]
        stop_wait = asyncio.create_task(stopped.wait())
        try:
            # Ninguna de las tres tareas debe terminar sola: si una muere (un fallo
            # que el ciclo no cubrió), el proceso sale con error en vez de quedarse
            # "vivo" sin operar.
            done, _ = await asyncio.wait({stop_wait, *tasks}, return_when=asyncio.FIRST_COMPLETED)
            dead = [t for t in done if t is not stop_wait]
            if dead and not stopped.is_set():
                for t in dead:
                    if not t.cancelled() and t.exception() is not None:
                        log.error("la tarea %s murió", t.get_name(), exc_info=t.exception())
                names = ", ".join(t.get_name() for t in dead)
                raise LoopTaskDied(f"terminó la tarea {names} sin que el bot estuviera detenido")
        finally:
            if self._stop_requested:
                # Parada ordenada: se deja terminar el ciclo en curso antes de cancelar nada
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._lock.acquire(), 25)
                    self._lock.release()
            await stream.stop()
            await debouncer.aclose()
            for t in (stop_wait, *tasks):
                t.cancel()
            await asyncio.gather(stop_wait, *tasks, return_exceptions=True)
        return self.last_report
