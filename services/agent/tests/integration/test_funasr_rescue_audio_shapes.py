"""Rescue gating and accounting across PCM shapes, concurrency and latency.

The fixtures here are deterministic *synthetic* waveforms, not recorded human
speech: they exist to pin the decision boundaries the rescue path owns — total
silence, low-RMS room floor, clipping, an over-long segment whose tail is kept,
concurrent rescues, and the rescue's own time budget. Provider transcription
quality is a different question and needs real recordings.
"""

from __future__ import annotations

import asyncio
import math
import struct
from collections.abc import Iterator
from typing import Any

import pytest
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession
from services.agent.src.providers.sensevoice import SenseVoiceRescueConfig
from services.agent.tests.integration.mock_servers import (
    MockFunASRServer,
    MockSenseVoiceServer,
)


def _silence(samples: int) -> bytes:
    return b"\x00\x00" * samples


def _tone(samples: int, *, amplitude: int, frequency: float = 220.0, rate: int = 16000) -> bytes:
    """A speech-shaped AC signal: real RMS and real zero crossings."""

    frames = bytearray()
    for index in range(samples):
        value = int(amplitude * math.sin(2 * math.pi * frequency * index / rate))
        frames += struct.pack("<h", value)
    return bytes(frames)


def _room_floor(samples: int) -> bytes:
    """Very low amplitude noise floor: above zero, far below any speech gate."""

    return _tone(samples, amplitude=8)


def _clipped(samples: int) -> bytes:
    """Saturated square wave: full-scale peak, the worst-case uplink gain."""

    return b"".join(struct.pack("<h", 32767 if index % 2 else -32768) for index in range(samples))


@pytest.fixture
def silent_funasr() -> Iterator[MockFunASRServer]:
    server = MockFunASRServer(scenario="silent")
    server.start()
    yield server
    server.stop()


@pytest.fixture
def rescue_server() -> Iterator[MockSenseVoiceServer]:
    server = MockSenseVoiceServer(scenario="happy")
    server.start()
    yield server
    server.stop()


def _buckets(metrics: MetricsRegistry) -> dict[str, float]:
    snapshot = metrics.snapshot()
    return {
        key: value
        for key, value in snapshot.items()
        if key.startswith("funasr_rescue_total") or key.startswith("funasr_empty_transcript_total")
    }


