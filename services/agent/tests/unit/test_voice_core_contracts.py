from __future__ import annotations

import base64
import json
import logging
import time

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.asr_stream_supervisor import (
    ASRDecisionReason,
    ASRStreamSupervisor,
)
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
    ASRWordTiming,
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


def test_playback_ledger_rejects_progress_beyond_received_audio() -> None:
    fence = _fence()
    ledger = PlaybackLedger()
    ledger.start(fence)
    assert ledger.register_audio(fence, 0, 0, 320)
    assert ledger.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=0,
            text_end=2,
            audio_start_sample=0,
            audio_end_sample=320,
            text="你好",
            sequence=0,
        )
    )

    # Neither a sequence the bridge has not sent nor a sample watermark past
    # the received frame may promote actual-heard text.
    assert ledger.acknowledge(fence, 321, received_sequence=0) == ()
    assert ledger.actual_heard_text(fence) == ""
    assert ledger.acknowledge(fence, 320, received_sequence=1) == ()
    assert ledger.actual_heard_text(fence) == ""

    assert ledger.acknowledge(fence, 320, received_sequence=0)
    assert ledger.actual_heard_text(fence) == "你好"


def test_playback_ledger_rejects_forged_terminal_receipt() -> None:
    fence = _fence()
    ledger = PlaybackLedger()
    ledger.start(fence)
    assert ledger.register_audio(fence, 0, 0, 320)

    assert ledger.acknowledge(
        fence,
        321,
        received_sequence=0,
        terminal=True,
    ) == ()
    assert not ledger.terminal_received(fence)
    assert not ledger.is_playback_complete(fence)

    assert ledger.acknowledge(
        fence,
        320,
        received_sequence=0,
        terminal=True,
    ) == ()
    assert ledger.terminal_received(fence)
    assert ledger.is_playback_complete(fence)

    stale_before = ledger.stale_ack_count
    assert ledger.acknowledge(fence, 320, received_sequence=0, terminal=False) == ()
    assert ledger.acknowledge(fence, 320, received_sequence=0) == ()
    assert ledger.stale_ack_count == stale_before + 2
    assert ledger.terminal_received(fence)


def test_playback_ledger_applies_existing_ack_to_late_provider_alignment() -> None:
    fence = _fence()
    ledger = PlaybackLedger()
    ledger.start(fence)
    assert ledger.register_audio(fence, 0, 0, 320)
    assert ledger.acknowledge(fence, 320, received_sequence=0) == ()

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
    assert ledger.actual_heard_text(fence) == "你好"
    assert ledger.is_fully_acknowledged(fence)


def test_playback_ledger_completes_playback_without_timed_text() -> None:
    fence = _fence()
    ledger = PlaybackLedger()
    ledger.start(fence)
    assert ledger.register_audio(fence, 0, 0, 320)
    assert ledger.acknowledge(fence, 320, received_sequence=0) == ()

    assert ledger.actual_heard_text(fence) == ""
    assert ledger.is_fully_acknowledged(fence)


def test_playback_ledger_requires_all_audio_and_text_to_be_acknowledged() -> None:
    fence = _fence()
    ledger = PlaybackLedger()
    ledger.start(fence)
    assert ledger.register_audio(fence, 0, 0, 1_000)
    assert ledger.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=0,
            text_end=2,
            audio_start_sample=0,
            audio_end_sample=800,
            text="你好",
            sequence=0,
        )
    )

    assert ledger.acknowledge(fence, 800, received_sequence=0)
    assert not ledger.is_fully_acknowledged(fence)

    assert ledger.acknowledge(fence, 1_000, received_sequence=0) == ()
    assert ledger.is_fully_acknowledged(fence)


