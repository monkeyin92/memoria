from __future__ import annotations

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.asr_stream_supervisor import ASRStreamSupervisor
from services.agent.src.voice_core.device_protocol import DeviceCommand, DeviceCommandAck
from services.agent.src.voice_core.generation_controller import GenerationController
from services.agent.src.voice_core.media_bridge_server import (
    MediaBridgeServer,
    PCMFrame,
)
from services.agent.src.voice_core.media_protocol import (
    AudioEncoding,
    AudioFormat,
    AudioFrame,
    MediaEnvelope,
    SessionIdentity,
)
from services.agent.src.voice_core.playback_ledger import PlaybackLedger, PlaybackSpan
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SpeechTimeline,
    asr_result_to_segment,
)


def _fence(generation: int = 1, *, turn: int = 1) -> GenerationFence:
    return GenerationFence("session", turn, generation, 0)


def test_speech_timeline_maps_asr_revisions_and_drops_late_results() -> None:
    timeline = SpeechTimeline()
    partial = ASRResult(
        task_epoch=1,
        sentence_id="sentence-1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=320,
        text="我今",
        is_final=False,
    )
    final = ASRResult(
        task_epoch=2,
        sentence_id="sentence-1",
        revision=2,
        capture_start_sample=0,
        capture_end_sample=320,
        text="我今天",
        is_final=True,
    )
    assert timeline.add(asr_result_to_segment(partial, session_id="session"))
    assert timeline.add(asr_result_to_segment(final, session_id="session"))
    assert timeline.canonical_text(stream_epoch=1, start_sample=0, end_sample=320) == "我今天"
    assert not timeline.add(
        asr_result_to_segment(
            ASRResult(
                task_epoch=3,
                sentence_id="late",
                revision=1,
                capture_start_sample=0,
                capture_end_sample=160,
                text="迟到",
                is_final=True,
            ),
            session_id="session",
        )
    )
    equal_revision_partial = ASRResult(
        task_epoch=4,
        sentence_id="sentence-1",
        revision=2,
        capture_start_sample=0,
        capture_end_sample=320,
        text="我",
        is_final=False,
    )
    assert not timeline.add(asr_result_to_segment(equal_revision_partial, session_id="session"))


def test_generation_controller_never_allows_old_response_cleanup_or_result() -> None:
    controller = GenerationController(session_id="session")
    first = controller.start_turn()
    cancelled: list[str] = []
    lease = controller.install_response(first, lambda: cancelled.append("first"))
    assert lease is not None
    second = controller.cancel(first)
    assert second is not None
    assert cancelled == ["first"]
    assert controller.gate(first, "old") is None
    assert controller.clear_response(first) is False

    third = controller.start_generation()
    assert controller.install_response(third, lambda: cancelled.append("third")) is not None
    assert controller.gate(third, "new") == "new"


def test_playback_ledger_commits_only_fully_acknowledged_spans() -> None:
    fence = _fence()
    ledger = PlaybackLedger()
    ledger.start(fence)
    assert ledger.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=0,
            text_end=2,
            audio_start_sample=0,
            audio_end_sample=320,
            text="你好",
        )
    )
    assert ledger.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=2,
            text_end=4,
            audio_start_sample=320,
            audio_end_sample=640,
            text="世界",
        )
    )
    assert ledger.actual_heard_text(fence) == ""
    ledger.acknowledge(fence, 321, approximate=True)
    assert ledger.actual_heard_text(fence) == "你好"
    assert ledger.actual_heard_text(_fence(generation=2)) == ""


def test_media_v1_envelope_and_audio_metadata_round_trip() -> None:
    identity = SessionIdentity(session_id="session", stream_epoch=2, client_type="h5")
    audio = AudioFrame(
        identity=identity,
        sequence=7,
        capture_start_sample=320,
        frame_samples=320,
        payload=b"\x00\x01",
        discontinuity=True,
    )
    assert audio.capture_end_sample == 640
    envelope = MediaEnvelope.create(
        type="audio",
        event_id="evt-1",
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        sequence=audio.sequence,
        payload=audio.to_payload(),
    )
    decoded = MediaEnvelope.decode(envelope.encode())
    assert decoded == envelope
    assert decoded.payload["payload_b64"] == "AAE="
    assert AudioFormat(AudioEncoding.PCM_S16LE, 16_000).frame_ms == 20


def test_media_bridge_drops_stale_audio_and_restarts_on_new_epoch() -> None:
    server = MediaBridgeServer(max_pending_audio_frames=2)
    identity = SessionIdentity(session_id="session", stream_epoch=1)
    bridge = server.open(identity)
    frame = PCMFrame(
        identity=identity,
        turn_id=0,
        generation_id=0,
        tool_epoch=0,
        sequence=0,
        source_start_sample=0,
        frame_samples=2,
        pcm_s16le=b"\x00\x00\x01\x00",
    )
    assert bridge.accept_downlink(frame)
    assert not bridge.accept_downlink(frame)
    assert bridge.stale_downlink_count == 1

    next_identity = SessionIdentity(session_id="session", stream_epoch=2)
    assert bridge.reconnect(next_identity)
    assert bridge.identity == next_identity
    assert bridge.accept_uplink(
        AudioFrame(
            identity=next_identity,
            sequence=0,
            capture_start_sample=0,
            frame_samples=2,
            payload=b"\x00\x00\x01\x00",
        )
    )


