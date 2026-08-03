from __future__ import annotations

import asyncio

import grpc
import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_bridge_server import PCMFrame
from services.agent.src.voice_core.media_protocol import MediaEnvelope, SessionIdentity


async def _request_stream(
    queue: asyncio.Queue[media_pb2.MediaToCore | None],
):
    while True:
        message = await queue.get()
        if message is None:
            return
        yield message


@pytest.mark.asyncio
async def test_bidirectional_media_v1_bridge_fences_audio_and_client_stop() -> None:
    client_events: list[str] = []
    audio_sequences: list[int] = []
    segment_kinds: list[str] = []
    segment_controls: list[tuple[bool, bool]] = []
    voiced_end_samples: list[int | None] = []
    audio_seen = asyncio.Event()
    segment_seen = asyncio.Event()

    async def on_client_event(_session, event: MediaEnvelope, detected_monotonic_ms: int = 0) -> None:
        client_events.append(event.type)

    async def on_audio_frame(_session, frame) -> None:
        audio_sequences.append(frame.sequence)
        audio_seen.set()

    async def on_speech_segment(_session, segment, detected_monotonic_ms: int = 0) -> None:
        segment_kinds.append(segment.kind.value)
        segment_controls.append((segment.final, segment.hard_stop))
        voiced_end_samples.append(segment.voiced_end_sample)
        segment_seen.set()

    bridge = MediaBridgeGrpcServer(
        on_client_event=on_client_event,
        on_audio_frame=on_audio_frame,
        on_speech_segment=on_speech_segment,
    )
    port = await bridge.start("127.0.0.1:0")
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    rpc = channel.stream_stream(
        "/memoria.media.v1.VoiceMediaBridge/Connect",
        request_serializer=media_pb2.MediaToCore.SerializeToString,
        response_deserializer=media_pb2.CoreToMedia.FromString,
    )
    requests: asyncio.Queue[media_pb2.MediaToCore | None] = asyncio.Queue()
    call = rpc(_request_stream(requests))
    identity = media_pb2.SessionIdentity(
        session_id="grpc-session",
        account_id="account",
        device_id="h5",
        client_type="h5",
        stream_epoch=1,
    )
    await requests.put(
        media_pb2.MediaToCore(
            hello=media_pb2.SessionHello(
                identity=identity,
                uplink_format=media_pb2.AudioFormat(
                    encoding=media_pb2.AUDIO_ENCODING_PCM_S16LE,
                    sample_rate=16_000,
                    channels=1,
                    frame_ms=20,
                ),
            )
        )
    )
    accepted = await call.read()
    assert accepted.accepted.identity.session_id == "grpc-session"

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
    await requests.put(
        media_pb2.MediaToCore(
            vad=media_pb2.VadEvent(
                identity=identity,
                type=media_pb2.VAD_EVENT_SPEECH_START,
                sample_position=0,
                probability=0.9,
            )
        )
    )
    await asyncio.wait_for(audio_seen.wait(), timeout=1)
    await asyncio.wait_for(segment_seen.wait(), timeout=1)
    assert audio_sequences == [0]
    assert segment_kinds == ["vad"]
    assert segment_controls == [(False, False)]
    assert voiced_end_samples == [None]

    segment_seen.clear()
    await requests.put(
        media_pb2.MediaToCore(
            vad=media_pb2.VadEvent(
                identity=identity,
                type=media_pb2.VAD_EVENT_SPEECH_END,
                sample_position=400,
                probability=0.1,
            )
        )
    )
    invalid_vad = await asyncio.wait_for(call.read(), timeout=1)
    assert invalid_vad.error.code == "invalid_vad_event"
    assert not segment_seen.is_set()

    await requests.put(
        media_pb2.MediaToCore(
            vad=media_pb2.VadEvent(
                identity=identity,
                type=media_pb2.VAD_EVENT_SPEECH_END,
                sample_position=400,
                voiced_end_sample=320,
                probability=0.1,
            )
        )
    )
    await asyncio.wait_for(segment_seen.wait(), timeout=1)
    assert segment_controls[-1] == (True, False)
    assert voiced_end_samples[-1] == 320

    session_identity = SessionIdentity("grpc-session", account_id="account", device_id="h5")
    assert await bridge.emit_pcm(
        "grpc-session",
        PCMFrame(
            identity=session_identity,
            turn_id=0,
            generation_id=0,
            tool_epoch=0,
            sequence=0,
            source_start_sample=0,
            frame_samples=2,
            pcm_s16le=b"\x00\x00\x01\x00",
        ),
    )
    audio = await call.read()
    assert audio.audio.sequence == 0

    stop = MediaEnvelope.create(
        type="client.stop_assistant",
        event_id="stop-1",
        session_id="grpc-session",
        stream_epoch=1,
        sequence=1,
        payload={"idempotency_key": "stop-1", "reason": "button"},
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
    generation = await call.read()
    assert generation.generation.action == media_pb2.GENERATION_ACTION_CANCEL
    assert generation.generation.generation_id == 1
    assert client_events == ["client.stop_assistant"]

    assert not await bridge.emit_pcm(
        "grpc-session",
        PCMFrame(
            identity=session_identity,
            turn_id=0,
            generation_id=0,
            tool_epoch=0,
            sequence=1,
            source_start_sample=2,
            frame_samples=2,
            pcm_s16le=b"\x00\x00\x01\x00",
        ),
    )

    # A hard-stop keyword closes the Edge gate before the callback is
    # delivered; it is still observed as a speech control event, but cannot
    # reopen the cancelled generation or admit stale PCM.
    segment_seen.clear()
    await requests.put(
        media_pb2.MediaToCore(
            keyword=media_pb2.KeywordEvent(
                identity=identity,
                keyword="停一下",
                confidence=0.95,
                start_sample=0,
                end_sample=2,
                hard_stop=True,
            )
        )
    )
    await asyncio.wait_for(segment_seen.wait(), timeout=1)
    assert segment_controls[-1] == (True, True)
    await requests.put(None)
    assert await call.read() is grpc.aio.EOF
    assert await call.code() == grpc.StatusCode.OK
    await channel.close()
    await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_rejects_non_monotonic_generation_controls() -> None:
    bridge = MediaBridgeGrpcServer()
    identity = SessionIdentity("generation-session")
    session = bridge.bridge.open(identity)
    assert not await bridge.emit_generation(
        identity.session_id,
        GenerationFence("generation-session", 0, 0, 0),
        action=media_pb2.GENERATION_ACTION_CANCEL,
    )
    assert session.fence.generation_id == 0


@pytest.mark.asyncio
async def test_outgoing_queue_overflow_wakes_writer_for_reconnect() -> None:
    bridge = MediaBridgeGrpcServer(max_pending_messages=1)
    connection = bridge._open_connection(SessionIdentity("overflow-session"))
    message = media_pb2.CoreToMedia(
        error=media_pb2.CoreError(code="one", message="queued")
    )
    assert await bridge._enqueue(connection, message)
    assert not await bridge._enqueue(connection, message)
    assert connection.closed is True
    assert await asyncio.wait_for(connection.outgoing.get(), timeout=0.1) is None


@pytest.mark.asyncio
async def test_outgoing_queue_overflow_cancels_generation_and_reconnect_sends_cancel() -> None:
    bridge = MediaBridgeGrpcServer(max_pending_messages=1)
    connection = bridge._open_connection(SessionIdentity("overflow-session", stream_epoch=1))
    fence = GenerationFence("overflow-session", 1, 1, 0)
    # The START control occupies the single queue slot; the next PCM/event
    # triggers overflow.
    assert await bridge.emit_generation(
        "overflow-session",
        fence,
        action=media_pb2.GENERATION_ACTION_START,
    )
    message = media_pb2.CoreToMedia(
        error=media_pb2.CoreError(code="one", message="queued")
    )
    assert not await bridge._enqueue(connection, message)
    assert connection.closed is True
    assert connection.session.generation_active is False
    terminal = await asyncio.wait_for(connection.outgoing.get(), timeout=0.1)
    assert terminal.generation.action == media_pb2.GENERATION_ACTION_CANCEL
    assert terminal.generation.reason == "downlink_queue_full"
    assert terminal.generation.generation_id == fence.generation_id + 1
    # A reconnect reuses the same session state: because the overflowed
    # generation is inactive, the transport announces CANCEL, never RESUME.
    reconnected = bridge._open_connection(
        SessionIdentity("overflow-session", stream_epoch=2)
    )
    assert reconnected.session is connection.session
    assert reconnected.session.generation_active is False


@pytest.mark.asyncio
async def test_old_transport_close_cannot_notify_after_reconnect_claims_session() -> None:
    closed_epochs: list[int] = []

    async def on_closed(session) -> None:
        closed_epochs.append(session.identity.stream_epoch)

    bridge = MediaBridgeGrpcServer(on_session_closed=on_closed)
    first = bridge._open_connection(SessionIdentity("race-session", stream_epoch=1))
    bridge._close_connection(first)
    second = bridge._open_connection(SessionIdentity("race-session", stream_epoch=2))

    await bridge._finish_connection(first)
    assert closed_epochs == []
    await bridge._finish_connection(second)
    assert closed_epochs == [2]
