from __future__ import annotations

import asyncio
import time

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.device_client import LinuxMediaDeviceClient, MediaDeviceConfig
from services.agent.src.voice_core.device_protocol import decide_remote_mute
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_bridge_server import PCMFrame
from services.agent.src.voice_core.media_protocol import MediaEnvelope, SessionIdentity


def _device_identity(session_id: str) -> SessionIdentity:
    return SessionIdentity(
        session_id,
        account_id="account",
        participant_id="device-participant",
        device_id="doll-1",
        client_type="device",
        subject_id="subject-1",
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_version=1,
    )


async def _wait_for_acks(acks: list[MediaEnvelope], count: int) -> None:
    for _ in range(50):
        if len(acks) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} command acks, got {len(acks)}")


def _mute_command(command_id: str, payload: dict[str, object], issued_offset_ms: int = 0) -> dict[str, object]:
    return {
        "command_id": command_id,
        "topic": "audio.mute.set",
        "ttl_ms": 1_000,
        "issued_at_monotonic_ms": int(time.monotonic() * 1000) + issued_offset_ms,
        "payload": payload,
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"muted": True}, "engage"),
        ({"muted": False}, "reject"),
        ({"muted": 0}, "reject"),
        ({"muted": 1}, "reject"),
        ({"muted": "true"}, "reject"),
        ({"muted": None}, "reject"),
        ({}, "reject"),
    ],
)
def test_decide_remote_mute_only_engages_on_literal_true(
    payload: dict[str, object],
    expected: str,
) -> None:
    assert decide_remote_mute(payload) == expected


@pytest.mark.asyncio
async def test_linux_device_client_capture_playback_ack_and_local_mute() -> None:
    uplink_seen: list[int] = []
    command_acks: list[MediaEnvelope] = []
    playback_progress: list[int] = []
    playback_seen = asyncio.Event()

    async def on_audio(_session, frame) -> None:
        uplink_seen.append(frame.sequence)

    async def on_event(_session, event: MediaEnvelope, detected_monotonic_ms: int = 0) -> None:
        if event.type == "device.command_ack":
            command_acks.append(event)

    async def on_progress(_session, progress) -> None:
        playback_progress.append(progress.rendered_sample_end)

    def on_playback(*_args: object) -> int:
        playback_seen.set()
        return 2

    bridge = MediaBridgeGrpcServer(
        on_audio_frame=on_audio,
        on_client_event=on_event,
        on_playback_progress=on_progress,
    )
    port = await bridge.start("127.0.0.1:0")
    identity = _device_identity("device-client-session")
    client = LinuxMediaDeviceClient(
        MediaDeviceConfig(
            address=f"127.0.0.1:{port}",
            identity=identity,
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        ),
        on_playback=on_playback,
    )
    try:
        await client.connect()
        session = bridge.bridge.get(identity.session_id)
        assert session is not None
        assert session.traceparent == (
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        )
        assert await client.capture([100] * 320)
        for _ in range(20):
            if uplink_seen:
                break
            await asyncio.sleep(0.01)
        assert uplink_seen == [0]

        fence = GenerationFence(identity.session_id, 1, 1, 0)
        assert await bridge.emit_generation(
            identity.session_id,
            fence,
            action=media_pb2.GENERATION_ACTION_START,
        )
        assert await bridge.emit_pcm(
            identity.session_id,
            PCMFrame(
                identity=identity,
                turn_id=1,
                generation_id=1,
                tool_epoch=0,
                sequence=0,
                source_start_sample=0,
                frame_samples=2,
                pcm_s16le=b"\x00\x00\x01\x00",
            ),
        )
        await asyncio.wait_for(playback_seen.wait(), timeout=1)
        for _ in range(20):
            if playback_progress:
                break
            await asyncio.sleep(0.01)
        assert playback_progress == [2]

        issued = int(time.monotonic() * 1000)
        assert await bridge.emit_event(
            identity.session_id,
            "device.command",
            {
                "command_id": "mute-1",
                "topic": "audio.mute.set",
                "ttl_ms": 1_000,
                "issued_at_monotonic_ms": issued,
                "payload": {"muted": True},
            },
        )
        for _ in range(20):
            if command_acks:
                break
            await asyncio.sleep(0.01)
        assert client.muted is True
        assert command_acks and command_acks[0].payload["status"] == "applied"
        assert await client.capture([100] * 320) is False
    finally:
        await client.close()
        await bridge.stop()