def test_media_bridge_audio_queues_are_consumable_and_generation_local() -> None:
    server = MediaBridgeServer(max_pending_audio_frames=100)
    identity = SessionIdentity("queue-session", stream_epoch=1)
    bridge = server.open(identity)

    # A consumer can sustain more than the bounded queue capacity by popping
    # and ACKing every accepted frame; accepted sequence remains monotonic.
    for sequence in range(250):
        frame = AudioFrame(
            identity=identity,
            sequence=sequence,
            capture_start_sample=sequence * 2,
            frame_samples=2,
            payload=b"\x00\x00\x01\x00",
        )
        assert bridge.accept_uplink(frame)
        assert bridge.pop_uplink(sequence) == frame
        assert bridge.ack_uplink(sequence)
    assert not bridge.uplink
    assert bridge.overflow_count == 0

    for sequence in range(250):
        frame = PCMFrame(
            identity=identity,
            turn_id=0,
            generation_id=0,
            tool_epoch=0,
            sequence=sequence,
            source_start_sample=sequence * 2,
            frame_samples=2,
            pcm_s16le=b"\x00\x00\x01\x00",
        )
        assert bridge.accept_downlink(frame)
        assert bridge.pop_downlink(sequence) == frame
        assert bridge.ack_downlink(sequence)

    next_fence = bridge.generation.start_generation()
    second = PCMFrame(
        identity=identity,
        turn_id=next_fence.turn_id,
        generation_id=next_fence.generation_id,
        tool_epoch=next_fence.tool_epoch,
        sequence=0,
        source_start_sample=0,
        frame_samples=2,
        pcm_s16le=b"\x00\x00\x01\x00",
    )
    assert bridge.accept_downlink(second)
    assert bridge.last_downlink_sequence == 0
    assert bridge.overflow_count == 0


def test_media_v1_envelope_and_audio_metadata_round_trip() -> None:
    identity = SessionIdentity(session_id="session", stream_epoch=2, client_type="h5")
    audio = AudioFrame(
        identity=identity,
        sequence=7,
        capture_start_sample=320,
        frame_samples=320,
        payload=b"\x00\x01" * 320,
        discontinuity=True,
    )
    assert audio.capture_end_sample == 640
    envelope = MediaEnvelope.create(
        type="audio",
        event_id="evt-1",
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        sequence=audio.sequence,
        task_epoch=5,
        context_version=7,
        session_epoch=9,
        payload=audio.to_payload(),
    )
    decoded = MediaEnvelope.decode(envelope.encode())
    assert decoded == envelope
    assert decoded.payload["payload_b64"] == base64.b64encode(audio.payload).decode("ascii")
    assert decoded.task_epoch == 5
    assert decoded.context_version == 7
    assert decoded.session_epoch == 9
    assert AudioFormat(AudioEncoding.PCM_S16LE, 16_000).frame_ms == 20


def test_session_identity_device_authority_fence_allows_empty_subject() -> None:
    """A device may carry an empty runtime subject (unknown_safe), while
    binding, runtime profile version and stream epoch stay mandatory."""

    identity = SessionIdentity(
        "device-empty-subject",
        device_id="dev-1",
        client_type="device",
        subject_id="",
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_version=19,
        stream_epoch=143,
    )
    assert identity.subject_id == ""
    assert identity.binding_id == "binding-1"
    assert identity.runtime_profile_version == 19

    with pytest.raises(ValueError, match="authority fence"):
        SessionIdentity(
            "device-missing-binding",
            device_id="dev-1",
            client_type="device",
            runtime_profile_version=19,
        )
    with pytest.raises(ValueError, match="device-only"):
        SessionIdentity(
            "h5-with-fence",
            client_type="h5",
            binding_id="binding-1",
            binding_version=1,
            runtime_profile_version=19,
        )
    with pytest.raises(ValueError, match="device-only"):
        SessionIdentity(
            "h5-with-subject",
            client_type="h5",
            subject_id="person-a",
        )


@pytest.mark.parametrize(
    "subject_id",
    (" ", "subject with spaces", "/subject", "_subject", "s" * 129),
)
def test_session_identity_rejects_malformed_nonempty_device_subject(
    subject_id: str,
) -> None:
    with pytest.raises(ValueError, match="subject_id"):
        SessionIdentity(
            "device-malformed-subject",
            device_id="dev-1",
            client_type="device",
            subject_id=subject_id,
            binding_id="binding-1",
            binding_version=1,
            runtime_profile_version=19,
        )


