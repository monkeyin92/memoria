from __future__ import annotations

import asyncio
import time

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.device_client import LinuxMediaDeviceClient, MediaDeviceConfig
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_bridge_server import PCMFrame
from services.agent.src.voice_core.media_protocol import MediaEnvelope, SessionIdentity


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
    identity = SessionIdentity(
        "device-client-session",
        account_id="account",
        participant_id="device-participant",
        device_id="doll-1",
        client_type="device",
    )
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
    identity = SessionIdentity(
        "device-no-playout-progress",
        account_id="account",
        participant_id="device-participant",
        device_id="doll-1",
        client_type="device",
    )
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
    identity = SessionIdentity(
        "device-telemetry-session",
        account_id="account",
        participant_id="device-participant",
        device_id="doll-1",
        client_type="device",
    )
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
    identity = SessionIdentity(
        "device-production-session",
        account_id="account",
        participant_id="device-participant",
        device_id="doll-1",
        client_type="device",
    )
    client = LinuxMediaDeviceClient(
        MediaDeviceConfig(address="127.0.0.1:1", identity=identity),
    )
    with pytest.raises(RuntimeError, match="mTLS"):
        await client.connect()
