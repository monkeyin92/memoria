"""Project continuous speech facts into provisional and committed turns."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.speech_timeline import (
    SegmentKind,
    SpeechSegment,
    SpeechTimeline,
)

SpeakerClass = Literal["owner", "guest", "uncertain"]


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

    @property
    def provisional(self) -> ProvisionalTurn | None:
        return self._provisional

    def _next_revision(self) -> int:
        self._last_revision += 1
        return self._last_revision

    def apply_continuous_event(
        self,
        segment: SpeechSegment,
        *,
        turn_id_hint: int,
        speaker_evidence: SpeakerEvidence | None = None,
    ) -> ProjectionPatch | None:
        if segment.session_id != self.session_id:
            return None
        if segment.kind not in {SegmentKind.VAD, SegmentKind.ASR_PARTIAL, SegmentKind.ASR_FINAL}:
            return None
        if segment not in self.timeline.pending:
            return None
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
        if current is not None and (
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
        ):
            return None
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
        return ProjectionPatch(
            ProjectionEventKind.PROVISIONAL_STARTED
            if started
            else ProjectionEventKind.PROVISIONAL_PATCH,
            provisional,
        )

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
        return ProjectionPatch(ProjectionEventKind.PROVISIONAL_PATCH, updated)

    def discard_provisional(
        self,
        turn_id: int | None,
        reason: str,
    ) -> ProjectionPatch | None:
        current = self._provisional
        if current is None or (turn_id is not None and turn_id != current.turn_id_hint):
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
        return ProjectionPatch(
            ProjectionEventKind.PROVISIONAL_DISCARDED,
            discarded,
            reason=reason or "unspecified",
        )
