from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence

import grpc
import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.state_machine import ConversationState
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import AudioFrame, MediaEnvelope, SessionIdentity
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaVoiceCoreRegistry,
    MediaVoiceProvider,
)
from services.agent.src.voice_core.playback_ledger import PlaybackSpan
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
)


class FakeMediaProvider(MediaVoiceProvider):
    def __init__(self) -> None:
        self.audio_calls: list[int] = []
        self.cancelled: list[GenerationFence] = []
        self.closed = False

    async def ingest_audio(
        self,
        _identity: SessionIdentity,
        frame: AudioFrame,
    ) -> Sequence[ASRResult]:
        self.audio_calls.append(frame.sequence)
        if frame.sequence != 0:
            return ()
        return (
            ASRResult(
                task_epoch=1,
                sentence_id="fake-sentence",
                revision=1,
                capture_start_sample=0,
                capture_end_sample=frame.frame_samples,
                text="你好",
                is_final=True,
                confidence=0.99,
                stream_epoch=frame.identity.stream_epoch,
            ),
        )

    def generate_reply(
        self,
        _identity: SessionIdentity,
        _user_text: str,
        _fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=b"\x02\x00\x03\x00",
                source_start_sample=0,
                text="你好。",
                first=True,
                final=True,
            )

        return chunks()

    def cancel_generation(self, fence: GenerationFence) -> bool:
        self.cancelled.append(fence)
        return True

    async def close(self, _identity: SessionIdentity) -> None:
        self.closed = True


class MultiFinalProvider(FakeMediaProvider):
    async def ingest_audio(
        self,
        _identity: SessionIdentity,
        frame: AudioFrame,
    ) -> Sequence[ASRResult]:
        self.audio_calls.append(frame.sequence)
        return (
            ASRResult(
                task_epoch=1,
                sentence_id=f"sentence-{frame.sequence}",
                revision=1,
                capture_start_sample=frame.capture_start_sample,
                capture_end_sample=frame.capture_end_sample,
                text=("第一句", "第二句")[frame.sequence],
                is_final=True,
                confidence=0.99,
                stream_epoch=frame.identity.stream_epoch,
            ),
        )


async def _requests(queue: asyncio.Queue[media_pb2.MediaToCore | None]):
    while True:
        message = await queue.get()
        if message is None:
            return
        yield message


async def _next_event(call, kind: str):
    for _ in range(12):
        event = await asyncio.wait_for(call.read(), timeout=1)
        if event is grpc.aio.EOF:
            raise AssertionError(f"bridge ended before {kind}")
        if event.WhichOneof("event") == kind:
            return event
    raise AssertionError(f"bridge did not emit {kind}")


@pytest.mark.asyncio
async def test_multiple_asr_finals_wait_for_vad_and_commit_one_logical_turn() -> None:
    provider = MultiFinalProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("multi-final-session")
    session = bridge.bridge.open(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    await registry.on_audio_frame(
        session,
        AudioFrame(
            identity=identity,
            sequence=0,
            capture_start_sample=0,
            frame_samples=2,
            payload=b"\x00\x00\x01\x00",
        ),
    )
    # A short VAD pause is not a logical turn boundary: the following speech
    # start cancels the pending endpoint and both provider finals stay in one
    # sample-clock turn.
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-pause",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=2,
            capture_end_sample=3,
            final=True,
        ),
    )
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-resume",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=2,
            capture_end_sample=3,
        ),
    )
    await registry.on_audio_frame(
        session,
        AudioFrame(
            identity=identity,
            sequence=1,
            capture_start_sample=2,
            frame_samples=2,
            payload=b"\x00\x00\x01\x00",
        ),
    )

    context = registry._sessions[identity.session_id]
    assert context.asr.last_sent_sample == 4
    assert [turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"] == []
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=4,
            capture_end_sample=5,
            final=True,
        ),
    )
    await asyncio.sleep(0.03)

    user_turns = [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ]
    assert user_turns == ["第一句 第二句"]
    assert context.asr.last_committed_sample == 4


