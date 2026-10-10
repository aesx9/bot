"""CLI: ``python -m backtest.funding {descargar-universo,universo,descargar}``."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from backtest.funding.download import download_series, download_universe, load_universe
from backtest.funding.report import render_universe

ROOT = Path(__file__).resolve().parent


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backtest.funding", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, text in (
        ("descargar-universo", "instrumentos y velas diarias de 90 días (red)"),
        ("universo", "muestra los universos A y B con los datos locales (sin red)"),
        ("descargar", "velas de 1h y funding de los universos seleccionados (red)"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--datos", type=Path, default=ROOT / "datos")

    args = parser.parse_args(argv)
    now_ms = int(time.time() * 1000)
    if args.cmd == "descargar-universo":
        download_universe(args.datos, now_ms, log=_log)
        print(render_universe(load_universe(args.datos)))
    elif args.cmd == "universo":
        print(render_universe(load_universe(args.datos)))
    elif args.cmd == "descargar":
        download_series(args.datos, now_ms, log=_log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
