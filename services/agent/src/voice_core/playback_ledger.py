"""Playback progress ledger used to derive actual-heard assistant text."""

from __future__ import annotations

from collections import deque
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
    # Optional media sequence that owns this text span.  Older callers did
    # not carry sequence metadata, so ``None`` remains valid and the ledger
    # can infer the received sample range from the span itself.
    sequence: int | None = None

    def __post_init__(self) -> None:
        if self.text_start < 0 or self.text_end <= self.text_start:
            raise ValueError("text range must be a positive interval")
        if self.audio_start_sample < 0 or self.audio_end_sample <= self.audio_start_sample:
            raise ValueError("audio range must be a positive interval")
        if self.text and len(self.text) != self.text_end - self.text_start:
            raise ValueError("text length must match its text range")
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("sequence must be non-negative")


@dataclass(slots=True)
class PlaybackLedger:
    """Track acknowledged audio without ever promoting stale generations."""

    max_fences: int = 64
    _spans: dict[GenerationFence, list[PlaybackSpan]] = field(default_factory=dict)
    _rendered_sample_end: dict[GenerationFence, int] = field(default_factory=dict)
    _completion_sample_end: dict[GenerationFence, int] = field(default_factory=dict)
    _received_sequence: dict[GenerationFence, int] = field(default_factory=dict)
    _received_sample_end: dict[GenerationFence, int] = field(default_factory=dict)
    _received_ranges: dict[GenerationFence, list[tuple[int, int, int]]] = field(
        default_factory=dict
    )
    _client_sequence: dict[GenerationFence, int] = field(default_factory=dict)
    _current_fence: GenerationFence | None = None
    _stale_ack_count: int = 0
    _fence_order: deque[GenerationFence] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if self.max_fences <= 0:
            raise ValueError("max_fences must be positive")

    @property
    def current_fence(self) -> GenerationFence | None:
        return self._current_fence

    @property
    def stale_ack_count(self) -> int:
        return self._stale_ack_count

    def start(self, fence: GenerationFence) -> None:
        """Make ``fence`` the only generation allowed to receive ACKs."""

        self._current_fence = fence
        if fence not in self._spans:
            self._fence_order.append(fence)
        self._spans.setdefault(fence, [])
        self._rendered_sample_end.setdefault(fence, 0)
        self._completion_sample_end.setdefault(fence, 0)
        self._received_sequence.setdefault(fence, -1)
        self._received_sample_end.setdefault(fence, 0)
        self._received_ranges.setdefault(fence, [])
        self._client_sequence.setdefault(fence, -1)
        while len(self._fence_order) > self.max_fences:
            evicted = self._fence_order.popleft()
            if evicted == self._current_fence:
                self._fence_order.append(evicted)
                break
            self._spans.pop(evicted, None)
            self._rendered_sample_end.pop(evicted, None)
            self._completion_sample_end.pop(evicted, None)
            self._received_sequence.pop(evicted, None)
            self._received_sample_end.pop(evicted, None)
            self._received_ranges.pop(evicted, None)
            self._client_sequence.pop(evicted, None)

    def register_audio(
        self,
        fence: GenerationFence,
        sequence: int,
        source_start_sample: int,
        frame_samples: int,
    ) -> bool:
        """Record an emitted PCM frame before accepting client progress.

        ``PlaybackProgress.received_sequence`` is a client claim, not proof
        that Voice Core sent that frame.  The bridge/registry records each
        accepted frame here, and ACK watermarks are then bounded by this
        authoritative sequence/range ledger.
        """

        if sequence < 0 or source_start_sample < 0 or frame_samples <= 0:
            raise ValueError("audio sequence/range must be non-negative")
        if self._current_fence is None:
            self.start(fence)
        if self._current_fence is None or not fence.matches(self._current_fence):
            self._stale_ack_count += 1
            return False
        previous_sequence = self._received_sequence.get(fence, -1)
        previous_end = self._received_sample_end.get(fence, 0)
        if sequence != previous_sequence + 1:
            self._stale_ack_count += 1
            return False
        if previous_sequence < 0 and source_start_sample != 0:
            self._stale_ack_count += 1
            return False
        if previous_sequence >= 0 and source_start_sample != previous_end:
            self._stale_ack_count += 1
            return False
        end_sample = source_start_sample + frame_samples
        self._received_ranges.setdefault(fence, []).append(
            (sequence, source_start_sample, end_sample)
        )
        self._received_sequence[fence] = sequence
        self._received_sample_end[fence] = end_sample
        return True

    # Explicit alias for callers that use transport terminology.
    record_received = register_audio

    def add_span(self, span: PlaybackSpan) -> bool:
        if self._current_fence is None:
            self.start(span.fence)
        if self._current_fence is None or not span.fence.matches(self._current_fence):
            return False
        received_end = self._received_sample_end.get(span.fence, 0)
        if self._received_ranges.get(span.fence):
            # A text span may cover multiple fixed frames and its first frame
            # can arrive before the rest of the phrase.  Keep the trusted
            # mapping now; ``acknowledge`` still caps client progress at the
            # received sequence/range watermark, so future audio is never
            # treated as already heard.
            if span.audio_start_sample < 0:
                self._stale_ack_count += 1
                return False
        else:
            # Backwards-compatible callers used to add a span without first
            # registering frames.  Treat its range as received audio; newer
            # registry code should call ``register_audio`` for every frame so
            # sequence claims can be checked as well.
            self._received_sample_end[span.fence] = max(
                received_end,
                span.audio_end_sample,
            )
        spans = self._spans.setdefault(span.fence, [])
        # Timed subtitles can arrive after their PCM has already been
        # acknowledged. They are still authoritative provider facts, so apply
        # the existing render watermark instead of waiting for a redundant
        # client progress event.
        if span.audio_end_sample <= self._rendered_sample_end.get(span.fence, 0):
            span.acknowledged = True
        spans.append(span)
        spans.sort(key=lambda item: (item.audio_start_sample, item.audio_end_sample))
        return True

    def acknowledge(
        self,
        fence: GenerationFence,
        rendered_sample_end: int,
        *,
        received_sequence: int | None = None,
        approximate: bool = False,
        heard_eligible: bool = True,
    ) -> tuple[PlaybackSpan, ...]:
        """Advance playback, promoting text only from an eligible watermark.

        ``approximate`` remains transport telemetry because H5 compatibility
        paths historically use it for software-owned progress. Hardware paths
        explicitly pass ``heard_eligible=False`` when the device only knows an
        upper bound (queued/I2S-delivered rather than DAC-rendered samples).
        """

        if rendered_sample_end < 0:
            raise ValueError("rendered_sample_end must be non-negative")
        if self._current_fence is None or not fence.matches(self._current_fence):
            self._stale_ack_count += 1
            return ()
        if received_sequence is not None:
            if received_sequence < 0:
                raise ValueError("received_sequence must be non-negative")
            known_sequence = self._received_sequence.get(fence, -1)
            previous_client_sequence = self._client_sequence.get(fence, -1)
            if received_sequence > known_sequence or received_sequence < previous_client_sequence:
                self._stale_ack_count += 1
                return ()
            self._client_sequence[fence] = received_sequence
        effective_sequence = (
            self._received_sequence.get(fence, -1)
            if received_sequence is None
            else received_sequence
        )
        renderable_sample_end = self._renderable_sample_end_for(
            fence,
            effective_sequence,
        )
        # A client cannot claim to have rendered bytes that Voice Core has not
        # accepted/sent yet.  Reject (rather than clamp) this progress so a
        # forged watermark can never promote actual-heard text.
        if rendered_sample_end > renderable_sample_end:
            self._stale_ack_count += 1
            return ()
        completion_watermark = max(
            self._completion_sample_end.get(fence, 0),
            rendered_sample_end,
        )
        self._completion_sample_end[fence] = completion_watermark
        _ = approximate
        previous = self._rendered_sample_end.get(fence, 0)
        watermark = max(previous, rendered_sample_end) if heard_eligible else previous
        self._rendered_sample_end[fence] = watermark
        spans = self._spans.setdefault(fence, [])
        acknowledged: list[PlaybackSpan] = []
        for span in spans:
            if span.audio_end_sample <= watermark:
                if not span.acknowledged:
                    span.acknowledged = True
                    acknowledged.append(span)
        return tuple(acknowledged)

    def _renderable_sample_end_for(
        self,
        fence: GenerationFence,
        received_sequence: int,
    ) -> int:
        if received_sequence < 0:
            if not self._received_ranges.get(fence):
                return self._received_sample_end.get(fence, 0)
            return 0
        ranges = self._received_ranges.get(fence, ())
        for sequence, _start, end in reversed(ranges):
            if sequence <= received_sequence:
                return end
        return 0

    def received_sequence(self, fence: GenerationFence) -> int:
        """Highest sequence registered for ``fence`` (or ``-1``)."""

        return self._received_sequence.get(fence, -1)

    def renderable_sample_end(
        self,
        fence: GenerationFence,
        received_sequence: int | None = None,
    ) -> int:
        """Return the greatest sample end a client may legitimately render."""

        if received_sequence is not None and (
            received_sequence < 0 or received_sequence > self._received_sequence.get(fence, -1)
        ):
            return 0
        sequence = (
            self._received_sequence.get(fence, -1)
            if received_sequence is None
            else received_sequence
        )
        return self._renderable_sample_end_for(fence, sequence)

    def acknowledged_spans(self, fence: GenerationFence) -> tuple[PlaybackSpan, ...]:
        return tuple(span for span in self._spans.get(fence, ()) if span.acknowledged)

    def is_fully_acknowledged(self, fence: GenerationFence) -> bool:
        """Return true once all registered audio and text spans are rendered.

        Text spans determine what may enter actual-heard history.  They are
        optional provider metadata, however, so an empty/invalid transcript
        must not leave a fully played response stuck in ``SPEAKING``.
        """

        received_end = self._received_sample_end.get(fence, 0)
        if received_end <= 0 or self._completion_sample_end.get(fence, 0) < received_end:
            return False
        spans = self._spans.get(fence, ())
        return all(span.acknowledged for span in spans)

    def is_playback_complete(self, fence: GenerationFence) -> bool:
        """Return true when transport playback reached all registered audio.

        This is intentionally independent of Actual Heard text eligibility:
        an approximate hardware terminal receipt may release the speaking
        state without asserting that buffered tail audio was heard.
        """

        received_end = self._received_sample_end.get(fence, 0)
        return received_end > 0 and self._completion_sample_end.get(fence, 0) >= received_end

    def actual_heard_text(self, fence: GenerationFence) -> str:
        """Return only text whose complete mapped span was actually rendered."""

        return "".join(span.text for span in self.acknowledged_spans(fence) if span.text)

    def rendered_sample_end(self, fence: GenerationFence) -> int:
        return self._rendered_sample_end.get(fence, 0)

    def discard(self, fence: GenerationFence) -> None:
        """Forget spans after a cancellation; old ACKs remain stale."""

        self._spans.pop(fence, None)
        self._rendered_sample_end.pop(fence, None)
        self._completion_sample_end.pop(fence, None)
        self._received_sequence.pop(fence, None)
        self._received_sample_end.pop(fence, None)
        self._received_ranges.pop(fence, None)
        self._client_sequence.pop(fence, None)
        try:
            self._fence_order.remove(fence)
        except ValueError:
            pass


__all__ = ["PlaybackLedger", "PlaybackSpan"]