@pytest.mark.asyncio
async def test_vad_endpoint_waits_for_late_asr_coverage_before_committing() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("late-final-session")
    session = bridge.bridge.open(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    assert await registry.accept_asr_result(
        identity.session_id,
        ASRResult(
            task_epoch=1,
            sentence_id="first",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=320,
            text="第一句",
            is_final=True,
            stream_epoch=1,
        ),
    )
    registry._observe_final_asr_result(
        registry._sessions[identity.session_id],
        ASRResult(
            task_epoch=1,
            sentence_id="first",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=320,
            text="第一句",
            is_final=True,
            stream_epoch=1,
        ),
    )
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=640,
            capture_end_sample=641,
            final=True,
            voiced_end_sample=640,
        ),
    )
    await asyncio.sleep(0.03)

    context = registry._sessions[identity.session_id]
    assert context.asr.last_committed_sample == 0
    assert [turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"] == []

    late = ASRResult(
        task_epoch=1,
        sentence_id="second",
        revision=1,
        capture_start_sample=320,
        capture_end_sample=640,
        text="第二句",
        is_final=True,
        stream_epoch=1,
    )
    assert await registry.accept_asr_result(identity.session_id, late)
    registry._observe_final_asr_result(context, late)
    await asyncio.sleep(0.03)

    assert [
        turn.content
        for turn in context.runtime.orchestrator.context.turns
        if turn.role == "user"
    ] == ["第一句 第二句"]
    assert context.asr.last_committed_sample == 640


@pytest.mark.asyncio
async def test_vad_tail_silence_tolerance_does_not_leave_turn_pending() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("tail-silence-session")
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    final = ASRResult(
        task_epoch=1,
        sentence_id="tail-final",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=16_000,
        text="尾音结束",
        is_final=True,
        stream_epoch=1,
    )
    assert await registry.accept_asr_result(identity.session_id, final)
    registry._observe_final_asr_result(context, final)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-tail-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=24_000,
            capture_end_sample=24_001,
            final=True,
            voiced_end_sample=16_000,
        ),
    )
    await asyncio.sleep(0.03)

    assert [
        turn.content
        for turn in context.runtime.orchestrator.context.turns
        if turn.role == "user"
    ] == ["尾音结束"]
    assert context.asr.last_committed_sample == 24_000

    late_tail = ASRResult(
        task_epoch=1,
        sentence_id="late-tail",
        revision=1,
        capture_start_sample=16_000,
        capture_end_sample=20_000,
        text="不应串入",
        is_final=True,
        stream_epoch=1,
    )
    assert not await registry.accept_asr_result(identity.session_id, late_tail)

    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="next-vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=24_000,
            capture_end_sample=24_001,
        ),
    )
    next_final = ASRResult(
        task_epoch=1,
        sentence_id="next-final",
        revision=1,
        capture_start_sample=24_000,
        capture_end_sample=25_000,
        text="新一轮",
        is_final=True,
        stream_epoch=1,
    )
    assert await registry.accept_asr_result(identity.session_id, next_final)
    registry._observe_final_asr_result(context, next_final)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="next-vad-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=26_000,
            capture_end_sample=26_001,
            final=True,
            voiced_end_sample=25_000,
        ),
    )
    await asyncio.sleep(0.03)
    assert [
        turn.content
        for turn in context.runtime.orchestrator.context.turns
        if turn.role == "user"
    ] == ["尾音结束", "新一轮"]


