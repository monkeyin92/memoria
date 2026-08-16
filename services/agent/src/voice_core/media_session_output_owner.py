"""Output ownership and generation cancellation for Media Voice."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.media_session_types import (
    OutputOwnerLease as _OutputOwnerLease,
)
from services.agent.src.voice_core.media_session_types import (
    OutputWork as _OutputWork,
)
from services.agent.src.voice_core.playback_ledger import PlaybackSpan

if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

logger = logging.getLogger(__name__)
media_pb2: Any = _media_pb2

_CONVERSATION_REPLY_TTL_MS = 120_000


class MediaOutputOwnerMixin:
    """Own and revoke the single physical output lease for a session."""

    if TYPE_CHECKING:
        def _event_versions(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
        ) -> tuple[int, int]: ...

    @staticmethod
    async def _cancel_provider_generation(
        context: _MediaVoiceSession,
        fence: GenerationFence,
        *,
        timeout_s: float = 5.0,
    ) -> None:
        """Best-effort remote cancellation after the local lease is revoked."""

        cancel = getattr(context.provider, "cancel_generation", None)
        if not callable(cancel):
            cancel = getattr(context.provider, "cancel", None)
        if not callable(cancel):
            return
        result = cancel(fence)
        if inspect.isawaitable(result):
            try:
                async with asyncio.timeout(timeout_s):
                    await result
            except TimeoutError:
                logger.warning(
                    "media provider cancellation timed out session=%s generation=%s",
                    fence.session_id,
                    fence.generation_id,
                )

    @staticmethod
    def _release_output_owner(
        context: _MediaVoiceSession,
        fence: GenerationFence,
        *,
        reason: str,
    ) -> bool:
        lease = context.output_owner
        if lease is None or not lease.fence.matches(fence):
            return False
        context.output_owner = None
        context.provider_complete = False
        context.output_work.pop(str(lease.intent.intent_id), None)
        coordinator = context.runtime.orchestrator.delegation
        coordinator.complete_output_intent(
            lease.intent,
            current_fence=context.runtime.fence,
            current_context_version=coordinator.current_context_version(fence.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
            reason=reason,
        )
        return True

    @staticmethod
    def _output_owner_is_current(
        context: _MediaVoiceSession,
        lease: _OutputOwnerLease,
    ) -> bool:
        if (
            context.output_owner is not lease
            or lease.task is not asyncio.current_task()
            or not context.runtime.fence.matches(lease.fence)
        ):
            return False
        coordinator = context.runtime.orchestrator.delegation
        return coordinator.output_intent_is_selected(
            lease.intent,
            current_fence=context.runtime.fence,
            current_context_version=coordinator.current_context_version(lease.fence.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
        )

    @staticmethod
    def _acquire_output_owner(
        context: _MediaVoiceSession,
        fence: GenerationFence,
        intent: Any | None = None,
    ) -> _OutputOwnerLease | None:
        if context.output_owner is not None:
            return None
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - every async call has a task
            return None
        coordinator = context.runtime.orchestrator.delegation
        context_version = context.runtime.orchestrator.context_version_for_fence(fence)
        now_ms = int(time.time() * 1_000)
        if intent is None:
            intent = coordinator.conversation_reply(
                fence=fence,
                context_version=context_version,
                expires_at_ms=now_ms + _CONVERSATION_REPLY_TTL_MS,
                now_ms=now_ms,
            )
            coordinator.admit_output_intent(
                intent,
                current_fence=context.runtime.fence,
                current_context_version=coordinator.current_context_version(fence.session_id),
                floor_allows_output=context.runtime.output_floor_allows_assistant,
                now_ms=now_ms,
            )
        if not coordinator.output_intent_is_selected(
            intent,
            current_fence=context.runtime.fence,
            current_context_version=coordinator.current_context_version(fence.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
            now_ms=now_ms,
        ):
            return None
        lease = _OutputOwnerLease(intent=intent, fence=fence, task=task)
        context.output_owner = lease
        return lease

    @staticmethod
    async def _record_interrupted_timed_spans(
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> None:
        """Attach provider-verified subtitle spans before a stream is cancelled."""

        getter = getattr(context.provider, "interrupted_timed_text_spans", None)
        if not callable(getter):
            return
        spans = getter(fence)
        if inspect.isawaitable(spans):
            spans = await spans
        if not isinstance(spans, (tuple, list)):
            return
        for span in spans:
            text = getattr(span, "text", None)
            start = getattr(span, "audio_start_sample", None)
            end = getattr(span, "audio_end_sample", None)
            if (
                not isinstance(text, str)
                or not text
                or not isinstance(start, int)
                or not isinstance(end, int)
            ):
                return
            text_start = context.output_text_offset
            context.output_text_offset += len(text)
            context.playback.add_span(
                PlaybackSpan(
                    fence=fence,
                    text_start=text_start,
                    text_end=context.output_text_offset,
                    audio_start_sample=start,
                    audio_end_sample=end,
                    text=text,
                )
            )

    @classmethod
    async def _cancel_reply_task(
        cls,
        context: _MediaVoiceSession,
        fence: GenerationFence,
        *,
        reason: str = "cancelled",
        cancel_timeout_s: float = 5.0,
    ) -> None:
        """Cancel provider work and drain the old reply task before reuse."""

        task = context.reply_task
        cls._release_output_owner(context, fence, reason=reason)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        else:
            task = None
        # A provider can own a remote stream after its local task has already
        # completed. Transport cancellation must still reach that provider.
        try:
            await cls._cancel_provider_generation(
                context,
                fence,
                timeout_s=cancel_timeout_s,
            )
        except Exception:
            logger.exception("media provider cancellation failed")
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("media reply cancellation observed a failed task")

    @staticmethod
    def _output_context_version(
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> int:
        return context.runtime.orchestrator.context_version_for_fence(fence)

    @staticmethod
    def _output_work_is_active(
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> bool:
        fence = work.fence
        coordinator = context.runtime.orchestrator.delegation
        return bool(
            context.runtime.fence.matches(fence)
            and coordinator.output_intent_is_active(
                work.intent,
                current_fence=context.runtime.fence,
                current_context_version=coordinator.current_context_version(fence.session_id),
                floor_allows_output=context.runtime.output_floor_allows_assistant,
            )
        )

    @staticmethod
    def _output_work_is_current(
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> bool:
        fence = work.fence
        coordinator = context.runtime.orchestrator.delegation
        return bool(
            context.runtime.fence.matches(fence)
            and coordinator.output_intent_is_selected(
                work.intent,
                current_fence=context.runtime.fence,
                current_context_version=coordinator.current_context_version(fence.session_id),
                floor_allows_output=context.runtime.output_floor_allows_assistant,
            )
        )

    @staticmethod
    def _rebind_output_work(
        work: _OutputWork,
        fence: GenerationFence,
        *,
        context_version: int,
    ) -> _OutputWork | None:
        now_ms = int(time.time() * 1_000)
        if int(work.intent.expires_at_ms) <= now_ms:
            return None
        rebound = media_pb2.OutputIntent()
        rebound.CopyFrom(work.intent)
        rebound.intent_id = str(uuid4())
        rebound.session_id = fence.session_id
        rebound.turn_id = fence.turn_id
        rebound.generation_id = fence.generation_id
        rebound.tool_epoch = fence.tool_epoch
        rebound.created_at_ms = now_ms
        rebound.context_version = context_version
        return _OutputWork(rebound, conversation_text=work.conversation_text)
