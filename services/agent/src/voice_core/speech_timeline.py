"""Voice-core speech contracts backed by the sample-clock timeline.

``orchestration.speech_timeline`` owns the interval algorithm.  This module is
the stable import seam for the media bridge and ASR providers, and adds the
provider result shape required to map a result to that clock.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

from services.agent.src.orchestration.speech_timeline import (
    ASRLogicalVersion,
    ASRWordTiming,
    SegmentKind,
    SpeechSegment,
    SpeechTimeline,
)


class ASRTimingCoverage(StrEnum):
    """How much of the provider transcript the word evidence proves."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ASRTimingEvidence:
    """Validated word evidence; only ``COMPLETE`` is safe for watermark trim."""

    words: tuple[ASRWordTiming, ...] = ()
    coverage: ASRTimingCoverage = ASRTimingCoverage.INVALID
    normalized_text: str = ""
    word_text_spans: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        if len(self.word_text_spans) > len(self.words):
            raise ValueError("timing evidence cannot have more spans than words")
        if any(start < 0 or end <= start for start, end in self.word_text_spans):
            raise ValueError("timing evidence text spans must be ordered ranges")


def _normalized_text_with_offsets(text: str) -> tuple[str, tuple[int, ...]]:
    """Normalize comparison text while retaining source offsets for safe tails."""

    normalized: list[str] = []
    offsets: list[int] = []
    for index, source_char in enumerate(text):
        for char in unicodedata.normalize("NFKC", source_char):
            if char.isspace():
                continue
            normalized.append(char)
            offsets.append(index)
    return "".join(normalized), tuple(offsets)


def build_asr_timing_evidence(
    text: str,
    words: tuple[ASRWordTiming, ...],
    *,
    capture_start_sample: int,
    capture_end_sample: int,
) -> ASRTimingEvidence:
    """Validate timing order, range and transcript coverage without raising."""

    if not words:
        return ASRTimingEvidence()
    previous_end = capture_start_sample
    for word in words:
        if (
            word.capture_start_sample < capture_start_sample
            or word.capture_end_sample > capture_end_sample
            or word.capture_start_sample < previous_end
        ):
            return ASRTimingEvidence()
        previous_end = word.capture_end_sample

    normalized_text, source_offsets = _normalized_text_with_offsets(text)
    normalized_words: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for word in words:
        normalized_word, _ = _normalized_text_with_offsets(word.text)
        if not normalized_word:
            return ASRTimingEvidence()
        start = normalized_text.find(normalized_word, cursor)
        if start < 0:
            return ASRTimingEvidence(
                words=words,
                coverage=ASRTimingCoverage.PARTIAL,
                normalized_text=normalized_text,
                word_text_spans=(),
            )
        end = start + len(normalized_word)
        spans.append((source_offsets[start], source_offsets[end - 1] + 1))
        normalized_words.append(normalized_word)
        cursor = end
    coverage = (
        ASRTimingCoverage.COMPLETE
        if "".join(normalized_words) == normalized_text and cursor == len(normalized_text)
        else ASRTimingCoverage.PARTIAL
    )
    return ASRTimingEvidence(
        words=words,
        coverage=coverage,
        normalized_text=normalized_text,
        word_text_spans=tuple(spans),
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
    loss_concealed: bool = False
    provider_begin_ms: int | None = None
    provider_end_ms: int | None = None
    stream_epoch: int = 1
    # Optional reliable word boundaries projected onto the absolute sample
    # clock. Supervisor uses these only for committed-watermark straddles.
    word_timings: tuple[ASRWordTiming, ...] = ()
    # True when the offline rescue synthesized this final because the realtime
    # provider stayed silent.  It stands in for a missing provider result, so a
    # real provider final covering the same audio must be able to replace it
    # even when the rescue interval is longer (mid-utterance rescue fires while
    # the VAD segment is still open and would otherwise win on span alone).
    rescue_synthesized: bool = False
    timing_evidence: ASRTimingEvidence = field(init=False)
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
        for word in self.word_timings:
            if not isinstance(word, ASRWordTiming):
                raise ValueError("word_timings must contain ASRWordTiming values")
        object.__setattr__(
            self,
            "timing_evidence",
            build_asr_timing_evidence(
                self.text,
                self.word_timings,
                capture_start_sample=self.capture_start_sample,
                capture_end_sample=self.capture_end_sample,
            ),
        )

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
    # Carried for overlap arbitration only; excluded from identity so an
    # interval stays poppable regardless of its provenance.
    rescue_synthesized: bool = field(default=False, compare=False)

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
            rescue_synthesized=result.rescue_synthesized,
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
        loss_concealed=result.loss_concealed,
    )


__all__ = [
    "ASRResult",
    "ASRFinalInterval",
    "ASRLogicalVersion",
    "ASRTimingCoverage",
    "ASRTimingEvidence",
    "ASRWordTiming",
    "SegmentKind",
    "SpeechSegment",
    "SpeechTimeline",
    "asr_result_to_segment",
    "build_asr_timing_evidence",
]
