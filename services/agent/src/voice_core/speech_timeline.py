"""Voice-core speech contracts backed by the sample-clock timeline.

``orchestration.speech_timeline`` owns the interval algorithm.  This module is
the stable import seam for the media bridge and ASR providers, and adds the
provider result shape required to map a result to that clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.agent.src.orchestration.speech_timeline import (
    ASRLogicalVersion,
    ASRWordTiming,
    SegmentKind,
    SpeechSegment,
    SpeechTimeline,
)


@dataclass(frozen=True, slots=True)
class ASRResult:
    """Provider-neutral ASR result with an absolute capture interval."""

    task_epoch: int
    sentence_id: str
    revision: int
    capture_start_sample: int
    capture_end_sample: int
    text: str
    is_final: bool
    confidence: float | None = None
    provider_begin_ms: int | None = None
    provider_end_ms: int | None = None
    stream_epoch: int = 1
    # Optional reliable word boundaries projected onto the absolute sample
    # clock. Supervisor uses these only for committed-watermark straddles.
    word_timings: tuple[ASRWordTiming, ...] = ()
    # Reconnected providers can trim an expanded sentence to a new tail. Keep
    # the provider sentence id for reconciliation while giving that tail its
    # own timeline replacement identity so it cannot erase the accepted
    # prefix.
    timeline_segment_id: str | None = None

    def __post_init__(self) -> None:
        if self.stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if self.task_epoch < 1:
            raise ValueError("task_epoch must be positive")
        if not isinstance(self.sentence_id, str) or not self.sentence_id:
            raise ValueError("sentence_id is required")
        if self.timeline_segment_id is not None and (
            not isinstance(self.timeline_segment_id, str) or not self.timeline_segment_id
        ):
            raise ValueError("timeline_segment_id must be a non-empty string")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if self.capture_start_sample < 0:
            raise ValueError("capture_start_sample must be non-negative")
        if self.capture_end_sample <= self.capture_start_sample:
            raise ValueError("capture_end_sample must be greater than start")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.provider_begin_ms is not None and self.provider_begin_ms < 0:
            raise ValueError("provider_begin_ms must be non-negative")
        if self.provider_end_ms is not None and self.provider_end_ms < 0:
            raise ValueError("provider_end_ms must be non-negative")
        if (
            self.provider_begin_ms is not None
            and self.provider_end_ms is not None
            and self.provider_end_ms < self.provider_begin_ms
        ):
            raise ValueError("provider_end_ms must not precede provider_begin_ms")
        previous_end = self.capture_start_sample
        for word in self.word_timings:
            if not isinstance(word, ASRWordTiming):
                raise ValueError("word_timings must contain ASRWordTiming values")
            if word.capture_start_sample < previous_end:
                raise ValueError("word timings must be ordered within the result")
            if word.capture_end_sample > self.capture_end_sample:
                raise ValueError("word timing must fit inside the result range")
            previous_end = word.capture_end_sample

    @property
    def segment_id(self) -> str:
        """Stable ID used by the timeline when replacing revisions."""

        return self.timeline_segment_id or self.sentence_id

    @property
    def logical_version(self) -> ASRLogicalVersion:
        """Comparable version that includes the provider task epoch."""

        return ASRLogicalVersion(self.task_epoch, self.revision)


@dataclass(frozen=True, slots=True)
class ASRFinalInterval:
    """One absolute final-result interval shared by adapter and supervisor."""

    stream_epoch: int
    task_epoch: int
    sentence_id: str
    capture_start_sample: int
    capture_end_sample: int
    text: str = field(default="", compare=False)
    revision: int = field(default=0, compare=False)

    @classmethod
    def from_result(cls, result: ASRResult) -> ASRFinalInterval:
        return cls(
            stream_epoch=result.stream_epoch,
            task_epoch=result.task_epoch,
            sentence_id=result.sentence_id,
            capture_start_sample=result.capture_start_sample,
            capture_end_sample=result.capture_end_sample,
            text=result.text,
            revision=result.revision,
        )

    def overlaps(self, other: ASRFinalInterval) -> bool:
        return (
            self.capture_start_sample < other.capture_end_sample
            and other.capture_start_sample < self.capture_end_sample
        )

    def has_same_range(self, other: ASRFinalInterval) -> bool:
        return (
            self.capture_start_sample == other.capture_start_sample
            and self.capture_end_sample == other.capture_end_sample
        )


def asr_result_to_segment(
    result: ASRResult,
    *,
    session_id: str,
    kind: SegmentKind | None = None,
    speaker_class: str | None = None,
) -> SpeechSegment:
    """Map one provider result to the shared interval model."""

    return SpeechSegment(
        session_id=session_id,
        stream_epoch=result.stream_epoch,
        provider_task_epoch=result.task_epoch,
        segment_id=result.segment_id,
        revision=result.revision,
        kind=kind or (SegmentKind.ASR_FINAL if result.is_final else SegmentKind.ASR_PARTIAL),
        capture_start_sample=result.capture_start_sample,
        capture_end_sample=result.capture_end_sample,
        text=result.text,
        final=result.is_final,
        confidence=result.confidence,
        speaker_class=speaker_class,
    )


__all__ = [
    "ASRResult",
    "ASRFinalInterval",
    "ASRLogicalVersion",
    "ASRWordTiming",
    "SegmentKind",
    "SpeechSegment",
    "SpeechTimeline",
    "asr_result_to_segment",
]
