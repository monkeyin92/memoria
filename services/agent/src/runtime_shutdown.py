"""Terminal shutdown support shared by the realtime duplex runtime.

``duplex_runtime.py`` is size-budgeted, so the close-once bookkeeping lives
here: mark the runtime closed, refuse work scheduled after close, and drain
background/durable tasks with the runtime's own bounded waits.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


async def _closed_noop() -> None:
    """Placeholder task returned when a closed runtime refuses to schedule work."""


class DuplexRuntimeShutdownMixin:
    """Close-once semantics for ``DuplexRuntime``.

    ``close()`` is terminal: nothing may be scheduled afterwards, and every
    task the close path itself schedules must be drained before it returns.
    """

    _closed: bool
    _background_tasks: set[asyncio.Task[Any]]
    _durable_tasks: set[asyncio.Task[Any]]
    _evidence_drain_timeout_s: float

    def begin_close(self) -> bool:
        """Mark the runtime closed; True means it was already closed."""
        if self._closed:
            return True
        self._closed = True
        return False

    def refuse_after_close(
        self,
        coroutine: Coroutine[Any, Any, Any],
        name: str,
    ) -> asyncio.Task[Any]:
        """Close an unstarted coroutine instead of leaking it past close()."""
        coroutine.close()
        logger.info("duplex runtime refused background task after close: %s", name)
        return asyncio.get_running_loop().create_task(_closed_noop(), name=name)

    async def drain_background_tasks(self) -> None:
        for task in tuple(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

    async def drain_durable_tasks(self) -> None:
        durable = tuple(self._durable_tasks)
        if not durable:
            return
        _, pending = await asyncio.wait(
            durable,
            timeout=self._evidence_drain_timeout_s,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
