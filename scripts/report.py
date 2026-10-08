#!/usr/bin/env python3
"""Atajo: python scripts/report.py --help (equivale a python -m copybot.analysis.report)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from copybot.analysis.report import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
