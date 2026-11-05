"""A reply request sent before its turn is committed, kept until the commit says whether it is the turn's own.

The reply model's first token comes about 0.75 s after the request (2026-10-31 device soak), and the request
used to leave only once the commit was done: the end-of-speech grace, the close verdict and the turn
preparation all ran first.  ``ReplyPipeline`` sends the request the committed turn is predicted to send while
those still run, and buffers what comes back.  The committed turn plays it only when the request it would send
itself is exactly the same; anything else cancels it and the turn asks the model as before.

Every speculative reply ends in exactly one log line, ``speculative reply ... outcome=...``, and never names the
child's words, so a robot run can count how often the head start was used.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.llm_types import ChatContext
from services.agent.src.mode_policy_client import ModePolicy

logger = logging.getLogger(__name__)

SPECULATIVE_REPLY_TASK_NAME = "speculative-reply"

Outcome = Literal[
    "used",  # the committed turn sent exactly this request and played what came back
    "other_text",  # another sentence was committed or named
    "other_speaker",
    "other_fence",
    "other_policy",
    "other_request",  # the same turn, but the request it sent differs (context, memory, speech plan)
    "late",  # the request had not gone out when the turn asked for it
    "close",  # the sentence ends the conversation
    "crisis",  # the turn plays a fixed safety script
    "lookup",  # a live lookup answers the turn
    "plan",  # the turn's plan answers without the reply model (a direct text, a planner reply)
    "memory",  # the bound person's memory is in scope but no fetch was warmed for the sentence
    "context",  # the snapshot the commit will freeze cannot be predicted
    "unused",  # the committed turn matched but never asked the reply model
    "not_committed",  # the commit rejected the sentence
    "restarted",  # a newer prediction for the same sentence replaced it
]


@dataclass(frozen=True, slots=True)
class SpeculationKey:
    """The turn a speculative reply was sent for; the committed turn has to be exactly this one."""

    text: str
    speaker: Any
    fence: GenerationFence
    policy: ModePolicy


def log_outcome(fence: GenerationFence, outcome: Outcome, *, lead_ms: int | None = None) -> None:
    logger.info(
        "speculative reply session_id=%s turn_id=%s outcome=%s lead_ms=%s",
        fence.session_id,
        fence.turn_id,
        outcome,
        lead_ms if lead_ms is not None else "-",
    )


@dataclass(frozen=True, slots=True)
class _Failure:
    error: Exception


_END = object()

Spawn = Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]]


class SpeculativeReply:
    """One speculative request: what it was sent for, the request itself, what came back, how it ended.

    ``claimed`` says a commit has begun for the sentence (an ASR final alone only names it); ``bound`` says the
    committed turn matched the key.  The model stream is read by a task of its own into a queue that only
    ``adopt`` can hand out, once; ``settle`` cancels the task and nothing read so far ever leaves the queue.
    """

    def __init__(
        self,
        key: SpeculationKey,
        *,
        claimed: bool,
        memory: asyncio.Task[Any] | None,
    ) -> None:
        self.key = key
        self.claimed = claimed
        self.memory = memory
        self.bound = False
        self.messages: list[dict[str, str]] | None = None
        self.outcome: Outcome | None = None
        self._sent_at: float | None = None
        self._chunks: asyncio.Queue[object] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    def send(self, spawn: Spawn, model: Any, chat_ctx: ChatContext) -> None:
        self._mark_sent(chat_ctx)
        self._task = spawn(self._stream(model, chat_ctx))

    def send_after_memory(
        self, spawn: Spawn, model: Any, build: Callable[[], ChatContext | None]
    ) -> None:
        """Send once the warmed memory fetch is over; ``build`` returns None when the turn moved on meanwhile."""

        async def _run() -> None:
            if self.memory is not None:
                await asyncio.wait({self.memory})
            chat_ctx = build()
            if chat_ctx is None:
                return
            self._mark_sent(chat_ctx)
            await self._stream(model, chat_ctx)

        self._task = spawn(_run())

    def adopt(self) -> AsyncGenerator[Any, None]:
        """The buffered and still-arriving stream, exactly as a fresh request would yield it."""

        self._finish("used")
        return self._replay()

    def settle(self, outcome: Outcome) -> None:
        """End without being used; a second call changes nothing."""

        if self.outcome is None:
            self._finish(outcome)
            self._cancel()

    def _mark_sent(self, chat_ctx: ChatContext) -> None:
        self.messages = chat_ctx.to_openai_messages()
        self._sent_at = time.monotonic()

    def _finish(self, outcome: Outcome) -> None:
        self.outcome = outcome
        lead_ms = (
            int((time.monotonic() - self._sent_at) * 1_000) if self._sent_at is not None else None
        )
        log_outcome(self.key.fence, outcome, lead_ms=lead_ms)

    def _cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _stream(self, model: Any, chat_ctx: ChatContext) -> None:
        try:
            async with model.chat(chat_ctx=chat_ctx, tools=[]) as stream:
                async for chunk in stream:
                    self._chunks.put_nowait(chunk)
        except Exception as exc:
            self._chunks.put_nowait(_Failure(exc))
            return
        self._chunks.put_nowait(_END)

    async def _replay(self) -> AsyncGenerator[Any, None]:
        try:
            while True:
                item = await self._chunks.get()
                if item is _END:
                    return
                if isinstance(item, _Failure):
                    raise item.error
                yield item
        finally:
            self._cancel()
