"""Wire Duplex Orchestrator invariants into the LiveKit AgentSession path (ch.8–18)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

from services.agent.src.contracts.events import UI_EVENT_TYPES, TimedWord
from services.agent.src.contracts.ids import GenerationFence, new_session_id
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.observability.tracing import LatencyTrace
from services.agent.src.orchestration.cue_scheduler import CueScheduler, ListenerCue
from services.agent.src.orchestration.emotion import (
    EmotionObservation,
    EmotionSmoother,
    neutral_observation,
)
from services.agent.src.orchestration.heard_text_tracker import HeardTextTracker
from services.agent.src.orchestration.interruption_guard import (
    PlaybackInputDecision,
    PlaybackInputGuard,
    interrupt_ack_phrase,
)
from services.agent.src.orchestration.orchestrator import Orchestrator, TTSPoolHandle
from services.agent.src.orchestration.phrase_segmenter import PhraseSegmenter
from services.agent.src.orchestration.prosody import (
    SpeechPlan,
    speech_plan_for_emotion,
    speech_plan_for_turn,
)
from services.agent.src.orchestration.speaker_verify import (
    SpeakerGateState,
    SpeakerVerifier,
    voiced_stats_from_pcm,
)
from services.agent.src.orchestration.state_machine import ConversationState, InteractionPhase
from services.agent.src.orchestration.utterance_router import (
    TargetSpeakerRoute,
    UtteranceIntent,
    UtteranceRoute,
    route_speaker_gate,
    route_target_speaker,
    route_utterance,
)
from services.common.evidence_policy import classify_prompt_kind
from services.common.redaction import redact_pii
from services.speaker.domain import (
    SpeakerDecision,
    SpeakerPermissions,
    permissions_for_speaker,
)

logger = logging.getLogger(__name__)

ResumeSpeakerBinding = tuple[str, str, int | None, str]

POST_PLAYBACK_ECHO_GUARD_MS = 800
PLAYBACK_INPUT_BLOCK_MIN_WORDS = 1000
TARGET_SPEAKER_MIN_PCM_MS = 600
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
CLIENT_AUDIO_TRACE_NAMES = frozenset(
    {
        "audio_unlock",
        "session_created",
        "room_connected",
        "agent_ready",
        "track_subscribed",
        "audio_attached",
        "play_resolved",
        "play_rejected",
        "playing",
        "first_playback",
        "media_error",
        "webrtc_inbound_audio",
    }
)
CLIENT_AUDIO_METRIC_NAMES = frozenset(
    {
        "jitter",
        "packets_lost",
        "packets_received",
        "packets_discarded",
        "packets_lost_delta",
        "packets_received_delta",
        "packets_discarded_delta",
        "bytes_received",
        "nack_count",
        "concealed_samples",
        "concealed_samples_delta",
        "silent_concealed_samples",
        "total_samples_received",
        "total_samples_received_delta",
        "concealment_events",
        "concealment_ratio",
        "non_silent_concealment_ratio",
        "jitter_buffer_delay",
        "jitter_buffer_target_delay",
        "jitter_buffer_minimum_delay",
        "jitter_buffer_emitted_count",
        "total_samples_duration",
        "average_jitter_buffer_delay_ms",
        "average_jitter_buffer_target_delay_ms",
        "encoded_audio_bitrate_kbps",
        "inserted_samples_for_deceleration",
        "removed_samples_for_acceleration",
    }
)

_TRANSCRIPT_BOUNDARY_CHARS = " \t\r\n。！？.!?，,；;：:\"'“”‘’（）()【】[]"


def _strip_matching_transcript_prefix(text: str, prefix: str) -> tuple[bool, str]:
    """Strip a previously observed ASR prefix while tolerating punctuation rewrites."""

    expected = "".join(char.casefold() for char in prefix if char.isalnum())
    actual = "".join(char.casefold() for char in text if char.isalnum())
    if len(expected) < 6 or len(actual) <= len(expected):
        return False, text
    matched = 0
    for index, char in enumerate(text):
        if not char.isalnum():
            continue
        folded = char.casefold()
        if len(folded) != 1 or folded != expected[matched]:
            return False, text
        matched += 1
        if matched == len(expected):
            return True, text[index + 1 :].lstrip(_TRANSCRIPT_BOUNDARY_CHARS)
    return False, text


@dataclass(frozen=True)
class CanonicalUserTurnSnapshot:
    """Transcript decisions frozen at one VAD speech-epoch boundary."""

    speech_epoch: int | None
    accepted_finals: tuple[str, ...]
    contaminated: bool
    suspected_playback_prefixes: tuple[str, ...]


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
class DuplexRuntime:
    """Session-scoped orchestration bound to LiveKit agent lifecycle."""

    orchestrator: Orchestrator
    tts: Any | None = None
    session_id: str = field(default_factory=new_session_id)
    input_guard: PlaybackInputGuard = field(default_factory=PlaybackInputGuard)
    latency_trace: LatencyTrace = field(default_factory=LatencyTrace)
    cue_scheduler: CueScheduler = field(default_factory=CueScheduler)
    emotion_smoother: EmotionSmoother = field(default_factory=EmotionSmoother)
    speech_plan: SpeechPlan = field(default_factory=lambda: speech_plan_for_emotion("neutral"))
    interaction_phase: InteractionPhase = InteractionPhase.CONNECTING
    use_paralinguistic_tags: bool = False
    speaker_verifier: SpeakerVerifier = field(default_factory=SpeakerVerifier)
    _unsubscribers: list[Callable[[], None]] = field(default_factory=list)
    _was_speaking: bool = False
    _pending_assistant_text: str = ""
    _played_assistant_text: str = ""
    _next_user_prompt_kind: str = "spontaneous"
    _fresh_user_speech: bool = False
    _accepted_user_finals: list[str] = field(default_factory=list)
    _user_transcript_contaminated: bool = False
    _suspected_playback_prefixes: list[str] = field(default_factory=list)
    _canonical_speech_epoch: int | None = None
    _canonical_final_observed: bool = False
    _canonical_turn_snapshots: deque[CanonicalUserTurnSnapshot] = field(default_factory=deque)
    _consumed_canonical_speech_epoch: int | None = None
    _persona_evidence_eligible: bool = False
    _playback_fence: GenerationFence | None = None
    _last_playback_completed_ns: int | None = None
    _set_interruption_min_words: Callable[[int], None] | None = None
    _base_interruption_min_words: int = 0
    _event_publisher: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None = None
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
    _history_eligible_by_fence: dict[tuple[int, int], bool] = field(default_factory=dict)
    _owner_projection_eligible_by_fence: dict[tuple[int, int], bool] = field(
        default_factory=dict
    )
    _mode_policy: ModePolicy = field(default_factory=lambda: ModePolicy.unavailable("not_fetched"))
    _mode_policy_by_fence: dict[tuple[int, int], ModePolicy] = field(default_factory=dict)
    _response_provenance_by_fence: dict[
        tuple[int, int, int],
        dict[str, Any],
    ] = field(default_factory=dict)
    _target_speaker_focus_enabled: bool = False
    _reject_non_owner_voice: bool = True
    _target_focus_epoch: int | None = None
    _target_focus_pending_epoch: int | None = None
    _target_speaker_interrupt: Callable[[], Awaitable[None]] | None = None
    _sticky_interrupt_epoch: int | None = None
    _sticky_interrupt_route: UtteranceRoute | None = None
    _result_speaker: Callable[[str], Any] | None = None
    _interrupt_yield: Callable[[str], Awaitable[None]] | None = None
    _false_interrupt_recover: Callable[[], Awaitable[None]] | None = None
    _user_turn_clearer: Callable[[], None] | None = None
    _cleared_control_epoch: int | None = None
    _paused_reply_available: bool = False
    _reply_speaker_binding: ResumeSpeakerBinding | None = None
    _paused_reply_binding: ResumeSpeakerBinding | None = None
    _resume_pending: bool = False
    _resume_fence: GenerationFence | None = None
    _last_interrupt_yield_ns: int | None = None
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
    _pending_tool_results: int = 0
    _listener_cue_player: Callable[[str], Any] | None = None
    _active_listener_cue: ListenerCue | None = None
    _active_listener_cue_handle: Any | None = None
    _listener_cue_candidate_task: asyncio.Task[Any] | None = None
    _listener_cue_aec_healthy: bool = False
    _emotion_turn_observer: Callable[[int], None] | None = None
    _emotion_by_turn: dict[int, EmotionObservation] = field(default_factory=dict)
    _voice_profile_refresher: Callable[[], Coroutine[Any, Any, Any]] | None = None
    _voice_profile_refresh_task: asyncio.Task[Any] | None = None

    @classmethod
    def create(
        cls,
        *,
        session_id: str | None = None,
        tts: Any | None = None,
        input_guard_enabled: bool = False,
        listener_cues_enabled: bool = False,
        use_paralinguistic_tags: bool = False,
        speaker_verifier: SpeakerVerifier | None = None,
    ) -> DuplexRuntime:
        sid = session_id or new_session_id()
        orch = Orchestrator(session_id=sid)
        if tts is not None:
            orch.tts_pool = LiveKitTTSPoolAdapter(pool=tts.pool)
        return cls(
            orchestrator=orch,
            tts=tts,
            session_id=sid,
            input_guard=PlaybackInputGuard(enabled=input_guard_enabled),
            cue_scheduler=CueScheduler(enabled=listener_cues_enabled),
            use_paralinguistic_tags=use_paralinguistic_tags,
            speaker_verifier=speaker_verifier
            if speaker_verifier is not None
            else SpeakerVerifier(enabled=False),
        )

    @property
    def fence(self) -> GenerationFence:
        return self.orchestrator.fence

    @property
    def heard_tracker(self) -> HeardTextTracker:
        return self.orchestrator.heard_tracker

    @property
    def segmenter(self) -> PhraseSegmenter | None:
        return self.orchestrator.segmenter

    def gate_llm_token(self, fence: GenerationFence, token: str) -> str | None:
        return self.orchestrator.gate_llm_token(fence, token)

    def gate_tts_audio(self, fence: GenerationFence, pcm: bytes) -> bytes | None:
        return self.orchestrator.gate_tts_audio(fence, pcm)

    def gate_tool_result(self, fence: GenerationFence, payload: Any) -> Any | None:
        return self.orchestrator.gate_tool_result(fence, payload)

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

    def set_reject_non_owner_voice(self, reject: bool) -> None:
        self._reject_non_owner_voice = reject

    def set_mode_policy(self, policy: ModePolicy) -> None:
        """Install the sole Control-issued policy before this session starts."""

        if self.fence.turn_id or self.fence.generation_id:
            raise RuntimeError("interaction policy must be frozen before the first turn")
        self._mode_policy = policy
        self._bind_mode_policy(self.fence, policy)

    @property
    def mode_policy(self) -> ModePolicy:
        return self._mode_policy

    @property
    def mode_policy_enforced(self) -> bool:
        """False only during construction before an entrypoint freezes policy."""

        return self._mode_policy.unavailable_reason != "not_fetched"

    def mode_policy_for_fence(self, fence: GenerationFence) -> ModePolicy:
        return self._mode_policy_by_fence.get(
            (fence.turn_id, fence.generation_id),
            ModePolicy.unavailable("policy_not_bound_to_fence"),
        )

    def _bind_mode_policy(self, fence: GenerationFence, policy: ModePolicy | None = None) -> None:
        self._mode_policy_by_fence[(fence.turn_id, fence.generation_id)] = policy or self._mode_policy
        while len(self._mode_policy_by_fence) > HISTORY_ELIGIBILITY_MAX_FENCES:
            self._mode_policy_by_fence.pop(next(iter(self._mode_policy_by_fence)))

    @property
    def speaker_permissions(self) -> SpeakerPermissions:
        if self._speaker_decision is not None:
            return self._speaker_decision.permissions
        classification = self._speaker_class if self._speaker_class == "owner" else "uncertain"
        return permissions_for_speaker(classification)  # type: ignore[arg-type]

    def _current_history_eligible(self) -> bool:
        decision = self._speaker_decision
        return decision is not None and self._mode_policy.history_eligible(
            decision.classification,
            reason_code=decision.reason_code,
        )

    def _current_owner_projection_eligible(self) -> bool:
        decision = self._speaker_decision
        return decision is not None and self._mode_policy.owner_projection_eligible(
            decision.classification
        )

    def is_shadow_speaker(self) -> bool:
        return (
            self._speaker_decision is not None
            and self._speaker_decision.classification == "uncertain"
            and self._speaker_decision.reason_code == "shadow_owner_candidate"
        )

    def _bind_history_eligibility(self, fence: GenerationFence, eligible: bool) -> None:
        self._history_eligible_by_fence[(fence.turn_id, fence.generation_id)] = eligible
        while len(self._history_eligible_by_fence) > HISTORY_ELIGIBILITY_MAX_FENCES:
            self._history_eligible_by_fence.pop(next(iter(self._history_eligible_by_fence)))

    def _history_eligible(self, fence: GenerationFence) -> bool:
        return self._history_eligible_by_fence.get((fence.turn_id, fence.generation_id), False)

    def _bind_owner_projection_eligibility(
        self,
        fence: GenerationFence,
        eligible: bool,
    ) -> None:
        key = (fence.turn_id, fence.generation_id)
        self._owner_projection_eligible_by_fence[key] = eligible
        while len(self._owner_projection_eligible_by_fence) > HISTORY_ELIGIBILITY_MAX_FENCES:
            self._owner_projection_eligible_by_fence.pop(
                next(iter(self._owner_projection_eligible_by_fence))
            )

    def _owner_projection_eligible(self, fence: GenerationFence) -> bool:
        return self._owner_projection_eligible_by_fence.get(
            (fence.turn_id, fence.generation_id),
            False,
        )

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
        key = (fence.turn_id, fence.generation_id, fence.tool_epoch)
        self._response_provenance_by_fence[key] = json.loads(encoded)
        while len(self._response_provenance_by_fence) > RESPONSE_PROVENANCE_MAX_FENCES:
            self._response_provenance_by_fence.pop(
                next(iter(self._response_provenance_by_fence))
            )
        return True

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
        stored = self._response_provenance_by_fence.get(
            (fence.turn_id, fence.generation_id, fence.tool_epoch)
        )
        return json.loads(json.dumps(stored)) if stored is not None else None

    def set_result_speaker(self, speaker: Callable[[str], Any]) -> None:
        self._result_speaker = speaker

    def set_listener_cue_player(self, player: Callable[[str], Any]) -> None:
        self._listener_cue_player = player

    def set_listener_cue_aec_healthy(self, healthy: bool) -> None:
        self._listener_cue_aec_healthy = healthy

    def set_emotion_turn_observer(self, observer: Callable[[int], None]) -> None:
        self._emotion_turn_observer = observer

    def set_voice_profile_refresher(
        self,
        refresher: Callable[[], Coroutine[Any, Any, Any]],
    ) -> None:
        self._voice_profile_refresher = refresher

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

    def feed_speaker_pcm(self, pcm: bytes) -> None:
        if self._speaker_collecting and pcm:
            self._speaker_pcm.extend(pcm)
            if len(self._speaker_pcm) > 4 * 1024 * 1024:
                del self._speaker_pcm[: len(self._speaker_pcm) - 4 * 1024 * 1024]
        # Never enroll assistant TTS that leaks into the mic during playback.
        # After begin_speaker_enrollment we force _was_speaking=False so user
        # enroll speech is always collected.
        if (
            self.speaker_verifier.state is SpeakerGateState.PENDING
            and self._was_speaking
            and not getattr(self, "_enroll_collecting", False)
        ):
            return
        self.speaker_verifier.feed_pcm(pcm)

    def on_user_voice_stopped(self) -> None:
        if self._speaker_collecting:
            self.mark_audio_event("last_user_audio")
        self._speaker_collecting = False
        self.speaker_verifier.mark_utterance_end()
        self._start_speaker_classification()

    def _start_speaker_classification(self) -> None:
        if (
            self._speaker_classifier is None
            or not self._speaker_pcm
            or (
                self._speaker_classification_task is not None
                and not self._speaker_classification_task.done()
            )
        ):
            return
        epoch = self._speaker_epoch
        pcm = bytes(self._speaker_pcm)
        self._speaker_classification_task = self._spawn(
            self._classify_speaker(epoch, pcm),
            name=f"speaker-authority-{epoch}",
        )

    async def _classify_speaker(self, epoch: int, pcm: bytes) -> SpeakerDecision:
        assert self._speaker_classifier is not None
        try:
            decision = await asyncio.wait_for(
                self._speaker_classifier(pcm, self._speaker_sample_rate),
                timeout=self._speaker_classify_timeout_s,
            )
        except TimeoutError:
            decision = self._uncertain_speaker_decision("authority_timeout")
        except Exception:
            logger.warning("speaker authority unavailable", exc_info=True)
            decision = self._uncertain_speaker_decision("authority_unavailable")
        if epoch != self._speaker_epoch:
            return decision
        self._speaker_decision = decision
        self._speaker_class = decision.classification
        self._publish_speaker_decision(epoch, decision)
        return decision

    def _uncertain_speaker_decision(self, reason: str) -> SpeakerDecision:
        return SpeakerDecision(
            classification="uncertain",
            score=None,
            quality_score=0.0,
            reason_code=reason,
            model_version="unavailable",
            template_version=None,
            profile_id=None,
            permissions=permissions_for_speaker("uncertain"),
        )

    def _target_speaker_route(
        self,
        *,
        context: Literal["conversation", "interrupt"],
        explicit_interrupt: bool = False,
    ) -> TargetSpeakerRoute:
        if not self._target_speaker_focus_enabled:
            return TargetSpeakerRoute(allow_input=True, reason="target_focus_disabled")
        decision = self._speaker_decision or self._uncertain_speaker_decision(
            "authority_unconfigured"
            if self._speaker_classifier is None
            else "classification_pending"
        )
        pcm_duration_ms = int(len(self._speaker_pcm) * 1000 / max(1, self._speaker_sample_rate * 2))
        return route_target_speaker(
            classification=decision.classification,
            reason_code=decision.reason_code,
            profile_id=decision.profile_id,
            pcm_duration_ms=pcm_duration_ms,
            context=context,
            explicit_interrupt=explicit_interrupt,
            reject_non_owner_voice=self._reject_non_owner_voice,
        )

    def _reject_target_speaker(
        self,
        *,
        context: str,
        route: TargetSpeakerRoute,
    ) -> None:
        self.orchestrator.metrics.inc_guarded_user_input(route.reason)
        self.mark_audio_event(
            "target_speaker_rejected",
            detail={
                "context": context,
                "reason": route.reason,
                "pcm_duration_ms": int(
                    len(self._speaker_pcm) * 1000 / max(1, self._speaker_sample_rate * 2)
                ),
            },
        )

    async def _confirm_target_speaker_interrupt(self, epoch: int) -> None:
        """Release playback only for target voice or an explicit yield command."""
        if epoch != self._speaker_epoch or self._target_focus_epoch == epoch:
            return
        try:
            await self.await_speaker_classification()
        except asyncio.CancelledError:
            return
        if epoch != self._speaker_epoch or self._target_focus_epoch == epoch:
            return
        barge_route = self._route_candidate()
        route = self._target_speaker_route(
            context="interrupt",
            explicit_interrupt=barge_route.should_interrupt,
        )
        if not route.allow_input:
            self.input_guard.candidate_decision = PlaybackInputDecision.IGNORE
            self._reject_target_speaker(context="playback", route=route)
            if self._set_interruption_min_words is not None:
                self._set_interruption_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
            return
        callback = self._target_speaker_interrupt
        if callback is None:
            return
        self._target_focus_epoch = epoch
        await callback()

    def _publish_speaker_decision(self, epoch: int, decision: SpeakerDecision) -> None:
        if self._evidence_publisher is None:
            return
        event: dict[str, Any] = {
            "event_id": str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"memoria:speaker-classification:{self.session_id}:{epoch}",
                )
            ),
            "session_id": self.session_id,
            "event_type": "speaker.classified",
            "occurred_at": datetime.now(UTC).isoformat(),
            "speaker_class": decision.classification,
            "source": "speaker_authority.formal_embedding",
            "turn_id": self.fence.turn_id + 1,
            "generation_id": self.fence.generation_id,
            "payload": {
                "score": decision.score,
                "quality_score": decision.quality_score,
                "reason_code": decision.reason_code,
                "model_version": decision.model_version,
                "template_version": decision.template_version,
                "profile_id": decision.profile_id,
            },
        }
        event["payload"].update(
            self._mode_policy_provenance(
                self.fence,
                decision.classification,
                reason_code=decision.reason_code,
            )
        )
        self._spawn(
            self._evidence_publisher(event),
            name="duplex-evidence-speaker-classified",
            durable=True,
        )

    async def await_speaker_classification(self) -> SpeakerDecision:
        self._start_speaker_classification()
        task = self._speaker_classification_task
        if task is None:
            decision = self._uncertain_speaker_decision(
                "authority_unconfigured" if self._speaker_classifier is None else "no_audio"
            )
            self._speaker_decision = decision
            self._speaker_class = "uncertain"
            return decision
        result = await task
        if not isinstance(result, SpeakerDecision):  # pragma: no cover - task contract guard
            return self._uncertain_speaker_decision("authority_invalid")
        return result

    def begin_speaker_enrollment(self) -> None:
        # Fixed session.say may leave _was_speaking stuck True (playback_finished
        # non-interrupt path used to not clear it). Clear so enroll PCM is fed.
        self._was_speaking = False
        self._enroll_collecting = True
        self._enroll_started_mono = time.monotonic()
        self.speaker_verifier.begin_enrollment()
        self.publish_assistant_state("speaker_enroll")
        self.mark_audio_event("speaker_enroll_started")

    def poll_speaker_enrollment(self, *, force: bool = False) -> dict[str, object] | None:
        wall_ms: int | None = None
        started = getattr(self, "_enroll_started_mono", None)
        if started is not None:
            wall_ms = int((time.monotonic() - started) * 1000)
        result = self.speaker_verifier.try_finalize_enrollment(
            force=force,
            wall_elapsed_ms=wall_ms,
        )
        if result is None:
            # Avoid spamming LiveKit data channel every poll tick.
            progress = self.speaker_verifier.enrollment_progress()
            speech_ms = int(progress["speech_ms"])
            last = getattr(self, "_last_enroll_progress_speech_ms", -1)
            if speech_ms - last >= 500 or speech_ms == 0 and last < 0:
                self._last_enroll_progress_speech_ms = speech_ms
                self._publish(
                    {
                        "type": "speaker_enroll_progress",
                        "session_id": self.session_id,
                        "state": progress["state"],
                        "speech_ms": progress["speech_ms"],
                        "target_ms": progress["target_ms"],
                        "elapsed_ms": progress["elapsed_ms"],
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
            return None
        payload = {
            "type": "speaker_enroll_result",
            "session_id": self.session_id,
            "accepted": result.accepted,
            "reason": result.reason,
            "score": round(result.score, 4),
            "speech_ms": result.speech_ms,
            "state": self.speaker_verifier.state.value,
            "at": datetime.now(UTC).isoformat(),
        }
        self._publish(payload)
        self._enroll_collecting = False
        self.mark_audio_event(
            "speaker_enrolled" if result.reason == "enrolled" else "speaker_enroll_open",
            detail={"reason": result.reason, "speech_ms": result.speech_ms},
        )
        logger.info(
            "speaker_enroll result=%s state=%s speech_ms=%s session_id=%s",
            result.reason,
            self.speaker_verifier.state.value,
            result.speech_ms,
            self.session_id,
        )
        return payload

    def _interrupt_candidate_text(self) -> str:
        return (getattr(self.input_guard, "candidate_text", None) or "").strip()

    def _route_candidate(self, text: str | None = None) -> UtteranceRoute:
        """Classify utterance via the shared control-plane router."""
        route = route_utterance(
            text if text is not None else self._interrupt_candidate_text(),
            speaker_state=self.speaker_verifier.state,
            resumable_reply=(
                self._paused_reply_available
                and self._paused_reply_binding is not None
                and self._paused_reply_binding == self._current_resume_speaker_binding()
            ),
        )
        if route.should_interrupt or route.intent is UtteranceIntent.RESUME:
            return route
        if self._sticky_interrupt_epoch == self._speaker_epoch:
            return self._sticky_interrupt_route or route
        return route

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
                    }
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
            }
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

    def _publish(self, event: dict[str, Any]) -> asyncio.Task[Any] | None:
        if event.get("type") not in UI_EVENT_TYPES:
            raise ValueError("unsupported voice-agent.ui event type")
        if self._event_publisher is not None:
            return self._spawn(
                self._event_publisher(event),
                name=f"duplex-ui-{event['type']}",
            )
        return None

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
        return self._publish(
            {
                "type": "assistant_state",
                "session_id": self.session_id,
                "state": state,
                "phase": self.interaction_phase.value,
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "at": datetime.now(UTC).isoformat(),
            }
        )

    @staticmethod
    def _phase_for_published_state(state: str) -> InteractionPhase | None:
        mapping = {
            "connecting": InteractionPhase.CONNECTING,
            "ready": InteractionPhase.LISTENING,
            "speaker_enroll": InteractionPhase.LISTENING,
            "listening": InteractionPhase.LISTENING,
            "user_speaking": InteractionPhase.USER_SPEAKING,
            "backchannel": InteractionPhase.BACKCHANNEL,
            "thinking": InteractionPhase.THINKING_SILENT,
            "thinking_silent": InteractionPhase.THINKING_SILENT,
            "speaking": InteractionPhase.SPEAKING,
            "interrupted": InteractionPhase.INTERRUPTED,
            "tool_waiting": InteractionPhase.TOOL_WAITING,
            "recovering": InteractionPhase.RECOVERING,
            "closed": InteractionPhase.CLOSED,
        }
        return mapping.get(state)

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
                "at": datetime.now(UTC).isoformat(),
            }
        )

    def mark_audio_event(
        self,
        name: str,
        *,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
        mono_ns: int | None = None,
    ) -> None:
        """Record and publish one server-side first-audio stage without blocking media."""
        if status == "ok":
            self.latency_trace.mark(name, mono_ns=mono_ns)
        fence = self.fence
        event: dict[str, Any] = {
            "type": "audio_trace",
            "source": "agent",
            "session_id": self.session_id,
            "name": name,
            "status": status,
            "turn_id": fence.turn_id,
            "generation_id": fence.generation_id,
            "at": datetime.now(UTC).isoformat(),
        }
        if detail:
            event["detail"] = detail
        logger.info(
            "agent_audio_trace name=%s status=%s turn_id=%s generation_id=%s",
            name,
            status,
            fence.turn_id,
            fence.generation_id,
        )
        self._publish(event)

    def observe_client_audio_trace(
        self,
        event: dict[str, Any],
        *,
        mono_ns: int | None = None,
    ) -> bool:
        """Accept bounded client playback facts; permanent keys/audio never enter telemetry."""
        if (
            event.get("session_id") != self.session_id
            or event.get("name") not in CLIENT_AUDIO_TRACE_NAMES
            or event.get("status") not in {"ok", "error"}
        ):
            return False
        metrics: dict[str, int | float] = {}
        if event["name"] == "webrtc_inbound_audio":
            detail = event.get("detail")
            if not isinstance(detail, dict) or not detail:
                return False
            if not set(detail).issubset(CLIENT_AUDIO_METRIC_NAMES):
                return False
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                for value in detail.values()
            ):
                return False
            metrics = {str(key): value for key, value in detail.items()}
        if event["name"] == "first_playback" and event["status"] == "ok":
            self.latency_trace.mark("client_first_playback", mono_ns=mono_ns)
        logger.info(
            "client_audio_trace name=%s status=%s turn_id=%s generation_id=%s metrics=%s",
            event["name"],
            event["status"],
            event.get("turn_id"),
            event.get("generation_id"),
            metrics,
        )
        return True

    def publish_transcript(
        self,
        *,
        speaker: str,
        text: str,
        final: bool,
        heard: bool | None = None,
        fence: GenerationFence | None = None,
        archive_fence: GenerationFence | None = None,
    ) -> None:
        fence = fence or self.fence
        archive_fence = archive_fence or fence
        event: dict[str, Any] = {
            "type": "transcript_delta",
            "speaker": speaker,
            "text": text,
            "final": final,
            "turn_id": fence.turn_id,
            "generation_id": fence.generation_id,
            "history_eligible": bool(final and self._history_eligible(fence)),
        }
        if heard is not None:
            event["heard"] = heard
        self._publish(event)
        if not final or not text.strip():
            return
        archive_text = redact_pii(text.strip())
        if speaker == "user":
            event_type = "speech.utterance_finalized"
            speaker_class = self._speaker_class
            payload: dict[str, Any] = {
                "text": archive_text,
                "persona_eligible": self._persona_evidence_eligible,
                "prompt_kind": self._next_user_prompt_kind,
            }
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
            return
        payload.update(
            self._mode_policy_provenance(
                archive_fence,
                speaker_class,
                history_eligible=self._history_eligible(archive_fence),
                owner_projection_eligible=self._owner_projection_eligible(archive_fence),
            )
        )
        fingerprint = hashlib.sha256(
            (
                f"{self.session_id}\0{event_type}\0{speaker}\0"
                f"{archive_fence.turn_id}\0{archive_fence.generation_id}\0"
                f"{archive_fence.tool_epoch}\0{archive_text}"
            ).encode()
        ).hexdigest()
        evidence = {
            "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:evidence:{fingerprint}")),
            "session_id": self.session_id,
            "event_type": event_type,
            "occurred_at": datetime.now(UTC).isoformat(),
            "speaker_class": speaker_class,
            "source": (
                "generation_fence.actual_heard"
                if speaker == "assistant"
                else "funasr.authoritative_final"
            ),
            "turn_id": archive_fence.turn_id,
            "generation_id": archive_fence.generation_id,
            "tool_epoch": archive_fence.tool_epoch,
            "payload": payload,
        }
        if (
            speaker_class == "owner"
            and payload["owner_projection_eligible"] is True
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

    def _speaker_persona_provenance(self) -> dict[str, Any]:
        """Bind Persona eligibility to the decision for this exact speech epoch."""

        decision = self._speaker_decision
        if decision is None:
            return {}
        return {
            "speaker_reason_code": decision.reason_code,
            "speaker_profile_id": decision.profile_id,
            "speaker_quality_score": decision.quality_score,
            "speaker_model_version": decision.model_version,
            "speaker_template_version": decision.template_version,
        }

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
        supported_speaker = cast(
            Literal["owner", "guest", "uncertain"],
            speaker_class if speaker_class in {"owner", "guest", "uncertain"} else "uncertain",
        )
        return {
            "interaction_mode": policy.mode or "unavailable",
            "mode_policy_version": policy.policy_version or "unavailable",
            "simulated_output": policy.mode in {"self_preview", "legacy"},
            "history_eligible": (
                history_eligible
                if history_eligible is not None
                else policy.history_eligible(
                    supported_speaker,
                    reason_code=reason_code,
                )
            ),
            "owner_projection_eligible": (
                owner_projection_eligible
                if owner_projection_eligible is not None
                else policy.owner_projection_eligible(supported_speaker)
            ),
        }

    def _owner_acoustic_evidence(self) -> dict[str, int | float]:
        decision = self._speaker_decision
        if (
            self._speaker_class != "owner"
            or decision is None
            or not self._mode_policy.owner_projection_eligible("owner")
            or decision.classification != "owner"
            or not decision.profile_id
            or decision.template_version is None
            or decision.template_version < 1
            or not self._speaker_pcm
        ):
            return {}
        stats = voiced_stats_from_pcm(
            bytes(self._speaker_pcm),
            sample_rate=self._speaker_sample_rate,
        )
        speech_ms = int(stats["speech_ms"])
        duty = float(stats["duty"])
        quality_score = float(decision.quality_score)
        if (
            speech_ms <= 0
            or speech_ms > 600_000
            or not math.isfinite(duty)
            or not 0 <= duty <= 1
            or not math.isfinite(quality_score)
            or not 0 <= quality_score <= 1
        ):
            return {}
        return {
            "speech_ms": speech_ms,
            "pause_ratio": min(1.0, max(0.0, 1.0 - duty)),
            "quality_score": quality_score,
        }

    def observe_acoustic_emotion(
        self,
        provider_label: str,
        *,
        text: str = "",
        turn_id: int | None = None,
    ) -> EmotionObservation:
        current_turn_id = self.fence.turn_id
        if turn_id is None or turn_id < current_turn_id or turn_id > current_turn_id + 1:
            return neutral_observation(time.monotonic_ns(), self.emotion_smoother.ttl_ms)
        observation = self.emotion_smoother.observe_acoustic(
            provider_label,
            text=text,
            turn_id=turn_id,
        )
        logger.info(
            "emotion_observation label=%s provider_label=%s evidence=%s turn_id=%s",
            observation.label,
            observation.provider_label,
            ",".join(observation.evidence),
            turn_id,
        )
        self._emotion_by_turn[turn_id] = observation
        self._emotion_by_turn = {
            key: value for key, value in self._emotion_by_turn.items() if key >= current_turn_id
        }
        fence = self.fence
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
            }
        )
        return observation

    def _apply_speech_plan(self, user_text: str, *, turn_id: int) -> SpeechPlan:
        acoustic = self._emotion_by_turn.pop(turn_id, None)
        self._emotion_by_turn = {
            key: value for key, value in self._emotion_by_turn.items() if key > turn_id
        }
        observation = self.emotion_smoother.observe_text(user_text, acoustic=acoustic)
        self.speech_plan = speech_plan_for_turn(
            label=observation.label,
            provider_label=observation.provider_label,
            text=user_text,
            evidence=observation.evidence,
            use_markup_tags=self.use_paralinguistic_tags,
        )
        apply_plan = getattr(self.tts, "apply_speech_plan", None)
        if callable(apply_plan):
            apply_plan(
                emotion=self.speech_plan.voice_emotion,
                rate=self.speech_plan.rate,
            )
        logger.info(
            "speech_plan_selected emotion=%s rate=%.2f delivery=%s "
            "tts_prefix=%s strip=%s turn_id=%s",
            self.speech_plan.voice_emotion,
            self.speech_plan.rate,
            self.speech_plan.delivery_mode,
            self.speech_plan.tts_prefix or "-",
            self.speech_plan.strip_paralinguistic,
            turn_id,
        )
        return self.speech_plan

    def _publish_listener_cue(self, cue: ListenerCue, state: str) -> None:
        self._publish(
            {
                "type": "listener_cue",
                "session_id": self.session_id,
                "cue_id": cue.cue_id,
                "cue_epoch": cue.cue_epoch,
                "user_turn_id": cue.user_turn_id,
                "state": state,
                "phase": self.interaction_phase.value,
                "at": datetime.now(UTC).isoformat(),
            }
        )

    async def _play_listener_cue(self, cue: ListenerCue) -> None:
        if self._listener_cue_player is None or not self.cue_scheduler.is_current(cue):
            return
        handle = self._listener_cue_player(cue.text)
        if inspect.isawaitable(handle):
            handle = await handle
        if not self.cue_scheduler.is_current(cue):
            stop = getattr(handle, "stop", None)
            if callable(stop):
                stop()
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
            if self.cue_scheduler.is_current(cue):
                self._publish_listener_cue(cue, "finished")
        finally:
            if self._active_listener_cue is cue:
                self._active_listener_cue = None
                self._active_listener_cue_handle = None
            if self.interaction_phase is InteractionPhase.BACKCHANNEL:
                self.set_interaction_phase(
                    InteractionPhase.USER_SPEAKING,
                    cause="listener_cue_finished",
                )

    def cancel_listener_cue(self) -> None:
        self._cancel_listener_cue_candidate()
        cue = self._active_listener_cue
        self.cue_scheduler.cancel_turn()
        handle = self._active_listener_cue_handle
        stop = getattr(handle, "stop", None)
        if callable(stop):
            stop()
        self._active_listener_cue = None
        self._active_listener_cue_handle = None
        if cue is not None:
            self._publish_listener_cue(cue, "cancelled")
        if self.interaction_phase is InteractionPhase.BACKCHANNEL:
            self.set_interaction_phase(
                InteractionPhase.USER_SPEAKING,
                cause="listener_cue_cancelled",
                publish=False,
            )

    def _cancel_listener_cue_candidate(self) -> None:
        task = self._listener_cue_candidate_task
        if task is not None and not task.done():
            task.cancel()
        self._listener_cue_candidate_task = None

    def _schedule_listener_cue(self, text: str, *, now_ns: int | None) -> None:
        self._cancel_listener_cue_candidate()
        observed_at_ns = now_ns if now_ns is not None else time.monotonic_ns()

        async def _after_micro_pause() -> None:
            try:
                await asyncio.sleep(self.cue_scheduler.pause_ms / 1000)
                cue = self.cue_scheduler.observe_partial(
                    text,
                    now_ns=observed_at_ns + self.cue_scheduler.pause_ms * 1_000_000,
                    aec_healthy=self._listener_cue_aec_healthy,
                    main_response_active=self.orchestrator.state
                    in {
                        ConversationState.THINKING,
                        ConversationState.SPEAKING,
                        ConversationState.TOOL_WAITING,
                    },
                )
                if cue is not None:
                    if self._listener_cue_candidate_task is asyncio.current_task():
                        self._listener_cue_candidate_task = None
                    await self._play_listener_cue(cue)
            finally:
                if self._listener_cue_candidate_task is asyncio.current_task():
                    self._listener_cue_candidate_task = None

        self._listener_cue_candidate_task = self._spawn(
            _after_micro_pause(),
            name="listener-cue-micro-pause",
        )

    def update_pending_assistant_text(self, text: str) -> None:
        self._pending_assistant_text = text
        self.orchestrator.heard_tracker.set_full_text(text)

    def on_user_voice_started(self, *, now_ns: int | None = None) -> PlaybackInputDecision:
        self.refresh_voice_profile()
        self._seal_canonical_speech_epoch()
        self._speaker_epoch += 1
        self._canonical_speech_epoch = self._speaker_epoch
        self._target_focus_epoch = None
        self._target_focus_pending_epoch = None
        self._sticky_interrupt_epoch = None
        self._sticky_interrupt_route = None
        self._speaker_class = "uncertain"
        self._speaker_decision = self._uncertain_speaker_decision("classification_pending")
        self._speaker_pcm.clear()
        self._speaker_collecting = True
        previous_classification = self._speaker_classification_task
        if previous_classification is not None and not previous_classification.done():
            previous_classification.cancel()
        self._speaker_classification_task = None
        self._fresh_user_speech = True
        self.speaker_verifier.mark_utterance_start()
        if not self._was_speaking:
            self.set_interaction_phase(
                InteractionPhase.USER_SPEAKING,
                cause="vad_start",
            )
        self.input_guard.start(during_playback=self._was_speaking, now_ns=now_ns)
        pending_turn_id = self.fence.turn_id + 1
        if self._emotion_turn_observer is not None:
            self._emotion_turn_observer(pending_turn_id)
        if not self._was_speaking:
            self.cue_scheduler.start_turn(
                user_turn_id=pending_turn_id,
                now_ns=now_ns,
            )
        if self.input_guard.candidate_during_playback:
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
        decision = self.input_guard.observe(
            text,
            final=final,
            assistant_text=self._pending_assistant_text or self._played_assistant_text,
            now_ns=now_ns,
            during_playback_if_unstarted=self._was_speaking,
        )
        if (
            self.input_guard.candidate_during_playback
            and not self.input_guard.candidate_vad_anchored
            and text.strip()
        ):
            self._user_transcript_contaminated = True
            prefix = text.strip()
            if not self._suspected_playback_prefixes or (
                self._suspected_playback_prefixes[-1] != prefix
            ):
                self._suspected_playback_prefixes.append(prefix)
                del self._suspected_playback_prefixes[:-8]
        if final:
            if self.input_guard.candidate_vad_anchored:
                self._canonical_final_observed = True
            if decision is PlaybackInputDecision.ACCEPT and self.input_guard.candidate_vad_anchored:
                self._accepted_user_finals.append(text.strip())
            elif decision is PlaybackInputDecision.IGNORE:
                self._user_transcript_contaminated = True
                self.orchestrator.metrics.inc_guarded_user_input(
                    self.input_guard.candidate_reason or "playback_noise"
                )
        raw_route = route_utterance(text, speaker_state=self.speaker_verifier.state)
        if decision is PlaybackInputDecision.ACCEPT and raw_route.should_interrupt:
            # FunASR may revise a clear interim「等一下」into a nearby final
            # homophone while TTS is also reaching the microphone. Keep the
            # accepted control intent monotonic for this VAD epoch.
            self._sticky_interrupt_epoch = self._speaker_epoch
            self._sticky_interrupt_route = raw_route
        if final:
            self._cancel_listener_cue_candidate()
        elif (
            not final
            and decision is PlaybackInputDecision.ACCEPT
            and self._listener_cue_player is not None
        ):
            self._schedule_listener_cue(text, now_ns=now_ns)
        return decision

    def _canonical_snapshot(self) -> CanonicalUserTurnSnapshot:
        return CanonicalUserTurnSnapshot(
            speech_epoch=self._canonical_speech_epoch,
            accepted_finals=tuple(part for part in self._accepted_user_finals if part),
            contaminated=self._user_transcript_contaminated,
            suspected_playback_prefixes=tuple(self._suspected_playback_prefixes),
        )

    def _reset_canonical_speech_epoch(self) -> None:
        self._accepted_user_finals.clear()
        self._user_transcript_contaminated = False
        self._suspected_playback_prefixes.clear()
        self._canonical_speech_epoch = None
        self._canonical_final_observed = False

    def _seal_canonical_speech_epoch(self) -> None:
        """Queue a completed VAD epoch before later ASR events can mutate it."""

        if self._canonical_speech_epoch is None or not self._canonical_final_observed:
            return
        self._canonical_turn_snapshots.append(self._canonical_snapshot())
        self._reset_canonical_speech_epoch()

    def consume_canonical_user_turn(self, raw_text: str) -> str | None:
        """Consume the oldest VAD-epoch snapshot for LiveKit's ordered callback."""

        if self._canonical_turn_snapshots:
            snapshot = self._canonical_turn_snapshots.popleft()
        elif (
            self._canonical_final_observed
            or self._accepted_user_finals
            or self._user_transcript_contaminated
        ):
            snapshot = self._canonical_snapshot()
            self._reset_canonical_speech_epoch()
        else:
            self._consumed_canonical_speech_epoch = (
                self._speaker_epoch if self._fresh_user_speech else None
            )
            return raw_text.strip()

        self._consumed_canonical_speech_epoch = snapshot.speech_epoch
        accepted = snapshot.accepted_finals
        contaminated = snapshot.contaminated
        suspected_prefixes = snapshot.suspected_playback_prefixes
        if not contaminated:
            return " ".join(accepted).strip() or raw_text.strip()
        canonical_parts: list[str] = []
        prefixes = sorted(
            suspected_prefixes,
            key=lambda value: sum(char.isalnum() for char in value),
            reverse=True,
        )
        for part in accepted:
            clean = part
            for prefix in prefixes:
                matched, remainder = _strip_matching_transcript_prefix(clean, prefix)
                if matched:
                    clean = remainder
                    break
            if clean.strip():
                canonical_parts.append(clean.strip())
        canonical = " ".join(canonical_parts).strip()
        logger.info(
            "canonical_user_turn_rebuilt accepted_segments=%s raw_len=%s canonical_len=%s "
            "session_id=%s",
            len(accepted),
            len(raw_text.strip()),
            len(canonical),
            self.session_id,
        )
        return canonical or None

    @property
    def consumed_canonical_speech_epoch(self) -> int | None:
        return self._consumed_canonical_speech_epoch

    def discard_pending_user_transcript(self) -> None:
        """Discard only the current endpoint buffer, never older queued callbacks."""

        self._reset_canonical_speech_epoch()

    def _clear_unanchored_playback_transcript(self) -> None:
        """Reset LiveKit STT after echo-only playback input, before the next VAD."""

        if (
            not self._suspected_playback_prefixes
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
        speech_anchored: bool | None = None,
        canonical_speech_epoch: int | None = None,
    ) -> tuple[bool, str | None]:
        self.speaker_verifier.mark_utterance_end()
        self._persona_evidence_eligible = False
        if self.mode_policy_enforced and not self._mode_policy.allows_conversation():
            self.orchestrator.metrics.inc_guarded_user_input("interaction_mode_blocked")
            return False, "interaction_mode_blocked"
        # Single control-plane decision: enroll / pure interrupt / chat.
        # Side effects (early enroll finalize, yield ack) stay here; intent is
        # owned by utterance_router so barge-in and turn-commit cannot diverge.
        route = self._route_candidate(text)
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
        if route.intent is UtteranceIntent.INTERRUPT_COMMAND:
            # 「等等」「停一下」「别说了」are control phrases, not chat questions.
            # If we let them through, the LLM answers「怎么了？」and covers the yield ack.
            self.input_guard.candidate_text = text
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
                self._maybe_say_interrupt_yield(cause="interrupt_command_turn"),
                name="interrupt-cmd-yield",
            )
            return False, route.reason
        if self.input_guard.enabled and speech_anchored is not None:
            snapshot_has_vad = canonical_speech_epoch is not None
            missing_anchor = not speech_anchored or not (
                snapshot_has_vad or self._fresh_user_speech
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
        if self.speaker_verifier.active and not self._speaker_allows_user_input(
            context="turn_commit"
        ):
            return False, "speaker_mismatch"
        post_playback_reason = self.post_playback_guard_reason(text)
        if post_playback_reason is not None:
            self.orchestrator.metrics.inc_guarded_user_input(post_playback_reason)
            return False, post_playback_reason
        accepted, reason = self.input_guard.accept_turn(text)
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
            )
            self._resume_pending = route.intent is UtteranceIntent.RESUME
            if route.enter_chat:
                self._paused_reply_available = False
                self._paused_reply_binding = None
        return accepted, reason

    async def on_turn_committed(self, user_text: str) -> GenerationFence:
        history_eligible = self._current_history_eligible()
        owner_projection_eligible = self._current_owner_projection_eligible()
        self.cancel_listener_cue()
        self._apply_speech_plan(user_text, turn_id=self.fence.turn_id + 1)
        self._last_playback_completed_ns = None
        if self.orchestrator.state is ConversationState.CONNECTING:
            await self.orchestrator.ready()
        if self.orchestrator.state is ConversationState.TOOL_WAITING:
            await self.orchestrator.bump_tool_epoch_on_condition_change()
        await self.orchestrator.on_vad_start()
        fence = await self.orchestrator.commit_turn(user_text)
        self._bind_mode_policy(fence)
        self._bind_history_eligibility(fence, history_eligible)
        self._bind_owner_projection_eligibility(
            fence,
            owner_projection_eligible,
        )
        self._reply_speaker_binding = self._current_resume_speaker_binding()
        self._resume_fence = fence if self._resume_pending else None
        self._resume_pending = False
        self.set_interaction_phase(
            InteractionPhase.THINKING_SILENT,
            cause="turn_committed",
        )
        self.mark_audio_event("turn_committed")
        self._pending_assistant_text = ""
        self._played_assistant_text = ""
        self._playback_fence = None
        if self.tts is not None:
            self.tts.bind_fence(fence)
        return fence

    async def on_assistant_speaking(
        self,
        full_text: str,
        words: list[TimedWord] | tuple[TimedWord, ...] | None = None,
    ) -> None:
        self._pending_assistant_text = full_text
        self._was_speaking = True
        w = list(words) if words else []
        # Only transition if we are in THINKING (or already SPEAKING is ok to re-enter carefully)
        if self.orchestrator.state is ConversationState.THINKING:
            await self.orchestrator.begin_speaking(w, full_text)
        elif self.orchestrator.state is ConversationState.SPEAKING:
            self.orchestrator.heard_tracker.set_full_text(full_text)
            if w:
                self.orchestrator.heard_tracker.add_words(w)
        else:
            # Still record tracker data for interrupt truncation.
            self.orchestrator.heard_tracker.set_full_text(full_text)
            if w:
                self.orchestrator.heard_tracker.add_words(w)

    async def on_playback_done(self, *, tools_active: bool = False) -> None:
        self._was_speaking = False
        if self.orchestrator.state is ConversationState.SPEAKING:
            await self.orchestrator.finish_speaking(tools_active=tools_active)
        elif self.orchestrator.state is ConversationState.THINKING and self._pending_assistant_text:
            # No audio path — still commit heard text as full if never interrupted.
            await self.orchestrator.begin_speaking([], self._pending_assistant_text)
            await self.orchestrator.finish_speaking(tools_active=tools_active)

    def set_interrupt_yield(self, speaker: Callable[[str], Awaitable[None]] | None) -> None:
        """speaker(phrase) — phrase is chosen from interrupt semantics."""
        self._interrupt_yield = speaker

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
        self._was_speaking = False
        self._playback_fence = None
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

    async def _maybe_say_interrupt_yield(self, *, cause: str) -> None:
        """Short ack after mid-reply stop so users know we yielded, not crashed."""
        if self._interrupt_yield is None:
            return
        if cause in {"user_button", "stop_response", "rtc_recovered"}:
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
        candidate = self._interrupt_candidate_text()
        route = self._route_candidate(candidate)
        phrase = route.ack_phrase or interrupt_ack_phrase(candidate)
        self.mark_audio_event(
            "interrupt_yield_started",
            detail={
                "cause": cause,
                "ack_len": len(phrase),
                "candidate_len": len(candidate),
                "intent": route.intent,
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

    async def on_real_interrupt(
        self,
        cause: str = "livekit_interruption",
        *,
        stop_playback: Callable[[], Awaitable[str | None]] | None = None,
        create_user_turn: bool = True,
        synchronized_transcript: str | None = None,
        force_generation_bump: bool = False,
    ) -> GenerationFence:
        self.cancel_listener_cue()
        was_speaking = self._was_speaking
        mid_reply = self._assistant_was_mid_reply(was_speaking=was_speaking)
        candidate = self._interrupt_candidate_text()
        # Barge-in uses the same router as turn-commit so speaker-reject recover
        # cannot fire on pure「停一下」while accept_user_turn treats it as control.
        barge_route = self._route_candidate(candidate)
        owner_cmd = barge_route.speaker_gate_override
        if (
            create_user_turn
            and self._target_speaker_focus_enabled
            and self._speaker_classifier is not None
        ):
            # LiveKit may request an interrupt on VAD start. Do not classify the
            # first few PCM frames: the final transcript handler will decide
            # against the complete endpointed utterance instead.
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
                explicit_interrupt=barge_route.should_interrupt,
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
        self._bind_mode_policy(new_fence)
        if create_user_turn and barge_route.intent is UtteranceIntent.INTERRUPT_COMMAND:
            self._clear_control_user_turn(cause=f"interrupt:{cause}")
        self._was_speaking = False
        self._last_playback_completed_ns = None
        self._pending_assistant_text = ""
        # Unlock commit gate immediately; yield will keep listening after playout.
        if self._set_interruption_min_words is not None:
            self._set_interruption_min_words(self._base_interruption_min_words)
        if self.tts is not None:
            self.tts.bind_fence(new_fence)
        if not new_fence.matches(old_fence):
            if (
                create_user_turn
                and mid_reply
                and barge_route.intent is UtteranceIntent.INTERRUPT_COMMAND
            ):
                self._paused_reply_binding = self._reply_speaker_binding
                self._paused_reply_available = self._paused_reply_binding is not None
            elif create_user_turn:
                self._paused_reply_available = False
                self._paused_reply_binding = None
            self.publish_assistant_state("interrupted")
            self.set_interaction_phase(
                InteractionPhase.INTERRUPTED,
                cause=f"interrupt:{cause}",
            )
            # User-facing yield when we actually stop mid-reply for the owner.
            if (
                create_user_turn
                and mid_reply
                and barge_route.intent is not UtteranceIntent.INTERRUPT_THEN_CHAT
                and cause
                not in {
                    "user_button",
                    "stop_response",
                    "rtc_recovered",
                }
            ):
                self._spawn(
                    self._maybe_say_interrupt_yield(cause=cause),
                    name="interrupt-yield",
                )
            else:
                self._restore_listen_after_control(cause=f"interrupt_no_yield:{cause}")
        return new_fence

    async def on_playback_started(self) -> None:
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
            finalized = await self.orchestrator.finalize_interrupted_playback(
                interrupted_from=interrupted_from,
                synchronized_transcript=combined_heard,
            )
            if finalized is None:
                return
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
                )

    async def on_assistant_reply_completed(self, text: str) -> None:
        """Commit one fully played reply after LiveKit adds its conversation item."""
        if self.orchestrator.state is not ConversationState.SPEAKING:
            return
        # LiveKit emits this item only after uninterrupted playout completes.
        heard = text
        reply_fence = self._playback_fence or self.fence
        heard = await self.orchestrator.finish_livekit_playback(
            tools_active=self._pending_tool_results > 0,
            synchronized_transcript=heard,
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
                heard=True,
                fence=reply_fence,
            )

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
        if reason == "backchannel" or self.input_guard.enabled:
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
                if decision is PlaybackInputDecision.WAIT:
                    _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                    self.mark_audio_event("barge_in_detected")
                else:
                    _set_min_words(base_min_words)
                return
            if state == "listening":
                self.on_user_voice_stopped()
                if (
                    self.input_guard.candidate_active
                    and self.input_guard.candidate_during_playback
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
                if self.input_guard.candidate_during_playback:
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
                        _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                        return
                    if self._target_speaker_focus_enabled and self._speaker_classifier is not None:
                        _set_min_words(PLAYBACK_INPUT_BLOCK_MIN_WORDS)
                        if final:
                            if self._target_focus_pending_epoch == self._speaker_epoch:
                                self._target_focus_pending_epoch = None
                            self._spawn(
                                self._confirm_target_speaker_interrupt(self._speaker_epoch),
                                name="target-speaker-playback-focus",
                            )
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
