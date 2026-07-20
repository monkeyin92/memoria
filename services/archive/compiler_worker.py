"""Small resilient outbox worker for rebuildable memory projections."""

from __future__ import annotations

import asyncio
import logging

from services.archive.memory_domain import MemoryCatalogPort

logger = logging.getLogger(__name__)


class MemoryCompilerWorker:
    def __init__(
        self,
        catalog: MemoryCatalogPort,
        *,
        interval_s: float = 1.0,
        batch_size: int = 100,
    ) -> None:
        if not 0.1 <= interval_s <= 300:
            raise ValueError("memory compiler interval must be between 0.1 and 300 seconds")
        if not 1 <= batch_size <= 1000:
            raise ValueError("memory compiler batch size must be between 1 and 1000")
        self._catalog = catalog
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="memory-compiler-worker")

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._catalog.compile_pending(limit=self._batch_size)
            except Exception:
                logger.exception("memory compiler iteration failed")
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval_s)
            except TimeoutError:
                pass
            self._wake.clear()
