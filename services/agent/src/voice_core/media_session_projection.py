"""Runtime event, delegation and Projection coordination for Media Voice."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.conversation_projection import (
    CommittedTurn,
    FloorState,
    ProjectionPatch,
    SpeakerEvidence,
    TurnPhase,
)
from services.agent.src.orchestration.delegation_coordinator import (
    DelegationEventKind,
    DelegationRequest,
    SideEffectPolicy,
)
from services.agent.src.orchestration.state_machine import ConversationState, InteractionPhase
from services.agent.src.prompts import BRIDGE_PHRASES, device_wake_phrase
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.grpc_bridge import (
    MediaBridgeGrpcServer,
    floor_state_for_phase,
)
from services.agent.src.voice_core.media_session_state import (
    MediaVoiceSessionState as _MediaVoiceSession,
)
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputClaim,
    DelegationOutputState,
    owned_delegation_holds_turn,
)
from services.agent.src.voice_core.media_session_types import OutputWork as _OutputWork
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent
from services.agent.src.voice_core.speech_timeline import SpeechSegment

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry

logger = logging.getLogger(__name__)
media_pb2: Any = _media_pb2


class MediaSessionProjectionMixin:
    """Keep Runtime/Projection emission behind the Registry interface."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry

        def _stream_epoch_is_current(
            self, context: _MediaVoiceSession, stream_epoch: int
        ) -> bool: ...

        def _sync_owner_silence_phase(
            self, context: _MediaVoiceSession, phase: str
        ) -> None: ...

        async def _enqueue_output_work(
            self, context: _MediaVoiceSession, work: _OutputWork
        ) -> bool: ...

        async def generate_reply(
            self,
            session_id: str,
            user_text: str,
            fence: GenerationFence,
        ) -> bool: ...

    async def _speak_device_wake_ack(self, context: _MediaVoiceSession) -> None:
        if context.identity.client_type != "device":
            return
        if not callable(getattr(context.provider, "generate_output", None)):
            return
        try:
            if context.closed or context.standby_requested:
                return
            if context.runtime.fence.turn_id != 0 or context.turn_start_sample is not None:
                return
            if context.output_owner is not None:
                return
            runtime = context.runtime
            if runtime.orchestrator.state is ConversationState.CONNECTING:
                await runtime.orchestrator.ready()
                runtime.set_interaction_phase(
                    InteractionPhase.LISTENING,
                    cause="device_wake_ack",
                )
            if context.closed or context.runtime.fence.turn_id != 0:
                return
            await self._speak_allowlisted_bridge_phrase(
                context,
                device_wake_phrase(context.identity.session_id),
                require_idle_input=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "device wake ack failed session=%s",
                context.identity.session_id,
            )

    def _nudge_missed_hearing(self, context: _MediaVoiceSession) -> None:
        if context.closed or context.standby_requested:
            return
        if context.identity.client_type != "device":
            return
        if context.runtime.assistant_speaking:
            return
        # Wake TTS echo can produce endpoint=0 with no real user speech.
        if (context.turn_endpoint_sample or 0) <= 0:
            return
        asyncio.create_task(
            self._speak_missed_hearing_ack(context),
            name=f"missed-hearing-{context.identity.session_id}",
        )

    async def _speak_missed_hearing_ack(self, context: _MediaVoiceSession) -> None:
        try:
            context.runtime.open_assistant_floor_for_nudge()
            await self._speak_allowlisted_bridge_phrase(
                context,
                BRIDGE_PHRASES[2],
                require_idle_input=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "missed hearing ack failed session=%s",
                context.identity.session_id,
            )

    async def _speak_allowlisted_bridge_phrase(
        self,
        context: _MediaVoiceSession,
        phrase: str,
        *,
        require_idle_input: bool,
    ) -> bool:
        if context.closed or context.standby_requested:
            return False
        if context.identity.client_type != "device":
            return False
        if not callable(getattr(context.provider, "generate_output", None)):
            return False
        if require_idle_input and (
            context.turn_start_sample is not None or context.output_owner is not None
        ):
            return False
        runtime = context.runtime
        if runtime.interaction_phase in {
            InteractionPhase.USER_SPEAKING,
            InteractionPhase.INTERRUPTED,
        } and require_idle_input:
            return False
        coordinator = runtime.orchestrator.delegation
        fence = runtime.fence
        now_ms = int(time.time() * 1_000)
        acknowledgement = coordinator.bridge_acknowledgement(
            phrase,
            fence=fence,
            context_version=coordinator.current_context_version(fence.session_id),
            expires_at_ms=now_ms + 8_000,
            now_ms=now_ms,
        )
        coordinator.admit_output_intent(
            acknowledgement,
            current_fence=runtime.fence,
            current_context_version=coordinator.current_context_version(fence.session_id),
            floor_allows_output=runtime.output_floor_allows_assistant,
            now_ms=now_ms,
        )
        if not coordinator.output_intent_is_active(
            acknowledgement,
            current_fence=runtime.fence,
            current_context_version=coordinator.current_context_version(fence.session_id),
            floor_allows_output=runtime.output_floor_allows_assistant,
            now_ms=now_ms,
        ):
            return False
        return await self._enqueue_output_work(
            context,
            _OutputWork(acknowledgement, fence),
        )

    async def _release_media_delegation_claim(
        self,
        context: _MediaVoiceSession,
        *,
        text: str,
        fence: GenerationFence,
        claim: DelegationOutputClaim,
        reason: str,
    ) -> None:
        """Release delegated ownership and start at most one local fallback."""

        if not claim.release():
            return
        logger.info(
            "media delegation released session=%s generation=%s reason=%s",
            fence.session_id,
            fence.generation_id,
            reason,
        )
        if not owned_delegation_holds_turn(context.delegation_output_claims, fence):
            await context.runtime.finish_owned_delegation_wait(cause=reason)
        if (
            not claim.normal_reply_observed
            or context.closed
            or not context.runtime.fence.matches(fence)
            or not context.runtime.output_floor_allows_assistant
        ):
            return
        try:
            await self.generate_reply(fence.session_id, text, fence)
        except Exception:
            logger.exception(
                "media delegation local fallback failed session=%s generation=%s",
                fence.session_id,
                fence.generation_id,
            )

    async def _run_media_delegation(
        self,
        context: _MediaVoiceSession,
        *,
        text: str,
        fence: GenerationFence,
        claim: DelegationOutputClaim,
        output_intent_acceptor: Callable[[Any], Any],
    ) -> None:
        runtime = context.runtime
        if not runtime.fence.matches(fence):
            claim.release()
            return
        coordinator = runtime.orchestrator.delegation
        try:
            context_version = runtime.orchestrator.context_version_for_fence(fence)
            handle = await coordinator.delegate(
                DelegationRequest(
                    tool_name="media_deep_response",
                    arguments={"text": text, "fence": fence},
                    fence=fence,
                    task_epoch=coordinator.next_task_epoch(fence.session_id),
                    context_version=context_version,
                    expires_at_ms=int(time.time() * 1_000) + 20_000,
                    side_effect_policy=SideEffectPolicy.READ_ONLY,
                    committed=True,
                    relevance=lambda: runtime.fence.matches(fence),
                    output_kind=media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT,
                )
            )
        except asyncio.CancelledError:
            claim.release()
            raise
        except Exception:
            logger.warning("media delegation rejected", exc_info=True)
            await self._release_media_delegation_claim(
                context,
                text=text,
                fence=fence,
                claim=claim,
                reason="delegate_rejected",
            )
            return
        if not claim.acquire():
            with contextlib.suppress(Exception):
                await coordinator.cancel(handle, "delegation_claim_released")
            return
        try:
            task_done, _pending = await asyncio.wait(
                {handle.record.task},
                timeout=0.02,
            )
            if not task_done and runtime.fence.matches(fence):
                now_ms = int(time.time() * 1_000)
                acknowledgement = coordinator.bridge_acknowledgement(
                    BRIDGE_PHRASES[1],
                    fence=fence,
                    context_version=context_version,
                    expires_at_ms=now_ms + 5_000,
                    now_ms=now_ms,
                )
                coordinator.admit_output_intent(
                    acknowledgement,
                    current_fence=runtime.fence,
                    current_context_version=coordinator.current_context_version(fence.session_id),
                    floor_allows_output=runtime.output_floor_allows_assistant,
                    now_ms=now_ms,
                )
                if coordinator.output_intent_is_active(
                    acknowledgement,
                    current_fence=runtime.fence,
                    current_context_version=coordinator.current_context_version(fence.session_id),
                    floor_allows_output=runtime.output_floor_allows_assistant,
                    now_ms=now_ms,
                ):
                    await self._enqueue_output_work(
                        context,
                        _OutputWork(acknowledgement, fence),
                    )
            terminal_kind: DelegationEventKind | None = None
            async for event in coordinator.events(handle):
                terminal_kind = event.kind
            if terminal_kind is not DelegationEventKind.RESULT_CANDIDATE or (
                isinstance(handle.record.result, dict) and "error" in handle.record.result
            ):
                await self._release_media_delegation_claim(
                    context,
                    text=text,
                    fence=fence,
                    claim=claim,
                    reason=(terminal_kind.value if terminal_kind is not None else "no_event"),
                )
                return
            intent = coordinator.output_intent(
                handle,
                current_fence=runtime.fence,
                current_task_epoch=handle.request.task_epoch,
                current_context_version=coordinator.current_context_version(fence.session_id),
                relevant=runtime.fence.matches(fence),
            )
            if intent is None:
                await self._release_media_delegation_claim(
                    context,
                    text=text,
                    fence=fence,
                    claim=claim,
                    reason="no_result",
                )
                return
            if (
                not runtime.barge_in_enabled
                and claim.state is DelegationOutputState.OWNED
                and runtime.fence.turn_id == fence.turn_id
            ):
                runtime.hold_floor_for_owned_delegation()
            coordinator.admit_output_intent(
                intent,
                current_fence=runtime.fence,
                current_context_version=coordinator.current_context_version(fence.session_id),
                floor_allows_output=runtime.output_floor_allows_assistant,
            )
            if not coordinator.output_intent_is_active(
                intent,
                current_fence=runtime.fence,
                current_context_version=coordinator.current_context_version(fence.session_id),
                floor_allows_output=runtime.output_floor_allows_assistant,
            ):
                await self._release_media_delegation_claim(
                    context,
                    text=text,
                    fence=fence,
                    claim=claim,
                    reason="output_intent_inactive",
                )
                return
            accepted = output_intent_acceptor(intent)
            if inspect.isawaitable(accepted):
                await accepted
            if not await self._enqueue_output_work(context, _OutputWork(intent, fence)):
                await self._release_media_delegation_claim(
                    context,
                    text=text,
                    fence=fence,
                    claim=claim,
                    reason="output_enqueue_rejected",
                )
                return
            claim.complete()
        except asyncio.CancelledError:
            claim.release()
            with contextlib.suppress(Exception):
                await coordinator.cancel(handle, "delegation_task_cancelled")
            raise
        except Exception:
            logger.exception(
                "media delegation failed session=%s generation=%s",
                fence.session_id,
                fence.generation_id,
            )
            await self._release_media_delegation_claim(
                context,
                text=text,
                fence=fence,
                claim=claim,
                reason="delegation_exception",
            )

    async def _publish_runtime_event(
        self,
        context: _MediaVoiceSession,
        event: dict[str, Any],
    ) -> None:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return
        payload = {key: value for key, value in event.items() if key != "type"}

        def event_int(key: str) -> int:
            value = event.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        fence = GenerationFence(
            context.identity.session_id,
            event_int("turn_id"),
            event_int("generation_id"),
            event_int("tool_epoch"),
        )
        task_epoch, context_version = self._event_versions(context, fence)
        if event_type == "assistant_state":
            phase = str(event.get("phase") or event.get("state") or "")
            self._sync_owner_silence_phase(context, phase)
            await self._emit_floor_effect(
                context,
                source_event_id=f"assistant_state:{phase}",
                phase=phase,
                fence=fence,
                task_epoch=task_epoch,
                context_version=context_version,
            )
        if event_type == "assistant_audio":
            effect_kind = {
                "duck": media_pb2.REALTIME_EFFECT_KIND_DUCK_OUTPUT,
                "restore": media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT,
            }.get(str(event.get("action") or ""))
            if effect_kind is not None:
                await self.bridge.emit_realtime_effect(
                    context.identity.session_id,
                    effect_kind,
                    fence,
                    source_event_id=f"assistant_audio:{event['action']}",
                    payload=payload,
                    task_epoch=task_epoch,
                    context_version=context_version,
                )
            return
        await self.bridge.emit_event(
            context.identity.session_id,
            event_type,
            payload,
            turn_id=event_int("turn_id"),
            generation_id=event_int("generation_id"),
            tool_epoch=event_int("tool_epoch"),
            task_epoch=task_epoch,
            context_version=context_version,
        )

    async def _emit_floor_effect(
        self,
        context: _MediaVoiceSession,
        *,
        source_event_id: str,
        phase: str | None = None,
        fence: GenerationFence | None = None,
        task_epoch: int | None = None,
        context_version: int | None = None,
    ) -> bool:
        if not self._stream_epoch_is_current(context, context.stream_epoch):
            return False
        floor_state = floor_state_for_phase(phase or context.runtime.interaction_phase.value)
        if floor_state is None:
            return False
        current_fence = fence or context.runtime.fence
        if task_epoch is None or context_version is None:
            task_epoch, context_version = self._event_versions(context, current_fence)
        context.floor_epoch += 1
        return await self.bridge.emit_floor_effect(
            context.identity.session_id,
            floor_state,
            floor_epoch=context.floor_epoch,
            fence=current_fence,
            source_event_id=source_event_id,
            task_epoch=task_epoch,
            context_version=context_version,
        )

    @staticmethod
    def _event_versions(
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> tuple[int, int]:
        coordinator = context.runtime.orchestrator.delegation
        try:
            context_version = context.runtime.orchestrator.context_version_for_fence(fence)
        except ValueError:
            context_version = coordinator.current_context_version(context.identity.session_id)
        return coordinator.current_task_epoch(context.identity.session_id), context_version

    @staticmethod
    def _projection_speaker_evidence(context: _MediaVoiceSession) -> SpeakerEvidence:
        return SpeakerEvidence(
            speaker_class=context.runtime.current_speaker_class,
            reason_code=context.runtime.current_speaker_reason_code,
            authority_verified=context.runtime.current_speaker_authority_verified,
        )

    async def _emit_projection_patch(
        self,
        context: _MediaVoiceSession,
        patch: ProjectionPatch,
    ) -> None:
        fence = context.runtime.fence
        task_epoch, context_version = self._event_versions(context, fence)
        await self.bridge.emit_event(
            context.identity.session_id,
            patch.kind.value,
            patch.to_payload(),
            turn_id=fence.turn_id,
            generation_id=fence.generation_id,
            tool_epoch=fence.tool_epoch,
            task_epoch=task_epoch,
            context_version=context_version,
        )

    async def _apply_projection_segment(
        self,
        context: _MediaVoiceSession,
        segment: SpeechSegment,
    ) -> None:
        previous_phase = context.projection.phase
        aec_verified = all(
            value is not None
            for value in (
                segment.near_end_rms,
                segment.far_end_rms,
                segment.residual_echo_score,
            )
        )
        loss_concealed = segment.loss_concealed or any(
            start < segment.capture_end_sample and end > segment.capture_start_sample
            for start, end in context.ingress.loss_concealed_ranges
        )
        patch = context.projection.apply_continuous_event(
            segment,
            turn_id_hint=context.runtime.fence.turn_id + 1,
            speaker_evidence=self._projection_speaker_evidence(context),
            playback_active=context.runtime.assistant_speaking,
            fence=context.playback.current_fence or context.runtime.fence,
            aec_verified=aec_verified or None,
            discontinuity=context.ingress.discontinuity_pending or loss_concealed,
        )
        self._observe_turn_phase(context, previous_phase)
        if patch is not None:
            await self._emit_projection_patch(context, patch)

    async def _discard_projection(
        self,
        context: _MediaVoiceSession,
        reason: str,
    ) -> None:
        previous_phase = context.projection.phase
        patch = context.projection.discard_provisional(None, reason)
        self._observe_turn_phase(context, previous_phase)
        if patch is not None:
            await self._emit_projection_patch(context, patch)

    def _observe_turn_phase(
        self,
        context: _MediaVoiceSession,
        previous_phase: TurnPhase,
    ) -> None:
        current = context.projection.phase
        if current is previous_phase:
            return
        self.metrics.inc_media_metric(
            "voice_turn_state_transition_total",
            labels={"from_state": previous_phase.value, "to_state": current.value},
        )
        reason = context.projection.phase_reason.value
        frame = context.projection.current_frame
        provisional = context.projection.provisional
        assistant_overlap = context.runtime.assistant_speaking or (
            frame is not None and frame.floor_state is FloorState.OVERLAP
        )
        if (
            previous_phase is TurnPhase.IDLE
            and current in {TurnPhase.ACOUSTIC_ONLY, TurnPhase.SEMANTIC_SPEAKING}
            and provisional is not None
            and context.conversation_initiation_provisional_id
            != provisional.provisional_id
        ):
            context.conversation_initiation_provisional_id = provisional.provisional_id
            self.metrics.inc_conversation_turn_initiation(
                "vad_first" if current is TurnPhase.ACOUSTIC_ONLY else "asr_direct",
                "assistant_overlap" if assistant_overlap else "open_floor",
            )
        if current is TurnPhase.UNCERTAIN:
            self.metrics.inc_media_metric(
                "voice_turn_uncertain_total",
                labels={"reason": reason},
            )
        if current is TurnPhase.BACKCHANNEL:
            self.metrics.inc_media_metric("voice_backchannel_filtered_total")
            self.metrics.inc_conversation_backchannel("detected")
        if previous_phase is TurnPhase.BACKCHANNEL:
            if current is TurnPhase.SEMANTIC_SPEAKING:
                self.metrics.inc_conversation_backchannel("promoted")
            elif current is TurnPhase.IDLE:
                self.metrics.inc_conversation_backchannel("continued")
        if (
            current is TurnPhase.SEMANTIC_SPEAKING
            and assistant_overlap
        ):
            candidate = context.playback.current_fence or (
                frame.captured_fence if frame is not None else None
            )
            if (
                candidate is not None
                and context.conversation_yield_candidate_fence != candidate
            ):
                context.conversation_yield_candidate_fence = candidate
                self.metrics.inc_conversation_yield("candidate")
        if previous_phase is TurnPhase.END_CANDIDATE and current is TurnPhase.SEMANTIC_SPEAKING:
            self.metrics.inc_media_metric(
                "voice_turn_end_candidate_retracted_total",
                labels={"reason": reason},
            )
        if current is TurnPhase.END_CANDIDATE:
            voiced = context.projection.voiced_end_sample
            latest = context.projection.latest_capture_sample
            if voiced is not None and latest >= voiced:
                self.metrics.observe_media_metric(
                    "voice_turn_end_candidate_latency_ms",
                    (latest - voiced) * 1_000 / 16_000,
                )

    def _observe_committed_conversation_turn(
        self,
        context: _MediaVoiceSession,
        committed: CommittedTurn,
        previous_phase: TurnPhase,
    ) -> None:
        """Project one authoritative owner turn into FCDR-style proxies."""

        self._observe_turn_phase(context, previous_phase)
        if not committed.history_eligible:
            return
        duration_ms = (
            committed.capture_end_sample - committed.capture_start_sample
        ) * 1_000 / 16_000
        self.metrics.add_conversation_participation_ms("owner", duration_ms)

    def _observe_conversation_yield_delivery(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
        event: ReplyDeliveryEvent,
    ) -> None:
        """Resolve one semantic-overlap yield proxy at the delivery terminal."""

        candidate = context.conversation_yield_candidate_fence
        if candidate is None or not candidate.matches(fence):
            return
        status = {
            ReplyDeliveryEvent.PREEMPTED: "confirmed",
            ReplyDeliveryEvent.PLAYBACK_ENDED: "continued",
            ReplyDeliveryEvent.TRANSPORT_REJECTED: "indeterminate",
            ReplyDeliveryEvent.ERROR: "indeterminate",
            ReplyDeliveryEvent.SKIPPED: "indeterminate",
            ReplyDeliveryEvent.NO_AUDIO: "indeterminate",
        }.get(event)
        if status is None:
            return
        context.conversation_yield_candidate_fence = None
        self.metrics.inc_conversation_yield(status)