@pytest.mark.asyncio
async def test_eight_hundred_ms_within_turn_pause_does_not_split_child_speech() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("child-pause-session")
    session = bridge.bridge.open(identity)
    context = await registry._get_or_create(identity)

    async def vad(segment_id: str, sample: int, *, final: bool) -> None:
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=1,
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

    await vad("start-1", 0, final=False)
    first = ASRResult(1, "first", 1, 0, 320, "我想说", True, stream_epoch=1)
    assert await registry.accept_asr_result(identity.session_id, first)
    registry._observe_final_asr_result(context, first)
    await vad("pause", 320, final=True)
    await asyncio.sleep(0.82)
    assert [turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"] == []

    await vad("resume", 320, final=False)
    second = ASRResult(1, "second", 1, 320, 640, "一个故事", True, stream_epoch=1)
    assert await registry.accept_asr_result(identity.session_id, second)
    registry._observe_final_asr_result(context, second)
    await vad("end", 640, final=True)
    endpoint_task = context.turn_endpoint_task
    assert endpoint_task is not None
    endpoint_task.cancel()
    await asyncio.gather(endpoint_task, return_exceptions=True)
    await registry._commit_pending_turn(context)

    assert [
        turn.content
        for turn in context.runtime.orchestrator.context.turns
        if turn.role == "user"
    ] == ["我想说 一个故事"]


@pytest.mark.asyncio
async def test_kws_hard_stop_requires_wire_flag_and_confidence_threshold() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        kws_hard_stop_min_confidence=0.8,
    )
    registry.install()
    identity = SessionIdentity("kws-session")
    session = bridge.bridge.open(identity)

    async def keyword(segment_id: str, confidence: float, hard_stop: bool) -> None:
        start = len(segment_id) * 10
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=1,
                provider_task_epoch=0,
                segment_id=segment_id,
                revision=1,
                kind=SegmentKind.KWS,
                capture_start_sample=start,
                capture_end_sample=start + 1,
                text="停一下",
                final=True,
                confidence=confidence,
                hard_stop=hard_stop,
            ),
        )

    await keyword("not-hard", 1.0, False)
    await keyword("low-confidence", 0.79, True)
    assert session.fence.generation_id == 0
    await keyword("accepted-hard-stop", 0.8, True)
    assert session.fence.generation_id == 1
    assert registry.metrics.latency_samples["interrupt_stop"]


async def _start_speaking_reply(
    registry: MediaVoiceCoreRegistry,
    session: object,
    identity: SessionIdentity,
) -> tuple[object, GenerationFence]:
    """Drive one VAD+ASR turn and a provider reply, returning its context."""

    context = await registry._get_or_create(identity)

    async def vad(segment_id: str, sample: int, *, final: bool) -> None:
        await registry.on_speech_segment(
            session,  # type: ignore[arg-type]
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=1,
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
    final = ASRResult(1, "turn-final", 1, 0, 320, "你好", True, stream_epoch=1)
    assert await registry.accept_asr_result(identity.session_id, final)
    registry._observe_final_asr_result(context, final)
    await vad("end", 320, final=True)
    await asyncio.sleep(0.03)
    fence = context.runtime.fence
    if context.reply_task is not None:
        assert await asyncio.wait_for(context.reply_task, timeout=1)
    assert context.runtime.orchestrator.state is ConversationState.SPEAKING
    # Keep the bridge-side authoritative gate in sync with the runtime fence
    # so a subsequent client stop/KWS cancel derives the expected fence.
    session.generation.advance(fence)  # type: ignore[attr-defined]
    session.reset_downlink_generation(fence)  # type: ignore[attr-defined]
    session.generation_active = True  # type: ignore[attr-defined]
    return context, fence


@pytest.mark.asyncio
async def test_client_stop_during_interruption_pending_finalizes_heard_prefix() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("pending-stop-session")
    session = bridge.bridge.open(identity)
    context, fence = await _start_speaking_reply(registry, session, identity)
    assert context.playback.register_audio(fence, 0, 0, 2)
    assert context.playback.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=0,
            text_end=3,
            audio_start_sample=0,
            audio_end_sample=2,
            text="你好。",
        )
    )
    assert context.playback.acknowledge(fence, 2)
    assert context.playback.actual_heard_text(fence) == "你好。"
    assert context.runtime.orchestrator.state is ConversationState.SPEAKING
    # VAD has already moved the runtime into INTERRUPTION_PENDING; the client
    # stop must still run the interrupted-playback finalize with the heard
    # prefix instead of switching fences without history.
    context.runtime.orchestrator.state_machine.state = ConversationState.INTERRUPTION_PENDING
    interrupted: list[tuple[GenerationFence, str]] = []
    original = context.runtime.on_media_playback_interrupted

    async def spy(*, interrupted_from: GenerationFence, synchronized_transcript: str) -> None:
        interrupted.append((interrupted_from, synchronized_transcript))
        await original(
            interrupted_from=interrupted_from,
            synchronized_transcript=synchronized_transcript,
        )

    context.runtime.on_media_playback_interrupted = spy  # type: ignore[method-assign]
    stop = MediaEnvelope.create(
        type="client.stop_assistant",
        event_id="stop-pending-1",
        session_id=identity.session_id,
        stream_epoch=1,
        sequence=1,
        turn_id=fence.turn_id,
        generation_id=fence.generation_id,
        tool_epoch=fence.tool_epoch,
        payload={"idempotency_key": "stop-pending-1", "reason": "test"},
    )
    assert session.accept_client_stop(stop)
    await registry.on_client_event(session, stop)
    assert interrupted == [(fence, "你好。")]
    assert registry.metrics.latency_samples["interrupt_stop"]