@pytest.mark.asyncio
async def test_linux_device_does_not_ack_when_renderer_reports_no_progress() -> None:
    playback_progress: list[int] = []

    async def on_progress(_session, progress) -> None:
        playback_progress.append(progress.rendered_sample_end)

    bridge = MediaBridgeGrpcServer(on_playback_progress=on_progress)
    port = await bridge.start("127.0.0.1:0")
    identity = _device_identity("device-no-playout-progress")
    client = LinuxMediaDeviceClient(
        MediaDeviceConfig(address=f"127.0.0.1:{port}", identity=identity),
        on_playback=lambda *_: None,
    )
    try:
        await client.connect()
        assert await bridge.emit_pcm(
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
        await asyncio.sleep(0.05)
        assert playback_progress == []
    finally:
        await client.close()
        await bridge.stop()


@pytest.mark.asyncio
async def test_linux_device_client_reports_button_and_network_telemetry() -> None:
    device_events: list[MediaEnvelope] = []
    generation_seen = asyncio.Event()

    async def on_event(_session, event: MediaEnvelope, detected_monotonic_ms: int = 0) -> None:
        if event.type == "client.device.event":
            device_events.append(event)

    bridge = MediaBridgeGrpcServer(on_client_event=on_event)
    port = await bridge.start("127.0.0.1:0")
    identity = _device_identity("device-telemetry-session")
    client = LinuxMediaDeviceClient(
        MediaDeviceConfig(address=f"127.0.0.1:{port}", identity=identity),
        on_generation=lambda *_: generation_seen.set(),
    )
    try:
        await client.connect()
        assert await bridge.emit_generation(
            identity.session_id,
            GenerationFence(identity.session_id, 3, 5, 2),
            action=media_pb2.GENERATION_ACTION_START,
        )
        await asyncio.wait_for(generation_seen.wait(), timeout=1)
        assert await client.send_device_event(
            event_type="button",
            payload={"button": "push_to_talk", "pressed": True},
        )
        assert await client.send_device_event(
            event_type="network_status",
            payload={"rtt_ms": 42, "transport": "wifi"},
        )
        for _ in range(20):
            if len(device_events) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(device_events) == 2
        assert device_events[0].payload["event_type"] == "button"
        assert device_events[0].payload["payload"] == {
            "button": "push_to_talk",
            "pressed": True,
        }
        assert (
            device_events[0].turn_id,
            device_events[0].generation_id,
            device_events[0].tool_epoch,
        ) == (3, 5, 2)
        assert device_events[1].payload["event_type"] == "network_status"
        assert device_events[1].payload["payload"]["rtt_ms"] == 42
        assert (
            device_events[1].turn_id,
            device_events[1].generation_id,
            device_events[1].tool_epoch,
        ) == (3, 5, 2)
    finally:
        await client.close()
        await bridge.stop()


@pytest.mark.asyncio
async def test_linux_device_client_rejects_plaintext_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    identity = _device_identity("device-production-session")
    client = LinuxMediaDeviceClient(
        MediaDeviceConfig(address="127.0.0.1:1", identity=identity),
    )
    with pytest.raises(RuntimeError, match="mTLS"):
        await client.connect()


@pytest.mark.asyncio
async def test_remote_mute_false_is_rejected_and_cannot_unmute() -> None:
    command_acks: list[MediaEnvelope] = []
    handler_calls: list[str] = []

    async def on_event(_session, event: MediaEnvelope, detected_monotonic_ms: int = 0) -> None:
        if event.type == "device.command_ack":
            command_acks.append(event)

    def on_command(command) -> bool:
        handler_calls.append(command.topic)
        return True

    bridge = MediaBridgeGrpcServer(on_client_event=on_event)
    port = await bridge.start("127.0.0.1:0")
    identity = _device_identity("device-remote-mute-false")
    client = LinuxMediaDeviceClient(
        MediaDeviceConfig(address=f"127.0.0.1:{port}", identity=identity),
        on_command=on_command,
    )
    try:
        await client.connect()
        assert await bridge.emit_event(
            identity.session_id,
            "device.command",
            _mute_command("mute-engage", {"muted": True}),
        )
        await _wait_for_acks(command_acks, 1)
        assert client.muted is True
        assert command_acks[0].payload["status"] == "applied"

        assert await bridge.emit_event(
            identity.session_id,
            "device.command",
            _mute_command("mute-release", {"muted": False}),
        )
        await _wait_for_acks(command_acks, 2)
        ack = command_acks[1].payload
        assert ack["status"] == "rejected"
        assert ack["message"] == ""
        assert client.muted is True
        # A custom on_command returning True must not bypass the built-in rule.
        assert handler_calls == []
    finally:
        await client.close()
        await bridge.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"muted": 0},
        {"muted": 1},
        {"muted": "true"},
        {"muted": None},
        {},
    ],
)
async def test_remote_mute_malformed_payloads_fail_closed(payload: dict[str, object]) -> None:
    command_acks: list[MediaEnvelope] = []
    seam_calls: list[tuple[object, ...]] = []

    async def on_event(_session, event: MediaEnvelope, detected_monotonic_ms: int = 0) -> None:
        if event.type == "device.command_ack":
            command_acks.append(event)

    bridge = MediaBridgeGrpcServer(on_client_event=on_event)
    port = await bridge.start("127.0.0.1:0")
    identity = _device_identity("device-remote-mute-malformed")
    client = LinuxMediaDeviceClient(
        MediaDeviceConfig(address=f"127.0.0.1:{port}", identity=identity),
    )
    original_seam = client.apply_local_hardware_mute

    def spy_seam(*args: object) -> None:
        seam_calls.append(args)
        raise AssertionError("remote command path must never call the local hardware seam")

    try:
        await client.connect()
        client.apply_local_hardware_mute = spy_seam  # type: ignore[method-assign]
        assert await bridge.emit_event(
            identity.session_id,
            "device.command",
            _mute_command("mute-engage", {"muted": True}),
        )
        await _wait_for_acks(command_acks, 1)
        assert client.muted is True
        assert command_acks[0].payload["status"] == "applied"

        assert await bridge.emit_event(
            identity.session_id,
            "device.command",
            _mute_command(f"mute-malformed-{len(command_acks)}", payload),
        )
        await _wait_for_acks(command_acks, 2)
        ack = command_acks[1].payload
        assert ack["status"] == "rejected"
        assert client.muted is True
        assert seam_calls == []
    finally:
        client.apply_local_hardware_mute = original_seam  # type: ignore[method-assign]
        await client.close()
        await bridge.stop()


