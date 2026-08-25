from __future__ import annotations

import gc
import hashlib
import time
import tracemalloc

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from services.agent.src.observability.media_otel import configure_otel
from services.agent.src.orchestration.conversation_projection import ConversationProjection
from services.agent.src.orchestration.speech_timeline import (
    SegmentKind,
    SpeechSegment,
    SpeechTimeline,
)
from services.agent.src.voice_core.device_protocol import DeviceCommand, DeviceCommandAck
from services.agent.src.voice_core.device_runtime import (
    AudioDeviceConfig,
    LinuxAudioPipeline,
    NLMSAcousticEchoCanceller,
)
from services.agent.src.voice_core.device_security import (
    OtaManifest,
    provision_device,
    sign_challenge,
    sign_ota_manifest,
    verify_artifact_digest,
    verify_challenge,
    verify_ota_manifest,
)
from services.agent.src.voice_core.replay_harness import (
    AudioReplayHarness,
    ChaosEvent,
    ChaosRunner,
    FixtureMetadata,
    LoadScenario,
    SyntheticFixture,
    estimate_load,
    replay_turn_phases,
)
from services.agent.src.voice_core.telemetry import (
    GOLDEN_TRACE_BASE_EVENTS,
    GOLDEN_TRACE_INTERRUPT_EVENTS,
    MediaTelemetry,
    TraceContext,
    TurnTimeline,
)


def test_nlms_reduces_known_echo() -> None:
    aec = NLMSAcousticEchoCanceller(taps=8, step=0.8)
    reference = tuple(1000.0 if index % 2 else -1000.0 for index in range(200))
    microphone = tuple(sample * 0.5 + 20.0 for sample in reference)
    first = sum(abs(sample) for sample in microphone[-20:])
    output = aec.process(microphone, reference)
    last = sum(abs(sample) for sample in output[-20:])
    assert last < first


def test_linux_pipeline_pairs_actual_playback_reference_and_epoch() -> None:
    pipeline = LinuxAudioPipeline(AudioDeviceConfig(aec_taps=4))
    pipeline.ingest_playback_reference(0, [1000] * 160)
    frame = pipeline.capture([500] * 160)
    assert frame.capture_start_sample == 0
    assert frame.stream_epoch == 1
    pipeline.reset_stream(2)
    assert pipeline.capture([0] * 80).stream_epoch == 2


def test_device_challenge_and_signed_ota() -> None:
    provisioned = provision_device("doll-1")
    challenge = sign_challenge(provisioned, nonce="n", issued_at_ms=1_000)
    assert verify_challenge(provisioned.identity, challenge, now_ms=1_500)
    assert not verify_challenge(provisioned.identity, challenge, now_ms=200_000)

    payload = b"firmware"
    signer = Ed25519PrivateKey.generate()
    unsigned = OtaManifest(
        version="1.2.3",
        artifact_url="https://firmware.example.invalid/doll.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        min_bootloader="1.0.0",
    )
    signed = sign_ota_manifest(unsigned, signer)
    assert verify_ota_manifest(signed, signer.public_key())
    assert verify_artifact_digest(payload, signed)
    assert not verify_artifact_digest(payload + b"x", signed)


def test_device_command_round_trip_is_allowlisted() -> None:
    command = DeviceCommand(
        command_id="cmd-1",
        topic="audio.mute.set",
        ttl_ms=1_000,
        issued_monotonic_ms=20,
        payload={"muted": True},
    )
    assert DeviceCommand.from_json(command.to_json()) == command
    ack = DeviceCommandAck("cmd-1", "applied", 42)
    assert DeviceCommandAck.from_json(ack.to_json()) == ack
    with pytest.raises(ValueError):
        DeviceCommand.from_json(b'{"type":"device.command","topic":"secret"}')


