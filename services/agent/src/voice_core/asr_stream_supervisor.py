"""Provider-neutral ASR task/reconnect supervisor.

The existing FunASR adapter feeds this seam while the provider remains a
replaceable implementation.  It owns only watermarks and sample ranges; it
never decides speaker permissions or invokes the LLM.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from enum import StrEnum

from services.agent.src.voice_core.speech_timeline import (
    ASRFinalInterval,
    ASRResult,
    ASRTimingCoverage,
    SpeechTimeline,
    asr_result_to_segment,
)


class ASRDecisionReason(StrEnum):
    ACCEPTED = "accepted"
    SESSION_NOT_FOUND = "session_not_found"
    STALE_STREAM_EPOCH = "stale_stream_epoch"
    STALE_TASK_EPOCH = "stale_task_epoch"
    TRANSPORT_DUPLICATE = "transport_duplicate"
    CROSS_SENTENCE_OVERLAP = "cross_sentence_overlap"
    REVISION_CONFLICT = "revision_conflict"
    FULLY_COMMITTED = "fully_committed"
    STRADDLES_COMMITTED_WITHOUT_TIMING = "straddles_committed_without_timing"
    STRADDLES_COMMITTED_WORD_BOUNDARY = "straddles_committed_word_boundary"
    INTERVAL_CONFLICT = "interval_conflict"


@dataclass(frozen=True, slots=True)
class ASRAcceptDecision:
    """The supervisor's single authoritative ASR decision.

    ``accepted`` is either the original result or its committed-watermark
    normalized tail. Consumers must never forward the provider result after a
    decision has been made. ``__bool__`` keeps the narrow legacy test seam
    source-compatible while callers migrate to ``accepted``/``reason``.
    """

    accepted: ASRResult | None
    reason: ASRDecisionReason

    @property
    def result(self) -> ASRResult | None:
        """Compatibility name for callers that treat this as a result decision."""

        return self.accepted

    def __bool__(self) -> bool:
        return self.accepted is not None


@dataclass(slots=True)
class ASRStreamSupervisor:
    sample_rate: int = 16_000
    reconnect_audio_ms: int = 500
    stream_epoch: int = 1
    task_epoch: int = 0
    latest_authoritative_task_epoch: int = 0
    timeline: SpeechTimeline = field(default_factory=SpeechTimeline)
    last_sent_sample: int = 0
    last_provider_acked_sample: int = 0
    last_committed_sample: int = 0
    last_emitted_final_sample: int = 0
    max_result_history: int = 256
    _revisions: dict[tuple[int, str], int] = field(default_factory=dict)
    _latest_task_by_segment: dict[str, int] = field(default_factory=dict)
    _revision_order: deque[tuple[int, str]] = field(default_factory=deque)
    _segment_order: deque[str] = field(default_factory=deque)
    _final_intervals: dict[ASRFinalInterval, ASRResult] = field(
        default_factory=dict,
        init=False,
    )
    _final_interval_order: deque[ASRFinalInterval] = field(
        default_factory=deque,
        init=False,
    )

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.reconnect_audio_ms <= 0 or self.max_result_history <= 0:
            raise ValueError("ASR sample rate, replay window and result history must be positive")
        if self.task_epoch < 0 or self.latest_authoritative_task_epoch < 0:
            raise ValueError("ASR task epochs must be non-negative")
        self.latest_authoritative_task_epoch = max(
            self.latest_authoritative_task_epoch,
            self.task_epoch,
        )
        self.timeline.start_stream_epoch(self.stream_epoch)

    def start_task(self) -> int:
        self.task_epoch = max(self.task_epoch, self.latest_authoritative_task_epoch) + 1
        self.latest_authoritative_task_epoch = self.task_epoch
        return self.task_epoch

    def observe_task(self, task_epoch: int) -> bool:
        """Promote a successfully started provider task for this stream."""

        if task_epoch < 1 or task_epoch < self.latest_authoritative_task_epoch:
            return False
        self.latest_authoritative_task_epoch = task_epoch
        return True

    def record_audio(self, *, start_sample: int, frame_samples: int) -> bool:
        if start_sample < self.last_sent_sample or frame_samples <= 0:
            return False
        self.last_sent_sample = start_sample + frame_samples
        return True

    def mark_provider_acked(self, sample: int) -> None:
        self.last_provider_acked_sample = max(
            self.last_provider_acked_sample,
            min(sample, self.last_sent_sample),
        )

    def mark_committed(self, sample: int) -> None:
        if sample < 0:
            raise ValueError("committed sample must be non-negative")
        self.last_committed_sample = max(self.last_committed_sample, sample)
        self.timeline.commit_range(
            stream_epoch=self.stream_epoch,
            start_sample=0,
            end_sample=max(1, self.last_committed_sample),
        )

    def _normalize_committed_result(self, result: ASRResult) -> ASRAcceptDecision:
        """Drop committed audio or produce a timing-backed tail result.

        A sentence that straddles the committed watermark cannot be split by
        character count or provider callback order. Only complete words with
        absolute sample boundaries are safe to forward.
        """

        committed = self.last_committed_sample
        if result.capture_end_sample <= committed:
            return ASRAcceptDecision(None, ASRDecisionReason.FULLY_COMMITTED)
        if result.capture_start_sample >= committed:
            return ASRAcceptDecision(result, ASRDecisionReason.ACCEPTED)
        evidence = result.timing_evidence
        if (
            not result.word_timings
            or evidence.coverage is not ASRTimingCoverage.COMPLETE
            or evidence.words != result.word_timings
        ):
            return ASRAcceptDecision(
                None,
                ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING,
            )
        tail_items = tuple(
            (index, word)
            for index, word in enumerate(evidence.words)
            if word.capture_start_sample >= committed
            and word.capture_end_sample <= result.capture_end_sample
        )
        if not tail_items:
            return ASRAcceptDecision(
                None,
                ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING,
            )
        first_tail = tail_items[0][0]
        last_tail = tail_items[-1][0] + 1
        tail_words = tuple(word for _, word in tail_items)
        spans = evidence.word_text_spans
        if len(spans) >= last_tail:
            tail_text = result.text[spans[first_tail][0] : spans[last_tail - 1][1]]
        else:
            tail_text = "".join(word.text for word in tail_words)
        tail = replace(
            result,
            capture_start_sample=tail_words[0].capture_start_sample,
            capture_end_sample=tail_words[-1].capture_end_sample,
            text=tail_text,
            timeline_segment_id=f"{result.sentence_id}:tail:{committed}",
            word_timings=tail_words,
        )
        return ASRAcceptDecision(tail, ASRDecisionReason.ACCEPTED)

    def preview_result(self, result: ASRResult) -> ASRAcceptDecision:
        """Apply stream/task/watermark gates without mutating supervisor state."""

        if result.stream_epoch != self.stream_epoch:
            return ASRAcceptDecision(None, ASRDecisionReason.STALE_STREAM_EPOCH)
        if (
            self.latest_authoritative_task_epoch > 0
            and result.task_epoch < self.latest_authoritative_task_epoch
        ):
            return ASRAcceptDecision(None, ASRDecisionReason.STALE_TASK_EPOCH)
        return self._normalize_committed_result(result)

    def accept_result(self, result: ASRResult, *, session_id: str) -> ASRAcceptDecision:
        normalized = self.preview_result(result)
        if normalized.accepted is None:
            return normalized
        result = normalized.accepted
        # A provider task epoch is part of the revision identity. Providers
        # can restart within one media epoch and legitimately reuse a sentence
        # id with an expanded absolute range; the stream-scoped fence above
        # still rejects every result from a superseded task, regardless of
        # sentence id or interval overlap.
        source_segment_id = result.sentence_id
        latest_task = self._latest_task_by_segment.get(source_segment_id)
        if latest_task is not None and result.task_epoch < latest_task:
            return ASRAcceptDecision(None, ASRDecisionReason.STALE_TASK_EPOCH)
        revision_key = (result.task_epoch, source_segment_id)
        latest_revision = self._revisions.get(revision_key, 0)
        if result.revision < latest_revision:
            return ASRAcceptDecision(None, ASRDecisionReason.REVISION_CONFLICT)
        final_interval = ASRFinalInterval.from_result(result)
        superseded_intervals: tuple[ASRFinalInterval, ...] = ()
        if result.is_final:
            # Interval-based dedup, not a global end watermark: out-of-order
            # non-overlapping finals (320..640 before 0..320) must survive,
            # while a higher revision of the exact same interval supersedes
            # the older text.  An identical range replayed by a different task
            # is a reconnect duplicate and fails closed; only a strict
            # expansion by a newer task of the same sentence may cover older
            # accepted audio.  An identical text with a higher revision is a
            # transport duplicate, never a correction, and must not be
            # re-published to the client.
            previous = self._final_intervals.get(final_interval)
            if previous is not None and (
                result.revision <= previous.revision or result.text == previous.text
            ):
                return ASRAcceptDecision(None, ASRDecisionReason.TRANSPORT_DUPLICATE)
            same_task_intervals = tuple(
                interval
                for interval in self._final_intervals
                if interval.stream_epoch == result.stream_epoch
                and interval.task_epoch == result.task_epoch
                and interval.sentence_id == result.sentence_id
            )
            if same_task_intervals and result.revision <= max(
                interval.revision for interval in same_task_intervals
            ):
                return ASRAcceptDecision(None, ASRDecisionReason.REVISION_CONFLICT)
            same_task_correction = bool(same_task_intervals)
            overlapping = [
                interval
                for interval in self._final_intervals
                if interval.stream_epoch == result.stream_epoch
                and interval.overlaps(final_interval)
                # A previously accepted full replay can straddle the
                # committed watermark. It is retained as replay metadata, but
                # must not block a timing-backed tail from the new task.
                and not (
                    self.last_committed_sample > 0
                    and result.capture_start_sample >= self.last_committed_sample
                    and interval.capture_start_sample < self.last_committed_sample
                )
            ]
            if overlapping:
                cross_sentence = [
                    interval
                    for interval in overlapping
                    if interval.sentence_id != result.sentence_id
                ]
                if cross_sentence:
                    return ASRAcceptDecision(None, ASRDecisionReason.CROSS_SENTENCE_OVERLAP)
                if not same_task_correction:
                    same_range_keys = [
                        interval
                        for interval in overlapping
                        if interval.has_same_range(final_interval)
                    ]
                    if same_range_keys and any(
                        interval.task_epoch != result.task_epoch for interval in same_range_keys
                    ):
                        # A new task may correct an exact range, but an identical
                        # text is reconnect transport replay and must not publish.
                        if any(interval.text == result.text for interval in same_range_keys):
                            return ASRAcceptDecision(
                                None,
                                ASRDecisionReason.TRANSPORT_DUPLICATE,
                            )
                    covered_start = min(interval.capture_start_sample for interval in overlapping)
                    covered_end = max(interval.capture_end_sample for interval in overlapping)
                    if not (
                        result.capture_start_sample <= covered_start
                        and result.capture_end_sample >= covered_end
                    ):
                        return ASRAcceptDecision(None, ASRDecisionReason.INTERVAL_CONFLICT)
                    if any(interval.task_epoch > result.task_epoch for interval in overlapping):
                        return ASRAcceptDecision(None, ASRDecisionReason.STALE_TASK_EPOCH)
            if same_task_correction:
                superseded_intervals = same_task_intervals
        elif result.capture_end_sample <= self.last_emitted_final_sample:
            # A partial fully inside already-finalized audio is stale; finals
            # themselves are handled by the interval set above.
            return ASRAcceptDecision(None, ASRDecisionReason.REVISION_CONFLICT)
        accepted = self.timeline.add(
            asr_result_to_segment(result, session_id=session_id),
        )
        if accepted:
            if result.task_epoch > self.latest_authoritative_task_epoch:
                self.observe_task(result.task_epoch)
            if latest_task is None:
                self._segment_order.append(source_segment_id)
            self._latest_task_by_segment[source_segment_id] = max(
                result.task_epoch,
                latest_task or 0,
            )
            if revision_key not in self._revisions:
                self._revision_order.append(revision_key)
            self._revisions[revision_key] = result.revision
            while len(self._revision_order) > self.max_result_history:
                evicted = self._revision_order.popleft()
                self._revisions.pop(evicted, None)
            while len(self._segment_order) > self.max_result_history:
                evicted_segment = self._segment_order.popleft()
                self._latest_task_by_segment.pop(evicted_segment, None)
                for key in tuple(self._revisions):
                    if key[1] == evicted_segment:
                        self._revisions.pop(key, None)
            self.mark_provider_acked(result.capture_end_sample)
            if result.is_final:
                replaced = set(superseded_intervals)
                if final_interval in self._final_intervals:
                    replaced.add(final_interval)
                for interval in replaced:
                    self._final_intervals.pop(interval, None)
                    try:
                        self._final_interval_order.remove(interval)
                    except ValueError:
                        pass
                self._final_intervals[final_interval] = result
                self._final_interval_order.append(final_interval)
                while len(self._final_interval_order) > self.max_result_history:
                    evicted_interval = self._final_interval_order.popleft()
                    self._final_intervals.pop(evicted_interval, None)
                self.last_emitted_final_sample = max(
                    self.last_emitted_final_sample,
                    result.capture_end_sample,
                )
        if not accepted:
            return ASRAcceptDecision(None, ASRDecisionReason.INTERVAL_CONFLICT)
        return ASRAcceptDecision(result, ASRDecisionReason.ACCEPTED)

    def replay_start_sample(self) -> int:
        replay_samples = self.sample_rate * self.reconnect_audio_ms // 1000
        return max(
            self.last_provider_acked_sample,
            self.last_committed_sample,
            self.last_sent_sample - max(1, replay_samples),
        )

    def reconnect(self, *, stream_epoch: int) -> bool:
        if stream_epoch <= self.stream_epoch:
            return False
        self.stream_epoch = stream_epoch
        self.task_epoch += 1
        self.latest_authoritative_task_epoch = self.task_epoch
        self.last_sent_sample = 0
        self.last_provider_acked_sample = 0
        self.last_committed_sample = 0
        self.last_emitted_final_sample = 0
        self._revisions.clear()
        self._final_intervals.clear()
        self._final_interval_order.clear()
        self._latest_task_by_segment.clear()
        self._revision_order.clear()
        self._segment_order.clear()
        self.timeline.start_stream_epoch(stream_epoch)
        return True


__all__ = [
    "ASRAcceptDecision",
    "ASRDecisionReason",
    "ASRStreamSupervisor",
]