async def _run_segment(
    *,
    funasr_url: str,
    rescue_url: str,
    pcm: bytes,
    metrics: MetricsRegistry,
    timeout_s: float = 2.5,
    max_audio_s: float = 30.0,
) -> FunASRSession:
    session = FunASRSession(
        FunASRConfig(
            api_key="test",
            ws_url=funasr_url,
            rescue_config=SenseVoiceRescueConfig(
                endpoint=rescue_url,
                timeout_s=timeout_s,
                max_audio_s=max_audio_s,
            ),
        ),
        metrics=metrics,
    )
    await session.connect()
    await session.send_pcm(pcm)
    await session.rotate_task(require_consumed=False)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "pcm"),
    [("digital_silence", _silence(8000)), ("room_floor", _room_floor(8000))],
)
async def test_quiet_segments_are_gated_locally_and_reported_as_low_rms(
    silent_funasr: MockFunASRServer,
    rescue_server: MockSenseVoiceServer,
    name: str,
    pcm: bytes,
) -> None:
    """Nothing above the speech gate must never reach the offline vendor."""

    metrics = MetricsRegistry()
    session = await _run_segment(
        funasr_url=silent_funasr.ws_url,
        rescue_url=rescue_server.url,
        pcm=pcm,
        metrics=metrics,
    )
    try:
        await asyncio.sleep(0.05)
        assert rescue_server.requests == 0, f"{name} must be gated before the vendor call"
        buckets = _buckets(metrics)
        assert buckets.get('funasr_rescue_total{outcome="skipped"}') == 1
        assert buckets.get('funasr_empty_transcript_total{class="low_rms"}') == 1
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_clipped_segment_is_rescued_with_the_exact_uplink_bytes(
    silent_funasr: MockFunASRServer,
    rescue_server: MockSenseVoiceServer,
) -> None:
    """A saturated signal is speech-loud; the vendor gets it byte for byte."""

    pcm = _clipped(8000)
    metrics = MetricsRegistry()
    session = await _run_segment(
        funasr_url=silent_funasr.ws_url,
        rescue_url=rescue_server.url,
        pcm=pcm,
        metrics=metrics,
    )
    try:
        for _ in range(50):
            if rescue_server.requests:
                break
            await asyncio.sleep(0.02)
        assert rescue_server.requests == 1
        assert rescue_server.received_bytes == len(pcm)
        assert rescue_server.formats == ["pcm"]
        assert rescue_server.sample_rates == ["16000"]
        assert _buckets(metrics).get('funasr_rescue_total{outcome="rescued"}') == 1
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_over_long_segment_rescues_only_its_tail(
    silent_funasr: MockFunASRServer,
    rescue_server: MockSenseVoiceServer,
) -> None:
    """The audio cap keeps the newest frames, which is what carries the tail word.

    The cap drops whole frames, so the assertion is content-exact: the rescue
    must send the newest frames and nothing older.
    """

    metrics = MetricsRegistry()
    session = FunASRSession(
        FunASRConfig(
            api_key="test",
            ws_url=silent_funasr.ws_url,
            rescue_config=SenseVoiceRescueConfig(
                endpoint=rescue_server.url,
                max_audio_s=0.25,
            ),
        ),
        metrics=metrics,
    )
    frames = [_tone(2000, amplitude=1000 * (index + 1)) for index in range(4)]
    sent: list[bytes] = []
    original = session._rescue.transcribe

    async def capture(pcm: bytes, **kwargs: Any) -> str | None:
        sent.append(pcm)
        return await original(pcm, **kwargs)

    session._rescue.transcribe = capture  # type: ignore[method-assign]
    try:
        await session.connect()
        for pcm in frames:
            await session.send_pcm(pcm)
        await session.rotate_task(require_consumed=False)
        for _ in range(200):
            if sent:
                break
            await asyncio.sleep(0.02)
        assert sent, "the rescue must run for a loud over-long segment"
        # 0.25s at 16kHz is the cap; the two newest frames fit exactly.
        assert sent[0] == frames[2] + frames[3]
        assert len(sent[0]) // 2 == int(0.25 * 16000)
        assert rescue_server.received_bytes == len(sent[0])
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_concurrent_rescues_degrade_without_raising_or_blocking(
    silent_funasr: MockFunASRServer,
) -> None:
    """A slow or failing vendor degrades to "no text", never to an exception."""

    slow = MockSenseVoiceServer(scenario="happy", delay_s=0.3)
    slow.start()
    try:
        metrics = MetricsRegistry()
        sessions = [
            await _run_segment(
                funasr_url=silent_funasr.ws_url,
                rescue_url=slow.url,
                pcm=_tone(8000, amplitude=2000),
                metrics=metrics,
                timeout_s=0.1,
            )
            for _ in range(3)
        ]
        try:
            await asyncio.gather(*(session.aclose() for session in sessions))
        finally:
            for session in sessions:
                await session.aclose()
        assert slow.requests >= 1
        # Every rescue ended as a bounded no-text outcome; none of them raised
        # out of the rescue path or left the realtime session wedged.
        buckets = _buckets(metrics)
        assert buckets.get('funasr_rescue_total{outcome="no_text"}') == 3
        assert all(session.pending_rescue_deadline() is None for session in sessions)
    finally:
        slow.stop()


@pytest.mark.asyncio
async def test_rescue_runs_inside_its_own_two_and_a_half_second_budget(
    silent_funasr: MockFunASRServer,
    rescue_server: MockSenseVoiceServer,
) -> None:
    """The in-flight rescue publishes the budget it will actually wait for."""

    metrics = MetricsRegistry()
    session = FunASRSession(
        FunASRConfig(
            api_key="test",
            ws_url=silent_funasr.ws_url,
            rescue_config=SenseVoiceRescueConfig(endpoint=rescue_server.url),
        ),
        metrics=metrics,
    )
    seen: list[tuple[float, float]] = []
    original = session._rescue.transcribe

    async def observe(*args: Any, **kwargs: Any) -> str | None:
        loop = asyncio.get_running_loop()
        seen.append(
            (
                loop.time(),
                session.pending_rescue_deadline()
                if session.pending_rescue_deadline() is not None
                else float("nan"),
            )
        )
        return await original(*args, **kwargs)

    session._rescue.transcribe = observe  # type: ignore[method-assign]
    try:
        await session.connect()
        await session.send_pcm(_tone(8000, amplitude=2000))
        await session.rotate_task(require_consumed=False)
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.02)
        assert seen, "the rescue call must have been issued"
        issued_at, deadline = seen[0]
        assert deadline == pytest.approx(issued_at + 2.5, abs=0.05)
    finally:
        await session.aclose()
