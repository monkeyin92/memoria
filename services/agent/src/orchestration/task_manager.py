"""Background tool TaskManager with tool_epoch isolation (ch.13/17)."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from services.agent.src.contracts.ids import GenerationFence, new_task_id
from services.agent.src.observability.metrics import MetricsRegistry


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    cancellable: bool
    idempotent: bool
    timeout_s: float
    contains_sensitive_data: bool = False


@dataclass
class ToolTask:
    tool_task_id: str
    tool_name: str
    fence: GenerationFence
    cancellable: bool
    task: asyncio.Task[Any]
    cancel_event: asyncio.Event
    started_mono_ns: int = field(default_factory=time.monotonic_ns)
    cancelled: bool = False
    finished: bool = False
    result: Any = None
    error: str | None = None


ToolHandler = Callable[[dict[str, Any], asyncio.Event], Awaitable[Any]]


@dataclass
class TaskManager:
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    tasks: dict[str, ToolTask] = field(default_factory=dict)
    handlers: dict[str, ToolHandler] = field(default_factory=dict)
    specs: dict[str, ToolSpec] = field(default_factory=dict)
    stale_broadcast_count: int = 0
    accepted_broadcast_count: int = 0

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self.specs[spec.name] = spec
        self.handlers[spec.name] = handler

    async def start(
        self,
        tool_name: str,
        args: dict[str, Any],
        fence: GenerationFence,
    ) -> ToolTask:
        if tool_name not in self.handlers:
            raise KeyError(f"unknown tool: {tool_name}")
        spec = self.specs[tool_name]
        cancel_event = asyncio.Event()
        tool_task_id = new_task_id()

        async def _run() -> Any:
            try:
                return await asyncio.wait_for(
                    self.handlers[tool_name](args, cancel_event),
                    timeout=spec.timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return {"error": "tool_timeout", "tool": tool_name}

        aio_task = asyncio.create_task(_run(), name=f"tool-{tool_name}-{tool_task_id}")
        rec = ToolTask(
            tool_task_id=tool_task_id,
            tool_name=tool_name,
            fence=fence,
            cancellable=spec.cancellable,
            task=aio_task,
            cancel_event=cancel_event,
        )
        self.tasks[tool_task_id] = rec

        def _done(t: asyncio.Task[Any]) -> None:
            rec.finished = True
            if t.cancelled():
                rec.cancelled = True
                return
            exc = t.exception()
            if exc is not None:
                rec.error = str(exc)
            else:
                rec.result = t.result()

        aio_task.add_done_callback(_done)
        return rec

    async def cancel_cancellable(self, fence: GenerationFence) -> None:
        for rec in list(self.tasks.values()):
            if rec.finished:
                continue
            if rec.fence.matches(fence):
                if rec.cancellable and not rec.task.done():
                    # Cooperative cancel first (set event so handlers can exit cleanly).
                    rec.cancel_event.set()
                    rec.cancelled = True
                    try:
                        await asyncio.wait_for(asyncio.shield(rec.task), timeout=0.15)
                    except (TimeoutError, asyncio.CancelledError):
                        if not rec.task.done():
                            rec.task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await rec.task

    def accept_result(
        self,
        tool_task_id: str,
        current_fence: GenerationFence,
    ) -> Any | None:
        """Return payload only when the complete generation fence matches."""
        rec = self.tasks.get(tool_task_id)
        if rec is None:
            return None
        if not rec.fence.matches(current_fence):
            self.stale_broadcast_count += 1
            self.metrics.inc_stale_result_dropped("tool")
            return None
        self.accepted_broadcast_count += 1
        return rec.result

    async def wait_result(
        self,
        tool_task_id: str,
        current_fence: GenerationFence,
    ) -> Any | None:
        rec = self.tasks[tool_task_id]
        try:
            await rec.task
        except asyncio.CancelledError:
            return None
        return self.accept_result(tool_task_id, current_fence)

    def active_count(self) -> int:
        return sum(1 for t in self.tasks.values() if not t.finished and not t.cancelled)


def spoken_result_summarizer(payload: dict[str, Any], *, max_sentences: int = 3) -> str:
    """Compress tool payload to 1–3 spoken sentences. Never dump raw JSON."""
    if "error" in payload:
        return "刚才这项查询没有成功，我们稍后再试一次。"
    if "summary" in payload and isinstance(payload["summary"], str):
        text = payload["summary"].strip()
    elif "spoken" in payload and isinstance(payload["spoken"], str):
        text = payload["spoken"].strip()
    else:
        # Pick a few short string values
        parts = [str(v) for v in payload.values() if isinstance(v, str | int | float)][:3]
        text = "，".join(parts) if parts else "结果已经出来了。"
    # Keep at most max_sentences by strong punctuation.
    chunks: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？":
            chunks.append(buf)
            buf = ""
            if len(chunks) >= max_sentences:
                break
    if buf and len(chunks) < max_sentences:
        chunks.append(buf if buf.endswith(("。", "！", "？")) else buf + "。")
    return "".join(chunks)[:200]


def new_idempotency_key() -> str:
    return str(uuid.uuid4())
