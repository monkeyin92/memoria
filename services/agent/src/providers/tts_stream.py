"""Streaming TTS base for the Doubao and CosyVoice adapters.

It replaces the ``livekit.agents.tts`` ``SynthesizeStream``/``AudioEmitter``
pair the adapters were written against and keeps the contract they and the
media provider adapter rely on:

- ``push_text``/``end_input`` feed one text segment; text pushed after the
  segment was flushed is dropped with a warning, text after ``end_input`` is
  ignored;
- ``_run`` starts as soon as the stream is created, so a provider can open its
  session while the first phrase is still being generated;
- a retryable ``APIError`` raised before any audio reached the consumer
  starts a fresh ``_run`` (at most ``max_retry`` times, 0.1 s then
  ``retry_interval`` apart) with the text pushed so far replayed into a new
  input channel; once audio was delivered the error is final;
- iterating the stream yields ``SynthesizedAudio`` events in push order and
  raises the stream's failure after the last delivered event;
- raw 16-bit PCM is cut into ``frame_size_ms`` frames and the last 10 ms is
  held back so the final frame of a segment can carry ``is_final``; a partial
  sample left at the end of a segment is dropped. Timed transcripts pushed by
  the provider ride on the next frame sent, in ``frame.userdata``.

Deliberate simplification: frames are delivered synchronously as audio is
pushed. The library ran a separate emitter task and, when audio arrived slower
than real time, flushed the held-back tail early on a timer; after such a flush
it could end the segment with an extra 10 ms silence marker. No consumer here
needs that pacing (the media provider adapter re-frames PCM itself).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from services.agent.src.providers.provider_errors import (
    APIConnectOptions,
    APIError,
    APIStatusError,
)

logger = logging.getLogger(__name__)

#: ``AudioFrame.userdata`` key holding the timed transcript sent with a frame.
USERDATA_TIMED_TRANSCRIPT = "timed_transcript"



class TimedString(str):
    """Text of one subtitle word with its start and end time in seconds."""

    start_time: float
    end_time: float

    def __new__(cls, text: str, *, start_time: float, end_time: float) -> TimedString:
        obj = super().__new__(cls, text)
        obj.start_time = start_time
        obj.end_time = end_time
        return obj


@dataclass(slots=True)
class AudioFrame:
    data: bytes
    sample_rate: int
    num_channels: int
    samples_per_channel: int
    userdata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.samples_per_channel / self.sample_rate


@dataclass(slots=True)
class SynthesizedAudio:
    frame: AudioFrame
    request_id: str
    segment_id: str = ""
    is_final: bool = False


class _Closed:
    pass


_CLOSED = _Closed()


class Chan[T]:
    """Unbounded async channel; iteration ends once it is closed and drained.

    ``__aiter__`` returns the channel itself, so a loop that breaks out early
    can be resumed by a later ``async for`` over the same channel.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[T | _Closed] = asyncio.Queue()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def send_nowait(self, item: T) -> None:
        if self._closed:
            raise RuntimeError("channel is closed")
        self._queue.put_nowait(item)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(_CLOSED)

    def __aiter__(self) -> Chan[T]:
        return self

    async def __anext__(self) -> T:
        item = await self._queue.get()
        if isinstance(item, _Closed):
            # Leave the marker for any later reader of the drained channel.
            self._queue.put_nowait(item)
            raise StopAsyncIteration
        return item


class AudioEmitter:
    """Turn provider PCM pushes into ``SynthesizedAudio`` events for one attempt."""

    #: Audio held back so the last frame of a segment can be marked final.
    _TAIL_MS = 10

    def __init__(self, dst: Chan[SynthesizedAudio]) -> None:
        self._dst = dst
        self._started = False
        self._request_id = ""
        self._sample_rate = 0
        self._num_channels = 1
        self._frame_bytes = 0
        self._segment_id: str | None = None
        self._buffer = bytearray()
        self._tail: bytes | None = None
        self._timed_transcripts: list[TimedString] = []
        self._segment_durations: list[float] = []

    @property
    def num_segments(self) -> int:
        return len(self._segment_durations)

    def pushed_duration(self, idx: int = -1) -> float:
        """Seconds of audio delivered for segment ``idx`` (0 when there is none)."""

        durations = self._segment_durations
        return durations[idx] if -len(durations) <= idx < len(durations) else 0.0

    def initialize(
        self,
        *,
        request_id: str,
        sample_rate: int,
        num_channels: int,
        mime_type: str,
        frame_size_ms: int = 200,
        stream: bool = False,
    ) -> None:
        if self._started:
            raise RuntimeError("AudioEmitter already started")
        if not mime_type.lower().strip().startswith(("audio/pcm", "audio/raw")):
            raise ValueError("AudioEmitter only carries raw 16-bit PCM")
        if not stream:
            raise ValueError("AudioEmitter only carries streamed segments")
        if not request_id:
            logger.warning("no request_id provided for TTS stream")
            request_id = "unknown"
        self._started = True
        self._request_id = request_id
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._frame_bytes = sample_rate // 1000 * frame_size_ms * num_channels * 2

    def start_segment(self, *, segment_id: str) -> None:
        self._require_started()
        if self._segment_id is not None:
            raise RuntimeError("start_segment() called before the previous segment was ended")
        self._segment_id = segment_id
        self._segment_durations.append(0.0)

    def push(self, data: bytes) -> None:
        self._require_started()
        if self._segment_id is None:
            raise RuntimeError("start_segment() must be called before pushing audio data")
        self._buffer.extend(data)
        while len(self._buffer) >= self._frame_bytes:
            frame = bytes(self._buffer[: self._frame_bytes])
            del self._buffer[: self._frame_bytes]
            self._emit(frame)

    def push_timed_transcript(self, delta_text: TimedString | list[TimedString]) -> None:
        self._require_started()
        if isinstance(delta_text, list):
            self._timed_transcripts.extend(delta_text)
        else:
            self._timed_transcripts.append(delta_text)

    def end_input(self) -> None:
        """End the open segment, if any; the attempt pushes nothing more."""

        self.end_segment()

    def end_segment(self) -> None:
        self._require_started()
        if self._segment_id is None:
            return
        remainder = bytes(self._buffer)
        self._buffer.clear()
        if remainder:
            if len(remainder) % (2 * self._num_channels):
                logger.warning("TTS segment ended with a partial sample; dropping %s bytes", len(remainder))
            else:
                self._emit(remainder)
        if self._tail is not None:
            self._send(self._tail, is_final=True)
            self._tail = None
        self._segment_id = None

    def _emit(self, pcm: bytes) -> None:
        combined = (self._tail or b"") + pcm
        tail_bytes = self._sample_rate * self._TAIL_MS // 1000 * self._num_channels * 2
        if len(combined) <= tail_bytes:
            self._tail = combined
            return
        self._send(combined[:-tail_bytes], is_final=False)
        self._tail = combined[-tail_bytes:]

    def _send(self, pcm: bytes, *, is_final: bool) -> None:
        assert self._segment_id is not None
        frame = AudioFrame(
            data=pcm,
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
            samples_per_channel=len(pcm) // (2 * self._num_channels),
            userdata={USERDATA_TIMED_TRANSCRIPT: self._timed_transcripts},
        )
        self._timed_transcripts = []
        self._dst.send_nowait(
            SynthesizedAudio(
                frame=frame,
                request_id=self._request_id,
                segment_id=self._segment_id,
                is_final=is_final,
            )
        )
        self._segment_durations[-1] += frame.duration

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("AudioEmitter isn't started")


