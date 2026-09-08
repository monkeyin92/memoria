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
from services.agent.src.voice_core.media_protocol import should_pause_asr_for_playback
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputState,
    MediaReplyChunk,
    OutputDispatchResult,
    OutputDispatchStatus,
)
from services.agent.src.voice_core.media_session_types import (
    OutputOwnerLease as _OutputOwnerLease,
)
from services.agent.src.voice_core.media_session_types import (
    OutputWork as _OutputWork,
)
from services.agent.src.voice_core.reply_delivery import (
    ReplyDeliveryEvent,
    reply_delivery_projection_payload,
)

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry
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


def _output_work_uses_tts(work: _OutputWork) -> bool:
    if work.conversation_text is not None:
        return True
    return getattr(work.intent, "WhichOneof", lambda _name: None)("source") == "tts_source"


def _reply_delivery_terminal_for_abort(reason: str) -> ReplyDeliveryEvent:
    if reason in {
        "superseded",
        "stale_generation",
        "cancelled",
        "reply_task_cancelled",
        "output_task_cancelled",
    }:
        return ReplyDeliveryEvent.PREEMPTED
    if reason in {"transport_rejected", "playback_rejected"}:
        return ReplyDeliveryEvent.TRANSPORT_REJECTED
    return ReplyDeliveryEvent.ERROR


