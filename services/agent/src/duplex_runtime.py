"""Wire Duplex Orchestrator invariants into the voice-core media session path (ch.8–18)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from services.agent.src.classifier_inflight import conversation_close_cache, live_lookup_cache
from services.agent.src.contracts.events import TimedWord
from services.agent.src.contracts.ids import (
    CancellationContext,
    GenerationFence,
    new_session_id,
)
from services.agent.src.event_identity import publish_ui_event
from services.agent.src.generation_records import GenerationRecords
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.observability.tracing import LatencyTrace
from services.agent.src.orchestration.context_snapshot_manager import (
    PendingSnapshot,
)
from services.agent.src.orchestration.emotion import (
    EmotionSmoother,
)
from services.agent.src.orchestration.heard_text_tracker import HeardTextTracker
from services.agent.src.orchestration.interaction_plane import (
    InteractionDecision,
    InteractionEvent,
    InteractionPlane,
    InteractionSnapshot,
)
from services.agent.src.orchestration.interruption_guard import (
    PlaybackInputDecision,
    PlaybackInputGuard,
    normalize_short,
)
from services.agent.src.orchestration.orchestrator import Orchestrator, TTSPoolHandle
from services.agent.src.orchestration.phrase_segmenter import PhraseSegmenter
from services.agent.src.orchestration.prosody import (
    SpeechPlan,
    speech_plan_for_emotion,
)
from services.agent.src.orchestration.speaker_verify import SpeakerVerifier
from services.agent.src.orchestration.speech_epoch_assembler import (
    SpeechEpochAssembler,
)
from services.agent.src.orchestration.speech_timeline import SpeechSegment, SpeechTimeline
from services.agent.src.orchestration.state_machine import ConversationState, InteractionPhase
from services.agent.src.orchestration.turn_revision import TurnRevisionTracker
from services.agent.src.orchestration.utterance_router import (
    InterruptSemanticVerdict,
    UtteranceIntent,
    UtteranceRoute,
)
from services.agent.src.runtime_classifiers import DuplexRuntimeClassifierMixin
from services.agent.src.runtime_context_activation import DuplexRuntimeContextActivationMixin
from services.agent.src.runtime_emotion import DuplexRuntimeEmotionMixin
from services.agent.src.runtime_input_authority import DuplexRuntimeInputAuthorityMixin
from services.agent.src.runtime_post_playback_guard import DuplexRuntimePostPlaybackGuardMixin
from services.agent.src.runtime_profile import VerifiedRuntimeProfile
from services.agent.src.runtime_profile_refresh import DuplexRuntimeProfileRefreshMixin
from services.agent.src.runtime_provenance import DuplexRuntimeProvenanceMixin
from services.agent.src.runtime_provenance import (
    GenerationVoiceSnapshot as GenerationVoiceSnapshot,
)
from services.agent.src.runtime_publication import DuplexRuntimePublicationMixin
from services.agent.src.runtime_shutdown import DuplexRuntimeShutdownMixin
from services.agent.src.runtime_speaker import DuplexSpeakerMixin, _InactiveFormalEnrollment
from services.agent.src.voice_floor import FloorSnapshot, VoiceFloorState
from services.common.companion_response_safety import SAFE_UNKNOWN_REPLY
from services.common.realtime_information import (
    is_incomplete_realtime_reply,
    is_realtime_followup_nudge,
)
from services.speaker.domain import (
    SpeakerDecision,
    is_voice_authority,
)

logger = logging.getLogger(__name__)

ResumeSpeakerBinding = tuple[str, str, int | None, str]


@dataclass(frozen=True, slots=True)
class PendingRealtimeRequest:
    """An unresolved live-information request bound to its speaker scope."""

    query: str
    speaker_scope: Literal["owner", "public"]
    fence: GenerationFence


@dataclass
class ActiveTTSPool(Protocol):
    async def discard_active_connection(self, fence: GenerationFence) -> None: ...


@dataclass
class LiveKitTTSPoolAdapter(TTSPoolHandle):
    """Adapt the active provider pool to the orchestrator cancellation seam."""

    pool: ActiveTTSPool | None = None
    discarded: list[GenerationFence] = field(default_factory=list)

    async def discard_active_connection(self, fence: GenerationFence) -> None:
        self.discarded.append(fence)
        if self.pool is not None:
            await self.pool.discard_active_connection(fence)


@dataclass
class DuplexRuntime(
    DuplexRuntimeClassifierMixin,
    DuplexRuntimeContextActivationMixin,
    DuplexRuntimeInputAuthorityMixin,
    DuplexRuntimeProfileRefreshMixin,
    DuplexRuntimePostPlaybackGuardMixin,
    DuplexRuntimeShutdownMixin,
    DuplexSpeakerMixin,
    DuplexRuntimeProvenanceMixin,
    DuplexRuntimeEmotionMixin,
    DuplexRuntimePublicationMixin,
):
    """Session-scoped orchestration bound to one voice-core media session."""

    orchestrator: Orchestrator
    tts: Any | None = None
    session_id: str = field(default_factory=new_session_id)
    input_guard: PlaybackInputGuard = field(default_factory=PlaybackInputGuard)
    interaction_plane: InteractionPlane = field(default_factory=InteractionPlane)
    barge_in_enabled: bool = True
    capture_release_holdoff_s: float = 0.0
    latency_trace: LatencyTrace = field(default_factory=LatencyTrace)
    emotion_smoother: EmotionSmoother = field(default_factory=EmotionSmoother)
    speech_plan: SpeechPlan = field(default_factory=lambda: speech_plan_for_emotion("neutral"))
    _generation_records: GenerationRecords = field(default_factory=GenerationRecords)
    _voice_floor: VoiceFloorState = field(default_factory=VoiceFloorState)
    use_paralinguistic_tags: bool = False
    speaker_verifier: SpeakerVerifier = field(default_factory=SpeakerVerifier)
    _pending_realtime_request: PendingRealtimeRequest | None = None
    _input_policy_epoch: int = 0
    _capture_release_task: asyncio.Task[Any] | None = None
    _transcript_revisions: TurnRevisionTracker = field(default_factory=TurnRevisionTracker)
    _next_user_prompt_kind: str = "spontaneous"
    _speech_epoch_assembler: SpeechEpochAssembler = field(default_factory=SpeechEpochAssembler)
    # Sample-clock timeline is the migration seam for media runtimes.  The
    # legacy text-matching assembler remains for LiveKit callbacks until the
    # transport starts supplying explicit ranges.
    _speech_timeline: SpeechTimeline = field(default_factory=SpeechTimeline)
    _consumed_canonical_speech_epoch: int | None = None
    _persona_evidence_eligible: bool = False
    _event_publisher: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None = None
    _event_sequence: int = 0
    _evidence_publisher: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None = None
    _owner_turn_publisher: (
        Callable[[dict[str, Any], bytes, int], Coroutine[Any, Any, None]] | None
    ) = None
    _speaker_class: str = "uncertain"
    _speaker_decision: SpeakerDecision | None = None
    _speaker_classifier: Callable[[bytes, int], Awaitable[SpeakerDecision]] | None = None
    _speaker_sample_rate: int = 16000
    _speaker_classify_timeout_s: float = 0.4
    _speaker_epoch: int = 0
    _speaker_pcm: bytearray = field(default_factory=bytearray)
    _speaker_collecting: bool = False
    _speaker_classification_task: asyncio.Task[Any] | None = None
    _mode_policy: ModePolicy = field(default_factory=lambda: ModePolicy.unavailable("not_fetched"))
    _assistant_expression_fence: GenerationFence | None = None
    _target_speaker_focus_enabled: bool = False
    _reject_non_owner_voice: bool = True
    _target_focus_epoch: int | None = None
    _target_focus_pending_epoch: int | None = None
    _device_conversation_controls_enabled: bool = False
    _sticky_interrupt_epoch: int | None = None
    _sticky_interrupt_route: UtteranceRoute | None = None
    _sticky_interrupt_text: str = ""
    _interaction_decision_epoch: int | None = None
    _interaction_decision: InteractionDecision | None = None
    _interaction_decision_text: str = ""
    _last_committed_user_text_normalized: str = ""
    fixed_safety_heard: bool = False
    fixed_safety_echo_text: str = ""
    _paused_reply_available: bool = False
    _reply_speaker_binding: ResumeSpeakerBinding | None = None
    _paused_reply_binding: ResumeSpeakerBinding | None = None
    _pending_semantic_pause_epoch: int | None = None
    _pending_semantic_pause_binding: ResumeSpeakerBinding | None = None
    _resume_pending: bool = False
    _resume_fence: GenerationFence | None = None
    _last_listen_restore_ns: int | None = None
    # After control yield, accept chat turns even if LiveKit omits speech anchors.
    CONTROL_RESTORE_SPEECH_EPOCH_GRACE_MS: int = 20_000
    _closed: bool = False
    _background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    _durable_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    _durable_task_errors: list[BaseException] = field(default_factory=list)
    _evidence_drain_timeout_s: float = 3.0
    RUNTIME_PROFILE_REFRESH_TIMEOUT_S: float = 0.8
    _pending_tool_results: int = 0
    _enroll_fence: GenerationFence | None = None
    _formal_enrollment: _InactiveFormalEnrollment = field(default_factory=_InactiveFormalEnrollment)
    _formal_enrollment_sample_sink: (
        Callable[[bytes, int], Coroutine[Any, Any, None]] | None
    ) = None
    _fast_model_warmer: Callable[[], Awaitable[Any] | Any] | None = None
    _delegation_starter: Callable[[str, GenerationFence], Coroutine[Any, Any, Any] | None] | None = None
    _live_lookup_cache: dict[str, bool] = field(default_factory=live_lookup_cache)
    _live_lookup_semantic_resolver: Callable[[str], Awaitable[bool]] | None = None
    _conversation_close_cache: dict[str, bool] = field(default_factory=conversation_close_cache)
    _conversation_close_semantic_resolver: Callable[[str], Awaitable[bool]] | None = None
    _turn_verdict_key: str | None = None
    _turn_verdict_serial: int = 0
    _interaction_prefetch_epoch: int | None = None
    _interaction_context_prefetch_key: tuple[int, str] | None = None
    _context_prefetch_text: str = ""
    _interaction_warm_epoch: int | None = None
    _interaction_delegated_fences: set[GenerationFence] = field(default_factory=set)
    _pending_context_snapshot: PendingSnapshot | None = None
    _pending_context_snapshot_epoch: int | None = None
    _pending_epoch_drain: GenerationFence | None = None
    _rotation_captured_tasks: list[Any] | None = None
    _context_snapshot_prepare_epoch: int = 0
    _context_snapshot_prepare_task: asyncio.Task[Any] | None = None
    context_snapshot_prepare_timeout_s: float = 1.0
    _emotion_segments_by_turn: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    _voice_profile_refresher: Callable[[], Coroutine[Any, Any, Any]] | None = None
    _runtime_profile_refresher: (
        Callable[[], Coroutine[Any, Any, VerifiedRuntimeProfile | None]] | None
    ) = None
    _voice_profile_refresh_task: asyncio.Task[Any] | None = None

    @classmethod
    def create(
        cls,
        *,
        session_id: str | None = None,
        device_id: str | None = None,
        tts: Any | None = None,
        input_guard_enabled: bool = False,
        barge_in_enabled: bool = True,
        capture_release_holdoff_s: float = 0.0,
        use_paralinguistic_tags: bool = False,
        speaker_verifier: SpeakerVerifier | None = None,
    ) -> DuplexRuntime:
        sid = session_id or new_session_id()
        orch = Orchestrator(session_id=sid, device_id=device_id)
        if tts is not None:
            orch.tts_pool = LiveKitTTSPoolAdapter(pool=tts.pool)
        return cls(
            orchestrator=orch,
            tts=tts,
            session_id=sid,
            input_guard=PlaybackInputGuard(enabled=input_guard_enabled),
            barge_in_enabled=barge_in_enabled,
            capture_release_holdoff_s=capture_release_holdoff_s,
            use_paralinguistic_tags=use_paralinguistic_tags,
            speaker_verifier=speaker_verifier
            if speaker_verifier is not None
            else SpeakerVerifier(enabled=False),
        )

    @property
    def fence(self) -> GenerationFence:
        return self.orchestrator.fence

    def cancellation_context(
        self,
        fence: GenerationFence | None = None,
    ) -> CancellationContext:
        return self.orchestrator.cancellation_context(fence)

    @property
    def interaction_phase(self) -> InteractionPhase:
        return self._voice_floor.phase

    @property
    def floor(self) -> FloorSnapshot:
        """Typed read-only floor view (state machine + phase + playback latch)."""

        return self._voice_floor.snapshot(self.orchestrator.state)

    @property
    def assistant_speaking(self) -> bool:
        return self._voice_floor.assistant_speaking

    @property
    def output_floor_allows_assistant(self) -> bool:
        return self.floor.output_floor_allows_assistant

    def decide_interaction(self, snapshot: InteractionSnapshot) -> InteractionDecision:
        return self.interaction_plane.decide(snapshot)

    def apply_interaction_decision(
        self,
        decision: InteractionDecision,
        *,
        text: str = "",
        fence: GenerationFence | None = None,
    ) -> None:
        if decision.start_prefetch:
            first_prefetch = self._interaction_prefetch_epoch != self._speaker_epoch
            if first_prefetch:
                self._interaction_prefetch_epoch = self._speaker_epoch
                self.refresh_voice_profile()
            query = text.strip()
            context_key = (self._speaker_epoch, query)
            if query and context_key != self._interaction_context_prefetch_key:
                self._interaction_context_prefetch_key = context_key
                self._context_prefetch_text = query
                self._schedule_context_snapshot_prepare()
                self.mark_audio_event("interaction_context_prefetch_started")
            elif first_prefetch:
                self._schedule_context_snapshot_prepare()
            if first_prefetch:
                self.mark_audio_event("interaction_prefetch_started")
        if decision.warm_fast_model and self._interaction_warm_epoch != self._speaker_epoch:
            self._interaction_warm_epoch = self._speaker_epoch
            warmer = self._fast_model_warmer
            if warmer is not None:

                async def _await_warmup() -> None:
                    warmed = warmer()
                    if inspect.isawaitable(warmed):
                        await warmed

                self._spawn(_await_warmup(), name="interaction-fast-model-warmup")
            self.mark_audio_event("interaction_fast_model_warmup")
        if (
            decision.start_delegation
            and fence is not None
            and self.fence.matches(fence)
            and fence not in self._interaction_delegated_fences
        ):
            self._interaction_delegated_fences = {fence}
            starter = self._delegation_starter
            if starter is not None:
                try:
                    started = starter(text, fence)
                except Exception:
                    logger.exception("interaction delegation starter failed")
                else:
                    if started is not None:
                        self._spawn(started, name="interaction-delegation-start")
                        self.mark_audio_event("interaction_delegation_started", fence=fence)

    def playback_guarded_reason(self, text: str, *, duration_ms: int) -> str | None:
        return self.input_guard.guarded_reason(
            text,
            duration_ms=duration_ms,
            assistant_text=self._voice_floor.echo_reference_text,
        )

    async def accept_media_generation(
        self,
        fence: GenerationFence,
        *,
        cause: str = "media_generation_control",
    ) -> bool:
        """Consume the exact generation published by Media Edge."""

        accepted = await self.orchestrator.accept_authoritative_fence(fence, cause=cause)
        if accepted:
            self.mark_audio_event("media_generation_accepted", fence=fence)
        else:
            self.mark_audio_event(
                "media_generation_rejected",
                status="ignored",
                fence=fence,
            )
        return accepted

    async def begin_media_auxiliary_output(
        self,
        expected_fence: GenerationFence,
    ) -> GenerationFence | None:
        """Start one queued audible source after a prior media reply completed."""

        next_fence = await self.orchestrator.begin_auxiliary_output(expected_fence)
        if next_fence is None:
            return None
        self._generation_records.inherit(
            expected_fence,
            next_fence,
            mode_policy=self.mode_policy_for_fence(expected_fence),
            speech_plan=self.speech_plan_for_fence(expected_fence),
        )
        # An explicit preemption leaves the interaction phase at INTERRUPTED
        # even though this source is an admitted assistant output. Move the
        # runtime through THINKING before the OutputWork admission re-check so
        # the floor gate does not discard the rebound source.
        self.set_interaction_phase(InteractionPhase.THINKING_SILENT, cause="media_auxiliary_output")
        self.apply_speech_plan_to_tts(next_fence)
        self._voice_floor.update(pending_assistant_text="", played_assistant_text="")
        self._assistant_expression_fence = None
        return next_fence

    async def preempt_media_output(
        self,
        *,
        cause: str,
        synchronized_transcript: str | None,
    ) -> GenerationFence:
        """Cancel one audible source so a higher-priority source can take over.

        This is an internal arbitration transition, not evidence of new user
        speech. Reuse the generation/history cleanup from the normal interrupt
        path, then reopen the assistant output floor for the replacement work.
        """

        previous = self.fence
        replacement = await self.on_real_interrupt(
            cause=cause,
            create_user_turn=False,
            synchronized_transcript=synchronized_transcript,
            force_generation_bump=True,
        )
        if not replacement.matches(previous):
            self._voice_floor.update(fresh_user_speech=False)
            self.set_interaction_phase(
                InteractionPhase.LISTENING,
                cause=f"media_output_preempt:{cause}",
            )
        return replacement

    @property
    def pending_realtime_request(self) -> PendingRealtimeRequest | None:
        return self._pending_realtime_request

    async def resolve_realtime_request(
        self,
        *,
        fence: GenerationFence,
        direct_text: str | None,
    ) -> tuple[PendingRealtimeRequest | None, bool]:
        """Create or resume a live request without crossing a speaker boundary."""

        query = next(
            (
                turn.content
                for turn in reversed(self.orchestrator.context.turns)
                if turn.role == "user" and turn.content
            ),
            "",
        )
        speaker_scope = self.orchestrator.speaker_scope_for_fence(fence)
        pending = self._pending_realtime_request
        if (
            pending is not None
            and pending.fence.session_id == fence.session_id
            and pending.fence.turn_id + 1 == fence.turn_id
            and pending.speaker_scope == speaker_scope
            and is_realtime_followup_nudge(query)
        ):
            return pending, True
        # Fresh-information intent outranks a planner's static fallback (for
        # example ``我不知道。``). The resolver is the only authority allowed
        # to produce a current weather/news result.
        static_reply_is_fallback = (
            direct_text is None
            or direct_text == SAFE_UNKNOWN_REPLY
            or (
                isinstance(direct_text, str)
                and is_incomplete_realtime_reply(direct_text, query=query)
            )
        )
        if static_reply_is_fallback and await self.resolve_live_lookup_needed(query):
            request = PendingRealtimeRequest(query, speaker_scope, fence)
            self._pending_realtime_request = request
            return request, False
        if pending is not None and pending.speaker_scope == speaker_scope:
            self._pending_realtime_request = None
        return None, False

    def complete_realtime_request(self, request: PendingRealtimeRequest) -> None:
        if self._pending_realtime_request == request:
            self._pending_realtime_request = None

    def speech_plan_for_fence(self, fence: GenerationFence) -> SpeechPlan:
        return self._generation_records.speech_plan_for(fence, self.speech_plan)

    def apply_speech_plan_to_tts(self, fence: GenerationFence) -> None:
        if self.tts is None:
            return
        plan = self.speech_plan_for_fence(fence)
        self.tts.bind_fence(fence)
        apply_plan = getattr(self.tts, "apply_speech_plan", None)
        if callable(apply_plan):
            apply_plan(
                emotion=plan.voice_emotion,
                rate=plan.rate,
                instruction=plan.tts_instruction,
                pitch=plan.pitch,
                reference_contexts=self._generation_records.tts_references_for(fence),
                fence=fence,
            )

    @property
    def heard_tracker(self) -> HeardTextTracker:
        return self.orchestrator.heard_tracker

    @property
    def segmenter(self) -> PhraseSegmenter | None:
        return self.orchestrator.segmenter

    def gate_llm_token(
        self,
        cancellation: GenerationFence | CancellationContext,
        token: str,
    ) -> str | None:
        if self._pending_epoch_drain is not None:
            return None
        return self.orchestrator.gate_llm_token(cancellation, token)

    def gate_tts_audio(
        self,
        cancellation: GenerationFence | CancellationContext,
        pcm: bytes,
    ) -> bytes | None:
        if self._pending_epoch_drain is not None:
            logger.warning(
                "tts gated by pending epoch drain session=%s current_epoch=%s drain_epoch=%s",
                self.session_id,
                self.fence.session_epoch,
                self._pending_epoch_drain.session_epoch,
            )
            return None
        return self.orchestrator.gate_tts_audio(cancellation, pcm)

    def gate_tool_result(
        self,
        cancellation: GenerationFence | CancellationContext,
        payload: Any,
    ) -> Any | None:
        if self._pending_epoch_drain is not None:
            return None
        return self.orchestrator.gate_tool_result(cancellation, payload)

    def set_speaker_classifier(
        self,
        classifier: Callable[[bytes, int], Awaitable[SpeakerDecision]],
        *,
        sample_rate: int,
        timeout_s: float = 0.4,
    ) -> None:
        if sample_rate < 8000 or timeout_s <= 0:
            raise ValueError("speaker classifier requires supported sample rate and timeout")
        self._speaker_classifier = classifier
        self._speaker_sample_rate = sample_rate
        self._speaker_classify_timeout_s = timeout_s

    def set_target_speaker_focus(self, enabled: bool) -> None:
        """Enable target-speaker routing for a formal authority session."""

        self._target_speaker_focus_enabled = enabled

    def set_device_conversation_controls(self, enabled: bool) -> None:
        """Enable hardware-only Router controls such as terminal standby phrases."""

        self._device_conversation_controls_enabled = bool(enabled)

    def set_reject_non_owner_voice(self, reject: bool) -> None:
        self._reject_non_owner_voice = reject

    def set_mode_policy(self, policy: ModePolicy) -> None:
        """Install the sole Control-issued policy before this session starts."""

        if self.fence.turn_id or self.fence.generation_id:
            raise RuntimeError("interaction policy must be frozen before the first turn")
        self._mode_policy = policy
        self.apply_runtime_profile(policy.runtime_profile)
        self._generation_records.bind_mode_policy(self.fence, policy)

    @property
    def mode_policy(self) -> ModePolicy:
        return self._mode_policy

    @property
    def mode_policy_enforced(self) -> bool:
        """False only during construction before an entrypoint freezes policy."""

        return self._mode_policy.unavailable_reason != "not_fetched"

    def mode_policy_for_fence(self, fence: GenerationFence) -> ModePolicy:
        bound = self._generation_records.mode_policy_for(fence)
        return bound if bound is not None else ModePolicy.unavailable("policy_not_bound_to_fence")

    def profile_permits(self, fence: GenerationFence, *, capability: str | None = None) -> bool:
        """Production permission check: valid signed profile for this fence."""

        return self.orchestrator.runtime_profiles.permits(
            fence, current_fence=self.fence, capability=capability
        )

    # Side-effect tools commit through the TaskManager's transactional
    # effect commit port (deep seam); the runtime never writes directly, and
    # the port re-verifies the complete fence + receipt inside one atomic op.

    @property
    def current_speaker_class(self) -> Literal["owner", "guest", "uncertain"]:
        return cast(
            Literal["owner", "guest", "uncertain"],
            self._speaker_class
            if self._speaker_class in {"owner", "guest", "uncertain"}
            else "uncertain",
        )

    @property
    def current_speaker_reason_code(self) -> str:
        decision = self._speaker_decision
        return decision.reason_code if decision is not None else "authority_unavailable"

    @property
    def current_speaker_authority_verified(self) -> bool:
        return self._speaker_decision is not None and is_voice_authority(self._speaker_decision)

    @property
    def current_speaker_decision(self) -> SpeakerDecision:
        return self._speaker_decision or self._uncertain_speaker_decision("authority_unavailable")

    @property
    def context_prefetch_text(self) -> str:
        return self._context_prefetch_text

    @property
    def current_history_eligible(self) -> bool:
        """Snapshot history policy before the next generation fence is bound."""

        return self._current_history_eligible()

    def input_modality_for_fence(self, fence: GenerationFence) -> str:
        return self._generation_records.input_modality_for(fence)

    def _current_history_eligible(self) -> bool:
        """Owner history requires the signed profile + memory_recall_private."""

        decision = self._speaker_decision
        return (
            decision is not None
            and self._mode_policy.history_eligible(
                decision.classification, reason_code=decision.reason_code
            )
            and self.profile_permits(self.fence, capability="memory_recall_private")
        )

    def _current_owner_projection_eligible(self) -> bool:
        """Owner projection requires the signed profile + memory_recall_private."""

        decision = self._speaker_decision
        return (
            decision is not None
            and self._mode_policy.owner_projection_eligible(decision.classification, reason_code=decision.reason_code)
            and self.profile_permits(self.fence, capability="memory_recall_private")
        )

    def is_shadow_speaker(self) -> bool:
        return (
            self._speaker_decision is not None
            and self._speaker_decision.classification == "uncertain"
            and self._speaker_decision.reason_code == "shadow_owner_candidate"
        )


    def set_fast_model_warmer(
        self,
        warmer: Callable[[], Awaitable[Any] | Any] | None,
    ) -> None:
        self._fast_model_warmer = warmer

    def set_delegation_starter(
        self,
        starter: Callable[[str, GenerationFence], Coroutine[Any, Any, Any] | None] | None,
    ) -> None:
        self._delegation_starter = starter

    def set_playback_stop_seam(
        self,
        seam: Callable[[], Awaitable[Any]] | None,
    ) -> None:
        self.orchestrator.playback_stop_seam = seam


    def _spawn(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str,
        durable: bool = False,
    ) -> asyncio.Task[Any]:
        if self._closed:
            # close() is terminal: never schedule work nothing can drain.
            return self.refuse_after_close(coroutine, name)
        task: asyncio.Task[Any] = asyncio.create_task(coroutine, name=name)
        tasks = self._durable_tasks if durable else self._background_tasks
        tasks.add(task)

        def _done(completed: asyncio.Task[Any]) -> None:
            tasks.discard(completed)
            if not completed.cancelled() and (error := completed.exception()) is not None:
                if durable:
                    self._durable_task_errors.append(error)
                logger.error(
                    "duplex background task failed: %s: %s",
                    completed.get_name(),
                    error,
                )

        task.add_done_callback(_done)
        return task

    def _publish(
        self,
        event: dict[str, Any],
        *,
        fence: GenerationFence | None = None,
    ) -> asyncio.Task[Any] | None:
        # §11.5: identity is overwritten from the event's own fence.
        return publish_ui_event(self, event, fence)

    def _assistant_response_blocks_barge_in(self) -> bool:
        return self._voice_floor.fixed_safety_audible or not self.barge_in_enabled and (
            self._voice_floor.assistant_speaking
            or self.orchestrator.state
            in {
                ConversationState.THINKING,
                ConversationState.SPEAKING,
                ConversationState.INTERRUPTION_PENDING,
                ConversationState.TOOL_WAITING,
                ConversationState.RECOVERING,
            }
        )

    def playback_overlap_input_blocked(self) -> bool:
        """True when half-duplex device input must not open or commit a user turn."""

        return self._assistant_response_blocks_barge_in()

    def on_user_voice_started(self, *, now_ns: int | None = None) -> PlaybackInputDecision:
        assistant_response_blocked = self._assistant_response_blocks_barge_in()
        self.refresh_voice_profile()
        self._speaker_epoch += 1
        self._speech_epoch_assembler.start_epoch(self._speaker_epoch)
        self._target_focus_epoch = None
        self._target_focus_pending_epoch = None
        self._sticky_interrupt_epoch = None
        self._sticky_interrupt_route = None
        self._sticky_interrupt_text = ""
        self._pending_semantic_pause_epoch = None
        self._pending_semantic_pause_binding = None
        self._speaker_class = "uncertain"
        self._speaker_decision = self._uncertain_speaker_decision("classification_pending")
        self._speaker_pcm.clear()
        self._speaker_collecting = True
        self._reset_speaker_classification_task()
        self._voice_floor.update(fresh_user_speech=True)
        self.speaker_verifier.mark_utterance_start()
        self.input_guard.start(
            during_playback=self._voice_floor.assistant_speaking or assistant_response_blocked,
            now_ns=now_ns,
        )
        if assistant_response_blocked:
            self.input_guard.candidate_during_playback = True
            self._voice_floor.update(fresh_user_speech=False)
            self._speaker_collecting = False
            self.input_guard.candidate_decision = PlaybackInputDecision.IGNORE
            self.input_guard.candidate_reason = "barge_in_disabled"
            self.mark_audio_event(
                "barge_in_ignored",
                status="ignored",
                detail={"reason": "disabled"},
            )
            return PlaybackInputDecision.IGNORE
        if not self._voice_floor.assistant_speaking:
            self.set_interaction_phase(
                InteractionPhase.USER_SPEAKING,
                cause="vad_start",
            )
        if self._voice_floor.assistant_speaking or self.input_guard.candidate_during_playback:
            # Nearby talker often starts with short energy; require speaker match
            # before treating this as a real barge-in candidate.
            if self.speaker_verifier.active and not self._speaker_allows_user_input(
                context="barge_in_start"
            ):
                self.speaker_verifier.mark_utterance_end()
                # VAD may already have moved SPEAKING → INTERRUPTION_PENDING;
                # dismiss so later commit_turn is not stuck.
                self._spawn(
                    self.orchestrator.dismiss_pending_interruption(cause="speaker_reject_barge_in"),
                    name="dismiss-false-barge",
                )
                return PlaybackInputDecision.IGNORE
            self.orchestrator.metrics.inc_interruption_candidate()
            return PlaybackInputDecision.WAIT
        return PlaybackInputDecision.ACCEPT

    def project_media_user_turn(
        self,
        *,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
    ) -> str | None:
        """Resolve canonical media text without advancing its committed watermark."""

        canonical = self._speech_timeline.projected_text(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        self._consumed_canonical_snapshot_bound = bool(canonical)
        if not canonical:
            return None
        self._consumed_canonical_speech_epoch = self._speaker_epoch
        self._voice_floor.update(fresh_user_speech=False)
        return canonical

    @property
    def consumed_canonical_speech_epoch(self) -> int | None:
        return self._consumed_canonical_speech_epoch

    @property
    def speech_timeline(self) -> SpeechTimeline:
        """Expose the sample-clock migration seam to a media bridge."""

        return self._speech_timeline

    def start_media_stream_epoch(self, stream_epoch: int) -> bool:
        """Fence media events after a reconnect/discontinuity."""

        return self._speech_timeline.start_stream_epoch(stream_epoch)

    def ingest_media_speech_segment(self, segment: SpeechSegment) -> bool:
        """Accept only range-stamped events from the active media epoch."""

        return self._speech_timeline.add(segment)

    def commit_media_speech_range(
        self,
        *,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
    ) -> tuple[SpeechSegment, ...]:
        return self._speech_timeline.commit_range(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )

    def discard_pending_user_transcript(self) -> None:
        """Discard only the current endpoint buffer, never older queued callbacks."""

        self._speech_epoch_assembler.discard_current()
        self.input_guard.candidate_active = False

    def accept_user_turn(
        self,
        text: str,
        *,
        input_modality: Literal["audio", "text"] = "audio",
        speech_anchored: bool | None = None,
        canonical_speech_epoch: int | None = None,
        canonical_snapshot_bound: bool | None = None,
        semantic_verdict: InterruptSemanticVerdict | None = None,
        post_playback_echo_possible: bool = True,
    ) -> tuple[bool, str | None]:
        if self.formal_speaker_enrollment_active:
            self.speaker_verifier.mark_utterance_end()
            return False, "speaker_enrolling"
        self.speaker_verifier.mark_utterance_end()
        self._persona_evidence_eligible = False
        if self.mode_policy_enforced and not self._mode_policy.allows_conversation():
            self.orchestrator.metrics.inc_guarded_user_input("interaction_mode_blocked")
            return False, "interaction_mode_blocked"
        if semantic_verdict is not None and canonical_speech_epoch != self._speaker_epoch:
            self.orchestrator.metrics.inc_guarded_user_input("stale_interrupt_semantic")
            self.mark_audio_event(
                "interrupt_semantic_stale",
                status="ignored",
                detail={
                    "request_speech_epoch": canonical_speech_epoch,
                    "current_speech_epoch": self._speaker_epoch,
                },
            )
            return False, "stale_interrupt_semantic"
        # Single control-plane decision: enroll / pure interrupt / chat.
        # Side effects (early enroll finalize, yield ack) stay here; intent is
        # owned by utterance_router so barge-in and turn-commit cannot diverge.
        route = self._route_candidate(text, semantic_verdict=semantic_verdict)
        interaction = (
            self._interaction_decision
            if self._interaction_decision_epoch == self._speaker_epoch
            and (canonical_speech_epoch is None or canonical_speech_epoch == self._speaker_epoch)
            and normalize_short(self._interaction_decision_text) == normalize_short(text)
            else None
        )
        if interaction is not None and interaction.continue_output:
            self.orchestrator.metrics.inc_guarded_user_input(interaction.reason)
            self.discard_pending_user_transcript()
            self.publish_assistant_audio("restore", gain=1.0)
            return False, interaction.reason
        if (
            route.should_interrupt
            and not route.enter_chat
            and (
                canonical_snapshot_bound is False
                or (
                    canonical_speech_epoch is not None
                    and canonical_speech_epoch != self._speaker_epoch
                )
            )
        ):
            self.orchestrator.metrics.inc_guarded_user_input("stale_control_epoch")
            self.mark_audio_event(
                "control_turn_stale",
                status="ignored",
                detail={
                    "request_speech_epoch": canonical_speech_epoch,
                    "current_speech_epoch": self._speaker_epoch,
                },
            )
            return False, "stale_control_epoch"
        if route.intent is UtteranceIntent.ENROLL:
            # User finished an enroll utterance — try finalize immediately so
            # we do not wait the full wall timeout after they already spoke.
            progress = self.speaker_verifier.enrollment_progress()
            speech_ms = int(progress.get("speech_ms") or 0)
            target_ms = int(progress.get("target_ms") or 2500)
            if speech_ms >= max(1500, int(target_ms * 0.85)):
                early = self.poll_speaker_enrollment()
                if early is not None and early.get("reason") == "enrolled":
                    logger.info(
                        "speaker_enroll early_finalize_on_endpoint speech_ms=%s session_id=%s",
                        speech_ms,
                        self.session_id,
                    )
            self.orchestrator.metrics.inc_guarded_user_input("speaker_enrolling")
            logger.info(
                "user_turn_ignored reason=speaker_enrolling text_len=%s speech_ms=%s session_id=%s",
                len(text),
                speech_ms,
                self.session_id,
            )
            return False, route.reason
        close_turn = route.intent is UtteranceIntent.END_SESSION
        target_route = self._target_speaker_route(
            context="interrupt" if close_turn else "conversation", explicit_interrupt=close_turn)
        if not target_route.allow_input:
            self._reject_target_speaker(context="turn_commit", route=target_route)
            return False, target_route.reason
        if close_turn and self._close_phrase_is_playback_echo(text):
            self.orchestrator.metrics.inc_guarded_user_input("conversation_end_playback_echo")
            self.mark_audio_event("conversation_end_playback_echo", status="ignored")
            return False, "conversation_end_playback_echo"
        if close_turn:
            # The Media Voice registry owns the terminal session projection;
            # runtime only suppresses side effects after the gate authorizes it.
            formal_guest = (
                self.current_speaker_class == "guest"
                or self.current_speaker_reason_code == "owner_mismatch"
            )
            owner_verified_close = (
                self.current_speaker_class == "owner"
                and self.current_speaker_authority_verified
            )
            device_trusted_close = (
                self._device_conversation_controls_enabled and not formal_guest
            )
            if not owner_verified_close and not device_trusted_close:
                self.orchestrator.metrics.inc_guarded_user_input(
                    "conversation_end_owner_unverified"
                )
                self.mark_audio_event(
                    "conversation_end_owner_unverified",
                    status="ignored",
                    detail={"reason_code": self.current_speaker_reason_code},
                )
                return False, "conversation_end_owner_unverified"
            self.input_guard.candidate_text = text
            self.orchestrator.metrics.inc_guarded_user_input(route.reason)
            self.mark_audio_event(
                "conversation_end_turn_suppressed",
                detail={
                    "text_len": len(text),
                    "intent": route.intent,
                    "reason": route.reason,
                },
            )
            self._clear_control_user_turn(
                cause="conversation_end_turn",
                speech_epoch=canonical_speech_epoch,
            )
            return False, route.reason
        if route.should_interrupt and not route.enter_chat:
            # Control-plane routes never become LLM user turns.
            self.input_guard.candidate_text = text
            if (
                route.reason
                in {
                    "interrupt_semantic_control_only",
                    "interrupt_semantic_unsure",
                }
                and canonical_speech_epoch == self._pending_semantic_pause_epoch
                and self._pending_semantic_pause_binding is not None
            ):
                self._paused_reply_binding = self._pending_semantic_pause_binding
                self._paused_reply_available = True
            self._pending_semantic_pause_epoch = None
            self._pending_semantic_pause_binding = None
            self.orchestrator.metrics.inc_guarded_user_input(route.reason)
            logger.info(
                "user_turn_ignored reason=%s intent=%s text_len=%s session_id=%s",
                route.reason,
                route.intent,
                len(text),
                self.session_id,
            )
            self.mark_audio_event(
                "interrupt_command_turn_suppressed",
                detail={
                    "text_len": len(text),
                    "intent": route.intent,
                    "reason": route.reason,
                    "ack_len": len(route.ack_phrase or ""),
                },
            )
            self._clear_control_user_turn(
                cause="interrupt_command_turn",
                speech_epoch=canonical_speech_epoch,
            )
            # Hand floor back: unlock LiveKit min_words so the next real utterance
            # can commit (prod: after 停一下, min_words stuck at 1000 + orphan FINAL).
            self._restore_listen_after_control(cause="interrupt_command_turn")
            return False, route.reason
        if input_modality == "audio" and self.input_guard.enabled and speech_anchored is not None:
            current_vad_has_pcm = (
                canonical_speech_epoch == self._speaker_epoch
                and self._voice_floor.fresh_user_speech
                and bool(self._speaker_pcm)
            )
            # LiveKit can emit a real endpointed final without timing metrics.
            # The current VAD epoch plus its collected PCM is still an
            # authoritative live-speech anchor; a bare orphan final is not.
            assembler_bound = bool(
                canonical_snapshot_bound and canonical_speech_epoch is not None
            )
            missing_anchor = not (
                speech_anchored or current_vad_has_pcm or assembler_bound
            )
            if missing_anchor:
                # Prod: after「停一下」LiveKit often emits orphan FINAL without
                # started/stopped speaking metrics → session goes permanently silent.
                if (
                    route.enter_chat
                    and route.intent in {UtteranceIntent.CHAT, UtteranceIntent.RESUME}
                    and self._within_control_restore_grace()
                    and len(route.normalized_text) >= 2
                ):
                    logger.info(
                        "speech_epoch_fail_open after_control text_len=%s "
                        "anchored=%s fresh=%s session_id=%s",
                        len(route.normalized_text),
                        speech_anchored,
                        self._voice_floor.fresh_user_speech,
                        self.session_id,
                    )
                    self.mark_audio_event(
                        "speech_epoch_fail_open",
                        detail={"reason": "after_control_grace"},
                    )
                    if canonical_speech_epoch in {None, self._speaker_epoch}:
                        self._voice_floor.update(fresh_user_speech=False)
                else:
                    if canonical_speech_epoch in {None, self._speaker_epoch}:
                        self._voice_floor.update(fresh_user_speech=False)
                    self.orchestrator.metrics.inc_guarded_user_input("missing_speech_epoch")
                    return False, "missing_speech_epoch"
            elif canonical_speech_epoch in {None, self._speaker_epoch}:
                self._voice_floor.update(fresh_user_speech=False)
        if (
            input_modality == "audio"
            and self.speaker_verifier.active
            and not self._speaker_allows_user_input(context="turn_commit")
        ):
            return False, "speaker_mismatch"
        post_playback_reason = (
            self.post_playback_guard_reason(text, echo_possible=post_playback_echo_possible)
            if input_modality == "audio"
            else None
        )
        if post_playback_reason is not None:
            self.orchestrator.metrics.inc_guarded_user_input(post_playback_reason)
            return False, post_playback_reason
        if input_modality == "text":
            self.input_guard.candidate_active = False
            self.input_guard.candidate_decision = PlaybackInputDecision.ACCEPT
        accepted, reason = self.input_guard.accept_turn(
            text,
            assistant_text=self._voice_floor.echo_reference_text,
        )
        if not accepted:
            self.orchestrator.metrics.inc_guarded_user_input(reason or "unknown")
        else:
            lexical_chars = sum(char.isalnum() for char in route.normalized_text)
            self._persona_evidence_eligible = (
                route.intent is UtteranceIntent.CHAT
                and lexical_chars >= 8
                and (
                    self._mode_policy.allows_learning(
                        self._speaker_class,  # type: ignore[arg-type]
                        reason_code=(
                            self._speaker_decision.reason_code
                            if self._speaker_decision is not None
                            else None
                        ),
                    )
                    or self._mode_policy.allows_low_sensitivity_persona(
                        is_shadow=self.is_shadow_speaker()
                    )
                )
                and self.profile_permits(self.fence, capability="memory_capture")
            )
            self._resume_pending = route.intent is UtteranceIntent.RESUME
            if route.enter_chat:
                self._paused_reply_available = False
                self._paused_reply_binding = None
                self._pending_semantic_pause_epoch = None
                self._pending_semantic_pause_binding = None
        return accepted, reason

    def route_user_turn(self, text: str) -> UtteranceRoute:
        """Expose the single Router decision to protocol facades."""

        return self._route_candidate(text)

    async def on_turn_committed(
        self,
        user_text: str,
        *,
        input_modality: Literal["audio", "text"] = "audio",
    ) -> GenerationFence:
        # Turn-boundary authority refresh: re-apply the signed profile before
        # any generation starts (never in the background mid-generation).
        await self.refresh_runtime_profile()
        if self._pending_epoch_drain is not None:
            # Epoch rotation barrier: old work drains before new output.
            await self._drain_epoch_rotation()
        self.orchestrator.context_snapshots.seed_or_rebind_if_empty(
            self.session_id,
            first_turn=self.fence.turn_id == 0,
            draft_factory=self._context_snapshot_draft,
            delegation=self.orchestrator.delegation,
        )
        history_eligible = self._current_history_eligible()
        owner_projection_eligible = self._current_owner_projection_eligible()
        next_speaker_scope: Literal["owner", "public"] = (
            "owner" if self._speaker_class == "owner" else "public"
        )
        # A turn boundary is the only activation point; snapshots from a
        # different speaker authority are discarded rather than reused.
        self._activate_pending_context_snapshot()
        if (
            self._pending_realtime_request is not None
            and self._pending_realtime_request.speaker_scope != next_speaker_scope
        ):
            self._pending_realtime_request = None
        self._voice_floor.update(last_playback_completed_ns=None)
        if self.orchestrator.state is ConversationState.CONNECTING:
            await self.orchestrator.ready()
        if self.orchestrator.state is ConversationState.TOOL_WAITING:
            await self.orchestrator.bump_tool_epoch_on_condition_change()
        await self.orchestrator.on_vad_start()
        fence = await self.orchestrator.commit_turn(
            user_text,
            speaker_scope=next_speaker_scope,
        )
        self._apply_speech_plan(user_text, turn_id=fence.turn_id, fence=fence)
        self._last_committed_user_text_normalized = normalize_short(user_text)
        records = self._generation_records
        records.bind_mode_policy(fence, self._mode_policy)
        records.bind_history_eligible(fence, history_eligible)
        records.bind_owner_projection_eligible(fence, owner_projection_eligible)
        records.bind_input_modality(fence, input_modality)
        self._reply_speaker_binding = self._current_resume_speaker_binding()
        self._resume_fence = fence if self._resume_pending else None
        self._resume_pending = False
        self.set_interaction_phase(
            InteractionPhase.THINKING_SILENT,
            cause="turn_committed",
        )
        self.mark_audio_event("turn_committed")
        committed_interaction = self.decide_interaction(
            InteractionSnapshot(
                event=InteractionEvent.TRANSCRIPT,
                assistant_speaking=False,
                text=user_text,
                final=True,
                has_speech_energy=input_modality == "audio",
                semantic_evidence=True,
                utterance_route=self.route_user_turn(user_text),
                turn_committed=True,
            )
        )
        self.apply_interaction_decision(
            committed_interaction,
            text=user_text,
            fence=fence,
        )
        self._schedule_context_snapshot_prepare()
        self._voice_floor.update(pending_assistant_text="", played_assistant_text="")
        self._assistant_expression_fence = None
        return fence

    async def on_assistant_speaking(
        self,
        full_text: str,
        words: list[TimedWord] | tuple[TimedWord, ...] | None = None,
        *,
        expected_fence: GenerationFence | None = None,
        precondition: Callable[[], bool] | None = None,
        publish_state: bool = True,
    ) -> bool:
        def may_publish() -> bool:
            return bool(
                (expected_fence is None or self.fence.matches(expected_fence))
                and (precondition is None or precondition())
            )
        if not may_publish():
            return False
        w = list(words) if words else []
        # Only transition if we are in THINKING (or already SPEAKING is ok to re-enter carefully)
        if self.orchestrator.state is ConversationState.THINKING:
            if not await self.orchestrator.begin_speaking(
                w,
                full_text,
                expected_fence=expected_fence,
                precondition=precondition,
            ):
                return False
        elif self.orchestrator.state is ConversationState.SPEAKING:
            if not may_publish():
                return False
            self.orchestrator.heard_tracker.set_full_text(full_text)
            if w:
                self.orchestrator.heard_tracker.add_words(w)
        else:
            if not may_publish():
                return False
            # Still record tracker data for interrupt truncation.
            self.orchestrator.heard_tracker.set_full_text(full_text)
            if w:
                self.orchestrator.heard_tracker.add_words(w)
        self._voice_floor.update(pending_assistant_text=full_text, assistant_speaking=True)
        self.set_interaction_phase(
            InteractionPhase.SPEAKING,
            cause="assistant_speaking",
            publish=publish_state,
        )
        self._publish_assistant_expression(full_text)
        return True
    async def on_assistant_reply_aborted(
        self,
        fence: GenerationFence,
        *,
        cause: str,
        precondition: Callable[[], bool] | None = None,
    ) -> bool:
        if not await self.orchestrator.abandon_response(
            fence,
            cause=cause,
            precondition=precondition,
        ):
            return False
        self._voice_floor.update(pending_assistant_text="", assistant_speaking=False)
        self._assistant_expression_fence = None
        self.set_interaction_phase(InteractionPhase.LISTENING, cause=cause)
        return True

    async def restore_listen_after_unheard_output(
        self,
        fence: GenerationFence,
        *,
        cause: str,
    ) -> None:
        """Clear the half-duplex speaking latch after TTS that never reached the device.

        Device sessions ignore barge-in while THINKING/SPEAKING. If wake or
        another source calls on_assistant_speaking and then aborts before the
        first Edge-accepted PCM, later user turns would otherwise be swallowed.
        """

        if await self.on_assistant_reply_aborted(fence, cause=cause):
            return
        self._voice_floor.update(pending_assistant_text="", assistant_speaking=False)
        self._assistant_expression_fence = None
        restored = await self.orchestrator.return_to_listening_after_unheard_output(
            fence,
            cause=cause,
        )
        if restored or self.orchestrator.state is ConversationState.LISTENING:
            self.set_interaction_phase(InteractionPhase.LISTENING, cause=cause)

    async def on_media_playback_done(
        self,
        fence: GenerationFence,
        heard_text: str,
        *,
        tools_active: bool = False,
    ) -> bool:
        """Commit exact sample-ACKed media-v1 playback text and arm echo guard.

        Prefer ledger watermark text over LiveKit wall-clock estimates."""

        if not self.fence.matches(fence):
            return False
        normalized = heard_text.strip()
        if self.orchestrator.state is ConversationState.SPEAKING:
            await self.orchestrator.finish_livekit_playback(
                tools_active=tools_active,
                synchronized_transcript=normalized,
                reply_fence=fence,
            )
        elif self.orchestrator.state is ConversationState.THINKING and normalized:
            await self.orchestrator.begin_speaking([], normalized)
            await self.orchestrator.finish_livekit_playback(
                tools_active=tools_active,
                synchronized_transcript=normalized,
                reply_fence=fence,
            )
        else:
            return False
        floor = self._voice_floor
        floor.update(
            played_assistant_text=(
                normalized or floor.pending_assistant_text.strip() or floor.played_assistant_text
            ),
            pending_assistant_text="",
            assistant_speaking=False,
            last_playback_completed_ns=time.monotonic_ns(),
        )
        self.note_fixed_safety_spoken(floor.played_assistant_text)
        if tools_active:
            self.set_interaction_phase(
                InteractionPhase.TOOL_WAITING,
                cause="media_playback_ack",
            )
        else:
            self.set_interaction_phase(InteractionPhase.LISTENING, cause="media_playback_ack")
        return True

    def open_assistant_floor(self, *, cause: str = "assistant_nudge") -> None:
        """Retire a speech placeholder without opening or authorizing a user turn."""

        self._voice_floor.update(fresh_user_speech=False)
        if self.interaction_phase in {
            InteractionPhase.USER_SPEAKING,
            InteractionPhase.INTERRUPTED,
        }:
            self.set_interaction_phase(
                InteractionPhase.LISTENING,
                cause=cause,
            )

    def hold_floor_for_owned_delegation(self) -> None:
        """Keep the half-duplex floor open for one same-turn successor result."""

        self._voice_floor.update(fresh_user_speech=False)
        if self.interaction_phase in {
            InteractionPhase.USER_SPEAKING,
            InteractionPhase.INTERRUPTED,
            InteractionPhase.LISTENING,
        }:
            self.set_interaction_phase(
                InteractionPhase.TOOL_WAITING,
                cause="owned_delegation_hold",
            )

    async def finish_owned_delegation_wait(self, *, cause: str) -> None:
        """Release a tool-wait that will not produce a successor generation."""

        await self.orchestrator.abandon_tool_wait(cause=cause)
        if self.interaction_phase is InteractionPhase.TOOL_WAITING:
            self.set_interaction_phase(InteractionPhase.LISTENING, cause=cause)

    async def on_media_playback_interrupted(
        self,
        *,
        interrupted_from: GenerationFence,
        synchronized_transcript: str | None,
    ) -> str | None:
        """Finalize a media stop using the exact prefix acknowledged so far."""

        finalized = await self.orchestrator.finalize_interrupted_playback(
            interrupted_from=interrupted_from,
            synchronized_transcript=synchronized_transcript,
        )
        if finalized is None:
            return None
        heard, event_fence = finalized
        records = self._generation_records
        records.bind_history_eligible(event_fence, records.history_eligible(interrupted_from))
        records.bind_owner_projection_eligible(
            event_fence, records.owner_projection_eligible(interrupted_from)
        )
        self._voice_floor.update(
            assistant_speaking=False,
            pending_assistant_text="",
            played_assistant_text=heard,
        )
        if heard:
            self.publish_transcript(
                speaker="assistant",
                text=heard,
                final=True,
                heard=True,
                fence=event_fence,
                archive_fence=interrupted_from,
            )
        return heard

    def _clear_control_user_turn(
        self,
        *,
        cause: str,
        speech_epoch: int | None = None,
    ) -> None:
        """Drop one endpoint buffer before the control ack can leak into ASR."""

        target_epoch = self._speaker_epoch if speech_epoch is None else speech_epoch
        if target_epoch != self._speaker_epoch:
            logger.info(
                "stale control user-turn clear skipped control_epoch=%s current_epoch=%s "
                "cause=%s session_id=%s",
                target_epoch,
                self._speaker_epoch,
                cause,
                self.session_id,
            )
            self.mark_audio_event(
                "control_user_turn_clear_skipped",
                status="ignored",
                detail={"cause": cause, "reason": "stale_speech_epoch"},
            )
            return
        self.discard_pending_user_transcript()

    def is_resume_generation(self, fence: GenerationFence | None = None) -> bool:
        """Whether this generation resumes the answer explicitly paused by the user."""

        candidate = fence or self.fence
        return self._resume_fence is not None and self._resume_fence.matches(candidate)

    def _current_resume_speaker_binding(self) -> ResumeSpeakerBinding | None:
        """Return the verified identity allowed to recover this speaker's reply."""

        decision = self._speaker_decision
        if decision is None or not decision.profile_id:
            return None
        if decision.classification == "owner":
            authority = "owner"
        elif (
            decision.classification == "uncertain"
            and decision.reason_code == "shadow_owner_candidate"
        ):
            authority = "shadow_owner_candidate"
        else:
            return None
        return (
            decision.classification,
            decision.profile_id,
            decision.template_version,
            authority,
        )

    def _within_control_restore_grace(self) -> bool:
        if self._last_listen_restore_ns is None:
            return False
        age_ms = (time.monotonic_ns() - self._last_listen_restore_ns) // 1_000_000
        return age_ms <= self.CONTROL_RESTORE_SPEECH_EPOCH_GRACE_MS

    def _restore_listen_after_control(self, *, cause: str) -> None:
        """After stop/wait, unlock turn commit and publish listening.

        Production logs (2026-07-17): after「停一下」yield finished in ~6ms and
        subsequent user speech was dropped as missing_speech_epoch while
        min_words stayed at PLAYBACK_INPUT_BLOCK_MIN_WORDS (1000).
        """
        self.publish_assistant_audio("restore", gain=1.0)
        self._voice_floor.update(assistant_speaking=False)
        self._voice_floor.update(fresh_user_speech=True)
        self._last_listen_restore_ns = time.monotonic_ns()
        # Always force listening after control so UI/logs match.
        if self.interaction_phase is not InteractionPhase.LISTENING:
            self.set_interaction_phase(
                InteractionPhase.LISTENING,
                cause=f"restore_listen:{cause}",
            )
        self.mark_audio_event(
            "listen_restored_after_control",
            detail={"cause": cause},
        )

    async def _apply_real_interrupt(
        self,
        *,
        cause: str,
        stop_playback: Callable[[], Awaitable[str | None]] | None,
        create_user_turn: bool,
        synchronized_transcript: str | None,
        force_generation_bump: bool,
        candidate: str,
        barge_route: UtteranceRoute,
        mid_reply: bool,
    ) -> GenerationFence:
        owner_cmd = barge_route.speaker_gate_override
        control_only = barge_route.should_interrupt and not barge_route.enter_chat
        if owner_cmd and create_user_turn:
            logger.info(
                "explicit_interrupt_cmd text_len=%s intent=%s cause=%s session_id=%s",
                len(candidate),
                barge_route.intent,
                cause,
                self.session_id,
            )
            self.mark_audio_event(
                "explicit_interrupt_cmd",
                detail={
                    "text_len": len(candidate),
                    "cause": cause,
                    "intent": barge_route.intent,
                },
            )
        old_fence = self.fence
        new_fence = await self.orchestrator.confirm_interruption(
            cause=cause,
            stop_playback=stop_playback,
            create_user_turn=create_user_turn,
            synchronized_transcript=synchronized_transcript,
            force_generation_bump=force_generation_bump,
        )
        self._generation_records.bind_mode_policy(new_fence, self._mode_policy)
        self.publish_assistant_audio("restore", gain=1.0)
        if create_user_turn and control_only:
            self._clear_control_user_turn(cause=f"interrupt:{cause}")
        self._voice_floor.update(
            assistant_speaking=False, pending_assistant_text="", last_playback_completed_ns=None
        )
        if self.tts is not None:
            self.tts.bind_fence(new_fence)
        if not new_fence.matches(old_fence):
            if create_user_turn and mid_reply and control_only:
                self._paused_reply_binding = self._reply_speaker_binding
                self._paused_reply_available = self._paused_reply_binding is not None
                self._pending_semantic_pause_epoch = None
                self._pending_semantic_pause_binding = None
            elif (
                create_user_turn
                and mid_reply
                and barge_route.intent is UtteranceIntent.INTERRUPT_THEN_CHAT
            ):
                self._pending_semantic_pause_epoch = self._speaker_epoch
                self._pending_semantic_pause_binding = self._reply_speaker_binding
                self._paused_reply_available = False
                self._paused_reply_binding = None
            elif create_user_turn:
                self._paused_reply_available = False
                self._paused_reply_binding = None
                self._pending_semantic_pause_epoch = None
                self._pending_semantic_pause_binding = None
            self.publish_assistant_state("interrupted")
            self.set_interaction_phase(
                InteractionPhase.INTERRUPTED,
                cause=f"interrupt:{cause}",
            )
            # A committed close owns the projection and keeps the floor; a
            # mid-reply control-only stop leaves the floor as the interrupt set it.
            yielded_mid_reply = (
                create_user_turn
                and mid_reply
                and control_only
                and cause not in {"user_button", "stop_response", "rtc_recovered"}
            )
            if barge_route.intent is not UtteranceIntent.END_SESSION and not yielded_mid_reply:
                self._restore_listen_after_control(cause=f"interrupt_no_yield:{cause}")
        return new_fence

    async def on_real_interrupt(
        self,
        cause: str = "livekit_interruption",
        *,
        stop_playback: Callable[[], Awaitable[str | None]] | None = None,
        create_user_turn: bool = True,
        synchronized_transcript: str | None = None,
        force_generation_bump: bool = False,
        candidate_text: str | None = None,
        utterance_route: UtteranceRoute | None = None,
    ) -> GenerationFence:
        mid_reply = self._voice_floor.assistant_speaking
        candidate = (
            candidate_text.strip()
            if candidate_text is not None
            else self._interrupt_candidate_text()
        )
        # Barge-in uses the same router as turn-commit so speaker-reject recover
        # cannot fire on pure「停一下」while accept_user_turn treats it as control.
        barge_route = utterance_route or self._route_candidate(candidate)
        owner_cmd = barge_route.speaker_gate_override
        if (
            create_user_turn
            and self._target_speaker_focus_enabled
            and self._speaker_classifier is not None
        ):
            if owner_cmd and self._target_focus_epoch == self._speaker_epoch:
                # A router-confirmed「等一下 / 停一下」must release playback
                # before ASR endpointing finishes. This path is reached only
                # once this speech epoch's voice was already confirmed; an
                # unconfirmed VAD interrupt still waits for endpointed audio
                # below.
                target_route = self._target_speaker_route(
                    context="interrupt",
                    explicit_interrupt=barge_route.intent
                    in {UtteranceIntent.INTERRUPT_COMMAND, UtteranceIntent.END_SESSION})
            else:
                # LiveKit may request an interrupt on VAD start. Do not classify
                # the first few PCM frames: the final transcript handler will
                # decide against the complete endpointed utterance instead.
                if self._speaker_collecting:
                    self._target_focus_pending_epoch = self._speaker_epoch
                    self.mark_audio_event(
                        "target_speaker_waiting_for_endpoint",
                        detail={"cause": cause},
                    )
                    return self.fence
                try:
                    await self.await_speaker_classification()
                except asyncio.CancelledError:
                    return self.fence
                # Unclassified utterance: only a device farewell may pass.
                target_route = self._target_speaker_route(
                    context="interrupt",
                    explicit_interrupt=barge_route.intent is UtteranceIntent.END_SESSION)
            if self._target_focus_pending_epoch == self._speaker_epoch:
                self._target_focus_pending_epoch = None
            if not target_route.allow_input:
                self.speaker_verifier.mark_utterance_end()
                await self.orchestrator.dismiss_pending_interruption(
                    cause="target_speaker_reject_interrupt"
                )
                self._reject_target_speaker(context="interrupt", route=target_route)
                return self.fence
            self._target_focus_epoch = self._speaker_epoch
        if (
            create_user_turn
            and self.speaker_verifier.active
            and not self._speaker_allows_user_input(context="interrupt")
            and not owner_cmd
        ):
            # Nearby talker: do not bump fence — but LiveKit may already have
            # stopped audio, so recover instead of dead silence.
            # Never recover when user said「停一下」etc. — that is a real yield.
            self.speaker_verifier.mark_utterance_end()
            await self.orchestrator.dismiss_pending_interruption(cause="speaker_reject_interrupt")
            self.mark_audio_event(
                "interrupt_blocked_by_speaker",
                status="ignored",
                detail={
                    "cause": cause,
                    "mid_reply": mid_reply,
                    "candidate_len": len(candidate),
                    "intent": barge_route.intent,
                },
            )
            return self.fence

        return await self._apply_real_interrupt(
            cause=cause,
            stop_playback=stop_playback,
            create_user_turn=create_user_turn,
            synchronized_transcript=synchronized_transcript,
            force_generation_bump=force_generation_bump,
            candidate=candidate,
            barge_route=barge_route,
            mid_reply=mid_reply,
        )

    async def close(self) -> None:
        if self.begin_close():
            return
        speaker_task = self._speaker_classification_task
        if speaker_task is not None and not speaker_task.done():
            await asyncio.wait(
                {speaker_task},
                timeout=self._speaker_classify_timeout_s + 0.1,
            )
        self._evidence_publisher = None
        await self.close_classifier_calls()
        await self.drain_durable_tasks()
        await self.drain_background_tasks()
        await self.orchestrator.close()
        await self.drain_background_tasks()
        if self._durable_task_errors:
            raise RuntimeError("one or more durable evidence tasks failed") from (
                self._durable_task_errors[0]
            )