class SynthesizeStream(ABC):
    """One text segment in, provider audio out; see the module docstring."""

    class _FlushSentinel:
        pass

    def __init__(self, *, conn_options: APIConnectOptions) -> None:
        self._conn_options = conn_options
        self._input_ch: Chan[str | SynthesizeStream._FlushSentinel] = Chan()
        self._event_ch: Chan[SynthesizedAudio] = Chan()
        self._input_buffer: list[str | SynthesizeStream._FlushSentinel] = []
        self._input_ended = False
        self._pushed_text = ""
        self._segment_text = ""
        self._num_segments = 0
        self._task = asyncio.create_task(self._main_task(), name="TTS._main_task")
        self._task.add_done_callback(lambda _: self._event_ch.close())

    @abstractmethod
    async def _run(self, output_emitter: AudioEmitter) -> None: ...

    async def _main_task(self) -> None:
        options = self._conn_options
        for attempt in range(options.max_retry + 1):
            emitter = AudioEmitter(self._event_ch)
            try:
                await self._run(emitter)
                emitter.end_input()
                if self._pushed_text.strip():
                    if emitter.pushed_duration() <= 0.0:
                        raise APIError(f"no audio frames were pushed for text: {self._pushed_text}")
                    if self._num_segments != emitter.num_segments:
                        raise APIError(
                            f"number of segments mismatch: expected {self._num_segments}, "
                            f"but got {emitter.num_segments}"
                        )
                return
            except APIError as exc:
                if isinstance(exc, APIStatusError) and exc.status_code == 499:
                    return
                pushed = emitter.pushed_duration()
                if not (exc.retryable and pushed == 0.0 and attempt < options.max_retry):
                    if pushed > 0.0:
                        logger.error(
                            "TTS failed after partial audio was already sent to the user, "
                            "skip retrying. pushed_duration=%s",
                            pushed,
                        )
                    raise
                interval = options.interval_for_retry(attempt)
                logger.warning(
                    "failed to synthesize speech: %s, retrying in %ss attempt=%s",
                    exc,
                    interval,
                    attempt + 1,
                )
                await asyncio.sleep(interval)
                self._input_ch = Chan()
                for item in self._input_buffer:
                    self._input_ch.send_nowait(item)
                if self._input_ended:
                    self._input_ch.close()

    def push_text(self, token: str) -> None:
        if not token or self._input_ch.closed:
            return
        self._pushed_text += token
        if not self._segment_text:
            if self._num_segments >= 1:
                logger.warning("SynthesizeStream carries one segment; dropping text after flush")
                return
            self._num_segments += 1
        self._segment_text += token
        self._input_ch.send_nowait(token)
        self._input_buffer.append(token)

    def flush(self) -> None:
        if self._input_ch.closed:
            return
        self._segment_text = ""
        sentinel = self._FlushSentinel()
        self._input_ch.send_nowait(sentinel)
        self._input_buffer.append(sentinel)

    def end_input(self) -> None:
        self.flush()
        self._input_ch.close()
        self._input_ended = True

    async def aclose(self) -> None:
        # Wait for the cancelled attempt without re-raising its failure: a
        # consumer closing after an error already received it while iterating.
        self._task.cancel()
        await asyncio.wait((self._task,))
        self._event_ch.close()
        self._input_ch.close()

    def __aiter__(self) -> AsyncIterator[SynthesizedAudio]:
        return self

    async def __anext__(self) -> SynthesizedAudio:
        try:
            return await self._event_ch.__anext__()
        except StopAsyncIteration:
            if not self._task.cancelled() and (exc := self._task.exception()):
                raise exc  # noqa: B904 - keep the provider's own cause chain
            raise

    async def __aenter__(self) -> SynthesizeStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


__all__ = [
    "USERDATA_TIMED_TRANSCRIPT",
    "AudioEmitter",
    "AudioFrame",
    "Chan",
    "SynthesizeStream",
    "SynthesizedAudio",
    "TimedString",
]
