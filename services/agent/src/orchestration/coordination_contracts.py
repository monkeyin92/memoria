"""Coordination harness contracts for the six-module orchestration architecture.

This module defines the types, protocols, and interfaces for Memoria's
coordination layer, aligned to DuplexAgent-RSI's Policy / Intake / Hold /
Worker / Deliver / Memory framework. All implementations stay in their
respective modules; this file only declares contracts.

References:
    docs/strategy/coordination-harness.md
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.voice_core.speech_timeline import SpeechSegment


# ============================================================================
# Policy Module (utterance routing + interruption decision)
# ============================================================================


class PolicyIntent(StrEnum):
    """Control-plane intent for one user utterance (aliased from UtteranceIntent)."""

    ENROLL = "enroll"
    END_SESSION = "end_session"
    INTERRUPT_COMMAND = "interrupt_command"
    INTERRUPT_THEN_CHAT = "interrupt_then_chat"
    INTERRUPT_REPLAY = "interrupt_replay"
    RESUME = "resume"
    CHAT = "chat"
    REQUEST_HINT = "request_hint"
    REQUEST_REPEAT = "request_repeat"
    PACE_CONTROL = "pace_control"
    GIVE_UP = "give_up"
    EMPTY = "empty"


class PolicyFailure(StrEnum):
    """Policy-level routing failure classification."""

    INVALID_INTENT = "invalid_intent"
    ROUTE_AMBIGUOUS = "route_ambiguous"
    INTERRUPT_WEAK_EVIDENCE = "interrupt_weak_evidence"
    BACKCHANNEL_ONLY = "backchannel_only"
    NON_TARGET_LANGUAGE = "non_target_language"


@dataclass(frozen=True)
class PolicyRoute:
    """Policy module's routing decision for one utterance.

    This is the domain contract returned by the Policy module. It tells
    downstream modules what control intent applies and whether the utterance
    should interrupt playback or enter chat.
    """

    intent: PolicyIntent
    reason: str
    """Machine-readable reason (also used as reject reason when suppressed)."""

    enter_chat: bool
    """Whether this text should become an LLM user turn."""

    should_interrupt: bool
    """Whether mid-reply barge-in should confirm interruption (not recover)."""

    speaker_gate_override: bool
    """When True, short/too_short speaker scores must not block owner stop phrases."""

    ack_phrase: str | None
    """Fixed TTS ack when suppressing or yielding (semantic 嗯你说 / 好的)."""

    normalized_text: str
    """Normalized text after stripping punctuation and whitespace."""

    failure: PolicyFailure | None = None
    """Set when routing failed; intent becomes a fallback default."""


class PolicyModule(Protocol):
    """Policy module interface: route utterance → control intent + interrupt decision."""

    def route_utterance(
        self,
        text: str,
        *,
        speaker_state: str | None = None,
        resumable_reply: bool = False,
        sticky_interrupt_route: PolicyRoute | None = None,
        previous_committed_text_normalized: str = "",
        semantic_verdict: str | None = None,
        session_focus: str | None = None,
        device_conversation: bool = False,
        conversation_close: bool | None = None,
    ) -> PolicyRoute:
        """Classify one utterance into policy route.

        Returns the complete routing decision with intent, interruption flags,
        and optional ack phrase. All downstream modules consume PolicyRoute.
        """
        raise NotImplementedError


# ============================================================================
# Intake Module (VAD/ASR/KWS packet admission gate)
# ============================================================================


class IntakeRejectReason(StrEnum):
    """Intake-level packet rejection classification."""

    STALE_STREAM_EPOCH = "stale_stream_epoch"
    SPURIOUS_VAD = "spurious_vad"
    FLOOR_CONFLICT = "floor_conflict"
    LISTENING_HOLD = "listening_hold"
    PRECEDES_PENDING_TURN = "precedes_pending_turn"
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_CLOSED = "session_closed"


@dataclass(frozen=True)
class SpeechPacket:
    """Unified VAD/ASR/KWS packet protocol for intake admission.

    This normalizes the three segment kinds into one intake contract so the
    admission gate does not branch on segment.kind repeatedly.
    """

    kind: str  # "VAD" | "ASR_PARTIAL" | "ASR_FINAL" | "KWS"
    session_id: str
    stream_epoch: int
    capture_start_sample: int
    capture_end_sample: int
    text: str
    final: bool
    confidence: float | None = None
    voiced_end_sample: int | None = None
    near_end_rms: float | None = None


@dataclass(frozen=True)
class AdmittedSpeech:
    """Result of intake admission: accepted packet + context annotations."""

    packet: SpeechPacket
    turn_opened: bool
    """Whether this packet opened a new turn (first vad.start)."""

    reopened_turn: bool
    """Whether this packet reopened a turn that already had an endpoint."""

    pending_turn_floor: int | None
    """Sample boundary below which no packet may be admitted (cross-boundary guard)."""


class IntakeRejectDecision:
    """Intake rejection with reason."""

    def __init__(self, reason: IntakeRejectReason, detail: str = ""):
        self.reason = reason
        self.detail = detail


class IntakeModule(Protocol):
    """Intake module interface: admit VAD/ASR/KWS packets into the coordination layer."""

    def admit_speech_packet(
        self,
        packet: SpeechPacket,
        *,
        session_id: str,
        stream_epoch: int,
    ) -> AdmittedSpeech | IntakeRejectDecision:
        """Admit or reject one VAD/ASR/KWS packet.

        Returns AdmittedSpeech if the packet passes all admission gates,
        or IntakeRejectDecision with reason if rejected.
        """
        raise NotImplementedError


# ============================================================================
# Hold Module (wait-for-evidence decision + timeout management)
# ============================================================================


class HoldDecision(StrEnum):
    """Hold module's decision for one admitted speech segment."""

    COMMIT_NOW = "commit_now"
    WAIT = "wait"
    DISCARD = "discard"


