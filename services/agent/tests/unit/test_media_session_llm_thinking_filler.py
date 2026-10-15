"""The llm_thinking filler covers a slow reply without replacing it."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.voice_core.media_protocol import (
    MediaEnvelope,
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaTextSpan,
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _device_identity,
    _FenceCheckingBridge,
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
        self.reply_ready = asyncio.Event()
        self.reply_closed = asyncio.Event()
        self.filler_rendered = asyncio.Event()
        self.filler_rendered.set()
        self.filler_closed = asyncio.Event()

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
            try:
                await self.filler_rendered.wait()
                yield MediaReplyChunk(
                    pcm_s16le=_FILLER_PCM,
                    source_start_sample=source_start_sample,
                    text=str(intent.tts_source),
                    first=True,
                    final=True,
                )
            finally:
                self.filler_closed.set()

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
                self.reply_ready.set()
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


class _TimedReplyProvider(_HeldReplyProvider):
    """Streams the reply like live TTS: text with the first frame, timing only once stopped."""

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
                    assistant_text_delta=_REPLY_TEXT,
                    first=True,
                )
                await asyncio.Event().wait()
            finally:
                self.reply_closed.set()

        return chunks()

    async def interrupted_timed_text_spans(
        self, _fence: GenerationFence
    ) -> tuple[MediaTextSpan, ...]:
        return (MediaTextSpan(_REPLY_TEXT, 0, 2),)


async def _open_device_session(
    session_id: str,
    provider: _HeldReplyProvider | None = None,
    **registry_options: Any,
) -> tuple[MediaVoiceCoreRegistry, _HeldReplyProvider, _FenceCheckingBridge, Any, Any]:
    provider = provider or _HeldReplyProvider()
    bridge = _FenceCheckingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
        **registry_options,
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


async def _commit_spoken_turn(
    registry: MediaVoiceCoreRegistry,
    session: Any,
    context: Any,
    *,
    vad_end_sample: int = 320,
) -> None:
    """The child speaks samples 0-320; the device reports VAD end at ``vad_end_sample``."""

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
                voiced_end_sample=320 if final else None,
            ),
        )

    await vad("start", 0, final=False)
    final = ASRResult(
        1, "turn-final", 1, 0, 320, "你喜欢什么颜色", True, stream_epoch=identity.stream_epoch
    )
    assert await registry.accept_asr_result(identity.session_id, final)
    registry._observe_final_asr_result(context, final)
    await vad("end", vad_end_sample, final=True)


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
async def test_latency_metrics_time_the_reply_not_the_filler() -> None:
    metrics = MetricsRegistry()
    registry, provider, bridge, session, context = await _open_device_session(
        "llm-thinking-latency", metrics=metrics
    )
    stages = ("first_audio", "tts_first_frame")
    before = {stage: len(metrics.latency_samples[stage]) for stage in stages}
    try:
        await _commit_spoken_turn(registry, session, context)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        during_filler = {stage: metrics.latency_samples[stage][before[stage] :] for stage in stages}
        released_ns = time.monotonic_ns()
        provider.release_reply.set()
        await _wait_until(lambda: len(bridge.frames) == 2, timeout=2.0)

        assert during_filler == {"first_audio": [], "tts_first_frame": []}
        (first_audio,) = metrics.latency_samples["first_audio"][before["first_audio"] :]
        assert context.turn_started_ns is not None
        assert first_audio >= (released_ns - context.turn_started_ns) / 1_000_000_000
        assert len(metrics.latency_samples["tts_first_frame"][before["tts_first_frame"] :]) == 1
    finally:
        provider.release_reply.set()
        await registry.finalize_session(context.identity.session_id)


@pytest.mark.asyncio
async def test_the_delay_counts_from_the_last_voiced_sample_not_the_vad_end() -> None:
    registry, provider, _bridge, session, context = await _open_device_session(
        "llm-thinking-voiced-end"
    )
    try:
        sent_at = time.monotonic()
        await _commit_spoken_turn(registry, session, context, vad_end_sample=320 + 8_000)
        handled_by = time.monotonic()
        await asyncio.wait_for(provider.reply_started.wait(), timeout=2.0)

        hangover_s = 8_000 / 16_000
        assert sent_at - hangover_s <= context.llm_thinking_filler.armed_at
        assert context.llm_thinking_filler.armed_at <= handled_by - hangover_s
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
async def test_a_reply_ready_before_the_filler_renders_plays_without_it() -> None:
    registry, provider, bridge, session, context = await _open_device_session(
        "llm-thinking-reply-beats-render"
    )
    provider.filler_rendered.clear()
    try:
        await _commit_spoken_turn(registry, session, context)
        await _wait_until(lambda: provider.output_texts == ["嗯，我想一想。"], timeout=2.0)
        provider.release_reply.set()
        await asyncio.wait_for(provider.reply_ready.wait(), timeout=2.0)
        provider.filler_rendered.set()
        await _wait_until(
            lambda: any(frame.pcm_s16le == _REPLY_PCM for frame in bridge.frames), timeout=2.0
        )

        assert _wire(bridge.frames) == [(_REPLY_PCM, 0, True, True)]
        assert provider.filler_closed.is_set()
    finally:
        provider.release_reply.set()
        provider.filler_rendered.set()
        await registry.finalize_session(context.identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(("rendered_sample_end", "heard"), [(2, ""), (4, _REPLY_TEXT)])
async def test_a_stop_after_the_filler_counts_only_reply_audio_the_child_heard(
    rendered_sample_end: int, heard: str
) -> None:
    registry, provider, bridge, session, context = await _open_device_session(
        f"llm-thinking-stop-at-{rendered_sample_end}", _TimedReplyProvider()
    )
    try:
        await _commit_spoken_turn(registry, session, context)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        provider.release_reply.set()
        await _wait_until(lambda: len(bridge.frames) == 2, timeout=2.0)
        fence = context.runtime.fence
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=context.identity,
                generation_id=fence.generation_id,
                received_sequence=bridge.frames[-1].sequence,
                rendered_sample_end=rendered_sample_end,
                client_monotonic_ms=2,
                turn_id=fence.turn_id,
                tool_epoch=fence.tool_epoch,
                event_type=PlaybackEventType.PROGRESS,
                approximate=False,
            ),
        )
        transcripts: list[str | None] = []
        finalize = context.runtime.on_media_playback_interrupted

        async def record(
            *, interrupted_from: GenerationFence, synchronized_transcript: str | None
        ) -> str | None:
            transcripts.append(synchronized_transcript)
            return await finalize(
                interrupted_from=interrupted_from,
                synchronized_transcript=synchronized_transcript,
            )

        context.runtime.on_media_playback_interrupted = record
        stop = MediaEnvelope.create(
            type="client.stop_assistant",
            event_id="stop-after-filler",
            session_id=context.identity.session_id,
            stream_epoch=context.identity.stream_epoch,
            sequence=1,
            turn_id=fence.turn_id,
            generation_id=fence.generation_id,
            tool_epoch=fence.tool_epoch,
            payload={"idempotency_key": "stop-after-filler", "reason": "test"},
        )
        assert session.accept_client_stop(stop)
        await registry.on_client_event(session, stop)

        assert transcripts == [heard]
    finally:
        provider.release_reply.set()
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