def test_media_v1_envelope_keeps_v1_compatibility_for_missing_and_future_versions() -> None:
    envelope = MediaEnvelope.create(
        type="client.trace",
        event_id="evt-legacy",
        session_id="session",
        stream_epoch=1,
        sequence=0,
        payload={},
    )
    legacy = envelope.to_dict()
    legacy.pop("task_epoch", None)
    legacy.pop("context_version", None)
    legacy["future_extension"] = {"ignored": True}

    decoded = MediaEnvelope.decode(json.dumps(legacy))

    assert decoded.task_epoch == 0
    assert decoded.context_version == 0
    assert decoded.type == "client.trace"


@pytest.mark.parametrize(
    ("frame_samples", "payload", "message"),
    [
        (0, b"\x00\x00", "frame_samples must be positive"),
        (1, b"", "audio payload must not be empty"),
        (160, b"\x00\x00", "payload length"),
    ],
)
def test_audio_frame_rejects_invalid_pcm_shape(
    frame_samples: int, payload: bytes, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        AudioFrame(
            identity=SessionIdentity("bad-audio"),
            sequence=0,
            capture_start_sample=0,
            frame_samples=frame_samples,
            payload=payload,
        )


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


def test_media_bridge_downlink_rejection_diagnostics_are_rate_limited(caplog) -> None:
    server = MediaBridgeServer()
    identity = SessionIdentity("reject-log", stream_epoch=1)
    bridge = server.open(identity)
    stale = PCMFrame(
        identity=identity,
        turn_id=1,
        generation_id=9,
        tool_epoch=0,
        sequence=0,
        source_start_sample=0,
        frame_samples=2,
        pcm_s16le=b"\x00\x00\x01\x00",
    )

    with caplog.at_level(logging.WARNING, logger="services.agent.src.voice_core.media_bridge_server"):
        assert not bridge.accept_downlink(stale)
        assert not bridge.accept_downlink(stale)
    assert bridge.stale_downlink_count == 2
    rejection_logs = [r for r in caplog.records if "media downlink rejected" in r.getMessage()]
    assert len(rejection_logs) == 1
    assert "reason=fence_mismatch_or_generation_rejected" in rejection_logs[0].getMessage()

    # After the rate-limit window passes the next rejection logs again.
    bridge.last_downlink_reject_log = time.monotonic() - 10.0
    with caplog.at_level(logging.WARNING, logger="services.agent.src.voice_core.media_bridge_server"):
        assert not bridge.accept_downlink(stale)
    rejection_logs = [r for r in caplog.records if "media downlink rejected" in r.getMessage()]
    assert len(rejection_logs) == 2


def test_media_bridge_downlink_rejection_logs_generation_gate_state(caplog) -> None:
    server = MediaBridgeServer()
    identity = SessionIdentity("reject-gate", stream_epoch=1)
    bridge = server.open(identity)
    bridge.generation_active = False
    frame = PCMFrame(
        identity=identity,
        turn_id=5,
        generation_id=5,
        tool_epoch=0,
        sequence=0,
        source_start_sample=0,
        frame_samples=2,
        pcm_s16le=b"\x00\x00\x01\x00",
    )

    with caplog.at_level(logging.WARNING, logger="services.agent.src.voice_core.media_bridge_server"):
        assert not bridge.accept_downlink(frame)
    messages = [r.getMessage() for r in caplog.records if "media downlink rejected" in r.getMessage()]
    assert len(messages) == 1
    assert "reason=generation_not_active" in messages[0]
    assert "generation_active=False" in messages[0]


def test_media_bridge_downlink_requires_complete_fence_session_epoch() -> None:
    server = MediaBridgeServer()
    identity = SessionIdentity("epoch-session", stream_epoch=1)
    bridge = server.open(identity)
    # Reproduce the production GENERATION_ACTION_START path: the controller
    # advances with an authoritative complete fence under a non-zero session
    # epoch, so downlink frames must carry the same epoch to match.
    authoritative = GenerationFence(
        session_id="epoch-session",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    bridge.generation.advance(authoritative)
    assert bridge.reset_downlink_generation(authoritative)
    bridge.generation_active = True

    def _frame(session_epoch: int) -> PCMFrame:
        return PCMFrame(
            identity=identity,
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            session_epoch=session_epoch,
            sequence=0,
            source_start_sample=0,
            frame_samples=2,
            pcm_s16le=b"\x00\x00\x01\x00",
        )

    # A frame stamped with the previous epoch cannot cross the switch.
    assert not bridge.accept_downlink(_frame(0))
    assert bridge.stale_downlink_count == 1
    # A frame carrying the authoritative complete fence is accepted.
    assert bridge.accept_downlink(_frame(1))
    assert bridge.last_downlink_sequence == 0


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
    expanded = ASRResult(
        task_epoch=2,
        sentence_id="s1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=800,
        text="你好世界",
        is_final=True,
    )
    decision = supervisor.accept_result(expanded, session_id="session")
    assert not decision
    assert decision.reason is ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING
    late_old_task = ASRResult(
        task_epoch=1,
        sentence_id="s1",
        revision=2,
        capture_start_sample=0,
        capture_end_sample=800,
        text="旧结果",
        is_final=True,
    )
    assert not supervisor.accept_result(late_old_task, session_id="session")
    assert supervisor.reconnect(stream_epoch=2)
    assert supervisor.task_epoch == 2


def test_asr_supervisor_accepts_out_of_order_non_overlapping_finals() -> None:
    supervisor = ASRStreamSupervisor(reconnect_audio_ms=500)
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=800)
    late_start = ASRResult(
        task_epoch=1,
        sentence_id="s2",
        revision=1,
        capture_start_sample=320,
        capture_end_sample=640,
        text="后半句",
        is_final=True,
    )
    assert supervisor.accept_result(late_start, session_id="session")
    earlier = ASRResult(
        task_epoch=1,
        sentence_id="s1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=320,
        text="前半句",
        is_final=True,
    )
    assert supervisor.accept_result(earlier, session_id="session")
    assert supervisor.last_emitted_final_sample == 640


def test_asr_supervisor_higher_revision_replaces_same_interval() -> None:
    supervisor = ASRStreamSupervisor(reconnect_audio_ms=500)
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=800)
    original = ASRResult(
        task_epoch=1,
        sentence_id="s1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=320,
        text="你好",
        is_final=True,
    )
    assert supervisor.accept_result(original, session_id="session")
    corrected = ASRResult(
        task_epoch=1,
        sentence_id="s1",
        revision=2,
        capture_start_sample=0,
        capture_end_sample=320,
        text="你好呀",
        is_final=True,
    )
    assert supervisor.accept_result(corrected, session_id="session")
    duplicate = supervisor.accept_result(corrected, session_id="session")
    assert not duplicate
    assert duplicate.reason is ASRDecisionReason.TRANSPORT_DUPLICATE


