from __future__ import annotations

import asyncio
import json

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_bridge_server import PCMFrame
from services.agent.src.voice_core.media_protocol import MediaEnvelope, SessionIdentity
from services.agent.src.voice_core.speech_timeline import SegmentKind, SpeechSegment


async def _request_stream(
    queue: asyncio.Queue[media_pb2.MediaToCore | None],
):
    while True:
        message = await queue.get()
        if message is None:
            return
        yield message


@pytest.mark.asyncio
async def test_grpc_health_serves_voice_media_bridge() -> None:
    bridge = MediaBridgeGrpcServer()
    port = await bridge.start("127.0.0.1:0")
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        response = await health_pb2_grpc.HealthStub(channel).Check(
            health_pb2.HealthCheckRequest(service=MediaBridgeGrpcServer._SERVICE)
        )
        assert response.status == health_pb2.HealthCheckResponse.SERVING
    finally:
        await channel.close()
        await bridge.stop()


@pytest.mark.asyncio
async def test_bridge_stamps_versions_and_never_emits_shadow_observations() -> None:
    bridge = MediaBridgeGrpcServer()
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
        session_id="bridge-versions",
        account_id="account",
        device_id="h5",
        client_type="h5",
        stream_epoch=1,
    )
    await requests.put(
        media_pb2.MediaToCore(
            hello=media_pb2.SessionHello(
                identity=identity,
                interaction_authority=media_pb2.INTERACTION_AUTHORITY_GO_SHADOW,
            )
        )
    )
    accepted = await call.read()
    # A Go-shadow request degrades to the only supported authority.
    assert (
        accepted.accepted.interaction_authority
        == media_pb2.INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE
    )

    assert await bridge.emit_event(
        "bridge-versions",
        "audio_trace",
        {"name": "client_event_versions"},
        turn_id=1,
        generation_id=1,
        tool_epoch=2,
        task_epoch=3,
        context_version=7,
    )
    client_event = await call.read()
    assert client_event.client.task_epoch == 3
    assert client_event.client.context_version == 7
    client_payload = json.loads(client_event.client.json_payload)
    assert client_payload["task_epoch"] == 3
    assert client_payload["context_version"] == 7

    fence = GenerationFence("bridge-versions", 1, 1, 2, 7)
    assert await bridge.emit_generation(
        "bridge-versions",
        fence,
        action=media_pb2.GENERATION_ACTION_START,
        task_epoch=3,
        context_version=7,
    )
    generation = await call.read()
    assert generation.generation.task_epoch == 3
    assert generation.generation.context_version == 7
    assert generation.generation.session_epoch == 7
    assert await bridge.emit_pcm(
        "bridge-versions",
        PCMFrame(
            identity=SessionIdentity("bridge-versions", account_id="account", device_id="h5"),
            turn_id=1,
            generation_id=1,
            tool_epoch=2,
            session_epoch=7,
            sequence=0,
            source_start_sample=0,
            frame_samples=1,
            pcm_s16le=b"\x00\x00",
            task_epoch=3,
            context_version=7,
        ),
    )
    audio = await call.read()
    assert audio.audio.task_epoch == 3
    assert audio.audio.context_version == 7
    assert audio.audio.session_epoch == 7

    # Assistant state is delivered as a client event only; no candidate-only
    # floor observation follows it.
    assert await bridge.emit_event(
        "bridge-versions",
        "assistant_state",
        {"state": "speaking", "phase": "speaking"},
        turn_id=1,
        generation_id=1,
        tool_epoch=2,
    )
    assert await bridge.emit_event("bridge-versions", "marker", {})
    state_event = await call.read()
    assert state_event.client.type == "assistant_state"
    marker_event = await call.read()
    assert marker_event.client.type == "marker"

    await requests.put(None)
    assert await call.read() is grpc.aio.EOF
    await channel.close()
    await bridge.stop()


