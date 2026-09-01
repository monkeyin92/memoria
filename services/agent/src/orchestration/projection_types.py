"""Value types shared by the conversation projection and its consumers."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

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
