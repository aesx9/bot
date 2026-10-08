"""Ranking de wallets candidatas a líder, por Sharpe de los últimos 30 días.

Uso:
  python -m copybot.analysis.leaders --wallets candidatas.txt [--config config.toml]
  python -m copybot.analysis.leaders --leaderboard --top 30

Origen de las candidatas:
- --wallets: lista manual (una dirección por línea; '#' para comentarios).
- --leaderboard: leaderboard NO OFICIAL de Hyperliquid
  (stats-data.hyperliquid.xyz). No está en la documentación de la API; su
  formato puede cambiar sin aviso y se marca como no oficial en la salida.
  Formato verificado el 2026-10-08 (tests/fixtures/hl_leaderboard.json).

Datos por candidata (endpoint /info oficial):
- userAbstraction: unified account / portfolio margin -> descartada (el bot
  no puede dimensionar sobre ellas).
- portfolio: historia de capital y PnL. perpAllTime da la antigüedad y
  perpMonth la serie de 30 días. Rentabilidad diaria = variación diaria del
  PnL acumulado / capital del día anterior (los depósitos no cuentan como
  rentabilidad). Sharpe anualizado = media / desviación x raíz(365).
- userFillsByTime (30 días): actividad (descarta scalpers y wallets inactivas) y mercados
  operados (descarta si demasiado volumen va a activos sin mercado en Kraken).
- clearinghouseState: posiciones simultáneas actuales (máximo 8).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from copybot.config import ConfigError, SymbolsConfig, load_config
from copybot.exchange.kraken_public import KrakenDataError, KrakenMarketData
from copybot.sources.hyperliquid_rest import (
    STANDARD_MODES,
    HyperliquidInfo,
    LeaderDataError,
)
from copybot.symbols import SymbolMapper

log = logging.getLogger(__name__)

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"  # NO OFICIAL
ZERO = Decimal(0)
FILLS_PAGE_LIMIT = 2000  # documentado: como mucho 2000 fills por respuesta


@dataclass(frozen=True)
class Criteria:
    max_positions: int = 8
    max_fills_per_day: Decimal = Decimal(40)
    min_fills: int = 5  # menos actividad en 30 días: no hay nada que copiar
    min_history_days: int = 30
    min_daily_returns: int = 20
    max_unmapped_pct: Decimal = Decimal(10)
    window_days: int = 30


@dataclass
class Candidate:
    address: str
    source: str
    account_mode: str | None = None
    history_days: int | None = None
    sharpe_30d: Decimal | None = None
    return_30d_pct: Decimal | None = None
    max_drawdown_30d_pct: Decimal | None = None
    fills_30d: int | None = None
    fills_per_day: Decimal | None = None
    positions_now: int | None = None
    unmapped_volume_pct: Decimal | None = None
    discarded: str = ""


def daily_series(history: dict[str, Any]) -> list[tuple[date, Decimal, Decimal]]:
    """Último (capital, PnL acumulado) de cada día UTC."""
    av = {int(t): Decimal(str(v)) for t, v in history.get("accountValueHistory") or []}
    pnl = {int(t): Decimal(str(v)) for t, v in history.get("pnlHistory") or []}
    per_day: dict[date, tuple[Decimal, Decimal]] = {}
    for t in sorted(set(av) & set(pnl)):
        per_day[datetime.fromtimestamp(t / 1000, tz=UTC).date()] = (av[t], pnl[t])
    return [(d, a, p) for d, (a, p) in sorted(per_day.items())]


def daily_returns(series: list[tuple[date, Decimal, Decimal]]) -> list[Decimal]:
    out = []
    for (_, av0, pnl0), (_, _, pnl1) in zip(series, series[1:], strict=False):
        if av0 > 0:
            out.append((pnl1 - pnl0) / av0)
    return out


def sharpe(returns: list[Decimal]) -> Decimal | None:
    if len(returns) < 2:
        return None
    mean = sum(returns, ZERO) / len(returns)
    var = sum(((r - mean) ** 2 for r in returns), ZERO) / (len(returns) - 1)
    if var <= 0:
        return None
    return mean / var.sqrt() * Decimal(365).sqrt()


def max_drawdown_pct(returns: list[Decimal]) -> Decimal:
    level = peak = Decimal(1)
    worst = ZERO
    for r in returns:
        level *= 1 + r
        peak = max(peak, level)
        worst = max(worst, (peak - level) / peak * 100)
    return worst


def is_perp(coin: str) -> bool:
    return not coin.startswith("@") and "/" not in coin  # spot: "@107", "PURR/USDC"


async def evaluate(info: HyperliquidInfo, address: str, source: str, markets: set[str],
                   symbols: SymbolsConfig, crit: Criteria, now: datetime) -> Candidate:
    c = Candidate(address=address.lower(), source=source)
    c.account_mode = await info.user_abstraction(c.address)
    if c.account_mode not in STANDARD_MODES:
        c.discarded = f"modo de cuenta {c.account_mode} no soportado"
        return c

    port = await info.portfolio(c.address)
    all_time = daily_series(port.get("perpAllTime", {}))
    c.history_days = (now.date() - all_time[0][0]).days if all_time else 0
    if c.history_days < crit.min_history_days:
        c.discarded = f"historial de {c.history_days} días (< {crit.min_history_days})"
        return c
    month = daily_series(port.get("perpMonth", {}))
    returns = daily_returns(month)
    if len(returns) < crit.min_daily_returns:
        c.discarded = f"solo {len(returns)} rentabilidades diarias en 30 días"
        return c
    c.sharpe_30d = sharpe(returns)
    c.max_drawdown_30d_pct = max_drawdown_pct(returns)
    if month[0][1] > 0:
        c.return_30d_pct = (month[-1][2] - month[0][2]) / month[0][1] * 100

    start_ms = int((now - timedelta(days=crit.window_days)).timestamp() * 1000)
    fills = await info.user_fills_by_time(c.address, start_ms)
    c.fills_30d = len(fills)
    c.fills_per_day = Decimal(len(fills)) / crit.window_days
    if len(fills) >= FILLS_PAGE_LIMIT or c.fills_per_day > crit.max_fills_per_day:
        more = "o más " if len(fills) >= FILLS_PAGE_LIMIT else ""
        c.discarded = f"scalper: {more}{c.fills_per_day:.1f} fills/día"
        return c
    if len(fills) < crit.min_fills:
        c.discarded = f"inactivo: {len(fills)} fills en {crit.window_days} días"
        return c
    mapper = SymbolMapper(symbols, markets)
    total = unmapped = ZERO
    for f in fills:
        coin = str(f.get("coin", ""))
        if not is_perp(coin):
            continue
        notional = Decimal(str(f.get("px", 0))) * Decimal(str(f.get("sz", 0)))
        total += notional
        sym = symbols.overrides.get(coin) or mapper.default_symbol(coin)
        if sym not in markets:
            unmapped += notional
    c.unmapped_volume_pct = unmapped / total * 100 if total else ZERO
    if c.unmapped_volume_pct > crit.max_unmapped_pct:
        c.discarded = (f"{c.unmapped_volume_pct:.1f} % del volumen en activos sin mercado "
                       "en Kraken")
        return c

    snap = await info.leader_snapshot(c.address)
    c.positions_now = len(snap.positions)
    if c.positions_now > crit.max_positions:
        c.discarded = f"{c.positions_now} posiciones simultáneas (> {crit.max_positions})"
    elif c.sharpe_30d is None:
        c.discarded = "Sharpe no calculable (sin variación)"
    return c


def parse_leaderboard(payload: Any, *, min_account_value: Decimal, top: int) -> list[str]:
    """Formato NO OFICIAL: {"leaderboardRows": [{"ethAddress", "accountValue",
    "windowPerformances": [["month", {"pnl", "roi", "vlm"}], ...]}]}."""
    rows = payload.get("leaderboardRows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise LeaderDataError("leaderboard no oficial: formato inesperado")
    picked: list[tuple[Decimal, str]] = []
    for r in rows:
        try:
            perf = dict(r.get("windowPerformances") or [])
            month = perf.get("month") or {}
            av = Decimal(str(r.get("accountValue")))
            roi, pnl = Decimal(str(month.get("roi"))), Decimal(str(month.get("pnl")))
            addr = str(r["ethAddress"]).lower()
        except (KeyError, TypeError, ArithmeticError, ValueError):
            continue
        if av >= min_account_value and pnl > 0:
            picked.append((roi, addr))
    return [a for _, a in sorted(picked, reverse=True)[:top]]


def read_wallets(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.lower())
    return out


def ranked(cands: list[Candidate]) -> list[Candidate]:
    ok = sorted((c for c in cands if not c.discarded),
                key=lambda c: c.sharpe_30d or ZERO, reverse=True)
    return ok + [c for c in cands if c.discarded]


def write_csv(path: Path, cands: list[Candidate]) -> None:
    fields = list(asdict(Candidate("", "")).keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for c in cands:
            w.writerow({k: ("" if v is None else v) for k, v in asdict(c).items()})


def _fmt(v: Decimal | None, places: str = "0.01") -> str:
    return "-" if v is None else str(v.quantize(Decimal(places)))


async def run(args: argparse.Namespace) -> int:
    symbols = SymbolsConfig()
    if args.config:
        symbols = load_config(args.config).symbols
    crit = Criteria(max_positions=args.max_positions,
                    max_fills_per_day=Decimal(str(args.max_fills_per_day)))
    now = datetime.now(UTC)
    async with httpx.AsyncClient(timeout=20) as http:
        markets = set(await KrakenMarketData(http).instruments())
        wallets: list[tuple[str, str]] = []
        if args.wallets:
            wallets += [(w, "manual") for w in read_wallets(args.wallets)]
        if args.leaderboard:
            print("AVISO: el leaderboard es una fuente NO OFICIAL de Hyperliquid.")
            r = await http.get(LEADERBOARD_URL)
            r.raise_for_status()
            wallets += [(w, "leaderboard_no_oficial") for w in parse_leaderboard(
                r.json(), min_account_value=Decimal(str(args.min_account_value)), top=args.top)]
        seen: set[str] = set()
        info = HyperliquidInfo(http)
        cands = []
        for addr, source in wallets:
            if addr in seen:
                continue
            seen.add(addr)
            try:
                cands.append(await evaluate(info, addr, source, markets, symbols, crit, now))
            except LeaderDataError as exc:
                cands.append(Candidate(addr, source, discarded=f"error de datos: {exc}"))
    result = ranked(cands)
    out = args.out or Path("rank_leaders.csv")
    write_csv(out, result)
    print(f"{'#':>3} {'wallet':42} {'Sharpe':>7} {'ret30%':>7} {'DD30%':>6} {'f/día':>6} "
          f"{'pos':>3}  origen / descarte")
    for i, c in enumerate(result, 1):
        print(f"{i:>3} {c.address:42} {_fmt(c.sharpe_30d):>7} {_fmt(c.return_30d_pct):>7} "
              f"{_fmt(c.max_drawdown_30d_pct):>6} {_fmt(c.fills_per_day, '0.1'):>6} "
              f"{c.positions_now if c.positions_now is not None else '-':>3}  "
              f"{c.source}{' / DESCARTADA: ' + c.discarded if c.discarded else ''}")
    print(f"Escrito {out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rank_leaders", description=__doc__.splitlines()[0])
    p.add_argument("--wallets", type=Path, help="lista manual de direcciones")
    p.add_argument("--leaderboard", action="store_true", help="usar el leaderboard NO OFICIAL")
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--min-account-value", type=float, default=10000)
    p.add_argument("--max-positions", type=int, default=8)
    p.add_argument("--max-fills-per-day", type=float, default=40)
    p.add_argument("--config", type=Path, help="para usar los overrides de símbolos")
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    if not args.wallets and not args.leaderboard:
        p.error("indica --wallets o --leaderboard")
    try:
        return asyncio.run(run(args))
    except (ConfigError, KrakenDataError, LeaderDataError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
