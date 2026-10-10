"""Línea de órdenes: ``python -m backtest descargar`` y ``python -m backtest ejecutar``."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from backtest.config import N_RANDOM, RANDOM_SEED, SYMBOLS
from backtest.data import download_all
from backtest.report import write_outputs
from backtest.runner import load_data, run_all

ROOT = Path(__file__).resolve().parent


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backtest", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    down = sub.add_parser("descargar", help="descarga velas 4h y funding de Kraken Futures")
    down.add_argument("--datos", type=Path, default=ROOT / "datos")

    run = sub.add_parser("ejecutar", help="ejecuta el backtest con los datos locales (sin red)")
    run.add_argument("--datos", type=Path, default=ROOT / "datos")
    run.add_argument("--salida", type=Path, default=ROOT / "resultados")
    run.add_argument("--informe", type=Path, default=ROOT / "REPORT.md")
    run.add_argument("--n-azar", type=int, default=N_RANDOM)
    run.add_argument("--semilla", type=int, default=RANDOM_SEED)
    run.add_argument(
        "--solo-desarrollo",
        action="store_true",
        help="no toca el tramo reservado (para depurar sin gastar la única pasada)",
    )

    args = parser.parse_args(argv)
    if args.cmd == "descargar":
        manifest = download_all(args.datos, SYMBOLS, int(time.time() * 1000))
        _log(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    candles, funding = load_data(args.datos)
    results = run_all(
        candles,
        funding,
        only_dev=args.solo_desarrollo,
        n_random=args.n_azar,
        seed=args.semilla,
        log=_log,
    )
    write_outputs(results, args.salida, args.informe)
    _log(f"informe: {args.informe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
