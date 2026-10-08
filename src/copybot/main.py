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
import json
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from copybot import limits
from copybot.alerts import Alerter, Level, LogAlerter, TelegramAlerter
from copybot.checks import (
    CheckReport,
    confirmation_record,
    live_check_valid,
    live_confirmation_valid,
    run_check,
)
from copybot.config import Config, ConfigError, Mode, load_config
from copybot.credentials import (
    CredentialsError,
    KrakenCredentials,
    load_kraken_credentials,
    load_telegram_credentials,
)
from copybot.engine import CycleReport, Engine, Outcome
from copybot.exchange.base import Exchange
from copybot.exchange.kraken_auth import KrakenPrivateClient
from copybot.exchange.kraken_public import KrakenMarketData
from copybot.exchange.live import LiveExchange
from copybot.exchange.paper import PaperAccount, PaperExchange
from copybot.logging_setup import setup_logging
from copybot.records import CsvRecorder
from copybot.risk import (
    STOP_FILENAME,
    activate_startup_profile_on_first_live,
    effective_sizing,
    release_startup_profile,
    reset_halt,
)
from copybot.sources.hyperliquid_rest import HyperliquidInfo
from copybot.sources.hyperliquid_ws import UserFillsStream
from copybot.state import AlreadyRunning, BotState, InstanceLock, StateError, StateStore

log = logging.getLogger("copybot")

RESET_PHRASE = "REANUDAR"
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
    p.add_argument("--live", action="store_true", help="operar con dinero real (ver README)")
    p.add_argument("--env", type=Path, default=Path(".env"), help="fichero de claves (live)")
    return p


def confirm(phrase: str, prompt: Callable[[str], str] = input) -> bool:
    try:
        return prompt(f'Escribe "{phrase}" para confirmar: ').strip() == phrase
    except EOFError:
        return False


def status_text(state: BotState, cfg: Config) -> str:
    info: dict[str, Any] = {
        "modo": cfg.mode.value,
        "detenido": state.halted,
        "motivo": state.halt_reason or None,
        "detenido_en": state.halted_at,
        "pico_capital_usd": None if state.peak_equity_usd is None else str(state.peak_equity_usd),
        "errores_seguidos": state.consecutive_errors,
        "fallos_sanity_seguidos": state.sanity.consecutive_failures,
        "simbolos_gestionados": sorted(state.managed_symbols),
        "preexistentes_lider": sorted(state.preexisting),
        "ordenes_pendientes": sorted(state.pending_orders),
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
        f"Stop de catástrofe: {'sí' if r.catastrophe_stop_enabled else 'NO'}, "
        f"al {r.catastrophe_stop_pct} % de la entrada",
    ]
    return "\n".join(lines)


async def run_bot(
    cfg: Config, state: BotState, store: StateStore, once: bool, env_path: Path,
    live_creds: KrakenCredentials | None = None,
) -> int:
    data_dir = cfg.paths.data_dir
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
            exchange = LiveExchange(KrakenPrivateClient(http, live_creds), state)
        engine = Engine(
            cfg=cfg, state=state, store=store, leader=HyperliquidInfo(http), market=market,
            exchange=exchange, recorder=CsvRecorder(data_dir), alerter=alerter,
            kill_dirs=[Path.cwd(), data_dir],
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

            report = await engine.run_forever(stream_factory)
        store.save(state)
        if report is not None and report.outcome is Outcome.HALTED:
            await alerter.alert(Level.CRITICAL, f"bot parado: {state.halt_reason}")
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
    if cfg.mode is Mode.LIVE and not (args.live or args.check or args.status
                                      or args.reset_halt or args.release_startup_profile):
        print('La configuración está en modo live: arranca con --live (o vuelve a "paper").',
              file=sys.stderr)
        return EXIT_USAGE
    if args.check and cfg.mode is not Mode.LIVE:
        print('--check es para live: pon mode = "live" en la configuración.', file=sys.stderr)
        return EXIT_USAGE

    data_dir = cfg.paths.data_dir
    setup_logging(data_dir / "logs")
    store = StateStore(data_dir / "state.json")
    try:
        with InstanceLock(data_dir / "copybot.lock"):
            state = store.load()
            if args.status:
                print(status_text(state, cfg))
                return EXIT_OK
            if args.reset_halt:
                if not state.halted:
                    print("El bot no está detenido.")
                    return EXIT_OK
                print(f"Motivo de la parada: {state.halt_reason}")
                print(f"Recuerda borrar el fichero {STOP_FILENAME} si existe.")
                if not confirm(RESET_PHRASE, prompt):
                    print("Cancelado.")
                    return EXIT_USAGE
                reset_halt(state)
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