def test_media_bridge_does_not_advance_generation_from_unannounced_audio() -> None:
    server = MediaBridgeServer()
    identity = SessionIdentity("future-audio", stream_epoch=1)
    bridge = server.open(identity)
    future = PCMFrame(
        identity=identity,
        turn_id=1,
        generation_id=9,
        tool_epoch=0,
        sequence=0,
        source_start_sample=0,
        frame_samples=2,
        pcm_s16le=b"\x00\x00\x01\x00",
    )
    assert not bridge.accept_downlink(future)
    assert bridge.fence == GenerationFence("future-audio", 0, 0, 0)


def test_media_bridge_reconnect_cannot_change_device_identity() -> None:
    server = MediaBridgeServer()
    bridge = server.open(SessionIdentity("identity-session", device_id="doll-1"))
    assert not bridge.reconnect(
        SessionIdentity("identity-session", device_id="doll-2", stream_epoch=2)
    )


def test_asr_supervisor_uses_watermark_and_task_epoch_on_reconnect() -> None:
    supervisor = ASRStreamSupervisor(reconnect_audio_ms=500)
    assert supervisor.start_task() == 1
    assert supervisor.record_audio(start_sample=0, frame_samples=800)
    result = ASRResult(
        task_epoch=1,
        sentence_id="s1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=640,
        text="你好",
        is_final=True,
    )
    assert supervisor.accept_result(result, session_id="session")
    supervisor.mark_committed(640)
    assert not supervisor.accept_result(result, session_id="session")
    assert supervisor.replay_start_sample() == 640
    assert supervisor.reconnect(stream_epoch=2)
    assert supervisor.task_epoch == 2


def test_device_commands_are_allowlisted_and_expire_without_cloud_dependency() -> None:
    command = DeviceCommand(
        command_id="cmd-1",
        topic="face.expression.set",
        ttl_ms=3_000,
        issued_monotonic_ms=100,
        payload={"expression": "happy"},
    )
    assert not command.expired(3_099)
    assert command.expired(3_100)
    assert b"device.command" in command.to_json()
    assert b"device.command_ack" in DeviceCommandAck(
        command_id="cmd-1",
        status="applied",
        device_monotonic_ms=200,
    ).to_json()


def test_media_bridge_stop_is_generation_gated_and_idempotent() -> None:
    server = MediaBridgeServer()
    bridge = server.open(SessionIdentity("stop-session", stream_epoch=1))
    event = MediaEnvelope.create(
        type="client.stop_assistant",
        event_id="evt-1",
        session_id="stop-session",
        stream_epoch=1,
        sequence=0,
        payload={"idempotency_key": "stop-1"},
    )
    assert server.accept_client_event(event)
    assert bridge.fence.generation_id == 1
    assert server.accept_client_event(event)


def test_media_bridge_stop_uses_envelope_event_id_when_payload_key_is_absent() -> None:
    server = MediaBridgeServer()
    bridge = server.open(SessionIdentity("stop-event-id", stream_epoch=1))
    event = MediaEnvelope.create(
        type="client.stop_assistant",
        event_id="evt-stable-stop",
        session_id="stop-event-id",
        stream_epoch=1,
        sequence=0,
        payload={"reason": "user_button"},
    )
    assert server.accept_client_event(event)
    assert bridge.fence.generation_id == 1
    assert server.accept_client_event(event)
    assert bridge.fence.generation_id == 1


def test_media_bridge_rejects_audio_discontinuity_and_sample_gap() -> None:
    server = MediaBridgeServer()
    bridge = server.open(SessionIdentity("gap-session", stream_epoch=1))
    first = AudioFrame(
        identity=bridge.identity,
        sequence=0,
        capture_start_sample=0,
        frame_samples=160,
        payload=b"pcm",
    )
    assert bridge.accept_uplink(first)
    gap = AudioFrame(
        identity=bridge.identity,
        sequence=1,
        capture_start_sample=320,
        frame_samples=160,
        payload=b"pcm",
    )
    assert not bridge.accept_uplink(gap)
    discontinuity = AudioFrame(
        identity=bridge.identity,
        sequence=1,
        capture_start_sample=160,
        frame_samples=160,
        payload=b"pcm",
        discontinuity=True,
    )
    assert not bridge.accept_uplink(discontinuity)

    bridge.uplink.clear()
    assert not bridge.accept_uplink(
        AudioFrame(
            identity=bridge.identity,
            sequence=2,
            capture_start_sample=320,
            frame_samples=160,
            payload=b"pcm",
        )
    )
