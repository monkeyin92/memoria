"""Project continuous speech facts into provisional and committed turns."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.interruption_guard import is_backchannel
from services.agent.src.orchestration.speech_timeline import (
    SegmentKind,
    SpeechSegment,
    SpeechTimeline,
)

SpeakerClass = Literal["owner", "guest", "uncertain"]

CAPTURE_SAMPLE_RATE = 16_000
PROJECTION_FRAME_SAMPLES = 1_280
_MAX_RESIDUAL_ECHO_SCORE = 0.65
_MIN_VAD_PROBABILITY = 0.45
_MAX_CONSECUTIVE_FRAMES = 32


class FloorState(StrEnum):
    USER_HOLDS_FLOOR = "user_holds_floor"
    ASSISTANT_HOLDS_FLOOR = "assistant_holds_floor"
    OVERLAP = "overlap"
    UNCERTAIN = "uncertain"
    SILENCE = "silence"


class ProjectionEventKind(StrEnum):
    PROVISIONAL_STARTED = "turn.provisional.started"
    PROVISIONAL_PATCH = "turn.provisional.patch"
    PROVISIONAL_DISCARDED = "turn.provisional.discarded"
    TURN_COMMITTED = "turn.committed"


class ProjectionRejectReason(StrEnum):
    NO_PROVISIONAL = "no_provisional_turn"
    SESSION_MISMATCH = "projection_session_mismatch"
    STREAM_EPOCH_MISMATCH = "projection_stream_epoch_mismatch"
    TURN_FENCE_MISMATCH = "projection_turn_fence_mismatch"
    RANGE_MISMATCH = "projection_range_mismatch"
    TEXT_MISMATCH = "projection_text_mismatch"
    EMPTY_TEXT = "projection_empty_text"
    NOT_PERSISTABLE = "projection_not_persistable"


class TurnPhase(StrEnum):
    IDLE = "idle"
    ACOUSTIC_ONLY = "acoustic_only"
    SEMANTIC_SPEAKING = "semantic_speaking"
    END_CANDIDATE = "end_candidate"
    BACKCHANNEL = "backchannel"
    UNCERTAIN = "uncertain"


class PhaseReason(StrEnum):
    NONE = "none"
    VAD_START = "vad_start"
    VAD_END_NO_SEMANTIC = "vad_end_no_semantic"
    ASR_SEMANTIC = "asr_semantic"
    ASR_CONTINUATION = "asr_continuation"
    ENDPOINT_COVERED = "endpoint_covered"
    ASR_UNCOVERED = "asr_uncovered"
    PLAYBACK_BACKCHANNEL = "playback_backchannel"
    BACKCHANNEL_IDLE = "backchannel_idle"
    BACKCHANNEL_PROMOTED = "backchannel_promoted"
    ECHO_OR_NOISE = "echo_or_noise"
    LOW_VAD = "low_vad"
    CLOCK_GAP = "clock_gap"
    LOSS_CONCEALED = "loss_concealed"
    DISCONTINUITY = "discontinuity"
    EVIDENCE_CONFLICT = "evidence_conflict"
    EVIDENCE_RECOVERED = "evidence_recovered"
    STALE_STREAM = "stale_stream"
    STALE_FENCE = "stale_fence"
    STALE_ASR_REVISION = "stale_asr_revision"
    COMMITTED = "committed"
    DISCARDED = "discarded"
    PENDING_ENDPOINT_RETRACTED = "pending_endpoint_retracted"


@dataclass(frozen=True, slots=True)
class TurnEvidence:
    # Derived by ConversationProjection from sample-clock endpoint coverage.
    covers_pending_endpoint: bool = False
    vad_start: bool = False
    vad_end: bool = False
    vad_ended: bool = False
    vad_active: bool | None = None
    vad_probability: float | None = None
    semantic_text_present: bool = False
    asr_final: bool = False
    asr_backchannel: bool = False
    asr_coverage: bool | None = None
    speaker_class: SpeakerClass = "uncertain"
    aec_verified: bool | None = None
    residual_echo_score: float | None = None
    playback_active: bool = False
    loss_concealed: bool = False
    discontinuity: bool = False
    clock_gap: bool = False
    stale_stream: bool = False
    stale_fence: bool = False
    stale_asr_revision: bool = False

    def __post_init__(self) -> None:
        if self.speaker_class not in {"owner", "guest", "uncertain"}:
            raise ValueError("turn evidence speaker class is invalid")
        if self.vad_probability is not None and not 0.0 <= self.vad_probability <= 1.0:
            raise ValueError("vad_probability must be between 0 and 1")
        if self.residual_echo_score is not None and not 0.0 <= self.residual_echo_score <= 1.0:
            raise ValueError("residual_echo_score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ProjectionFrame:
    session_id: str
    stream_epoch: int
    frame_index: int
    capture_start_sample: int
    capture_end_sample: int
    state: TurnPhase
    reason: PhaseReason
    floor_state: FloorState
    vad_active: bool | None = None
    vad_probability: float | None = None
    asr_task_epoch: int | None = None
    asr_revision: int | None = None
    asr_coverage: bool | None = None
    semantic_text_present: bool = False
    asr_final: bool = False
    speaker_class: SpeakerClass = "uncertain"
    aec_verified: bool | None = None
    residual_echo_score: float | None = None
    captured_fence: GenerationFence | None = None
    loss_concealed: bool = False
    discontinuity: bool = False

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("projection frame requires a session_id")
        if self.stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if self.frame_index < 0:
            raise ValueError("frame_index must be non-negative")
        if self.capture_start_sample < 0:
            raise ValueError("capture_start_sample must be non-negative")
        if self.capture_end_sample <= self.capture_start_sample:
            raise ValueError("capture_end_sample must follow start")
        if self.speaker_class not in {"owner", "guest", "uncertain"}:
            raise ValueError("projection frame speaker class is invalid")
        expected_index = self.capture_start_sample // PROJECTION_FRAME_SAMPLES
        if self.frame_index != expected_index:
            raise ValueError("frame_index must equal capture_start_sample // 1280")

    def allows_generation_cancel(self) -> bool:
        return self.state is TurnPhase.SEMANTIC_SPEAKING

    def allows_endpoint_schedule(self) -> bool:
        return self.state is TurnPhase.END_CANDIDATE

    def must_hold(self) -> bool:
        return self.state is TurnPhase.UNCERTAIN


def _echo_conflict(evidence: TurnEvidence) -> bool:
    return bool(
        evidence.playback_active
        and evidence.aec_verified is True
        and evidence.residual_echo_score is not None
        and evidence.residual_echo_score >= _MAX_RESIDUAL_ECHO_SCORE
    )


def _low_vad(evidence: TurnEvidence) -> bool:
    return bool(
        evidence.playback_active
        and evidence.vad_probability is not None
        and evidence.vad_probability < _MIN_VAD_PROBABILITY
    )


def _determined_phase(evidence: TurnEvidence) -> tuple[TurnPhase, PhaseReason] | None:
    if _echo_conflict(evidence):
        return TurnPhase.UNCERTAIN, PhaseReason.ECHO_OR_NOISE
    if _low_vad(evidence):
        return TurnPhase.UNCERTAIN, PhaseReason.LOW_VAD
    if evidence.semantic_text_present and not evidence.asr_backchannel:
        if evidence.vad_end or evidence.vad_ended:
            if evidence.asr_coverage is True:
                return TurnPhase.END_CANDIDATE, PhaseReason.ENDPOINT_COVERED
            if evidence.asr_coverage is False:
                return TurnPhase.UNCERTAIN, PhaseReason.ASR_UNCOVERED
        return TurnPhase.SEMANTIC_SPEAKING, PhaseReason.ASR_SEMANTIC
    if evidence.asr_backchannel and evidence.playback_active:
        return TurnPhase.BACKCHANNEL, PhaseReason.PLAYBACK_BACKCHANNEL
    if evidence.vad_end and not evidence.semantic_text_present:
        return TurnPhase.IDLE, PhaseReason.VAD_END_NO_SEMANTIC
    if evidence.vad_start or evidence.vad_active is True:
        return TurnPhase.ACOUSTIC_ONLY, PhaseReason.VAD_START
    return None


def transition_turn_phase(
    current: TurnPhase,
    evidence: TurnEvidence,
) -> tuple[TurnPhase, PhaseReason] | None:
    """Pure CPU transition. None means drop the event or keep the current phase."""

    if evidence.stale_stream or evidence.stale_fence or evidence.stale_asr_revision:
        return None
    if evidence.loss_concealed:
        return TurnPhase.UNCERTAIN, PhaseReason.LOSS_CONCEALED
    if evidence.discontinuity:
        return TurnPhase.UNCERTAIN, PhaseReason.DISCONTINUITY
    if evidence.clock_gap:
        return TurnPhase.UNCERTAIN, PhaseReason.CLOCK_GAP

    if current is TurnPhase.UNCERTAIN:
        recovered = _determined_phase(evidence)
        if recovered is not None and recovered[0] is not TurnPhase.UNCERTAIN:
            return recovered[0], PhaseReason.EVIDENCE_RECOVERED
        return None

    if current is TurnPhase.ACOUSTIC_ONLY and _echo_conflict(evidence):
        return TurnPhase.UNCERTAIN, PhaseReason.ECHO_OR_NOISE
    if current is TurnPhase.ACOUSTIC_ONLY and _low_vad(evidence):
        return TurnPhase.UNCERTAIN, PhaseReason.LOW_VAD

    if current is TurnPhase.IDLE:
        if _echo_conflict(evidence):
            return TurnPhase.UNCERTAIN, PhaseReason.ECHO_OR_NOISE
        if _low_vad(evidence):
            return TurnPhase.UNCERTAIN, PhaseReason.LOW_VAD
        if evidence.vad_start or evidence.vad_active is True:
            return TurnPhase.ACOUSTIC_ONLY, PhaseReason.VAD_START
        if evidence.semantic_text_present and not evidence.asr_backchannel:
            return TurnPhase.SEMANTIC_SPEAKING, PhaseReason.ASR_SEMANTIC
        return None

    if current is TurnPhase.ACOUSTIC_ONLY:
        if evidence.semantic_text_present and not evidence.asr_backchannel:
            return TurnPhase.SEMANTIC_SPEAKING, PhaseReason.ASR_SEMANTIC
        if evidence.asr_backchannel and evidence.playback_active:
            return TurnPhase.BACKCHANNEL, PhaseReason.PLAYBACK_BACKCHANNEL
        if evidence.vad_end and not evidence.semantic_text_present:
            return TurnPhase.IDLE, PhaseReason.VAD_END_NO_SEMANTIC
        return None

    if current is TurnPhase.SEMANTIC_SPEAKING:
        if evidence.asr_backchannel and evidence.playback_active:
            return TurnPhase.BACKCHANNEL, PhaseReason.PLAYBACK_BACKCHANNEL
        if evidence.vad_end or evidence.vad_ended:
            if evidence.asr_coverage is True:
                return TurnPhase.END_CANDIDATE, PhaseReason.ENDPOINT_COVERED
            return TurnPhase.UNCERTAIN, PhaseReason.ASR_UNCOVERED
        if evidence.vad_start or evidence.semantic_text_present:
            return TurnPhase.SEMANTIC_SPEAKING, PhaseReason.ASR_CONTINUATION
        return None

    if current is TurnPhase.END_CANDIDATE:
        # ConversationProjection marks only a continuous, non-final ASR range
        # extension as endpoint coverage. Late finals remain ended evidence.
        if evidence.covers_pending_endpoint or evidence.vad_start:
            return TurnPhase.SEMANTIC_SPEAKING, PhaseReason.PENDING_ENDPOINT_RETRACTED
        return None

    if current is TurnPhase.BACKCHANNEL:
        if evidence.semantic_text_present and not evidence.asr_backchannel:
            return TurnPhase.SEMANTIC_SPEAKING, PhaseReason.BACKCHANNEL_PROMOTED
        if evidence.vad_end or evidence.vad_active is False or not evidence.playback_active:
            return TurnPhase.IDLE, PhaseReason.BACKCHANNEL_IDLE
        return None

    return None


@dataclass(frozen=True, slots=True)
class SpeakerEvidence:
    speaker_class: SpeakerClass = "uncertain"
    reason_code: str = "authority_unavailable"
    authority_verified: bool = False

    def __post_init__(self) -> None:
        if self.speaker_class not in {"owner", "guest", "uncertain"}:
            raise ValueError("projection speaker class is invalid")
        if not self.reason_code:
            raise ValueError("projection speaker evidence requires a reason")


@dataclass(frozen=True, slots=True)
class ProvisionalTurn:
    provisional_id: str
    session_id: str
    stream_epoch: int
    turn_id_hint: int
    revision: int
    capture_start_sample: int
    capture_end_sample: int
    text: str
    floor_state: FloorState
    speaker_evidence: SpeakerEvidence
    source_task_epoch: int
    source_revision: int
    persist_as_turn: bool = False
    history_eligible: bool = False


@dataclass(frozen=True, slots=True)
class ProjectionPatch:
    kind: ProjectionEventKind
    turn: ProvisionalTurn
    reason: str = ""

    def to_payload(self) -> dict[str, object]:
        turn = self.turn
        return {
            "provisional_id": turn.provisional_id,
            "stream_epoch": turn.stream_epoch,
            "provisional_turn_id": turn.turn_id_hint,
            "projection_revision": turn.revision,
            "capture_start_sample": turn.capture_start_sample,
            "capture_end_sample": turn.capture_end_sample,
            "text": turn.text,
            "speaker": "user",
            "floor_state": turn.floor_state.value,
            "speaker_evidence": {
                "speaker_class": turn.speaker_evidence.speaker_class,
                "reason_code": turn.speaker_evidence.reason_code,
                "authority_verified": turn.speaker_evidence.authority_verified,
            },
            "persist_as_turn": False,
            "history_eligible": False,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CommitEvidence:
    session_id: str
    stream_epoch: int
    capture_start_sample: int
    capture_end_sample: int
    text: str
    fence: GenerationFence
    speaker_evidence: SpeakerEvidence
    history_eligible: bool
    context_version: int = 0
    persist_as_turn: bool = True
    provider_final_missing: bool = False


@dataclass(frozen=True, slots=True)
class CommittedTurn:
    provisional_id: str
    session_id: str
    stream_epoch: int
    revision: int
    capture_start_sample: int
    capture_end_sample: int
    text: str
    fence: GenerationFence
    speaker_evidence: SpeakerEvidence
    history_eligible: bool
    context_version: int
    persist_as_turn: bool = True
    provider_final_missing: bool = False

    def to_payload(self) -> dict[str, object]:
        payload = {
            "provisional_id": self.provisional_id,
            "stream_epoch": self.stream_epoch,
            "projection_revision": self.revision,
            "turn_revision": self.revision,
            "capture_start_sample": self.capture_start_sample,
            "capture_end_sample": self.capture_end_sample,
            "text": self.text,
            "speaker": "user",
            "final": True,
            "speaker_evidence": {
                "speaker_class": self.speaker_evidence.speaker_class,
                "reason_code": self.speaker_evidence.reason_code,
                "authority_verified": self.speaker_evidence.authority_verified,
            },
            "persist_as_turn": True,
            "history_eligible": self.history_eligible,
            "context_version": self.context_version,
        }
        if self.provider_final_missing:
            payload["provider_final_missing"] = True
        return payload


@dataclass(slots=True)
class ConversationProjection:
    session_id: str
    timeline: SpeechTimeline
    _provisional: ProvisionalTurn | None = field(default=None, init=False)
    _last_revision: int = field(default=0, init=False)
    _phase: TurnPhase = field(default=TurnPhase.IDLE, init=False)
    _phase_reason: PhaseReason = field(default=PhaseReason.NONE, init=False)
    _floor_state: FloorState = field(default=FloorState.SILENCE, init=False)
    _current_frame: ProjectionFrame | None = field(default=None, init=False)
    _stream_epoch: int | None = field(default=None, init=False)
    _next_frame_index: int = field(default=0, init=False)
    _consecutive_phase_frames: int = field(default=0, init=False)
    _asr_task_epoch: int = field(default=0, init=False)
    _asr_revision: int = field(default=0, init=False)
    _asr_coverage: bool | None = field(default=None, init=False)
    _asr_end_sample: int | None = field(default=None, init=False)
    _asr_final: bool = field(default=False, init=False)
    _semantic_text_present: bool = field(default=False, init=False)
    _voiced_end_sample: int | None = field(default=None, init=False)
    _playback_active: bool = field(default=False, init=False)
    _captured_fence: GenerationFence | None = field(default=None, init=False)
    _latest_capture_sample: int = field(default=0, init=False)
    _emitted_frame_count: int = field(default=0, init=False)
    _speaker_class: SpeakerClass = field(default="uncertain", init=False)
    _aec_verified: bool | None = field(default=None, init=False)
    _residual_echo_score: float | None = field(default=None, init=False)
    _vad_active: bool | None = field(default=None, init=False)
    _vad_probability: float | None = field(default=None, init=False)
    _loss_concealed: bool = field(default=False, init=False)
    _discontinuity: bool = field(default=False, init=False)

    @property
    def provisional(self) -> ProvisionalTurn | None:
        return self._provisional

    @property
    def phase(self) -> TurnPhase:
        return self._phase

    @property
    def phase_reason(self) -> PhaseReason:
        return self._phase_reason

    @property
    def floor_state(self) -> FloorState:
        """Return the phase-authoritative floor."""
        return self._floor_state

    @property
    def current_frame(self) -> ProjectionFrame | None:
        return self._current_frame

    @property
    def consecutive_phase_frames(self) -> int:
        return self._consecutive_phase_frames

    @property
    def emitted_frame_count(self) -> int:
        """Return session-lifetime emitted ProjectionFrames."""
        return self._emitted_frame_count

    @property
    def voiced_end_sample(self) -> int | None:
        return self._voiced_end_sample

    @property
    def latest_capture_sample(self) -> int:
        """Latest accepted uplink sample watermark for shadow telemetry."""

        return self._latest_capture_sample

    @property
    def pending_endpoint(self) -> bool:
        return self._phase is TurnPhase.END_CANDIDATE

    def _next_revision(self) -> int:
        self._last_revision += 1
        return self._last_revision

    def apply_continuous_event(
        self,
        segment: SpeechSegment,
        *,
        turn_id_hint: int,
        speaker_evidence: SpeakerEvidence | None = None,
        playback_active: bool | None = None,
        fence: GenerationFence | None = None,
        aec_verified: bool | None = None,
        discontinuity: bool = False,
    ) -> ProjectionPatch | None:
        if segment.session_id != self.session_id:
            return None
        if segment.kind not in {SegmentKind.VAD, SegmentKind.ASR_PARTIAL, SegmentKind.ASR_FINAL}:
            return None
        if segment not in self.timeline.pending:
            return None
        if fence is not None and self._fence_is_stale(fence):
            return None
        if self._stream_epoch is not None and segment.stream_epoch < self._stream_epoch:
            return None
        if self._stream_epoch is not None and segment.stream_epoch > self._stream_epoch:
            self.reset_phase(stream_epoch=segment.stream_epoch)
        current = self._provisional
        started = current is None
        start_sample = min(
            segment.capture_start_sample,
            current.capture_start_sample if current is not None else segment.capture_start_sample,
        )
        end_sample = max(
            segment.capture_end_sample,
            current.capture_end_sample if current is not None else segment.capture_end_sample,
        )
        text = self.timeline.projected_text(
            stream_epoch=segment.stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        floor_state = self._floor_after(segment, current)
        evidence = speaker_evidence or (
            current.speaker_evidence if current is not None else SpeakerEvidence()
        )
        source_task_epoch, source_revision = max(
            (segment.provider_task_epoch, segment.revision),
            (
                (current.source_task_epoch, current.source_revision)
                if current is not None
                else (0, 0)
            ),
        )
        unchanged = current is not None and (
            current.capture_start_sample,
            current.capture_end_sample,
            current.text,
            current.floor_state,
            current.speaker_evidence,
            current.source_task_epoch,
            current.source_revision,
        ) == (
            start_sample,
            end_sample,
            text,
            floor_state,
            evidence,
            source_task_epoch,
            source_revision,
        )
        patch: ProjectionPatch | None = None
        if not unchanged:
            revision = self._next_revision()
            provisional = ProvisionalTurn(
                provisional_id=(
                    f"{self.session_id}:{segment.stream_epoch}:{turn_id_hint}:{revision}"
                    if current is None
                    else current.provisional_id
                ),
                session_id=self.session_id,
                stream_epoch=segment.stream_epoch,
                turn_id_hint=turn_id_hint if current is None else current.turn_id_hint,
                revision=revision,
                capture_start_sample=start_sample,
                capture_end_sample=end_sample,
                text=text,
                floor_state=floor_state,
                speaker_evidence=evidence,
                source_task_epoch=source_task_epoch,
                source_revision=source_revision,
            )
            self._provisional = provisional
            patch = ProjectionPatch(
                ProjectionEventKind.PROVISIONAL_STARTED
                if started
                else ProjectionEventKind.PROVISIONAL_PATCH,
                provisional,
            )
        self._apply_phase_from_segment(
            segment,
            projected_text=text,
            speaker_class=evidence.speaker_class,
            playback_active=playback_active,
            fence=fence,
            aec_verified=aec_verified,
            discontinuity=discontinuity,
        )
        return self._sync_patch_floor(patch)

    @staticmethod
    def _floor_after(
        segment: SpeechSegment,
        current: ProvisionalTurn | None,
    ) -> FloorState:
        if segment.kind is SegmentKind.VAD:
            return FloorState.UNCERTAIN if segment.final else FloorState.USER_HOLDS_FLOOR
        if current is not None:
            return current.floor_state
        return FloorState.UNCERTAIN if segment.final else FloorState.USER_HOLDS_FLOOR

    def commit_turn(
        self,
        evidence: CommitEvidence,
    ) -> CommittedTurn | ProjectionRejectReason:
        rejected = self.validate_commit(evidence)
        if rejected is not None:
            return rejected
        current = self._provisional
        assert current is not None
        text = evidence.text.strip()
        eligible = bool(
            evidence.history_eligible
            and evidence.speaker_evidence.speaker_class == "owner"
            and evidence.speaker_evidence.authority_verified
        )
        committed = CommittedTurn(
            provisional_id=current.provisional_id,
            session_id=self.session_id,
            stream_epoch=current.stream_epoch,
            revision=self._next_revision(),
            capture_start_sample=evidence.capture_start_sample,
            capture_end_sample=evidence.capture_end_sample,
            text=text,
            fence=evidence.fence,
            speaker_evidence=evidence.speaker_evidence,
            history_eligible=eligible,
            context_version=evidence.context_version,
            provider_final_missing=evidence.provider_final_missing,
        )
        self._provisional = None
        self._enter_phase(
            TurnPhase.IDLE,
            PhaseReason.COMMITTED,
            stream_epoch=current.stream_epoch,
            capture_start_sample=evidence.capture_start_sample,
            capture_end_sample=evidence.capture_end_sample,
            fence=evidence.fence,
            speaker_class=evidence.speaker_evidence.speaker_class,
            asr_final=True,
            semantic_text_present=False,
            asr_coverage=True,
        )
        return committed

    def validate_commit(self, evidence: CommitEvidence) -> ProjectionRejectReason | None:
        """Validate a commit without consuming its provisional turn."""

        current = self._provisional
        if current is None:
            return ProjectionRejectReason.NO_PROVISIONAL
        if evidence.session_id != self.session_id or evidence.fence.session_id != self.session_id:
            return ProjectionRejectReason.SESSION_MISMATCH
        if evidence.stream_epoch != current.stream_epoch:
            return ProjectionRejectReason.STREAM_EPOCH_MISMATCH
        if evidence.fence.turn_id != current.turn_id_hint:
            return ProjectionRejectReason.TURN_FENCE_MISMATCH
        if (
            evidence.capture_start_sample < current.capture_start_sample
            or evidence.capture_end_sample <= evidence.capture_start_sample
            or evidence.capture_end_sample > current.capture_end_sample
        ):
            return ProjectionRejectReason.RANGE_MISMATCH
        text = evidence.text.strip()
        if not text:
            return ProjectionRejectReason.EMPTY_TEXT
        if text != current.text.strip():
            return ProjectionRejectReason.TEXT_MISMATCH
        if not evidence.persist_as_turn:
            return ProjectionRejectReason.NOT_PERSISTABLE
        return None

    def align_provisional_text(self, text: str) -> ProjectionPatch | None:
        """Keep the live provisional on the same sample-clock text as commit.

        Media commit re-reads the timeline after speaker classify. A late ASR
        final can change ``projected_text`` while the provisional still holds
        the previous revision, which used to fail closed as TEXT_MISMATCH and
        swallow a usable weather/weekday turn.
        """

        current = self._provisional
        stripped = text.strip()
        if current is None or not stripped or stripped == current.text.strip():
            return None
        updated = replace(
            current,
            revision=self._next_revision(),
            text=stripped,
        )
        self._provisional = updated
        return ProjectionPatch(ProjectionEventKind.PROVISIONAL_PATCH, updated)

    def apply_speaker_evidence(
        self,
        evidence: SpeakerEvidence,
    ) -> ProjectionPatch | None:
        """Patch late formal authority evidence without changing speech text."""

        current = self._provisional
        if current is None or current.speaker_evidence == evidence:
            return None
        updated = replace(
            current,
            revision=self._next_revision(),
            speaker_evidence=evidence,
        )
        self._provisional = updated
        self._speaker_class = evidence.speaker_class
        return ProjectionPatch(ProjectionEventKind.PROVISIONAL_PATCH, updated)

    def discard_provisional(
        self,
        turn_id: int | None,
        reason: str,
    ) -> ProjectionPatch | None:
        current = self._provisional
        if current is None or (turn_id is not None and turn_id != current.turn_id_hint):
            if current is None and turn_id is None:
                self.reset_phase()
            return None
        discarded = ProvisionalTurn(
            provisional_id=current.provisional_id,
            session_id=current.session_id,
            stream_epoch=current.stream_epoch,
            turn_id_hint=current.turn_id_hint,
            revision=self._next_revision(),
            capture_start_sample=current.capture_start_sample,
            capture_end_sample=current.capture_end_sample,
            text=current.text,
            floor_state=FloorState.SILENCE,
            speaker_evidence=current.speaker_evidence,
            source_task_epoch=current.source_task_epoch,
            source_revision=current.source_revision,
        )
        self._provisional = None
        self._enter_phase(
            TurnPhase.IDLE,
            PhaseReason.DISCARDED,
            stream_epoch=current.stream_epoch,
            capture_start_sample=current.capture_start_sample,
            capture_end_sample=current.capture_end_sample,
            speaker_class=current.speaker_evidence.speaker_class,
            semantic_text_present=False,
        )
        return ProjectionPatch(
            ProjectionEventKind.PROVISIONAL_DISCARDED,
            discarded,
            reason=reason or "unspecified",
        )

    def apply_playback_evidence(
        self,
        *,
        playback_active: bool,
        fence: GenerationFence | None = None,
        capture_sample: int | None = None,
    ) -> ProjectionFrame | None:
        """Update playback/fence context without executing output side effects."""

        if fence is not None and self._fence_is_stale(fence):
            return None
        stream_epoch = self._stream_epoch
        if stream_epoch is None:
            # Playback is context, not a clock source.  Do not synthesize an
            # epoch or frame before real speech evidence establishes one.
            self._playback_active = playback_active
            if fence is not None:
                self._captured_fence = fence
            self._sync_visible_state()
            return None
        previous = self._phase
        self._playback_active = playback_active
        if fence is not None:
            self._captured_fence = fence
        sample = (
            capture_sample if capture_sample is not None else max(self._latest_capture_sample, 1)
        )
        if previous is TurnPhase.BACKCHANNEL and not playback_active:
            self._enter_phase(
                TurnPhase.IDLE,
                PhaseReason.BACKCHANNEL_IDLE,
                stream_epoch=stream_epoch,
                capture_start_sample=max(0, sample - 1),
                capture_end_sample=sample,
                fence=fence,
            )
        else:
            self._sync_visible_state()
            self._publish_frame(
                stream_epoch=stream_epoch,
                capture_start_sample=max(0, sample - 1),
                capture_end_sample=sample,
            )
        return self._current_frame

    def reset_phase(self, *, stream_epoch: int | None = None) -> None:
        """Atomically clear phase, frame counters and pending endpoint."""

        previous_stream_epoch = self._stream_epoch
        target_stream_epoch = previous_stream_epoch if stream_epoch is None else stream_epoch
        self._phase = TurnPhase.IDLE
        self._phase_reason = PhaseReason.DISCARDED
        self._floor_state = FloorState.SILENCE
        self._current_frame = None
        self._stream_epoch = target_stream_epoch
        stream_changed = target_stream_epoch != previous_stream_epoch
        if stream_changed:
            self._next_frame_index = 0
            self._provisional = None
        self._consecutive_phase_frames = 0
        self._asr_task_epoch = 0
        self._asr_revision = 0
        self._asr_coverage = None
        self._asr_end_sample = None
        self._asr_final = False
        self._semantic_text_present = False
        self._voiced_end_sample = None
        self._playback_active = False
        self._captured_fence = None
        if stream_changed:
            self._latest_capture_sample = 0
        self._speaker_class = "uncertain"
        self._aec_verified = None
        self._residual_echo_score = None
        self._vad_active = None
        self._vad_probability = None
        self._loss_concealed = False
        self._discontinuity = False

    def _apply_phase_from_segment(
        self,
        segment: SpeechSegment,
        *,
        projected_text: str,
        speaker_class: SpeakerClass,
        playback_active: bool | None,
        fence: GenerationFence | None,
        aec_verified: bool | None,
        discontinuity: bool,
    ) -> None:
        if fence is not None:
            if self._fence_is_stale(fence):
                return
            self._captured_fence = fence
        if self._stream_epoch is not None and segment.stream_epoch < self._stream_epoch:
            return
        if self._stream_epoch is not None and segment.stream_epoch > self._stream_epoch:
            self.reset_phase(stream_epoch=segment.stream_epoch)
        self._stream_epoch = segment.stream_epoch
        if playback_active is not None:
            self._playback_active = playback_active

        asr_kind = segment.kind in {SegmentKind.ASR_PARTIAL, SegmentKind.ASR_FINAL}
        previous_asr_end = self._asr_end_sample
        if asr_kind:
            # Timeline owns per-segment task/revision ordering.
            self._asr_task_epoch = segment.provider_task_epoch
            self._asr_revision = segment.revision
            self._asr_final = segment.final or segment.kind is SegmentKind.ASR_FINAL
        self._latest_capture_sample = max(self._latest_capture_sample, segment.capture_end_sample)

        if asr_kind:
            self._asr_end_sample = max(self._asr_end_sample or 0, segment.capture_end_sample)
        if segment.kind is SegmentKind.VAD:
            self._vad_active = not segment.final
            if segment.confidence is not None:
                self._vad_probability = segment.confidence
            if segment.final:
                voiced = (
                    segment.voiced_end_sample
                    if segment.voiced_end_sample is not None
                    else segment.capture_start_sample
                )
                self._voiced_end_sample = max(self._voiced_end_sample or 0, voiced)
            elif self._phase is TurnPhase.END_CANDIDATE:
                self._voiced_end_sample = None

        stripped = projected_text.strip()
        duration_ms = max(
            0,
            (segment.capture_end_sample - segment.capture_start_sample)
            * 1_000
            // CAPTURE_SAMPLE_RATE,
        )
        if stripped:
            self._semantic_text_present = not is_backchannel(stripped, duration_ms=duration_ms)
        elif segment.kind is SegmentKind.VAD and segment.final and not self._asr_final:
            pass
        backchannel = bool(stripped and is_backchannel(stripped, duration_ms=duration_ms))
        if self._voiced_end_sample is not None and self._asr_end_sample is not None:
            self._asr_coverage = self._asr_end_sample >= self._voiced_end_sample
        elif self._voiced_end_sample is None:
            self._asr_coverage = None

        if aec_verified is not None:
            self._aec_verified = aec_verified
        elif all(
            value is not None
            for value in (
                segment.near_end_rms,
                segment.far_end_rms,
                segment.residual_echo_score,
            )
        ):
            self._aec_verified = True
        if segment.residual_echo_score is not None:
            self._residual_echo_score = segment.residual_echo_score
        self._speaker_class = speaker_class
        self._loss_concealed = segment.loss_concealed
        self._discontinuity = discontinuity
        # Delayed finals do not prove resumed speech.
        extends_endpoint = bool(
            segment.kind is SegmentKind.ASR_PARTIAL
            and not segment.final
            and self._phase is TurnPhase.END_CANDIDATE
            and self._voiced_end_sample is not None
            and previous_asr_end is not None
            and segment.capture_end_sample > max(previous_asr_end, self._voiced_end_sample)
        )
        covers_endpoint = bool(
            extends_endpoint
            and previous_asr_end is not None
            and segment.capture_start_sample <= previous_asr_end
        )
        asr_clock_gap = bool(
            extends_endpoint
            and previous_asr_end is not None
            and segment.capture_start_sample > previous_asr_end
        )

        evidence = TurnEvidence(
            covers_pending_endpoint=covers_endpoint,
            vad_start=segment.kind is SegmentKind.VAD and not segment.final,
            vad_end=segment.kind is SegmentKind.VAD and segment.final,
            vad_ended=self._voiced_end_sample is not None,
            vad_active=self._vad_active,
            vad_probability=self._vad_probability,
            semantic_text_present=self._semantic_text_present,
            asr_final=self._asr_final,
            asr_backchannel=backchannel,
            asr_coverage=self._asr_coverage,
            speaker_class=speaker_class,
            aec_verified=self._aec_verified,
            residual_echo_score=self._residual_echo_score,
            playback_active=self._playback_active,
            loss_concealed=segment.loss_concealed,
            discontinuity=discontinuity,
            clock_gap=asr_clock_gap,
            stale_stream=False,
            stale_fence=False,
            stale_asr_revision=False,
        )
        transition = transition_turn_phase(self._phase, evidence)
        if transition is None:
            self._publish_frame(
                stream_epoch=segment.stream_epoch,
                capture_start_sample=segment.capture_start_sample,
                capture_end_sample=segment.capture_end_sample,
            )
            return
        next_phase, reason = transition
        self._enter_phase(
            next_phase,
            reason,
            stream_epoch=segment.stream_epoch,
            capture_start_sample=segment.capture_start_sample,
            capture_end_sample=segment.capture_end_sample,
            fence=self._captured_fence,
            speaker_class=speaker_class,
        )

    def _enter_phase(
        self,
        phase: TurnPhase,
        reason: PhaseReason,
        *,
        stream_epoch: int,
        capture_start_sample: int,
        capture_end_sample: int,
        fence: GenerationFence | None = None,
        speaker_class: SpeakerClass | None = None,
        asr_final: bool | None = None,
        semantic_text_present: bool | None = None,
        asr_coverage: bool | None = None,
    ) -> None:
        changed = phase is not self._phase
        self._phase = phase
        self._phase_reason = reason
        if speaker_class is not None:
            self._speaker_class = speaker_class
        if fence is not None:
            self._captured_fence = fence
        if asr_final is not None:
            self._asr_final = asr_final
        if semantic_text_present is not None:
            self._semantic_text_present = semantic_text_present
        if asr_coverage is not None:
            self._asr_coverage = asr_coverage
        if phase is TurnPhase.IDLE:
            self._vad_active = False
            self._voiced_end_sample = None
            self._asr_end_sample = None
            self._asr_coverage = None if asr_coverage is None else asr_coverage
            self._semantic_text_present = (
                False if semantic_text_present is None else semantic_text_present
            )
            self._asr_final = False if asr_final is None else asr_final
            self._loss_concealed = False
            self._discontinuity = False
        if (
            phase is not TurnPhase.END_CANDIDATE
            and reason is PhaseReason.PENDING_ENDPOINT_RETRACTED
        ):
            self._voiced_end_sample = None
        self._floor_state = self._floor_for_phase(phase, self._playback_active)
        self._sync_visible_state()
        if changed:
            self._consecutive_phase_frames = 0
        self._publish_frame(
            stream_epoch=stream_epoch,
            capture_start_sample=capture_start_sample,
            capture_end_sample=capture_end_sample,
        )

    def _publish_frame(
        self,
        *,
        stream_epoch: int,
        capture_start_sample: int,
        capture_end_sample: int,
    ) -> None:
        """Publish complete aligned windows covered by this fact only."""
        first_index = (capture_start_sample + PROJECTION_FRAME_SAMPLES - 1) // (
            PROJECTION_FRAME_SAMPLES
        )
        last_index = capture_end_sample // PROJECTION_FRAME_SAMPLES - 1
        start_index = max(self._next_frame_index, first_index)
        if first_index > self._next_frame_index:
            self._consecutive_phase_frames = 0
        if last_index < start_index:
            return
        # Skip uncovered gaps; never replay an emitted frame index.
        for frame_index in range(start_index, last_index + 1):
            self._current_frame = ProjectionFrame(
                session_id=self.session_id,
                stream_epoch=stream_epoch,
                frame_index=frame_index,
                capture_start_sample=frame_index * PROJECTION_FRAME_SAMPLES,
                capture_end_sample=(frame_index + 1) * PROJECTION_FRAME_SAMPLES,
                state=self._phase,
                reason=self._phase_reason,
                floor_state=self._floor_state,
                vad_active=self._vad_active,
                vad_probability=self._vad_probability,
                asr_task_epoch=self._asr_task_epoch or None,
                asr_revision=self._asr_revision or None,
                asr_coverage=self._asr_coverage,
                semantic_text_present=self._semantic_text_present,
                asr_final=self._asr_final,
                speaker_class=self._speaker_class,
                aec_verified=self._aec_verified,
                residual_echo_score=self._residual_echo_score,
                captured_fence=self._captured_fence,
                loss_concealed=self._loss_concealed,
                discontinuity=self._discontinuity,
            )
            self._emitted_frame_count += 1
        self._next_frame_index = last_index + 1
        self._consecutive_phase_frames = min(
            _MAX_CONSECUTIVE_FRAMES,
            self._consecutive_phase_frames + last_index - start_index + 1,
        )

    def _sync_visible_state(self) -> None:
        """Keep all externally visible phase/floor snapshots atomic."""
        self._floor_state = self._floor_for_phase(self._phase, self._playback_active)
        current = self._provisional
        if current is not None and current.floor_state is not self._floor_state:
            self._provisional = replace(current, floor_state=self._floor_state)

    def _sync_patch_floor(self, patch: ProjectionPatch | None) -> ProjectionPatch | None:
        """Refresh a returned provisional snapshot after phase-owned floor sync."""
        current = self._provisional
        if patch is None or current is None or patch.turn.revision != current.revision:
            return patch
        if patch.turn.floor_state is current.floor_state:
            return patch
        refreshed = replace(patch.turn, floor_state=current.floor_state)
        return replace(patch, turn=refreshed)

    def _fence_is_stale(self, fence: GenerationFence) -> bool:
        if fence.session_id != self.session_id:
            return True
        current = self._captured_fence
        if current is None:
            return False
        if fence.session_epoch < current.session_epoch:
            return True
        if fence.session_epoch > current.session_epoch:
            return False
        return (fence.turn_id, fence.generation_id, fence.tool_epoch) < (
            current.turn_id,
            current.generation_id,
            current.tool_epoch,
        )

    @staticmethod
    def _floor_for_phase(phase: TurnPhase, playback_active: bool) -> FloorState:
        if phase is TurnPhase.IDLE:
            return FloorState.ASSISTANT_HOLDS_FLOOR if playback_active else FloorState.SILENCE
        if phase is TurnPhase.UNCERTAIN:
            return FloorState.UNCERTAIN
        if phase is TurnPhase.END_CANDIDATE:
            return FloorState.UNCERTAIN
        if playback_active:
            return FloorState.OVERLAP
        if phase is TurnPhase.BACKCHANNEL:
            return FloorState.SILENCE
        return FloorState.USER_HOLDS_FLOOR