def test_asr_supervisor_normalizes_committed_cross_task_extension_with_word_timing() -> None:
    supervisor = ASRStreamSupervisor(reconnect_audio_ms=500)
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=800)
    first = ASRResult(
        task_epoch=1,
        sentence_id="s1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=320,
        text="你好",
        is_final=True,
    )
    assert supervisor.accept_result(first, session_id="session")
    supervisor.mark_committed(320)
    extension = ASRResult(
        task_epoch=2,
        sentence_id="s1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=800,
        text="你好世界",
        is_final=True,
        word_timings=(
            ASRWordTiming("你", 0, 160),
            ASRWordTiming("好", 160, 320),
            ASRWordTiming("世", 320, 560),
            ASRWordTiming("界", 560, 800),
        ),
    )
    decision = supervisor.accept_result(extension, session_id="session")
    assert decision
    assert decision.accepted is not None
    assert decision.accepted.capture_start_sample == 320
    assert decision.accepted.capture_end_sample == 800
    assert decision.accepted.text == "世界"
    assert decision.accepted.timeline_segment_id == "s1:tail:320"


def test_asr_supervisor_orders_reconnect_revision_above_old_task_revision() -> None:
    supervisor = ASRStreamSupervisor()
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=640)
    first = ASRResult(1, "s1", 1, 0, 320, "你号", True)
    old_correction = ASRResult(1, "s1", 2, 0, 320, "你好", True)
    expanded = ASRResult(2, "s1", 1, 0, 640, "你好世界", True)
    late_old = ASRResult(1, "s1", 3, 0, 640, "旧结果", True)

    assert supervisor.accept_result(first, session_id="session")
    assert supervisor.accept_result(old_correction, session_id="session")
    assert supervisor.accept_result(expanded, session_id="session")
    assert (
        supervisor.timeline.canonical_text(
            stream_epoch=1,
            start_sample=0,
            end_sample=640,
        )
        == "你好世界"
    )
    assert not supervisor.accept_result(late_old, session_id="session")


