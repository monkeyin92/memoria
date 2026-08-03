"""Provider-neutral ASR task/reconnect supervisor.

The existing FunASR adapter feeds this seam while the provider remains a
replaceable implementation.  It owns only watermarks and sample ranges; it
never decides speaker permissions or invokes the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.agent.src.voice_core.speech_timeline import (
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
    _revisions: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.reconnect_audio_ms <= 0:
            raise ValueError("ASR sample rate and replay window must be positive")
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
        if result.is_final and result.capture_end_sample <= self.last_emitted_final_sample:
            return False
        latest_revision = self._revisions.get(result.segment_id, 0)
        if result.revision < latest_revision:
            return False
        accepted = self.timeline.add(
            asr_result_to_segment(result, session_id=session_id),
        )
        if accepted:
            self._revisions[result.segment_id] = result.revision
            self.mark_provider_acked(result.capture_end_sample)
            if result.is_final:
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
        self.timeline.start_stream_epoch(stream_epoch)
        return True


__all__ = ["ASRStreamSupervisor"]