@pytest.mark.asyncio
async def test_coalescing_overflow_never_cancels_authoritative_delivery() -> None:
    bridge = MediaBridgeGrpcServer(max_pending_messages=1)
    identity = SessionIdentity(
        "coalescing-overflow",
        account_id="account",
        device_id="h5",
        stream_epoch=1,
    )
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    connection.outgoing.put_nowait(
        media_pb2.CoreToMedia(
            transcript=media_pb2.TranscriptEvent(turn_id=1, revision=1, text="草稿")
        )
    )

    # A full queue first sheds the coalescing partial transcript; the
    # authoritative event is delivered and the transport stays open.
    assert await bridge.emit_event("coalescing-overflow", "authoritative", {})
    assert connection.closed is False
    assert connection.session.overflow_count == 0
    assert connection.outgoing.qsize() == 1
    queued = connection.outgoing.get_nowait()
    assert queued.client.type == "authoritative"


@pytest.mark.asyncio
async def test_closed_conversation_state_preempts_reliable_output() -> None:
    bridge = MediaBridgeGrpcServer(max_pending_messages=4)
    identity = SessionIdentity("conversation-close", account_id="account", device_id="device")
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    assert await bridge.emit_event("conversation-close", "assistant_state", {})

    fence = connection.session.fence.with_session_epoch(1)
    assert await bridge.emit_conversation_state(
        identity.session_id,
        media_pb2.CONVERSATION_STATE_CLOSED,
        fence=fence,
        reason="owner_silence_timeout",
        task_epoch=2,
        context_version=3,
    )

    closed = connection.outgoing.get_nowait()
    assert closed.WhichOneof("event") == "state"
    assert closed.state.state == media_pb2.CONVERSATION_STATE_CLOSED
    assert closed.state.reason == "owner_silence_timeout"
    assert closed.state.task_epoch == 2
    assert closed.state.context_version == 3
    assert connection.session.fence == fence
    assert connection.session.generation_active is False
    assert connection.outgoing.get_nowait().client.type == "assistant_state"

    assert not await bridge.emit_conversation_state(
        identity.session_id,
        media_pb2.CONVERSATION_STATE_CLOSED,
        fence=fence.with_session_epoch(0),
        reason="stale_owner_silence_timeout",
    )


@pytest.mark.asyncio
async def test_closed_conversation_state_survives_a_full_outgoing_queue() -> None:
    bridge = MediaBridgeGrpcServer(max_pending_messages=1)
    identity = SessionIdentity("conversation-close-full", account_id="account", device_id="device")
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    assert await bridge.emit_event(identity.session_id, "reliable.before_close", {})

    assert await bridge.emit_conversation_state(
        identity.session_id,
        media_pb2.CONVERSATION_STATE_CLOSED,
        fence=connection.session.fence,
        reason="max_user_speech_duration_timeout",
    )

    # The old reliable event may be discarded under pressure, but the typed
    # terminal receipt must remain deliverable and close the transport.
    assert connection.closed is True
    terminal = connection.outgoing.get_nowait()
    assert terminal.WhichOneof("event") == "state"
    assert terminal.state.state == media_pb2.CONVERSATION_STATE_CLOSED
    assert connection.outgoing.empty()


@pytest.mark.asyncio
async def test_terminal_tombstone_blocks_closed_epoch_and_allows_newer_epoch() -> None:
    bridge = MediaBridgeGrpcServer()
    identity = SessionIdentity("conversation-tombstone", account_id="account", device_id="device")
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    assert await bridge.emit_conversation_state(
        identity.session_id,
        media_pb2.CONVERSATION_STATE_CLOSED,
        fence=connection.session.fence,
        reason="owner_silence_timeout",
    )
    connection.outgoing.get_nowait()

    bridge._close_connection(connection)  # noqa: SLF001 - deterministic cleanup
    assert bridge.bridge.close_if_epoch(identity.session_id, identity.stream_epoch)
    assert bridge.bridge.get(identity.session_id) is None
    assert bridge.bridge.is_terminal(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
    )
    with pytest.raises(ValueError, match="media session is terminal"):
        bridge._open_connection(  # noqa: SLF001 - transport seam under test
            SessionIdentity(
                identity.session_id,
                account_id=identity.account_id,
                device_id=identity.device_id,
                stream_epoch=identity.stream_epoch,
            )
        )

    replacement = bridge._open_connection(  # noqa: SLF001 - transport seam under test
        SessionIdentity(
            identity.session_id,
            account_id=identity.account_id,
            device_id=identity.device_id,
            stream_epoch=identity.stream_epoch + 1,
        )
    )
    assert replacement.session.identity.stream_epoch == identity.stream_epoch + 1


