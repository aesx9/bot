"""Línea de comandos del bot.

Modo por defecto: paper, sin .env ni claves.

Live exige TODO a la vez:
1. mode = "live" en config.toml;
2. el flag --live;
3. un --check superado con ESTA config y ESTA clave;
4. leer el resumen de topes y escribir la frase de confirmación.
La primera vez que se arranca en live se activa el perfil de arranque
(1x, 100 USD por activo), que solo se quita con --release-startup-profile.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from copybot import limits
from copybot.alerts import Alerter, Level, LogAlerter, TelegramAlerter
from copybot.checks import (
    CheckReport,
    confirmation_record,
    first_live_start,
    key_problem,
    live_check_valid,
    live_confirmation_valid,
    run_check,
)
from copybot.config import Config, ConfigError, Mode, load_config
from copybot.credentials import (
    CredentialsError,
    KrakenCredentials,
    load_healthcheck_url,
    load_kraken_credentials,
    load_telegram_credentials,
)
from copybot.engine import (
    CycleReport,
    Engine,
    LedgerUpdate,
    LoopTaskDied,
    Outcome,
    protective_fills_note,
    record_protective_note,
    update_ledger,
)
from copybot.exchange.base import Exchange, ExchangeError
from copybot.exchange.kraken_auth import KrakenPrivateClient
from copybot.exchange.kraken_public import KrakenMarketData
from copybot.exchange.live import LiveExchange
from copybot.exchange.paper import PaperAccount, PaperExchange
from copybot.healthcheck import Healthcheck, HttpHealthcheck
from copybot.logging_setup import setup_logging
from copybot.records import CsvRecorder
from copybot.risk import (
    STOP_FILENAME,
    activate_startup_profile_on_first_live,
    catastrophe_stop_pct,
    effective_sizing,
    halt,
    release_startup_profile,
    reset_halt,
)
from copybot.sources.hyperliquid_rest import HyperliquidInfo
from copybot.sources.hyperliquid_ws import UserFillsStream
from copybot.state import AlreadyRunning, BotState, InstanceLock, StateError, StateStore

log = logging.getLogger("copybot")

RESET_PHRASE = "REANUDAR"
REVIEWED_PHRASE = "HE REVISADO LOS FILLS"
LIVE_PHRASE = "OPERAR CON DINERO REAL"
RELEASE_PHRASE = "QUITAR PERFIL DE ARRANQUE"

EXIT_OK, EXIT_ERROR, EXIT_HALTED, EXIT_USAGE = 0, 1, 3, 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="copybot", description="Copy bot Hyperliquid -> Kraken")
    p.add_argument("--config", type=Path, default=Path("config.toml"))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="un solo ciclo y salir")
    g.add_argument("--status", action="store_true", help="mostrar el estado guardado")
    g.add_argument("--reset-halt", action="store_true", help="quitar una parada tras revisarla")
    g.add_argument("--release-startup-profile", action="store_true",
                   help="quitar el perfil de arranque live (1x, 100 USD/activo)")
    g.add_argument("--check", action="store_true", help="verificaciones previas a live")
    g.add_argument("--sync-ledger", action="store_true",
                   help="live: traer al libro fiscal fills, comisiones y funding pendientes "
                        "(solo lecturas en Kraken; no envía órdenes)")
    p.add_argument("--live", action="store_true", help="operar con dinero real (ver README)")
    p.add_argument("--env", type=Path, default=Path(".env"), help="fichero de claves (live)")
    return p


def confirm(phrase: str, prompt: Callable[[str], str] = input) -> bool:
    try:
        return prompt(f'Escribe "{phrase}" para confirmar: ').strip() == phrase
    except EOFError:
        return False


def status_text(state: BotState, cfg: Config, running: bool | None = None) -> str:
    info: dict[str, Any] = {
        "modo": cfg.mode.value,
        "instancia_en_marcha": running,
        "detenido": state.halted,
        "motivo": state.halt_reason or None,
        "detenido_en": state.halted_at,
        "pico_capital_usd": None if state.peak_equity_usd is None else str(state.peak_equity_usd),
        "errores_seguidos": state.consecutive_errors,
        "fallos_sanity_seguidos": state.sanity.consecutive_failures,
        "simbolos_gestionados": sorted(state.managed_symbols),
        "preexistentes_lider": sorted(state.preexisting),
        "ordenes_pendientes": sorted(state.pending_orders),
        "fills_protectores_sin_revisar": state.protective_fills_unreviewed,
        "perfil_arranque_live": state.live_startup_profile,
    }
    if state.paper:
        info["paper"] = {k: state.paper[k] for k in ("eur_collateral", "usd_balance",
                                                       "positions")}
    return json.dumps(info, indent=2, ensure_ascii=False)


def live_summary(cfg: Config, startup_profile: bool) -> str:
    sz = effective_sizing(cfg.sizing, startup_profile=startup_profile)
    r = cfg.risk
    lines = [
        "=== ARRANQUE EN LIVE: DINERO REAL ===",
        f"Líder: {cfg.leader_address}",
        f"Perfil de arranque: {'ACTIVO (1x, 100 USD/activo)' if startup_profile else 'quitado'}",
        f"Sizing: modo {sz.mode.value}; máx. {sz.max_asset_usd} USD y "
        f"{sz.max_asset_pct_equity} % del capital por activo; apalancamiento total "
        f"{sz.max_total_leverage}x",
        f"Topes absolutos: {limits.HARD_MAX_LEVERAGE}x, "
        f"{limits.HARD_MAX_NOTIONAL_PER_ASSET_USD} USD/activo, "
        f"{limits.HARD_MAX_NOTIONAL_TOTAL_USD} USD total",
        f"Órdenes IOC con tope de slippage {cfg.execution.slippage_cap_pct} %",
        f"Circuit breaker: {r.max_orders_per_minute} órdenes/min (se aplazan), "
        f"{r.max_notional_per_hour_usd} USD/h (detiene)",
        f"Drawdown: {r.max_drawdown_pct} % (cerrar todo: {r.close_all_on_drawdown}); "
        f"kill switch: fichero {STOP_FILENAME} (cerrar todo: {r.close_all_on_kill_switch})",
        f"Stop de catástrofe: {'sí' if r.catastrophe_stop_enabled else 'NO'}, al "
        f"{catastrophe_stop_pct(r, sz.max_total_leverage):.2f} % de la entrada "
        f"(drawdown {r.max_drawdown_pct} % / apalancamiento {sz.max_total_leverage}x)",
    ]
    return "\n".join(lines)


@contextlib.contextmanager
def stop_on_signals(engine: Engine) -> Iterator[None]:
    """SIGTERM (systemctl stop) y SIGINT (Ctrl+C) piden una parada ORDENADA: termina el ciclo
    en curso, guarda el estado y sale con código 0. Sin esto, una señal mataba el proceso a
    mitad de un ciclo."""
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    for sig in signals:
        loop.add_signal_handler(sig, engine.request_stop)
    try:
        yield
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)


async def run_bot(
    cfg: Config, state: BotState, store: StateStore, once: bool, env_path: Path,
    live_creds: KrakenCredentials | None = None,
) -> int:
    data_dir = cfg.run_dir
    async with httpx.AsyncClient(timeout=15) as http:
        alerter: Alerter = LogAlerter()
        if cfg.telegram.enabled:
            alerter = TelegramAlerter(load_telegram_credentials(env_path), http)
        market = KrakenMarketData(http)
        exchange: Exchange
        if live_creds is None:
            account = (PaperAccount.from_dict(state.paper) if state.paper
                       else PaperAccount.new(cfg.paper))

            def sync_paper(st: BotState) -> None:
                st.paper = account.to_dict()

            store.before_save = sync_paper
            exchange = PaperExchange(account, market, cfg.paper)
        else:
            client = KrakenPrivateClient(http, live_creds)
            try:
                problem = await key_problem(client)
            except ExchangeError as exc:
                log.error("no se pudo verificar la clave API al arrancar (%s): no se opera", exc)
                return EXIT_ERROR  # transitorio: systemd lo reintenta
            if problem:
                # Los permisos pueden haber cambiado desde el --check: se revalidan siempre
                state.live_confirmation = None
                state.live_check = None
                store.save(state)
                log.critical("la clave de Kraken ya no es válida para operar: %s", problem)
                await alerter.alert(Level.CRITICAL, f"live NO arranca: {problem}")
                return EXIT_USAGE
            exchange = LiveExchange(client, state)
        health: Healthcheck | None = None
        if cfg.healthcheck.enabled:
            health = HttpHealthcheck(load_healthcheck_url(env_path), http)
        engine = Engine(
            cfg=cfg, state=state, store=store, leader=HyperliquidInfo(http), market=market,
            exchange=exchange, recorder=CsvRecorder(data_dir), alerter=alerter,
            healthcheck=health, kill_dirs=[Path.cwd(), data_dir],
            startup_profile=live_creds is not None and state.live_startup_profile is True,
        )
        await alerter.alert(Level.INFO,
                            f"arranque en modo {exchange.mode}, líder {cfg.leader_address}")
        report: CycleReport | None
        if once:
            report = await engine.cycle("manual")
        else:
            timing = cfg.timing

            def stream_factory(**callbacks: object) -> UserFillsStream:
                return UserFillsStream(
                    cfg.leader_address,
                    backoff_initial_seconds=float(timing.ws_backoff_initial_seconds),
                    backoff_max_seconds=float(timing.ws_backoff_max_seconds),
                    **callbacks,  # type: ignore[arg-type]
                )

            try:
                with stop_on_signals(engine):
                    report = await engine.run_forever(stream_factory)
            except LoopTaskDied as exc:
                # Salida con error: systemd reinicia el proceso (la confirmación live sigue
                # vigente) en vez de dejarlo "vivo" sin operar.
                log.critical("bucle principal roto: %s", exc)
                try:
                    store.save(state)
                    await alerter.alert(Level.CRITICAL, f"bucle principal roto, se reinicia: {exc}")
                    if health is not None:
                        await health.fail(f"bucle principal roto: {exc}")
                except Exception:
                    log.exception("no se pudo guardar el estado o avisar tras romperse el bucle")
                return EXIT_ERROR
        store.save(state)
        if engine.stop_requested:
            log.warning("parada ordenada por señal: posiciones y stops quedan como están")
            await alerter.alert(Level.WARNING, "bot parado por señal (SIGTERM/SIGINT); las "
                                "posiciones y los stops de catástrofe siguen abiertos")
            return EXIT_OK
        if report is not None and report.outcome is Outcome.HALTED:
            await alerter.alert(Level.CRITICAL, f"bot parado: {state.halt_reason}")
            if health is not None:
                await health.fail(f"bot parado: {state.halt_reason}")
    if report is None:
        return EXIT_ERROR
    return {Outcome.OK: EXIT_OK, Outcome.SKIPPED: EXIT_OK,
            Outcome.HALTED: EXIT_HALTED}.get(report.outcome, EXIT_ERROR)


async def run_check_command(cfg: Config, state: BotState, creds: KrakenCredentials,
                            prompt: Callable[[str], str]) -> CheckReport:
    async with httpx.AsyncClient(timeout=15) as http:
        return await run_check(
            cfg=cfg, creds=creds, client=KrakenPrivateClient(http, creds), state=state,
            leader=HyperliquidInfo(http), market=KrakenMarketData(http), prompt=prompt,
        )


async def sync_ledger_command(cfg: Config, state: BotState,
                              creds: KrakenCredentials) -> LedgerUpdate:
    """--sync-ledger: solo lecturas en Kraken (fills, log de cuenta, posiciones)."""
    async with httpx.AsyncClient(timeout=15) as http:
        exchange = LiveExchange(KrakenPrivateClient(http, creds), state)
        return await update_ledger(exchange, CsvRecorder(cfg.run_dir), datetime.now(UTC))


async def live_open_positions(creds: KrakenCredentials) -> dict[str, Decimal]:
    """Posiciones abiertas hoy en la cuenta de Kraken Futures (solo lectura)."""
    async with httpx.AsyncClient(timeout=15) as http:
        return await LiveExchange(KrakenPrivateClient(http, creds), BotState()).positions()


def main(argv: Sequence[str] | None = None, prompt: Callable[[str], str] = input) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"error de configuración: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.live and cfg.mode is not Mode.LIVE:
        print('--live exige mode = "live" en la configuración.', file=sys.stderr)
        return EXIT_USAGE
    if cfg.mode is Mode.LIVE and not (args.live or args.check or args.status or args.reset_halt
                                      or args.release_startup_profile or args.sync_ledger):
        print('La configuración está en modo live: arranca con --live (o vuelve a "paper").',
              file=sys.stderr)
        return EXIT_USAGE
    if args.check and cfg.mode is not Mode.LIVE:
        print('--check es para live: pon mode = "live" en la configuración.', file=sys.stderr)
        return EXIT_USAGE
    if args.sync_ledger and cfg.mode is not Mode.LIVE:
        print('--sync-ledger es para live: el libro fiscal solo existe en live.', file=sys.stderr)
        return EXIT_USAGE

    legacy_state = cfg.paths.data_dir / "state.json"
    if legacy_state.exists():
        print(f"Hay un estado del diseño anterior en {legacy_state}. Ahora cada modo usa su "
              f"directorio ({cfg.paths.data_dir}/paper y {cfg.paths.data_dir}/live): mueve ese "
              "estado y sus CSV al directorio del modo que corresponda, revisando su modo "
              "(paper o live), o bórralo si no hace falta.", file=sys.stderr)
        return EXIT_USAGE
    data_dir = cfg.run_dir
    store = StateStore(data_dir / "state.json")
    if args.status:
        # Solo lectura: funciona con el servicio en marcha (state.json se escribe de forma
        # atómica, así que nunca se lee a medias) y no crea ficheros ni toma el bloqueo.
        try:
            state = store.load()
            state.bind_mode(cfg.mode.value)
        except StateError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(status_text(state, cfg, running=InstanceLock.is_held(data_dir / "copybot.lock")))
        return EXIT_OK
    setup_logging(data_dir / "logs", external_rotation=cfg.logging.external_rotation)
    try:
        with InstanceLock(data_dir / "copybot.lock"):
            state = store.load()
            state.bind_mode(cfg.mode.value)
            if args.reset_halt:
                unreviewed = list(state.protective_fills_unreviewed)
                if not state.halted and not unreviewed:
                    print("El bot no está detenido.")
                    return EXIT_OK
                print(f"Motivo de la parada: {state.halt_reason or '-'}")
                if unreviewed:
                    print("FILLS PROTECTORES SIN REVISAR (stop de catástrofe, liquidación, "
                          "desapalancamiento o fills ajenos en símbolos gestionados):")
                    for note in unreviewed:
                        print(f"  - {note}")
                    print("Al reanudar, el bot volverá a copiar al líder y puede reabrir esas "
                          "posiciones. Revisa la cuenta en Kraken antes de seguir.")
                    if not confirm(REVIEWED_PHRASE, prompt):
                        print("Cancelado.")
                        return EXIT_USAGE
                print(f"Recuerda borrar el fichero {STOP_FILENAME} si existe.")
                if not confirm(RESET_PHRASE, prompt):
                    print("Cancelado.")
                    return EXIT_USAGE
                reset_halt(state)
                state.protective_fills_unreviewed = []
                store.save(state)
                print("Parada quitada. La referencia de los controles del líder se reinicia.")
                return EXIT_OK
            if args.release_startup_profile:
                print("El perfil de arranque limita live a 1x y 100 USD por activo.")
                if not confirm(RELEASE_PHRASE, prompt):
                    print("Cancelado.")
                    return EXIT_USAGE
                release_startup_profile(state)
                store.save(state)
                print("Perfil de arranque quitado: rigen los topes normales.")
                return EXIT_OK
            if args.sync_ledger:
                if state.live_funding_cursor_ms is None:
                    # Fijar aquí la línea base desactivaría el control de posiciones ajenas
                    # del primer arranque live (M10) y no habría nada del bot que importar
                    print("El bot nunca ha operado en live: no hay libro que sincronizar.",
                          file=sys.stderr)
                    return EXIT_USAGE
                creds = load_kraken_credentials(args.env)
                update = asyncio.run(sync_ledger_command(cfg, state, creds))
                # Un fill protector importado aquí ya no lo verá el bot al reanudar: se guarda
                # para que --reset-halt lo muestre y, como haría el bot, se detiene
                note = protective_fills_note(update.fills, state.managed_symbols)
                if note:
                    record_protective_note(state, note)
                    halt(state, f"fills protectores importados con --sync-ledger: {note}")
                store.save(state)
                print(f"Libro actualizado: {len(update.fills)} fills, {update.fees} comisiones, "
                      f"{update.funding} pagos de funding.")
                for text in update.alerts:
                    print(f"AVISO: {text}")
                if note:
                    print(f"ATENCIÓN: actuó la protección del exchange o hubo fills ajenos en "
                          f"símbolos gestionados: {note}. El bot queda detenido; revisa la "
                          "cuenta en Kraken y después --reset-halt.")
                return EXIT_OK
            if args.check:
                creds = load_kraken_credentials(args.env)
                report = asyncio.run(run_check_command(cfg, state, creds, prompt))
                store.save(state)
                print(report.text())
                return EXIT_OK if report.passed else EXIT_ERROR
            if args.live:
                creds = load_kraken_credentials(args.env)
                problem = live_check_valid(state, cfg, creds)
                if problem:
                    print(f"No se puede arrancar en live: {problem}. Ejecuta --check.",
                          file=sys.stderr)
                    return EXIT_USAGE
                if first_live_start(state):
                    try:
                        open_positions = asyncio.run(live_open_positions(creds))
                    except ExchangeError as exc:
                        print(f"No se pudo comprobar las posiciones de la cuenta: {exc}",
                              file=sys.stderr)
                        return EXIT_ERROR
                    if open_positions:
                        print("No se puede arrancar en live por primera vez con posiciones "
                              f"abiertas en la cuenta ({', '.join(sorted(open_positions))}): el "
                              "bot las tomaría como suyas y podría cerrarlas. Ciérralas antes "
                              "(o usa una cuenta de Kraken solo para el bot).", file=sys.stderr)
                        return EXIT_USAGE
                first_profile = state.live_startup_profile is not False
                print(live_summary(cfg, startup_profile=first_profile))
                needs = live_confirmation_valid(state, cfg, creds)
                if needs is None:
                    confirmed_at = (state.live_confirmation or {}).get("confirmed_at")
                    print(f"Confirmación vigente desde {confirmed_at}: config, clave y "
                          "código sin cambios y sin paradas desde entonces.")
                else:
                    print(f"Hace falta confirmar: {needs}.")
                    if not confirm(LIVE_PHRASE, prompt):
                        print("Cancelado: no se opera.")
                        return EXIT_USAGE
                    state.live_confirmation = confirmation_record(cfg, creds, datetime.now(UTC))
                activate_startup_profile_on_first_live(state)
                store.save(state)
                log.warning("ARRANQUE EN LIVE (%s)",
                            "confirmación vigente" if needs is None else "confirmado ahora")
                return asyncio.run(run_bot(cfg, state, store, args.once, args.env, creds))
            log.info("arranque en modo paper (sin claves)")
            return asyncio.run(run_bot(cfg, state, store, args.once, args.env))
    except AlreadyRunning as exc:
        print(f"ya hay una instancia en marcha: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (StateError, CredentialsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
