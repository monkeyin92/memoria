"""Playback progress ledger used to derive actual-heard assistant text."""

from __future__ import annotations

from dataclasses import dataclass, field

from services.agent.src.contracts.ids import GenerationFence


@dataclass(slots=True)
class PlaybackSpan:
    """Map a text range to the audio samples that render it."""

    fence: GenerationFence
    text_start: int
    text_end: int
    audio_start_sample: int
    audio_end_sample: int
    text: str = ""
    acknowledged: bool = False

    def __post_init__(self) -> None:
        if self.text_start < 0 or self.text_end <= self.text_start:
            raise ValueError("text range must be a positive interval")
        if self.audio_start_sample < 0 or self.audio_end_sample <= self.audio_start_sample:
            raise ValueError("audio range must be a positive interval")
        if self.text and len(self.text) != self.text_end - self.text_start:
            raise ValueError("text length must match its text range")


@dataclass(slots=True)
class PlaybackLedger:
    """Track acknowledged audio without ever promoting stale generations."""

    _spans: dict[GenerationFence, list[PlaybackSpan]] = field(default_factory=dict)
    _rendered_sample_end: dict[GenerationFence, int] = field(default_factory=dict)
    _current_fence: GenerationFence | None = None
    _stale_ack_count: int = 0

    @property
    def current_fence(self) -> GenerationFence | None:
        return self._current_fence

    @property
    def stale_ack_count(self) -> int:
        return self._stale_ack_count

    def start(self, fence: GenerationFence) -> None:
        """Make ``fence`` the only generation allowed to receive ACKs."""

        self._current_fence = fence
        self._spans.setdefault(fence, [])
        self._rendered_sample_end.setdefault(fence, 0)

    def add_span(self, span: PlaybackSpan) -> bool:
        if self._current_fence is None:
            self.start(span.fence)
        if self._current_fence is None or not span.fence.matches(self._current_fence):
            return False
        spans = self._spans.setdefault(span.fence, [])
        spans.append(span)
        spans.sort(key=lambda item: (item.audio_start_sample, item.audio_end_sample))
        return True

    def acknowledge(
        self,
        fence: GenerationFence,
        rendered_sample_end: int,
        *,
        approximate: bool = False,
    ) -> tuple[PlaybackSpan, ...]:
        """Mark all fully rendered spans up to the monotonic sample watermark."""

        _ = approximate  # retained in the API for H5-vs-hardware telemetry.
        if rendered_sample_end < 0:
            raise ValueError("rendered_sample_end must be non-negative")
        if self._current_fence is None or not fence.matches(self._current_fence):
            self._stale_ack_count += 1
            return ()
        previous = self._rendered_sample_end.get(fence, 0)
        watermark = max(previous, rendered_sample_end)
        self._rendered_sample_end[fence] = watermark
        spans = self._spans.setdefault(fence, [])
        acknowledged: list[PlaybackSpan] = []
        for span in spans:
            if span.audio_end_sample <= watermark:
                if not span.acknowledged:
                    span.acknowledged = True
                    acknowledged.append(span)
        return tuple(acknowledged)

    def acknowledged_spans(self, fence: GenerationFence) -> tuple[PlaybackSpan, ...]:
        return tuple(span for span in self._spans.get(fence, ()) if span.acknowledged)

    def is_fully_acknowledged(self, fence: GenerationFence) -> bool:
        """Return true only after every mapped text span crossed the watermark."""

        spans = self._spans.get(fence, ())
        return bool(spans) and all(span.acknowledged for span in spans)

    def actual_heard_text(self, fence: GenerationFence) -> str:
        """Return only text whose complete mapped span was actually rendered."""

        return "".join(
            span.text
            for span in self.acknowledged_spans(fence)
            if span.text
        )

    def rendered_sample_end(self, fence: GenerationFence) -> int:
        return self._rendered_sample_end.get(fence, 0)

    def discard(self, fence: GenerationFence) -> None:
        """Forget spans after a cancellation; old ACKs remain stale."""

        self._spans.pop(fence, None)
        self._rendered_sample_end.pop(fence, None)


__all__ = ["PlaybackLedger", "PlaybackSpan"]