@pytest.mark.asyncio
async def test_terminal_session_rejects_late_media_without_invoking_callbacks() -> None:
    audio_seen: list[int] = []
    speech_seen: list[str] = []

    async def on_audio(_session, frame) -> None:
        audio_seen.append(frame.sequence)

    async def on_speech(_session, segment, _detected_monotonic_ms=0) -> None:
        speech_seen.append(segment.kind.value)

    bridge = MediaBridgeGrpcServer(
        on_audio_frame=on_audio,
        on_speech_segment=on_speech,
    )
    identity = SessionIdentity("terminal-input", account_id="account", device_id="device")
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test

    assert await bridge.emit_conversation_state(
        identity.session_id,
        media_pb2.CONVERSATION_STATE_CLOSED,
        fence=connection.session.fence,
        reason="owner_silence_timeout",
    )
    terminal = connection.outgoing.get_nowait()
    assert terminal.WhichOneof("event") == "state"
    assert terminal.state.state == media_pb2.CONVERSATION_STATE_CLOSED
    assert connection.session.terminal_requested is True
    assert connection.session.accepts_input() is False
    assert connection.session.generation_active is False

    proto_identity = media_pb2.SessionIdentity(
        session_id=identity.session_id,
        account_id=identity.account_id,
        device_id=identity.device_id,
        client_type=identity.client_type,
        stream_epoch=identity.stream_epoch,
    )
    await bridge._handle_request(  # noqa: SLF001 - transport seam under test
        connection,
        media_pb2.MediaToCore(
            audio=media_pb2.AudioFrame(
                identity=proto_identity,
                sequence=0,
                capture_start_sample=0,
                frame_samples=2,
                payload=b"\x00\x00\x01\x00",
            )
        ),
    )
    await bridge._handle_request(  # noqa: SLF001 - transport seam under test
        connection,
        media_pb2.MediaToCore(
            vad=media_pb2.VadEvent(
                identity=proto_identity,
                type=media_pb2.VAD_EVENT_SPEECH_START,
                sample_position=0,
                probability=0.99,
            )
        ),
    )
    await bridge._handle_request(  # noqa: SLF001 - transport seam under test
        connection,
        media_pb2.MediaToCore(
            keyword=media_pb2.KeywordEvent(
                identity=proto_identity,
                keyword="停一下",
                confidence=0.99,
                start_sample=0,
                end_sample=2,
                hard_stop=True,
            )
        ),
    )

    assert audio_seen == []
    assert speech_seen == []
    assert connection.session.timeline.pending == ()
    assert not connection.session.uplink
    assert connection.outgoing.empty()
    assert not await bridge.emit_pcm(
        identity.session_id,
        PCMFrame(
            identity=identity,
            turn_id=0,
            generation_id=0,
            tool_epoch=0,
            sequence=0,
            source_start_sample=0,
            frame_samples=2,
            pcm_s16le=b"\x00\x00\x01\x00",
        ),
    )
    assert not await bridge.emit_event(identity.session_id, "late.event", {})
    assert not connection.session.reconnect(
        SessionIdentity(
            identity.session_id,
            account_id=identity.account_id,
            device_id=identity.device_id,
            stream_epoch=identity.stream_epoch + 1,
        )
    )
    with pytest.raises(ValueError, match="media session is terminal"):
        bridge._open_connection(  # noqa: SLF001 - transport seam under test
            SessionIdentity(
                identity.session_id,
                account_id=identity.account_id,
                device_id=identity.device_id,
                stream_epoch=identity.stream_epoch + 1,
            )
        )