class MediaOutputDispatchMixin:
    """Select one intent and schedule its output owner."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        _sessions: dict[str, _MediaVoiceSession]
        output_generation_timeout_s: float
        delegation_initial_decision_timeout_s: float

        def _event_versions(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
        ) -> tuple[int, int]: ...

        def _observe_conversation_yield_delivery(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
            event: ReplyDeliveryEvent,
        ) -> None: ...

        @staticmethod
        def _owner_has_started_playback(
            context: _MediaVoiceSession,
            lease: _OutputOwnerLease,
        ) -> bool: ...

        @staticmethod
        def _fast_ack_has_started_playback(
            context: _MediaVoiceSession,
            lease: _OutputOwnerLease,
        ) -> bool: ...

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
        async def _advance_failed_output_generation(
            context: _MediaVoiceSession,
            fence: GenerationFence,
            *,
            reason: str,
        ) -> GenerationFence | None: ...

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
            *,
            measure_tts_first_frame: bool,
            stall_deadline: asyncio.Timeout,
        ) -> OutputDispatchResult: ...

        def _output_chunks(
            self,
            context: _MediaVoiceSession,
            work: _OutputWork,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]: ...

    def _record_output_dispatch_result(
        self,
        context: _MediaVoiceSession | None,
        result: OutputDispatchResult,
    ) -> None:
        """Publish one bounded, text-free dispatch outcome for diagnosis."""

        self._record_reply_delivery_dispatch(context, result)
        if context is not None:
            context.output_results.append(result)
            if len(context.output_results) > 32:
                del context.output_results[: len(context.output_results) - 32]
        self.metrics.inc_media_metric(
            "voice_output_dispatch_total",
            labels={
                "status": result.status.value,
                "reason": result.reason,
            },
        )
        log = logger.warning if result.status is OutputDispatchStatus.ABORTED else logger.info
        log(
            "media output result session=%s turn=%s generation=%s tool_epoch=%s "
            "session_epoch=%s status=%s reason=%s emitted_audio=%s",
            result.fence.session_id,
            result.fence.turn_id,
            result.fence.generation_id,
            result.fence.tool_epoch,
            result.fence.session_epoch,
            result.status.value,
            result.reason,
            result.emitted_audio,
        )

    def _record_reply_delivery_dispatch(
        self,
        context: _MediaVoiceSession | None,
        result: OutputDispatchResult,
    ) -> None:
        """Project dispatch lifecycle results into the single delivery ledger."""

        if context is None:
            return
        if result.status in {
            OutputDispatchStatus.STARTED,
            OutputDispatchStatus.QUEUED,
        }:
            context.reply_delivery.ensure(result.fence)
            return

        event: ReplyDeliveryEvent | None
        if result.status is OutputDispatchStatus.SKIPPED:
            event = ReplyDeliveryEvent.SKIPPED
        elif result.status is OutputDispatchStatus.COMPLETED:
            if result.reason == "provider_stream_complete" and result.emitted_audio:
                event = ReplyDeliveryEvent.PROVIDER_COMPLETED
            elif result.reason == "provider_completed_without_audio":
                event = ReplyDeliveryEvent.NO_AUDIO
            else:
                # Delegation ownership and a reserved local reply are
                # dispatch handoffs, not terminal delivery outcomes.  The
                # eventual output owner records the real terminal event.
                event = None
        else:
            event = _reply_delivery_terminal_for_abort(result.reason)

        if event is None:
            context.reply_delivery.ensure(result.fence)
            return
        self._record_reply_delivery_event(context, result.fence, event, result.reason)

    def _record_reply_delivery_event(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
        event: ReplyDeliveryEvent,
        reason: str = "",
    ) -> None:
        snapshot, changed = context.reply_delivery.record(fence, event, reason=reason)
        if not changed:
            return
        self._observe_conversation_yield_delivery(context, fence, event)
        labels = {"status": event.value}
        if reason:
            labels["reason"] = reason
        self.metrics.inc_media_metric(
            "voice_reply_delivery_event_total",
            labels=labels,
        )
        if event is ReplyDeliveryEvent.PREEMPTED and not snapshot.first_frame_sent:
            self.metrics.inc_media_metric("voice_first_frame_preempted_total")
        logger.info(
            "media reply delivery session=%s delivery_id=%s event=%s "
            "terminal=%s terminal_reason=%s first_frame_sent=%s "
            "provider_completed=%s playback_ended=%s actual_heard=%s",
            fence.session_id,
            snapshot.delivery_id,
            event.value,
            snapshot.terminal_event.value if snapshot.terminal_event else "",
            snapshot.terminal_reason or "",
            snapshot.first_frame_sent,
            snapshot.provider_completed,
            snapshot.playback_ended,
            snapshot.actual_heard,
        )
        publisher = getattr(self, "reply_delivery_publisher", None)
        if publisher is not None:
            try:
                publisher(
                    reply_delivery_projection_payload(
                        snapshot,
                        event,
                        reason=reason,
                    )
                )
            except Exception:
                # Projection is diagnostic only and must never rewrite the
                # in-process delivery authority or block the audio path.
                logger.exception(
                    "reply delivery projection admission failed session=%s event=%s",
                    fence.session_id,
                    event.value,
                )

    async def generate_reply(
        self,
        session_id: str,
        user_text: str,
        fence: GenerationFence,
    ) -> bool:
        """Submit the normal reply through the same owner as every source."""

        try:
            result = await self._dispatch_reply(session_id, user_text, fence)
        except asyncio.CancelledError:
            self._record_output_dispatch_result(
                self._sessions.get(session_id),
                OutputDispatchResult(
                    fence,
                    OutputDispatchStatus.ABORTED,
                    "reply_task_cancelled",
                ),
            )
            raise
        except Exception:
            self._record_output_dispatch_result(
                self._sessions.get(session_id),
                OutputDispatchResult(
                    fence,
                    OutputDispatchStatus.ABORTED,
                    "reply_task_exception",
                ),
            )
            raise
        self._record_output_dispatch_result(self._sessions.get(session_id), result)
        return bool(result)

    async def _dispatch_reply(
        self,
        session_id: str,
        user_text: str,
        fence: GenerationFence,
    ) -> OutputDispatchResult:
        """Return the exact scheduling/terminal outcome behind the bool API."""

        context = self._sessions.get(session_id)
        if context is None:
            return OutputDispatchResult(
                fence,
                OutputDispatchStatus.SKIPPED,
                "session_not_found",
            )
        if context.closed:
            return OutputDispatchResult(
                fence,
                OutputDispatchStatus.SKIPPED,
                "session_closed",
            )
        if not context.runtime.fence.matches(fence):
            return OutputDispatchResult(
                fence,
                OutputDispatchStatus.SKIPPED,
                "stale_fence",
            )
        claim = context.delegation_output_claims.get(fence)
        if claim is None and context.runtime.live_lookup_needed(user_text):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.delegation_initial_decision_timeout_s
            while claim is None and loop.time() < deadline:
                await asyncio.sleep(0.01)
                if context.closed or not context.runtime.fence.matches(fence):
                    return OutputDispatchResult(
                        fence,
                        OutputDispatchStatus.SKIPPED,
                        "stale_fence",
                    )
                claim = context.delegation_output_claims.get(fence)
        if claim is not None:
            claim.observe_normal_reply()
            if claim.state is DelegationOutputState.PENDING:
                try:
                    await asyncio.wait_for(
                        claim.wait_initial_decision(),
                        timeout=self.delegation_initial_decision_timeout_s,
                    )
                except TimeoutError:
                    if claim.release():
                        logger.warning(
                            "media delegation claim timed out session=%s generation=%s",
                            fence.session_id,
                            fence.generation_id,
                        )
            if context.closed or not context.runtime.fence.matches(fence):
                return OutputDispatchResult(
                    fence,
                    OutputDispatchStatus.SKIPPED,
                    "stale_fence",
                )
            if claim.state in {
                DelegationOutputState.OWNED,
                DelegationOutputState.COMPLETED,
            }:
                return OutputDispatchResult(
                    fence,
                    OutputDispatchStatus.COMPLETED,
                    "delegation_output_owned",
                )
            if (
                claim.state is DelegationOutputState.RELEASED
                and not claim.reserve_local_reply()
            ):
                return OutputDispatchResult(
                    fence,
                    OutputDispatchStatus.COMPLETED,
                    "local_reply_already_reserved",
                )
        elif context.runtime.live_lookup_needed(user_text):
            # Lookup owns the audible path. Starting a parallel conversation
            # reply here is what cut FAST_ACK down to 「稍」 on live queries.
            return OutputDispatchResult(
                fence,
                OutputDispatchStatus.COMPLETED,
                "live_lookup_pending",
            )
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
        work = _OutputWork(intent, fence, conversation_text=user_text)
        if not coordinator.output_intent_is_active(
            intent,
            current_fence=context.runtime.fence,
            current_context_version=coordinator.current_context_version(fence.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
            now_ms=now_ms,
        ):
            return OutputDispatchResult(
                fence,
                OutputDispatchStatus.SKIPPED,
                "output_intent_inactive",
            )
        context.output_work[work.intent_id] = work
        if (
            context.output_owner is not None
            or context.reply_lock.locked()
            or context.runtime.orchestrator.state
            in {ConversationState.LISTENING, ConversationState.TOOL_WAITING}
        ):
            if not await self._enqueue_output_work(context, work):
                return OutputDispatchResult(
                    fence,
                    OutputDispatchStatus.SKIPPED,
                    "output_enqueue_rejected",
                )
            dispatch = context.output_dispatch_task
            if dispatch is not None and not dispatch.done():
                return OutputDispatchResult(
                    fence,
                    OutputDispatchStatus.STARTED,
                    "output_task_started",
                )
            return OutputDispatchResult(
                fence,
                OutputDispatchStatus.QUEUED,
                "output_queued",
            )
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
            if context.runtime.orchestrator.state in {
                ConversationState.LISTENING,
                ConversationState.TOOL_WAITING,
            }:
                work = await self._promote_auxiliary_output(context, work)
                if work is None:
                    return False
                continue
            if not await self._ensure_transport_generation_started(context, work.fence):
                coordinator.complete_output_intent(
                    work.intent,
                    current_fence=fence,
                    current_context_version=coordinator.current_context_version(session_id),
                    floor_allows_output=context.runtime.output_floor_allows_assistant,
                    reason="generation_start_rejected",
                )
                context.output_work.pop(work.intent_id, None)
                continue
            task = asyncio.create_task(
                self._run_output_work(context, work),
                name=f"media-output-{session_id}-{work.intent_id}",
            )
            context.output_dispatch_task = task
            dispatch_fence = work.fence

            def clear_dispatch(
                done: asyncio.Task[OutputDispatchResult],
                dispatch_fence: GenerationFence = dispatch_fence,
            ) -> None:
                if context.output_dispatch_task is done:
                    context.output_dispatch_task = None
                if done.cancelled():
                    self._record_output_dispatch_result(
                        context,
                        OutputDispatchResult(
                            dispatch_fence,
                            OutputDispatchStatus.ABORTED,
                            "output_task_cancelled",
                        ),
                    )
                    return
                try:
                    result = done.result()
                except Exception:
                    self._record_output_dispatch_result(
                        context,
                        OutputDispatchResult(
                            dispatch_fence,
                            OutputDispatchStatus.ABORTED,
                            "output_task_exception",
                        ),
                    )
                    logger.exception(
                        "media output task failed session=%s fence=%s",
                        dispatch_fence.session_id,
                        dispatch_fence,
                    )
                    return
                self._record_output_dispatch_result(context, result)

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
        if should_pause_asr_for_playback(context.identity):
            await context.provider.pause_asr_for_playback(context.identity)
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

    async def _ensure_transport_generation_started(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> bool:
        """Start the transport generation before the first PCM of this fence.

        Turn commit emits START asynchronously. A live-lookup ACK can win that
        race and be rejected while the gate is still on the previous greeting.
        """

        gate = getattr(self.bridge, "bridge", None)
        session = gate.get(context.identity.session_id) if gate is not None else None
        if session is None or not session.accepts_input():
            return True
        if session.generation_active and session.fence.matches(fence):
            return True
        task_epoch, context_version = self._event_versions(context, fence)
        return await self.bridge.emit_generation(
            fence.session_id,
            fence,
            action=media_pb2.GENERATION_ACTION_START,
            reason="output_generation_start",
            task_epoch=task_epoch,
            context_version=context_version,
        )

    @staticmethod
    def _device_playback_flush_required(
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> bool:
        """Return whether CANCEL_GENERATION may become a device playback.flush.

        Direct-device firmware fail-closes ``playback.flush`` unless that
        generation is the active playing fence. An unheard owner (wake already
        ended, nudge still synthesizing) must not emit the effect.
        """

        if context.identity.client_type != "device":
            return True
        delivery = context.reply_delivery.get(fence)
        if delivery is not None and delivery.first_frame_sent:
            return True
        if context.playback.rendered_sample_end(fence) > 0:
            return True
        return bool(context.playback.actual_heard_text(fence))

    async def _emit_cancel_generation(
        self,
        context: _MediaVoiceSession,
        cancelled: GenerationFence,
        *,
        heard_fence: GenerationFence,
        source_event_id: str,
        payload: dict[str, Any],
        playback_flush_required: bool | None = None,
    ) -> bool:
        required = (
            playback_flush_required
            if playback_flush_required is not None
            else self._device_playback_flush_required(context, heard_fence)
        )
        if not required:
            logger.warning(
                "skip device playback.flush for unheard generation session=%s "
                "old_turn=%s old_gen=%s replacement_gen=%s source=%s",
                context.identity.session_id,
                heard_fence.turn_id,
                heard_fence.generation_id,
                cancelled.generation_id,
                source_event_id,
            )
            return True
        task_epoch, context_version = self._event_versions(context, cancelled)
        return await self.bridge.emit_realtime_effect(
            cancelled.session_id,
            media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
            cancelled,
            source_event_id=source_event_id,
            payload=payload,
            task_epoch=task_epoch,
            context_version=context_version,
        )

    async def _preempt_output_owner(
        self,
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> bool:
        """Flush a lower-priority owner before starting the selected source."""

        owner = context.output_owner
        if owner is None or not context.runtime.fence.matches(owner.fence):
            return False
        owner_started = self._owner_has_started_playback(context, owner)
        if owner_started and (
            not context.runtime.barge_in_enabled
            or self._fast_ack_has_started_playback(context, owner)
            or int(getattr(work.intent, "kind", 0))
            == int(media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT)
        ):
            logger.info(
                "defer output preempt until current playback finishes session=%s "
                "owner_turn=%s owner_gen=%s owner_kind=%s barge_in=%s replacement=%s",
                context.identity.session_id,
                owner.fence.turn_id,
                owner.fence.generation_id,
                int(getattr(owner.intent, "kind", 0)),
                context.runtime.barge_in_enabled,
                work.intent_id,
            )
            return True
        old_fence = owner.fence
        flush_required = self._device_playback_flush_required(context, old_fence)
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
        if not await self._emit_cancel_generation(
            context,
            cancelled,
            heard_fence=old_fence,
            source_event_id="output_preempted",
            payload={"reason": "output_preempted"},
            playback_flush_required=flush_required,
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
    ) -> OutputDispatchResult:
        fence = work.fence
        async with context.reply_lock:
            if context.closed:
                return OutputDispatchResult(
                    fence,
                    OutputDispatchStatus.SKIPPED,
                    "session_closed",
                )
            if not context.runtime.fence.matches(fence):
                return OutputDispatchResult(
                    fence,
                    OutputDispatchStatus.SKIPPED,
                    "stale_fence",
                )
            if not self._output_work_is_current(context, work):
                return OutputDispatchResult(
                    fence,
                    OutputDispatchStatus.SKIPPED,
                    "output_intent_not_selected",
                )
            lease = self._acquire_output_owner(context, fence, work.intent)
            if lease is None:
                return OutputDispatchResult(
                    fence,
                    OutputDispatchStatus.SKIPPED,
                    "output_owner_unavailable",
                )
            context.output_complete_emitted = False
            task = asyncio.current_task()
            if task is not None:
                context.reply_task = task
            chunks = self._output_chunks(context, work, context.playback.renderable_sample_end(fence))
            try:
                deadline = asyncio.timeout(self.output_generation_timeout_s)
                try:
                    async with deadline:
                        # Wall-clock duration of a long reply is not a failure.
                        # `_stream_output` reschedules this deadline on each
                        # PCM chunk so only a stalled provider/iterator aborts.
                        return await self._stream_output(
                            context,
                            fence.session_id,
                            fence,
                            lease,
                            chunks,
                            measure_tts_first_frame=_output_work_uses_tts(work),
                            stall_deadline=deadline,
                        )
                except TimeoutError:
                    if not deadline.expired():
                        # The provider itself raised TimeoutError. Preserve the
                        # provider failure semantics; it is not our generation
                        # deadline and must not be reported as output_timeout.
                        raise
                    await self._abort_output_timeout(context, fence, chunks)
                    return OutputDispatchResult(
                        fence,
                        OutputDispatchStatus.ABORTED,
                        "output_timeout",
                    )
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
        flush_required = self._device_playback_flush_required(context, fence)
        cancelled = await self._advance_failed_output_generation(
            context,
            fence,
            reason="output_timeout",
        )
        context.playback.discard(fence)
        context.assistant_text = ""
        context.output_sequence = 0
        context.output_text_offset = 0
        context.provider_complete = False
        context.output_complete_emitted = False
        for intent_id, pending in tuple(context.output_work.items()):
            if pending.fence.matches(fence):
                context.output_work.pop(intent_id, None)
        context.runtime.orchestrator.delegation.reset_output_intent_state(
            fence.session_id
        )

        if cancelled is None:
            await context.runtime.on_assistant_reply_aborted(
                fence,
                cause="output_timeout",
            )
            return
        await self._emit_cancel_generation(
            context,
            cancelled,
            heard_fence=fence,
            source_event_id="output_timeout",
            payload={"reason": "output_timeout"},
            playback_flush_required=flush_required,
        )