def test_replay_chaos_and_load_are_deterministic() -> None:
    metadata = FixtureMetadata(
        fixture_id="child-001",
        kind="child_clean",
        expected_text="你好",
        expected_turns=1,
        expected_interrupt=False,
        speaker_profile="child",
    )
    result = AudioReplayHarness().replay(SyntheticFixture(metadata), stream_epoch=3)
    assert result.passed
    report = ChaosRunner().run(
        (
            ChaosEvent(10, "asr_disconnect", 300),
            ChaosEvent(20, "late_final"),
        )
    )
    assert report.passed and report.stale_events_rejected == 1
    load = estimate_load(LoadScenario(sessions=2, duration_s=1))
    assert load.passed and load.frames == 100


def test_turn_phase_replay_is_deterministic_for_child_pause() -> None:
    segments = (
        SpeechSegment(
            session_id="session",
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=320,
        ),
        SpeechSegment(
            session_id="session",
            stream_epoch=1,
            provider_task_epoch=1,
            segment_id="asr",
            revision=1,
            kind=SegmentKind.ASR_PARTIAL,
            capture_start_sample=0,
            capture_end_sample=1600,
            text="我想",
        ),
        SpeechSegment(
            session_id="session",
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=1600,
            capture_end_sample=1601,
            final=True,
            voiced_end_sample=1600,
        ),
        SpeechSegment(
            session_id="session",
            stream_epoch=1,
            provider_task_epoch=0,
            segment_id="vad-resume",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=8000,
            capture_end_sample=8320,
        ),
    )
    first = replay_turn_phases(segments, fixture_id="child_pause")
    second = replay_turn_phases(segments, fixture_id="child_pause")
    assert first == second
    assert first.phases == (
        "idle",
        "acoustic_only",
        "semantic_speaking",
        "end_candidate",
        "semantic_speaking",
    )
    # Only the ASR fact covers one complete 80ms bucket. The far-ahead 20ms VAD
    # resume jumps the cursor without backfilling nonexistent historical frames.
    assert first.frame_count == 1


def test_turn_phase_projection_meets_cpu_and_memory_budget() -> None:
    timeline = SpeechTimeline()
    timeline.start_stream_epoch(1)
    projection = ConversationProjection("resource-budget", timeline)
    vad = SpeechSegment(
        session_id="resource-budget",
        stream_epoch=1,
        provider_task_epoch=0,
        segment_id="vad",
        revision=1,
        kind=SegmentKind.VAD,
        capture_start_sample=0,
        capture_end_sample=320,
    )
    assert timeline.add(vad)
    projection.apply_continuous_event(vad, turn_id_hint=1)
    revisions = tuple(
        SpeechSegment(
            session_id="resource-budget",
            stream_epoch=1,
            provider_task_epoch=1,
            segment_id="asr",
            revision=revision,
            kind=SegmentKind.ASR_PARTIAL,
            capture_start_sample=0,
            capture_end_sample=1_280 + revision * 32,
            text="性能预算测试内容",
        )
        for revision in range(1, 501)
    )

    latencies_ns: list[int] = []
    tracemalloc.start()
    gc.collect()
    baseline_bytes = tracemalloc.get_traced_memory()[0]
    try:
        for segment in revisions:
            assert timeline.add(segment)
            started_ns = time.perf_counter_ns()
            projection.apply_continuous_event(segment, turn_id_hint=1)
            latencies_ns.append(time.perf_counter_ns() - started_ns)
        gc.collect()
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    latencies_ns.sort()
    p95_ns = latencies_ns[int(len(latencies_ns) * 0.95) - 1]
    assert p95_ns < 2_000_000
    assert current_bytes - baseline_bytes < 65_536
    assert peak_bytes - baseline_bytes < 65_536


def test_telemetry_redacts_labels_and_bounds_timeline() -> None:
    metrics = MediaTelemetry(max_series=4)
    metrics.inc("voice_kws_hits_total", labels={"kind": "hard_stop"})
    metrics.observe_ms("voice_first_audio_ms", 42)
    assert "voice_kws_hits_total" in metrics.render_prometheus()
    with pytest.raises(ValueError):
        metrics.inc("voice_kws_hits_total", labels={"session_id": "secret"})
    context = TraceContext(trace_id="t", session_id="s", stream_epoch=1)
    timeline = TurnTimeline(context, max_events=2)
    timeline.add("vad.detect", at_monotonic_ns=10)
    timeline.add("asr.final", at_monotonic_ns=20)
    timeline.add("turn.commit", at_monotonic_ns=30)
    assert [event.name for event in timeline.events()] == ["asr.final", "turn.commit"]
    assert not configure_otel("")


