"""--check: verificaciones previas a live. No envía órdenes (solo lecturas).

Aborta si:
- la config no está en modo live o no carga;
- no hay conectividad con Kraken (público y privado) o con Hyperliquid;
- el líder usa un modo de cuenta no soportado;
- la clave NO tiene lectura y trading (permissions.general = FULL_ACCESS);
- la clave tiene CUALQUIER acceso de transferencia/retiro
  (permissions.transfer distinto de NO_ACCESS);
- no se puede verificar el permiso de retiro y además la clave no tiene
  restricción de IP activa o no se confirma por escrito;
- el capital de la cuenta no es positivo o un override apunta a un
  mercado inexistente.

Verificado con la documentación oficial (GET /api/auth/v1/api-keys/v3/check):
permissions = {general, transfer} con valores NO_ACCESS | READ_ONLY |
FULL_ACCESS (lista exhaustiva), y allowedCidrBlocks con las IP permitidas.

Si todo pasa, se guarda en el estado el hash de la config y una huella de la
clave: si cambia cualquiera de las dos, hay que repetir --check.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from copybot import limits
from copybot.config import Config, Mode
from copybot.credentials import KrakenCredentials
from copybot.engine import LeaderSource, MarketSource
from copybot.exchange.base import ExchangeError
from copybot.exchange.kraken_auth import KrakenPrivateClient
from copybot.exchange.kraken_public import KrakenDataError
from copybot.exchange.live import LiveExchange
from copybot.sources.hyperliquid_rest import LeaderDataError
from copybot.state import BotState
from copybot.symbols import SymbolMapper

KEY_CHECK_PATH = "/api/auth/v1/api-keys/v3/check"
ACCESS_LEVELS = frozenset({"NO_ACCESS", "READ_ONLY", "FULL_ACCESS"})
WITHDRAW_CONFIRM_PHRASE = "CONFIRMO QUE LA CLAVE NO PUEDE RETIRAR NI TRANSFERIR FONDOS"


@dataclass
class CheckItem:
    name: str
    ok: bool
    detail: str
    fatal: bool = True


@dataclass
class CheckReport:
    items: list[CheckItem] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, fatal: bool = True) -> None:
        self.items.append(CheckItem(name, ok, detail, fatal))

    @property
    def passed(self) -> bool:
        return all(i.ok for i in self.items if i.fatal)

    def text(self) -> str:
        lines = []
        for i in self.items:
            mark = "OK " if i.ok else ("FALLO" if i.fatal else "AVISO")
            lines.append(f"[{mark}] {i.name}: {i.detail}")
        lines.append("RESULTADO: " + ("superado" if self.passed else "NO superado"))
        return "\n".join(lines)


def config_hash(cfg: Config) -> str:
    return hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()


def key_fingerprint(creds: KrakenCredentials) -> str:
    """Huella no reversible de la clave pública (para detectar cambios de clave)."""
    return hashlib.sha256(creds.api_key.get_secret_value().encode()).hexdigest()[:16]


def evaluate_key(payload: Any) -> tuple[str, str, list[str]]:
    """('ok' | 'forbidden' | 'unverifiable', detalle, bloques de IP permitidos)."""
    cidrs: list[str] = []
    if isinstance(payload, dict):
        cidrs = [str(c) for c in (payload.get("allowedCidrBlocks") or []) if c]
        single = payload.get("allowedCidrBlock")
        if single and str(single) not in cidrs:
            cidrs.append(str(single))
    perms = payload.get("permissions") if isinstance(payload, dict) else None
    general = perms.get("general") if isinstance(perms, dict) else None
    transfer = perms.get("transfer") if isinstance(perms, dict) else None
    if general not in ACCESS_LEVELS or transfer not in ACCESS_LEVELS:
        return "unverifiable", "la respuesta no permite saber los permisos de la clave", cidrs
    if transfer != "NO_ACCESS":
        return "forbidden", f"la clave tiene acceso de transferencia/retiro ({transfer})", cidrs
    if general != "FULL_ACCESS":
        return ("forbidden",
                f"la clave necesita lectura y trading (general = {general}, se exige FULL_ACCESS)",
                cidrs)
    return "ok", "lectura y trading; sin transferencias ni retiros", cidrs


async def run_check(
    *,
    cfg: Config,
    creds: KrakenCredentials,
    client: KrakenPrivateClient,
    state: BotState,
    leader: LeaderSource,
    market: MarketSource,
    prompt: Callable[[str], str],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CheckReport:
    r = CheckReport()
    r.add("modo", cfg.mode is Mode.LIVE, f"config en modo {cfg.mode.value}")

    # Kraken público
    try:
        markets = await market.instruments()
        tickers = await market.tickers()
        r.add("kraken público", True, f"{len(markets)} perpetuos PF_ operables")
    except KrakenDataError as exc:
        r.add("kraken público", False, str(exc))
        return r
    bad = [f"{c}->{s}" for c, s in cfg.symbols.overrides.items() if s not in markets]
    r.add("overrides de símbolos", not bad,
          "todos existen" if not bad else f"mercados inexistentes: {', '.join(bad)}")

    # Hyperliquid
    try:
        snap = await leader.leader_snapshot(cfg.leader_address)
        mapper = SymbolMapper(cfg.symbols, markets)
        mapped = [c for c in snap.positions if mapper.symbol_for(c)]
        r.add("líder", True, f"capital {snap.equity_usd} USD, {len(snap.positions)} posiciones "
              f"({len(mapped)} con mercado en Kraken)")
    except LeaderDataError as exc:
        r.add("líder", False, str(exc))

    # Clave: permisos
    try:
        key_payload = await client.request("GET", KEY_CHECK_PATH)
    except ExchangeError as exc:
        r.add("clave API", False, f"no se pudo consultar ({exc})")
        return r
    verdict, detail, cidrs = evaluate_key(key_payload)
    if verdict == "ok":
        r.add("permisos de la clave", True, detail)
        r.add("restricción de IP", bool(cidrs),
              f"IP permitidas: {', '.join(cidrs)}" if cidrs
              else "sin restricción de IP: actívala en Kraken (recomendado)", fatal=False)
    elif verdict == "forbidden":
        r.add("permisos de la clave", False, detail + ": crea otra clave sin ese permiso")
    else:
        if not cidrs:
            r.add("permisos de la clave", False,
                  detail + "; sin restricción de IP activa no se puede continuar")
        else:
            answer = prompt(
                f"{detail}.\nLa clave solo funciona desde: {', '.join(cidrs)}.\n"
                f'Revisa en Kraken que NO tiene permiso de retiro ni transferencia y escribe\n'
                f'"{WITHDRAW_CONFIRM_PHRASE}": '
            )
            confirmed = answer.strip() == WITHDRAW_CONFIRM_PHRASE
            r.add("permisos de la clave", confirmed,
                  "no verificable; restricción de IP activa y confirmación escrita"
                  if confirmed else "no verificable y sin confirmación escrita")

    # Cuenta
    live = LiveExchange(client, state)
    try:
        equity = await live.equity_usd()
        positions = await live.positions()
        r.add("cuenta", equity > 0, f"capital (marginEquity) {equity} USD")
        others = sorted(set(positions) - state.managed_symbols)
        if others:
            r.add("posiciones no gestionadas", True,
                  f"{', '.join(others)}: el bot no las tocará salvo que el líder opere ese "
                  "mercado", fatal=False)
    except ExchangeError as exc:
        r.add("cuenta", False, str(exc))

    if "PF_EURUSD" not in tickers:
        r.add("EUR/USD", False, "sin ticker PF_EURUSD", fatal=False)

    r.add("topes absolutos", True,
          f"{limits.HARD_MAX_LEVERAGE}x, {limits.HARD_MAX_NOTIONAL_PER_ASSET_USD} USD/activo, "
          f"{limits.HARD_MAX_NOTIONAL_TOTAL_USD} USD total; perfil de arranque "
          f"{limits.STARTUP_PROFILE_MAX_LEVERAGE}x y "
          f"{limits.STARTUP_PROFILE_MAX_ASSET_USD} USD/activo", fatal=False)

    if r.passed:
        state.live_check = {
            "config_hash": config_hash(cfg),
            "key_fingerprint": key_fingerprint(creds),
            "passed_at": now().isoformat(),
        }
    else:
        state.live_check = None
    return r


def live_check_valid(state: BotState, cfg: Config, creds: KrakenCredentials) -> str | None:
    """None si el --check guardado vale para esta config y esta clave; si no, el motivo."""
    chk = state.live_check
    if not chk:
        return "no hay un --check superado"
    if chk.get("config_hash") != config_hash(cfg):
        return "la configuración cambió desde el último --check"
    if chk.get("key_fingerprint") != key_fingerprint(creds):
        return "la clave de Kraken cambió desde el último --check"
    return None
