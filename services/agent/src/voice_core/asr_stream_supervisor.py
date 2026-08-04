"""Provider-neutral ASR task/reconnect supervisor.

The existing FunASR adapter feeds this seam while the provider remains a
replaceable implementation.  It owns only watermarks and sample ranges; it
never decides speaker permissions or invokes the LLM.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from services.agent.src.voice_core.speech_timeline import (
    ASRFinalInterval,
    ASRResult,
    SpeechTimeline,
    asr_result_to_segment,
)


@dataclass(slots=True)
class ASRStreamSupervisor:
    sample_rate: int = 16_000
    reconnect_audio_ms: int = 500
    stream_epoch: int = 1
    task_epoch: int = 0
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
    _final_intervals: dict[ASRFinalInterval, int] = field(
        default_factory=dict,
        init=False,
    )
    _final_interval_order: deque[ASRFinalInterval] = field(
        default_factory=deque,
        init=False,
    )

    def __post_init__(self) -> None:
        if (
            self.sample_rate <= 0
            or self.reconnect_audio_ms <= 0
            or self.max_result_history <= 0
        ):
            raise ValueError("ASR sample rate, replay window and result history must be positive")
        self.timeline.start_stream_epoch(self.stream_epoch)

    def start_task(self) -> int:
        self.task_epoch += 1
        return self.task_epoch

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

    def accept_result(self, result: ASRResult, *, session_id: str) -> bool:
        if result.stream_epoch != self.stream_epoch:
            return False
        if result.capture_end_sample <= self.last_committed_sample:
            return False
        # A provider task epoch is part of the revision identity.  Providers
        # can restart within one media epoch and legitimately reuse a
        # sentence id with an expanded absolute range; treating that as the
        # old sentence would drop the only final covering the tail.  A late
        # result from an older task is still rejected once a newer task has
        # published the same segment, so reconnect replay cannot roll the
        # timeline backwards.
        latest_task = self._latest_task_by_segment.get(result.segment_id)
        if latest_task is not None and result.task_epoch < latest_task:
            return False
        revision_key = (result.task_epoch, result.segment_id)
        latest_revision = self._revisions.get(revision_key, 0)
        if result.revision < latest_revision:
            return False
        final_interval = ASRFinalInterval.from_result(result)
        if result.is_final:
            # Interval-based dedup, not a global end watermark: out-of-order
            # non-overlapping finals (320..640 before 0..320) must survive,
            # while a higher revision of the exact same interval supersedes
            # the older text.  An identical range replayed by a different task
            # is a reconnect duplicate and fails closed; only a strict
            # expansion by a newer task of the same sentence may cover older
            # accepted audio.
            previous_revision = self._final_intervals.get(final_interval)
            if previous_revision is not None and result.revision <= previous_revision:
                return False
            overlapping = [
                interval
                for interval in self._final_intervals
                if interval.stream_epoch == result.stream_epoch
                and interval.overlaps(final_interval)
            ]
            if overlapping:
                cross_sentence = [
                    interval for interval in overlapping if interval.sentence_id != result.sentence_id
                ]
                if cross_sentence:
                    return False
                same_range_keys = [
                    interval
                    for interval in overlapping
                    if interval.has_same_range(final_interval)
                ]
                if same_range_keys and any(
                    interval.task_epoch != result.task_epoch for interval in same_range_keys
                ):
                    # The exact same audio interval already accepted by another
                    # provider task: reconnect replay, never a new turn.
                    return False
                covered_start = min(interval.capture_start_sample for interval in overlapping)
                covered_end = max(interval.capture_end_sample for interval in overlapping)
                if not (
                    result.capture_start_sample <= covered_start
                    and result.capture_end_sample >= covered_end
                ):
                    return False
                if any(interval.task_epoch > result.task_epoch for interval in overlapping):
                    return False
        elif result.capture_end_sample <= self.last_emitted_final_sample:
            # A partial fully inside already-finalized audio is stale; finals
            # themselves are handled by the interval set above.
            return False
        accepted = self.timeline.add(
            asr_result_to_segment(result, session_id=session_id),
        )
        if accepted:
            if latest_task is None:
                self._segment_order.append(result.segment_id)
            self._latest_task_by_segment[result.segment_id] = max(
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
                if final_interval in self._final_intervals:
                    # A higher revision of the same interval replaces the
                    # previously accepted final instead of being dropped.
                    self._final_intervals.pop(final_interval, None)
                    try:
                        self._final_interval_order.remove(final_interval)
                    except ValueError:
                        pass
                self._final_intervals[final_interval] = result.revision
                self._final_interval_order.append(final_interval)
                while len(self._final_interval_order) > self.max_result_history:
                    evicted_interval = self._final_interval_order.popleft()
                    self._final_intervals.pop(evicted_interval, None)
                self.last_emitted_final_sample = max(
                    self.last_emitted_final_sample,
                    result.capture_end_sample,
                )
        return accepted

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


__all__ = ["ASRStreamSupervisor"]