def test_asr_supervisor_accepts_new_task_same_interval_correction() -> None:
    supervisor = ASRStreamSupervisor()
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=320)
    first = ASRResult(1, "s1", 2, 0, 320, "你号", True)
    correction = ASRResult(2, "s1", 1, 0, 320, "你好", True)

    assert supervisor.accept_result(first, session_id="session")
    assert supervisor.accept_result(correction, session_id="session")
    assert (
        supervisor.timeline.canonical_text(
            stream_epoch=1,
            start_sample=0,
            end_sample=320,
        )
        == "你好"
    )


def test_asr_supervisor_rejects_cross_task_same_range_replay() -> None:
    supervisor = ASRStreamSupervisor(reconnect_audio_ms=500)
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=800)
    first = ASRResult(
        task_epoch=1,
        sentence_id="s1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=320,
        text="你好",
        is_final=True,
    )
    assert supervisor.accept_result(first, session_id="session")
    replay = ASRResult(
        task_epoch=2,
        sentence_id="s1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=320,
        text="你好",
        is_final=True,
    )
    assert not supervisor.accept_result(replay, session_id="session")


def test_asr_supervisor_rejects_cross_sentence_ambiguous_overlap() -> None:
    supervisor = ASRStreamSupervisor(reconnect_audio_ms=500)
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=800)
    first = ASRResult(
        task_epoch=1,
        sentence_id="s1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=640,
        text="第一句",
        is_final=True,
    )
    assert supervisor.accept_result(first, session_id="session")
    ambiguous = ASRResult(
        task_epoch=1,
        sentence_id="s2",
        revision=1,
        capture_start_sample=160,
        capture_end_sample=320,
        text="重叠",
        is_final=True,
    )
    assert not supervisor.accept_result(ambiguous, session_id="session")


def test_asr_supervisor_supersedes_cross_sentence_when_later_final_extends() -> None:
    supervisor = ASRStreamSupervisor(reconnect_audio_ms=500)
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=800)
    fragment = ASRResult(
        task_epoch=1,
        sentence_id="s1",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=320,
        text="星期几",
        is_final=True,
    )
    assert supervisor.accept_result(fragment, session_id="session")
    extended = ASRResult(
        task_epoch=1,
        sentence_id="s2",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=640,
        text="今天星期几",
        is_final=True,
    )
    assert supervisor.accept_result(extended, session_id="session")


def test_asr_supervisor_fences_old_task_with_a_different_sentence_id() -> None:
    supervisor = ASRStreamSupervisor()
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=960)
    newer = ASRResult(2, "new-sentence", 1, 320, 640, "新话轮", True)
    older = ASRResult(1, "old-sentence", 1, 0, 320, "旧话轮", True)

    assert supervisor.accept_result(newer, session_id="session")
    decision = supervisor.accept_result(older, session_id="session")
    assert not decision
    assert decision.reason is ASRDecisionReason.STALE_TASK_EPOCH


