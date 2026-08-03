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
    playback_seen = asyncio.Event()

    async def on_audio(_session, frame) -> None:
        uplink_seen.append(frame.sequence)

    async def on_event(_session, event: MediaEnvelope) -> None:
        if event.type == "device.command_ack":
            command_acks.append(event)

    bridge = MediaBridgeGrpcServer(on_audio_frame=on_audio, on_client_event=on_event)
    port = await bridge.start("127.0.0.1:0")
    identity = SessionIdentity(
        "device-client-session",
        account_id="account",
        participant_id="device-participant",
        device_id="doll-1",
        client_type="device",
    )
    client = LinuxMediaDeviceClient(
        MediaDeviceConfig(address=f"127.0.0.1:{port}", identity=identity),
        on_playback=lambda *_: playback_seen.set(),
    )
    try:
        await client.connect()
        assert await client.capture([100] * 320)
        for _ in range(20):
            if uplink_seen:
                break
            await asyncio.sleep(0.01)
        assert uplink_seen == [0]

        session = bridge.bridge.get(identity.session_id)
        assert session is not None
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
