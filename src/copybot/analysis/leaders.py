"""Ranking de wallets candidatas a líder por CONSISTENCIA (mes y total).

Uso:
  python -m copybot.analysis.leaders --wallets candidatas.txt [--config config.toml]
  python -m copybot.analysis.leaders --leaderboard --top 30 [--ignore-account-mode]

Origen de las candidatas:
- --wallets: lista manual (una dirección por línea; '#' para comentarios).
- --leaderboard: leaderboard NO OFICIAL de Hyperliquid
  (stats-data.hyperliquid.xyz). No está en la documentación de la API; su
  formato puede cambiar sin aviso y se marca como no oficial en la salida.
  Formato verificado el 2026-10-08 (tests/fixtures/hl_leaderboard.json).
  Preselección: ganancia en el mes Y en el total; orden por el PEOR de los dos
  puestos de ROI (mes y total), no solo por el ROI del mes.

Datos por candidata (endpoint /info oficial):
- userAbstraction: unified account / portfolio margin -> descartada (el bot no
  puede dimensionar sobre ellas). Con --ignore-account-mode se evalúan igual,
  SOLO PARA INFORMAR, marcadas como "no compatible con el bot".
- clearinghouseState: capital en perpetuos (marginSummary.accountValue),
  posiciones actuales y apalancamiento efectivo actual (suma de positionValue /
  capital). En unified account / portfolio margin el capital de perpetuos es 0
  (verificado con la API real): el colateral está en el estado spot, así que
  se usa el capital total de la cuenta (serie "month" de portfolio) y se indica.
- spotClearinghouseState: moneda del colateral.
- portfolio: series de capital y PnL acumulado. Cuentas estándar: perpMonth y
  perpAllTime. Rentabilidad de cada periodo descontando depósitos y retiros
  (Modified Dietz): flujo = variación de capital - variación de PnL;
  r = variación de PnL / (capital inicial + flujo / 2). Los periodos con una
  base de capital por debajo de un mínimo no se usan (evitan rentabilidades
  absurdas con la cuenta casi vacía); si son demasiados, se descarta.
  Unified / portfolio margin: las series perp* traen capital 0 pero sí el PnL de
  perpetuos. El numerador es ese PnL de perpetuos (lo que el bot copiaría; el
  resultado del spot no cuenta) y la base es el capital total de la cuenta
  (month / allTime), con los flujos calculados sobre la serie total.
- userFillsByTime (30 días): actividad (scalpers e inactivas), peso de los
  perpetuos en el volumen (los traders sobre todo de spot se descartan: el bot
  solo copia perpetuos), mercados operados (volumen sin mercado en Kraken) y
  apalancamiento efectivo histórico (posiciones reconstruidas con
  startPosition / capital de ese momento).

Orden: puntuación de consistencia = el MENOR de los Sharpe anualizados del mes
(rentabilidades diarias) y del total (periodos de la serie histórica).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

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
STABLE_COLLATERAL = frozenset({"USDC", "USDT", "USDT0", "USDH", "USDE", "USD"})
DAY_MS = 86_400_000

Point = tuple[int, Decimal, Decimal]  # (ms, capital, PnL acumulado)


class InfoSource(Protocol):
    async def user_abstraction(self, user: str) -> str: ...
    async def clearinghouse_raw(self, user: str) -> dict[str, Any]: ...
    async def spot_state(self, user: str) -> dict[str, Any]: ...
    async def portfolio(self, user: str) -> dict[str, Any]: ...
    async def user_fills_by_time(self, user: str, start_ms: int) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Criteria:
    max_positions: int = 8
    max_fills_per_day: Decimal = Decimal(40)
    min_fills: int = 5  # menos actividad en 30 días: no hay nada que copiar
    min_history_days: int = 90
    min_perp_capital: Decimal = Decimal(10000)
    max_leverage: Decimal = Decimal(10)
    leverage_percentile: Decimal = Decimal("0.9")
    min_daily_returns: int = 20
    min_total_periods: int = 8
    max_skipped_share: Decimal = Decimal("0.2")  # periodos sin base de capital suficiente
    max_unmapped_pct: Decimal = Decimal(10)
    min_perp_volume_pct: Decimal = Decimal(50)  # peso mínimo de perpetuos en el volumen
    window_days: int = 30
    ignore_account_mode: bool = False

    @property
    def capital_floor(self) -> Decimal:
        """Base mínima de un periodo para medir su rentabilidad."""
        return self.min_perp_capital / 10


@dataclass
class Candidate:
    address: str
    source: str
    account_mode: str | None = None
    bot_compatible: bool | None = None
    capital_usd: Decimal | None = None
    capital_source: str = ""  # "perpetuos" | "cuenta_total"
    pnl_source: str = ""  # "perpetuos" | "perpetuos_sobre_cuenta_total"
    collateral: str = ""
    history_days: int | None = None
    consistency: Decimal | None = None
    sharpe_30d: Decimal | None = None
    return_30d_pct: Decimal | None = None
    max_drawdown_30d_pct: Decimal | None = None
    sharpe_total: Decimal | None = None
    return_total_pct: Decimal | None = None
    max_drawdown_total_pct: Decimal | None = None
    leverage_now: Decimal | None = None
    leverage_p90_30d: Decimal | None = None
    fills_30d: int | None = None
    fills_per_day: Decimal | None = None
    positions_now: int | None = None
    positions_max_30d: int | None = None
    perp_volume_pct: Decimal | None = None
    unmapped_volume_pct: Decimal | None = None
    discarded: str = ""


@dataclass(frozen=True)
class WindowStats:
    returns: list[Decimal]
    skipped: int
    period_days: Decimal  # duración media de un periodo

    @property
    def total_return_pct(self) -> Decimal:
        level = Decimal(1)
        for r in self.returns:
            level *= 1 + r
        return (level - 1) * 100

    @property
    def max_drawdown_pct(self) -> Decimal:
        return max_drawdown_pct(self.returns)

    @property
    def sharpe(self) -> Decimal | None:
        if self.period_days <= 0:
            return None
        return sharpe(self.returns, Decimal(365) / self.period_days)

    @property
    def skipped_share(self) -> Decimal:
        n = len(self.returns) + self.skipped
        return Decimal(self.skipped) / n if n else Decimal(1)


# --- series y rentabilidades ---


def points(history: dict[str, Any]) -> list[Point]:
    """(ms, capital, PnL acumulado), ordenados por tiempo."""
    av = {int(t): Decimal(str(v)) for t, v in history.get("accountValueHistory") or []}
    pnl = {int(t): Decimal(str(v)) for t, v in history.get("pnlHistory") or []}
    return [(t, av[t], pnl[t]) for t in sorted(set(av) & set(pnl))]


def daily(pts: list[Point]) -> list[Point]:
    """Último punto de cada día UTC."""
    per_day: dict[int, Point] = {}
    for p in pts:
        per_day[p[0] // DAY_MS] = p
    return [per_day[d] for d in sorted(per_day)]


def pnl_at(pts: list[Point], t: int) -> Decimal:
    """PnL acumulado del último punto de la serie en o antes de t (0 si no hay)."""
    i = bisect_right([p[0] for p in pts], t) - 1
    return pts[i][2] if i >= 0 else ZERO


def flow_adjusted_returns(pts: list[Point], floor: Decimal,
                          gains_from: list[Point] | None = None) -> WindowStats:
    """Rentabilidad de cada periodo descontando depósitos y retiros (Modified Dietz).

    Con gains_from, el numerador es la variación del PnL de esa otra serie (p. ej. el
    PnL de perpetuos de una unified account); capital y flujos salen de pts."""
    returns: list[Decimal] = []
    skipped = 0
    spans: list[int] = []
    for (t0, av0, p0), (t1, av1, p1) in zip(pts, pts[1:], strict=False):
        flow = (av1 - av0) - (p1 - p0)  # depósitos (+) o retiros (-)
        gain = p1 - p0 if gains_from is None else pnl_at(gains_from, t1) - pnl_at(gains_from, t0)
        base = av0 + flow / 2
        if base < floor or base <= 0:
            skipped += 1
            continue
        returns.append(max(gain / base, Decimal(-1)))  # no se puede perder más del 100 %
        spans.append(t1 - t0)
    period_days = (Decimal(sum(spans)) / len(spans) / DAY_MS) if spans else ZERO
    return WindowStats(returns, skipped, period_days)


def sharpe(returns: list[Decimal], periods_per_year: Decimal = Decimal(365)) -> Decimal | None:
    if len(returns) < 2:
        return None
    mean = sum(returns, ZERO) / len(returns)
    var = sum(((r - mean) ** 2 for r in returns), ZERO) / (len(returns) - 1)
    if var <= 0:
        return None
    return mean / var.sqrt() * periods_per_year.sqrt()


def max_drawdown_pct(returns: list[Decimal]) -> Decimal:
    level = peak = Decimal(1)
    worst = ZERO
    for r in returns:
        level *= 1 + r
        peak = max(peak, level)
        worst = max(worst, (peak - level) / peak * 100 if peak > 0 else Decimal(100))
    return worst


# --- posiciones, apalancamiento y colateral ---


def is_perp(coin: str) -> bool:
    return not coin.startswith("@") and "/" not in coin  # spot: "@107", "PURR/USDC"


def leverage_history(fills: list[dict[str, Any]],
                     capital: list[Point]) -> tuple[list[Decimal], int]:
    """(apalancamiento efectivo tras cada fill, máximo de posiciones simultáneas).

    Las posiciones se reconstruyen con startPosition + fill; el capital es el último
    punto de la serie anterior al fill."""
    pos: dict[str, Decimal] = {}
    px: dict[str, Decimal] = {}
    times = [t for t, _, _ in capital]
    samples: list[Decimal] = []
    max_open = 0
    for f in sorted(fills, key=lambda f: int(f.get("time", 0))):
        coin = str(f.get("coin", ""))
        if not is_perp(coin) or f.get("startPosition") is None:
            continue
        size = Decimal(str(f["sz"]))
        pos[coin] = Decimal(str(f["startPosition"])) + (size if f.get("side") == "B" else -size)
        px[coin] = Decimal(str(f["px"]))
        open_coins = [c for c, p in pos.items() if p != 0]
        max_open = max(max_open, len(open_coins))
        i = bisect_right(times, int(f.get("time", 0))) - 1
        if i >= 0 and capital[i][1] > 0:
            notional = sum((abs(pos[c]) * px[c] for c in open_coins), ZERO)
            samples.append(notional / capital[i][1])
    return samples, max_open


def percentile(xs: list[Decimal], q: Decimal) -> Decimal | None:
    if not xs:
        return None
    ordered = sorted(xs)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def collateral_summary(spot: dict[str, Any], perp_capital: Decimal) -> str:
    """Moneda del colateral. Valor de los tokens no estables: coste de entrada (aprox.)."""
    parts: dict[str, Decimal] = {}
    if perp_capital > 0:
        parts["USDC (cuenta de perpetuos)"] = perp_capital
    for b in spot.get("balances") or []:
        coin = str(b.get("coin", ""))
        total = Decimal(str(b.get("total") or 0))
        value = total if coin in STABLE_COLLATERAL else Decimal(str(b.get("entryNtl") or 0))
        if value > 0:
            parts[coin] = parts.get(coin, ZERO) + value
    grand = sum(parts.values(), ZERO)
    if grand <= 0:
        return "sin saldo"
    top = sorted(parts.items(), key=lambda kv: -kv[1])[:4]
    return ", ".join(f"{c} {v / grand * 100:.0f} %" for c, v in top)


# --- evaluación ---


async def evaluate(info: InfoSource, address: str, source: str, markets: set[str],
                   symbols: SymbolsConfig, crit: Criteria, now: datetime) -> Candidate:
    c = Candidate(address=address.lower(), source=source)
    c.account_mode = await info.user_abstraction(c.address)
    c.bot_compatible = c.account_mode in STANDARD_MODES
    if not c.bot_compatible and not crit.ignore_account_mode:
        c.discarded = f"modo de cuenta {c.account_mode} no soportado"
        return c

    chs = await info.clearinghouse_raw(c.address)
    spot = await info.spot_state(c.address)
    port = await info.portfolio(c.address)
    perp_capital = Decimal(str(chs["marginSummary"].get("accountValue") or 0))
    perp_month = points(port.get("perpMonth", {}))
    perp_total = points(port.get("perpAllTime", {}))
    if c.bot_compatible:
        month_pts, total_pts = perp_month, perp_total
        gains_month: list[Point] | None = None
        gains_total: list[Point] | None = None
        c.capital_usd, c.capital_source, c.pnl_source = perp_capital, "perpetuos", "perpetuos"
    else:
        # Capital de perpetuos 0: base = cuenta total; numerador = solo PnL de perpetuos
        month_pts, total_pts = points(port.get("month", {})), points(port.get("allTime", {}))
        gains_month, gains_total = perp_month, perp_total
        c.capital_usd = month_pts[-1][1] if month_pts else ZERO
        c.capital_source, c.pnl_source = "cuenta_total", "perpetuos_sobre_cuenta_total"
    c.collateral = collateral_summary(spot, perp_capital)
    if c.capital_usd < crit.min_perp_capital:
        c.discarded = (f"capital en {c.capital_source} {c.capital_usd:.0f} USD "
                       f"(< {crit.min_perp_capital})")
        return c

    funded = [p for p in total_pts if p[1] > 0]
    c.history_days = ((now - datetime.fromtimestamp(funded[0][0] / 1000, UTC)).days
                      if funded else 0)
    if c.history_days < crit.min_history_days:
        c.discarded = f"historial de {c.history_days} días (< {crit.min_history_days})"
        return c

    month = flow_adjusted_returns(daily(month_pts), crit.capital_floor, gains_month)
    total = flow_adjusted_returns(total_pts, crit.capital_floor, gains_total)
    for name, stats, needed in (("30 días", month, crit.min_daily_returns),
                                ("total", total, crit.min_total_periods)):
        if len(stats.returns) < needed or stats.skipped_share > crit.max_skipped_share:
            c.discarded = (f"serie {name} no medible: {len(stats.returns)} periodos válidos, "
                           f"{stats.skipped} con capital < {crit.capital_floor:.0f} USD")
            return c
    c.sharpe_30d, c.sharpe_total = month.sharpe, total.sharpe
    c.return_30d_pct, c.return_total_pct = month.total_return_pct, total.total_return_pct
    c.max_drawdown_30d_pct = month.max_drawdown_pct
    c.max_drawdown_total_pct = total.max_drawdown_pct

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
    vol = unmapped = spot_vol = ZERO
    for f in fills:
        coin = str(f.get("coin", ""))
        notional = Decimal(str(f.get("px", 0))) * Decimal(str(f.get("sz", 0)))
        if not is_perp(coin):
            spot_vol += notional
            continue
        vol += notional
        if (symbols.overrides.get(coin) or mapper.default_symbol(coin)) not in markets:
            unmapped += notional
    c.perp_volume_pct = vol / (vol + spot_vol) * 100 if vol + spot_vol else ZERO
    if c.perp_volume_pct < crit.min_perp_volume_pct:
        c.discarded = (f"volumen en perpetuos {c.perp_volume_pct:.1f} % "
                       f"(< {crit.min_perp_volume_pct} %): opera sobre todo spot")
        return c
    c.unmapped_volume_pct = unmapped / vol * 100 if vol else ZERO
    if c.unmapped_volume_pct > crit.max_unmapped_pct:
        c.discarded = (f"{c.unmapped_volume_pct:.1f} % del volumen en activos sin mercado "
                       "en Kraken")
        return c

    positions = [a for a in chs.get("assetPositions") or []
                 if Decimal(str(a["position"].get("szi") or 0)) != 0]
    c.positions_now = len(positions)
    exposure = sum((abs(Decimal(str(a["position"].get("positionValue") or 0)))
                    for a in positions), ZERO)
    c.leverage_now = exposure / c.capital_usd if c.capital_usd > 0 else None
    samples, c.positions_max_30d = leverage_history(fills, month_pts)
    c.leverage_p90_30d = percentile(samples, crit.leverage_percentile)
    worst_leverage = max(v for v in (c.leverage_now, c.leverage_p90_30d, ZERO) if v is not None)
    if worst_leverage > crit.max_leverage:
        c.discarded = (f"apalancamiento efectivo {worst_leverage:.1f}x "
                       f"(> {crit.max_leverage}x; ahora o p90 de 30 días)")
    elif c.positions_now > crit.max_positions:
        c.discarded = f"{c.positions_now} posiciones simultáneas (> {crit.max_positions})"
    elif c.sharpe_30d is None or c.sharpe_total is None:
        c.discarded = "Sharpe no calculable (sin variación)"
    else:
        c.consistency = min(c.sharpe_30d, c.sharpe_total)
    return c


# --- leaderboard, ranking y salida ---


def parse_leaderboard(payload: Any, *, top: int,
                      min_account_value: Decimal = ZERO) -> list[str]:
    """Formato NO OFICIAL: {"leaderboardRows": [{"ethAddress", "accountValue",
    "windowPerformances": [["month", {"pnl", "roi", "vlm"}], ...]}]}.

    Preselección por consistencia: ganancia en el mes y en el total; orden por el
    peor de los dos puestos de ROI. min_account_value es solo un prefiltro opcional
    (el capital que cuenta se mide después en perpetuos)."""
    rows = payload.get("leaderboardRows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise LeaderDataError("leaderboard no oficial: formato inesperado")
    picked: list[tuple[str, Decimal, Decimal]] = []
    for r in rows:
        try:
            perf = dict(r.get("windowPerformances") or [])
            month, total = perf.get("month") or {}, perf.get("allTime") or {}
            av = Decimal(str(r.get("accountValue")))
            roi_m, pnl_m = Decimal(str(month.get("roi"))), Decimal(str(month.get("pnl")))
            roi_t, pnl_t = Decimal(str(total.get("roi"))), Decimal(str(total.get("pnl")))
            addr = str(r["ethAddress"]).lower()
        except (KeyError, TypeError, ArithmeticError, ValueError):
            continue
        if av >= min_account_value and pnl_m > 0 and pnl_t > 0:
            picked.append((addr, roi_m, roi_t))
    rank_m = {a: i for i, (a, _, _) in enumerate(sorted(picked, key=lambda x: -x[1]))}
    rank_t = {a: i for i, (a, _, _) in enumerate(sorted(picked, key=lambda x: -x[2]))}
    order = sorted(picked, key=lambda x: (max(rank_m[x[0]], rank_t[x[0]]),
                                          rank_m[x[0]] + rank_t[x[0]]))
    return [a for a, _, _ in order[:top]]


def read_wallets(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.lower())
    return out


def ranked(cands: list[Candidate]) -> list[Candidate]:
    ok = sorted((c for c in cands if not c.discarded),
                key=lambda c: c.consistency if c.consistency is not None else Decimal(-10**9),
                reverse=True)
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


def table(result: list[Candidate]) -> str:
    lines = [f"{'#':>3} {'wallet':42} {'consist':>7} {'Sh30':>6} {'ShTot':>6} {'ret30%':>8} "
             f"{'retTot%':>9} {'DD30%':>6} {'DDTot%':>6} {'lev':>5} {'pos':>3} "
             f"{'capital':>11}  modo / origen / descarte"]
    for i, c in enumerate(result, 1):
        mode = c.account_mode or "-"
        if c.bot_compatible is False:
            mode += " [NO COMPATIBLE CON EL BOT: solo informativo]"
        lev = max((v for v in (c.leverage_now, c.leverage_p90_30d) if v is not None),
                  default=None)
        lines.append(
            f"{i:>3} {c.address:42} {_fmt(c.consistency):>7} {_fmt(c.sharpe_30d):>6} "
            f"{_fmt(c.sharpe_total):>6} {_fmt(c.return_30d_pct, '0.1'):>8} "
            f"{_fmt(c.return_total_pct, '0.1'):>9} {_fmt(c.max_drawdown_30d_pct, '0.1'):>6} "
            f"{_fmt(c.max_drawdown_total_pct, '0.1'):>6} {_fmt(lev, '0.1'):>5} "
            f"{c.positions_now if c.positions_now is not None else '-':>3} "
            f"{_fmt(c.capital_usd, '1'):>11}  {mode} / {c.source}"
            f"{' / DESCARTADA: ' + c.discarded if c.discarded else ''}")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    symbols = SymbolsConfig()
    if args.config:
        symbols = load_config(args.config).symbols
    crit = Criteria(
        max_positions=args.max_positions,
        max_fills_per_day=Decimal(str(args.max_fills_per_day)),
        min_history_days=args.min_history_days,
        min_perp_capital=Decimal(str(args.min_perp_capital)),
        max_leverage=Decimal(str(args.max_leverage)),
        min_perp_volume_pct=Decimal(str(args.min_perp_volume_pct)),
        ignore_account_mode=args.ignore_account_mode,
    )
    now = datetime.now(UTC)
    if crit.ignore_account_mode:
        print("AVISO: --ignore-account-mode evalúa también unified account y portfolio "
              "margin SOLO PARA INFORMAR: el bot no puede seguirlas.")
    async with httpx.AsyncClient(timeout=20) as http:
        markets = set(await KrakenMarketData(http).instruments())
        wallets: list[tuple[str, str]] = []
        if args.wallets:
            wallets += [(w, "manual") for w in read_wallets(args.wallets)]
        if args.leaderboard:
            print("AVISO: el leaderboard es una fuente NO OFICIAL de Hyperliquid.")
            r = await http.get(LEADERBOARD_URL)
            r.raise_for_status()
            wallets += [(w, "leaderboard_no_oficial")
                        for w in parse_leaderboard(r.json(), top=args.top)]
        seen: set[str] = set()
        info = HyperliquidInfo(http)
        cands = []
        for addr, source in wallets:
            if addr in seen:
                continue
            seen.add(addr)
            try:
                cands.append(await evaluate(info, addr, source, markets, symbols, crit, now))
            except (LeaderDataError, KeyError, ArithmeticError, ValueError) as exc:
                cands.append(Candidate(addr, source, discarded=f"error de datos: {exc}"))
    result = ranked(cands)
    out = args.out or Path("rank_leaders.csv")
    write_csv(out, result)
    print(table(result))
    print(f"Escrito {out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rank_leaders", description=__doc__.splitlines()[0])
    p.add_argument("--wallets", type=Path, help="lista manual de direcciones")
    p.add_argument("--leaderboard", action="store_true", help="usar el leaderboard NO OFICIAL")
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--min-perp-capital", type=float, default=10000,
                   help="capital mínimo medido en perpetuos (USD)")
    p.add_argument("--max-leverage", type=float, default=10,
                   help="apalancamiento efectivo máximo (ahora y p90 de 30 días)")
    p.add_argument("--min-perp-volume-pct", type=float, default=50,
                   help="peso mínimo de perpetuos en el volumen de 30 días (%%)")
    p.add_argument("--min-history-days", type=int, default=90)
    p.add_argument("--max-positions", type=int, default=8)
    p.add_argument("--max-fills-per-day", type=float, default=40)
    p.add_argument("--ignore-account-mode", action="store_true",
                   help="evaluar también cuentas no compatibles (solo informativo)")
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