def test_outgoing_queue_drains_critical_before_reliable_and_coalescing() -> None:
    bridge = MediaBridgeGrpcServer(max_pending_messages=4)
    connection = bridge._open_connection(  # noqa: SLF001 - transport seam under test
        SessionIdentity("priority-outgoing", account_id="account", device_id="h5")
    )
    connection.outgoing.put_nowait(
        media_pb2.CoreToMedia(client=media_pb2.ClientEvent(type="committed"))
    )
    connection.outgoing.put_nowait(
        media_pb2.CoreToMedia(
            transcript=media_pb2.TranscriptEvent(turn_id=1, revision=1, text="草稿")
        )
    )
    connection.outgoing.put_nowait(
        media_pb2.CoreToMedia(
            generation=media_pb2.GenerationControl(
                action=media_pb2.GENERATION_ACTION_CANCEL,
                reason="user_stop",
            )
        )
    )

    assert connection.outgoing.get_nowait().WhichOneof("event") == "generation"
    assert connection.outgoing.get_nowait().WhichOneof("event") == "client"
    assert connection.outgoing.get_nowait().WhichOneof("event") == "transcript"


@pytest.mark.asyncio
async def test_outgoing_wire_sequence_follows_priority_drain_order() -> None:
    bridge = MediaBridgeGrpcServer(max_pending_messages=4)
    identity = SessionIdentity(
        "priority-sequence",
        account_id="account",
        device_id="device",
        client_type="h5",
        stream_epoch=7,
    )
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test

    # A coalescing partial transcript is followed by a reliable client event.
    # A later generation control is critical and therefore overtakes both.
    # Wire sequence must describe that actual send order, otherwise Media
    # Edge correctly rejects the delayed smaller sequence.
    connection.outgoing.put_nowait(
        media_pb2.CoreToMedia(
            transcript=media_pb2.TranscriptEvent(turn_id=1, revision=1, text="草稿")
        )
    )
    assert await bridge.emit_event(
        identity.session_id,
        "assistant_state",
        {"phase": "user_speaking"},
    )
    assert await bridge.emit_generation(
        identity.session_id,
        GenerationFence(identity.session_id, 1, 1, 0),
        action=media_pb2.GENERATION_ACTION_START,
    )

    generation = connection.outgoing.get_nowait()
    client = connection.outgoing.get_nowait()
    transcript = connection.outgoing.get_nowait()

    assert generation.WhichOneof("event") == "generation"
    assert client.WhichOneof("event") == "client"
    assert transcript.WhichOneof("event") == "transcript"
    assert connection.outgoing.empty()
    assert [
        generation.generation.sequence,
        client.client.sequence,
        transcript.transcript.sequence,
    ] == [0, 1, 2]
    client_payload = json.loads(client.client.json_payload)
    assert client_payload["sequence"] == 1
    assert client_payload["event_id"].endswith(":1")


@pytest.mark.asyncio
async def test_python_executor_emits_fenced_realtime_effect() -> None:
    bridge = MediaBridgeGrpcServer()
    identity = SessionIdentity(
        "authoritative-effect",
        account_id="account",
        device_id="h5",
        stream_epoch=3,
    )
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    fence = GenerationFence(identity.session_id, 4, 5, 6)
    assert await bridge.emit_generation(
        identity.session_id,
        fence,
        action=media_pb2.GENERATION_ACTION_START,
        task_epoch=7,
        context_version=8,
    )
    connection.outgoing.get_nowait()

    assert await bridge.emit_realtime_effect(
        identity.session_id,
        media_pb2.REALTIME_EFFECT_KIND_DUCK_OUTPUT,
        fence,
        source_event_id="assistant_audio:duck",
        payload={"action": "duck", "gain": 0.0},
        task_epoch=7,
        context_version=8,
    )
    event = connection.outgoing.get_nowait()
    assert event.WhichOneof("event") == "realtime_effect"
    effect = event.realtime_effect
    assert effect.identity.session_id == identity.session_id
    assert effect.identity.stream_epoch == identity.stream_epoch
    assert effect.session_id == identity.session_id
    assert effect.stream_epoch == identity.stream_epoch
    assert effect.effect_id
    assert effect.source_event_id == "assistant_audio:duck"
    assert effect.effect_kind == media_pb2.REALTIME_EFFECT_KIND_DUCK_OUTPUT
    assert effect.candidate_only is False
    assert (effect.turn_id, effect.generation_id, effect.tool_epoch) == (4, 5, 6)
    assert (effect.task_epoch, effect.context_version) == (7, 8)
    assert json.loads(effect.payload) == {"action": "duck", "gain": 0.0}

    assert not await bridge.emit_realtime_effect(
        identity.session_id,
        media_pb2.REALTIME_EFFECT_KIND_DUCK_OUTPUT,
        GenerationFence(identity.session_id, 4, 4, 6),
        source_event_id="stale",
        payload={"action": "duck", "gain": 0.0},
    )
    assert not await bridge.emit_realtime_effect(
        identity.session_id,
        media_pb2.REALTIME_EFFECT_KIND_DUCK_OUTPUT,
        fence,
        source_event_id="invalid-json-number",
        payload={"action": "duck", "gain": float("nan")},
    )

    bridge._close_connection(connection)  # noqa: SLF001 - deterministic cleanup
    bridge.bridge.close(identity.session_id)


