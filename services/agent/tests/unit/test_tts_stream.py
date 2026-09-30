"""The streaming TTS base the Doubao and CosyVoice adapters run on."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from services.agent.src.providers.provider_errors import (
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
)
from services.agent.src.providers.tts_stream import (
    USERDATA_TIMED_TRANSCRIPT,
    AudioEmitter,
    SynthesizeStream,
    TimedString,
)

RATE = 24_000
FRAME = RATE // 1000 * 20 * 2  # 20 ms of 16-bit mono
TAIL = RATE // 1000 * 10 * 2  # 10 ms held back for the final frame

Attempt = Callable[["ScriptedStream", AudioEmitter, int], Coroutine[Any, Any, None]]


def ramp(samples: int, start: int = 0) -> bytes:
    return struct.pack(f"<{samples}h", *(((start + i) * 7) % 20000 for i in range(samples)))


class ScriptedStream(SynthesizeStream):
    """Each ``_run`` attempt is handed to ``attempt`` with its index."""

    def __init__(self, attempt: Attempt, *, max_retry: int = 0) -> None:
        self._attempt_fn = attempt
        self.attempts = 0
        self.received: list[list[str]] = []
        super().__init__(conn_options=APIConnectOptions(max_retry=max_retry, retry_interval=0.0))

    async def _run(self, output_emitter: AudioEmitter) -> None:
        index = self.attempts
        self.attempts += 1
        await self._attempt_fn(self, output_emitter, index)

    async def read_text(self) -> list[str]:
        texts = [str(item) async for item in self._input_ch if isinstance(item, str)]
        self.received.append(texts)
        return texts


def start(emitter: AudioEmitter, segment: str = "seg") -> None:
    emitter.initialize(
        request_id="req",
        sample_rate=RATE,
        num_channels=1,
        mime_type="audio/pcm",
        frame_size_ms=20,
        stream=True,
    )
    emitter.start_segment(segment_id=segment)


async def drain(stream: SynthesizeStream) -> list[Any]:
    return [event async for event in stream]


async def test_pcm_is_cut_into_frames_in_order_with_a_final_tail() -> None:
    pcm = ramp(1300)  # 2600 bytes: two whole frames, one partial

    async def attempt(stream: ScriptedStream, emitter: AudioEmitter, _index: int) -> None:
        await stream.read_text()
        start(emitter)
        emitter.push(pcm[:1000])
        emitter.push(pcm[1000:])
        emitter.end_segment()

    stream = ScriptedStream(attempt)
    stream.push_text("你好")
    stream.end_input()
    async with stream:
        events = await drain(stream)

    assert b"".join(event.frame.data for event in events) == pcm
    assert [len(event.frame.data) for event in events] == [FRAME - TAIL, FRAME, 680, TAIL]
    assert [event.is_final for event in events] == [False, False, False, True]
    assert all(event.request_id == "req" and event.segment_id == "seg" for event in events)
    assert sum(event.frame.duration for event in events) == pytest.approx(1300 / RATE)


async def test_timed_transcript_rides_on_the_next_frame_sent() -> None:
    words = [TimedString("你", start_time=0.0, end_time=0.01), TimedString("好", start_time=0.01, end_time=0.02)]

    async def attempt(stream: ScriptedStream, emitter: AudioEmitter, _index: int) -> None:
        await stream.read_text()
        start(emitter)
        emitter.push(ramp(960))
        emitter.push_timed_transcript(words)
        emitter.end_segment()

    stream = ScriptedStream(attempt)
    stream.push_text("你好")
    stream.end_input()
    async with stream:
        events = await drain(stream)

    carried = [event.frame.userdata[USERDATA_TIMED_TRANSCRIPT] for event in events]
    assert carried[:-1] == [[]] * (len(events) - 1)
    assert carried[-1] == words
    assert (str(words[0]), words[0].start_time, words[0].end_time) == ("你", 0.0, 0.01)


async def test_a_trailing_partial_sample_is_dropped() -> None:
    async def attempt(stream: ScriptedStream, emitter: AudioEmitter, _index: int) -> None:
        await stream.read_text()
        start(emitter)
        emitter.push(ramp(600) + b"\x01")
        emitter.end_segment()

    stream = ScriptedStream(attempt)
    stream.push_text("你好")
    stream.end_input()
    async with stream:
        events = await drain(stream)

    assert b"".join(event.frame.data for event in events) == ramp(480)


async def test_failure_before_audio_retries_with_the_text_replayed() -> None:
    async def attempt(stream: ScriptedStream, emitter: AudioEmitter, index: int) -> None:
        texts = await stream.read_text()
        if index == 0:
            raise APIConnectionError("first-audio-timeout")
        assert texts == ["你", "好"]
        start(emitter)
        emitter.push(ramp(960))
        emitter.end_segment()

    stream = ScriptedStream(attempt, max_retry=1)
    stream.push_text("你")
    stream.push_text("好")
    stream.end_input()
    async with stream:
        events = await drain(stream)

    assert stream.attempts == 2
    assert stream.received == [["你", "好"], ["你", "好"]]
    assert b"".join(event.frame.data for event in events) == ramp(960)


async def test_failure_after_audio_is_final_and_follows_the_delivered_audio() -> None:
    async def attempt(stream: ScriptedStream, emitter: AudioEmitter, _index: int) -> None:
        await stream.read_text()
        start(emitter)
        emitter.push(ramp(960))
        raise APIConnectionError("total-timeout")

    stream = ScriptedStream(attempt, max_retry=3)
    stream.push_text("你好")
    stream.end_input()
    received: list[Any] = []
    async with stream:
        with pytest.raises(APIConnectionError, match="total-timeout"):
            async for event in stream:
                received.append(event)

    assert stream.attempts == 1
    assert received and b"".join(event.frame.data for event in received) == ramp(960)[: len(ramp(960)) - TAIL]


async def test_non_retryable_and_non_api_errors_are_final() -> None:
    async def not_retryable(stream: ScriptedStream, _emitter: AudioEmitter, _index: int) -> None:
        await stream.read_text()
        raise APIConnectionError("baseline failed", retryable=False)

    async def not_api(stream: ScriptedStream, _emitter: AudioEmitter, _index: int) -> None:
        await stream.read_text()
        raise ValueError("odd PCM")

    for attempt, expected in ((not_retryable, APIConnectionError), (not_api, ValueError)):
        stream = ScriptedStream(attempt, max_retry=3)
        stream.push_text("你好")
        stream.end_input()
        async with stream:
            with pytest.raises(expected):
                await drain(stream)
        assert stream.attempts == 1


async def test_status_499_ends_quietly_and_a_silent_success_is_retried() -> None:
    async def closed(stream: ScriptedStream, _emitter: AudioEmitter, _index: int) -> None:
        await stream.read_text()
        raise APIStatusError("client closed", status_code=499)

    stream = ScriptedStream(closed, max_retry=3)
    stream.push_text("你好")
    stream.end_input()
    async with stream:
        assert await drain(stream) == []
    assert stream.attempts == 1

    async def silent(stream: ScriptedStream, emitter: AudioEmitter, index: int) -> None:
        await stream.read_text()
        start(emitter)
        if index:
            emitter.push(ramp(480))
        emitter.end_segment()

    stream = ScriptedStream(silent, max_retry=1)
    stream.push_text("你好")
    stream.end_input()
    async with stream:
        events = await drain(stream)
    assert stream.attempts == 2
    assert b"".join(event.frame.data for event in events) == ramp(480)


async def test_one_segment_per_stream_and_no_text_after_end_input() -> None:
    async def attempt(stream: ScriptedStream, emitter: AudioEmitter, _index: int) -> None:
        await stream.read_text()
        start(emitter)
        emitter.push(ramp(480))
        emitter.end_segment()

    stream = ScriptedStream(attempt)
    stream.push_text("第一段")
    stream.flush()
    stream.push_text("第二段")
    stream.end_input()
    stream.push_text("结束后")
    async with stream:
        await drain(stream)

    assert stream.received == [["第一段"]]


async def test_run_starts_before_any_text_and_close_cancels_it() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def attempt(stream: ScriptedStream, _emitter: AudioEmitter, _index: int) -> None:
        started.set()
        try:
            await stream.read_text()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    stream = ScriptedStream(attempt)
    await asyncio.wait_for(started.wait(), timeout=1)
    await stream.aclose()
    assert cancelled.is_set()


async def test_close_after_a_failure_does_not_raise_again() -> None:
    async def attempt(stream: ScriptedStream, _emitter: AudioEmitter, _index: int) -> None:
        await stream.read_text()
        raise ValueError("boom")

    stream = ScriptedStream(attempt)
    stream.push_text("你好")
    stream.end_input()
    with pytest.raises(ValueError):
        await drain(stream)
    await stream.aclose()


def test_emitter_rejects_misuse() -> None:
    emitter = AudioEmitter(dst=None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        emitter.push(b"\x00\x00")
    with pytest.raises(ValueError):
        emitter.initialize(request_id="r", sample_rate=RATE, num_channels=1, mime_type="audio/mpeg", stream=True)
    emitter.initialize(request_id="r", sample_rate=RATE, num_channels=1, mime_type="audio/pcm", stream=True)
    with pytest.raises(RuntimeError):
        emitter.push(b"\x00\x00")
    with pytest.raises(RuntimeError):
        emitter.initialize(request_id="r", sample_rate=RATE, num_channels=1, mime_type="audio/pcm", stream=True)
