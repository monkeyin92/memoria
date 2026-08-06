"""Linux device client for the self-authored media-v1 bridge.

The client owns the sample-clocked capture queue, local mute, playback ACK and
allowlisted device-command handling.  ALSA/I2S integration remains an injected
callback so audio work never blocks the transport loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

import grpc

from services.agent.src.voice_core.device_protocol import (
    DEVICE_EVENT_TYPES,
    DeviceCommand,
    DeviceCommandAck,
    DeviceEvent,
)
from services.agent.src.voice_core.device_runtime import AudioDeviceConfig, LinuxAudioPipeline
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.media_protocol import MediaEnvelope, SessionIdentity

media_pb2: Any = _media_pb2

PcmPlaybackHandler = Callable[
    [bytes, int, int, int],
    Awaitable[int | None] | int | None,
]
GenerationHandler = Callable[[int, int, int], Awaitable[None] | None]
DeviceCommandHandler = Callable[[DeviceCommand], Awaitable[bool] | bool]


@dataclass(frozen=True, slots=True)
class MediaDeviceTLS:
    """Client certificate material for the internal bridge."""

    private_key_pem: bytes
    certificate_chain_pem: bytes
    server_ca_pem: bytes

    def credentials(self) -> grpc.ChannelCredentials:
        if not self.private_key_pem or not self.certificate_chain_pem or not self.server_ca_pem:
            raise ValueError("device mTLS key, certificate and server CA are required")
        return grpc.ssl_channel_credentials(
            root_certificates=self.server_ca_pem,
            private_key=self.private_key_pem,
            certificate_chain=self.certificate_chain_pem,
        )


@dataclass(frozen=True, slots=True)
class MediaDeviceConfig:
    address: str
    identity: SessionIdentity
    audio: AudioDeviceConfig = AudioDeviceConfig()
    max_pending_frames: int = 32
    traceparent: str = ""

    def __post_init__(self) -> None:
        if not self.address.strip():
            raise ValueError("device bridge address is required")
        if self.identity.client_type != "device":
            raise ValueError("device client identity must use client_type=device")
        if self.max_pending_frames <= 0:
            raise ValueError("max_pending_frames must be positive")
        if len(self.traceparent) > 128:
            raise ValueError("traceparent must be a short string")


async def _request_stream(
    queue: asyncio.Queue[media_pb2.MediaToCore | None],
) -> AsyncIterator[media_pb2.MediaToCore]:
    while True:
        message = await queue.get()
        if message is None:
            return
        yield message


class LinuxMediaDeviceClient:
    """Bounded gRPC device runtime with local AEC and exact playback ACKs."""

    _METHOD = "/memoria.media.v1.VoiceMediaBridge/Connect"

    def __init__(
        self,
        config: MediaDeviceConfig,
        *,
        tls: MediaDeviceTLS | None = None,
        on_playback: PcmPlaybackHandler | None = None,
        on_generation: GenerationHandler | None = None,
        on_command: DeviceCommandHandler | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.tls = tls
        self.on_playback = on_playback
        self.on_generation = on_generation
        self.on_command = on_command
        self.on_error = on_error
        self.pipeline = LinuxAudioPipeline(config=config.audio, stream_epoch=config.identity.stream_epoch)
        self.identity = config.identity
        self.muted = False
        self._channel: grpc.aio.Channel | None = None
        self._call: Any | None = None
        self._requests: asyncio.Queue[media_pb2.MediaToCore | None] = asyncio.Queue(
            maxsize=config.max_pending_frames
        )
        self._receiver: asyncio.Task[None] | None = None
        self._closed = True
        self._event_sequence = 0
        self._current_turn_id = 0
        self._current_generation_id = 0
        self._current_tool_epoch = 0

    @property
    def connected(self) -> bool:
        return not self._closed and self._call is not None

    async def connect(self) -> None:
        if self.connected:
            return
        if os.getenv("ENVIRONMENT", "development").strip().lower() == "production" and self.tls is None:
            raise RuntimeError("production device media bridge requires mTLS")
        self._closed = False
        self._channel = (
            grpc.aio.secure_channel(self.config.address, self.tls.credentials())
            if self.tls is not None
            else grpc.aio.insecure_channel(self.config.address)
        )
        rpc = self._channel.stream_stream(
            self._METHOD,
            request_serializer=media_pb2.MediaToCore.SerializeToString,
            response_deserializer=media_pb2.CoreToMedia.FromString,
        )
        self._call = rpc(_request_stream(self._requests))
        await self._requests.put(
            media_pb2.MediaToCore(
                hello=media_pb2.SessionHello(
                    identity=self._proto_identity(),
                    traceparent=self.config.traceparent,
                    uplink_format=media_pb2.AudioFormat(
                        encoding=media_pb2.AUDIO_ENCODING_PCM_S16LE,
                        sample_rate=self.config.audio.sample_rate,
                        channels=1,
                        frame_ms=self.config.audio.frame_ms,
                    ),
                    downlink_format=media_pb2.AudioFormat(
                        encoding=media_pb2.AUDIO_ENCODING_PCM_S16LE,
                        sample_rate=24_000,
                        channels=1,
                        frame_ms=self.config.audio.frame_ms,
                    ),
                )
            )
        )
        accepted = await self._call.read()
        if accepted is grpc.aio.EOF or accepted.WhichOneof("event") != "accepted":
            await self.close()
            raise RuntimeError("device media bridge did not accept hello")
        self._current_generation_id = int(accepted.accepted.current_generation_id)
        self._receiver = asyncio.create_task(self._receive_loop(), name="media-device-receiver")

    async def _receive_loop(self) -> None:
        call = self._call
        if call is None:
            return
        try:
            while not self._closed:
                event = await call.read()
                if event is grpc.aio.EOF:
                    return
                await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                self._report_error(str(exc))

    async def _handle_event(self, event: Any) -> None:
        kind = event.WhichOneof("event")
        if kind == "audio":
            audio = event.audio
            if self.muted:
                return
            payload = bytes(audio.pcm_s16le)
            rendered_sample_end: int | None = None
            if self.on_playback is not None:
                result = self.on_playback(
                    payload,
                    int(audio.source_start_sample),
                    int(audio.generation_id),
                    int(audio.sequence),
                )
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, int) and not isinstance(result, bool):
                    rendered_sample_end = result
                elif result is not None:
                    self._report_error("device playback callback returned an invalid sample position")
            if rendered_sample_end is not None:
                frame_start = int(audio.source_start_sample)
                frame_end = frame_start + int(audio.frame_samples)
                if not frame_start <= rendered_sample_end <= frame_end:
                    self._report_error("device playback sample position is outside the audio frame")
                    return
                await self.send_playback_progress(
                    generation_id=int(audio.generation_id),
                    received_sequence=int(audio.sequence),
                    rendered_sample_end=rendered_sample_end,
                    turn_id=int(audio.turn_id),
                    tool_epoch=int(audio.tool_epoch),
                )
        elif kind == "generation":
            self._current_turn_id = int(event.generation.turn_id)
            self._current_generation_id = int(event.generation.generation_id)
            self._current_tool_epoch = int(event.generation.tool_epoch)
            if self.on_generation is not None:
                result = self.on_generation(
                    int(event.generation.turn_id),
                    int(event.generation.generation_id),
                    int(event.generation.tool_epoch),
                )
                if inspect.isawaitable(result):
                    await result
        elif kind == "client":
            await self._handle_client_event(event.client)
        elif kind == "error":
            self._report_error(str(event.error.message))

    async def _handle_client_event(self, event: Any) -> None:
        try:
            value = json.loads(bytes(event.json_payload))
            if not isinstance(value, dict):
                raise ValueError("device event must be an object")
            payload = value.get("payload", value)
            if isinstance(payload, dict) and payload.get("type") == "device.command":
                raw = payload
            elif isinstance(payload, dict):
                raw = {"v": 1, "type": "device.command", **payload}
            else:
                raise ValueError("device command payload must be an object")
            command = DeviceCommand.from_json(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            await self._send_ack(
                DeviceCommandAck(
                    command_id="invalid",
                    status="rejected",
                    device_monotonic_ms=self._now_ms(),
                    message=str(exc)[:160],
                )
            )
            return
        now = self._now_ms()
        if command.expired(now):
            await self._send_ack(
                DeviceCommandAck(command.command_id, "expired", now, "command TTL expired")
            )
            return
        if command.topic == "audio.mute.set":
            muted = command.payload.get("muted")
            if not isinstance(muted, bool):
                status = "rejected"
                message = "muted must be boolean"
            else:
                self.muted = muted
                status = "applied"
                message = ""
        elif self.on_command is None:
            status = "rejected"
            message = "device command handler is unavailable"
        else:
            try:
                result = self.on_command(command)
                applied = await result if inspect.isawaitable(result) else result
                status = "applied" if applied else "rejected"
                message = "" if applied else "device handler rejected command"
            except Exception as exc:
                status = "failed"
                message = str(exc)[:160]
        await self._send_ack(
            DeviceCommandAck(
                command.command_id,
                cast(Literal["applied", "rejected", "expired", "failed"], status),
                now,
                message,
            )
        )

    async def _send_ack(self, ack: DeviceCommandAck) -> None:
        if self._closed:
            return
        envelope = MediaEnvelope.create(
            type="device.command_ack",
            event_id=ack.command_id,
            session_id=self.identity.session_id,
            stream_epoch=self.identity.stream_epoch,
            sequence=self._event_sequence,
            turn_id=self._current_turn_id,
            generation_id=self._current_generation_id,
            tool_epoch=self._current_tool_epoch,
            payload=json.loads(ack.to_json()),
        )
        self._event_sequence += 1
        message = media_pb2.MediaToCore(
            device=media_pb2.DeviceEvent(
                identity=self._proto_identity(),
                event_type="device.command_ack",
                json_payload=envelope.encode(),
                monotonic_ms=ack.device_monotonic_ms,
            )
        )
        try:
            self._requests.put_nowait(message)
        except asyncio.QueueFull:
            self._report_error("device uplink queue is full")

    async def send_device_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
    ) -> bool:
        """Report bounded device telemetry (button/network) to the bridge."""

        if self._closed:
            return False
        if event_type not in DEVICE_EVENT_TYPES:
            raise ValueError("device event type is not allowlisted")
        event = DeviceEvent(
            event_type=event_type,  # type: ignore[arg-type]
            device_monotonic_ms=self._now_ms(),
            payload=dict(payload),
        )
        envelope = MediaEnvelope.create(
            type="client.device.event",
            event_id=f"{self.identity.session_id}:{self._event_sequence}:{event.event_type}",
            session_id=self.identity.session_id,
            stream_epoch=self.identity.stream_epoch,
            sequence=self._event_sequence,
            turn_id=self._current_turn_id,
            generation_id=self._current_generation_id,
            tool_epoch=self._current_tool_epoch,
            payload=json.loads(event.to_json()),
        )
        self._event_sequence += 1
        message = media_pb2.MediaToCore(
            device=media_pb2.DeviceEvent(
                identity=self._proto_identity(),
                event_type="client.device.event",
                json_payload=envelope.encode(),
                monotonic_ms=event.device_monotonic_ms,
            )
        )
        try:
            self._requests.put_nowait(message)
        except asyncio.QueueFull:
            self._report_error("device telemetry queue is full")
            return False
        return True

    async def capture(self, samples: Sequence[int | float]) -> bool:
        if self._closed or self.muted:
            return False
        frame = self.pipeline.capture(samples)
        message = media_pb2.MediaToCore(
            audio=media_pb2.AudioFrame(
                identity=self._proto_identity(),
                sequence=frame.sequence,
                capture_start_sample=frame.capture_start_sample,
                frame_samples=len(frame.samples),
                payload=frame.to_pcm_s16le(),
            )
        )
        try:
            self._requests.put_nowait(message)
        except asyncio.QueueFull:
            self._report_error("device capture queue is full")
            return False
        return True

    def ingest_playback_reference(
        self,
        start_sample: int,
        samples: Sequence[int | float],
    ) -> None:
        self.pipeline.ingest_playback_reference(start_sample, samples)

    async def send_playback_progress(
        self,
        *,
        generation_id: int,
        received_sequence: int,
        rendered_sample_end: int,
        turn_id: int = 0,
        tool_epoch: int = 0,
    ) -> bool:
        if self._closed:
            return False
        message = media_pb2.MediaToCore(
            playback=media_pb2.PlaybackProgress(
                identity=self._proto_identity(),
                generation_id=generation_id,
                received_sequence=received_sequence,
                rendered_sample_end=rendered_sample_end,
                client_monotonic_ms=self._now_ms(),
                approximate=False,
                turn_id=turn_id,
                tool_epoch=tool_epoch,
            )
        )
        try:
            self._requests.put_nowait(message)
        except asyncio.QueueFull:
            self._report_error("device playback ACK queue is full")
            return False
        return True

    async def reconnect(self) -> None:
        next_epoch = self.identity.stream_epoch + 1
        await self.close()
        self.identity = replace(self.identity, stream_epoch=next_epoch)
        self.pipeline.reset_stream(next_epoch)
        self._event_sequence = 0
        self._current_turn_id = 0
        self._current_generation_id = 0
        self._current_tool_epoch = 0
        self._requests = asyncio.Queue(maxsize=self.config.max_pending_frames)
        await self.connect()

    async def close(self) -> None:
        self._closed = True
        try:
            self._requests.put_nowait(None)
        except asyncio.QueueFull:
            pass
        receiver, self._receiver = self._receiver, None
        if receiver is not None and not receiver.done():
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver
        call, self._call = self._call, None
        if call is not None:
            with contextlib.suppress(Exception):
                call.cancel()
        channel, self._channel = self._channel, None
        if channel is not None:
            await channel.close()

    def set_muted(self, muted: bool) -> None:
        self.muted = bool(muted)

    def _proto_identity(self) -> Any:
        return media_pb2.SessionIdentity(
            session_id=self.identity.session_id,
            account_id=self.identity.account_id,
            participant_id=self.identity.participant_id,
            device_id=self.identity.device_id,
            client_type=self.identity.client_type,
            stream_epoch=self.identity.stream_epoch,
        )

    @staticmethod
    def _now_ms() -> int:
        return int(time.monotonic() * 1000)

    def _report_error(self, message: str) -> None:
        if self.on_error is not None:
            self.on_error(message[:256])


__all__ = [
    "DeviceCommandHandler",
    "GenerationHandler",
    "LinuxMediaDeviceClient",
    "MediaDeviceConfig",
    "MediaDeviceTLS",
    "PcmPlaybackHandler",
]
