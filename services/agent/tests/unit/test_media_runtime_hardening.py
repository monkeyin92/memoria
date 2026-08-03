from __future__ import annotations

import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from services.agent.src.observability.media_otel import configure_otel
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
)
from services.agent.src.voice_core.telemetry import (
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
