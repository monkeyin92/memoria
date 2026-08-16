"""Sample-clock based speech timeline.

The LiveKit callback order is not an audio clock.  This module is deliberately
provider-neutral: VAD, KWS and ASR all publish intervals on the same sample
clock and a committed range is the only operation that consumes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


@dataclass(frozen=True, slots=True, order=True)
class ASRLogicalVersion:
    """Comparable ASR version shared by provider tasks and the timeline.

    Provider ``revision`` values are task-local and restart at one after a
    reconnect. Pairing that revision with the monotonically increasing task
    epoch gives every result one ordering that remains valid across tasks.
    """

    task_epoch: int
    provider_revision: int

    def __post_init__(self) -> None:
        if self.task_epoch < 0:
            raise ValueError("task epoch must be non-negative")
        if self.provider_revision < 1:
            raise ValueError("provider revision must be positive")


@dataclass(frozen=True, slots=True)
class ASRWordTiming:
    """A provider word boundary projected onto the capture sample clock."""

    text: str
    capture_start_sample: int
    capture_end_sample: int

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("word text must not be empty")
        if self.capture_start_sample < 0:
            raise ValueError("word start sample must be non-negative")
        if self.capture_end_sample <= self.capture_start_sample:
            raise ValueError("word end sample must follow its start")


class SegmentKind(StrEnum):
    VAD = "vad"
    ASR_PARTIAL = "asr_partial"
    ASR_FINAL = "asr_final"
    KWS = "kws"


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    session_id: str
    stream_epoch: int
    provider_task_epoch: int
    segment_id: str
    revision: int
    kind: SegmentKind
    capture_start_sample: int
    capture_end_sample: int
    text: str = ""
    final: bool = False
    confidence: float | None = None
    speaker_class: str | None = None
    hard_stop: bool = False
    voiced_end_sample: int | None = None
    loss_concealed: bool = False
    # Transport-level acoustic facts for the hardware-agnostic interruption
    # policy.  Absent values are None: a policy must treat them as
    # unproven rather than trusted.
    near_end_rms: float | None = None
    far_end_rms: float | None = None
    residual_echo_score: float | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("speech segment requires a session_id")
        if self.stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if self.provider_task_epoch < 0:
            raise ValueError("provider_task_epoch must be non-negative")
        if not self.segment_id:
            raise ValueError("speech segment requires a segment_id")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if self.capture_start_sample < 0:
            raise ValueError("capture_start_sample must be non-negative")
        if self.capture_end_sample <= self.capture_start_sample:
            raise ValueError("capture_end_sample must be greater than start")
        if self.voiced_end_sample is not None and not (
            0 <= self.voiced_end_sample < self.capture_end_sample
        ):
            raise ValueError("voiced end sample must precede the event sample end")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        for name, value in (
            ("near_end_rms", self.near_end_rms),
            ("far_end_rms", self.far_end_rms),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.residual_echo_score is not None and not (
            0.0 <= self.residual_echo_score <= 1.0
        ):
            raise ValueError("residual_echo_score must be between 0 and 1")

    @property
    def logical_version(self) -> ASRLogicalVersion:
        """Version used for all replacement and canonical selection decisions."""

        return ASRLogicalVersion(self.provider_task_epoch, self.revision)


@dataclass(slots=True)
class SpeechTimeline:
    """Interval store with a committed watermark and epoch fence.

    A segment is accepted at most once per ``segment_id``/logical version.  A
    higher ``(task_epoch, provider_revision)`` replaces the older provider
    result, while a final result always wins over a partial at the same
    version. Events from an old epoch or behind the committed watermark are
    rejected instead of being re-associated with the newest turn.
    """

    _segments: list[SpeechSegment] = field(default_factory=list)
    _current_stream_epoch: int | None = None
    _committed_sample: int = 0
    # Kept under the historical attribute name for introspection compatibility;
    # values are logical versions, never bare provider revisions.
    _last_segment_revision: dict[tuple[int, str], ASRLogicalVersion] = field(
        default_factory=dict,
    )
    _dropped_late: int = 0
    _dropped_epoch: int = 0

    @property
    def stream_epoch(self) -> int | None:
        return self._current_stream_epoch

    @property
    def committed_sample(self) -> int:
        return self._committed_sample

    @property
    def dropped_late(self) -> int:
        return self._dropped_late

    @property
    def dropped_epoch(self) -> int:
        return self._dropped_epoch

    @property
    def pending(self) -> tuple[SpeechSegment, ...]:
        return tuple(self._segments)

    def start_stream_epoch(self, stream_epoch: int) -> bool:
        """Move to a new media epoch after a discontinuity.

        Epochs are monotonic.  Moving forward drops all old pending events and
        resets the sample watermark because sample positions are local to an
        epoch.  Repeating the current epoch is harmless; going backwards is
        rejected.
        """

        if stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        current = self._current_stream_epoch
        if current is not None and stream_epoch < current:
            self._dropped_epoch += 1
            return False
        if current == stream_epoch:
            return True
        self._current_stream_epoch = stream_epoch
        self._segments.clear()
        self._last_segment_revision.clear()
        self._committed_sample = 0
        return True

    def mark_discontinuity(self) -> int:
        """Advance the epoch and return it for callers stamping new frames."""

        next_epoch = (self._current_stream_epoch or 0) + 1
        self.start_stream_epoch(next_epoch)
        return next_epoch

    def add(self, segment: SpeechSegment) -> bool:
        """Add a timeline event, returning ``False`` when it is stale."""

        current = self._current_stream_epoch
        if current is None:
            self.start_stream_epoch(segment.stream_epoch)
            current = segment.stream_epoch
        if not self.can_add(segment):
            if segment.stream_epoch != current:
                self._dropped_epoch += 1
            else:
                self._dropped_late += 1
            return False

        # Replace the provider's previous revision rather than concatenating
        # partials.  This is the key difference from callback/FIFO assembly.
        self._segments = [
            item
            for item in self._segments
            if not (
                item.stream_epoch == segment.stream_epoch and item.segment_id == segment.segment_id
            )
        ]
        self._segments.append(segment)
        self._last_segment_revision[(segment.stream_epoch, segment.segment_id)] = (
            segment.logical_version
        )
        self._segments.sort(
            key=lambda item: (
                item.capture_start_sample,
                item.capture_end_sample,
                item.segment_id,
                item.logical_version,
            )
        )
        return True

    def can_add(self, segment: SpeechSegment) -> bool:
        """Return whether ``add`` would accept the segment, without mutation."""

        current = self._current_stream_epoch
        if current is not None and segment.stream_epoch != current:
            return False
        if segment.capture_end_sample <= self._committed_sample:
            return False
        key = (segment.stream_epoch, segment.segment_id)
        previous_version = self._last_segment_revision.get(key)
        if previous_version is not None and segment.logical_version < previous_version:
            return False
        previous = next(
            (
                item
                for item in self._segments
                if item.stream_epoch == segment.stream_epoch
                and item.segment_id == segment.segment_id
            ),
            None,
        )
        if previous is None or previous_version != segment.logical_version:
            return True
        # A logical version is idempotent.  The sole same-version transition
        # is partial -> final; exact replays and final -> partial regressions
        # are stale observations rather than new timeline facts.
        return not previous.final and segment.final

    def latest_task_epoch(self, stream_epoch: int) -> int:
        """Return the highest provider task version observed in one stream."""

        return max(
            (
                version.task_epoch
                for (epoch, _), version in self._last_segment_revision.items()
                if epoch == stream_epoch
            ),
            default=0,
        )

    def commit_range(
        self,
        *,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
    ) -> tuple[SpeechSegment, ...]:
        """Consume the events overlapping one logical turn interval."""

        if start_sample < 0 or end_sample <= start_sample:
            raise ValueError("commit range must be a positive sample interval")
        if self._current_stream_epoch != stream_epoch:
            self._dropped_epoch += 1
            return ()
        matched = self.segments_in_range(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        self._committed_sample = max(self._committed_sample, end_sample)
        self._segments = [
            item
            for item in self._segments
            if not (
                item.stream_epoch == stream_epoch
                and item.capture_end_sample <= self._committed_sample
            )
        ]
        return matched

    def segments_in_range(
        self,
        *,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
    ) -> tuple[SpeechSegment, ...]:
        """Read overlapping pending segments without advancing the watermark."""

        if start_sample < 0 or end_sample <= start_sample:
            raise ValueError("projection range must be a positive sample interval")
        if self._current_stream_epoch != stream_epoch:
            return ()
        return tuple(
            item
            for item in self._segments
            if item.stream_epoch == stream_epoch
            and item.capture_end_sample > start_sample
            and item.capture_start_sample < end_sample
        )

    @staticmethod
    def _canonical_text_from_segments(segments: tuple[SpeechSegment, ...]) -> str:
        selected: dict[tuple[int, int], SpeechSegment] = {}
        for segment in segments:
            if not segment.text.strip():
                continue
            key = (segment.capture_start_sample, segment.capture_end_sample)
            current = selected.get(key)
            if current is None or (segment.final, segment.logical_version) > (
                current.final,
                current.logical_version,
            ):
                selected[key] = segment
        return " ".join(item.text.strip() for item in selected.values()).strip()

    def projected_text(
        self,
        *,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
    ) -> str:
        """Project the current canonical text without committing the range."""

        return self._canonical_text_from_segments(
            self.segments_in_range(
                stream_epoch=stream_epoch,
                start_sample=start_sample,
                end_sample=end_sample,
            )
        )

    def canonical_text(
        self,
        *,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
    ) -> str:
        """Return one text per audio interval, preferring final revisions."""

        segments = self.commit_range(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        return self._canonical_text_from_segments(segments)