def test_rejected_new_task_result_does_not_take_authority() -> None:
    supervisor = ASRStreamSupervisor()
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=640)
    supervisor.mark_committed(320)
    rejected = ASRResult(2, "bad-replay", 1, 0, 640, "不可靠回放", True)

    decision = supervisor.accept_result(rejected, session_id="session")
    assert not decision
    assert decision.reason is ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING

    current = ASRResult(1, "current", 1, 320, 640, "当前任务", True)
    assert supervisor.accept_result(current, session_id="session")


def test_policy_rejected_new_task_does_not_take_authority() -> None:
    supervisor = ASRStreamSupervisor()
    supervisor.record_audio(start_sample=0, frame_samples=640)
    first = ASRResult(1, "first", 1, 0, 320, "第一句", True)
    assert supervisor.accept_result(first, session_id="session")

    conflict = ASRResult(2, "conflict", 1, 160, 480, "冲突", True)
    decision = supervisor.accept_result(conflict, session_id="session")
    assert not decision
    assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
    assert supervisor.latest_authoritative_task_epoch == 1

    current = ASRResult(1, "second", 1, 320, 640, "第二句", True)
    assert supervisor.accept_result(current, session_id="session")


def test_asr_supervisor_supersedes_cross_sentence_extension() -> None:
    supervisor = ASRStreamSupervisor()
    supervisor.record_audio(start_sample=0, frame_samples=1_000)
    first = ASRResult(1, "s1", 1, 0, 320, "今天", True)
    assert supervisor.accept_result(first, session_id="session")

    extended = ASRResult(2, "s2", 1, 0, 640, "今天是星期几", True)
    decision = supervisor.accept_result(extended, session_id="session")
    assert decision.accepted is extended
    assert decision.evicted_sentence_ids == ("s1",)
    assert (
        supervisor.timeline.canonical_text(
            stream_epoch=1,
            start_sample=0,
            end_sample=640,
        )
        == "今天是星期几"
    )


def test_asr_supervisor_supersedes_cross_sentence_tail_extension() -> None:
    supervisor = ASRStreamSupervisor()
    supervisor.record_audio(start_sample=0, frame_samples=1_000_000)
    first = ASRResult(1, "s1", 1, 289_600, 400_000, "今天南京", True)
    assert supervisor.accept_result(first, session_id="session")

    tail = ASRResult(
        2,
        "s2",
        1,
        342_400,
        433_600,
        "今天南京的天气怎么样",
        True,
    )
    decision = supervisor.accept_result(tail, session_id="session")
    assert decision.accepted is tail
    assert decision.evicted_sentence_ids == ("s1",)
    assert (
        supervisor.timeline.canonical_text(
            stream_epoch=1,
            start_sample=289_600,
            end_sample=433_600,
        )
        == "今天南京的天气怎么样"
    )


def test_provider_final_supersedes_shorter_rescue_interval() -> None:
    """A real provider final must replace a longer rescue stand-in.

    Reproduces production session 0703b3a8 (stream_epoch 1331): the
    mid-utterance rescue fired while the VAD segment was still open and
    registered samples 0-76_480, then FunASR's own final for 38_560-64_160
    arrived and was dropped as cross_sentence_overlap because the rescue
    interval was longer.  The transcript was lost even though the provider
    had delivered it.
    """

    supervisor = ASRStreamSupervisor()
    supervisor.record_audio(start_sample=0, frame_samples=100_000)
    rescue = ASRResult(
        1,
        "0",
        1,
        0,
        76_480,
        "救援合成结果",
        True,
        rescue_synthesized=True,
    )
    assert supervisor.accept_result(rescue, session_id="session")

    provider_final = ASRResult(1, "s1", 1, 38_560, 64_160, "今天星期几", True)
    decision = supervisor.accept_result(provider_final, session_id="session")
    assert decision.accepted is provider_final
    assert decision.evicted_sentence_ids == ("0",)
    assert (
        supervisor.timeline.canonical_text(
            stream_epoch=1,
            start_sample=38_560,
            end_sample=64_160,
        )
        == "今天星期几"
    )


