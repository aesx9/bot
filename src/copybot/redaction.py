"""Redacción de secretos en cualquier texto que salga del proceso.

Se usa en el formateador de logs (incluidas trazas de excepción) y está
pensado para reutilizarse en CSV y alertas. Dos mecanismos:
1. Valores exactos registrados con register_secret() al cargar .env.
2. Patrones genéricos (cabeceras de autenticación, tokens de Telegram en URLs).
"""

from __future__ import annotations

import re
import threading

REDACTED = "***REDACTED***"
_MIN_SECRET_LEN = 6  # evita redactar cadenas triviales que romperían los logs

_lock = threading.Lock()
_secrets: set[str] = set()

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Cabeceras de autenticación de Kraken Futures (y genéricas)
    # Con esquema opcional ("Authorization: Bearer <token>"): se enmascara el token entero,
    # no solo la palabra Bearer
    re.compile(r"(?i)\b(apikey|authent|authorization|api[_-]?secret|api[_-]?key)"
               r"(\s*[:=]\s*|['\"]\s*:\s*['\"])(?:(?:bearer|basic|token)\s+)?([^\s'\",}]+)"),
    # Token de bot de Telegram dentro de URLs: /bot<id>:<token>/
    re.compile(r"(bot)(\d+:)([A-Za-z0-9_-]{20,})"),
)


def register_secret(value: str) -> None:
    if len(value) >= _MIN_SECRET_LEN:
        with _lock:
            _secrets.add(value)


def clear_registered_secrets() -> None:
    """Solo para tests."""
    with _lock:
        _secrets.clear()


def redact(text: str) -> str:
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, REDACTED)
    for pat in _PATTERNS:
        text = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    return text
