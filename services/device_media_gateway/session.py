"""Device session adapter: Memoria protocol ↔ existing Mini Program bridge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.agent.src.contracts.events import UI_EVENT_TYPES
from services.common.miniprogram_gateway_ticket import (
    DEVICE_AGENT_DISPATCH_METADATA,
    DeviceGatewayTicketClaims,
    GatewayTicketClaims,
)
from services.device_media_gateway.config import DeviceMediaGatewaySettings
from services.device_media_gateway.opus import OpusDecoder, OpusEncoder
from services.device_media_gateway.protocol import (
    DOWNLINK_FRAME_SAMPLES,
    DOWNLINK_SAMPLE_RATE,
    UPLINK_FRAME_SAMPLES,
    UPLINK_SAMPLE_RATE,
    FrameType,
    MemoriaAudioFrameV1,
    ProtocolError,
    decode_audio_frame,
    encode_audio_frame,
    parse_json_message,
    validate_audio_shape,
    validate_device_event,
)
from services.miniprogram_gateway.bridge import GatewayOutboundMessage, MiniProgramLiveKitBridge
from services.miniprogram_gateway.protocol import FrameType as PcmFrameType
from services.miniprogram_gateway.protocol import PcmFrame

_MAX_UINT32 = (1 << 32) - 1


class DeviceBridge(Protocol):
    async def connect(self) -> None: ...

    async def accept_uplink(self, frame: PcmFrame) -> None: ...

    def accept_transport_event(self, event: dict[str, object]) -> None: ...

    async def next_outbound(self) -> GatewayOutboundMessage: ...

    def outbound_sent(self, message: GatewayOutboundMessage) -> None: ...

    async def wait_for_room_disconnect(self) -> None: ...

    async def close(self) -> None: ...


def gateway_claims(claims: DeviceGatewayTicketClaims) -> GatewayTicketClaims:
    """Project device claims into the existing bridge's user-session shape."""
    return GatewayTicketClaims(
        session_id=claims.session_id,
        user_id=claims.user_id,
        room_name=claims.room_name,
        identity=claims.identity,
        agent_name=claims.agent_name,
        voice_backend=claims.voice_backend,
        issued_at_s=claims.issued_at_s,
        expires_at_s=claims.expires_at_s,
        ticket_id=claims.ticket_id,
    )


class MiniProgramBridgeAdapter:
    """Reuse the proven LiveKit bridge without exposing its PCM protocol."""

    def __init__(
        self, *, settings: DeviceMediaGatewaySettings, claims: DeviceGatewayTicketClaims
    ) -> None:
        self._inner = MiniProgramLiveKitBridge(
            settings=settings,
            claims=gateway_claims(claims),
            dispatch_metadata=DEVICE_AGENT_DISPATCH_METADATA,
            microphone_track_name="device-microphone",
        )

    async def connect(self) -> None:
        self._inner.set_downlink_generation_protocol(True)
        await self._inner.connect()

    async def accept_uplink(self, frame: PcmFrame) -> None:
        await self._inner.accept_uplink(frame)

    def accept_transport_event(self, event: dict[str, object]) -> None:
        self._inner.accept_transport_event(event)

    async def next_outbound(self) -> GatewayOutboundMessage:
        return await self._inner.next_outbound()

    def outbound_sent(self, message: GatewayOutboundMessage) -> None:
        self._inner.outbound_sent(message)

    async def wait_for_room_disconnect(self) -> None:
        await self._inner.wait_for_room_disconnect()

    async def close(self) -> None:
        await self._inner.close()


@dataclass(frozen=True, slots=True)
class DeviceOutboundMessage:
    source: GatewayOutboundMessage
    binary: bytes | None = None
    event: dict[str, object] | None = None


