from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.handlers import (
    LanguageModelRequest,
    SpeechSynthesisRequest,
)
from services.agent.src.providers.funasr_protocol import (
    FunASRSentence,
    FunASRServerEvent,
)
from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter


class FakeASR:
    def __init__(self) -> None:
        self.task_epoch = 1
        self.task_sample_origin = 0
        self.events: asyncio.Queue[FunASRServerEvent] = asyncio.Queue()
        self.sent: list[tuple[bytes, int]] = []
        self.closed = False

    async def connect(self) -> None:
        return None

    async def reconnect_with_replay(self) -> None:
        self.task_epoch += 1
        self.task_sample_origin = 320

    async def send_pcm(self, pcm: bytes, *, capture_start_sample: int) -> None:
        self.sent.append((pcm, capture_start_sample))
        if len(self.sent) == 1:
            await self.events.put(
                FunASRServerEvent(
                    event="result-generated",
                    task_id="task-1",
                    sentence=FunASRSentence(
                        sentence_id=7,
                        text="你好",
                        begin_ms=0,
                        end_ms=20,
                        sentence_end=True,
                        heartbeat=False,
                        words=(),
                    ),
                )
            )

    async def aclose(self) -> None:
        self.closed = True


class FakeLLM:
    def stream(self, request: LanguageModelRequest) -> AsyncIterator[str]:
        _ = request

        async def tokens() -> AsyncIterator[str]:
            yield "你好。"

        return tokens()


class FakeTTS:
    async def synthesize(self, request: SpeechSynthesisRequest) -> Any:
        assert request.phrases
        return SimpleNamespace(pcm=b"\x00\x00" * 8)


class SizedTTS:
    def __init__(self, samples: int) -> None:
        self.samples = samples

    async def synthesize(self, request: SpeechSynthesisRequest) -> Any:
        assert request.phrases
        return SimpleNamespace(pcm=b"\x01\x00" * self.samples)


@pytest.mark.asyncio
async def test_existing_provider_adapter_maps_asr_and_streams_existing_handlers() -> None:
    asr = FakeASR()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: asr),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, FakeTTS()),
    )
    identity = SessionIdentity("adapter-session", stream_epoch=1)
    results = await adapter.ingest_audio(
        identity,
        AudioFrame(
            identity=identity,
            sequence=0,
            capture_start_sample=0,
            frame_samples=320,
            payload=b"\x00\x00" * 320,
        ),
    )
    assert len(results) == 1
    assert results[0].text == "你好"
    assert results[0].capture_end_sample == 320
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    chunks = [chunk async for chunk in adapter.generate_reply(identity, "hi", fence)]
    assert len(chunks) == 1
    assert chunks[0].text == "你好。"
    assert chunks[0].source_start_sample == 0
    assert chunks[0].frame_samples == 480
    assert chunks[0].first is True
    assert chunks[0].final is True
    assert chunks[0].text_audio_start_sample == 0
    assert chunks[0].text_audio_end_sample == 480
    await adapter.close(identity)
    assert asr.closed


@pytest.mark.asyncio
async def test_existing_provider_adapter_splits_pcm_and_deduplicates_generation() -> None:
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, SizedTTS(481)),
    )
    identity = SessionIdentity("framed-adapter", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)

    chunks = [chunk async for chunk in adapter.generate_reply(identity, "hi", fence)]
    assert [chunk.source_start_sample for chunk in chunks] == [0, 480]
    assert [chunk.frame_samples for chunk in chunks] == [480, 480]
    assert chunks[0].first is True
    assert chunks[0].final is False
    assert chunks[0].text == "你好。"
    assert chunks[0].text_audio_start_sample == 0
    assert chunks[0].text_audio_end_sample == 960
    assert chunks[1].text == ""
    assert chunks[1].final is True

    # Replaying the same authoritative fence must not duplicate sequence or
    # sample ranges.  A new fence gets a fresh local sample clock.
    assert [chunk async for chunk in adapter.generate_reply(identity, "hi", fence)] == []
    next_fence = GenerationFence(identity.session_id, 2, 2, 0)
    next_chunks = [
        chunk async for chunk in adapter.generate_reply(identity, "hi", next_fence)
    ]
    assert next_chunks[0].source_start_sample == 0


@pytest.mark.asyncio
async def test_existing_provider_adapter_cancellation_stops_pending_frames() -> None:
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: FakeASR()),
        language_model=cast(Any, FakeLLM()),
        speech_synthesis=cast(Any, SizedTTS(1_000)),
    )
    identity = SessionIdentity("cancel-adapter", stream_epoch=1)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    stream = adapter.generate_reply(identity, "hi", fence)
    first = await anext(stream)
    assert first.first is True
    assert adapter.cancel_generation(fence)
    assert [chunk async for chunk in stream] == []

    pre_start_fence = GenerationFence(identity.session_id, 2, 2, 0)
    pre_start_stream = adapter.generate_reply(identity, "hi", pre_start_fence)
    assert adapter.cancel_generation(pre_start_fence)
    assert [chunk async for chunk in pre_start_stream] == []
