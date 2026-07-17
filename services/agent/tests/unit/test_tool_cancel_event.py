"""Cooperative cancel_event must be set on cancel_cancellable."""

from __future__ import annotations

import asyncio

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec


@pytest.mark.asyncio
async def test_cancel_sets_cancel_event() -> None:
    tm = TaskManager()
    saw_cancel = asyncio.Event()

    async def slow_tool(args: dict[str, object], cancel_event: asyncio.Event) -> dict[str, object]:
        for _ in range(500):
            if cancel_event.is_set():
                saw_cancel.set()
                return {"error": "cancelled", "via": "cancel_event"}
            await asyncio.sleep(0.005)
        return {"summary": "late"}

    tm.register(
        ToolSpec(
            name="slow",
            description="slow",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=5.0,
        ),
        slow_tool,
    )
    fence = GenerationFence("s", 1, 1, 0)
    rec = await tm.start("slow", {}, fence)
    assert rec.cancel_event.is_set() is False
    await tm.cancel_cancellable(fence)
    assert rec.cancel_event.is_set() is True
    await asyncio.wait_for(saw_cancel.wait(), timeout=1.0)
    assert saw_cancel.is_set()