@pytest.mark.asyncio
async def test_python_executor_emits_typed_floor_effect() -> None:
    bridge = MediaBridgeGrpcServer()
    identity = SessionIdentity(
        "authoritative-floor",
        account_id="account",
        device_id="h5",
        stream_epoch=3,
    )
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    fence = GenerationFence(identity.session_id, 4, 5, 6)
    assert await bridge.emit_generation(
        identity.session_id,
        fence,
        action=media_pb2.GENERATION_ACTION_START,
        task_epoch=7,
        context_version=8,
    )
    connection.outgoing.get_nowait()

    assert await bridge.emit_floor_effect(
        identity.session_id,
        media_pb2.FLOOR_STATE_USER_HOLDS_FLOOR,
        floor_epoch=1,
        fence=fence,
        source_event_id="assistant_state:user_speaking",
        task_epoch=7,
        context_version=8,
    )
    event = connection.outgoing.get_nowait()
    assert event.WhichOneof("event") == "floor_effect"
    effect = event.floor_effect
    assert effect.identity == media_pb2.SessionIdentity(
        session_id=identity.session_id,
        account_id=identity.account_id,
        device_id=identity.device_id,
        client_type=identity.client_type,
        stream_epoch=identity.stream_epoch,
    )
    assert effect.effect_id
    assert effect.source_event_id == "assistant_state:user_speaking"
    assert effect.floor_state == media_pb2.FLOOR_STATE_USER_HOLDS_FLOOR
    assert effect.floor_epoch == 1
    assert effect.candidate_only is False
    assert (effect.turn_id, effect.generation_id, effect.tool_epoch) == (4, 5, 6)
    assert (effect.task_epoch, effect.context_version) == (7, 8)

    assert not await bridge.emit_floor_effect(
        identity.session_id,
        media_pb2.FLOOR_STATE_USER_HOLDS_FLOOR,
        floor_epoch=2,
        fence=GenerationFence(identity.session_id, 4, 4, 6),
        source_event_id="stale",
    )

    bridge._close_connection(connection)  # noqa: SLF001 - deterministic cleanup
    bridge.bridge.close(identity.session_id)


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
                traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
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
    assert bridge.bridge.get("grpc-session").traceparent == (
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
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
@pytest.mark.parametrize(
    "requested",
    (
        media_pb2.INTERACTION_AUTHORITY_UNSPECIFIED,
        media_pb2.INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE,
        media_pb2.INTERACTION_AUTHORITY_GO_SHADOW,
        media_pb2.INTERACTION_AUTHORITY_GO_AUTHORITATIVE,
        99,
    ),
)
async def test_bridge_always_returns_python_interaction_authority(requested: int) -> None:
    bridge = MediaBridgeGrpcServer()
    requests: asyncio.Queue[media_pb2.MediaToCore | None] = asyncio.Queue()
    stream = bridge.connect(_request_stream(requests), None)  # type: ignore[arg-type]
    identity = media_pb2.SessionIdentity(
        session_id=f"authority-{requested}",
        account_id="account",
        device_id="h5",
        client_type="h5",
        stream_epoch=1,
    )
    await requests.put(
        media_pb2.MediaToCore(
            hello=media_pb2.SessionHello(
                identity=identity,
                interaction_authority=requested,
            )
        )
    )

    accepted = await anext(stream)
    assert (
        accepted.accepted.interaction_authority
        == media_pb2.INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE
    )
    assert bridge.bridge.get(identity.session_id) is not None

    await requests.put(None)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_duplicate_generation_start_is_not_sent_to_the_device() -> None:
    bridge = MediaBridgeGrpcServer()
    connection = bridge._open_connection(SessionIdentity("dup-start"))
    fence = GenerationFence("dup-start", 2, 2, 0, 1)
    assert await bridge.emit_generation(
        "dup-start",
        fence,
        action=media_pb2.GENERATION_ACTION_START,
        reason="output_generation_start",
    )
    first = connection.outgoing.get_nowait()
    assert first.generation.action == media_pb2.GENERATION_ACTION_START
    assert first.generation.turn_id == 2
    assert first.generation.generation_id == 2
    assert first.generation.reason == "output_generation_start"
    assert await bridge.emit_generation(
        "dup-start",
        fence,
        action=media_pb2.GENERATION_ACTION_START,
        reason="user_turn_committed",
    )
    with pytest.raises(asyncio.QueueEmpty):
        connection.outgoing.get_nowait()
    newer = GenerationFence("dup-start", 2, 3, 0, 1)
    assert await bridge.emit_generation(
        "dup-start",
        newer,
        action=media_pb2.GENERATION_ACTION_START,
        reason="auxiliary_output",
    )
    second = connection.outgoing.get_nowait()
    assert second.generation.generation_id == 3
    assert second.generation.reason == "auxiliary_output"


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


def test_active_bridge_rejects_changed_runtime_authority_without_disconnect() -> None:
    bridge = MediaBridgeGrpcServer()
    identity = SessionIdentity(
        "grpc-authority-reconnect",
        account_id="account-a",
        participant_id="participant-a",
        device_id="device-a",
        client_type="device",
        stream_epoch=1,
        subject_id="",
        binding_id="binding-a",
        binding_version=3,
        runtime_profile_version=27,
    )
    first = bridge._open_connection(identity)  # noqa: SLF001 - transport seam

    with pytest.raises(ValueError, match="reconnect authority changed"):
        bridge._open_connection(  # noqa: SLF001 - transport seam
            SessionIdentity(
                "grpc-authority-reconnect",
                account_id="account-a",
                participant_id="participant-a",
                device_id="device-a",
                client_type="device",
                stream_epoch=2,
                subject_id="subject-a",
                binding_id="binding-a",
                binding_version=3,
                runtime_profile_version=28,
            )
        )

    assert bridge._connections[identity.session_id] is first  # noqa: SLF001
    assert first.closed is False
    assert first.session.identity == identity


@pytest.mark.asyncio
async def test_overflow_delivers_terminal_before_runtime_cancellation_finishes() -> None:
    cancellation_started = asyncio.Event()
    allow_cancellation_to_finish = asyncio.Event()

    async def on_downlink_overflow(_session) -> None:
        cancellation_started.set()
        await allow_cancellation_to_finish.wait()

    bridge = MediaBridgeGrpcServer(
        max_pending_messages=1,
        on_downlink_overflow=on_downlink_overflow,
    )
    requests: asyncio.Queue[media_pb2.MediaToCore | None] = asyncio.Queue()
    identity = media_pb2.SessionIdentity(
        session_id="overflow-terminal-session",
        account_id="account",
        device_id="h5",
        client_type="h5",
        stream_epoch=1,
    )
    stream = bridge.connect(_request_stream(requests), None)  # type: ignore[arg-type]
    await requests.put(media_pb2.MediaToCore(hello=media_pb2.SessionHello(identity=identity)))
    await anext(stream)
    connection = bridge._connections["overflow-terminal-session"]
    stale_message = media_pb2.CoreToMedia(error=media_pb2.CoreError(code="one", message="stale"))
    overflow_message = media_pb2.CoreToMedia(
        error=media_pb2.CoreError(code="three", message="overflow")
    )

    assert await bridge._enqueue(connection, stale_message)
    stale = await anext(stream)
    assert stale == stale_message
    assert await bridge.emit_generation(
        "overflow-terminal-session",
        GenerationFence("overflow-terminal-session", 1, 1, 0),
        action=media_pb2.GENERATION_ACTION_START,
    )
    overflow = asyncio.create_task(bridge._enqueue(connection, overflow_message))
    await asyncio.wait_for(cancellation_started.wait(), timeout=0.1)
    terminal = await asyncio.wait_for(anext(stream), timeout=0.1)
    assert terminal.generation.action == media_pb2.GENERATION_ACTION_CANCEL
    assert not overflow.done()

    allow_cancellation_to_finish.set()
    assert not await asyncio.wait_for(overflow, timeout=0.1)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=0.1)


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