def test_hardware_golden_trace_requires_full_fence_profile_and_safe_sample_fields() -> None:
    context = TraceContext(
        trace_id="0123456789abcdef0123456789abcdef",
        session_id="session-1",
        stream_epoch=3,
        turn_id=4,
        generation_id=5,
        tool_epoch=2,
        device_id="device-1",
        provider_task_epoch=7,
        runtime_profile_version=9,
    )
    timeline = TurnTimeline(context, max_events=32)
    for index, name in enumerate(GOLDEN_TRACE_BASE_EVENTS):
        timeline.add(
            name,
            at_monotonic_ns=index + 1,
            fields={"device_sequence": index, "capture_start_sample": index * 320},
        )
    assert timeline.completion_gaps() == ()
    assert timeline.completion_gaps(require_interrupt=True) == GOLDEN_TRACE_INTERRUPT_EVENTS
    for index, name in enumerate(GOLDEN_TRACE_INTERRUPT_EVENTS, start=100):
        timeline.add(name, at_monotonic_ns=index, fields={"reason": "hard_stop"})
    assert timeline.completion_gaps(require_interrupt=True) == ()
    assert context.fields()["runtime_profile_version"] == 9


def test_hardware_trace_rejects_transcript_audio_and_secret_fields() -> None:
    timeline = TurnTimeline(TraceContext(trace_id="trace", session_id="session", stream_epoch=1))
    for field in ("text", "transcript", "audio_payload", "wifi_password", "provider_token"):
        with pytest.raises(ValueError, match="sensitive"):
            timeline.add("asr.final", fields={field: "forbidden"})
    with pytest.raises(ValueError, match="not allowlisted"):
        timeline.add("asr.final", fields={"family_name": "forbidden"})


def test_hardware_metric_names_are_allowlisted_without_identifier_labels() -> None:
    metrics = MediaTelemetry()
    for name in (
        "device_media_connect_success_total",
        "device_media_reconnect_total",
        "device_uplink_gap_samples_total",
        "device_downlink_queue_ms",
        "device_playback_ack_lag_ms",
        "stale_generation_drop_total",
        "interrupt_candidate_total",
        "interrupt_confirmed_total",
        "interrupt_false_positive_total",
        "interrupt_audible_stop_ms",
        "aec_far_end_false_vad_total",
        "aec_double_talk_asr_error_rate",
        "runtime_profile_version_lag",
        "voice_turn_state_transition_total",
        "voice_turn_end_candidate_retracted_total",
        "voice_turn_uncertain_total",
        "voice_backchannel_filtered_total",
        "voice_acoustic_only_cancel_blocked_total",
        "voice_conversation_turn_initiation_total",
        "voice_conversation_backchannel_total",
        "voice_conversation_yield_proxy_total",
        "voice_conversation_participation_proxy_ms_total",
    ):
        metrics.inc(name)
    metrics.observe_ms("voice_turn_end_candidate_latency_ms", 12.0)
    metrics.inc(
        "voice_turn_state_transition_total",
        labels={"from_state": "idle", "to_state": "acoustic_only"},
    )
    metrics.inc(
        "voice_conversation_turn_initiation_total",
        labels={"kind": "vad_first", "state": "assistant_overlap"},
    )
    metrics.inc("voice_conversation_backchannel_total", labels={"status": "detected"})
    metrics.inc("voice_conversation_yield_proxy_total", labels={"status": "confirmed"})
    metrics.inc(
        "voice_conversation_participation_proxy_ms_total",
        labels={"kind": "assistant"},
    )
    with pytest.raises(ValueError, match="not allowlisted"):
        metrics.inc("stale_generation_drop_total", labels={"device_id": "device-1"})
