"""CLI: ``python -m backtest.funding {descargar-universo,universo,descargar,ejecutar}``."""

from __future__ import annotations

import argparse
import subprocess  # noqa: S404
import sys
import time
from pathlib import Path

from backtest.data import iso
from backtest.funding.config import Account, Costs, SpotFee, Strategy
from backtest.funding.data import read_json, write_json
from backtest.funding.download import MANIFEST, download_series, download_universe, load_universe
from backtest.funding.prepare import build_a, build_b
from backtest.funding.report import Meta, render_universe, write_outputs
from backtest.funding.runner import Outcome, Unevaluable, run_strategy, spec_a, spec_b

ROOT = Path(__file__).resolve().parent
LOCK = "reservado_ejecutado.json"


class FrozenError(Exception):
    """El tramo reservado no puede ejecutarse en el estado actual."""


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def git_state(repo: Path) -> tuple[str, bool]:
    """(commit HEAD, hay cambios sin commit)."""
    head = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True  # noqa: S607
    ).stdout.strip()
    dirty = subprocess.run(  # noqa: S603
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True  # noqa: S607
    ).stdout.strip()
    return head, bool(dirty)


def guard_reserved(lock_path: Path, commit: str, dirty: bool) -> None:
    """El reservado se ejecuta una sola vez y desde un commit congelado (árbol limpio)."""
    if lock_path.exists():
        prev = read_json(lock_path)
        raise FrozenError(
            f"el tramo reservado ya se ejecutó desde {prev.get('commit')} el {prev.get('at')}"
            f" ({lock_path}); no se repite"
        )
    if dirty:
        raise FrozenError("hay cambios sin commit: el reservado solo se ejecuta desde un commit "
                          "congelado (o usa --solo-desarrollo)")


def run_all(data_dir: Path, *, only_dev: bool) -> list[Outcome]:
    """Una estrategia sin activos con datos válidos queda «no evaluable» (no falla)."""
    uni = load_universe(data_dir)
    account, costs = Account(), Costs()
    out: list[Outcome] = []
    assets_a, win_a, cov_a, exc_a = build_a(data_dir, uni, costs)
    if assets_a:
        out.append(run_strategy(Strategy.A, assets_a, win_a, cov_a, exc_a,
                                spec_a(account, costs), only_dev=only_dev, costs=costs,
                                log=_log))
    else:
        out.append(Unevaluable(Strategy.A, win_a, exc_a))
    assets_b, win_b, cov_b, exc_b = build_b(data_dir, uni, costs, SpotFee.MAKER)
    if assets_b:
        taker_b, _, _, _ = build_b(data_dir, uni, costs, SpotFee.TAKER)
        spec = spec_b(account, costs)
        out.append(run_strategy(Strategy.B, assets_b, win_b, cov_b, exc_b, spec,
                                only_dev=only_dev, taker_assets=taker_b, taker_spec=spec,
                                costs=costs, log=_log))
    else:
        out.append(Unevaluable(Strategy.B, win_b, exc_b))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backtest.funding", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, text in (
        ("descargar-universo", "instrumentos y velas diarias de 90 días (red)"),
        ("universo", "muestra los universos A y B con los datos locales (sin red)"),
        ("descargar", "velas de 1h y funding de los universos seleccionados (red)"),
        ("ejecutar", "ejecuta el backtest con los datos locales (sin red)"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--datos", type=Path, default=ROOT / "datos")
        if name == "ejecutar":
            p.add_argument("--salida", type=Path, default=ROOT / "resultados")
            p.add_argument("--informe", type=Path, default=ROOT / "REPORT.md")
            p.add_argument("--solo-desarrollo", action="store_true",
                           help="no toca el tramo reservado (para depurar sin gastar la pasada)")

    args = parser.parse_args(argv)
    now_ms = int(time.time() * 1000)
    if args.cmd == "descargar-universo":
        download_universe(args.datos, now_ms, log=_log)
        print(render_universe(load_universe(args.datos)))
    elif args.cmd == "universo":
        print(render_universe(load_universe(args.datos)))
    elif args.cmd == "descargar":
        download_series(args.datos, now_ms, log=_log)
    else:
        commit, dirty = git_state(ROOT)
        lock = args.salida / LOCK
        if not args.solo_desarrollo:
            try:
                guard_reserved(lock, commit, dirty)
            except FrozenError as exc:
                _log(f"error: {exc}")
                return 2
        results = run_all(args.datos, only_dev=args.solo_desarrollo)
        meta = Meta(
            commit=commit + (" (con cambios sin commit)" if dirty else ""),
            downloaded_at=read_json(args.datos / MANIFEST)["downloaded_at"],
            only_dev=args.solo_desarrollo,
            account=Account(),
            costs=Costs(),
        )
        write_outputs(results, meta, args.salida, args.informe)
        if not args.solo_desarrollo:
            write_json(lock, {"commit": commit, "at": iso(now_ms)})
        _log(f"informe: {args.informe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
