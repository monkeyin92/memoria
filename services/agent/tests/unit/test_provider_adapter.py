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
    await adapter.close(identity)
    assert asr.closed