@pytest.mark.asyncio
async def test_kws_stop_during_interruption_pending_finalizes_heard_prefix() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("kws-pending-stop-session")
    session = bridge.bridge.open(identity)
    context, fence = await _start_speaking_reply(registry, session, identity)
    assert context.playback.register_audio(fence, 0, 0, 2)
    assert context.playback.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=0,
            text_end=3,
            audio_start_sample=0,
            audio_end_sample=2,
            text="你好。",
        )
    )
    assert context.playback.acknowledge(fence, 2)
    context.runtime.orchestrator.state_machine.state = ConversationState.INTERRUPTION_PENDING
    interrupted: list[str] = []
    original = context.runtime.on_media_playback_interrupted

    async def spy(*, interrupted_from: GenerationFence, synchronized_transcript: str) -> None:
        interrupted.append(synchronized_transcript)
        await original(
            interrupted_from=interrupted_from,
            synchronized_transcript=synchronized_transcript,
        )

    context.runtime.on_media_playback_interrupted = spy  # type: ignore[method-assign]
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="kws-pending-stop",
            revision=1,
            kind=SegmentKind.KWS,
            capture_start_sample=400,
            capture_end_sample=402,
            text="停一下",
            final=True,
            confidence=0.95,
            hard_stop=True,
        ),
    )
    assert interrupted == ["你好。"]
    assert registry.metrics.latency_samples["interrupt_stop"]


@pytest.mark.asyncio
async def test_interrupt_slo_uses_edge_detected_timestamp() -> None:
    import time as time_module

    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("slo-session")
    session = bridge.bridge.open(identity)
    context, fence = await _start_speaking_reply(registry, session, identity)
    detected_ms = int(time_module.time() * 1000) - 1_000
    stop = MediaEnvelope.create(
        type="client.stop_assistant",
        event_id="slo-stop-1",
        session_id=identity.session_id,
        stream_epoch=1,
        sequence=1,
        turn_id=fence.turn_id,
        generation_id=fence.generation_id,
        tool_epoch=fence.tool_epoch,
        payload={"idempotency_key": "slo-stop-1", "reason": "test"},
    )
    assert session.accept_client_stop(stop)
    await registry.on_client_event(session, stop, detected_ms)
    latency = registry.metrics.latency_samples["interrupt_stop"][-1]
    assert 0.5 <= latency <= 2.5