class HoldTimeout(StrEnum):
    """Hold timeout classification."""

    GRACE_EXPIRED = "grace_expired"
    TAIL_TIMEOUT = "tail_timeout"
    EVIDENCE_WINDOW_EXPIRED = "evidence_window_expired"
    MAX_USER_SPEECH = "max_user_speech"


@dataclass(frozen=True)
class HoldState:
    """Hold module's state for one pending turn."""

    turn_start_sample: int | None
    turn_end_sample: int | None
    turn_endpoint_sample: int | None
    turn_retire_sample: int | None
    has_text_evidence: bool
    endpoint_covered_by_asr: bool
    grace_deadline: float | None
    tail_deadline: float | None
    evidence_window_deadline: float | None


class HoldModule(Protocol):
    """Hold module interface: decide whether to commit or wait for more evidence."""

    def decide_hold(
        self,
        *,
        session_id: str,
        stream_epoch: int,
        admitted: AdmittedSpeech,
        state: HoldState,
    ) -> HoldDecision:
        """Decide whether to commit, wait, or discard the current turn.

        Returns HoldDecision enum. COMMIT_NOW means the turn has enough
        evidence; WAIT means more evidence is needed; DISCARD means the
        turn should be dropped (e.g., empty turn during playback).
        """
        raise NotImplementedError


# ============================================================================
# Worker Module (background tool coordination + OutputIntent)
# ============================================================================


class WorkerFailure(StrEnum):
    """Worker-level task failure classification."""

    FENCE_STALE = "fence_stale"
    TASK_TIMEOUT = "task_timeout"
    POLICY_DENIED = "policy_denied"
    EFFECT_UNCOMMITTED = "effect_uncommitted"
    TOOL_ERROR = "tool_error"
    CONTEXT_VERSION_MISMATCH = "context_version_mismatch"


class OutputIntentState(StrEnum):
    """OutputIntent lifecycle state."""

    CANDIDATE = "candidate"
    QUEUED = "queued"
    SELECTED = "selected"
    CONSUMED = "consumed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class WorkerDelegation:
    """Worker delegation request."""

    tool_name: str
    arguments: dict[str, Any]
    fence: GenerationFence
    task_epoch: int
    context_version: int
    expires_at_ms: int
    side_effect_policy: str
    committed: bool


@dataclass(frozen=True)
class WorkerResult:
    """Worker task result."""

    task_id: str
    fence: GenerationFence
    task_epoch: int
    context_version: int
    payload: Any | None
    failure: WorkerFailure | None = None


class WorkerModule(Protocol):
    """Worker module interface: coordinate background tools + OutputIntent admission."""

    async def delegate_task(
        self,
        delegation: WorkerDelegation,
    ) -> WorkerResult:
        """Delegate one tool invocation to the background worker pool.

        Returns WorkerResult with payload on success, or failure reason.
        """
        raise NotImplementedError

    def admit_output_intent(
        self,
        intent: Any,  # media_pb2.OutputIntent
        *,
        current_fence: GenerationFence,
        current_context_version: int,
        floor_allows_output: bool,
        now_ms: int | None = None,
    ) -> str | None:
        """Admit one OutputIntent candidate.

        Returns TTS source text if admitted and selected, None otherwise.
        The shadow state management tracks queued/superseded/consumed.
        """
        raise NotImplementedError