@pytest.mark.asyncio
async def test_pcm_waits_for_new_epoch_and_preserves_generation_source_clock() -> None:
    bridge = MediaBridgeGrpcServer()
    first_identity = SessionIdentity("resume-pcm", stream_epoch=1)
    first = bridge._open_connection(first_identity)
    fence = GenerationFence("resume-pcm", 1, 1, 0)
    assert await bridge.emit_generation(
        first_identity.session_id,
        fence,
        action=media_pb2.GENERATION_ACTION_START,
    )
    await first.outgoing.get()  # generation.started
    assert await bridge.emit_pcm(
        first_identity.session_id,
        PCMFrame(
            identity=first_identity,
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            sequence=0,
            source_start_sample=0,
            frame_samples=1,
            pcm_s16le=b"\x00\x00",
        ),
    )
    await first.outgoing.get()
    bridge._close_connection(first)

    pending = asyncio.create_task(
        bridge.emit_pcm_when_connected(
            first_identity.session_id,
            PCMFrame(
                identity=first_identity,
                turn_id=1,
                generation_id=1,
                tool_epoch=0,
                sequence=1,
                source_start_sample=1,
                frame_samples=1,
                pcm_s16le=b"\x01\x00",
            ),
            timeout_s=0.2,
        )
    )
    await asyncio.sleep(0.02)
    assert not pending.done()
    second = bridge._open_connection(SessionIdentity("resume-pcm", stream_epoch=2))
    assert await pending
    resumed = await second.outgoing.get()
    assert resumed.audio.identity.stream_epoch == 2
    assert resumed.audio.sequence == 1
    assert resumed.audio.source_start_sample == 1