@pytest.mark.asyncio
async def test_media_registry_runs_fake_asr_llm_tts_through_both_fences() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    port = await bridge.start("127.0.0.1:0")
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    rpc = channel.stream_stream(
        "/memoria.media.v1.VoiceMediaBridge/Connect",
        request_serializer=media_pb2.MediaToCore.SerializeToString,
        response_deserializer=media_pb2.CoreToMedia.FromString,
    )
    requests: asyncio.Queue[media_pb2.MediaToCore | None] = asyncio.Queue()
    call = rpc(_requests(requests))
    identity = media_pb2.SessionIdentity(
        session_id="media-registry-session",
        account_id="account",
        participant_id="participant",
        device_id="h5",
        client_type="h5",
        stream_epoch=1,
    )
    session_identity = SessionIdentity(
        "media-registry-session",
        account_id="account",
        participant_id="participant",
        device_id="h5",
    )
    try:
        await requests.put(media_pb2.MediaToCore(hello=media_pb2.SessionHello(identity=identity)))
        accepted = await asyncio.wait_for(call.read(), timeout=1)
        assert accepted.accepted.identity.session_id == session_identity.session_id
        await requests.put(
            media_pb2.MediaToCore(
                vad=media_pb2.VadEvent(
                    identity=identity,
                    type=media_pb2.VAD_EVENT_SPEECH_START,
                    sample_position=0,
                    probability=0.99,
                )
            )
        )
        await requests.put(
            media_pb2.MediaToCore(
                audio=media_pb2.AudioFrame(
                    identity=identity,
                    sequence=0,
                    capture_start_sample=0,
                    frame_samples=2,
                    payload=b"\x00\x00\x01\x00",
                )
            )
        )
        transcript = await _next_event(call, "transcript")
        assert transcript.transcript.text == "你好"
        assert provider.audio_calls == [0]

        await requests.put(
            media_pb2.MediaToCore(
                vad=media_pb2.VadEvent(
                    identity=identity,
                    type=media_pb2.VAD_EVENT_SPEECH_END,
                    sample_position=2,
                    probability=0.99,
                    voiced_end_sample=2,
                )
            )
        )
        started = await _next_event(call, "generation")
        assert started.generation.generation_id == 1
        fence = GenerationFence(session_identity.session_id, 1, 1, 0)
        audio = await _next_event(call, "audio")
        assert audio.audio.generation_id == fence.generation_id
        completed = await _next_event(call, "generation")
        assert completed.generation.action == media_pb2.GENERATION_ACTION_COMPLETE
        context = registry._sessions[session_identity.session_id]
        assert context.playback.current_fence == fence
        assert context.playback._spans[fence]
        assert context.runtime.orchestrator.state is ConversationState.SPEAKING

        await requests.put(
            media_pb2.MediaToCore(
                playback=media_pb2.PlaybackProgress(
                    identity=identity,
                    generation_id=fence.generation_id,
                    received_sequence=audio.audio.sequence,
                    rendered_sample_end=2,
                    client_monotonic_ms=1,
                    approximate=False,
                    turn_id=fence.turn_id,
                    tool_epoch=fence.tool_epoch,
                )
            )
        )
        heard_payload: dict[str, object] = {}
        for _ in range(8):
            heard_event = await _next_event(call, "client")
            heard_payload = json.loads(bytes(heard_event.client.json_payload))
            payload = heard_payload.get("payload")
            if isinstance(payload, dict) and payload.get("heard") is True:
                break
        assert heard_payload["type"] == "transcript_delta"
        assert heard_payload["v"] == 1
        assert heard_payload["protocol"] == "media-v1"
        assert heard_payload["event_id"]
        assert heard_payload["server_monotonic_ms"] > 0
        assert heard_payload["payload"]["heard"] is True  # type: ignore[index]
        assert context.runtime.orchestrator.state is ConversationState.LISTENING
        assert context.runtime.orchestrator.context.turns[-1].content == "你好。"

        stop = MediaEnvelope.create(
        type="client.stop_assistant",
        event_id="stop-1",
        session_id=session_identity.session_id,
        stream_epoch=1,
        sequence=1,
        turn_id=fence.turn_id,
        generation_id=fence.generation_id,
        tool_epoch=fence.tool_epoch,
        payload={"idempotency_key": "stop-1", "reason": "test"},
        )
        await requests.put(
            media_pb2.MediaToCore(
                device=media_pb2.DeviceEvent(
                    identity=identity,
                    event_type="client.stop_assistant",
                    json_payload=stop.encode(),
                )
            )
        )
        cancelled = await _next_event(call, "generation")
        assert cancelled.generation.action == media_pb2.GENERATION_ACTION_CANCEL
        assert cancelled.generation.generation_id == 2
        assert not await registry.generate_reply(session_identity.session_id, "旧回复", fence)
        assert registry.context(session_identity.session_id) is not None
    finally:
        await requests.put(None)
        await call.read()
        await channel.close()
        await bridge.stop()
    assert provider.closed is True
    assert bridge.bridge.get(session_identity.session_id) is None