class DeviceMediaSession:
    def __init__(
        self,
        *,
        settings: DeviceMediaGatewaySettings,
        claims: DeviceGatewayTicketClaims,
        bridge: DeviceBridge,
    ) -> None:
        self.settings = settings
        self.claims = claims
        self.bridge = bridge
        self._uplink_decoder = OpusDecoder(
            sample_rate=UPLINK_SAMPLE_RATE,
            frame_samples=UPLINK_FRAME_SAMPLES,
            max_packet_bytes=settings.device_media_gateway_max_opus_payload_bytes,
        )
        self._downlink_encoder = OpusEncoder(
            sample_rate=DOWNLINK_SAMPLE_RATE,
            frame_samples=DOWNLINK_FRAME_SAMPLES,
        )
        self._uplink_sequence: int | None = None
        self._uplink_sample_start = 0
        self._decoded_uplink_sequence = 0
        self._decoded_uplink_sample_start = 0
        self._downlink_sequence = 0
        self._downlink_sample_start = 0
        self._active_generation = 0
        self._sent_generation_end: dict[int, int] = {}
        self._received_playback_end: dict[int, int] = {}
        self._vad_active = False
        self._last_vad_sample = 0
        self._closed = False

    @property
    def ready_event(self) -> dict[str, object]:
        return {
            "type": "session.ready",
            "session_id": self.claims.session_id,
            "stream_epoch": self.claims.stream_epoch,
            "turn_id": 0,
            "generation_id": 0,
            "tool_epoch": 0,
        }

    async def connect(self) -> None:
        await self.bridge.connect()

    async def accept_binary(self, raw: bytes) -> None:
        if self._closed:
            raise ProtocolError("device media session is closed")
        frame = decode_audio_frame(
            raw,
            expected_type=FrameType.UPLINK_AUDIO,
            max_payload_bytes=self.settings.device_media_gateway_max_opus_payload_bytes,
        )
        validate_audio_shape(frame)
        self._validate_uplink_fence(frame)
        pcm_frames = self._uplink_decoder.decode(frame.payload)
        for pcm in pcm_frames:
            await self.bridge.accept_uplink(
                PcmFrame(
                    frame_type=PcmFrameType.UPLINK_AUDIO,
                    sequence=self._decoded_uplink_sequence,
                    timestamp_ms=(
                        self._decoded_uplink_sample_start * 1_000 // UPLINK_SAMPLE_RATE
                    ),
                    payload=pcm,
                )
            )
            self._decoded_uplink_sequence = (self._decoded_uplink_sequence + 1) & 0xFFFFFFFF
            self._decoded_uplink_sample_start += UPLINK_FRAME_SAMPLES

    def accept_text(self, text: str) -> None:
        raw = parse_json_message(text)
        event = validate_device_event(raw, stream_epoch=self.claims.stream_epoch)
        self._validate_event_fence(event)
        self.bridge.accept_transport_event(self._project_event_to_bridge(event))

    async def next_outbound(self) -> DeviceOutboundMessage:
        while True:
            source = await self.bridge.next_outbound()
            if source.event is None:
                break
            event = self._translate_event(source.event)
            if event is not None:
                return DeviceOutboundMessage(source=source, event=event)
            # Agent UI events with no hardware surface are still known and
            # consumed. Unknown event types continue to fail closed below.
            self.bridge.outbound_sent(source)
        if source.binary is None:
            raise ProtocolError("bridge emitted an empty outbound message")
        pcm_frame = self._decode_bridge_pcm(source.binary)
        bridge_generation_id = source.generation_id
        if bridge_generation_id is None:
            bridge_generation_id = pcm_frame.generation_id
        elif (
            pcm_frame.generation_id is not None
            and bridge_generation_id != pcm_frame.generation_id
        ):
            raise ProtocolError("bridge downlink generation fences disagree")
        if not isinstance(bridge_generation_id, int):
            raise ProtocolError("downlink audio is missing a generation fence")
        generation_id = self._device_generation_id(bridge_generation_id)
        if generation_id != self._active_generation:
            raise ProtocolError("downlink audio generation is not active")
        packet = self._downlink_encoder.encode(pcm_frame.payload)
        self._downlink_sample_start += DOWNLINK_FRAME_SAMPLES
        self._sent_generation_end[generation_id] = self._downlink_sample_start
        binary = encode_audio_frame(
            FrameType.DOWNLINK_AUDIO,
            stream_epoch=self.claims.stream_epoch,
            sequence=self._downlink_sequence,
            sample_start=self._downlink_sample_start - DOWNLINK_FRAME_SAMPLES,
            frame_samples=DOWNLINK_FRAME_SAMPLES,
            generation_id=generation_id,
            payload=packet,
            max_payload_bytes=self.settings.device_media_gateway_max_opus_payload_bytes,
        )
        self._downlink_sequence = (self._downlink_sequence + 1) & 0xFFFFFFFF
        return DeviceOutboundMessage(source=source, binary=binary)

    def outbound_sent(self, message: DeviceOutboundMessage) -> None:
        self.bridge.outbound_sent(message.source)

    async def wait_for_room_disconnect(self) -> None:
        await self.bridge.wait_for_room_disconnect()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.bridge.close()

    def _validate_uplink_fence(self, frame: MemoriaAudioFrameV1) -> None:
        if frame.stream_epoch != self.claims.stream_epoch:
            raise ProtocolError("uplink stream_epoch does not match ticket")
        expected_sequence = (
            0 if self._uplink_sequence is None else (self._uplink_sequence + 1) & 0xFFFFFFFF
        )
        if frame.sequence != expected_sequence:
            raise ProtocolError("uplink sequence is not contiguous")
        if frame.sample_start != self._uplink_sample_start:
            raise ProtocolError("uplink sample_start is not contiguous")
        if frame.generation_id != 0:
            raise ProtocolError("uplink generation_id must be zero")
        self._uplink_sequence = frame.sequence
        self._uplink_sample_start += UPLINK_FRAME_SAMPLES

    def _validate_event_fence(self, event: dict[str, object]) -> None:
        if event["type"] in {"vad.start", "vad.end"}:
            sample_position = event["sample_position"]
            assert isinstance(sample_position, int)
            if sample_position > self._uplink_sample_start:
                raise ProtocolError("device VAD sample position is ahead of uplink audio")
            if sample_position < self._last_vad_sample:
                raise ProtocolError("device VAD sample clock moved backwards")
            if event["type"] == "vad.start":
                if self._vad_active:
                    raise ProtocolError("device VAD speech is already active")
                self._vad_active = True
            else:
                if not self._vad_active:
                    raise ProtocolError("device VAD speech is not active")
                self._vad_active = False
            self._last_vad_sample = sample_position
        elif event["type"] in {
            "playback.started",
            "playback.progress",
            "playback.ended",
            "playback.error",
        }:
            generation_id = event["generation_id"]
            played_end = event["played_sample_end"]
            assert isinstance(generation_id, int) and isinstance(played_end, int)
            if generation_id != self._active_generation or generation_id == 0:
                raise ProtocolError("playback receipt generation is stale")
            sent_end = self._sent_generation_end.get(generation_id, 0)
            if played_end > sent_end:
                raise ProtocolError("playback receipt is ahead of sent audio")
            previous = self._received_playback_end.get(generation_id, 0)
            if played_end < previous:
                raise ProtocolError("playback receipt sample clock moved backwards")
            self._received_playback_end[generation_id] = played_end
        elif event["type"] == "button.event":
            generation_id = event["generation_id"]
            assert isinstance(generation_id, int)
            if generation_id != self._active_generation:
                raise ProtocolError("button event generation is stale")

    def _decode_bridge_pcm(self, raw: bytes) -> MemoriaAudioFrameV1:
        from services.miniprogram_gateway.protocol import decode_pcm_frame

        frame = decode_pcm_frame(raw, expected_type=PcmFrameType.DOWNLINK_AUDIO)
        if len(frame.payload) != DOWNLINK_FRAME_SAMPLES * 2:
            raise ProtocolError("bridge downlink is not PCM16/24 kHz/20 ms")
        return MemoriaAudioFrameV1(
            frame_type=FrameType.DOWNLINK_AUDIO,
            flags=0,
            stream_epoch=self.claims.stream_epoch,
            sequence=frame.sequence,
            sample_start=0,
            frame_samples=DOWNLINK_FRAME_SAMPLES,
            generation_id=frame.generation_id or 0,
            payload=frame.payload,
        )

    def _translate_event(self, event: dict[str, object]) -> dict[str, object] | None:
        event_type = event.get("type")
        if event_type == "audio_reset":
            bridge_generation_id = event.get("generation_id")
            if not isinstance(bridge_generation_id, int):
                raise ProtocolError("invalid generation reset")
            generation_id = self._device_generation_id(bridge_generation_id)
            if generation_id <= self._active_generation:
                raise ProtocolError("invalid or stale generation reset")
            self._active_generation = generation_id
            self._downlink_encoder = OpusEncoder(
                sample_rate=DOWNLINK_SAMPLE_RATE,
                frame_samples=DOWNLINK_FRAME_SAMPLES,
            )
            return {
                "type": "playback.flush",
                "stream_epoch": self.claims.stream_epoch,
                "generation_id": generation_id,
                "barrier_sequence": event.get("barrier_sequence", 0),
            }
        if event_type == "ui_event":
            nested = event.get("event")
            if not isinstance(nested, dict):
                raise ProtocolError("invalid bridge UI event")
            return self._translate_ui_event(nested)
        if event_type == "transcription":
            return {
                "type": "transcript.partial",
                "stream_epoch": self.claims.stream_epoch,
                "generation_id": self._active_generation,
                "segments": event.get("segments", []),
            }
        if event_type == "transport_state":
            return {
                "type": "assistant.state",
                "stream_epoch": self.claims.stream_epoch,
                "generation_id": self._active_generation,
                "state": event.get("state"),
            }
        if event_type == "gateway_error":
            return {
                "type": "session.close",
                "stream_epoch": self.claims.stream_epoch,
                "reason": event.get("code", "gateway_error"),
            }
        raise ProtocolError("bridge emitted an unsupported event")

    def _translate_ui_event(self, event: dict[str, object]) -> dict[str, object] | None:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise ProtocolError("bridge UI event type is invalid")
        mapping = {
            "assistant_state": "assistant.state",
            "assistant_expression": "screen.expression",
            "assistant_subtitle": "screen.subtitle",
            "transcript_delta": "transcript.partial",
            "transcript_final": "transcript.final",
        }
        output_type = mapping.get(event_type)
        if output_type is None:
            if event_type in UI_EVENT_TYPES:
                return None
            raise ProtocolError("bridge UI event is not allowlisted")
        result: dict[str, object] = {
            "type": output_type,
            "stream_epoch": self.claims.stream_epoch,
        }
        for key in ("turn_id", "generation_id", "tool_epoch", "state", "expression", "text", "at"):
            if key in event:
                result[key] = event[key]
        generation_id = result.get("generation_id")
        if generation_id is not None:
            if not isinstance(generation_id, int):
                raise ProtocolError("bridge UI event generation is invalid")
            generation_id = self._device_generation_id(generation_id)
            if generation_id < self._active_generation:
                raise ProtocolError("bridge UI event generation is stale")
            result["generation_id"] = generation_id
        return result

    @staticmethod
    def _device_generation_id(bridge_generation_id: int) -> int:
        """Map Agent generation zero onto the device protocol's positive range."""
        if (
            isinstance(bridge_generation_id, bool)
            or bridge_generation_id < 0
            or bridge_generation_id >= _MAX_UINT32
        ):
            raise ProtocolError("bridge generation is outside the device range")
        return bridge_generation_id + 1

    @staticmethod
    def _bridge_generation_id(device_generation_id: int) -> int:
        if (
            isinstance(device_generation_id, bool)
            or device_generation_id <= 0
            or device_generation_id > _MAX_UINT32
        ):
            raise ProtocolError("device generation is outside the bridge range")
        return device_generation_id - 1

    def _project_event_to_bridge(self, event: dict[str, object]) -> dict[str, object]:
        result = dict(event)
        if result.get("type") in {"vad.start", "vad.end"}:
            result.pop("stream_epoch", None)
        generation_id = result.get("generation_id")
        if isinstance(generation_id, int) and generation_id > 0:
            result["generation_id"] = self._bridge_generation_id(generation_id)
        return result