@pytest.mark.asyncio
async def test_hello_capabilities_carry_negotiated_audio_mode() -> None:
    captured: list[SessionIdentity] = []
    audio_seen = asyncio.Event()

    async def on_session_connected(session) -> None:
        captured.append(session.identity)

    async def on_audio_frame(_session, frame) -> None:
        audio_seen.set()

    bridge = MediaBridgeGrpcServer(
        on_session_connected=on_session_connected,
        on_audio_frame=on_audio_frame,
    )
    requests: asyncio.Queue[media_pb2.MediaToCore | None] = asyncio.Queue()
    identity = media_pb2.SessionIdentity(
        session_id="device-audio-mode",
        account_id="account",
        device_id="dev-1",
        client_type="device",
        stream_epoch=1,
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_version=1,
    )
    stream = bridge.connect(_request_stream(requests), None)  # type: ignore[arg-type]
    await requests.put(
        media_pb2.MediaToCore(
            hello=media_pb2.SessionHello(
                identity=identity,
                capabilities={"audio_mode": "interrupt_assist"},
            )
        )
    )
    await anext(stream)
    assert captured[0].audio_mode == "interrupt_assist"

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
    await asyncio.wait_for(audio_seen.wait(), timeout=1)
    await requests.put(None)


def test_require_identity_ignores_hello_audio_mode() -> None:
    identity = SessionIdentity(
        "device-audio-mode-fence",
        account_id="account",
        device_id="dev-1",
        client_type="device",
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_version=1,
        audio_mode="interrupt_assist",
    )
    bridge = MediaBridgeGrpcServer()
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    proto = media_pb2.SessionIdentity(
        session_id=identity.session_id,
        account_id=identity.account_id,
        device_id=identity.device_id,
        client_type=identity.client_type,
        stream_epoch=identity.stream_epoch,
        binding_id=identity.binding_id,
        binding_version=identity.binding_version,
        runtime_profile_version=identity.runtime_profile_version,
    )
    MediaBridgeGrpcServer._require_identity(connection, proto)  # noqa: SLF001
    proto.session_id = "other-session"
    with pytest.raises(ValueError, match="media event identity does not match the session"):
        MediaBridgeGrpcServer._require_identity(connection, proto)  # noqa: SLF001


