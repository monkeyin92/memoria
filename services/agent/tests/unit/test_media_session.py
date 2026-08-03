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
from services.agent.src.voice_core.speech_timeline import ASRResult


class FakeMediaProvider(MediaVoiceProvider):
    def __init__(self) -> None:
        self.audio_calls: list[int] = []
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

    async def close(self, _identity: SessionIdentity) -> None:
        self.closed = True


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
async def test_media_registry_runs_fake_asr_llm_tts_through_both_fences() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
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

        fence, reason = await registry.commit_user_turn(
            session_identity.session_id,
            stream_epoch=1,
            start_sample=0,
            end_sample=2,
        )
        assert reason is None
        assert fence == GenerationFence(session_identity.session_id, 1, 1, 0)
        started = await _next_event(call, "generation")
        assert started.generation.generation_id == 1

        assert fence is not None
        assert await registry.generate_reply(session_identity.session_id, "你好", fence)
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
        assert heard_payload["payload"]["heard"] is True  # type: ignore[index]
        assert context.runtime.orchestrator.state is ConversationState.LISTENING
        assert context.runtime.orchestrator.context.turns[-1].content == "你好。"

        stop = MediaEnvelope.create(
            type="client.stop_assistant",
            event_id="stop-1",
            session_id=session_identity.session_id,
            stream_epoch=1,
            sequence=1,
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
