"""100 tool condition changes: zero stale tool-epoch broadcast."""

from __future__ import annotations

import asyncio

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.orchestrator import Orchestrator
from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current",
    [
        GenerationFence("other", 1, 1, 0),
        GenerationFence("s", 2, 1, 0),
        GenerationFence("s", 1, 2, 0),
        GenerationFence("s", 1, 1, 1),
    ],
)
async def test_tool_result_requires_complete_generation_fence(
    current: GenerationFence,
) -> None:
    tm = TaskManager()

    async def tool(
        args: dict[str, object], cancel_event: asyncio.Event
    ) -> dict[str, object]:
        _ = args, cancel_event
        return {"summary": "current"}

    tm.register(
        ToolSpec("lookup", "lookup", {}, True, True, 1.0),
        tool,
    )
    rec = await tm.start("lookup", {}, GenerationFence("s", 1, 1, 0))
    await rec.task

    assert tm.accept_result(rec.tool_task_id, current) is None
    assert tm.stale_broadcast_count == 1


@pytest.mark.asyncio
async def test_100_tool_condition_changes_zero_stale() -> None:
    orch = Orchestrator()
    await orch.ready()
    tm = orch.task_manager

    async def slow_search(args: dict[str, object], cancel_event: asyncio.Event) -> dict[str, object]:
        for _ in range(50):
            if cancel_event.is_set():
                return {"error": "cancelled"}
            await asyncio.sleep(0.001)
        return {"summary": f"结果{args.get('q')}", "q": args.get("q")}

    tm.register(
        ToolSpec(
            name="search",
            description="search",
            input_schema={"type": "object"},
            cancellable=True,
            idempotent=True,
            timeout_s=5.0,
        ),
        slow_search,
    )

    stale = 0
    for i in range(100):
        fence = orch.fence
        # start tool under current epoch
        task = await tm.start("search", {"q": f"old-{i}"}, fence)
        # user changes conditions → bump tool_epoch
        # move to tool_waiting-like path if needed
        from services.agent.src.orchestration.state_machine import (
            ConversationState,
        )

        if orch.state is ConversationState.LISTENING:
            # jump for test
            orch.state_machine.state = ConversationState.TOOL_WAITING  # type: ignore[union-attr]
        new_fence = await orch.bump_tool_epoch_on_condition_change()
        assert new_fence.tool_epoch == fence.tool_epoch + 1

        # old tool result must not broadcast
        # finish old task
        try:
            await asyncio.wait_for(task.task, timeout=2)
        except (TimeoutError, asyncio.CancelledError):
            pass
        accepted = tm.accept_result(task.tool_task_id, new_fence)
        if accepted is not None:
            stale += 1
        # also gate via orchestrator full fence
        old_payload = {"summary": "stale"}
        old_fence = GenerationFence(
            session_id=fence.session_id,
            turn_id=fence.turn_id,
            generation_id=fence.generation_id,
            tool_epoch=fence.tool_epoch,
        )
        if orch.gate_tool_result(old_fence, old_payload) is not None:
            stale += 1

    assert stale == 0
    assert tm.stale_broadcast_count == 100