def test_rescue_final_does_not_supersede_provider_final() -> None:
    """The reverse direction must stay closed: rescue never evicts a provider final."""

    supervisor = ASRStreamSupervisor()
    supervisor.record_audio(start_sample=0, frame_samples=100_000)
    provider_final = ASRResult(1, "s1", 1, 38_560, 64_160, "今天星期几", True)
    assert supervisor.accept_result(provider_final, session_id="session")

    rescue = ASRResult(
        1,
        "0",
        1,
        0,
        76_480,
        "救援合成结果",
        True,
        rescue_synthesized=True,
    )
    decision = supervisor.accept_result(rescue, session_id="session")
    assert decision.accepted is None
    assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP


def test_same_task_higher_revision_can_move_sentence_start_forward() -> None:
    supervisor = ASRStreamSupervisor()
    supervisor.record_audio(start_sample=0, frame_samples=320)
    original = ASRResult(1, "same", 1, 0, 320, "原结果", True)
    corrected = ASRResult(1, "same", 2, 160, 320, "修正结果", True)

    assert supervisor.accept_result(original, session_id="session")
    assert supervisor.accept_result(corrected, session_id="session")
    assert (
        supervisor.timeline.canonical_text(
            stream_epoch=1,
            start_sample=0,
            end_sample=320,
        )
        == "修正结果"
    )


def test_asr_supervisor_discards_crossing_word_but_keeps_safe_suffix() -> None:
    supervisor = ASRStreamSupervisor()
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=960)
    supervisor.mark_committed(500)
    result = ASRResult(
        1,
        "sentence",
        1,
        0,
        900,
        "前跨后",
        True,
        word_timings=(
            ASRWordTiming("前", 0, 400),
            ASRWordTiming("跨", 400, 600),
            ASRWordTiming("后", 700, 900),
        ),
    )

    decision = supervisor.accept_result(result, session_id="session")
    assert decision
    assert decision.accepted is not None
    assert decision.accepted.text == "后"
    assert decision.accepted.capture_start_sample == 700


def test_asr_supervisor_rejects_incomplete_timing_evidence() -> None:
    supervisor = ASRStreamSupervisor()
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=800)
    supervisor.mark_committed(320)
    result = ASRResult(
        1,
        "sentence",
        1,
        0,
        800,
        "你好世界",
        True,
        word_timings=(ASRWordTiming("世", 320, 560),),
    )

    decision = supervisor.accept_result(result, session_id="session")
    assert not decision
    assert decision.reason is ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING


def test_asr_supervisor_preserves_spaces_when_extracting_timed_tail() -> None:
    supervisor = ASRStreamSupervisor()
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=300)
    supervisor.mark_committed(100)
    result = ASRResult(
        1,
        "sentence",
        1,
        0,
        300,
        "前 hello world",
        True,
        word_timings=(
            ASRWordTiming("前", 0, 100),
            ASRWordTiming("hello", 100, 200),
            ASRWordTiming("world", 200, 300),
        ),
    )

    decision = supervisor.accept_result(result, session_id="session")
    assert decision.accepted is not None
    assert decision.accepted.text == "hello world"


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
    assert (
        b"device.command_ack"
        in DeviceCommandAck(
            command_id="cmd-1",
            status="applied",
            device_monotonic_ms=200,
        ).to_json()
    )


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


def test_media_bridge_stop_retry_cannot_rebind_idempotency_key_to_other_fence() -> None:
    server = MediaBridgeServer()
    server.open(SessionIdentity("stop-fence", stream_epoch=1))
    event = MediaEnvelope.create(
        type="client.stop_assistant",
        event_id="evt-stop",
        session_id="stop-fence",
        stream_epoch=1,
        sequence=0,
        turn_id=0,
        generation_id=0,
        tool_epoch=0,
        payload={"idempotency_key": "stable-stop"},
    )
    assert server.accept_client_event(event)
    forged = MediaEnvelope.create(
        type="client.stop_assistant",
        event_id="evt-other",
        session_id="stop-fence",
        stream_epoch=1,
        sequence=0,
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        payload={"idempotency_key": "stable-stop"},
    )
    assert not server.accept_client_event(forged)


