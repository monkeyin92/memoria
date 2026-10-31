"""A slow reply reaches the device on its own, with no thinking line before it."""

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
from services.agent.src.voice_core.media_session import MediaReplyChunk, MediaVoiceCoreRegistry
from services.agent.src.voice_core.speech_timeline import ASRResult, SegmentKind, SpeechSegment
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _device_identity,
    _FenceCheckingBridge,
    _wait_until,
)

_REPLY_PCM = b"\x07\x00\x08\x00"
_REPLY_TEXT = "我是 AI，你最喜欢什么颜色呀？"
_UPLINK_SAMPLE_RATE = 16_000
_VOICED_END_SAMPLE = 320


class _SlowReplyProvider(FakeMediaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.output_texts: list[str] = []
        self.output_frame_samples = 2
        self.release_reply = asyncio.Event()
        self.reply_started = asyncio.Event()

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
        text = str(getattr(intent, "tts_source", "") or "")
        self.output_texts.append(text)

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=b"\x02\x00\x03\x00",
                source_start_sample=source_start_sample,
                text=text,
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
            await self.release_reply.wait()
            yield MediaReplyChunk(
                pcm_s16le=_REPLY_PCM,
                source_start_sample=0,
                text=_REPLY_TEXT,
                first=True,
                final=True,
            )

        return chunks()


async def _open_device_session(
    session_id: str,
) -> tuple[MediaVoiceCoreRegistry, _SlowReplyProvider, _FenceCheckingBridge, Any, Any]:
    provider = _SlowReplyProvider()
    bridge = _FenceCheckingBridge()
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
    return registry, provider, bridge, session, context


async def _commit_turn_spoken_seconds_ago(
    registry: MediaVoiceCoreRegistry, session: Any, context: Any, *, seconds_ago: float
) -> None:
    """The device reports VAD end ``seconds_ago`` after the child's last voiced sample."""

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
                voiced_end_sample=_VOICED_END_SAMPLE if final else None,
            ),
        )

    await vad("start", 0, final=False)
    final = ASRResult(
        1,
        "turn-final",
        1,
        0,
        _VOICED_END_SAMPLE,
        "你是谁",
        True,
        stream_epoch=identity.stream_epoch,
    )
    assert await registry.accept_asr_result(identity.session_id, final)
    registry._observe_final_asr_result(context, final)
    vad_end = _VOICED_END_SAMPLE + int(seconds_ago * _UPLINK_SAMPLE_RATE)
    await vad("end", vad_end, final=True)


@pytest.mark.asyncio
async def test_reply_later_than_three_seconds_after_speech_end_is_spoken_alone() -> None:
    registry, provider, bridge, session, context = await _open_device_session("slow-reply-alone")
    try:
        await _commit_turn_spoken_seconds_ago(registry, session, context, seconds_ago=3.0)
        await asyncio.wait_for(provider.reply_started.wait(), timeout=2.0)
        await asyncio.sleep(0.3)
        provider.release_reply.set()
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)

        assert provider.output_texts == []
        first = bridge.frames[0]
        assert (first.pcm_s16le, first.source_start_sample, first.first, first.final) == (
            _REPLY_PCM,
            0,
            True,
            True,
        )
        await _wait_until(lambda: context.output.assistant_text == _REPLY_TEXT, timeout=2.0)
        assert len(bridge.frames) == 1
    finally:
        provider.release_reply.set()
        await registry.finalize_session(context.identity.session_id)
