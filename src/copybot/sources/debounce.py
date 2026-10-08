"""Debounce de disparos (fills del WebSocket) antes de lanzar un ciclo.

Un fill suele venir acompañado de otros (una orden grande se ejecuta en
varios trozos). Se espera a que pasen `delay` segundos sin fills nuevos para
leer la posición ya asentada. Para que un goteo continuo (p. ej. un TWAP) no
aplace el ciclo indefinidamente, se ejecuta como mucho `max_wait` segundos
después del primer disparo pendiente. Las ejecuciones nunca se solapan.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


class Debouncer:
    def __init__(
        self,
        delay_seconds: float,
        action: Callable[[], Awaitable[None]],
        *,
        max_wait_seconds: float | None = None,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds no puede ser negativo")
        self._delay = delay_seconds
        self._max_wait = max(max_wait_seconds or 5 * delay_seconds, delay_seconds)
        self._action = action
        self._first: float | None = None
        self._deadline = 0.0
        self._task: asyncio.Task[None] | None = None
        self._running = asyncio.Lock()
        self.runs = 0

    @property
    def pending(self) -> bool:
        return self._task is not None and not self._task.done()

    def trigger(self) -> None:
        now = asyncio.get_running_loop().time()
        if self._first is None:
            self._first = now
        self._deadline = min(now + self._delay, self._first + self._max_wait)
        if not self.pending:
            self._task = asyncio.create_task(self._wait_and_run())

    async def _wait_and_run(self) -> None:
        loop = asyncio.get_running_loop()
        while (remaining := self._deadline - loop.time()) > 0:
            await asyncio.sleep(remaining)
        self._first = None  # los disparos que lleguen ahora programan otro ciclo
        self._task = None
        async with self._running:
            self.runs += 1
            try:
                await self._action()
            except Exception:
                log.exception("fallo en el ciclo disparado por el WebSocket")

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
