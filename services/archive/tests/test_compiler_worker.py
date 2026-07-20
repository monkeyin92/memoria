from __future__ import annotations

import asyncio

import pytest
from services.archive.compiler_worker import MemoryCompilerWorker
from services.archive.memory_domain import CompileReport


class RecoveringCatalog:
    def __init__(self) -> None:
        self.calls = 0
        self.recovered = asyncio.Event()

    async def compile_pending(self, *, limit: int = 100) -> CompileReport:
        assert limit == 7
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary database outage")
        self.recovered.set()
        return CompileReport(compiled_events=1)


@pytest.mark.asyncio
async def test_background_compiler_recovers_and_stops_without_waiting_for_interval() -> None:
    catalog = RecoveringCatalog()
    worker = MemoryCompilerWorker(catalog, interval_s=30, batch_size=7)  # type: ignore[arg-type]

    worker.start()
    worker.wake()
    await asyncio.wait_for(catalog.recovered.wait(), timeout=1)
    await asyncio.wait_for(worker.stop(), timeout=1)

    assert catalog.calls == 2
