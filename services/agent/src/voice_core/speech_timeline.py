"""Voice-core speech contracts backed by the sample-clock timeline.

``orchestration.speech_timeline`` owns the interval algorithm.  This module is
the stable import seam for the media bridge and ASR providers, and adds the
provider result shape required to map a result to that clock.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.agent.src.orchestration.speech_timeline import (
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

    def __post_init__(self) -> None:
        if self.stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if self.task_epoch < 1:
            raise ValueError("task_epoch must be positive")
        if not isinstance(self.sentence_id, str) or not self.sentence_id:
            raise ValueError("sentence_id is required")
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

    @property
    def segment_id(self) -> str:
        """Stable ID used by the timeline when replacing revisions."""

        return self.sentence_id


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
    "SegmentKind",
    "SpeechSegment",
    "SpeechTimeline",
    "asr_result_to_segment",
]
