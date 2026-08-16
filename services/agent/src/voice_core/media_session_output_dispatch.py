"""Output intent admission, scheduling and preemption for Media Voice."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.state_machine import ConversationState, InteractionPhase
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.media_session_types import (
    MediaReplyChunk,
)
from services.agent.src.voice_core.media_session_types import (
    OutputOwnerLease as _OutputOwnerLease,
)
from services.agent.src.voice_core.media_session_types import (
    OutputWork as _OutputWork,
)
from services.common.realtime_information import requires_realtime_lookup

if TYPE_CHECKING:
    from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

media_pb2: Any = _media_pb2
logger = logging.getLogger(__name__)

_CONVERSATION_REPLY_TTL_MS = 120_000
_STREAMCORE_EXECUTABLE_OUTPUT_KINDS = frozenset(
    {
        int(media_pb2.OUTPUT_INTENT_KIND_CONVERSATION_REPLY),
        int(media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT),
        int(media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT),
    }
)


class MediaOutputDispatchMixin:
    """Select one intent and schedule its output owner."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        _sessions: dict[str, _MediaVoiceSession]
        output_generation_timeout_s: float

        def _event_versions(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
        ) -> tuple[int, int]: ...

        @staticmethod
        def _acquire_output_owner(
            context: _MediaVoiceSession,
            fence: GenerationFence,
            intent: Any | None = None,
        ) -> _OutputOwnerLease | None: ...

        @staticmethod
        def _output_context_version(
            context: _MediaVoiceSession,
            fence: GenerationFence,
        ) -> int: ...

        @staticmethod
        def _output_work_is_active(
            context: _MediaVoiceSession,
            work: _OutputWork,
        ) -> bool: ...

        @staticmethod
        def _output_work_is_current(
            context: _MediaVoiceSession,
            work: _OutputWork,
        ) -> bool: ...

        async def _cancel_reply_task(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
            *,
            reason: str = "cancelled",
            cancel_timeout_s: float = 5.0,
        ) -> None: ...

        @staticmethod
        def _rebind_output_work(
            work: _OutputWork,
            fence: GenerationFence,
            *,
            context_version: int,
        ) -> _OutputWork | None: ...

        async def _stream_output(
            self,
            context: _MediaVoiceSession,
            session_id: str,
            fence: GenerationFence,
            lease: _OutputOwnerLease,
            chunks: Any,
        ) -> bool: ...

        def _output_chunks(
            self,
            context: _MediaVoiceSession,
            work: _OutputWork,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]: ...

    async def generate_reply(
        self,
        session_id: str,
        user_text: str,
        fence: GenerationFence,
    ) -> bool:
        """Submit the normal reply through the same owner as every source."""

        context = self._sessions.get(session_id)
        if context is None or context.closed or not context.runtime.fence.matches(fence):
            return False
        if (
            context.delegation_owns_realtime_output
            and requires_realtime_lookup(user_text)
        ):
            return True
        coordinator = context.runtime.orchestrator.delegation
        now_ms = int(time.time() * 1_000)
        intent = coordinator.conversation_reply(
            fence=fence,
            context_version=self._output_context_version(context, fence),
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
        work = _OutputWork(intent, conversation_text=user_text)
        if not coordinator.output_intent_is_active(
            intent,
            current_fence=context.runtime.fence,
            current_context_version=coordinator.current_context_version(fence.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
            now_ms=now_ms,
        ):
            return False
        context.output_work[work.intent_id] = work
        if (
            context.output_owner is not None
            or context.reply_lock.locked()
            or context.runtime.orchestrator.state is ConversationState.LISTENING
        ):
            return await self._enqueue_output_work(context, work)
        return await self._run_output_work(context, work)

    async def _generate_reply(
        self,
        session_id: str,
        user_text: str,
        fence: GenerationFence,
    ) -> bool:
        # Compatibility seam retained for callers that exercised the old
        # private method directly.
        return await self.generate_reply(session_id, user_text, fence)

    async def _enqueue_output_work(
        self,
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> bool:
        """Retain an admitted source and start it only when it owns playback."""

        coordinator = context.runtime.orchestrator.delegation
        if int(work.intent.kind) not in _STREAMCORE_EXECUTABLE_OUTPUT_KINDS:
            context.output_work.pop(work.intent_id, None)
            coordinator.complete_output_intent(
                work.intent,
                current_fence=context.runtime.fence,
                current_context_version=coordinator.current_context_version(
                    work.fence.session_id
                ),
                floor_allows_output=context.runtime.output_floor_allows_assistant,
                reason="unsupported_streamcore_output_kind",
            )
            return False
        if context.closed or not self._output_work_is_active(context, work):
            return False
        context.output_work[work.intent_id] = work
        owner = context.output_owner
        if owner is not None:
            if (
                str(owner.intent.intent_id) != work.intent_id
                and self._output_work_is_current(context, work)
            ):
                return await self._preempt_output_owner(context, work)
            return True
        return await self._start_selected_output(context)

    async def _start_selected_output(self, context: _MediaVoiceSession) -> bool:
        """Start the admitted winner, or discard an unbound candidate safely."""

        if context.closed or context.output_owner is not None:
            return False
        pending = context.output_dispatch_task
        if pending is not None and not pending.done():
            return True
        coordinator = context.runtime.orchestrator.delegation
        session_id = context.identity.session_id
        while True:
            fence = context.runtime.fence
            candidate = coordinator.current_output_intent(
                session_id,
                current_fence=fence,
                current_context_version=coordinator.current_context_version(session_id),
                floor_allows_output=context.runtime.output_floor_allows_assistant,
            )
            if candidate is None:
                return False
            work = context.output_work.get(str(candidate.intent_id))
            if work is None:
                coordinator.complete_output_intent(
                    candidate,
                    current_fence=fence,
                    current_context_version=coordinator.current_context_version(session_id),
                    floor_allows_output=context.runtime.output_floor_allows_assistant,
                    reason="missing_output_work",
                )
                continue
            if context.runtime.orchestrator.state is ConversationState.LISTENING:
                work = await self._promote_auxiliary_output(context, work)
                if work is None:
                    return False
                continue
            task = asyncio.create_task(
                self._run_output_work(context, work),
                name=f"media-output-{session_id}-{work.intent_id}",
            )
            context.output_dispatch_task = task

            def clear_dispatch(done: asyncio.Task[bool]) -> None:
                if context.output_dispatch_task is done:
                    context.output_dispatch_task = None

            task.add_done_callback(clear_dispatch)
            return True

    async def _promote_auxiliary_output(
        self,
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> _OutputWork | None:
        """Move a late selected source to a new audible generation."""

        old_fence = work.fence
        next_fence = await context.runtime.begin_media_auxiliary_output(old_fence)
        if next_fence is None:
            return None
        coordinator = context.runtime.orchestrator.delegation
        rebound = self._rebind_output_work(
            work,
            next_fence,
            context_version=coordinator.current_context_version(next_fence.session_id),
        )
        if rebound is None:
            return None
        coordinator.reset_output_intent_state(next_fence.session_id)
        context.output_work.clear()
        coordinator.admit_output_intent(
            rebound.intent,
            current_fence=next_fence,
            current_context_version=coordinator.current_context_version(next_fence.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
        )
        if not self._output_work_is_current(context, rebound):
            return None
        context.output_work[rebound.intent_id] = rebound
        context.playback.start(next_fence)
        context.output_sequence = 0
        context.output_text_offset = 0
        context.assistant_text = ""
        context.provider_complete = False
        context.output_complete_emitted = False
        task_epoch, context_version = self._event_versions(context, next_fence)
        if not await self.bridge.emit_generation(
            next_fence.session_id,
            next_fence,
            action=media_pb2.GENERATION_ACTION_START,
            reason="auxiliary_output",
            task_epoch=task_epoch,
            context_version=context_version,
        ):
            coordinator.complete_output_intent(
                rebound.intent,
                current_fence=next_fence,
                current_context_version=context_version,
                floor_allows_output=context.runtime.output_floor_allows_assistant,
                reason="generation_start_rejected",
            )
            context.output_work.pop(rebound.intent_id, None)
            return None
        return rebound

    async def _preempt_output_owner(
        self,
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> bool:
        """Flush a lower-priority owner before starting the selected source."""

        owner = context.output_owner
        if owner is None or not context.runtime.fence.matches(owner.fence):
            return False
        old_fence = owner.fence
        heard = context.playback.actual_heard_text(old_fence)
        await self._cancel_reply_task(context, old_fence, reason="preempted")
        cancelled = await context.runtime.preempt_media_output(
            cause="output_preempted",
            synchronized_transcript=heard,
        )
        if cancelled.matches(old_fence):
            return False
        await context.runtime.on_media_playback_interrupted(
            interrupted_from=old_fence,
            synchronized_transcript=heard,
        )
        context.playback.discard(old_fence)
        task_epoch, context_version = self._event_versions(context, cancelled)
        if not await self.bridge.emit_realtime_effect(
            cancelled.session_id,
            media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
            cancelled,
            source_event_id="output_preempted",
            payload={"reason": "output_preempted"},
            task_epoch=task_epoch,
            context_version=context_version,
        ):
            return False
        coordinator = context.runtime.orchestrator.delegation
        context.runtime.set_interaction_phase(
            InteractionPhase.THINKING_SILENT,
            cause="media_output_preempt",
        )
        staged = self._rebind_output_work(
            work,
            cancelled,
            context_version=coordinator.current_context_version(cancelled.session_id),
        )
        if staged is None:
            return False
        coordinator.reset_output_intent_state(cancelled.session_id)
        context.output_work.clear()
        coordinator.admit_output_intent(
            staged.intent,
            current_fence=cancelled,
            current_context_version=coordinator.current_context_version(cancelled.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
        )
        if not self._output_work_is_current(context, staged):
            return False
        context.output_work[staged.intent_id] = staged
        rebound = await self._promote_auxiliary_output(context, staged)
        if rebound is None:
            return False
        return await self._start_selected_output(context)

    async def _run_output_work(
        self,
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> bool:
        fence = work.fence
        async with context.reply_lock:
            if context.closed or not self._output_work_is_current(context, work):
                return False
            lease = self._acquire_output_owner(context, fence, work.intent)
            if lease is None:
                return False
            context.output_complete_emitted = False
            task = asyncio.current_task()
            if task is not None:
                context.reply_task = task
            chunks = self._output_chunks(context, work, context.playback.renderable_sample_end(fence))
            try:
                deadline = asyncio.timeout(self.output_generation_timeout_s)
                try:
                    async with deadline:
                        return await self._stream_output(
                            context,
                            fence.session_id,
                            fence,
                            lease,
                            chunks,
                        )
                except TimeoutError:
                    if not deadline.expired():
                        # The provider itself raised TimeoutError. Preserve the
                        # provider failure semantics; it is not our generation
                        # deadline and must not be reported as output_timeout.
                        raise
                    await self._abort_output_timeout(context, fence, chunks)
                    return False
            finally:
                if context.reply_task is task:
                    context.reply_task = None
                if context.output_dispatch_task is task:
                    context.output_dispatch_task = None

    async def _abort_output_timeout(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
        chunks: AsyncIterator[MediaReplyChunk],
    ) -> None:
        """Fail closed when a provider stream stops making progress."""

        close = getattr(chunks, "aclose", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    async with asyncio.timeout(
                        min(5.0, max(0.1, self.output_generation_timeout_s))
                    ):
                        await result
            except TimeoutError:
                logger.warning(
                    "media output iterator close timed out session=%s generation=%s",
                    fence.session_id,
                    fence.generation_id,
                )
            except Exception:
                logger.exception("media output iterator close failed")

        await self._cancel_reply_task(
            context,
            fence,
            reason="output_timeout",
            cancel_timeout_s=min(5.0, max(0.1, self.output_generation_timeout_s)),
        )
        context.playback.discard(fence)
        context.assistant_text = ""
        context.output_text_offset = 0
        context.provider_complete = False
        context.output_complete_emitted = False
        for intent_id, pending in tuple(context.output_work.items()):
            if pending.fence.matches(fence):
                context.output_work.pop(intent_id, None)
        context.runtime.orchestrator.delegation.reset_output_intent_state(
            fence.session_id
        )

        if not context.runtime.fence.matches(fence):
            return
        task_epoch, context_version = self._event_versions(context, fence)
        await self.bridge.emit_realtime_effect(
            fence.session_id,
            media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
            fence,
            source_event_id="output_timeout",
            payload={"reason": "output_timeout"},
            task_epoch=task_epoch,
            context_version=context_version,
        )
        await context.runtime.on_assistant_reply_aborted(
            fence,
            cause="output_timeout",
        )
