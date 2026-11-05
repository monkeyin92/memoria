"""Media session: registry fence chain through fake ASR, LLM and TTS."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import grpc
import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.state_machine import (
    ConversationState,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    MediaEnvelope,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    ASRWordTiming,
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _bind_verified_owner_classifier,
    _next_event,
    _requests,
)


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
        await requests.put(
            media_pb2.MediaToCore(
                hello=media_pb2.SessionHello(
                    identity=identity,
                    interaction_authority=media_pb2.INTERACTION_AUTHORITY_GO_SHADOW,
                )
            )
        )
        accepted = await asyncio.wait_for(call.read(), timeout=1)
        assert accepted.accepted.identity.session_id == session_identity.session_id
        # A Go-shadow request degrades to the only supported authority.
        assert (
            accepted.accepted.interaction_authority
            == media_pb2.INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE
        )
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
        floor = await _next_event(call, "floor_effect")
        assert floor.floor_effect.floor_state == media_pb2.FLOOR_STATE_SILENCE
        assert floor.floor_effect.floor_epoch == 1
        assert floor.floor_effect.candidate_only is False
        assert floor.floor_effect.expires_at_ms > 0
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
        context = registry.session_state(session_identity.session_id)
        assert context.output.playback.current_fence == fence
        assert context.output.playback._spans[fence]
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
        for _ in range(32):
            heard_event = await _next_event(call, "client")
            heard_payload = json.loads(bytes(heard_event.client.json_payload))
            payload = heard_payload.get("payload")
            if (
                heard_payload.get("type") == "transcript_delta"
                and isinstance(payload, dict)
                and payload.get("heard") is True
            ):
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


@pytest.mark.asyncio
async def test_registry_forwards_only_normalized_watermark_tail() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("registry-normalized-tail")
    context = await registry.open_session(identity)
    first = ASRResult(1, "sentence", 1, 0, 320, "你好", True)
    assert await registry.accept_asr_result(identity.session_id, first)
    context.asr.mark_committed(320)
    extension = ASRResult(
        2,
        "sentence",
        1,
        0,
        800,
        "你好世界",
        True,
        word_timings=(
            # Complete evidence lets the supervisor drop the committed prefix
            # without guessing a character boundary.
            ASRWordTiming("你", 0, 160),
            ASRWordTiming("好", 160, 320),
            ASRWordTiming("世", 320, 560),
            ASRWordTiming("界", 560, 800),
        ),
    )

    decision = await registry._accept_asr_result_decision(identity.session_id, extension)
    assert decision.accepted is not None
    assert decision.accepted.text == "世界"
    assert (
        context.runtime.speech_timeline.canonical_text(
            stream_epoch=1,
            start_sample=0,
            end_sample=800,
        )
        == "你好 世界"
    )


@pytest.mark.asyncio
async def test_registry_chain_normalizes_tail_and_keeps_rejected_replay_out_of_next_turn() -> None:
    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.transcripts: list[SpeechSegment] = []
            self.client_events: list[tuple[str, dict[str, object]]] = []

        async def emit_transcript(
            self,
            session_id: str,
            segment: SpeechSegment,
            *,
            turn_id: int | None = None,
            speaker_class: str = "",
            task_epoch: int = 0,
            context_version: int = 0,
        ) -> bool:
            _ = session_id, turn_id, speaker_class, task_epoch, context_version
            self.transcripts.append(segment)
            return True

        async def emit_event(
            self,
            session_id: str,
            event_type: str,
            payload: dict[str, object],
            *,
            turn_id: int = 0,
            generation_id: int = 0,
            tool_epoch: int = 0,
            task_epoch: int = 0,
            context_version: int = 0,
        ) -> bool:
            _ = session_id, turn_id, generation_id, tool_epoch, task_epoch, context_version
            self.client_events.append((event_type, payload))
            return True

    class WatermarkProvider(FakeMediaProvider):
        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            results = (
                ASRResult(1, "sentence", 1, 0, 320, "你好", True),
                ASRResult(
                    2,
                    "sentence",
                    1,
                    0,
                    640,
                    "你好世界",
                    True,
                    word_timings=(
                        ASRWordTiming("你", 0, 160),
                        ASRWordTiming("好", 160, 320),
                        ASRWordTiming("世", 320, 480),
                        ASRWordTiming("界", 480, 640),
                    ),
                ),
                ASRResult(3, "replay", 1, 0, 960, "旧前缀污染", True),
                ASRResult(2, "next", 1, 960, 1280, "下一轮", True),
            )
            return (results[frame.sequence],)

    provider = WatermarkProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=60,
    )
    registry.install()
    identity = SessionIdentity("registry-full-chain")
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    # Successive overlapping turns are ordinary barge-ins: they need verified
    # owner authority for the utterance that produces them.
    _bind_verified_owner_classifier(context.runtime)

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

    async def audio(sequence: int, start_sample: int) -> None:
        await registry.on_audio_frame(
            session,
            AudioFrame(identity, sequence, start_sample, 320, b"\x00\x00" * 320),
        )

    async def commit(segment_id: str, sample: int) -> None:
        await vad(segment_id, sample, final=True)
        endpoint_task = context.turn_endpoint_task
        assert endpoint_task is not None
        endpoint_task.cancel()
        await asyncio.gather(endpoint_task, return_exceptions=True)
        await registry._commit_pending_turn(context)

    await vad("start-1", 0, final=False)
    context.runtime.feed_speaker_pcm(b"\x00\x00" * 8_000)
    await audio(0, 0)
    assert context.projection.provisional is not None
    assert not [turn for turn in context.runtime.orchestrator.context.turns if turn.role == "user"]
    await commit("end-1", 320)
    assert context.projection.provisional is None

    await vad("start-2", 320, final=False)
    context.runtime.feed_speaker_pcm(b"\x00\x00" * 8_000)
    await audio(1, 320)
    await commit("end-2", 640)
    assert [segment.text for segment in bridge.transcripts] == ["你好", "世界"]

    await audio(2, 640)
    assert [segment.text for segment in bridge.transcripts] == ["你好", "世界"]
    assert context.pending.turn_start_sample is None
    assert context.pending.turn_end_sample is None

    await vad("start-3", 960, final=False)
    context.runtime.feed_speaker_pcm(b"\x00\x00" * 8_000)
    await audio(3, 960)
    await commit("end-3", 1280)

    user_turns = [
        turn.content for turn in context.runtime.orchestrator.context.turns if turn.role == "user"
    ]
    assert user_turns == ["你好", "世界", "下一轮"]
    assert [segment.text for segment in bridge.transcripts] == ["你好", "世界", "下一轮"]
    provisional_started = [
        payload
        for event_type, payload in bridge.client_events
        if event_type == "turn.provisional.started"
    ]
    committed = [
        payload for event_type, payload in bridge.client_events if event_type == "turn.committed"
    ]
    assert len(provisional_started) == 3
    assert len({payload["provisional_id"] for payload in provisional_started}) == 3
    assert [payload["text"] for payload in committed] == ["你好", "世界", "下一轮"]
    assert all(payload["history_eligible"] is False for payload in committed)
