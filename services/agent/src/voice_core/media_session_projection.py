"""Runtime event, delegation and Projection coordination for Media Voice."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from services.agent.src.agent_voice_profile import generation_tts_voice_can_bind
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
from services.agent.src.prompts import (
    BRIDGE_PHRASES,
    LIVE_LOOKUP_FILLER,
    device_wake_phrase,
    hours_since_device_wake,
    remember_device_wake,
)
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
from services.common.realtime_information import current_local_time

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry

logger = logging.getLogger(__name__)

_MISSED_HEARING_NUDGE_COOLDOWN_S = 12.0
_MAX_MISSED_HEARING_NUDGES = 2
_OUTPUT_IDLE_TIMEOUT_S = 30.0
# Duplicate ASR finals for one question arrive within a few seconds, so a
# filler heard inside this window belongs to the same lookup burst.  Beyond it
# a later question may legitimately prefix its own absent acknowledgement.
_LIVE_LOOKUP_FILLER_MEMORY_S = 30.0
# Deciding whether to *play* another acknowledgement needs a much tighter
# window than stripping a result prefix: within one burst the sibling delegations
# decide within a second or two, while a later question only arrives after its
# predecessor's answer has been spoken.  Reusing the 30s burst window here
# swallowed the next question's acknowledgement (epoch 1900).
_LIVE_LOOKUP_FILLER_ACK_REPEAT_S = 5.0
media_pb2: Any = _media_pb2


def _strip_leading_live_lookup_filler(spoken: str) -> str:
    text = spoken.lstrip()
    if text.startswith(LIVE_LOOKUP_FILLER):
        return text[len(LIVE_LOOKUP_FILLER) :].lstrip()
    return spoken


def _same_turn_fence(left: GenerationFence, right: GenerationFence) -> bool:
    return (
        left.session_id == right.session_id
        and left.session_epoch == right.session_epoch
        and left.turn_id == right.turn_id
    )


def _live_lookup_filler_was_heard(context: _MediaVoiceSession, fence: GenerationFence) -> bool:
    owner = context.output_owner
    if owner is not None and int(getattr(owner.intent, "kind", 0)) == int(
        media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT
    ):
        delivery = context.reply_delivery.get(owner.fence)
        if delivery is not None and delivery.first_frame_sent:
            return True
        if context.playback.rendered_sample_end(owner.fence) > 0:
            return True
        if context.playback.actual_heard_text(owner.fence):
            return True
    if LIVE_LOOKUP_FILLER in context.playback.actual_heard_text(fence):
        return True
    for snapshot in context.reply_delivery.snapshots():
        if not _same_turn_fence(snapshot.key.fence, fence):
            continue
        if snapshot.first_frame_sent or snapshot.actual_heard:
            return True
    for result in context.output_results:
        if not _same_turn_fence(result.fence, fence):
            continue
        if result.emitted_audio:
            return True
    delivery = context.reply_delivery.get(fence)
    return bool(delivery is not None and (delivery.first_frame_sent or delivery.actual_heard))


def _remember_live_lookup_filler(context: _MediaVoiceSession, fence: GenerationFence) -> None:
    """Record the fence of the lookup acknowledgement this session admitted."""

    context.live_lookup_filler_fence = fence
    context.live_lookup_filler_admitted_at = time.monotonic()


def _live_lookup_filler_already_audible(
    context: _MediaVoiceSession,
    fence: GenerationFence,
    *,
    window_s: float = _LIVE_LOOKUP_FILLER_MEMORY_S,
) -> bool:
    """True when this session already made the lookup filler audible.

    A sibling delegation cannot see the acknowledgement its predecessor played,
    so without this session-scoped check a re-committed question prefixes the
    deep result with a filler the user just heard (epoch 1897).
    """

    remembered = context.live_lookup_filler_fence
    admitted_at = context.live_lookup_filler_admitted_at
    if remembered is None or admitted_at is None:
        return False
    if time.monotonic() - admitted_at > window_s:
        return False
    if remembered.session_id != fence.session_id:
        return False
    if remembered.session_epoch != fence.session_epoch:
        return False
    return _live_lookup_filler_was_heard(context, remembered)


def _forget_live_lookup_filler(context: _MediaVoiceSession) -> None:
    """Release the burst memory once a lookup delivered its answer.

    The memory only exists to coalesce sibling delegations opened by duplicate
    finals of one question, so a delivered answer ends the burst and the next
    lookup is allowed to announce itself again.
    """

    context.live_lookup_filler_fence = None
    context.live_lookup_filler_admitted_at = None


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

        def _pause_owner_silence_timer(self, context: _MediaVoiceSession) -> None: ...

        async def _request_device_standby(
            self, context: _MediaVoiceSession, *, reason: str
        ) -> bool: ...

        def _arm_owner_silence_timer(
            self, context: _MediaVoiceSession, *, reset: bool
        ) -> None: ...

    async def _speak_device_wake_ack(self, context: _MediaVoiceSession) -> None:
        if context.identity.client_type != "device" or not callable(
            getattr(context.provider, "generate_output", None)
        ):
            context.device_wake_ack_pending = False
            return
        now = current_local_time(os.getenv("MEMORIA_TIMEZONE", "Asia/Shanghai"))
        try:
            if context.closed or context.standby_requested:
                context.device_wake_ack_pending = False
                return
            if context.runtime.fence.turn_id != 0 or context.turn_start_sample is not None:
                context.device_wake_ack_pending = False
                return
            if context.output_owner is not None:
                context.device_wake_ack_pending = False
                return
            runtime = context.runtime
            if runtime.orchestrator.state is ConversationState.CONNECTING:
                await runtime.orchestrator.ready()
                runtime.set_interaction_phase(
                    InteractionPhase.LISTENING,
                    cause="device_wake_ack",
                )
            if (
                context.closed
                or context.runtime.fence.turn_id != 0
                or context.turn_start_sample is not None
            ):
                context.device_wake_ack_pending = False
                return
            generation_tts_voice_can_bind(runtime)
            policy = runtime.mode_policy
            companion_style_id = policy.companion_style_id if policy.available else None
            spoken = await self._speak_allowlisted_bridge_phrase(
                context,
                device_wake_phrase(
                    context.identity.session_id,
                    now=now,
                    weather_label=None,
                    companion_style_id=companion_style_id,
                    hours_since_last_wake=hours_since_device_wake(
                        context.identity.device_id, now
                    ),
                ),
                require_idle_input=True,
            )
            if spoken:
                context.device_wake_ack_fence = runtime.fence
            else:
                context.device_wake_ack_fence = None
                context.device_wake_ack_pending = False
        except asyncio.CancelledError:
            context.device_wake_ack_pending = False
            raise
        except Exception:
            context.device_wake_ack_pending = False
            logger.exception(
                "device wake ack failed session=%s",
                context.identity.session_id,
            )
        finally:
            remember_device_wake(context.identity.device_id, now)

    def _spawn_device_speaker_enrollment(self, context: _MediaVoiceSession) -> None:
        if context.identity.client_type != "device":
            return
        enabled = os.environ.get("MEMORIA_SPEAKER_AUTHORITY_ENABLED", "").strip().lower()
        if enabled not in {"1", "true", "yes"}:
            return
        context.speaker_enrollment_task = asyncio.create_task(
            self._run_device_speaker_enrollment(context),
            name=f"device-speaker-enrollment-{context.identity.session_id}",
        )

    def _media_output_is_busy(self, context: _MediaVoiceSession) -> bool:
        task = context.output_dispatch_task
        return (
            context.output_owner is not None
            or context.runtime.assistant_speaking
            or (task is not None and not task.done())
        )

    async def _wait_for_output_idle(
        self,
        context: _MediaVoiceSession,
        *,
        timeout_s: float = _OUTPUT_IDLE_TIMEOUT_S,
        wait_for_start: bool = False,
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        seen_busy = self._media_output_is_busy(context)
        while loop.time() < deadline:
            if context.closed or context.standby_requested:
                return False
            busy = self._media_output_is_busy(context)
            if busy:
                seen_busy = True
            elif seen_busy:
                return True
            elif not wait_for_start:
                return True
            await asyncio.sleep(0.05)
        return bool(seen_busy and not self._media_output_is_busy(context))

    async def _speak_device_enrollment_phrase(
        self,
        context: _MediaVoiceSession,
        phrase: str,
    ) -> bool:
        context.runtime.open_assistant_floor_for_nudge()
        spoken = await self._speak_allowlisted_bridge_phrase(
            context,
            phrase,
            require_idle_input=False,
        )
        if not spoken:
            return False
        return await self._wait_for_output_idle(context, wait_for_start=True)

    async def _run_device_speaker_enrollment(self, context: _MediaVoiceSession) -> None:
        from services.agent.src.orchestration.formal_speaker_enrollment import (
            run_formal_speaker_enrollment,
        )
        from services.agent.src.prompts import (
            SPEAKER_ENROLLMENT_DONE_PHRASE,
            SPEAKER_ENROLLMENT_INCOMPLETE_PHRASE,
            SPEAKER_ENROLLMENT_NEED_CONSENT_PHRASE,
        )
        from services.agent.src.speaker_authority_client import (
            load_device_enrollment_status,
            speaker_authority_client_from_settings,
        )

        paused_silence = False
        try:
            status = await load_device_enrollment_status(
                session_id=context.identity.session_id,
            )
            if context.closed or context.standby_requested:
                return
            enrollment = (status or {}).get("enrollment") or {}
            state = str(enrollment.get("state") or "")
            intent_id = str(enrollment.get("intent_id") or "")
            if state not in {"requested", "required"}:
                logger.info(
                    "device speaker enrollment skipped session=%s state=%s intent=%s",
                    context.identity.session_id,
                    state or "unavailable",
                    "yes" if intent_id else "no",
                )
                return
            if not intent_id:
                logger.info(
                    "device speaker enrollment skipped session=%s state=%s intent=no",
                    context.identity.session_id,
                    state,
                )
                self._pause_owner_silence_timer(context)
                paused_silence = True
                wait_for_wake = context.device_wake_ack_fence is not None
                if not await self._wait_for_output_idle(
                    context,
                    wait_for_start=wait_for_wake,
                ):
                    return
                await self._speak_device_enrollment_phrase(
                    context,
                    SPEAKER_ENROLLMENT_NEED_CONSENT_PHRASE,
                )
                await self._request_device_standby(
                    context,
                    reason="speaker_enrollment_needs_consent",
                )
                return
            authority = speaker_authority_client_from_settings()
            if authority is None:
                logger.info(
                    "device speaker enrollment skipped session=%s reason=authority_disabled",
                    context.identity.session_id,
                )
                return
            self._pause_owner_silence_timer(context)
            paused_silence = True
            wait_for_wake = context.device_wake_ack_fence is not None
            if not await self._wait_for_output_idle(
                context,
                wait_for_start=wait_for_wake,
            ):
                logger.warning(
                    "device speaker enrollment missed idle floor session=%s",
                    context.identity.session_id,
                )
                return

            async def _speak(text: str) -> None:
                if not await self._speak_device_enrollment_phrase(context, text):
                    raise RuntimeError("enrollment prompt was not spoken")

            logger.info(
                "device speaker enrollment started session=%s intent=%s",
                context.identity.session_id,
                intent_id,
            )
            result = await run_formal_speaker_enrollment(
                runtime=context.runtime,
                speak=_speak,
                authority=authority,
                intent_id=intent_id,
            )
            if context.closed or context.standby_requested:
                return
            failed = str(result.get("status") or "") == "failed" or result.get("reason") in {
                "sample_timeout",
                "authority_error",
            }
            await self._speak_device_enrollment_phrase(
                context,
                SPEAKER_ENROLLMENT_INCOMPLETE_PHRASE
                if failed
                else SPEAKER_ENROLLMENT_DONE_PHRASE,
            )
            if failed:
                await self._request_device_standby(
                    context,
                    reason="speaker_enrollment_incomplete",
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "device speaker enrollment failed session=%s",
                context.identity.session_id,
                exc_info=True,
            )
        finally:
            if (
                paused_silence
                and not context.closed
                and not context.standby_requested
            ):
                self._arm_owner_silence_timer(context, reset=True)

    def _owner_speech_is_established(self, context: _MediaVoiceSession) -> bool:
        """True only when this turn carries verified owner authority.

        An empty ASR result proves nothing about who produced the audio. On the
        current single-mic board there is no playback AEC reference, so the
        assistant's own TTS tail and ambient room noise both reach the mic and
        both endpoint as turns with no text. Speaker classification runs on the
        captured PCM independently of ASR text, so it is the only evidence that
        separates "the owner spoke and ASR failed" from "something else made
        noise". Without it the device would answer the room.
        """

        runtime = context.runtime
        return (
            runtime.current_speaker_class == "owner"
            and runtime.current_speaker_authority_verified
        )

    def _nudge_missed_hearing(
        self,
        context: _MediaVoiceSession,
        *,
        endpoint_sample: int | None = None,
    ) -> None:
        if context.closed or context.standby_requested:
            return
        if context.identity.client_type != "device":
            return
        if self._is_wake_echo_discard(context):
            return
        if context.runtime.assistant_speaking:
            context.pending_missed_hearing_nudge = True
            return
        effective_endpoint = (
            endpoint_sample
            if endpoint_sample is not None
            else (context.turn_endpoint_sample or 0)
        )
        if effective_endpoint <= 0:
            return
        if not self._owner_speech_is_established(context):
            return
        now = time.monotonic()
        last = context.last_missed_hearing_nudge_at
        if last is not None and now - last < _MISSED_HEARING_NUDGE_COOLDOWN_S:
            return
        if context.missed_hearing_nudge_count >= _MAX_MISSED_HEARING_NUDGES:
            request_standby = getattr(self, "_request_device_standby", None)
            if callable(request_standby):
                asyncio.create_task(
                    request_standby(context, reason="missed_hearing_loop"),
                    name=f"missed-hearing-standby-{context.identity.session_id}",
                )
            return
        self._schedule_missed_hearing_nudge(context)

    def _is_wake_echo_discard(self, context: _MediaVoiceSession) -> bool:
        wake_fence = context.device_wake_ack_fence
        if wake_fence is None:
            return False
        return context.runtime.assistant_speaking and context.runtime.fence.matches(
            wake_fence
        )

    def _schedule_missed_hearing_nudge(self, context: _MediaVoiceSession) -> None:
        context.missed_hearing_nudge_count += 1
        context.last_missed_hearing_nudge_at = time.monotonic()
        asyncio.create_task(
            self._speak_missed_hearing_ack(context),
            name=f"missed-hearing-{context.identity.session_id}",
        )

    def flush_pending_missed_hearing_nudge(self, context: _MediaVoiceSession) -> None:
        if not context.pending_missed_hearing_nudge:
            return
        context.pending_missed_hearing_nudge = False
        if context.closed or context.standby_requested:
            return
        if context.runtime.assistant_speaking:
            context.pending_missed_hearing_nudge = True
            return
        if (context.turn_endpoint_sample or 0) <= 0:
            return
        if not self._owner_speech_is_established(context):
            return
        self._schedule_missed_hearing_nudge(context)

    def clear_device_wake_ack_fence(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> None:
        wake_fence = context.device_wake_ack_fence
        if wake_fence is not None and wake_fence.matches(fence):
            context.device_wake_ack_fence = None
            context.device_wake_ack_pending = False

    async def _speak_missed_hearing_ack(self, context: _MediaVoiceSession) -> None:
        try:
            context.runtime.open_assistant_floor_for_nudge()
            echo_fence = context.runtime.fence
            context.device_wake_ack_fence = echo_fence
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
            played_lookup_filler = False
            task_done, _pending = await asyncio.wait(
                {handle.record.task},
                timeout=0.02,
            )
            if (
                not task_done
                and runtime.fence.matches(fence)
                and not _live_lookup_filler_already_audible(
                    context,
                    fence,
                    window_s=_LIVE_LOOKUP_FILLER_ACK_REPEAT_S,
                )
            ):
                now_ms = int(time.time() * 1_000)
                acknowledgement = coordinator.bridge_acknowledgement(
                    LIVE_LOOKUP_FILLER,
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
                    _remember_live_lookup_filler(context, fence)
                    played_lookup_filler = await self._enqueue_output_work(
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
            spoken = str(getattr(intent, "tts_source", "") or "")
            filler_started = (
                played_lookup_filler and _live_lookup_filler_was_heard(context, fence)
            ) or _live_lookup_filler_already_audible(context, fence)
            if filler_started:
                intent.tts_source = _strip_leading_live_lookup_filler(spoken)
            elif spoken and not spoken.startswith(LIVE_LOOKUP_FILLER):
                intent.tts_source = LIVE_LOOKUP_FILLER + spoken
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
            _forget_live_lookup_filler(context)
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
