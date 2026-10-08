"""Logging: UTC, rotación por tamaño y redacción de secretos.

La redacción se aplica al texto final formateado, así que cubre también
argumentos, trazas de excepción y stack info.
"""

from __future__ import annotations

import logging
import logging.handlers
import time
from pathlib import Path

from copybot.redaction import redact

LOG_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


class RedactingFormatter(logging.Formatter):
    converter = time.gmtime  # type: ignore[assignment]  # timestamps siempre en UTC

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(
    log_dir: Path | None,
    level: int = logging.INFO,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 10,
) -> None:
    formatter = RedactingFormatter(LOG_FORMAT, DATE_FORMAT)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "copybot.log", maxBytes=max_bytes, backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)

    # httpx registra las URLs de cada petición en INFO; las bajamos a WARNING
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