def test_media_bridge_stop_idempotency_is_scoped_to_stream_epoch() -> None:
    server = MediaBridgeServer()
    bridge = server.open(SessionIdentity("stop-reconnect", stream_epoch=1))
    event = MediaEnvelope.create(
        type="client.stop_assistant",
        event_id="evt-reused",
        session_id="stop-reconnect",
        stream_epoch=1,
        sequence=0,
        payload={},
    )
    assert server.accept_client_event(event)
    assert bridge.fence.generation_id == 1
    assert bridge.reconnect(SessionIdentity("stop-reconnect", stream_epoch=2))
    # Reconnect preserves the cancelled gate; a new START/RESUME control must
    # arrive before a stop can target the new stream epoch.
    replay = MediaEnvelope.create(
        type="client.stop_assistant",
        event_id="evt-reused",
        session_id="stop-reconnect",
        stream_epoch=2,
        sequence=0,
        payload={},
    )
    assert not server.accept_client_event(replay)
    assert bridge.fence.generation_id == 1


def test_media_bridge_reconnect_rejects_runtime_authority_change() -> None:
    server = MediaBridgeServer()
    identity = SessionIdentity(
        "authority-reconnect",
        account_id="account-a",
        participant_id="participant-a",
        device_id="device-a",
        client_type="device",
        stream_epoch=1,
        subject_id="subject-a",
        binding_id="binding-a",
        binding_version=1,
        runtime_profile_version=7,
    )
    bridge = server.open(identity)

    assert not bridge.reconnect(
        SessionIdentity(
            "authority-reconnect",
            account_id="account-a",
            participant_id="participant-a",
            device_id="device-a",
            client_type="device",
            stream_epoch=2,
            subject_id="subject-b",
            binding_id="binding-a",
            binding_version=1,
            runtime_profile_version=7,
        )
    )
    assert bridge.identity == identity
    assert bridge.state == "connected"


def test_media_bridge_rejects_audio_discontinuity_and_sample_gap() -> None:
    server = MediaBridgeServer()
    bridge = server.open(SessionIdentity("gap-session", stream_epoch=1))
    first = AudioFrame(
        identity=bridge.identity,
        sequence=0,
        capture_start_sample=0,
        frame_samples=160,
        payload=b"\x00\x00" * 160,
    )
    assert bridge.accept_uplink(first)
    gap = AudioFrame(
        identity=bridge.identity,
        sequence=1,
        capture_start_sample=320,
        frame_samples=160,
        payload=b"\x00\x00" * 160,
    )
    assert not bridge.accept_uplink(gap)
    discontinuity = AudioFrame(
        identity=bridge.identity,
        sequence=1,
        capture_start_sample=160,
        frame_samples=160,
        payload=b"\x00\x00" * 160,
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
            payload=b"\x00\x00" * 160,
        )
    )


def test_media_bridge_playback_retry_cannot_change_same_event_payload() -> None:
    server = MediaBridgeServer()
    bridge = server.open(SessionIdentity("progress-retry", stream_epoch=1))
    event = MediaEnvelope.create(
        type="client.playback.progress",
        event_id="progress-1",
        session_id="progress-retry",
        stream_epoch=1,
        sequence=0,
        payload={
            "turn_id": 0,
            "generation_id": 0,
            "tool_epoch": 0,
            "received_sequence": 0,
            "rendered_sample_end": 10,
            "client_monotonic_ms": 20,
            "approximate": True,
        },
    )
    assert bridge.accept_client_progress(event)
    assert bridge.accept_client_progress(event)
    forged = MediaEnvelope.create(
        type="client.playback.progress",
        event_id="progress-1",
        session_id="progress-retry",
        stream_epoch=1,
        sequence=0,
        payload={**event.payload, "rendered_sample_end": 11},
    )
    assert not bridge.accept_client_progress(forged)