@pytest.mark.asyncio
async def test_vad_event_forwards_rms_as_near_end_rms() -> None:
    seen: list[SpeechSegment] = []

    async def on_speech(_session, segment, _detected_monotonic_ms: int = 0) -> None:
        seen.append(segment)

    bridge = MediaBridgeGrpcServer(on_speech_segment=on_speech)
    identity = SessionIdentity("vad-rms", account_id="account", device_id="device")
    connection = bridge._open_connection(identity)  # noqa: SLF001 - transport seam under test
    proto_identity = media_pb2.SessionIdentity(
        session_id=identity.session_id,
        account_id=identity.account_id,
        device_id=identity.device_id,
        client_type=identity.client_type,
        stream_epoch=identity.stream_epoch,
    )
    try:
        await bridge._handle_request(  # noqa: SLF001 - transport seam under test
            connection,
            media_pb2.MediaToCore(
                vad=media_pb2.VadEvent(
                    identity=proto_identity,
                    type=media_pb2.VAD_EVENT_SPEECH_START,
                    sample_position=0,
                    probability=1.0,
                    rms=12.5,
                )
            ),
        )
        assert len(seen) == 1
        assert seen[0].kind is SegmentKind.VAD
        assert seen[0].near_end_rms == pytest.approx(12.5)
    finally:
        bridge._close_connection(connection)  # noqa: SLF001 - deterministic cleanup
        bridge.bridge.close(identity.session_id)


def test_downlink_pacing_reports_produced_audio_against_the_wall_clock(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The device plays in real time, so the informative number is produced
    audio against the wall clock it arrived in.  Real epoch 1948: ~38.9s of
    audio arrived over ~39.3s with no queue standing and the operator heard
    stuttering that the delivery ledger recorded as a complete playback.
    """

    from services.agent.src.voice_core import media_bridge_server as mbs

    clock = iter([100.0, 100.5, 104.0])
    monkeypatch.setattr(mbs.time, "monotonic", lambda: next(clock))
    session = mbs.MediaBridgeSession(
        identity=SessionIdentity("pacing", account_id="account", device_id="h5")
    )
    def frame(sequence: int, samples: int, *, final: bool) -> mbs.PCMFrame:
        return mbs.PCMFrame(
                identity=session.identity,
                turn_id=1,
                generation_id=1,
                tool_epoch=0,
                sequence=sequence,
                source_start_sample=(0, 12_000, 24_000)[sequence],
            frame_samples=samples,
            pcm_s16le=b"\x00" * (samples * 2),
            final=final,
        )

    fence = GenerationFence(
        session_id=session.identity.session_id,
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        # Generation session_epoch and transport stream_epoch are separate
        # namespaces.  A fresh runtime generation starts at session_epoch 0;
        # the identity's stream_epoch is transport metadata only.
        session_epoch=0,
    )
    session.generation.advance(fence)
    assert session.reset_downlink_generation(fence)
    # accept_downlink appends before measuring, so the queue depth it samples is
    # the depth the transport-visible queue actually has.
    with caplog.at_level("INFO"):
        for sequence, samples, final in ((0, 12_000, False), (1, 12_000, False), (2, 24_000, True)):
            pending = frame(sequence, samples, final=final)
            assert session.accept_downlink(pending)

    pacing = [record.message for record in caplog.records if "downlink pacing" in record.message]
    assert len(pacing) == 1
    # 48000 samples at 24 kHz is 2000 ms of audio produced across 4000 ms.
    assert "audio_ms=2000" in pacing[0]
    assert "wall_ms=4000" in pacing[0]
    assert "max_gap_ms=3500" in pacing[0]
    assert "measurement=post_pacer_send" in pacing[0]
    assert "send_audio_ratio=0.50" in pacing[0]
    assert "reason=final_frame" in pacing[0]
    assert session._pacing_queue_high_water == 3
