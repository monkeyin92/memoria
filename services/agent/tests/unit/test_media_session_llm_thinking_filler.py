"""The llm_thinking filler covers a slow reply without replacing it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.media_protocol import (
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _CapturingGenerationBridge,
    _device_identity,
    _wait_until,
)

_FILLER_PCM = b"\x02\x00\x03\x00"
_REPLY_PCM = b"\x07\x00\x08\x00"
_REPLY_TEXT = "我是 AI，你最喜欢什么颜色呀？"


class _HeldReplyProvider(FakeMediaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.output_texts: list[str] = []
        self.release_reply = asyncio.Event()
        self.reply_started = asyncio.Event()
        self.reply_closed = asyncio.Event()

    def generate_output(
        self,
        _identity: SessionIdentity,
        intent: Any,
        _fence: GenerationFence,
        *,
        work_id: str,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        _ = work_id
        self.output_texts.append(str(intent.tts_source))

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=_FILLER_PCM,
                source_start_sample=source_start_sample,
                text=str(intent.tts_source),
                first=True,
                final=True,
            )

        return chunks()

    def generate_reply(
        self,
        _identity: SessionIdentity,
        _user_text: str,
        _fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            self.reply_started.set()
            try:
                await self.release_reply.wait()
                yield MediaReplyChunk(
                    pcm_s16le=_REPLY_PCM,
                    source_start_sample=0,
                    text=_REPLY_TEXT,
                    first=True,
                    final=True,
                )
            finally:
                self.reply_closed.set()

        return chunks()


async def _open_device_session(
    session_id: str,
) -> tuple[MediaVoiceCoreRegistry, _HeldReplyProvider, _CapturingGenerationBridge, Any, Any]:
    provider = _HeldReplyProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = _device_identity(session_id)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
    wake = bridge.frames[-1]
    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=wake.generation_id,
            received_sequence=wake.sequence,
            rendered_sample_end=wake.source_start_sample + wake.frame_samples,
            client_monotonic_ms=1,
            turn_id=wake.turn_id,
            tool_epoch=wake.tool_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )
    bridge.frames.clear()
    provider.output_texts.clear()
    context.llm_thinking_filler.delay_s = 0.05
    return registry, provider, bridge, session, context


async def _commit_spoken_turn(registry: MediaVoiceCoreRegistry, session: Any, context: Any) -> None:
    identity = context.identity

    async def vad(segment_id: str, sample: int, *, final: bool) -> None:
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=0,
                segment_id=segment_id,
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=sample,
                capture_end_sample=sample + 1,
                final=final,
                voiced_end_sample=sample if final else None,
            ),
        )

    await vad("start", 0, final=False)
    final = ASRResult(
        1, "turn-final", 1, 0, 320, "你喜欢什么颜色", True, stream_epoch=identity.stream_epoch
    )
    assert await registry.accept_asr_result(identity.session_id, final)
    registry._observe_final_asr_result(context, final)
    await vad("end", 320, final=True)


def _wire(frames: list[Any]) -> list[tuple[bytes, int, bool, bool]]:
    return [(f.pcm_s16le, f.source_start_sample, f.first, f.final) for f in frames]


@pytest.mark.asyncio
async def test_slow_reply_plays_filler_then_the_reply_in_one_generation() -> None:
    registry, provider, bridge, session, context = await _open_device_session(
        "llm-thinking-slow-reply"
    )
    try:
        await _commit_spoken_turn(registry, session, context)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        provider.release_reply.set()
        await _wait_until(lambda: len(bridge.frames) == 2, timeout=2.0)

        assert provider.output_texts == ["嗯，我想一想。"]
        assert _wire(bridge.frames) == [
            (_FILLER_PCM, 0, True, False),
            (_REPLY_PCM, 2, False, True),
        ]
        assert {frame.generation_id for frame in bridge.frames} == {
            context.runtime.fence.generation_id
        }
        assert context.output.assistant_text == _REPLY_TEXT
    finally:
        provider.release_reply.set()
        await registry.finalize_session(context.identity.session_id)


@pytest.mark.asyncio
async def test_reply_that_starts_before_the_delay_plays_no_filler() -> None:
    registry, provider, bridge, session, context = await _open_device_session(
        "llm-thinking-fast-reply"
    )
    try:
        provider.release_reply.set()
        await _commit_spoken_turn(registry, session, context)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        await asyncio.sleep(0.15)

        assert provider.output_texts == []
        assert _wire(bridge.frames) == [(_REPLY_PCM, 0, True, True)]
    finally:
        await registry.finalize_session(context.identity.session_id)


@pytest.mark.asyncio
async def test_stop_while_the_reply_is_silent_closes_it_and_plays_no_filler() -> None:
    registry, provider, bridge, session, context = await _open_device_session("llm-thinking-stop")
    context.llm_thinking_filler.delay_s = 0.3
    try:
        await _commit_spoken_turn(registry, session, context)
        await asyncio.wait_for(provider.reply_started.wait(), timeout=2.0)
        reply_task = context.output.reply_task
        await registry._cancel_reply_task(context, context.runtime.fence)  # noqa: SLF001
        await asyncio.sleep(0.4)

        assert provider.reply_closed.is_set()
        assert reply_task is not None and reply_task.done()
        assert provider.output_texts == []
        assert bridge.frames == []
    finally:
        await registry.finalize_session(context.identity.session_id)
