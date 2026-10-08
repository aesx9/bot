"""Estado persistente (state.json) y bloqueo de instancia única.

- Escritura atómica: fichero temporal en el mismo directorio, fsync, rename
  y fsync del directorio. Un corte de luz deja el fichero viejo o el nuevo,
  nunca uno a medias.
- Un state.json ilegible NO se reinicia en silencio: se lanza StateError y
  el bot no arranca (perdería el pico de capital, la parada y las órdenes
  pendientes).
- Lockfile con flock: impide dos instancias a la vez sobre el mismo
  directorio de datos. El bloqueo lo libera el sistema si el proceso muere.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any

from copybot.sources.sanity import SanityState

STATE_VERSION = 1


class StateError(Exception):
    """state.json ilegible o de otra versión: revisar a mano antes de arrancar."""


class AlreadyRunning(Exception):
    """Otra instancia del bot tiene el bloqueo del directorio de datos."""


def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


@dataclass
class BotState:
    mode: str | None = None  # "paper" | "live": el estado solo vale para el modo que lo creó
    halted: bool = False
    halt_reason: str = ""
    halted_at: str | None = None  # ISO UTC
    peak_equity_usd: Decimal | None = None
    consecutive_errors: int = 0
    preexisting_initialized: bool = False
    preexisting: dict[str, Decimal] = field(default_factory=dict)
    managed_symbols: set[str] = field(default_factory=set)
    sanity: SanityState = field(default_factory=SanityState)
    # cliOrdId -> datos de la orden, guardados ANTES de enviarla
    pending_orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    # (epoch, nocional USD) de las órdenes enviadas, para el circuit breaker
    breaker_log: list[tuple[float, Decimal]] = field(default_factory=list)
    # None = nunca se ha arrancado en live; True = perfil de arranque activo
    live_startup_profile: bool | None = None
    last_equity_record_at: float | None = None  # epoch
    paced_streak: int = 0  # ciclos seguidos aplazando órdenes por el límite/min
    kill_switch_closed: bool = False  # ya se cerró (del todo) lo gestionado por el fichero STOP
    # Detenido con un cierre de emergencia (STOP o drawdown) que no terminó: se reintenta
    emergency_close_pending: bool = False
    live_check: dict[str, Any] | None = None  # resultado del último --check superado
    live_funding_cursor_ms: int | None = None
    # Confirmación escrita de live: vale mientras no cambien config, clave ni código
    # y no haya habido una parada o un --reset-halt
    live_confirmation: dict[str, Any] | None = None
    funding_sign_verified: bool = False  # el signo del funding real ya cuadró una vez
    fills_seen: list[str] = field(default_factory=list)  # fill_id ya registrados (live)
    paper: dict[str, Any] | None = None  # cuenta simulada (PaperAccount.to_dict)

    def bind_mode(self, mode: str) -> None:
        """Ata el estado a un modo; si ya pertenecía a otro, el bot no arranca."""
        if self.mode is None:
            self.mode = mode
        elif self.mode != mode:
            raise StateError(
                f"el estado es del modo {self.mode!r} y se está arrancando en {mode!r}: "
                "paper y live no comparten estado (usa el directorio de datos de cada modo)"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "mode": self.mode,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "halted_at": self.halted_at,
            "peak_equity_usd": None if self.peak_equity_usd is None else str(self.peak_equity_usd),
            "consecutive_errors": self.consecutive_errors,
            "preexisting_initialized": self.preexisting_initialized,
            "preexisting": {c: str(s) for c, s in self.preexisting.items()},
            "managed_symbols": sorted(self.managed_symbols),
            "sanity": self.sanity.to_dict(),
            "pending_orders": self.pending_orders,
            "breaker_log": [[t, str(n)] for t, n in self.breaker_log],
            "live_startup_profile": self.live_startup_profile,
            "last_equity_record_at": self.last_equity_record_at,
            "paced_streak": self.paced_streak,
            "kill_switch_closed": self.kill_switch_closed,
            "emergency_close_pending": self.emergency_close_pending,
            "live_check": self.live_check,
            "live_funding_cursor_ms": self.live_funding_cursor_ms,
            "live_confirmation": self.live_confirmation,
            "funding_sign_verified": self.funding_sign_verified,
            "fills_seen": self.fills_seen,
            "paper": self.paper,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BotState:
        if not isinstance(d, dict):
            raise StateError("state.json no contiene un objeto")
        if d.get("version") != STATE_VERSION:
            raise StateError(f"versión de estado no soportada: {d.get('version')!r}")
        return cls(
            mode=d.get("mode"),
            halted=bool(d["halted"]),
            halt_reason=str(d.get("halt_reason", "")),
            halted_at=d.get("halted_at"),
            peak_equity_usd=_dec(d.get("peak_equity_usd")),
            consecutive_errors=int(d.get("consecutive_errors", 0)),
            preexisting_initialized=bool(d.get("preexisting_initialized", False)),
            preexisting={c: Decimal(s) for c, s in d.get("preexisting", {}).items()},
            managed_symbols=set(d.get("managed_symbols", [])),
            sanity=SanityState.from_dict(d.get("sanity", {})),
            pending_orders=dict(d.get("pending_orders", {})),
            breaker_log=[(float(t), Decimal(n)) for t, n in d.get("breaker_log", [])],
            live_startup_profile=d.get("live_startup_profile"),
            last_equity_record_at=d.get("last_equity_record_at"),
            paced_streak=int(d.get("paced_streak", 0)),
            kill_switch_closed=bool(d.get("kill_switch_closed", False)),
            emergency_close_pending=bool(d.get("emergency_close_pending", False)),
            live_check=d.get("live_check"),
            live_funding_cursor_ms=d.get("live_funding_cursor_ms"),
            live_confirmation=d.get("live_confirmation"),
            funding_sign_verified=bool(d.get("funding_sign_verified", False)),
            fills_seen=list(d.get("fills_seen", [])),
            paper=d.get("paper"),
        )


class StateStore:
    def __init__(
        self, path: Path, *, before_save: Callable[[BotState], None] | None = None
    ) -> None:
        self.path = path
        # p. ej. volcar la cuenta paper al estado en cada guardado, para que
        # órdenes pendientes y cuenta simulada nunca queden desincronizadas
        self.before_save = before_save

    def load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        try:
            return BotState.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except StateError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
            raise StateError(
                f"{self.path} ilegible ({type(exc).__name__}); revísalo antes de arrancar"
            ) from None

    def save(self, state: BotState) -> None:
        if self.before_save is not None:
            self.before_save(state)
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = json.dumps(state.to_dict(), indent=1, sort_keys=True).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning(f"otra instancia usa {self.path.parent}") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    @staticmethod
    def is_held(path: Path) -> bool:
        """¿Hay otra instancia con el bloqueo? Solo mira: no toma el bloqueo exclusivo."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()
