"""Línea de comandos del bot.

Modo por defecto: paper, sin .env ni claves. --live y --check llegan en la
fase 5; hasta entonces se rechazan.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

from copybot.alerts import Alerter, Level, LogAlerter, TelegramAlerter
from copybot.config import Config, ConfigError, Mode, load_config
from copybot.credentials import CredentialsError, load_telegram_credentials
from copybot.engine import CycleReport, Engine, Outcome
from copybot.exchange.kraken_public import KrakenMarketData
from copybot.exchange.paper import PaperAccount, PaperExchange
from copybot.logging_setup import setup_logging
from copybot.records import CsvRecorder
from copybot.risk import STOP_FILENAME, release_startup_profile, reset_halt
from copybot.sources.hyperliquid_rest import HyperliquidInfo
from copybot.sources.hyperliquid_ws import UserFillsStream
from copybot.state import AlreadyRunning, BotState, InstanceLock, StateError, StateStore

log = logging.getLogger("copybot")

RESET_PHRASE = "REANUDAR"
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
    g.add_argument("--check", action="store_true", help="verificaciones previas a live (fase 5)")
    p.add_argument("--live", action="store_true", help="modo live (fase 5)")
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


async def run_paper(cfg: Config, state: BotState, store: StateStore, once: bool) -> int:
    data_dir = cfg.paths.data_dir
    account = (PaperAccount.from_dict(state.paper) if state.paper
               else PaperAccount.new(cfg.paper))

    def sync_paper(st: BotState) -> None:
        st.paper = account.to_dict()

    store.before_save = sync_paper
    async with httpx.AsyncClient(timeout=15) as http:
        alerter: Alerter = LogAlerter()
        if cfg.telegram.enabled:
            alerter = TelegramAlerter(load_telegram_credentials(Path(".env")), http)
        market = KrakenMarketData(http)
        engine = Engine(
            cfg=cfg, state=state, store=store, leader=HyperliquidInfo(http), market=market,
            exchange=PaperExchange(account, market, cfg.paper),
            recorder=CsvRecorder(data_dir), alerter=alerter,
            kill_dirs=[Path.cwd(), data_dir],
        )
        await alerter.alert(Level.INFO, f"arranque en modo paper, líder {cfg.leader_address}")
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
    if report is None:
        return EXIT_ERROR
    return {Outcome.OK: EXIT_OK, Outcome.SKIPPED: EXIT_OK,
            Outcome.HALTED: EXIT_HALTED}.get(report.outcome, EXIT_ERROR)


def main(argv: Sequence[str] | None = None, prompt: Callable[[str], str] = input) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"error de configuración: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.live or args.check or cfg.mode is Mode.LIVE:
        print("El modo live y --check llegan en la fase 5. Ahora solo paper.", file=sys.stderr)
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
            log.info("arranque en modo paper (sin claves)")
            return asyncio.run(run_paper(cfg, state, store, args.once))
    except AlreadyRunning as exc:
        print(f"ya hay una instancia en marcha: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (StateError, CredentialsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
