"""Bridge Mini Program PCM frames to one existing LiveKit room participant."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from livekit import rtc
from livekit.api import AccessToken, RoomAgentDispatch, RoomConfiguration, VideoGrants

from services.common.miniprogram_gateway_ticket import (
    MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA,
    GatewayTicketClaims,
)
from services.miniprogram_gateway.audio_processing import MiniProgramAudioProcessor
from services.miniprogram_gateway.config import MiniProgramGatewaySettings
from services.miniprogram_gateway.protocol import FrameType, PcmFrame, encode_pcm_frame

logger = logging.getLogger(__name__)
UI_TOPIC = "voice-agent.ui"
CONTROL_ACK_TRACK_NAME = "memoria-ack"


class GatewayMediaError(ValueError):
    """A valid gateway connection attempted invalid media or cannot use its room."""


@dataclass(frozen=True, slots=True)
class GatewayOutboundMessage:
    binary: bytes | None = None
    event: dict[str, object] | None = None
    audio_reference: bytes | None = None

    def __post_init__(self) -> None:
        if (self.binary is None) == (self.event is None):
            raise ValueError("an outbound gateway message must contain exactly one payload")
        if self.audio_reference is not None and self.binary is None:
            raise ValueError("an outbound audio reference requires a binary payload")


class PcmFrameAccumulator:
    """Reframe arbitrary RecorderManager PCM chunks into fixed LiveKit audio frames."""

    def __init__(self, *, sample_rate: int, frame_ms: int, max_buffered_frames: int = 10) -> None:
        samples_per_frame = sample_rate * frame_ms // 1_000
        if (
            sample_rate <= 0
            or frame_ms <= 0
            or sample_rate * frame_ms % 1_000 != 0
            or max_buffered_frames <= 0
        ):
            raise ValueError("invalid PCM frame accumulator configuration")
        self.samples_per_frame = samples_per_frame
        self.bytes_per_frame = samples_per_frame * 2
        self._max_buffered_bytes = self.bytes_per_frame * max_buffered_frames
        self._pending = bytearray()

    def feed(self, payload: bytes) -> tuple[bytes, ...]:
        if len(payload) % 2:
            raise GatewayMediaError("PCM16 payload must contain whole samples")
        self._pending.extend(payload)
        if len(self._pending) > self._max_buffered_bytes:
            self._pending.clear()
            raise GatewayMediaError("PCM uplink buffer exceeded limit")
        frames: list[bytes] = []
        while len(self._pending) >= self.bytes_per_frame:
            frames.append(bytes(self._pending[: self.bytes_per_frame]))
            del self._pending[: self.bytes_per_frame]
        return tuple(frames)


class MiniProgramLiveKitBridge:
    """Owns one server-side user participant for one authenticated Mini Program socket."""

    def __init__(
        self,
        *,
        settings: MiniProgramGatewaySettings,
        claims: GatewayTicketClaims,
    ) -> None:
        if claims.voice_backend != "cascade":
            raise GatewayMediaError("only cascade sessions may use the media gateway")
        if claims.agent_name != settings.livekit_agent_name:
            raise GatewayMediaError("gateway ticket agent does not match configured agent")
        self._settings = settings
        self._claims = claims
        self._room: Any | None = None
        self._audio_source: Any | None = None
        self._publication: Any | None = None
        self._downlink_sequence = 0
        self._last_uplink_sequence: int | None = None
        self._uplink_message_count = 0
        self._uplink_payload_bytes = 0
        self._uplink_livekit_frame_count = 0
        self._downlink_drop_count = 0
        self._turn_id: int | None = None
        self._generation_id: int | None = None
        self._audio_generation_id: int | None = None
        self._downlink_generation_protocol = False
        self._downlink_quarantine_until = 0.0
        self._uplink = PcmFrameAccumulator(
            sample_rate=settings.miniprogram_gateway_uplink_sample_rate,
            frame_ms=settings.miniprogram_gateway_frame_ms,
        )
        self._audio_processor = MiniProgramAudioProcessor(
            enabled=settings.miniprogram_gateway_aec_enabled,
            downlink_sample_rate=settings.miniprogram_gateway_downlink_sample_rate,
            uplink_sample_rate=settings.miniprogram_gateway_uplink_sample_rate,
            stream_delay_ms=settings.miniprogram_gateway_aec_stream_delay_ms,
            active_window_ms=settings.miniprogram_gateway_aec_active_window_ms,
        )
        self._audio_messages: asyncio.Queue[GatewayOutboundMessage] = asyncio.Queue(
            maxsize=settings.miniprogram_gateway_audio_queue_frames
        )
        self._event_messages: asyncio.Queue[GatewayOutboundMessage] = asyncio.Queue(
            maxsize=settings.miniprogram_gateway_event_queue_size
        )
        self._outbound_ready = asyncio.Event()
        self._room_disconnected = asyncio.Event()
        self._audio_streams: set[Any] = set()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def ready_event(self) -> dict[str, object]:
        return {
            "type": "ready",
            "protocol_version": 1,
            "session_id": self._claims.session_id,
            "audio": {
                "sample_rate": self._settings.miniprogram_gateway_downlink_sample_rate,
                "channels": 1,
                "sample_format": "s16le",
                "frame_ms": self._settings.miniprogram_gateway_frame_ms,
                "frame_protocol_version": (
                    2 if self._downlink_generation_protocol else 1
                ),
            },
        }

    def set_downlink_generation_protocol(self, enabled: bool) -> None:
        self._downlink_generation_protocol = enabled

    async def connect(self) -> None:
        if self._closed:
            raise GatewayMediaError("media bridge is closed")
        if not self._settings.livekit_url or not self._settings.livekit_api_key:
            raise GatewayMediaError("LiveKit gateway configuration is unavailable")
        if not self._settings.livekit_api_secret:
            raise GatewayMediaError("LiveKit gateway credentials are unavailable")
        room = rtc.Room()
        self._room = room
        self._register_room_handlers(room)
        try:
            await room.connect(self._settings.livekit_url, self._mint_livekit_token())
            source = rtc.AudioSource(
                self._settings.miniprogram_gateway_uplink_sample_rate,
                1,
                queue_size_ms=self._settings.miniprogram_gateway_frame_ms * 10,
            )
            track = rtc.LocalAudioTrack.create_audio_track("miniprogram-microphone", source)
            self._publication = await room.local_participant.publish_track(
                track,
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
            )
            self._audio_source = source
            self._subscribe_existing_agent_audio(room)
        except Exception as exc:
            await self.close()
            raise GatewayMediaError("could not connect gateway participant to LiveKit") from exc

    async def accept_uplink(self, frame: PcmFrame) -> None:
        if self._closed or self._audio_source is None:
            raise GatewayMediaError("media bridge is not connected")
        if frame.frame_type is not FrameType.UPLINK_AUDIO:
            raise GatewayMediaError("media bridge received a non-uplink frame")
        if frame.generation_id is not None:
            raise GatewayMediaError("PCM uplink must not define a generation")
        if self._last_uplink_sequence is not None:
            expected = (self._last_uplink_sequence + 1) & 0xFFFFFFFF
            if frame.sequence != expected:
                raise GatewayMediaError("PCM uplink sequence is not contiguous")
        self._last_uplink_sequence = frame.sequence
        self._uplink_message_count += 1
        self._uplink_payload_bytes += len(frame.payload)
        if self._uplink_message_count == 1:
            logger.info("mini_program_uplink_started payload_bytes=%s", len(frame.payload))
        for pcm in self._uplink.feed(frame.payload):
            pcm = self._audio_processor.process_uplink(pcm)
            audio_frame = rtc.AudioFrame(
                data=pcm,
                sample_rate=self._settings.miniprogram_gateway_uplink_sample_rate,
                num_channels=1,
                samples_per_channel=self._uplink.samples_per_frame,
            )
            await self._audio_source.capture_frame(audio_frame)
            self._uplink_livekit_frame_count += 1

    async def next_outbound(self) -> GatewayOutboundMessage:
        """Prioritize Agent UI/transcription events without blocking audio when idle."""
        while True:
            try:
                return self._event_messages.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                return self._audio_messages.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._outbound_ready.clear()
            if not self._event_messages.empty() or not self._audio_messages.empty():
                self._outbound_ready.set()
                continue
            await self._outbound_ready.wait()

    def outbound_sent(self, message: GatewayOutboundMessage) -> None:
        """Advance AEC only for bytes/control barriers accepted by the WebSocket."""
        if message.audio_reference is not None:
            self._audio_processor.observe_downlink(message.audio_reference)
        if message.event is not None and message.event.get("type") == "audio_reset":
            self._audio_processor.reset()

    def accept_transport_event(self, event: dict[str, object]) -> None:
        """Record bounded client playout facts without accepting business commands."""
        logger.info(
            "mini_program_playout_event type=%s generation_id=%s "
            "barrier_sequence=%s client_timestamp_ms=%s session_id=%s",
            event.get("type"),
            event.get("generation_id"),
            event.get("barrier_sequence"),
            event.get("client_timestamp_ms"),
            self._claims.session_id,
        )

    async def wait_for_room_disconnect(self) -> None:
        await self._room_disconnected.wait()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in tuple(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        for stream in tuple(self._audio_streams):
            with contextlib.suppress(Exception):
                await stream.aclose()
        self._audio_streams.clear()
        room = self._room
        publication = self._publication
        self._publication = None
        self._audio_source = None
        self._room = None
        if room is not None:
            if publication is not None:
                with contextlib.suppress(Exception):
                    await room.local_participant.unpublish_track(publication.sid)
            with contextlib.suppress(Exception):
                await room.disconnect()
            logger.info(
                "mini_program_media_summary uplink_messages=%s payload_bytes=%s "
                "livekit_frames=%s downlink_drops=%s",
                self._uplink_message_count,
                self._uplink_payload_bytes,
                self._uplink_livekit_frame_count,
                self._downlink_drop_count,
            )
        self._room_disconnected.set()

    def _mint_livekit_token(self) -> str:
        access_token = (
            AccessToken(self._settings.livekit_api_key, self._settings.livekit_api_secret)
            .with_identity(self._claims.identity)
            .with_name(self._claims.identity)
            .with_grants(
                VideoGrants(
                    room_join=True,
                    room=self._claims.room_name,
                    can_publish=True,
                    can_subscribe=True,
                )
            )
            .with_ttl(timedelta(seconds=self._settings.miniprogram_gateway_livekit_token_ttl_s))
        )
        access_token.with_room_config(
            RoomConfiguration(
                agents=[
                    RoomAgentDispatch(
                        agent_name=self._claims.agent_name,
                        metadata=(
                            MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA
                            if self._audio_processor.aec_ready
                            else ""
                        ),
                    )
                ]
            )
        )
        return str(access_token.to_jwt())

    def _register_room_handlers(self, room: Any) -> None:
        room.on("track_subscribed", self._on_track_subscribed)
        room.on("data_received", self._on_data_received)
        room.on("transcription_received", self._on_transcription_received)
        room.on("reconnecting", self._on_room_reconnecting)
        room.on("reconnected", self._on_room_reconnected)
        room.on("disconnected", self._on_room_disconnected)

    def _subscribe_existing_agent_audio(self, room: Any) -> None:
        for participant in room.remote_participants.values():
            if not self._is_agent(participant):
                continue
            for publication in participant.track_publications.values():
                track = getattr(publication, "track", None)
                if track is not None:
                    self._on_track_subscribed(track, publication, participant)

    def _on_track_subscribed(self, track: Any, publication: Any, participant: Any) -> None:
        if self._closed or not self._is_agent(participant):
            return
        if getattr(track, "kind", None) != rtc.TrackKind.KIND_AUDIO:
            return
        sid = str(getattr(track, "sid", ""))
        if not sid:
            return
        if any(task.get_name() == f"mini-program-audio-{sid}" for task in self._background_tasks):
            return
        track_name = str(
            getattr(publication, "name", "")
            or getattr(track, "name", "")
        )
        self._spawn(
            self._pump_downlink_track(
                track,
                control_track=track_name == CONTROL_ACK_TRACK_NAME,
            ),
            name=f"mini-program-audio-{sid}",
        )

    async def _pump_downlink_track(
        self,
        track: Any,
        *,
        control_track: bool = False,
    ) -> None:
        stream = rtc.AudioStream.from_track(
            track=track,
            capacity=1,
            sample_rate=self._settings.miniprogram_gateway_downlink_sample_rate,
            num_channels=1,
            frame_size_ms=self._settings.miniprogram_gateway_frame_ms,
        )
        self._audio_streams.add(stream)
        try:
            async for event in stream:
                if self._closed:
                    return
                pcm = bytes(event.frame.data)
                if not pcm:
                    continue
                sequence = self._downlink_sequence
                self._downlink_sequence = (self._downlink_sequence + 1) & 0xFFFFFFFF
                audio_generation_id = (
                    self._generation_id if control_track else self._audio_generation_id
                )
                if audio_generation_id is None:
                    continue
                if not control_track:
                    if (
                        asyncio.get_running_loop().time()
                        < self._downlink_quarantine_until
                    ):
                        continue
                self._enqueue_audio(
                    GatewayOutboundMessage(
                        binary=encode_pcm_frame(
                            FrameType.DOWNLINK_AUDIO,
                            sequence=sequence,
                            generation_id=(
                                audio_generation_id
                                if self._downlink_generation_protocol
                                else None
                            ),
                            timestamp_ms=int(asyncio.get_running_loop().time() * 1_000),
                            payload=pcm,
                        ),
                        audio_reference=pcm,
                    )
                )
        finally:
            self._audio_streams.discard(stream)
            with contextlib.suppress(Exception):
                await stream.aclose()

    def _on_data_received(self, packet: Any) -> None:
        if self._closed or not self._is_agent(getattr(packet, "participant", None)):
            return
        if getattr(packet, "topic", None) != UI_TOPIC:
            return
        data = getattr(packet, "data", b"")
        if not isinstance(data, bytes) or len(data) > 16 * 1024:
            return
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str):
            return
        generation_id = parsed.get("generation_id")
        generation_changed = False
        first_generation = self._generation_id is None
        if (
            isinstance(generation_id, int)
            and not isinstance(generation_id, bool)
            and generation_id >= 0
        ):
            if self._generation_id is None:
                self._generation_id = generation_id
                generation_changed = True
            elif generation_id < self._generation_id:
                return
            elif generation_id > self._generation_id:
                self._generation_id = generation_id
                generation_changed = True
            if generation_changed:
                self._audio_generation_id = None
                self._downlink_quarantine_until = (
                    0.0
                    if first_generation
                    else (
                        asyncio.get_running_loop().time()
                        + self._settings.miniprogram_gateway_generation_quarantine_ms
                        / 1_000
                    )
                )
                barrier_sequence = self._downlink_sequence
                self._clear_queued_audio()
                self._enqueue_event(
                    GatewayOutboundMessage(
                        event={
                            "type": "audio_reset",
                            "generation_id": generation_id,
                            "barrier_sequence": barrier_sequence,
                        }
                    )
                )
            if (
                parsed.get("type") == "assistant_state"
                and generation_id == self._generation_id
            ):
                if parsed.get("state") == "speaking":
                    self._audio_generation_id = generation_id
                    self._downlink_quarantine_until = 0.0
                elif self._audio_generation_id == generation_id:
                    self._audio_generation_id = None
        turn_id = parsed.get("turn_id")
        if isinstance(turn_id, int) and not isinstance(turn_id, bool) and turn_id >= 0:
            self._turn_id = turn_id
        self._enqueue_event(
            GatewayOutboundMessage(
                event={
                    "type": "ui_event",
                    "topic": UI_TOPIC,
                    "event": parsed,
                }
            )
        )

    def _on_transcription_received(
        self,
        segments: list[Any],
        participant: Any,
        _publication: Any,
    ) -> None:
        if self._closed or not self._is_agent(participant):
            return
        normalized: list[dict[str, object]] = []
        for segment in segments:
            text = getattr(segment, "text", None)
            if not isinstance(text, str) or not text:
                continue
            normalized.append(
                {
                    "id": str(getattr(segment, "id", "")),
                    "text": text[:4_000],
                    "start_time": int(getattr(segment, "start_time", 0)),
                    "end_time": int(getattr(segment, "end_time", 0)),
                    "language": str(getattr(segment, "language", "")),
                    "final": bool(getattr(segment, "final", False)),
                }
            )
        if normalized:
            event: dict[str, object] = {
                "type": "transcription",
                "participant_identity": str(getattr(participant, "identity", "")),
                "segments": normalized,
            }
            if self._turn_id is not None:
                event["turn_id"] = self._turn_id
            if self._generation_id is not None:
                event["generation_id"] = self._generation_id
            self._enqueue_event(
                GatewayOutboundMessage(
                    event=event
                )
            )

    def _on_room_reconnecting(self) -> None:
        self._enqueue_event(GatewayOutboundMessage(event={"type": "transport_state", "state": "reconnecting"}))

    def _on_room_reconnected(self) -> None:
        self._enqueue_event(GatewayOutboundMessage(event={"type": "transport_state", "state": "reconnected"}))

    def _on_room_disconnected(self, _reason: Any) -> None:
        if not self._closed:
            self._enqueue_event(
                GatewayOutboundMessage(
                    event={"type": "gateway_error", "code": "livekit_disconnected"}
                )
            )
        self._room_disconnected.set()

    @staticmethod
    def _is_agent(participant: Any) -> bool:
        return (
            participant is not None
            and getattr(participant, "kind", None) == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        )

    def _enqueue_audio(self, message: GatewayOutboundMessage) -> None:
        if self._audio_messages.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._audio_messages.get_nowait()
                self._downlink_drop_count += 1
                if self._downlink_drop_count == 1 or self._downlink_drop_count % 25 == 0:
                    logger.warning(
                        "mini_program_downlink_drop count=%s",
                        self._downlink_drop_count,
                    )
        self._audio_messages.put_nowait(message)
        self._outbound_ready.set()

    def _clear_queued_audio(self) -> None:
        while True:
            try:
                self._audio_messages.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _enqueue_event(self, message: GatewayOutboundMessage) -> None:
        if self._event_messages.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._event_messages.get_nowait()
        self._event_messages.put_nowait(message)
        self._outbound_ready.set()

    def _spawn(self, coroutine: Coroutine[Any, Any, None], *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)

        def _done(completed: asyncio.Task[None]) -> None:
            self._background_tasks.discard(completed)
            if completed.cancelled():
                return
            if completed.exception() is not None:
                logger.warning(
                    "Mini Program media background task failed task=%s error=%s",
                    completed.get_name(),
                    type(completed.exception()).__name__,
                )

        task.add_done_callback(_done)