# ============================================================================
# Deliver Module (output send decision + floor gate)
# ============================================================================


class DeliverDecision(StrEnum):
    """Deliver module's decision for one OutputIntent."""

    SEND = "send"
    QUEUE = "queue"
    SUPERSEDE = "supersede"
    DROP = "drop"


class DeliverRejectReason(StrEnum):
    """Deliver-level rejection classification."""

    FLOOR_BLOCKED = "floor_blocked"
    FENCE_MISMATCH = "fence_mismatch"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    INVALID_KIND = "invalid_kind"
    MISSING_SOURCE = "missing_source"


@dataclass(frozen=True)
class FloorGate:
    """Floor requirement gate for output delivery."""

    assistant_may_speak: bool
    reason: str


class DeliverModule(Protocol):
    """Deliver module interface: decide whether to send OutputIntent."""

    def decide_delivery(
        self,
        intent: Any,  # media_pb2.OutputIntent
        *,
        current_fence: GenerationFence,
        current_context_version: int,
        floor_gate: FloorGate,
        now_ms: int | None = None,
    ) -> DeliverDecision:
        """Decide whether to send, queue, supersede, or drop one OutputIntent.

        Returns DeliverDecision enum. SEND means the intent passes all gates
        and should be dispatched; QUEUE means it should be retained as a
        candidate; SUPERSEDE means it lost to a higher-priority candidate;
        DROP means it should be discarded.
        """
        raise NotImplementedError


# ============================================================================
# Memory Module (projection commit + conversation history)
# ============================================================================


class MemoryFailure(StrEnum):
    """Memory-level commit failure classification."""

    TEXT_MISMATCH = "text_mismatch"
    RANGE_MISMATCH = "range_mismatch"
    SPEAKER_MISMATCH = "speaker_mismatch"
    PROJECTION_REJECTED = "projection_rejected"
    UNCOMMITTED = "uncommitted"


class ProjectionState(StrEnum):
    """Projection lifecycle state."""

    PROVISIONAL = "provisional"
    ALIGNED = "aligned"
    COMMITTED = "committed"


@dataclass(frozen=True)
class MemoryCommitEvidence:
    """Evidence for committing one user turn to conversation history."""

    session_id: str
    stream_epoch: int
    capture_start_sample: int
    capture_end_sample: int
    text: str
    fence: GenerationFence
    speaker_class: str
    speaker_authority_verified: bool
    history_eligible: bool


@dataclass(frozen=True)
class MemoryCommittedTurn:
    """Result of memory commit: committed turn + metadata."""

    fence: GenerationFence
    text: str
    capture_start_sample: int
    capture_end_sample: int
    revision: int
    speaker_class: str
    failure: MemoryFailure | None = None


class MemoryModule(Protocol):
    """Memory module interface: commit user turn to conversation history."""

    def commit_turn(
        self,
        evidence: MemoryCommitEvidence,
    ) -> MemoryCommittedTurn:
        """Commit one user turn to conversation history.

        Returns MemoryCommittedTurn with fence + revision on success,
        or failure reason if projection validation failed.
        """
        raise NotImplementedError


# ============================================================================
# Coordination Harness (end-to-end coordinator)
# ============================================================================


class CoordinationHarness(Protocol):
    """Top-level coordination harness that wires the six modules together.

    This is the entry point for end-to-end coordination. It orchestrates
    Policy → Intake → Hold → Worker → Deliver → Memory flow and tracks
    failures across module boundaries.
    """

    policy: PolicyModule
    intake: IntakeModule
    hold: HoldModule
    worker: WorkerModule
    deliver: DeliverModule
    memory: MemoryModule

    async def coordinate_user_input(
        self,
        session_id: str,
        segment: SpeechSegment,
    ) -> MemoryCommittedTurn | None:
        """End-to-end coordination for one user speech segment.

        This is the top-level orchestration method. It runs the segment
        through Policy → Intake → Hold, and if Hold decides to commit,
        it runs Worker → Deliver → Memory to produce the committed turn.

        Returns MemoryCommittedTurn on success, None if any module rejected.
        """
        raise NotImplementedError
