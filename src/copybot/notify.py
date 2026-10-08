"""Aviso de fallo del servicio (lo lanza systemd con OnFailure=copybot-failure.service).

    python -m copybot.notify --env /var/lib/copybot/.env --message "texto"

Si el proceso del bot muere, se cuelga tras agotar los reinicios (StartLimitBurst) o sale
por una protección (códigos 2/3), el propio bot ya no puede avisar: este módulo lo hace
desde fuera por Telegram y, si está configurado, marcando /fail en el healthcheck externo.
Es de mejor esfuerzo: siempre termina con código 0 y nunca imprime secretos.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx

from copybot.credentials import (
    CredentialsError,
    load_healthcheck_url,
    load_telegram_credentials,
)
from copybot.redaction import redact


def notify(env: Path, message: str, *, client: httpx.Client | None = None) -> list[str]:
    """Devuelve qué canales se avisaron (para el log de systemd)."""
    http = client or httpx.Client(timeout=10)
    text = redact(f"[copybot] CRÍTICO: {message}")
    sent: list[str] = []
    try:
        tg = load_telegram_credentials(env)
        url = f"https://api.telegram.org/bot{tg.bot_token.get_secret_value()}/sendMessage"
        try:
            http.post(url, json={"chat_id": tg.chat_id.get_secret_value(),
                                 "text": text}).raise_for_status()
            sent.append("telegram")
        except Exception as exc:
            print(f"notify: Telegram falló ({type(exc).__name__})", file=sys.stderr)
    except CredentialsError:
        print("notify: sin credenciales de Telegram en .env", file=sys.stderr)
    try:
        hc = load_healthcheck_url(env).get_secret_value().rstrip("/") + "/fail"
        try:
            http.post(hc, content=text.encode("utf-8")).raise_for_status()
            sent.append("healthcheck")
        except Exception as exc:
            print(f"notify: healthcheck falló ({type(exc).__name__})", file=sys.stderr)
    except CredentialsError:
        pass
    return sent


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="copybot.notify")
    p.add_argument("--env", type=Path, default=Path(".env"))
    p.add_argument("--message", required=True)
    args = p.parse_args(argv)
    sent = notify(args.env, args.message)
    print("notify: avisado por " + (", ".join(sent) if sent else "ningún canal"))
    return 0  # un aviso fallido no debe convertir la unidad de fallo en otro fallo


if __name__ == "__main__":
    sys.exit(main())