@pytest.mark.asyncio
async def test_local_hardware_mute_seam_unmutes_and_is_isolated_from_remote() -> None:
    command_acks: list[MediaEnvelope] = []

    async def on_event(_session, event: MediaEnvelope, detected_monotonic_ms: int = 0) -> None:
        if event.type == "device.command_ack":
            command_acks.append(event)

    bridge = MediaBridgeGrpcServer(on_client_event=on_event)
    port = await bridge.start("127.0.0.1:0")
    identity = _device_identity("device-local-hardware-seam")
    client = LinuxMediaDeviceClient(
        MediaDeviceConfig(address=f"127.0.0.1:{port}", identity=identity),
    )
    try:
        await client.connect()
        assert await bridge.emit_event(
            identity.session_id,
            "device.command",
            _mute_command("mute-engage", {"muted": True}),
        )
        await _wait_for_acks(command_acks, 1)
        assert client.muted is True

        # Real local hardware control unmutes directly and restores capture.
        client.apply_local_hardware_mute(False)
        assert client.muted is False
        assert await client.capture([100] * 320) is True
        client.apply_local_hardware_mute(True)
        assert client.muted is True
        assert await client.capture([100] * 320) is False

        # Remote re-engage still works after a local unmute; remote release stays rejected.
        client.apply_local_hardware_mute(False)
        assert await bridge.emit_event(
            identity.session_id,
            "device.command",
            _mute_command("mute-reengage", {"muted": True}),
        )
        await _wait_for_acks(command_acks, 2)
        assert client.muted is True
        assert command_acks[1].payload["status"] == "applied"
        assert await bridge.emit_event(
            identity.session_id,
            "device.command",
            _mute_command("mute-release-again", {"muted": False}),
        )
        await _wait_for_acks(command_acks, 3)
        assert command_acks[2].payload["status"] == "rejected"
        assert client.muted is True
    finally:
        await client.close()
        await bridge.stop()


@pytest.mark.asyncio
async def test_remote_mute_duplicate_and_expired_commands_keep_safety_semantics() -> None:
    command_acks: list[MediaEnvelope] = []

    async def on_event(_session, event: MediaEnvelope, detected_monotonic_ms: int = 0) -> None:
        if event.type == "device.command_ack":
            command_acks.append(event)

    bridge = MediaBridgeGrpcServer(on_client_event=on_event)
    port = await bridge.start("127.0.0.1:0")
    identity = _device_identity("device-remote-mute-duplicate")
    client = LinuxMediaDeviceClient(
        MediaDeviceConfig(address=f"127.0.0.1:{port}", identity=identity),
    )
    try:
        await client.connect()
        # Same command_id replayed twice: both acks applied, state stays muted
        # (safe idempotency, no anti-replay in this slice by design).
        for index in range(2):
            assert await bridge.emit_event(
                identity.session_id,
                "device.command",
                _mute_command("mute-duplicate-replay", {"muted": True}),
            )
            await _wait_for_acks(command_acks, index + 1)
            assert command_acks[index].payload["command_id"] == "mute-duplicate-replay"
            assert command_acks[index].payload["status"] == "applied"
        assert client.muted is True

        # Expired commands are not executed at all, regardless of payload.
        assert await bridge.emit_event(
            identity.session_id,
            "device.command",
            _mute_command("mute-expired-engage", {"muted": True}, issued_offset_ms=-5_000),
        )
        assert await bridge.emit_event(
            identity.session_id,
            "device.command",
            _mute_command("mute-expired-release", {"muted": False}, issued_offset_ms=-5_000),
        )
        await _wait_for_acks(command_acks, 4)
        assert command_acks[2].payload["status"] == "expired"
        assert command_acks[3].payload["status"] == "expired"
        assert client.muted is True
    finally:
        await client.close()
        await bridge.stop()
