from __future__ import annotations

import asyncio
import uuid

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.task_manager import (
    TaskManager,
    ToolSpec,
    new_idempotency_key,
    spoken_result_summarizer,
)


def _spec(*, cancellable: bool = True, timeout_s: float = 1.0) -> ToolSpec:
    return ToolSpec("tool", "tool", {}, cancellable, True, timeout_s)


@pytest.mark.asyncio
async def test_start_unknown_tool_fails() -> None:
    with pytest.raises(KeyError):
        await TaskManager().start("missing", {}, GenerationFence("s", 1, 1, 0))


@pytest.mark.asyncio
async def test_wait_result_accepts_current_fence_and_counts() -> None:
    tm = TaskManager()

    async def handler(
        args: dict[str, object], cancel_event: asyncio.Event
    ) -> dict[str, object]:
        _ = cancel_event
        return {"summary": args["value"]}

    tm.register(_spec(), handler)
    fence = GenerationFence("s", 1, 1, 0)
    rec = await tm.start("tool", {"value": "ok"}, fence)

    assert tm.active_count() == 1
    assert await tm.wait_result(rec.tool_task_id, fence) == {"summary": "ok"}
    assert tm.accepted_broadcast_count == 1
    assert tm.active_count() == 0
    assert tm.accept_result("missing", fence) is None


@pytest.mark.asyncio
async def test_tool_timeout_becomes_spoken_error_payload() -> None:
    tm = TaskManager()

    async def handler(
        args: dict[str, object], cancel_event: asyncio.Event
    ) -> dict[str, object]:
        _ = args, cancel_event
        await asyncio.sleep(1)
        return {}

    tm.register(_spec(timeout_s=0.01), handler)
    fence = GenerationFence("s", 1, 1, 0)
    rec = await tm.start("tool", {}, fence)

    assert await tm.wait_result(rec.tool_task_id, fence) == {
        "error": "tool_timeout",
        "tool": "tool",
    }


@pytest.mark.asyncio
async def test_handler_exception_is_recorded() -> None:
    tm = TaskManager()

    async def handler(
        args: dict[str, object], cancel_event: asyncio.Event
    ) -> dict[str, object]:
        _ = args, cancel_event
        raise RuntimeError("boom")

    tm.register(_spec(), handler)
    rec = await tm.start("tool", {}, GenerationFence("s", 1, 1, 0))
    with pytest.raises(RuntimeError, match="boom"):
        await rec.task
    await asyncio.sleep(0)
    assert rec.finished
    assert rec.error == "boom"


@pytest.mark.asyncio
async def test_forced_cancel_and_full_fence_isolation() -> None:
    tm = TaskManager()
    started = asyncio.Event()

    async def handler(
        args: dict[str, object], cancel_event: asyncio.Event
    ) -> dict[str, object]:
        _ = args, cancel_event
        started.set()
        await asyncio.Event().wait()
        return {}

    tm.register(_spec(), handler)
    fence = GenerationFence("s", 1, 1, 0)
    rec = await tm.start("tool", {}, fence)
    await started.wait()

    await tm.cancel_cancellable(GenerationFence("s", 2, 1, 0))
    assert not rec.task.done()
    await tm.cancel_cancellable(fence)

    assert rec.cancelled
    assert rec.task.cancelled()
    assert await tm.wait_result(rec.tool_task_id, fence) is None


@pytest.mark.asyncio
async def test_noncancellable_task_is_left_running() -> None:
    tm = TaskManager()
    release = asyncio.Event()

    async def handler(
        args: dict[str, object], cancel_event: asyncio.Event
    ) -> dict[str, object]:
        _ = args, cancel_event
        await release.wait()
        return {"summary": "done"}

    tm.register(_spec(cancellable=False), handler)
    fence = GenerationFence("s", 1, 1, 0)
    rec = await tm.start("tool", {}, fence)
    await tm.cancel_cancellable(fence)
    assert not rec.task.done()
    release.set()
    await rec.task


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"error": "timeout"}, "刚才这项查询没有成功，我们稍后再试一次。"),
        ({"summary": "第一句。第二句！第三句？第四句。"}, "第一句。第二句！第三句？"),
        ({"spoken": "口语结果"}, "口语结果。"),
        ({"a": 1, "b": "二"}, "1，二。"),
        ({"nested": {}}, "结果已经出来了。"),
    ],
)
def test_spoken_result_summarizer(payload: dict[str, object], expected: str) -> None:
    assert spoken_result_summarizer(payload) == expected


def test_idempotency_key_is_uuid() -> None:
    assert uuid.UUID(new_idempotency_key())
