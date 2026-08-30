"""Wire Duplex Orchestrator invariants into the LiveKit AgentSession path (ch.8–18)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

from services.agent.src.contracts.events import TimedWord
from services.agent.src.contracts.ids import (
    CancellationContext,
    GenerationFence,
    new_session_id,
)
from services.agent.src.cue_playback import (
    cancel_listener_cue,
    cancel_listener_cue_candidate,
    publish_listener_cue,
    schedule_listener_cue,
    stop_cue_handle,
)
from services.agent.src.event_identity import (
    archive_evidence,
    evidence_fingerprint,
    input_policy_for_state,
    join_ui_publishes,
    phase_for_published_state,
    preview_provenance,
    publish_ui_event,
    transcript_delta_event,
)
from services.agent.src.generation_output_policy import generation_voice_allowed
from services.agent.src.identity_state import (
    capture_identity_tasks,
    clear_identity_private_state,
    drain_epoch_rotation,
    invalidate_identity_epochs,
)
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.observability.audio_trace import parse_client_audio_trace
from services.agent.src.observability.tracing import LatencyTrace
from services.agent.src.orchestration.context_snapshot_manager import (
    ContextSnapshot,
    ContextSnapshotDraft,
    ContextTurn,
    MemoryCapsule,
    PendingSnapshot,
    PersonaCapsule,
    scope_context_snapshot_draft,
)
from services.agent.src.orchestration.cue_scheduler import CueScheduler, ListenerCue
from services.agent.src.orchestration.emotion import (
    EmotionObservation,
    EmotionSmoother,
    aggregate_acoustic_segments,
)
from services.agent.src.orchestration.formal_speaker_enrollment import (
    FormalSpeakerEnrollment,
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
    interrupt_ack_phrase,
    normalize_short,
)
from services.agent.src.orchestration.orchestrator import Orchestrator, TTSPoolHandle
from services.agent.src.orchestration.phrase_segmenter import PhraseSegmenter
from services.agent.src.orchestration.prosody import (
    SpeechPlan,
    mascot_expression_for_reply,
    speech_plan_for_emotion,
    speech_plan_for_turn,
)
from services.agent.src.orchestration.speaker_verify import (
    SpeakerGateState,
    SpeakerVerifier,
)
from services.agent.src.orchestration.speech_epoch_assembler import (
    AssembledUserTurn,
    SpeechEpochAssembler,
)
from services.agent.src.orchestration.speech_timeline import SpeechSegment, SpeechTimeline
from services.agent.src.orchestration.state_machine import ConversationState, InteractionPhase
from services.agent.src.orchestration.turn_revision import TurnRevisionTracker
from services.agent.src.orchestration.utterance_router import (
    InterruptSemanticVerdict,
    UtteranceIntent,
    UtteranceRoute,
    route_speaker_gate,
    route_utterance,
)
from services.agent.src.output_provenance import (
    mode_policy_provenance,
    owner_acoustic_evidence,
    speaker_persona_provenance,
)
from services.agent.src.runtime_profile import VerifiedRuntimeProfile
from services.agent.src.runtime_speaker import (
    PLAYBACK_INPUT_BLOCK_MIN_WORDS,
    DuplexSpeakerMixin,
    KeywordSpotterBinding,
)
from services.common.companion_response_safety import SAFE_UNKNOWN_REPLY
from services.common.evidence_policy import classify_prompt_kind
from services.common.realtime_information import (
    is_incomplete_realtime_reply,
    is_realtime_followup_nudge,
    requires_realtime_lookup,
)
from services.common.redaction import redact_pii
from services.speaker.domain import (
    SpeakerDecision,
    SpeakerPermissions,
    permissions_for_speaker,
)

logger = logging.getLogger(__name__)

ResumeSpeakerBinding = tuple[str, str, int | None, str]
POST_PLAYBACK_BACKCHANNEL_GUARD_MS = 800
POST_PLAYBACK_ECHO_GUARD_MS = 10_000
HISTORY_ELIGIBILITY_MAX_FENCES = 32
RESPONSE_PROVENANCE_MAX_BYTES = 16 * 1024
RESPONSE_PROVENANCE_MAX_FENCES = 32
_RESPONSE_PROVENANCE_FORBIDDEN_KEYS = frozenset(
    {
        "query",
        "prompt",
        "instructions",
        "content",
        "excerpt",
        "score",
        "quality_score",
        "embedding",
        "audio",
        "token",
        "cookie",
    }
)




@dataclass(frozen=True, slots=True)
class GenerationVoiceSnapshot:
    """Applied TTS identity frozen to one exact generation fence."""

    profile_id: str | None
    resource_id: str
    speaker_sha256: str
    voice_kind: Literal["designed", "personal"]


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
class DuplexRuntime(DuplexSpeakerMixin):
    """Session-scoped orchestration bound to LiveKit agent lifecycle."""

    orchestrator: Orchestrator
    tts: Any | None = None
    session_id: str = field(default_factory=new_session_id)
    input_guard: PlaybackInputGuard = field(default_factory=PlaybackInputGuard)
    interaction_plane: InteractionPlane = field(default_factory=InteractionPlane)
    trusted_aec_playback_control: bool = False
    barge_in_enabled: bool = True
    capture_release_holdoff_s: float = 0.0
    latency_trace: LatencyTrace = field(default_factory=LatencyTrace)
    cue_scheduler: CueScheduler = field(default_factory=CueScheduler)
    emotion_smoother: EmotionSmoother = field(default_factory=EmotionSmoother)
    speech_plan: SpeechPlan = field(default_factory=lambda: speech_plan_for_emotion("neutral"))
    _speech_plans_by_fence: dict[GenerationFence, SpeechPlan] = field(default_factory=dict)
    _tts_references_by_fence: dict[GenerationFence, tuple[str, ...]] = field(default_factory=dict)
    interaction_phase: InteractionPhase = InteractionPhase.CONNECTING
    use_paralinguistic_tags: bool = False
    speaker_verifier: SpeakerVerifier = field(default_factory=SpeakerVerifier)
    _unsubscribers: list[Callable[[], None]] = field(default_factory=list)
    _was_speaking: bool = False
    _pending_assistant_text: str = ""
    _pending_realtime_request: PendingRealtimeRequest | None = None
    _pending_assistant_text_epoch: int = 0
    _input_policy_epoch: int = 0
    _capture_blocked: bool = False
    _capture_release_task: asyncio.Task[Any] | None = None
    _phase_listener: Callable[[InteractionPhase, InteractionPhase], None] | None = None
    _transcript_revisions: TurnRevisionTracker = field(default_factory=TurnRevisionTracker)
    _played_assistant_text: str = ""
    _next_user_prompt_kind: str = "spontaneous"
    _fresh_user_speech: bool = False
    _speech_epoch_assembler: SpeechEpochAssembler = field(default_factory=SpeechEpochAssembler)
    # Sample-clock timeline is the migration seam for media runtimes.  The
    # legacy text-matching assembler remains for LiveKit callbacks until the
    # transport starts supplying explicit ranges.
    _speech_timeline: SpeechTimeline = field(default_factory=SpeechTimeline)
    _consumed_canonical_speech_epoch: int | None = None
    _consumed_canonical_snapshot_bound: bool = False
    _persona_evidence_eligible: bool = False
    _playback_fence: GenerationFence | None = None
    _last_playback_completed_ns: int | None = None
    _set_interruption_min_words: Callable[[int], None] | None = None
    _base_interruption_min_words: int = 0
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
    _trusted_playback_pcm: bytearray = field(default_factory=bytearray)
    _trusted_playback_witness_pcm: bytes = b""
    _trusted_playback_witness_ns: int | None = None
    _speaker_collecting: bool = False
    _speaker_classification_task: asyncio.Task[Any] | None = None
    _playback_epoch: int = 0
    _playback_control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _history_eligible_by_fence: dict[GenerationFence, bool] = field(default_factory=dict)
    _owner_projection_eligible_by_fence: dict[GenerationFence, bool] = field(default_factory=dict)
    _input_modality_by_fence: dict[GenerationFence, str] = field(default_factory=dict)
    _text_only_delivery: bool = False
    _mode_policy: ModePolicy = field(default_factory=lambda: ModePolicy.unavailable("not_fetched"))
    _mode_policy_by_fence: dict[GenerationFence, ModePolicy] = field(default_factory=dict)
    _response_provenance_by_fence: dict[GenerationFence, dict[str, Any]] = field(
        default_factory=dict
    )
    _voice_snapshot_by_fence: dict[GenerationFence, GenerationVoiceSnapshot] = field(
        default_factory=dict
    )
    _assistant_expression_fence: GenerationFence | None = None
    _target_speaker_focus_enabled: bool = False
    _reject_non_owner_voice: bool = True
    _target_focus_epoch: int | None = None
    _target_focus_pending_epoch: int | None = None
    _target_speaker_interrupt: Callable[[], Awaitable[None]] | None = None
    _device_conversation_controls_enabled: bool = False
    _sticky_interrupt_epoch: int | None = None
    _sticky_interrupt_route: UtteranceRoute | None = None
    _sticky_interrupt_text: str = ""
    _interrupt_semantic_resolver: (
        Callable[[str, str, str], Awaitable[InterruptSemanticVerdict]] | None
    ) = None
    _interrupt_semantic_speech_epoch: int | None = None
    _interrupt_semantic_playback_epoch: int | None = None
    _interrupt_semantic_assistant_text: str = ""
    _interrupt_semantic_result_epoch: int | None = None
    _interrupt_semantic_result_fence: GenerationFence | None = None
    _interrupt_semantic_result: InterruptSemanticVerdict | None = None
    _interaction_decision_epoch: int | None = None
    _interaction_decision: InteractionDecision | None = None
    _interaction_decision_text: str = ""
    _last_committed_user_text_normalized: str = ""
    _trusted_unanchored_control_epoch: int | None = None
    _trusted_unanchored_playback_epoch: int | None = None
    _pending_keyword_interrupt_binding: KeywordSpotterBinding | None = None
    _result_speaker: Callable[[str], Any] | None = None
    _interrupt_yield: Callable[[str], Awaitable[None]] | None = None
    _false_interrupt_recover: Callable[[], Awaitable[None]] | None = None
    _user_turn_clearer: Callable[[], None] | None = None
    _cleared_control_epoch: int | None = None
    _paused_reply_available: bool = False
    _reply_speaker_binding: ResumeSpeakerBinding | None = None
    _paused_reply_binding: ResumeSpeakerBinding | None = None
    _pending_semantic_pause_epoch: int | None = None
    _pending_semantic_pause_binding: ResumeSpeakerBinding | None = None
    _resume_pending: bool = False
    _resume_fence: GenerationFence | None = None
    _last_interrupt_yield_ns: int | None = None
    _last_interrupt_yield_speech_epoch: int | None = None
    _last_false_recover_ns: int | None = None
    _last_listen_restore_ns: int | None = None
    _playback_started_ns: int | None = None
    # After control yield, accept chat turns even if LiveKit omits speech anchors.
    CONTROL_RESTORE_SPEECH_EPOCH_GRACE_MS: int = 20_000
    _background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    _durable_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    _durable_task_errors: list[BaseException] = field(default_factory=list)
    _evidence_drain_timeout_s: float = 3.0
    # Friendly yield when we stop mid-reply so silence does not feel like a crash.
    INTERRUPT_YIELD_COOLDOWN_MS: int = 4_000
    FALSE_INTERRUPT_RECOVER_COOLDOWN_MS: int = 3_000
    RUNTIME_PROFILE_REFRESH_TIMEOUT_S: float = 0.8
    _pending_tool_results: int = 0
    _listener_cue_player: Callable[[str], Any] | None = None
    _active_listener_cue: ListenerCue | None = None
    _active_listener_cue_handle: Any | None = None
    _listener_cue_candidate_task: asyncio.Task[Any] | None = None
    _listener_cue_aec_healthy: bool = False
    _enroll_fence: GenerationFence | None = None
    _formal_enrollment: FormalSpeakerEnrollment = field(
        default_factory=FormalSpeakerEnrollment
    )
    _formal_enrollment_sample_sink: (
        Callable[[bytes, int], Coroutine[Any, Any, None]] | None
    ) = None
    _emotion_turn_observer: Callable[[int], None] | None = None
    _speech_segment_finalizers: list[Callable[..., None]] = field(default_factory=list)
    _fast_model_warmer: Callable[[], Awaitable[Any] | Any] | None = None
    _delegation_starter: Callable[[str, GenerationFence], Coroutine[Any, Any, Any] | None] | None = None
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
        trusted_aec_playback_control: bool = False,
        barge_in_enabled: bool = True,
        capture_release_holdoff_s: float = 0.0,
        listener_cues_enabled: bool = False,
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
            trusted_aec_playback_control=trusted_aec_playback_control,
            barge_in_enabled=barge_in_enabled,
            capture_release_holdoff_s=capture_release_holdoff_s,
            cue_scheduler=CueScheduler(enabled=listener_cues_enabled),
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
    def assistant_speaking(self) -> bool:
        return self._was_speaking

    @property
    def output_floor_allows_assistant(self) -> bool:
        return not self._fresh_user_speech and self.interaction_phase not in {
            InteractionPhase.USER_SPEAKING,
            InteractionPhase.INTERRUPTED,
        }

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
            assistant_text=self._pending_assistant_text or self._played_assistant_text,
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
        self._bind_mode_policy(next_fence, self.mode_policy_for_fence(expected_fence))
        self._bind_history_eligibility(next_fence, self._history_eligible(expected_fence))
        self._bind_owner_projection_eligibility(
            next_fence,
            self._owner_projection_eligible(expected_fence),
        )
        self._speech_plans_by_fence[next_fence] = self.speech_plan_for_fence(expected_fence)
        if expected_fence in self._response_provenance_by_fence:
            self._response_provenance_by_fence[next_fence] = self._response_provenance_by_fence[
                expected_fence
            ]
        # An explicit preemption leaves the interaction phase at INTERRUPTED
        # even though this source is an admitted assistant output. Move the
        # runtime through THINKING before the OutputWork admission re-check so
        # the floor gate does not discard the rebound source.
        self.set_interaction_phase(InteractionPhase.THINKING_SILENT, cause="media_auxiliary_output")
        if expected_fence in self._voice_snapshot_by_fence:
            self._voice_snapshot_by_fence[next_fence] = self._voice_snapshot_by_fence[
                expected_fence
            ]
        self.apply_speech_plan_to_tts(next_fence)
        self._pending_assistant_text = ""
        self._played_assistant_text = ""
        self._playback_fence = None
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
            self._fresh_user_speech = False
            self.set_interaction_phase(
                InteractionPhase.LISTENING,
                cause=f"media_output_preempt:{cause}",
            )
        return replacement

    @property
    def pending_realtime_request(self) -> PendingRealtimeRequest | None:
        return self._pending_realtime_request

    def resolve_realtime_request(
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
        if static_reply_is_fallback and requires_realtime_lookup(query):
            request = PendingRealtimeRequest(query, speaker_scope, fence)
            self._pending_realtime_request = request
            return request, False
        if pending is not None and pending.speaker_scope == speaker_scope:
            self._pending_realtime_request = None
        return None, False

    def complete_realtime_request(self, request: PendingRealtimeRequest) -> None:
        if self._pending_realtime_request == request:
            self._pending_realtime_request = None

    def context_snapshot_for_fence(self, fence: GenerationFence) -> ContextSnapshot:
        version = self.orchestrator.context_version_for_fence(fence)
        snapshot = self.orchestrator.context_snapshots.current(self.session_id)
        if snapshot.version != version:
            raise RuntimeError("frozen context snapshot is no longer retained")
        return snapshot

    def _activate_pending_context_snapshot(self) -> None:
        pending = self._pending_context_snapshot
        if (
            pending is None
            or self._pending_context_snapshot_epoch != self._context_snapshot_prepare_epoch
        ):
            return
        if pending.candidate.speaker_class != self.current_speaker_class:
            self._pending_context_snapshot = None
            self._pending_context_snapshot_epoch = None
            return
        activated = self.orchestrator.context_snapshots.activate(
            pending,
            expected_current_version=pending.base_version,
        )
        self._pending_context_snapshot = None
        self._pending_context_snapshot_epoch = None
        if isinstance(activated, ContextSnapshot):
            self.orchestrator.delegation.activate_context_version(
                self.session_id,
                activated.version,
            )

    def activate_context_snapshot_for_turn(self) -> int:
        self._activate_pending_context_snapshot()
        return self.orchestrator.context_snapshots.current(self.session_id).version

    def _context_snapshot_draft(self) -> ContextSnapshotDraft:
        current = self.orchestrator.context_snapshots.current(self.session_id)
        turns = tuple(
            ContextTurn(
                "user" if turn.role == "user" else "assistant",
                turn.content,
                turn.speaker_scope,
            )
            for turn in self.orchestrator.context.turns
            if turn.role in {"user", "assistant"}
        )
        policy = self._mode_policy
        # Public read-only tools stay available; revoked epochs stop them too.
        # Tool authorization is per ToolSpec, never a coarse policy aggregate.
        outputs_allowed = self.orchestrator.runtime_profiles.output_allowed(
            self.fence, current_fence=self.fence
        )
        return scope_context_snapshot_draft(
            ContextSnapshotDraft(
                recent_committed_turns=turns,
                memory_capsule=current.memory_capsule,
                persona_capsule=current.persona_capsule,
                relationship_policy=policy,
                tool_permission=outputs_allowed,
                speaker_class=self.current_speaker_class,
                summary=self.orchestrator.context.context_summary(),
            )
        )

    def prepare_context_capsules(
        self,
        *,
        memory_capsule: MemoryCapsule,
        persona_capsule: PersonaCapsule,
    ) -> None:
        draft = self._context_snapshot_draft()
        self._schedule_context_snapshot_prepare(
            scope_context_snapshot_draft(
                ContextSnapshotDraft(
                    recent_committed_turns=draft.recent_committed_turns,
                    memory_capsule=memory_capsule,
                    persona_capsule=persona_capsule,
                    relationship_policy=draft.relationship_policy,
                    tool_permission=draft.tool_permission,
                    speaker_class=draft.speaker_class,
                    summary=draft.summary,
                )
            )
        )

    async def freeze_context_capsules_for_generation(
        self,
        fence: GenerationFence,
        *,
        memory_capsule: MemoryCapsule,
        persona_capsule: PersonaCapsule,
    ) -> ContextSnapshot | None:
        """Activate and freeze the exact capsules used by one current reply."""

        if not self.fence.matches(fence):
            return None
        self._context_snapshot_prepare_epoch += 1
        prepare_epoch = self._context_snapshot_prepare_epoch
        previous = self._context_snapshot_prepare_task
        if previous is not None and not previous.done():
            previous.cancel()
        self._pending_context_snapshot = None
        self._pending_context_snapshot_epoch = None
        manager = self.orchestrator.context_snapshots
        base = manager.current(self.session_id)
        draft = self._context_snapshot_draft()
        prepared = await manager.prepare_next(
            self.session_id,
            base_version=base.version,
            committed_events=draft.recent_committed_turns,
            draft=scope_context_snapshot_draft(
                ContextSnapshotDraft(
                    recent_committed_turns=draft.recent_committed_turns,
                    memory_capsule=memory_capsule,
                    persona_capsule=persona_capsule,
                    relationship_policy=draft.relationship_policy,
                    tool_permission=draft.tool_permission,
                    speaker_class=draft.speaker_class,
                    summary=draft.summary,
                )
            ),
        )
        if (
            prepare_epoch != self._context_snapshot_prepare_epoch
            or not self.fence.matches(fence)
            or not isinstance(prepared, PendingSnapshot)
            or prepared.candidate.speaker_class != self.current_speaker_class
        ):
            return None
        activated = manager.activate(
            prepared,
            expected_current_version=prepared.base_version,
        )
        if not isinstance(activated, ContextSnapshot):
            return None
        self.orchestrator.delegation.activate_context_version(self.session_id, activated.version)
        if not self.orchestrator.bind_context_version(fence, activated.version):
            return None
        return activated

    def freeze_current_context_for_generation(
        self,
        fence: GenerationFence,
    ) -> ContextSnapshot | None:
        """Keep the last valid snapshot when background preparation fails."""

        if not self.fence.matches(fence):
            return None
        snapshot = self.orchestrator.context_snapshots.current(self.session_id)
        self.orchestrator.delegation.activate_context_version(
            self.session_id,
            snapshot.version,
        )
        return snapshot if self.orchestrator.bind_context_version(fence, snapshot.version) else None

    def _schedule_context_snapshot_prepare(
        self,
        draft: ContextSnapshotDraft | None = None,
    ) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._context_snapshot_prepare_epoch += 1
        prepare_epoch = self._context_snapshot_prepare_epoch
        previous = self._context_snapshot_prepare_task
        if previous is not None and not previous.done():
            previous.cancel()
        self._pending_context_snapshot = None
        self._pending_context_snapshot_epoch = None
        manager = self.orchestrator.context_snapshots
        base = manager.current(self.session_id)
        draft = draft or self._context_snapshot_draft()

        async def _prepare() -> None:
            try:
                prepared = await asyncio.wait_for(
                    manager.prepare_next(
                        self.session_id,
                        base_version=base.version,
                        committed_events=draft.recent_committed_turns,
                        draft=draft if manager.builder is None else None,
                    ),
                    timeout=self.context_snapshot_prepare_timeout_s,
                )
            except TimeoutError:
                manager.metrics.inc_context_snapshot_build_failed("timeout")
                return
            except Exception:
                logger.warning("context snapshot prepare failed", exc_info=True)
                return
            if prepare_epoch == self._context_snapshot_prepare_epoch and isinstance(
                prepared,
                PendingSnapshot,
            ):
                self._pending_context_snapshot = prepared
                self._pending_context_snapshot_epoch = prepare_epoch

        self._context_snapshot_prepare_task = self._spawn(
            _prepare(),
            name="context-snapshot-prepare",
        )

    def speech_plan_for_fence(self, fence: GenerationFence) -> SpeechPlan:
        exact = self._speech_plans_by_fence.get(fence)
        if exact is not None:
            return exact
        return next(
            (
                plan
                for bound_fence, plan in reversed(self._speech_plans_by_fence.items())
                if bound_fence.turn_id == fence.turn_id
            ),
            self.speech_plan,
        )

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
                reference_contexts=self._tts_references_by_fence.get(fence, ()),
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

    def set_event_publisher(
        self,
        publisher: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        self._event_publisher = publisher

    def set_evidence_publisher(
        self,
        publisher: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        self._evidence_publisher = publisher

    def set_owner_turn_publisher(
        self,
        publisher: Callable[
            [dict[str, Any], bytes, int],
            Coroutine[Any, Any, None],
        ],
    ) -> None:
        self._owner_turn_publisher = publisher

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

    def set_target_speaker_interrupt(
        self,
        interrupt: Callable[[], Awaitable[None]] | None,
    ) -> None:
        """Install the only callback allowed to stop playout after focus passes."""

        self._target_speaker_interrupt = interrupt

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
        self._bind_mode_policy(self.fence, policy)

    def apply_runtime_profile(
        self, payload: object, *, now: datetime | None = None
    ) -> VerifiedRuntimeProfile | None:
        """Consumer seam: between-turn refresh/subject switch (P1-8)."""

        previous_epoch = self.fence.session_epoch
        old_fence = self.fence
        applied = self.orchestrator.runtime_profiles.apply(payload, self.fence, now=now)
        if applied is not None and applied.profile.session_epoch > previous_epoch:
            self._rotate_identity_epoch(
                applied, old_fence=old_fence, install_policy=previous_epoch > 0
            )
        return applied

    def _rotate_identity_epoch(
        self,
        profile: VerifiedRuntimeProfile | None,
        *,
        old_fence: GenerationFence,
        install_policy: bool,
    ) -> None:
        """One atomic identity-epoch rotation; ONE authority after it."""
        self._pending_epoch_drain = old_fence
        # Capture identity-owned tasks BEFORE clearing references, bump the
        # identity-private epochs so late results are logically void, then
        # clear state; the drain barrier bounded-cancels the captured tasks.
        self._rotation_captured_tasks = capture_identity_tasks(self)
        invalidate_identity_epochs(self)
        self._reset_identity_private_caches()
        clear_identity_private_state(self)
        if install_policy:
            self._mode_policy = (
                ModePolicy.from_runtime_profile(profile)
                if profile is not None
                else ModePolicy.degraded_unknown_safe()
            )
            self._mode_policy_by_fence = {
                fence: old
                for fence, old in self._mode_policy_by_fence.items()
                if fence.session_epoch == self.fence.session_epoch
            }
            self._bind_mode_policy(self.fence, self._mode_policy)

    async def _drain_epoch_rotation(self) -> None:
        """Barrier stays visible until every physical owner drained."""
        old_fence = self._pending_epoch_drain
        if old_fence is None:
            return
        try:
            await drain_epoch_rotation(self, old_fence)
        except Exception:
            # Barrier stays visible (fail-closed) and the turn is aborted.
            self._pending_epoch_drain = old_fence
            self.orchestrator.metrics.inc_runtime_profile_refresh_failure("drain_error")
            logger.exception("epoch rotation drain failed")
            raise
        self._pending_epoch_drain = None

    async def settle_bootstrap_identity_epoch(self) -> None:
        """Drain a session-start profile rotation before pre-turn TTS.

        Binding a signed RuntimeProfile can advance ``session_epoch`` and
        leave ``_pending_epoch_drain`` set until the first user turn.
        Device wake acknowledgement is audible before any turn, so the
        barrier must close when the media session is published.
        """

        if self._pending_epoch_drain is None or self.fence.turn_id != 0:
            return
        await self._drain_epoch_rotation()

    def degrade_runtime_profile(self) -> GenerationFence:
        """Authority-loss transition to a true unknown-safe degraded epoch."""

        previous_epoch = self.fence.session_epoch
        old_fence = self.fence
        new_fence = self.orchestrator.runtime_profiles.degrade(self.fence)
        if new_fence.session_epoch == previous_epoch:
            return new_fence  # already degraded (idempotent)
        self._rotate_identity_epoch(None, old_fence=old_fence, install_policy=True)
        return new_fence

    def _reset_identity_private_caches(self) -> None:
        """Drop every pending/prefetched private field of the old subject."""

        # Called once per epoch bump, atomically inside the apply seam.
        self._pending_context_snapshot = None
        self._pending_context_snapshot_epoch = None
        self._context_prefetch_text = ""
        self._pending_realtime_request = None
        self._persona_evidence_eligible = False
        self._next_user_prompt_kind = "spontaneous"

    @property
    def mode_policy(self) -> ModePolicy:
        return self._mode_policy

    @property
    def mode_policy_enforced(self) -> bool:
        """False only during construction before an entrypoint freezes policy."""

        return self._mode_policy.unavailable_reason != "not_fetched"

    def mode_policy_for_fence(self, fence: GenerationFence) -> ModePolicy:
        return self._mode_policy_by_fence.get(
            fence, ModePolicy.unavailable("policy_not_bound_to_fence")
        )

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
        decision = self._speaker_decision
        return bool(
            decision is not None
            and decision.classification in {"owner", "guest"}
            and not decision.reason_code.startswith("shadow_")
        )

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

    @property
    def text_only_delivery(self) -> bool:
        return self._text_only_delivery

    def enable_text_only_delivery(self) -> None:
        self._text_only_delivery = True

    def input_modality_for_fence(self, fence: GenerationFence) -> str:
        return self._input_modality_by_fence.get(fence, "audio")

    def authenticate_text_owner(self) -> SpeakerDecision:
        """Bind an lk.chat turn from the linked account participant as owner."""

        decision = SpeakerDecision(
            classification="owner",
            score=1.0,
            quality_score=1.0,
            reason_code="authenticated_text_input",
            model_version="account-auth-v1",
            template_version=None,
            profile_id=None,
            permissions=permissions_for_speaker("owner"),
        )
        self._speaker_class = "owner"
        self._speaker_decision = decision
        self._speaker_pcm.clear()
        return decision

    def _bind_mode_policy(self, fence: GenerationFence, policy: ModePolicy | None = None) -> None:
        self._mode_policy_by_fence[fence] = policy or self._mode_policy
        while len(self._mode_policy_by_fence) > HISTORY_ELIGIBILITY_MAX_FENCES:
            self._mode_policy_by_fence.pop(next(iter(self._mode_policy_by_fence)))

    @property
    def speaker_permissions(self) -> SpeakerPermissions:
        if self._speaker_decision is not None:
            return self._speaker_decision.permissions
        classification = self._speaker_class if self._speaker_class == "owner" else "uncertain"
        return permissions_for_speaker(classification)  # type: ignore[arg-type]

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
            and self._mode_policy.owner_projection_eligible(decision.classification)
            and self.profile_permits(self.fence, capability="memory_recall_private")
        )

    def is_shadow_speaker(self) -> bool:
        return (
            self._speaker_decision is not None
            and self._speaker_decision.classification == "uncertain"
            and self._speaker_decision.reason_code == "shadow_owner_candidate"
        )

    def _bind_history_eligibility(self, fence: GenerationFence, eligible: bool) -> None:
        self._history_eligible_by_fence[fence] = eligible
        while len(self._history_eligible_by_fence) > HISTORY_ELIGIBILITY_MAX_FENCES:
            self._history_eligible_by_fence.pop(next(iter(self._history_eligible_by_fence)))

    def _history_eligible(self, fence: GenerationFence) -> bool:
        return self._history_eligible_by_fence.get(fence, False)

    def _bind_owner_projection_eligibility(
        self,
        fence: GenerationFence,
        eligible: bool,
    ) -> None:
        self._owner_projection_eligible_by_fence[fence] = eligible
        while len(self._owner_projection_eligible_by_fence) > HISTORY_ELIGIBILITY_MAX_FENCES:
            self._owner_projection_eligible_by_fence.pop(
                next(iter(self._owner_projection_eligible_by_fence))
            )

    def _owner_projection_eligible(self, fence: GenerationFence) -> bool:
        return self._owner_projection_eligible_by_fence.get(fence, False)

    def bind_response_provenance(
        self,
        fence: GenerationFence,
        provenance: dict[str, Any],
    ) -> bool:
        """Freeze bounded planner/model/source IDs for one exact generation."""

        if not self.fence.matches(fence) or fence.session_id != self.session_id:
            return False
        try:
            encoded = json.dumps(
                provenance,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError):
            return False
        if (
            not provenance
            or len(encoded) > RESPONSE_PROVENANCE_MAX_BYTES
            or self._contains_forbidden_provenance_key(provenance)
        ):
            return False
        self._response_provenance_by_fence[fence] = json.loads(encoded)
        while len(self._response_provenance_by_fence) > RESPONSE_PROVENANCE_MAX_FENCES:
            self._response_provenance_by_fence.pop(next(iter(self._response_provenance_by_fence)))
        return True

    def bind_generation_voice(
        self,
        fence: GenerationFence,
        *,
        profile_id: str | None,
        resource_id: str,
        speaker_sha256: str,
        voice_kind: Literal["designed", "personal"],
    ) -> bool:
        """Bind or update the voice actually used by this exact generation."""

        policy = self.mode_policy_for_fence(fence)
        if (
            fence.session_id != self.session_id
            or not self.fence.matches(fence)
            or not generation_voice_allowed(
                policy,
                personal_voice_permitted=self.profile_permits(
                    fence, capability="voice_clone_use"
                ),
                profile_id=profile_id,
                resource_id=resource_id,
                speaker_sha256=speaker_sha256,
                voice_kind=voice_kind,
            )
        ):
            return False
        self._voice_snapshot_by_fence[fence] = GenerationVoiceSnapshot(
            profile_id=profile_id,
            resource_id=resource_id,
            speaker_sha256=speaker_sha256,
            voice_kind=voice_kind,
        )
        while len(self._voice_snapshot_by_fence) > RESPONSE_PROVENANCE_MAX_FENCES:
            self._voice_snapshot_by_fence.pop(next(iter(self._voice_snapshot_by_fence)))
        return True

    def generation_voice_for(
        self,
        fence: GenerationFence,
    ) -> GenerationVoiceSnapshot | None:
        return self._voice_snapshot_by_fence.get(fence)

    @classmethod
    def _contains_forbidden_provenance_key(cls, value: object) -> bool:
        if isinstance(value, dict):
            return any(
                str(key).lower() in _RESPONSE_PROVENANCE_FORBIDDEN_KEYS
                or cls._contains_forbidden_provenance_key(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(cls._contains_forbidden_provenance_key(item) for item in value)
        return False

    def response_provenance_for(
        self,
        fence: GenerationFence,
    ) -> dict[str, Any] | None:
        stored = self._response_provenance_by_fence.get(fence)
        if stored is None:
            return None
        provenance = cast(dict[str, Any], json.loads(json.dumps(stored)))
        voice = self.generation_voice_for(fence)
        if voice is not None:
            provenance.update(
                {
                    "tts_model": voice.resource_id,
                    "actual_voice_profile_id": voice.profile_id,
                    "actual_voice_resource_id": voice.resource_id,
                    "actual_voice_speaker_sha256": voice.speaker_sha256,
                }
            )
        return provenance

    def set_result_speaker(self, speaker: Callable[[str], Any]) -> None:
        self._result_speaker = speaker

    def set_listener_cue_player(self, player: Callable[[str], Any]) -> None:
        self._listener_cue_player = player

    def set_listener_cue_aec_healthy(self, healthy: bool) -> None:
        self._listener_cue_aec_healthy = healthy

    def set_emotion_turn_observer(self, observer: Callable[[int], None]) -> None:
        self._emotion_turn_observer = observer

    def set_keyword_spotter_finalizer(
        self,
        finalizer: Callable[[KeywordSpotterBinding | None], None],
    ) -> None:
        self._speech_segment_finalizers.append(finalizer)

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

    def set_voice_profile_refresher(
        self,
        refresher: Callable[[], Coroutine[Any, Any, Any]],
    ) -> None:
        self._voice_profile_refresher = refresher

    def set_runtime_profile_refresher(
        self,
        refresher: Callable[[], Coroutine[Any, Any, VerifiedRuntimeProfile | None]] | None,
    ) -> None:
        """Install the authoritative refresh source (P1-8/audit 2)."""

        self._runtime_profile_refresher = refresher

    def set_playback_stop_seam(
        self,
        seam: Callable[[], Awaitable[Any]] | None,
    ) -> None:
        self.orchestrator.playback_stop_seam = seam

    async def refresh_runtime_profile(self) -> VerifiedRuntimeProfile | None:
        if self._runtime_profile_refresher is None:
            self.degrade_runtime_profile()
            return None
        try:
            verified = await asyncio.wait_for(
                self._runtime_profile_refresher(), timeout=self.RUNTIME_PROFILE_REFRESH_TIMEOUT_S
            )
        except TimeoutError:
            self.orchestrator.metrics.inc_runtime_profile_refresh_failure("timeout")
            logger.warning(
                "runtime profile refresh timed out after %ss",
                self.RUNTIME_PROFILE_REFRESH_TIMEOUT_S,
            )
            self.degrade_runtime_profile()
            return None
        except Exception:
            self.orchestrator.metrics.inc_runtime_profile_refresh_failure("error")
            logger.exception("runtime profile refresh failed")
            self.degrade_runtime_profile()
            return None
        applied = self.apply_runtime_profile(verified)
        if applied is None:
            self.orchestrator.metrics.inc_runtime_profile_refresh_failure("invalid")
            self.degrade_runtime_profile()
        return applied

    def refresh_voice_profile(self) -> asyncio.Task[Any] | None:
        if self._voice_profile_refresher is None:
            return None
        current = self._voice_profile_refresh_task
        if current is not None and not current.done():
            return current
        # Keep the currently applied voice while the resolver is in flight.
        # The result is applied atomically at the next TTS boundary; changing
        # to baseline here makes a fixed welcome sentence sound like another
        # companion whenever VAD starts a turn.
        self._voice_profile_refresh_task = self._spawn(
            self._voice_profile_refresher(),
            name="voice-profile-refresh",
        )
        return self._voice_profile_refresh_task

    async def wait_for_voice_profile_refresh(self) -> None:
        """Wait for the latest profile refresh before a response is rendered."""
        task = self._voice_profile_refresh_task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            return
        except Exception:
            # The resolver is fail-closed; the caller will apply the baseline
            # because the cache is empty, while the conversation remains live.
            logger.warning("voice profile refresh failed", exc_info=True)


    def _interrupt_candidate_text(self) -> str:
        return (getattr(self.input_guard, "candidate_text", None) or "").strip()

    def _route_candidate(
        self,
        text: str | None = None,
        *,
        semantic_verdict: InterruptSemanticVerdict | None = None,
    ) -> UtteranceRoute:
        sticky_route = (
            self._sticky_interrupt_route
            if self._sticky_interrupt_epoch == self._speaker_epoch
            else None
        )
        return route_utterance(
            text if text is not None else self._interrupt_candidate_text(),
            speaker_state=self.speaker_verifier.state,
            resumable_reply=(
                self._paused_reply_available
                and self._paused_reply_binding is not None
                and self._paused_reply_binding == self._current_resume_speaker_binding()
            ),
            sticky_interrupt_route=sticky_route,
            previous_committed_text_normalized=(
                self._last_committed_user_text_normalized
                if self.input_guard.candidate_during_playback
                else ""
            ),
            semantic_verdict=semantic_verdict,
            session_focus=self._mode_policy.session_focus,
            device_conversation=self._device_conversation_controls_enabled,
        )

    def _is_explicit_owner_interrupt_cmd(self) -> bool:
        """True when ASR heard stop/yield phrases like「停一下」「等等」."""
        return self._route_candidate().speaker_gate_override

    def _speaker_allows_user_input(self, *, context: str) -> bool:
        if not self.speaker_verifier.enabled:
            return True
        if self.speaker_verifier.state in {
            SpeakerGateState.DISABLED,
            SpeakerGateState.UNAVAILABLE,
        }:
            # Conversation stays open, but formal SpeakerAuthority remains uncertain.
            return True
        # Enrollment speech must not become a normal user turn / interrupt.
        if self.speaker_verifier.state is SpeakerGateState.PENDING:
            if context in {"turn_commit", "interrupt", "barge_in_start"}:
                return False
            return True
        # Explicit stop phrases from the mic: always treat as real interrupt.
        # Short「停一下」often scores too_short and used to wrongly trigger「我继续」.
        if context in {"interrupt", "barge_in_start"} and self._is_explicit_owner_interrupt_cmd():
            return True
        utt = self.speaker_verifier.score_latest_utterance()
        roll = self.speaker_verifier.score_pcm()  # rolling ~4s window
        score = utt
        if utt.reason == "too_short":
            # Prefer rolling window over "allow any short blip" — nearby noise was
            # cancelling TTS while owner was enrolled.
            score = roll
            if score.reason == "too_short":
                # barge-in / interrupt: never cancel on micro-blips.
                # turn_commit: fail-closed so tablet/TV fragments cannot enter chat.
                if context in {"barge_in_start", "interrupt"}:
                    return False
                return True

        # Near-field / media-pollution gates (turn_commit): tablet/TV often rides
        # under a short owner phrase (「等一下」) and used to pass dual-window.
        if context == "turn_commit":
            reject_reason: str | None = None
            if self.speaker_verifier.is_far_field(score):
                reject_reason = "far_field"
            elif self.speaker_verifier.is_media_polluted(score):
                reject_reason = "media_polluted"
            elif score.reason == "far_field":
                reject_reason = "far_field"
            if reject_reason is not None:
                self.orchestrator.metrics.inc_guarded_user_input(f"speaker_{reject_reason}")
                logger.info(
                    "speaker_reject context=%s reason=%s utt=%.3f roll=%.3f "
                    "rms=%.4f ref_rms=%.4f near_ms=%s far_ms=%s speech_ms=%s "
                    "session_id=%s",
                    context,
                    reject_reason,
                    utt.score,
                    roll.score,
                    score.rms,
                    self.speaker_verifier.owner_ref_rms,
                    score.near_ms,
                    score.far_ms,
                    score.speech_ms,
                    self.session_id,
                )
                self.mark_audio_event(
                    "speaker_rejected",
                    status="ignored",
                    detail={
                        "context": context,
                        "reason": reject_reason,
                        "utt": round(utt.score, 4),
                        "roll": round(roll.score, 4),
                        "rms": round(score.rms, 5),
                        "ref_rms": round(self.speaker_verifier.owner_ref_rms, 5),
                        "near_ms": score.near_ms,
                        "far_ms": score.far_ms,
                        "speech_ms": score.speech_ms,
                    },
                )
                self._publish(
                    {
                        "type": "speaker_reject",
                        "session_id": self.session_id,
                        "context": context,
                        "reason": reject_reason,
                        "score": round(score.score, 4),
                        "utt": round(utt.score, 4),
                        "roll": round(roll.score, 4),
                        "speech_ms": score.speech_ms,
                        "at": datetime.now(UTC).isoformat(),
                    },
                    fence=self.fence,
                )
                return False

        thr = float(self.speaker_verifier.accept_threshold)
        # Continuous tablet/TV: long high-duty audio needs a higher match bar.
        media_like = self.speaker_verifier.looks_like_continuous_media(
            roll if roll.speech_ms >= utt.speech_ms else score
        )
        if media_like:
            thr = min(0.92, thr + 0.10)

        dual_ok = utt.reason not in {"too_short", "embed_failed"} and roll.reason not in {
            "too_short",
            "embed_failed",
        }
        # Dual-window consensus: never accept on a single mid-band window.
        # Owner usually scores high on both utterance and rolling PCM.
        best = max(utt.score, roll.score) if dual_ok else score.score
        worst = min(utt.score, roll.score) if dual_ok else score.score
        if context == "turn_commit":
            # Chat turns: both windows must clear thr (tablet video was getting in
            # via single-window score.accepted).
            hard_ok = dual_ok and worst >= thr
        else:
            # Barge-in / interrupt: slightly more lenient so owner can stop TTS.
            hard_ok = score.accepted or (dual_ok and worst >= thr)

        # Soft margin only for short owner turns near thr — not for continuous media.
        soft_floor = thr - 0.02
        soft_ok = (
            context == "turn_commit"
            and not hard_ok
            and dual_ok
            and not media_like
            and score.reason == "mismatch"
            and score.speech_ms >= max(700, int(self.speaker_verifier.min_verify_speech_ms))
            and best >= thr
            and worst >= soft_floor
        )
        if hard_ok or soft_ok:
            logger.info(
                "speaker_accept context=%s soft=%s utt=%.3f roll=%.3f thr=%.3f "
                "near_ms=%s far_ms=%s speech_ms=%s session_id=%s",
                context,
                soft_ok,
                utt.score,
                roll.score,
                thr,
                score.near_ms,
                score.far_ms,
                score.speech_ms,
                self.session_id,
            )
            if soft_ok:
                self.mark_audio_event(
                    "speaker_soft_accept",
                    detail={
                        "context": context,
                        "utt": round(utt.score, 4),
                        "roll": round(roll.score, 4),
                        "threshold": thr,
                        "soft_floor": soft_floor,
                        "speech_ms": score.speech_ms,
                        "near_ms": score.near_ms,
                        "far_ms": score.far_ms,
                    },
                )
            return True

        # A clear human voice mismatch is a guest, not noise. The shared router
        # keeps this conversational path separate from private-memory authority.
        gate_route = route_speaker_gate(score_reason=score.reason)
        if gate_route.allow_input:
            logger.info(
                "speaker_gate_allow reason=%s context=%s utt=%.3f roll=%.3f session_id=%s",
                gate_route.reason,
                context,
                utt.score,
                roll.score,
                self.session_id,
            )
            return True

        self.orchestrator.metrics.inc_guarded_user_input(f"speaker_{score.reason}")
        logger.info(
            "speaker_reject context=%s reason=%s utt=%.3f roll=%.3f "
            "speech_ms=%s thr=%.3f media_like=%s session_id=%s",
            context,
            score.reason,
            utt.score,
            roll.score,
            score.speech_ms,
            thr,
            media_like,
            self.session_id,
        )
        self.mark_audio_event(
            "speaker_rejected",
            status="ignored",
            detail={
                "context": context,
                "reason": score.reason,
                "utt": round(utt.score, 4),
                "roll": round(roll.score, 4),
                "speech_ms": score.speech_ms,
                "threshold": thr,
                "media_like": media_like,
            },
        )
        self._publish(
            {
                "type": "speaker_reject",
                "session_id": self.session_id,
                "context": context,
                "reason": score.reason,
                "score": round(score.score, 4),
                "utt": round(utt.score, 4),
                "roll": round(roll.score, 4),
                "speech_ms": score.speech_ms,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=self.fence,
        )
        return False

    def _spawn(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str,
        durable: bool = False,
    ) -> asyncio.Task[Any]:
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

    def set_interaction_phase(
        self,
        phase: InteractionPhase,
        *,
        cause: str = "",
        publish: bool = True,
    ) -> asyncio.Task[Any] | None:
        if phase is self.interaction_phase:
            return None
        previous = self.interaction_phase
        self.interaction_phase = phase
        cause_s = cause or "unspecified"
        logger.info(
            "interaction_phase from=%s to=%s cause=%s session_id=%s turn_id=%s generation_id=%s",
            previous.value,
            phase.value,
            cause_s,
            self.session_id,
            self.fence.turn_id,
            self.fence.generation_id,
        )
        self._notify_phase(previous, phase)
        self.orchestrator.metrics.inc_interaction_phase(
            previous.value,
            phase.value,
            cause_s,
        )
        # Audio-trace seam so production logs can grep phase without UI events.
        self.mark_audio_event(
            "interaction_phase",
            detail={
                "from": previous.value,
                "to": phase.value,
                "cause": cause_s[:80],
            },
        )
        if not publish:
            return None
        return self.publish_assistant_state(phase.value, phase=phase)

    def publish_assistant_state(
        self,
        state: str,
        *,
        phase: InteractionPhase | None = None,
    ) -> asyncio.Task[Any] | None:
        fence = self.fence
        mapped_phase = phase or self._phase_for_published_state(state)
        if mapped_phase is not None and mapped_phase is not self.interaction_phase:
            previous = self.interaction_phase
            self.interaction_phase = mapped_phase
            logger.info(
                "interaction_phase from=%s to=%s cause=publish:%s session_id=%s "
                "turn_id=%s generation_id=%s",
                previous.value,
                mapped_phase.value,
                state,
                self.session_id,
                fence.turn_id,
                fence.generation_id,
            )
            self._notify_phase(previous, mapped_phase)
        state_task = self._publish(
            {
                "type": "assistant_state",
                "session_id": self.session_id,
                "state": state,
                "phase": self.interaction_phase.value,
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "tool_epoch": fence.tool_epoch,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=fence,
        )
        if self.barge_in_enabled:
            return state_task
        policy = self._input_policy_for_state(state)
        if policy is None:
            return state_task
        capture_allowed, reason = policy
        if capture_allowed:
            if self._capture_blocked and self.capture_release_holdoff_s > 0:
                self._schedule_capture_release(reason)
                return state_task
            self._capture_blocked = False
            self._cancel_capture_release()
        else:
            self._cancel_capture_release()
            self._capture_blocked = True
        policy_task = self.publish_input_policy(capture_allowed=capture_allowed, reason=reason)
        return join_ui_publishes(self, state_task, policy_task)

    def set_phase_listener(
        self,
        listener: Callable[[InteractionPhase, InteractionPhase], None] | None,
    ) -> None:
        self._phase_listener = listener

    def _notify_phase(self, previous: InteractionPhase, phase: InteractionPhase) -> None:
        listener = self._phase_listener
        if listener is None:
            return
        try:
            listener(phase, previous)
        except Exception:
            logger.warning("interaction phase listener failed", exc_info=True)

    def _cancel_capture_release(self) -> None:
        task = self._capture_release_task
        self._capture_release_task = None
        if task is not None:
            task.cancel()

    def _schedule_capture_release(self, reason: str) -> None:
        self._cancel_capture_release()
        delay = self.capture_release_holdoff_s

        async def _release() -> None:
            await asyncio.sleep(delay)
            self._capture_blocked = False
            self._capture_release_task = None
            self.publish_input_policy(capture_allowed=True, reason=reason)

        self._capture_release_task = self._spawn(
            _release(),
            name="capture-release-holdoff",
        )

    def publish_input_policy(
        self,
        *,
        capture_allowed: bool,
        reason: str,
    ) -> asyncio.Task[Any] | None:
        if not reason:
            raise ValueError("input policy reason is required")
        self._input_policy_epoch += 1
        fence = self.fence
        return self._publish(
            {
                "type": "input_policy",
                "session_id": self.session_id,
                "capture_allowed": capture_allowed,
                "policy_epoch": self._input_policy_epoch,
                "reason": reason,
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=fence,
        )

    @staticmethod
    def _input_policy_for_state(state: str) -> tuple[bool, str] | None:
        return input_policy_for_state(state)

    @staticmethod
    def _phase_for_published_state(state: str) -> InteractionPhase | None:
        return phase_for_published_state(state)  # type: ignore[return-value]

    def publish_assistant_audio(
        self,
        action: str,
        *,
        gain: float,
    ) -> asyncio.Task[Any] | None:
        if action not in {"duck", "restore"}:
            raise ValueError(f"unsupported assistant audio action: {action}")
        fence = self.fence
        return self._publish(
            {
                "type": "assistant_audio",
                "session_id": self.session_id,
                "action": action,
                "gain": min(1.0, max(0.0, gain)),
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "tool_epoch": fence.tool_epoch,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=fence,
        )

    def mark_audio_event(
        self,
        name: str,
        *,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
        mono_ns: int | None = None,
        fence: GenerationFence | None = None,
    ) -> None:
        """Record and publish one server-side first-audio stage without blocking media."""
        if status == "ok":
            self.latency_trace.mark(name, mono_ns=mono_ns)
        event: dict[str, Any] = {
            "type": "audio_trace",
            "source": "agent",
            "session_id": self.session_id,
            "name": name,
            "status": status,
            "at": datetime.now(UTC).isoformat(),
        }
        if fence is not None:
            event.update(
                {
                    "turn_id": fence.turn_id,
                    "generation_id": fence.generation_id,
                    "tool_epoch": fence.tool_epoch,
                }
            )
        if detail:
            event["detail"] = detail
        logger.info(
            "agent_audio_trace name=%s status=%s turn_id=%s generation_id=%s",
            name,
            status,
            None if fence is None else fence.turn_id,
            None if fence is None else fence.generation_id,
        )
        self._publish(event, fence=fence)

    def observe_client_audio_trace(
        self,
        event: dict[str, Any],
        *,
        mono_ns: int | None = None,
    ) -> bool:
        """Accept bounded client playback facts; permanent keys/audio never enter telemetry."""
        trace = parse_client_audio_trace(event, session_id=self.session_id)
        if trace is None:
            return False
        if trace.name == "first_playback" and trace.status == "ok":
            self.latency_trace.mark("client_first_playback", mono_ns=mono_ns)
        logger.info(
            "client_audio_trace name=%s status=%s turn_id=%s generation_id=%s metrics=%s",
            trace.name,
            trace.status,
            event.get("turn_id"),
            event.get("generation_id"),
            trace.metrics,
        )
        return True

    def publish_transcript(
        self,
        *,
        speaker: str,
        text: str,
        final: bool,
        heard: bool | None = None,
        text_delivered: bool = False,
        fence: GenerationFence | None = None,
        archive_fence: GenerationFence | None = None,
        turn_revision: int | None = None,
    ) -> bool:
        original_fence = fence
        fence = fence or self.fence
        archive_fence = archive_fence or fence
        revision = self._transcript_revisions.issue(
            speaker=speaker,
            fence=fence,
            requested=turn_revision,
            final=bool(final and (speaker == "user" or heard is True or text_delivered)),
        )
        if revision is None:
            return False
        disclosure: dict[str, object] | None = None
        if (
            speaker == "assistant"
            and final
            and heard is True
            and self.mode_policy_for_fence(archive_fence).mode == "self_preview"
        ):
            disclosure = preview_provenance(
                self.response_provenance_for(archive_fence), archive_fence
            )
        event = transcript_delta_event(
            session_id=self.session_id,
            speaker=speaker,
            text=text,
            final=final,
            fence=fence,
            revision=revision,
            history_eligible=bool(final and self._history_eligible(fence)),
            heard=heard,
            text_delivered=text_delivered,
            preview=disclosure,
        )
        self._publish(event, fence=original_fence)
        if not final or not text.strip():
            return True
        archive_text = redact_pii(text.strip())
        if speaker == "user":
            event_type = "speech.utterance_finalized"
            speaker_class = self._speaker_class
            input_modality = self._input_modality_by_fence.get(archive_fence, "audio")
            payload: dict[str, Any] = {
                "text": archive_text,
                "persona_eligible": self._persona_evidence_eligible,
                "prompt_kind": self._next_user_prompt_kind,
            }
            if input_modality == "text":
                payload["input_modality"] = "text"
            self._next_user_prompt_kind = "spontaneous"
            payload.update(self._speaker_persona_provenance())
            payload.update(self._owner_acoustic_evidence())
        elif speaker == "assistant" and heard is True:
            event_type = "assistant.playout_stopped"
            speaker_class = "assistant"
            payload = {"text": archive_text, "actual_heard": True}
            response_provenance = self.response_provenance_for(archive_fence)
            if response_provenance is not None:
                payload["response_provenance"] = response_provenance
            self._next_user_prompt_kind = classify_prompt_kind(archive_text)
        else:
            return True
        payload.update(
            self._mode_policy_provenance(
                archive_fence,
                speaker_class,
                history_eligible=self._history_eligible(archive_fence),
                owner_projection_eligible=self._owner_projection_eligible(archive_fence),
            )
        )
        decision = self.orchestrator.runtime_profiles.persistence_decision(
            archive_fence, current_fence=self.fence
        )
        fingerprint = evidence_fingerprint(
            session_id=self.session_id,
            event_type=event_type,
            speaker=speaker,
            fence=archive_fence,
            text=archive_text,
        )
        # MemoryWriteFence: evidence carries device + subject revision and the
        # exact verified capability receipt so the archive can re-verify the
        # write against the signed RuntimeProfile.
        if not decision.allowed or original_fence is None:
            # P0-6: without a verified receipt (or caller fence) no archive.
            return True
        self._event_sequence += 1
        evidence = archive_evidence(
            event_type=event_type,
            session_id=self.session_id,
            speaker_class=speaker_class,
            payload=payload,
            source=(
                "generation_fence.actual_heard"
                if speaker == "assistant"
                else (
                    "authenticated_text_input"
                    if payload.get("input_modality") == "text"
                    else "funasr.authoritative_final"
                )
            ),
            fence=archive_fence,
            decision=decision,
            fingerprint=fingerprint,
            event_sequence=self._event_sequence,
        )
        if (
            speaker_class == "owner"
            and payload["owner_projection_eligible"] is True
            and not decision.aggregate_only
            and decision.raw_audio_allowed
            and self._owner_turn_publisher is not None
        ):
            self._spawn(
                self._owner_turn_publisher(
                    evidence,
                    bytes(self._speaker_pcm),
                    self._speaker_sample_rate,
                ),
                name="duplex-evidence-owner-turn",
                durable=True,
            )
        elif self._evidence_publisher is not None:
            self._spawn(
                self._evidence_publisher(evidence),
                name=f"duplex-evidence-{event_type.replace('.', '-')}",
                durable=True,
            )
        return True

    def _speaker_persona_provenance(self) -> dict[str, Any]:
        return speaker_persona_provenance(self._speaker_decision)

    def _mode_policy_provenance(
        self,
        fence: GenerationFence,
        speaker_class: str,
        *,
        history_eligible: bool | None = None,
        owner_projection_eligible: bool | None = None,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        policy = self.mode_policy_for_fence(fence)
        return mode_policy_provenance(
            policy,
            speaker_class,
            history_eligible=history_eligible,
            owner_projection_eligible=owner_projection_eligible,
            reason_code=reason_code,
        )

    def _owner_acoustic_evidence(self) -> dict[str, int | float]:
        return owner_acoustic_evidence(
            speaker_class=self._speaker_class,
            decision=self._speaker_decision,
            owner_projection_eligible=self._mode_policy.owner_projection_eligible("owner"),
            profile_permits_memory_capture=self.profile_permits(
                self.fence, capability="memory_capture"
            ),
            pcm=self._speaker_pcm,
            sample_rate=self._speaker_sample_rate,
        )

    def observe_acoustic_emotion(
        self,
        provider_label: str,
        *,
        text: str = "",
        turn_id: int | None = None,
    ) -> None:
        current_turn_id = self.fence.turn_id
        if turn_id is None or turn_id <= current_turn_id or turn_id > current_turn_id + 1:
            return
        segments = self._emotion_segments_by_turn.setdefault(turn_id, [])
        if len(segments) < 8:
            segments.append((provider_label, text[:512]))
        self._emotion_segments_by_turn = {
            key: value
            for key, value in self._emotion_segments_by_turn.items()
            if key > current_turn_id
        }

    def _publish_emotion_observation(
        self,
        observation: EmotionObservation,
        *,
        turn_id: int,
        fence: GenerationFence,
    ) -> None:
        logger.info(
            "emotion_observation label=%s provider_label=%s evidence=%s turn_id=%s",
            observation.label,
            observation.provider_label,
            ",".join(observation.evidence),
            turn_id,
        )
        target_generation_id = fence.generation_id + int(turn_id > fence.turn_id)
        self._publish(
            {
                "type": "emotion_observation",
                "session_id": self.session_id,
                "label": observation.label,
                "provider_label": observation.provider_label,
                "provider_confidence": None,
                "evidence": list(observation.evidence),
                "persist": False,
                "turn_id": turn_id,
                "generation_id": target_generation_id,
                "expires_after_ms": self.emotion_smoother.ttl_ms,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=fence,
        )

    def _apply_speech_plan(
        self,
        user_text: str,
        *,
        turn_id: int,
        fence: GenerationFence,
    ) -> SpeechPlan:
        segments = self._emotion_segments_by_turn.pop(turn_id, [])
        self._emotion_segments_by_turn = {
            key: value for key, value in self._emotion_segments_by_turn.items() if key > turn_id
        }
        acoustic = None
        if segments:
            provider_label, acoustic_text = aggregate_acoustic_segments(segments)
            acoustic = self.emotion_smoother.observe_acoustic(
                provider_label,
                text=acoustic_text,
                turn_id=turn_id,
            )
        observation = self.emotion_smoother.observe_text(user_text, acoustic=acoustic)
        self._publish_emotion_observation(observation, turn_id=turn_id, fence=fence)
        self.speech_plan = speech_plan_for_turn(
            label=observation.label,
            provider_label=observation.provider_label,
            text=user_text,
            evidence=observation.evidence,
            use_markup_tags=self.use_paralinguistic_tags,
            companion_id=self.mode_policy.companion_style_id,
        )
        self._speech_plans_by_fence[fence] = self.speech_plan
        while len(self._speech_plans_by_fence) > 16:
            self._speech_plans_by_fence.pop(next(iter(self._speech_plans_by_fence)))
        apply_plan = getattr(self.tts, "apply_speech_plan", None)
        if callable(apply_plan):
            speaker_scope: Literal["owner", "public"] = (
                "owner" if self._speaker_class == "owner" else "public"
            )
            reference_contexts = self.orchestrator.context.tts_reference_context(
                current_user_final=user_text,
                speaker_scope=speaker_scope,
            )
            self._tts_references_by_fence[fence] = reference_contexts
            while len(self._tts_references_by_fence) > 16:
                self._tts_references_by_fence.pop(next(iter(self._tts_references_by_fence)))
            self.apply_speech_plan_to_tts(fence)
        logger.info(
            "speech_plan_selected emotion=%s dialect=%s tone=%s rate=%.2f pitch=%s "
            "delivery=%s tts_prefix=%s strip=%s turn_id=%s",
            self.speech_plan.voice_emotion,
            self.speech_plan.dialect,
            self.speech_plan.tone,
            self.speech_plan.rate,
            self.speech_plan.pitch,
            self.speech_plan.delivery_mode,
            self.speech_plan.tts_prefix or "-",
            self.speech_plan.strip_paralinguistic,
            turn_id,
        )
        return self.speech_plan

    def _publish_listener_cue(self, cue: ListenerCue, state: str) -> None:
        publish_listener_cue(self, cue, state)

    async def _play_listener_cue(self, cue: ListenerCue) -> None:
        if (
            self._listener_cue_player is None
            or not self.cue_scheduler.is_current(cue)
            or cue.fence.session_epoch != self.fence.session_epoch
        ):
            return
        handle = self._listener_cue_player(cue.text)
        if inspect.isawaitable(handle):
            handle = await handle
        if not self.cue_scheduler.is_current(cue) or not cue.fence.matches(self.fence):
            # Superseded or identity switched: stop the physical handle.
            await stop_cue_handle(handle)
            return
        self._active_listener_cue = cue
        self._active_listener_cue_handle = handle
        self.set_interaction_phase(
            InteractionPhase.BACKCHANNEL,
            cause=f"listener_cue:{cue.text}",
        )
        self._publish_listener_cue(cue, "started")
        try:
            wait_for_playout = getattr(handle, "wait_for_playout", None)
            if callable(wait_for_playout):
                await wait_for_playout()
            elif inspect.isawaitable(handle):
                await handle
            if self.cue_scheduler.is_current(cue) and cue.fence.matches(self.fence):
                self._publish_listener_cue(cue, "finished")
        finally:
            if self._active_listener_cue is cue:
                self._active_listener_cue = None
                self._active_listener_cue_handle = None
            # The physical cue must not keep playing after the task was
            # cancelled (identity switch/interrupt) or the cue was superseded.
            if not self.cue_scheduler.is_current(cue) or not cue.fence.matches(self.fence):
                # Stop the physical handle so no old audio keeps playing.
                await stop_cue_handle(handle)
            # Never mutate a newer epoch's phase from an old cue's finally.
            if (
                cue.fence.matches(self.fence)
                and self.cue_scheduler.is_current(cue)
                and self.interaction_phase is InteractionPhase.BACKCHANNEL
            ):
                self.set_interaction_phase(
                    InteractionPhase.USER_SPEAKING,
                    cause="listener_cue_finished",
                )

    def cancel_listener_cue(self) -> None:
        cancel_listener_cue(self)

    def _cancel_listener_cue_candidate(self) -> None:
        cancel_listener_cue_candidate(self)

    def _schedule_listener_cue(self, text: str, *, now_ns: int | None) -> None:
        schedule_listener_cue(self, text, now_ns=now_ns)

    def update_pending_assistant_text(self, text: str) -> None:
        self._pending_assistant_text = text
        self._pending_assistant_text_epoch += 1
        self.orchestrator.heard_tracker.set_full_text(text)

    def _assistant_response_blocks_barge_in(self) -> bool:
        return not self.barge_in_enabled and (
            self._was_speaking
            or self.orchestrator.state
            in {
                ConversationState.THINKING,
                ConversationState.SPEAKING,
                ConversationState.INTERRUPTION_PENDING,
                ConversationState.TOOL_WAITING,
                ConversationState.RECOVERING,
            }
        )

    def on_user_voice_started(self, *, now_ns: int | None = None) -> PlaybackInputDecision:
        assistant_response_blocked = self._assistant_response_blocks_barge_in()
        interrupted_assistant_text = (
            (self._pending_assistant_text or self._played_assistant_text)
            if self._was_speaking
            else ""
        )
        interrupted_playback_epoch = self._playback_epoch if self._was_speaking else None
        self.refresh_voice_profile()
        self._speaker_epoch += 1
        self._speech_epoch_assembler.start_epoch(self._speaker_epoch)
        self._target_focus_epoch = None
        self._target_focus_pending_epoch = None
        self._sticky_interrupt_epoch = None
        self._sticky_interrupt_route = None
        self._sticky_interrupt_text = ""
        self._interrupt_semantic_speech_epoch = (
            self._speaker_epoch if interrupted_assistant_text else None
        )
        self._interrupt_semantic_playback_epoch = interrupted_playback_epoch
        self._interrupt_semantic_assistant_text = interrupted_assistant_text
        self._interrupt_semantic_result_epoch = None
        self._interrupt_semantic_result_fence = None
        self._interrupt_semantic_result = None
        self._pending_semantic_pause_epoch = None
        self._pending_semantic_pause_binding = None
        self._trusted_unanchored_control_epoch = None
        self._trusted_unanchored_playback_epoch = None
        self._pending_keyword_interrupt_binding = None
        self._clear_trusted_playback_audio()
        self._speaker_class = "uncertain"
        self._speaker_decision = self._uncertain_speaker_decision("classification_pending")
        self._speaker_pcm.clear()
        self._speaker_collecting = True
        self._reset_speaker_classification_task()
        self._fresh_user_speech = True
        self.speaker_verifier.mark_utterance_start()
        self.input_guard.start(
            during_playback=self._was_speaking or assistant_response_blocked,
            now_ns=now_ns,
        )
        if assistant_response_blocked:
            self.input_guard.candidate_during_playback = True
            self._fresh_user_speech = False
            self._speaker_collecting = False
            self.input_guard.candidate_decision = PlaybackInputDecision.IGNORE
            self.input_guard.candidate_reason = "barge_in_disabled"
            self.mark_audio_event(
                "barge_in_ignored",
                status="ignored",
                detail={"reason": "disabled"},
            )
            return PlaybackInputDecision.IGNORE
        if not self._was_speaking:
            self.set_interaction_phase(
                InteractionPhase.USER_SPEAKING,
                cause="vad_start",
            )
        pending_turn_id = self.fence.turn_id + 1
        if self._emotion_turn_observer is not None:
            self._emotion_turn_observer(pending_turn_id)
        if not self._was_speaking:
            self.cue_scheduler.start_turn(
                user_turn_id=pending_turn_id,
                now_ns=now_ns,
            )
        if self._was_speaking or self.input_guard.candidate_during_playback:
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

    def observe_user_transcript(
        self,
        text: str,
        *,
        final: bool,
        now_ns: int | None = None,
    ) -> PlaybackInputDecision:
        if self.formal_speaker_enrollment_active:
            if final:
                self._speech_epoch_assembler.observe_final(
                    text,
                    accepted=False,
                    contaminated=True,
                )
            return PlaybackInputDecision.IGNORE
        raw_route = route_utterance(
            text,
            speaker_state=self.speaker_verifier.state,
            device_conversation=self._device_conversation_controls_enabled,
        )
        barge_in_blocked = not self.barge_in_enabled and (
            self._assistant_response_blocks_barge_in()
            or self.input_guard.candidate_reason == "barge_in_disabled"
        )
        if barge_in_blocked:
            if not self.input_guard.candidate_active:
                self.input_guard.start(
                    during_playback=True,
                    now_ns=now_ns,
                    vad_anchored=False,
                )
            self.input_guard.candidate_text = text
            self.input_guard.candidate_during_playback = True
            self.input_guard.candidate_decision = PlaybackInputDecision.IGNORE
            self.input_guard.candidate_reason = "barge_in_disabled"
            if final:
                self._speech_epoch_assembler.observe_final(
                    text,
                    accepted=False,
                    contaminated=True,
                )
                self.orchestrator.metrics.inc_guarded_user_input("barge_in_disabled")
            return PlaybackInputDecision.IGNORE
        decision = self.input_guard.observe(
            text,
            final=final,
            assistant_text=self._pending_assistant_text or self._played_assistant_text,
            now_ns=now_ns,
            during_playback_if_unstarted=self._was_speaking,
        )
        trusted_unanchored_control = (
            self.trusted_aec_playback_control
            and self.input_guard.candidate_during_playback
            and not self.input_guard.candidate_vad_anchored
            and raw_route.intent is UtteranceIntent.INTERRUPT_COMMAND
            and self._trusted_playback_has_voice(now_ns=now_ns)
            and raw_route.normalized_text
            not in normalize_short(self._pending_assistant_text or self._played_assistant_text)
            and self.input_guard.guarded_reason(
                text,
                duration_ms=900,
                assistant_text=self._pending_assistant_text or self._played_assistant_text,
            )
            is None
        )
        if trusted_unanchored_control:
            if self._trusted_unanchored_control_epoch is None:
                self._speaker_epoch += 1
                self._speech_epoch_assembler.start_epoch(self._speaker_epoch)
                self._target_focus_epoch = None
                self._target_focus_pending_epoch = None
                self._reset_speaker_classification_task()
                self._speaker_class = "uncertain"
                self._speaker_decision = self._uncertain_speaker_decision(
                    "trusted_aec_unanchored_control"
                )
                self._speaker_pcm = bytearray(self._trusted_playback_witness_pcm)
                self._speaker_collecting = False
                self._fresh_user_speech = False
                self._trusted_unanchored_control_epoch = self._speaker_epoch
                self._trusted_unanchored_playback_epoch = self._playback_epoch
                self.mark_audio_event("trusted_unanchored_interrupt_cmd")
            self.input_guard.candidate_reason = None
            self.input_guard.candidate_decision = PlaybackInputDecision.ACCEPT
            decision = PlaybackInputDecision.ACCEPT
        elif (
            self.input_guard.candidate_during_playback
            and not self.input_guard.candidate_vad_anchored
            and text.strip()
        ):
            self._speech_epoch_assembler.mark_contaminated(text)
        if final:
            candidate_started_during_playback = self.input_guard.candidate_during_playback
            current_vad_epoch = (
                self.input_guard.candidate_vad_anchored
                and self._speech_epoch_assembler.current_epoch == self._speaker_epoch
            )
            if current_vad_epoch:
                self._speech_epoch_assembler.observe_final(
                    text,
                    accepted=decision is PlaybackInputDecision.ACCEPT,
                    # An accepted barge-in final can still contain a prefix of
                    # the assistant playback.  Preserve it as a contaminated
                    # candidate so the later LiveKit endpoint can match the
                    # real user phrase instead of discarding the whole epoch.
                    contaminated=(
                        decision is PlaybackInputDecision.IGNORE
                        or candidate_started_during_playback
                    ),
                )
            if decision is PlaybackInputDecision.ACCEPT and not current_vad_epoch:
                self.mark_audio_event(
                    "orphan_transcript_ignored",
                    status="ignored",
                    detail={"reason": "missing_speech_epoch"},
                )
            elif decision is PlaybackInputDecision.IGNORE:
                self.orchestrator.metrics.inc_guarded_user_input(
                    self.input_guard.candidate_reason or "playback_noise"
                )
        if decision is PlaybackInputDecision.ACCEPT and raw_route.should_interrupt:
            # FunASR may revise a clear interim「等一下」into a nearby final
            # homophone while TTS is also reaching the microphone. Keep the
            # accepted control intent monotonic for this VAD epoch.
            if self._sticky_interrupt_epoch != self._speaker_epoch:
                self._sticky_interrupt_text = raw_route.normalized_text
            self._sticky_interrupt_epoch = self._speaker_epoch
            self._sticky_interrupt_route = raw_route
        if self._was_speaking or self.input_guard.candidate_during_playback:
            started_ns = self.input_guard.candidate_started_ns
            observed_ns = now_ns if now_ns is not None else time.monotonic_ns()
            elapsed_ms = (
                max(0, int((observed_ns - started_ns) / 1_000_000)) if started_ns is not None else 0
            )
            interaction = self.decide_interaction(
                InteractionSnapshot(
                    event=InteractionEvent.TRANSCRIPT,
                    assistant_speaking=self._was_speaking,
                    text=text,
                    elapsed_ms=elapsed_ms,
                    final=final,
                    has_speech_energy=True,
                    guarded_reason=self.playback_guarded_reason(
                        text,
                        duration_ms=elapsed_ms,
                    ),
                    playback_decision=decision,
                    utterance_route=self._route_candidate(text),
                )
            )
            self.apply_interaction_decision(interaction, text=text)
            if interaction.continue_output:
                decision = PlaybackInputDecision.IGNORE
                self.input_guard.candidate_reason = interaction.reason
            elif interaction.cancel_generation:
                decision = PlaybackInputDecision.ACCEPT
                self.input_guard.candidate_reason = None
            elif decision is PlaybackInputDecision.ACCEPT:
                decision = PlaybackInputDecision.WAIT
                self.input_guard.candidate_reason = interaction.reason
            self.input_guard.candidate_decision = decision
            self._interaction_decision_epoch = self._speaker_epoch
            self._interaction_decision = interaction
            self._interaction_decision_text = text
        elif (
            not final
            and decision is PlaybackInputDecision.ACCEPT
            and self.input_guard.candidate_active
            and self.input_guard.candidate_vad_anchored
            and text.strip()
        ):
            interaction = self.decide_interaction(
                InteractionSnapshot(
                    event=InteractionEvent.TRANSCRIPT,
                    assistant_speaking=False,
                    text=text,
                    final=False,
                    has_speech_energy=True,
                    playback_decision=decision,
                    utterance_route=self._route_candidate(text),
                )
            )
            self.apply_interaction_decision(interaction, text=text)
            self._interaction_decision_epoch = self._speaker_epoch
            self._interaction_decision = interaction
            self._interaction_decision_text = text
        if final:
            self._cancel_listener_cue_candidate()
        elif (
            not final
            and decision is PlaybackInputDecision.ACCEPT
            and self._listener_cue_player is not None
        ):
            self._schedule_listener_cue(text, now_ns=now_ns)
        return decision

    def consume_canonical_user_turn(self, raw_text: str) -> str | None:
        """Resolve the callback against one or more matching VAD speech epochs."""

        # Media runtimes that provide a sample-clock interval are authoritative
        # for text assembly.  Keep the callback/FIFO assembler only as a
        # compatibility fallback for legacy LiveKit callbacks with no range.
        pending = self._speech_timeline.pending
        if pending:
            stream_epoch = self._speech_timeline.stream_epoch
            if stream_epoch is not None:
                canonical = self.consume_media_user_turn(
                    stream_epoch=stream_epoch,
                    start_sample=min(item.capture_start_sample for item in pending),
                    end_sample=max(item.capture_end_sample for item in pending),
                )
                if canonical:
                    return canonical

        assembled: AssembledUserTurn = self._speech_epoch_assembler.consume(
            raw_text,
            fallback_epoch=self._speaker_epoch if self._fresh_user_speech else None,
        )
        self._consumed_canonical_snapshot_bound = assembled.snapshot_bound
        self._consumed_canonical_speech_epoch = assembled.speech_epoch
        if assembled.discarded_epochs:
            self.mark_audio_event(
                "stale_speech_epochs_discarded",
                status="ignored",
                detail={"count": len(assembled.discarded_epochs)},
            )
        if assembled.snapshot_bound:
            logger.info(
                "canonical_user_turn_resolved raw_len=%s canonical_len=%s "
                "discarded_epochs=%s session_id=%s",
                len(raw_text.strip()),
                len(assembled.text or ""),
                len(assembled.discarded_epochs),
                self.session_id,
            )
        return assembled.text

    def consume_media_user_turn(
        self,
        *,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
    ) -> str | None:
        """Commit one explicit sample range and return canonical ASR text.

        This is the preferred entry point for a Media Edge bridge.  It never
        guesses a turn from callback order or from the currently latest
        speaker; the caller supplies the frozen epoch and interval.
        """

        canonical = self.project_media_user_turn(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        if not canonical:
            return None
        self._speech_timeline.commit_range(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        return canonical

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
        self._fresh_user_speech = False
        return canonical

    @property
    def consumed_canonical_speech_epoch(self) -> int | None:
        return self._consumed_canonical_speech_epoch

    @property
    def consumed_canonical_snapshot_bound(self) -> bool:
        return self._consumed_canonical_snapshot_bound

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
        self._trusted_unanchored_control_epoch = None
        self._trusted_unanchored_playback_epoch = None
        self._clear_trusted_playback_audio()

    def _clear_unanchored_playback_transcript(self) -> None:
        """Reset LiveKit STT after echo-only playback input, before the next VAD."""

        if (
            not self._speech_epoch_assembler.has_suspected_playback_prefix
            or self._fresh_user_speech
            or self._user_turn_clearer is None
        ):
            return
        try:
            self._user_turn_clearer()
        except Exception:
            logger.warning(
                "playback transcript clear failed session_id=%s",
                self.session_id,
                exc_info=True,
            )
            self.mark_audio_event("playback_transcript_cleared", status="error")
            return
        self.discard_pending_user_transcript()
        self.mark_audio_event("playback_transcript_cleared")

    def accept_user_turn(
        self,
        text: str,
        *,
        input_modality: Literal["audio", "text"] = "audio",
        speech_anchored: bool | None = None,
        canonical_speech_epoch: int | None = None,
        canonical_snapshot_bound: bool | None = None,
        semantic_verdict: InterruptSemanticVerdict | None = None,
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
        target_route = self._target_speaker_route(context="conversation")
        if not target_route.allow_input:
            self._reject_target_speaker(context="turn_commit", route=target_route)
            return False, target_route.reason
        if route.intent is UtteranceIntent.END_SESSION:
            # The Media Voice registry owns the terminal session projection.
            # Runtime only suppresses chat/control side effects after the
            # target-speaker gate has authorized this exact close phrase.
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
            # Ensure semantic ack (cooldown skips if interrupt path already said it).
            self._spawn(
                self._maybe_say_interrupt_yield(
                    cause="interrupt_command_turn",
                    route=route,
                    speech_epoch=(
                        canonical_speech_epoch
                        if canonical_speech_epoch is not None
                        else self._speaker_epoch
                    ),
                ),
                name="interrupt-cmd-yield",
            )
            return False, route.reason
        if input_modality == "audio" and self.input_guard.enabled and speech_anchored is not None:
            current_vad_has_pcm = (
                canonical_speech_epoch == self._speaker_epoch
                and self._fresh_user_speech
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
                        self._fresh_user_speech,
                        self.session_id,
                    )
                    self.mark_audio_event(
                        "speech_epoch_fail_open",
                        detail={"reason": "after_control_grace"},
                    )
                    if canonical_speech_epoch in {None, self._speaker_epoch}:
                        self._fresh_user_speech = False
                else:
                    if canonical_speech_epoch in {None, self._speaker_epoch}:
                        self._fresh_user_speech = False
                    self.orchestrator.metrics.inc_guarded_user_input("missing_speech_epoch")
                    return False, "missing_speech_epoch"
            elif canonical_speech_epoch in {None, self._speaker_epoch}:
                self._fresh_user_speech = False
        if (
            input_modality == "audio"
            and self.speaker_verifier.active
            and not self._speaker_allows_user_input(context="turn_commit")
        ):
            return False, "speaker_mismatch"
        post_playback_reason = (
            self.post_playback_guard_reason(text) if input_modality == "audio" else None
        )
        if post_playback_reason is not None:
            self.orchestrator.metrics.inc_guarded_user_input(post_playback_reason)
            return False, post_playback_reason
        if input_modality == "text":
            self.input_guard.candidate_active = False
            self.input_guard.candidate_decision = PlaybackInputDecision.ACCEPT
        accepted, reason = self.input_guard.accept_turn(
            text,
            assistant_text=self._pending_assistant_text or self._played_assistant_text,
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
        self.cancel_listener_cue()
        self._last_playback_completed_ns = None
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
        self._bind_mode_policy(fence)
        self._bind_history_eligibility(fence, history_eligible)
        self._bind_owner_projection_eligibility(
            fence,
            owner_projection_eligible,
        )
        self._input_modality_by_fence[fence] = input_modality
        while len(self._input_modality_by_fence) > HISTORY_ELIGIBILITY_MAX_FENCES:
            self._input_modality_by_fence.pop(next(iter(self._input_modality_by_fence)))
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
        self._pending_assistant_text = ""
        self._played_assistant_text = ""
        self._playback_fence = None
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
        self._pending_assistant_text = full_text
        self._was_speaking = True
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
        self._pending_assistant_text = ""
        self._was_speaking = False
        self._assistant_expression_fence = None
        self._playback_fence = None
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
        self._pending_assistant_text = ""
        self._was_speaking = False
        self._assistant_expression_fence = None
        self._playback_fence = None
        restored = await self.orchestrator.return_to_listening_after_unheard_output(
            fence,
            cause=cause,
        )
        if restored or self.orchestrator.state is ConversationState.LISTENING:
            self.set_interaction_phase(InteractionPhase.LISTENING, cause=cause)

    def _publish_assistant_expression(self, full_text: str) -> None:
        fence = self.fence
        if self._assistant_expression_fence == fence:
            return
        self._assistant_expression_fence = fence
        self._publish(
            {
                "type": "assistant_expression",
                "session_id": self.session_id,
                "expression": mascot_expression_for_reply(
                    plan=self.speech_plan_for_fence(fence),
                    text=full_text,
                ),
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "tool_epoch": fence.tool_epoch,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=fence,
        )

    async def on_playback_done(self, *, tools_active: bool = False) -> None:
        self._was_speaking = False
        if self.orchestrator.state is ConversationState.SPEAKING:
            await self.orchestrator.finish_speaking(tools_active=tools_active)
        elif self.orchestrator.state is ConversationState.THINKING and self._pending_assistant_text:
            # No audio path — still commit heard text as full if never interrupted.
            await self.orchestrator.begin_speaking([], self._pending_assistant_text)
            await self.orchestrator.finish_speaking(tools_active=tools_active)

    async def on_media_playback_done(
        self,
        fence: GenerationFence,
        heard_text: str,
        *,
        tools_active: bool = False,
    ) -> bool:
        """Commit the exact sample-ACKed text for a media-v1 playback.

        The generic LiveKit callback estimates heard text from wall-clock
        playout. A media device/browser can provide a stronger sample
        watermark, so the ledger's exact text must be passed through rather
        than re-estimated with a safety margin.
        """

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
        self._played_assistant_text = normalized
        self._pending_assistant_text = ""
        self._was_speaking = False
        if tools_active:
            # Filler / acknowledgement playback is not the turn terminal while
            # an OWNED same-turn delegation is still waiting to speak.
            self.set_interaction_phase(
                InteractionPhase.TOOL_WAITING,
                cause="media_playback_ack",
            )
        else:
            self.set_interaction_phase(InteractionPhase.LISTENING, cause="media_playback_ack")
        return True

    def open_assistant_floor_for_nudge(self) -> None:
        """Let a device ack speak after a missed hear, without opening a user turn."""

        self._fresh_user_speech = False
        if self.interaction_phase in {
            InteractionPhase.USER_SPEAKING,
            InteractionPhase.INTERRUPTED,
        }:
            self.set_interaction_phase(
                InteractionPhase.LISTENING,
                cause="assistant_nudge",
            )

    def hold_floor_for_owned_delegation(self) -> None:
        """Keep the half-duplex floor open for one same-turn successor result."""

        self._fresh_user_speech = False
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
        self._bind_history_eligibility(
            event_fence,
            self._history_eligible(interrupted_from),
        )
        self._bind_owner_projection_eligibility(
            event_fence,
            self._owner_projection_eligible(interrupted_from),
        )
        self._was_speaking = False
        self._pending_assistant_text = ""
        self._played_assistant_text = heard
        self._playback_fence = None
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

    def set_interrupt_yield(self, speaker: Callable[[str], Awaitable[None]] | None) -> None:
        """speaker(phrase) — phrase is chosen from interrupt semantics."""
        self._interrupt_yield = speaker

    def set_interrupt_semantic_resolver(
        self,
        resolver: Callable[[str, str, str], Awaitable[InterruptSemanticVerdict]] | None,
    ) -> None:
        """Inject the small-model evidence adapter; the Router still owns policy."""

        self._interrupt_semantic_resolver = resolver

    async def resolve_interrupt_semantic(
        self,
        text: str,
        *,
        canonical_speech_epoch: int | None,
    ) -> InterruptSemanticVerdict | None:
        """Review only one playback-bound ambiguous final for the current epoch."""

        resolver = self._interrupt_semantic_resolver
        sticky = (
            self._sticky_interrupt_route
            if self._sticky_interrupt_epoch == self._speaker_epoch
            else None
        )
        if (
            resolver is None
            or canonical_speech_epoch is None
            or canonical_speech_epoch != self._speaker_epoch
            or self._interrupt_semantic_speech_epoch != canonical_speech_epoch
            or self._interrupt_semantic_playback_epoch is None
            or sticky is None
            or sticky.intent is not UtteranceIntent.INTERRUPT_THEN_CHAT
            or self._route_candidate(text).intent is not UtteranceIntent.INTERRUPT_THEN_CHAT
        ):
            return None
        if (
            self._interrupt_semantic_result_epoch == canonical_speech_epoch
            and self._interrupt_semantic_result_fence is not None
            and self._interrupt_semantic_result_fence.matches(self.fence)
            and self._interrupt_semantic_result is not None
        ):
            return self._interrupt_semantic_result

        request_fence = self.fence
        request_playback_epoch = self._interrupt_semantic_playback_epoch
        try:
            verdict = await resolver(
                text,
                self._sticky_interrupt_text or sticky.normalized_text,
                self._interrupt_semantic_assistant_text,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("interrupt semantic resolver unavailable", exc_info=True)
            verdict = InterruptSemanticVerdict.UNSURE
        if not isinstance(verdict, InterruptSemanticVerdict):
            verdict = InterruptSemanticVerdict.UNSURE
        if (
            canonical_speech_epoch != self._speaker_epoch
            or self._interrupt_semantic_speech_epoch != canonical_speech_epoch
            or request_playback_epoch != self._interrupt_semantic_playback_epoch
            or not request_fence.matches(self.fence)
        ):
            self.mark_audio_event(
                "interrupt_semantic_stale",
                status="ignored",
                detail={
                    "request_speech_epoch": canonical_speech_epoch,
                    "current_speech_epoch": self._speaker_epoch,
                    "request_playback_epoch": request_playback_epoch,
                    "current_playback_epoch": self._interrupt_semantic_playback_epoch,
                },
            )
            return InterruptSemanticVerdict.UNSURE

        self._interrupt_semantic_result_epoch = canonical_speech_epoch
        self._interrupt_semantic_result_fence = request_fence
        self._interrupt_semantic_result = verdict
        self.mark_audio_event(
            "interrupt_semantic_resolved",
            status="degraded" if verdict is InterruptSemanticVerdict.UNSURE else "ok",
            detail={
                "verdict": verdict,
                "speech_epoch": canonical_speech_epoch,
                "playback_epoch": request_playback_epoch,
            },
        )
        return verdict

    def set_false_interrupt_recover(self, recover: Callable[[], Awaitable[None]] | None) -> None:
        """Called when LiveKit already stopped audio but speaker gate rejected barge-in."""
        self._false_interrupt_recover = recover

    def set_user_turn_clearer(self, clearer: Callable[[], None] | None) -> None:
        """Inject LiveKit's synchronous user-turn reset at the session boundary."""

        self._user_turn_clearer = clearer

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
        if self._user_turn_clearer is None or self._cleared_control_epoch == target_epoch:
            return
        try:
            self._user_turn_clearer()
        except Exception:
            logger.warning(
                "control user-turn clear failed cause=%s session_id=%s",
                cause,
                self.session_id,
                exc_info=True,
            )
            self.mark_audio_event(
                "control_user_turn_cleared",
                status="error",
                detail={"cause": cause},
            )
            return
        self._cleared_control_epoch = target_epoch
        self.mark_audio_event(
            "control_user_turn_cleared",
            detail={"cause": cause},
        )

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

    def _assistant_was_mid_reply(self, *, was_speaking: bool) -> bool:
        if was_speaking or self._playback_fence is not None:
            return True
        if self._playback_started_ns is None:
            return False
        # Cover the gap after LiveKit flips agent to listening but before our
        # interrupt handler runs (logs showed ~1s gap and silent death).
        age_ms = (time.monotonic_ns() - self._playback_started_ns) // 1_000_000
        return age_ms < 8_000

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
        if self._set_interruption_min_words is not None:
            self._set_interruption_min_words(self._base_interruption_min_words)
        self.publish_assistant_audio("restore", gain=1.0)
        self._was_speaking = False
        # LiveKit can emit playback_finished after we hand the floor back.
        # Keep its original fence until that callback finalizes the heard
        # assistant segment; a subsequent user turn or new playback replaces it.
        self._fresh_user_speech = True
        self._last_listen_restore_ns = time.monotonic_ns()
        # Always force listening after control so UI/logs match.
        if self.interaction_phase is not InteractionPhase.LISTENING:
            self.set_interaction_phase(
                InteractionPhase.LISTENING,
                cause=f"restore_listen:{cause}",
            )
        self.mark_audio_event(
            "listen_restored_after_control",
            detail={
                "cause": cause,
                "min_words": self._base_interruption_min_words,
            },
        )

    async def _maybe_say_interrupt_yield(
        self,
        *,
        cause: str,
        route: UtteranceRoute | None = None,
        speech_epoch: int | None = None,
    ) -> None:
        """Short ack after mid-reply stop so users know we yielded, not crashed."""
        if self._interrupt_yield is None:
            return
        if cause in {"user_button", "stop_response", "rtc_recovered"}:
            return
        yield_speech_epoch = self._speaker_epoch if speech_epoch is None else speech_epoch
        if self._last_interrupt_yield_speech_epoch == yield_speech_epoch:
            self.mark_audio_event(
                "interrupt_yield_duplicate",
                status="ignored",
                detail={
                    "cause": cause,
                    "speech_epoch": yield_speech_epoch,
                },
            )
            self._restore_listen_after_control(cause=f"yield_duplicate:{cause}")
            return
        now = time.monotonic_ns()
        if (
            self._last_interrupt_yield_ns is not None
            and (now - self._last_interrupt_yield_ns) // 1_000_000
            < self.INTERRUPT_YIELD_COOLDOWN_MS
        ):
            # Still unlock listening if barge-in yield already fired.
            self._restore_listen_after_control(cause=f"yield_cooldown:{cause}")
            return
        self._last_interrupt_yield_ns = now
        self._last_interrupt_yield_speech_epoch = yield_speech_epoch
        candidate = self._interrupt_candidate_text()
        final_route = route or self._route_candidate(candidate)
        phrase = final_route.ack_phrase or interrupt_ack_phrase(candidate)
        self.mark_audio_event(
            "interrupt_yield_started",
            detail={
                "cause": cause,
                "ack_len": len(phrase),
                "candidate_len": len(candidate),
                "intent": final_route.intent,
                "reason": final_route.reason,
            },
        )
        try:
            await self._interrupt_yield(phrase)
            self.mark_audio_event(
                "interrupt_yield_done",
                detail={"cause": cause, "ack_len": len(phrase)},
            )
        except Exception:
            logger.warning("interrupt yield failed cause=%s", cause, exc_info=True)
            self.mark_audio_event("interrupt_yield_done", status="error")
        finally:
            self._restore_listen_after_control(cause=f"yield_done:{cause}")

    async def _maybe_recover_false_interrupt(self, *, cause: str) -> None:
        """Resume after nearby noise stopped playout but speaker gate rejected it."""
        if self._false_interrupt_recover is None:
            return
        now = time.monotonic_ns()
        if (
            self._last_false_recover_ns is not None
            and (now - self._last_false_recover_ns) // 1_000_000
            < self.FALSE_INTERRUPT_RECOVER_COOLDOWN_MS
        ):
            return
        self._last_false_recover_ns = now
        self.mark_audio_event(
            "false_interrupt_recover_started",
            detail={"cause": cause},
        )
        try:
            await self._false_interrupt_recover()
            self.mark_audio_event(
                "false_interrupt_recover_done",
                detail={"cause": cause},
            )
        except Exception:
            logger.warning("false interrupt recover failed cause=%s", cause, exc_info=True)
            self.mark_audio_event(
                "false_interrupt_recover_done",
                status="error",
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
        interrupt_precondition: Callable[[], bool] | None,
        admission_state: list[bool],
        expected_speaker_epoch: int | None,
        expected_playback_epoch: int | None,
        expected_pending_text_epoch: int | None,
    ) -> GenerationFence:
        owner_cmd = barge_route.speaker_gate_override
        control_only = barge_route.should_interrupt and not barge_route.enter_chat

        def _record_explicit_interrupt() -> None:
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

        if owner_cmd and create_user_turn and interrupt_precondition is None:
            _record_explicit_interrupt()
        old_fence = self.fence
        new_fence = await self.orchestrator.confirm_interruption(
            cause=cause,
            stop_playback=stop_playback,
            precondition=interrupt_precondition,
            create_user_turn=create_user_turn,
            synchronized_transcript=synchronized_transcript,
            force_generation_bump=force_generation_bump,
        )
        if interrupt_precondition is not None and not admission_state[0]:
            self.mark_audio_event(
                "trusted_interrupt_stale",
                status="ignored",
                detail={
                    "expected_speaker_epoch": expected_speaker_epoch,
                    "expected_playback_epoch": expected_playback_epoch,
                    "current_speaker_epoch": self._speaker_epoch,
                    "current_playback_epoch": self._playback_epoch,
                },
            )
            return new_fence
        if owner_cmd and create_user_turn and interrupt_precondition is not None:
            _record_explicit_interrupt()
        self._bind_mode_policy(new_fence)
        self.publish_assistant_audio("restore", gain=1.0)
        if create_user_turn and control_only:
            self._clear_control_user_turn(cause=f"interrupt:{cause}")
        self._was_speaking = False
        self._last_playback_completed_ns = None
        if (
            expected_pending_text_epoch is None
            or self._pending_assistant_text_epoch == expected_pending_text_epoch
        ):
            self._pending_assistant_text = ""
        if self._set_interruption_min_words is not None:
            self._set_interruption_min_words(self._base_interruption_min_words)
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
            if (
                create_user_turn
                and mid_reply
                and control_only
                and cause
                not in {
                    "user_button",
                    "stop_response",
                    "rtc_recovered",
                }
            ):
                self._spawn(
                    self._maybe_say_interrupt_yield(
                        cause=cause,
                        speech_epoch=(
                            expected_speaker_epoch
                            if expected_speaker_epoch is not None
                            else self._speaker_epoch
                        ),
                    ),
                    name="interrupt-yield",
                )
            else:
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
        self.cancel_listener_cue()
        keyword_binding = self._pending_keyword_interrupt_binding
        expected_speaker_epoch: int | None
        expected_playback_epoch: int | None
        expected_playback_fence: GenerationFence | None
        if keyword_binding is not None and keyword_binding.speaker_epoch == self._speaker_epoch:
            expected_speaker_epoch = keyword_binding.speaker_epoch
            expected_playback_epoch = keyword_binding.playback_epoch
            expected_playback_fence = keyword_binding.fence
        else:
            keyword_binding = None
            expected_speaker_epoch = (
                self._speaker_epoch
                if self._trusted_unanchored_control_epoch == self._speaker_epoch
                else None
            )
            expected_playback_epoch = (
                self._trusted_unanchored_playback_epoch
                if expected_speaker_epoch is not None
                else None
            )
            expected_playback_fence = (
                self._playback_fence if expected_playback_epoch is not None else None
            )
        expected_pending_text_epoch = (
            self._pending_assistant_text_epoch if expected_playback_epoch is not None else None
        )
        admission_state = [expected_playback_epoch is None]

        def _trusted_interrupt_is_current() -> bool:
            admission_state[0] = bool(
                self.trusted_aec_playback_control
                and expected_speaker_epoch is not None
                and self._speaker_epoch == expected_speaker_epoch
                and (
                    (
                        keyword_binding is not None
                        and self._pending_keyword_interrupt_binding == keyword_binding
                    )
                    or (
                        keyword_binding is None
                        and self._trusted_unanchored_control_epoch == expected_speaker_epoch
                        and self._trusted_unanchored_playback_epoch == expected_playback_epoch
                    )
                )
                and self._playback_epoch == expected_playback_epoch
                and self._was_speaking
                and (
                    expected_playback_fence is None
                    or (
                        self._playback_fence is not None
                        and self._playback_fence.matches(expected_playback_fence)
                    )
                )
            )
            return admission_state[0]

        interrupt_precondition = (
            _trusted_interrupt_is_current if expected_playback_epoch is not None else None
        )
        was_speaking = self._was_speaking
        mid_reply = self._assistant_was_mid_reply(was_speaking=was_speaking)
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
                # after _confirm_target_speaker_interrupt has checked the
                # current voiceprint; an unconfirmed LiveKit VAD interrupt
                # still waits for endpointed audio below.
                target_route = self._target_speaker_route(
                    context="interrupt",
                    explicit_interrupt=(barge_route.intent is UtteranceIntent.INTERRUPT_COMMAND),
                )
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
                target_route = self._target_speaker_route(
                    context="interrupt",
                    explicit_interrupt=False,
                )
            if self._target_focus_pending_epoch == self._speaker_epoch:
                self._target_focus_pending_epoch = None
            if not target_route.allow_input:
                self.speaker_verifier.mark_utterance_end()
                await self.orchestrator.dismiss_pending_interruption(
                    cause="target_speaker_reject_interrupt"
                )
                self._reject_target_speaker(context="interrupt", route=target_route)
                if mid_reply and stop_playback is None:
                    self._spawn(
                        self._maybe_recover_false_interrupt(cause=cause),
                        name="false-interrupt-recover",
                    )
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
            if mid_reply and stop_playback is None:
                self._spawn(
                    self._maybe_recover_false_interrupt(cause=cause),
                    name="false-interrupt-recover",
                )
            return self.fence

        async def _apply() -> GenerationFence:
            return await self._apply_real_interrupt(
                cause=cause,
                stop_playback=stop_playback,
                create_user_turn=create_user_turn,
                synchronized_transcript=synchronized_transcript,
                force_generation_bump=force_generation_bump,
                candidate=candidate,
                barge_route=barge_route,
                mid_reply=mid_reply,
                interrupt_precondition=interrupt_precondition,
                admission_state=admission_state,
                expected_speaker_epoch=expected_speaker_epoch,
                expected_playback_epoch=expected_playback_epoch,
                expected_pending_text_epoch=expected_pending_text_epoch,
            )

        if interrupt_precondition is not None:
            async with self._playback_control_lock:
                return await _apply()
        return await _apply()

    async def on_playback_started(self) -> None:
        async with self._playback_control_lock:
            self._invalidate_trusted_unanchored_control(reason="playback_replaced")
            self._pending_keyword_interrupt_binding = None
            self._playback_epoch += 1
            if self._set_interruption_min_words is not None:
                self._set_interruption_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
            self._last_playback_completed_ns = None
            self._playback_started_ns = time.monotonic_ns()
            if self._playback_fence is None or not self._playback_fence.matches(self.fence):
                self._playback_fence = self.fence
                self._played_assistant_text = ""
            await self.on_assistant_speaking(self._pending_assistant_text)
            self.mark_audio_event("playback_started")
            self.set_interaction_phase(InteractionPhase.SPEAKING, cause="playback_started")

    async def on_playback_finished(
        self,
        *,
        playback_position_s: float,
        interrupted: bool,
        synchronized_transcript: str | None,
    ) -> None:
        if interrupted:
            await self._apply_playback_finished(
                playback_position_s=playback_position_s,
                interrupted=True,
                synchronized_transcript=synchronized_transcript,
            )
            return
        async with self._playback_control_lock:
            await self._apply_playback_finished(
                playback_position_s=playback_position_s,
                interrupted=False,
                synchronized_transcript=synchronized_transcript,
            )

    async def _apply_playback_finished(
        self,
        *,
        playback_position_s: float,
        interrupted: bool,
        synchronized_transcript: str | None,
    ) -> None:
        self._pending_keyword_interrupt_binding = None
        self._invalidate_trusted_unanchored_control(reason="playback_finished")
        self.publish_assistant_audio("restore", gain=1.0)
        if interrupted:
            interrupted_from = self._playback_fence or self.fence
            combined_heard = self._combine_played_text(
                self._played_assistant_text,
                synchronized_transcript,
            )
            await self.on_real_interrupt(
                cause="livekit_playback_interrupted",
                # Completed segments are facts; if alignment is unavailable for the
                # interrupted segment, conservatively omit that partial segment.
                synchronized_transcript=combined_heard,
            )
            await self.on_media_playback_interrupted(
                interrupted_from=interrupted_from,
                synchronized_transcript=combined_heard,
            )
        else:
            _ = playback_position_s
            self._played_assistant_text = self._combine_played_text(
                self._played_assistant_text,
                synchronized_transcript,
            )
            # Always clear speaking flag. Fixed session.say(add_to_chat_ctx=False)
            # may never emit conversation_item_added → on_assistant_reply_completed,
            # which used to leave _was_speaking stuck and block enroll PCM forever.
            self._was_speaking = False
            self._last_playback_completed_ns = time.monotonic_ns()
            self._clear_unanchored_playback_transcript()
            if self._played_assistant_text:
                self.publish_transcript(
                    speaker="assistant",
                    text=self._played_assistant_text,
                    final=False,
                    heard=True,
                    fence=self.fence,
                )

    async def on_assistant_reply_completed(self, text: str) -> None:
        """Commit one fully played reply after LiveKit adds its conversation item."""
        text_delivered = self._text_only_delivery
        if text_delivered and self.orchestrator.state is ConversationState.THINKING:
            await self.orchestrator.begin_speaking([], text)
        if self.orchestrator.state is not ConversationState.SPEAKING:
            return
        # Voice items arrive after playout; text-only items arrive after the
        # complete assistant text has been published to the linked participant.
        heard = text
        reply_fence = self._playback_fence or self.fence
        heard = await self.orchestrator.finish_livekit_playback(
            tools_active=self._pending_tool_results > 0,
            synchronized_transcript=heard,
            reply_fence=reply_fence,
        )
        self._was_speaking = False
        self._pending_assistant_text = ""
        self._played_assistant_text = heard
        self._playback_fence = None
        self._last_playback_completed_ns = time.monotonic_ns()
        self._clear_unanchored_playback_transcript()
        self.publish_assistant_state(
            "tool_waiting" if self._pending_tool_results > 0 else "listening"
        )
        if heard:
            self.publish_transcript(
                speaker="assistant",
                text=heard,
                final=True,
                heard=not text_delivered,
                text_delivered=text_delivered,
                fence=reply_fence,
            )
            self._schedule_context_snapshot_prepare()

    def should_ignore_post_playback_backchannel(
        self,
        text: str,
        *,
        now_ns: int | None = None,
    ) -> bool:
        return self.post_playback_guard_reason(text, now_ns=now_ns) is not None

    def post_playback_guard_reason(
        self,
        text: str,
        *,
        now_ns: int | None = None,
    ) -> str | None:
        completed_ns = self._last_playback_completed_ns
        if completed_ns is None:
            return None
        elapsed_ms = ((now_ns or time.monotonic_ns()) - completed_ns) // 1_000_000
        if not 0 <= elapsed_ms <= POST_PLAYBACK_ECHO_GUARD_MS:
            return None
        reason = self.input_guard.guarded_reason(
            text,
            duration_ms=int(elapsed_ms),
            assistant_text=self._played_assistant_text,
        )
        if reason == "assistant_echo":
            return reason
        if elapsed_ms <= POST_PLAYBACK_BACKCHANNEL_GUARD_MS and (
            reason == "backchannel" or self.input_guard.enabled
        ):
            return reason
        return None

    @staticmethod
    def _combine_played_text(existing: str, segment: str | None) -> str:
        if not segment:
            return existing
        if segment.startswith(existing):
            return segment
        if existing.endswith(segment):
            return existing
        return existing + segment

    def attach_session_events(self, session: Any) -> None:
        """Subscribe to public AgentSession events; playout is attached separately."""

        interruption = getattr(getattr(session, "options", None), "interruption", None)
        base_min_words = (
            int(interruption.get("min_words", 0)) if isinstance(interruption, dict) else 0
        )
        false_timeout = (
            float(interruption.get("false_interruption_timeout") or 0.8)
            if isinstance(interruption, dict)
            else 0.8
        )
        false_resume_task: asyncio.Task[Any] | None = None

        def _set_min_words(value: int) -> None:
            if isinstance(interruption, dict):
                interruption["min_words"] = value

        self._set_interruption_min_words = _set_min_words
        self._base_interruption_min_words = base_min_words

        def _cancel_false_resume() -> None:
            nonlocal false_resume_task
            if false_resume_task is not None:
                false_resume_task.cancel()
                false_resume_task = None

        def _on_user_state(ev: Any) -> None:
            nonlocal false_resume_task
            state = str(getattr(ev, "new_state", ""))
            if state == "speaking":
                _cancel_false_resume()
                decision = self.on_user_voice_started()
                interaction = self.decide_interaction(
                    InteractionSnapshot(
                        event=InteractionEvent.VAD_START,
                        assistant_speaking=self._was_speaking and self.barge_in_enabled,
                        has_speech_energy=True,
                    )
                )
                self.apply_interaction_decision(interaction)
                if interaction.duck_output:
                    self.publish_assistant_audio("duck", gain=0.0)
                    self.mark_audio_event("barge_in_detected")
                if decision is PlaybackInputDecision.WAIT:
                    _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                else:
                    _set_min_words(base_min_words)
                    if interaction.duck_output and decision is PlaybackInputDecision.IGNORE:
                        self.publish_assistant_audio("restore", gain=1.0)
                return
            if state == "listening":
                self.on_user_voice_stopped()
                if (
                    self.input_guard.candidate_active
                    and (self.input_guard.candidate_during_playback or self._was_speaking)
                    and self.input_guard.candidate_decision is PlaybackInputDecision.WAIT
                ):
                    candidate_started = self.input_guard.candidate_started_ns

                    async def _resume_false_interruption() -> None:
                        await asyncio.sleep(false_timeout)
                        if (
                            self.input_guard.candidate_active
                            and self.input_guard.candidate_started_ns == candidate_started
                            and self.input_guard.candidate_decision is PlaybackInputDecision.WAIT
                        ):
                            _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                            self.publish_assistant_audio("restore", gain=1.0)
                            self.orchestrator.metrics.inc_false_interruptions()

                    false_resume_task = self._spawn(
                        _resume_false_interruption(),
                        name="duplex-false-interruption-resume",
                    )

        def _on_agent_state(ev: Any) -> None:
            state = getattr(ev, "new_state", None) or getattr(ev, "state", None)
            if state is None and isinstance(ev, str):
                state = ev
            state_s = str(state)
            # Mic/VAD/ASR must remain open while assistant speaks.
            if state_s in ("speaking", "thinking"):
                assert self.orchestrator.mic_open and self.orchestrator.asr_active
            mapped = {
                "initializing": "connecting",
                "idle": "listening",
                "listening": "listening",
                # Quiet planning before first audio: explicit thinking_silent phase.
                "thinking": "thinking_silent",
                "speaking": "speaking",
            }.get(state_s)
            if mapped is not None:
                # Do not clobber an active listener backchannel with idle/listening.
                if mapped == "listening" and self.interaction_phase is InteractionPhase.BACKCHANNEL:
                    return
                self.publish_assistant_state(mapped)

        def _on_user_transcript(ev: Any) -> None:
            text = str(getattr(ev, "transcript", "") or "")
            if text:
                final = bool(getattr(ev, "is_final", False))
                decision = self.observe_user_transcript(text, final=final)
                if self._was_speaking or self.input_guard.candidate_during_playback:
                    if decision is PlaybackInputDecision.WAIT:
                        return
                    _cancel_false_resume()
                    if decision is PlaybackInputDecision.IGNORE:
                        if final and self._target_focus_pending_epoch == self._speaker_epoch:
                            self._target_focus_pending_epoch = None
                        logger.info(
                            "playback_input_ignored reason=%s",
                            self.input_guard.candidate_reason or "playback_noise",
                        )
                        if self.barge_in_enabled:
                            self.publish_assistant_audio("restore", gain=1.0)
                        _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                        return
                    route = self._route_candidate()
                    if self._target_speaker_focus_enabled and self._speaker_classifier is not None:
                        _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                        if (
                            not final
                            and route.should_interrupt
                            and (
                                self._speaker_pcm
                                or self._trusted_unanchored_control_epoch == self._speaker_epoch
                            )
                        ):
                            # Verify a router-confirmed control phrase as soon
                            # as streaming ASR hears it. Waiting for VAD
                            # endpointing made「等一下」arrive after the reply
                            # had already finished; the classifier still gets
                            # the currently collected PCM and can reject a
                            # clear non-owner before the stop bridge runs.
                            self._start_speaker_classification()
                            self._spawn(
                                self._confirm_target_speaker_interrupt(self._speaker_epoch),
                                name="target-speaker-explicit-partial",
                            )
                            return
                        if final:
                            if self._target_focus_pending_epoch == self._speaker_epoch:
                                self._target_focus_pending_epoch = None
                            self._spawn(
                                self._confirm_target_speaker_interrupt(self._speaker_epoch),
                                name="target-speaker-playback-focus",
                            )
                        return
                    if (
                        decision is PlaybackInputDecision.ACCEPT
                        and (final or route.should_interrupt)
                        and self._request_playback_interrupt()
                    ):
                        return
                    _set_min_words(base_min_words)
                if (
                    final
                    and self._target_speaker_focus_enabled
                    and self._speaker_classifier is not None
                    and self._target_focus_pending_epoch == self._speaker_epoch
                ):
                    self._target_focus_pending_epoch = None
                    _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                    self._spawn(
                        self._confirm_target_speaker_interrupt(self._speaker_epoch),
                        name="target-speaker-playback-focus",
                    )
                    return
                if not final:
                    self.publish_transcript(
                        speaker="user",
                        text=text,
                        final=False,
                        fence=self.fence,
                    )

        def _on_conversation_item(ev: Any) -> None:
            item = getattr(ev, "item", None)
            role = getattr(item, "role", None) if item is not None else None
            if str(role) == "user":
                # Only raise min_words while assistant is mid-reply. Always
                # locking to 1000 after every user item left the session deaf
                # after「停一下」(orphan FINAL + blocked next turn).
                if self._was_speaking or self.interaction_phase in {
                    InteractionPhase.SPEAKING,
                    InteractionPhase.THINKING_SILENT,
                }:
                    _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                else:
                    _set_min_words(base_min_words)
                return
            if str(role) != "assistant":
                return
            text = ""
            if item is not None:
                content = getattr(item, "text_content", None)
                if callable(content):
                    text = str(content() or "")
                elif content is not None:
                    text = str(content)
            if text and not bool(getattr(item, "interrupted", False)):
                self._spawn(
                    self.on_assistant_reply_completed(text),
                    name="duplex-assistant-reply-completed",
                )

        def _make_unsub(event_name: str, event_handler: Any) -> Callable[[], None]:
            def _unsub() -> None:
                session.off(event_name, event_handler)

            return _unsub

        for name, handler in (
            ("agent_state_changed", _on_agent_state),
            ("user_state_changed", _on_user_state),
            ("user_input_transcribed", _on_user_transcript),
            ("conversation_item_added", _on_conversation_item),
        ):
            try:
                session.on(name, handler)
                self._unsubscribers.append(_make_unsub(name, handler))
            except Exception:
                logger.warning("could not attach session event %s", name, exc_info=True)

    def attach_playback_events(self, audio_output: Any) -> None:
        """Use LiveKit's actual playout position and synchronized transcript as facts."""

        def _on_started(_ev: Any) -> None:
            self._spawn(self.on_playback_started(), name="duplex-playback-started")

        def _on_finished(ev: Any) -> None:
            self._spawn(
                self.on_playback_finished(
                    playback_position_s=float(getattr(ev, "playback_position", 0.0) or 0.0),
                    interrupted=bool(getattr(ev, "interrupted", False)),
                    synchronized_transcript=getattr(ev, "synchronized_transcript", None),
                ),
                name="duplex-playback-finished",
            )

        for name, handler in (
            ("playback_started", _on_started),
            ("playback_finished", _on_finished),
        ):
            audio_output.on(name, handler)

            def _unsub(
                event_name: str = name,
                event_handler: Any = handler,
            ) -> None:
                audio_output.off(event_name, event_handler)

            self._unsubscribers.append(_unsub)

    async def close(self) -> None:
        self.cancel_listener_cue()
        self._set_interruption_min_words = None
        for unsub in self._unsubscribers:
            with contextlib.suppress(Exception):
                unsub()
        # A classification already in flight can still emit the final
        # speaker.classified evidence. Give its own bounded provider timeout a
        # chance to finish before closing the evidence admission gate.
        speaker_task = self._speaker_classification_task
        if speaker_task is not None and not speaker_task.done():
            await asyncio.wait(
                {speaker_task},
                timeout=self._speaker_classify_timeout_s + 0.1,
            )
        # Stop admitting new evidence, then give already-created durable tasks
        # a bounded drain window. ArchiveSink persists a task before a timeout
        # cancellation can propagate.
        self._evidence_publisher = None
        durable_tasks = tuple(self._durable_tasks)
        if durable_tasks:
            _, pending = await asyncio.wait(
                durable_tasks,
                timeout=self._evidence_drain_timeout_s,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for task in tuple(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await self.orchestrator.close()
        if self._durable_task_errors:
            raise RuntimeError("one or more durable evidence tasks failed") from (
                self._durable_task_errors[0]
            )
