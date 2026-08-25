"""Bidirectional protobuf bridge between Media Edge and Voice Core.

The bridge is intentionally small and provider-neutral.  It owns transport
authentication, session/epoch fencing and bounded delivery; orchestration and
provider work remain in the existing Voice Core.  No StreamCore/Pion source is
embedded here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.interaction_authority import (
    InteractionAuthority,
    InteractionRuntime,
    can_execute_realtime_effect,
    interaction_authority_from_proto,
    interaction_authority_to_proto,
)
from services.agent.src.voice_core.media_bridge_server import (
    MediaBridgeServer,
    MediaBridgeSession,
    PCMFrame,
)
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    MediaEnvelope,
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.speech_timeline import SegmentKind, SpeechSegment, SpeechTimeline

media_pb2: Any = _media_pb2
KWS_HARD_STOP_MIN_CONFIDENCE = 0.8
FLOOR_EFFECT_TTL_MS = 10_000
logger = logging.getLogger(__name__)


def _playback_event_type_from_proto(value: int) -> PlaybackEventType:
    mapping = {
        int(media_pb2.PLAYBACK_EVENT_TYPE_UNSPECIFIED): PlaybackEventType.WATERMARK,
        int(media_pb2.PLAYBACK_EVENT_TYPE_STARTED): PlaybackEventType.STARTED,
        int(media_pb2.PLAYBACK_EVENT_TYPE_PROGRESS): PlaybackEventType.PROGRESS,
        int(media_pb2.PLAYBACK_EVENT_TYPE_ENDED): PlaybackEventType.ENDED,
        int(media_pb2.PLAYBACK_EVENT_TYPE_ERROR): PlaybackEventType.ERROR,
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise ValueError("unsupported playback event type") from exc


@dataclass(frozen=True, slots=True)
class MediaBridgeTLS:
    """Server certificate material for internal mTLS."""

    private_key_pem: bytes
    certificate_chain_pem: bytes
    client_ca_pem: bytes | None = None

    @classmethod
    def from_files(
        cls,
        *,
        private_key_file: str,
        certificate_chain_file: str,
        client_ca_file: str | None = None,
    ) -> MediaBridgeTLS:
        """Load mTLS material without ever logging the key contents."""

        def read_required(path_value: str, label: str) -> bytes:
            path = Path(path_value).expanduser()
            if not path_value.strip() or not path.is_file():
                raise ValueError(f"media bridge {label} file does not exist")
            data = path.read_bytes()
            if not data:
                raise ValueError(f"media bridge {label} file is empty")
            return data

        client_ca = None
        if client_ca_file is not None:
            client_ca = read_required(client_ca_file, "client CA")
        return cls(
            private_key_pem=read_required(private_key_file, "private key"),
            certificate_chain_pem=read_required(certificate_chain_file, "certificate"),
            client_ca_pem=client_ca,
        )

    def credentials(self) -> grpc.ServerCredentials:
        if not self.private_key_pem or not self.certificate_chain_pem:
            raise ValueError("bridge TLS key and certificate are required")
        return grpc.ssl_server_credentials(
            ((self.private_key_pem, self.certificate_chain_pem),),
            root_certificates=self.client_ca_pem,
            require_client_auth=self.client_ca_pem is not None,
        )


ClientEventHandler = Callable[[MediaBridgeSession, MediaEnvelope, int], Awaitable[None]]
AudioFrameHandler = Callable[[MediaBridgeSession, AudioFrame], Awaitable[None]]
SpeechSegmentHandler = Callable[[MediaBridgeSession, SpeechSegment, int], Awaitable[None]]
SessionClosedHandler = Callable[[MediaBridgeSession], Awaitable[None]]
SessionConnectedHandler = Callable[[MediaBridgeSession], Awaitable[None]]
PlaybackProgressHandler = Callable[[MediaBridgeSession, PlaybackProgress], Awaitable[None]]
DownlinkOverflowHandler = Callable[[MediaBridgeSession], Awaitable[None]]


class _PriorityOutgoing:
    """Bounded Critical/Reliable/Coalescing queue with strict priority drain."""

    _RELIABLE_BUDGET = 16

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._critical: deque[media_pb2.CoreToMedia | None] = deque()
        self._reliable: deque[media_pb2.CoreToMedia | None] = deque()
        self._coalescing: deque[media_pb2.CoreToMedia | None] = deque()
        self._ready = asyncio.Event()
        self._space = asyncio.Event()
        self._space.set()
        self._reliable_since_bulk = 0
        self._next_event_sequence = 0
        self._next_shadow_sequence = 0

    def prepare_for_send(
        self,
        message: media_pb2.CoreToMedia | None,
    ) -> media_pb2.CoreToMedia | None:
        """Stamp ordered event identifiers at the actual transport boundary.

        Producers enqueue across priority lanes.  Assigning the shared event
        sequence before that arbitration lets a later critical event overtake
        an earlier reliable event while retaining a larger sequence, causing
        Media Edge to reject the delayed event as stale.
        """

        if message is None:
            return None
        kind = message.WhichOneof("event")
        if kind not in {
            "generation",
            "transcript",
            "state",
            "client",
            "shadow_observation",
            "realtime_effect",
            "floor_effect",
        }:
            return message
        event = getattr(message, kind)
        sequence = self._next_event_sequence
        self._next_event_sequence += 1
        event.sequence = sequence
        if kind == "realtime_effect":
            event.effect_id = (
                f"{event.identity.session_id}:{event.identity.stream_epoch}:effect:{sequence}"
            )
        elif kind == "floor_effect":
            event.effect_id = (
                f"{event.identity.session_id}:{event.identity.stream_epoch}:"
                f"floor:{event.floor_epoch}:{sequence}"
            )
        elif kind == "client":
            try:
                payload = json.loads(bytes(event.json_payload))
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                payload["sequence"] = sequence
                payload["event_id"] = (
                    f"{event.identity.session_id}:{event.identity.stream_epoch}:{sequence}"
                )
                event.json_payload = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
        elif kind == "shadow_observation":
            event.shadow_sequence = self._next_shadow_sequence
            self._next_shadow_sequence += 1
        return message

    @staticmethod
    def _lane(message: media_pb2.CoreToMedia | None) -> str:
        if message is None:
            return "critical"
        kind = message.WhichOneof("event")
        if kind in {"generation", "realtime_effect", "floor_effect", "state", "error"}:
            return "critical"
        if kind == "shadow_observation":
            return "coalescing"
        if kind == "transcript" and not bool(message.transcript.final):
            return "coalescing"
        if kind == "client" and str(message.client.type).startswith("turn.provisional."):
            return "coalescing"
        return "reliable"

    @staticmethod
    def _coalescing_key(message: media_pb2.CoreToMedia | None) -> tuple[object, ...] | None:
        if message is None:
            return None
        kind = message.WhichOneof("event")
        if kind == "transcript":
            return (kind, int(message.transcript.turn_id))
        if kind == "client":
            return (kind, str(message.client.type), int(message.client.turn_id))
        return None

    def put_nowait(self, message: media_pb2.CoreToMedia | None) -> None:
        lane = self._lane(message)
        if lane == "coalescing":
            key = self._coalescing_key(message)
            if key is not None:
                self._coalescing = deque(
                    queued for queued in self._coalescing if self._coalescing_key(queued) != key
                )
        if self.qsize() >= self.maxsize:
            raise asyncio.QueueFull
        target = {
            "critical": self._critical,
            "reliable": self._reliable,
            "coalescing": self._coalescing,
        }[lane]
        target.append(message)
        self._ready.set()
        if self.qsize() >= self.maxsize:
            self._space.clear()

    async def put(self, message: media_pb2.CoreToMedia | None) -> None:
        while True:
            try:
                self.put_nowait(message)
                return
            except asyncio.QueueFull:
                self._space.clear()
                if self.qsize() < self.maxsize:
                    continue
                await self._space.wait()

    def get_nowait(self) -> media_pb2.CoreToMedia | None:
        if self._critical:
            message = self._critical.popleft()
        elif self._coalescing and (
            not self._reliable or self._reliable_since_bulk >= self._RELIABLE_BUDGET
        ):
            message = self._coalescing.popleft()
            self._reliable_since_bulk = 0
        elif self._reliable:
            message = self._reliable.popleft()
            self._reliable_since_bulk += 1
        elif self._coalescing:
            message = self._coalescing.popleft()
            self._reliable_since_bulk = 0
        else:
            raise asyncio.QueueEmpty
        message = self.prepare_for_send(message)
        self._space.set()
        if self.empty():
            self._ready.clear()
        return message

    async def get(self) -> media_pb2.CoreToMedia | None:
        while True:
            try:
                return self.get_nowait()
            except asyncio.QueueEmpty:
                self._ready.clear()
                if not self.empty():
                    continue
                await self._ready.wait()

    def evict_coalescing(self) -> media_pb2.CoreToMedia | None:
        if not self._coalescing:
            return None
        message = self._coalescing.popleft()
        self._space.set()
        if self.empty():
            self._ready.clear()
        return message

    def clear(self) -> None:
        self._critical.clear()
        self._reliable.clear()
        self._coalescing.clear()
        self._reliable_since_bulk = 0
        self._ready.clear()
        self._space.set()

    def qsize(self) -> int:
        return len(self._critical) + len(self._reliable) + len(self._coalescing)

    def empty(self) -> bool:
        return self.qsize() == 0


@dataclass(slots=True)
class _Connection:
    session: MediaBridgeSession
    outgoing: _PriorityOutgoing
    dropped_shadow_observations: int = 0
    closed: bool = False
    close_notified: bool = False


def _identity_from_proto(value: Any) -> SessionIdentity:
    return SessionIdentity(
        session_id=str(value.session_id),
        account_id=str(value.account_id),
        participant_id=str(value.participant_id),
        device_id=str(value.device_id),
        client_type=str(value.client_type or "h5"),
        stream_epoch=int(value.stream_epoch),
        subject_id=str(value.subject_id),
        binding_id=str(value.binding_id),
        binding_version=int(value.binding_version),
        runtime_profile_version=int(value.runtime_profile_version),
    )


def _identity_to_proto(identity: SessionIdentity) -> Any:
    return media_pb2.SessionIdentity(
        session_id=identity.session_id,
        account_id=identity.account_id,
        participant_id=identity.participant_id,
        device_id=identity.device_id,
        client_type=identity.client_type,
        stream_epoch=identity.stream_epoch,
        subject_id=identity.subject_id,
        binding_id=identity.binding_id,
        binding_version=identity.binding_version,
        runtime_profile_version=identity.runtime_profile_version,
    )


def floor_state_for_phase(phase: str) -> int | None:
    """Map the Python-owned interaction phase to one typed Floor state."""

    return {
        "connecting": media_pb2.FLOOR_STATE_SILENCE,
        "ready": media_pb2.FLOOR_STATE_SILENCE,
        "speaker_enroll": media_pb2.FLOOR_STATE_SILENCE,
        "listening": media_pb2.FLOOR_STATE_SILENCE,
        "user_speaking": media_pb2.FLOOR_STATE_USER_HOLDS_FLOOR,
        "backchannel": media_pb2.FLOOR_STATE_OVERLAP,
        "thinking": media_pb2.FLOOR_STATE_SILENCE,
        "thinking_silent": media_pb2.FLOOR_STATE_SILENCE,
        "tool_waiting": media_pb2.FLOOR_STATE_SILENCE,
        "speaking": media_pb2.FLOOR_STATE_ASSISTANT_HOLDS_FLOOR,
        "interrupted": media_pb2.FLOOR_STATE_USER_HOLDS_FLOOR,
        "recovering": media_pb2.FLOOR_STATE_SILENCE,
        "closed": media_pb2.FLOOR_STATE_SILENCE,
    }.get(phase)


class MediaBridgeGrpcServer:
    """A real asyncio gRPC streaming endpoint over the media-v1 contract."""

    _SERVICE = "memoria.media.v1.VoiceMediaBridge"

    def __init__(
        self,
        *,
        max_pending_audio_frames: int = 20,
        max_pending_messages: int = 128,
        allow_go_shadow: bool = False,
        on_client_event: ClientEventHandler | None = None,
        on_audio_frame: AudioFrameHandler | None = None,
        on_speech_segment: SpeechSegmentHandler | None = None,
        on_session_closed: SessionClosedHandler | None = None,
        on_session_connected: SessionConnectedHandler | None = None,
        on_playback_progress: PlaybackProgressHandler | None = None,
        on_downlink_overflow: DownlinkOverflowHandler | None = None,
    ) -> None:
        if max_pending_messages <= 0:
            raise ValueError("max_pending_messages must be positive")
        self.bridge = MediaBridgeServer(max_pending_audio_frames=max_pending_audio_frames)
        self.max_pending_messages = max_pending_messages
        self.allow_go_shadow = bool(allow_go_shadow)
        self.on_client_event = on_client_event
        self.on_audio_frame = on_audio_frame
        self.on_speech_segment = on_speech_segment
        self.on_session_closed = on_session_closed
        self.on_session_connected = on_session_connected
        self.on_playback_progress = on_playback_progress
        self.on_downlink_overflow = on_downlink_overflow
        self._connections: dict[str, _Connection] = {}
        self._transport_sessions_seen: set[str] = set()
        self._closed_session_notifications: set[str] = set()
        self._server: grpc.aio.Server | None = None
        self._health = health.aio.HealthServicer()

    async def start(self, address: str, *, tls: MediaBridgeTLS | None = None) -> int:
        if self._server is not None:
            raise RuntimeError("media bridge server is already started")
        if not address.strip():
            raise ValueError("bridge address is required")
        server = grpc.aio.server()
        handler = grpc.method_handlers_generic_handler(
            self._SERVICE,
            {
                "Connect": grpc.stream_stream_rpc_method_handler(
                    self.connect,
                    request_deserializer=media_pb2.MediaToCore.FromString,
                    response_serializer=media_pb2.CoreToMedia.SerializeToString,
                )
            },
        )
        server.add_generic_rpc_handlers((handler,))
        health_pb2_grpc.add_HealthServicer_to_server(self._health, server)
        if tls is None:
            port = server.add_insecure_port(address)
        else:
            port = server.add_secure_port(address, tls.credentials())
        if port <= 0:
            raise RuntimeError(f"failed to bind media bridge address: {address}")
        await self._health.set(
            self._SERVICE,
            health_pb2.HealthCheckResponse.SERVING,
        )
        await server.start()
        self._server = server
        return cast(int, port)

    async def stop(self, grace_s: float = 1.0) -> None:
        server, self._server = self._server, None
        if server is not None:
            await self._health.set(
                self._SERVICE,
                health_pb2.HealthCheckResponse.NOT_SERVING,
            )
            await server.stop(grace_s)
        for connection in tuple(self._connections.values()):
            connection.closed = True
            connection.session.close()
            if not connection.close_notified:
                connection.close_notified = True
                await self._notify_session_closed(connection.session)
        self._connections.clear()
        # A client may have disconnected before the server itself stops.  The
        # transport map is then empty, but the bounded bridge session still
        # owns a provider/runtime context; close those sessions explicitly so
        # shutdown does not leave the reconnect-grace task pending forever.
        for session_id, session in tuple(self.bridge.sessions.items()):
            self.bridge.close(session_id)
            await self._notify_session_closed(session, force=True)

    async def connect(
        self,
        requests: AsyncIterator[media_pb2.MediaToCore],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[media_pb2.CoreToMedia]:
        outgoing = _PriorityOutgoing(self.max_pending_messages)
        ready = asyncio.Event()
        holder: dict[str, object] = {}
        consumer = asyncio.create_task(
            self._consume(requests, outgoing, ready, holder),
            name="media-bridge-grpc-reader",
        )
        connection: _Connection | None = None
        try:
            await ready.wait()
            error = holder.get("error")
            if error is not None:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
                return
            value = holder.get("connection")
            if not isinstance(value, _Connection):
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "media hello is required")
                return
            connection = value
            fence = connection.session.fence
            yield media_pb2.CoreToMedia(
                accepted=media_pb2.SessionAccepted(
                    identity=_identity_to_proto(connection.session.identity),
                    state=media_pb2.CONVERSATION_STATE_LISTENING,
                    current_generation_id=fence.generation_id,
                    interaction_authority=interaction_authority_to_proto(
                        connection.session.interaction_authority
                    ),
                    current_turn_id=fence.turn_id,
                    current_tool_epoch=fence.tool_epoch,
                    current_session_epoch=fence.session_epoch,
                    task_epoch=connection.session.task_epoch,
                    context_version=connection.session.context_version,
                )
            )
            if (
                connection.session.fence.turn_id
                or connection.session.fence.generation_id
                or connection.session.fence.tool_epoch
            ):
                action = (
                    media_pb2.GENERATION_ACTION_RESUME
                    if connection.session.generation_active
                    else media_pb2.GENERATION_ACTION_CANCEL
                )
                resumed = media_pb2.CoreToMedia(
                    generation=media_pb2.GenerationControl(
                        identity=_identity_to_proto(connection.session.identity),
                        turn_id=connection.session.fence.turn_id,
                        generation_id=connection.session.fence.generation_id,
                        tool_epoch=connection.session.fence.tool_epoch,
                        session_epoch=connection.session.fence.session_epoch,
                        action=action,
                        reason="stream_reconnected",
                    )
                )
                yield outgoing.prepare_for_send(resumed)
            while True:
                message = await outgoing.get()
                if message is None:
                    break
                yield message
                if connection.closed and outgoing.empty():
                    # A closed transport replaces pending output with either
                    # its terminal control or a sentinel.  Do not abandon a
                    # terminal queued behind the message just yielded.
                    break
        finally:
            if not consumer.done():
                consumer.cancel()
                try:
                    await consumer
                except asyncio.CancelledError:
                    pass
            if connection is not None:
                await self._finish_connection(connection)

    async def _consume(
        self,
        requests: AsyncIterator[media_pb2.MediaToCore],
        outgoing: _PriorityOutgoing,
        ready: asyncio.Event,
        holder: dict[str, object],
    ) -> None:
        connection: _Connection | None = None
        try:
            async for request in requests:
                event_name = request.WhichOneof("event")
                if connection is None:
                    if event_name != "hello":
                        raise ValueError("the first media event must be hello")
                    self._validate_hello(request.hello)
                    interaction_authority = self._select_interaction_authority(
                        int(request.hello.interaction_authority)
                    )
                    connection = self._open_connection(
                        _identity_from_proto(request.hello.identity),
                        traceparent=str(request.hello.traceparent or ""),
                        interaction_authority=interaction_authority,
                    )
                    connection.outgoing = outgoing
                    if self.on_session_connected is not None:
                        await self.on_session_connected(connection.session)
                    holder["connection"] = connection
                    ready.set()
                    continue
                if connection.closed:
                    break
                await self._handle_request(connection, request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("media bridge request loop failed")
            holder["error"] = exc
            if not ready.is_set():
                ready.set()
        finally:
            if not ready.is_set():
                ready.set()
            if connection is not None:
                self._close_connection(connection)
            await outgoing.put(None)

    @staticmethod
    def _validate_hello(hello: Any) -> None:
        """Reject explicitly mismatched media formats at the session boundary."""

        identity = getattr(hello, "identity", None)
        if identity is None:
            raise ValueError("media hello identity is required")
        for field_name in ("session_id", "account_id", "device_id"):
            if not str(getattr(identity, field_name, "")).strip():
                raise ValueError(f"media hello {field_name} is required")
        if str(getattr(identity, "client_type", "")).strip() not in {"h5", "device"}:
            raise ValueError("media hello client_type must be h5 or device")

        expected = {
            "uplink_format": (16_000, 1, 20),
            "downlink_format": (24_000, 1, 20),
        }
        for field_name, (sample_rate, channels, frame_ms) in expected.items():
            value = getattr(hello, field_name)
            # Older local clients omitted the downlink message; the bridge
            # uses the protocol defaults in that case, while rejecting every
            # explicitly supplied mismatch.
            if (
                int(value.encoding) == media_pb2.AUDIO_ENCODING_UNSPECIFIED
                and int(value.sample_rate) == 0
                and int(value.channels) == 0
                and int(value.frame_ms) == 0
            ):
                continue
            if int(value.encoding) != media_pb2.AUDIO_ENCODING_PCM_S16LE:
                raise ValueError(f"{field_name} must use PCM_S16LE")
            if (
                int(value.sample_rate) != sample_rate
                or int(value.channels) != channels
                or int(value.frame_ms) != frame_ms
            ):
                raise ValueError(f"{field_name} format is not supported")

    def _select_interaction_authority(self, requested: int) -> InteractionAuthority:
        mode = interaction_authority_from_proto(requested)
        if mode is InteractionAuthority.GO_SHADOW and self.allow_go_shadow:
            return mode
        # Go authority is not selectable until A6 parity, SLO and rollback
        # gates are proven. Unknown/old clients also fail closed here.
        return InteractionAuthority.PYTHON_AUTHORITATIVE

    def _open_connection(
        self,
        identity: SessionIdentity,
        *,
        traceparent: str = "",
        interaction_authority: InteractionAuthority = InteractionAuthority.PYTHON_AUTHORITATIVE,
    ) -> _Connection:
        session = self.bridge.get(identity.session_id)
        if session is not None and not session.identity.has_same_reconnect_authority(identity):
            raise ValueError("media session reconnect authority changed")
        existing = self._connections.get(identity.session_id)
        if existing is not None:
            if identity.stream_epoch <= existing.session.identity.stream_epoch:
                raise ValueError("media session already has an active bridge")
            # A newer stream epoch owns the session immediately.  Wake the
            # older writer; its late ``finally`` must not tear down the new
            # connection or provider context.
            self._terminate_outgoing(existing)
            self._connections.pop(identity.session_id, None)
        if session is None:
            session = self.bridge.open(
                identity,
                traceparent=traceparent,
                interaction_authority=interaction_authority,
            )
        elif not session.reconnect(identity):
            raise ValueError("media stream epoch did not advance")
        else:
            session.interaction_authority = interaction_authority
        self._closed_session_notifications.discard(identity.session_id)
        connection = _Connection(
            session=session,
            outgoing=_PriorityOutgoing(self.max_pending_messages),
        )
        self._connections[identity.session_id] = connection
        self._transport_sessions_seen.add(identity.session_id)
        return connection

    def _close_connection(self, connection: _Connection) -> None:
        connection.closed = True
        session_id = connection.session.identity.session_id
        if self._connections.get(session_id) is connection:
            self._connections.pop(session_id, None)

    async def _finish_connection(self, connection: _Connection) -> None:
        """Detach one transport without closing a replacement epoch."""

        current = self._connections.get(connection.session.identity.session_id)
        replaced = current is not None and current is not connection
        self._close_connection(connection)
        if not replaced and not connection.close_notified:
            connection.close_notified = True
            await self._notify_session_closed(connection.session)

    async def _notify_session_closed(
        self,
        session: MediaBridgeSession,
        *,
        force: bool = False,
    ) -> None:
        """Call the lifecycle hook once for the current session epoch."""

        session_id = session.identity.session_id
        if not force and session_id in self._closed_session_notifications:
            return
        self._closed_session_notifications.add(session_id)
        if self.on_session_closed is not None:
            await self.on_session_closed(session)

    async def _handle_request(
        self,
        connection: _Connection,
        request: media_pb2.MediaToCore,
    ) -> None:
        event_name = request.WhichOneof("event")
        if event_name is None:
            await self._error(connection, "invalid_media_event", "media event is required")
            return
        if event_name == "audio":
            audio = request.audio
            self._require_identity(connection, audio.identity)
            raw_payload = bytes(audio.payload)
            frame_samples = int(audio.frame_samples)
            if frame_samples <= 0 or len(raw_payload) != frame_samples * 2:
                await self._error(
                    connection,
                    "invalid_audio_frame",
                    "PCM payload length does not match frame_samples",
                )
                return
            frame = AudioFrame(
                identity=connection.session.identity,
                sequence=int(audio.sequence),
                capture_start_sample=int(audio.capture_start_sample),
                frame_samples=frame_samples,
                payload=raw_payload,
                crc32c=int(audio.crc32c) if audio.crc32c else None,
                discontinuity=bool(audio.discontinuity),
                loss_concealed=bool(audio.loss_concealed),
            )
            accepted = connection.session.accept_uplink(frame)
            if not accepted:
                await self._error(connection, "stale_or_invalid_audio", "audio frame rejected")
            else:
                if self.on_audio_frame is not None:
                    await self.on_audio_frame(connection.session, frame)
                # ``accept_uplink`` reserves bounded queue capacity.  Release
                # it only after the provider callback has consumed the frame;
                # a missing callback is still an intentional sink, not a
                # reason to retain every frame forever.
                connection.session.pop_uplink(frame.sequence)
                connection.session.ack_uplink(frame.sequence)
            return
        if event_name == "vad":
            event = request.vad
            self._require_identity(connection, event.identity)
            sample = int(event.sample_position)
            speech_end = int(event.type) == media_pb2.VAD_EVENT_SPEECH_END
            voiced_end_sample: int | None = None
            if speech_end:
                if not event.HasField("voiced_end_sample"):
                    await self._error(
                        connection,
                        "invalid_vad_event",
                        "speech-end VAD requires voiced_end_sample",
                    )
                    return
                voiced_end_sample = int(event.voiced_end_sample)
                if voiced_end_sample > sample:
                    await self._error(
                        connection,
                        "invalid_vad_event",
                        "voiced_end_sample cannot exceed sample_position",
                    )
                    return
            segment = SpeechSegment(
                session_id=connection.session.identity.session_id,
                stream_epoch=connection.session.identity.stream_epoch,
                provider_task_epoch=0,
                segment_id=f"vad:{sample}:{event.type}",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=sample,
                capture_end_sample=sample + 1,
                confidence=float(event.probability),
                final=speech_end,
                voiced_end_sample=voiced_end_sample,
            )
            accepted = connection.session.timeline.add(segment)
            if (
                accepted
                and segment.hard_stop
                and (segment.confidence or 0.0) >= KWS_HARD_STOP_MIN_CONFIDENCE
            ):
                connection.session.apply_local_keyword_stop(
                    confidence=float(segment.confidence or 0.0),
                    min_confidence=KWS_HARD_STOP_MIN_CONFIDENCE,
                )
            if accepted and self.on_speech_segment is not None:
                await self.on_speech_segment(connection.session, segment, 0)
            return
        if event_name == "keyword":
            event = request.keyword
            self._require_identity(connection, event.identity)
            detected_monotonic_ms = (
                int(event.detected_monotonic_ms) if event.HasField("detected_monotonic_ms") else 0
            )
            segment = SpeechSegment(
                session_id=connection.session.identity.session_id,
                stream_epoch=connection.session.identity.stream_epoch,
                provider_task_epoch=0,
                segment_id=f"kws:{event.start_sample}:{event.end_sample}:{event.keyword}",
                revision=1,
                kind=SegmentKind.KWS,
                capture_start_sample=int(event.start_sample),
                capture_end_sample=int(event.end_sample),
                text=str(event.keyword),
                final=True,
                confidence=float(event.confidence),
                hard_stop=bool(event.hard_stop),
            )
            accepted = connection.session.timeline.add(segment)
            if (
                accepted
                and segment.hard_stop
                and (segment.confidence or 0.0) >= KWS_HARD_STOP_MIN_CONFIDENCE
            ):
                connection.session.apply_local_keyword_stop(
                    confidence=float(segment.confidence or 0.0),
                    min_confidence=KWS_HARD_STOP_MIN_CONFIDENCE,
                )
            if accepted and self.on_speech_segment is not None:
                await self.on_speech_segment(
                    connection.session,
                    segment,
                    detected_monotonic_ms,
                )
            return
        if event_name == "device":
            event = request.device
            self._require_identity(connection, event.identity)
            try:
                envelope = MediaEnvelope.decode(bytes(event.json_payload))
            except ValueError as exc:
                await self._error(connection, "invalid_client_event", str(exc))
                return
            if envelope.type == "client.stop_assistant":
                if not self.bridge.accept_client_event(envelope):
                    await self._error(connection, "stale_client_event", "client event rejected")
                    return
                if self.on_client_event is not None:
                    await self.on_client_event(
                        connection.session,
                        envelope,
                        int(event.monotonic_ms),
                    )
                await self.emit_generation(
                    connection.session.identity.session_id,
                    connection.session.fence,
                    action=media_pb2.GENERATION_ACTION_CANCEL,
                    reason=str(envelope.payload.get("reason", "client_stop")),
                )
            elif envelope.type == "client.playback.progress":
                try:
                    payload = envelope.payload

                    def payload_int(name: str) -> int:
                        if name not in payload:
                            raise ValueError(f"{name} is required")
                        value = payload[name]
                        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                            raise ValueError(f"{name} must be a non-negative integer")
                        return int(value)

                    if "received_sequence" not in payload:
                        raise ValueError("received_sequence is required")
                    approximate = payload.get("approximate", True)
                    if not isinstance(approximate, bool):
                        raise ValueError("approximate must be boolean")
                    session_epoch = payload.get("session_epoch", 0)
                    if (
                        isinstance(session_epoch, bool)
                        or not isinstance(session_epoch, int)
                        or session_epoch < 0
                    ):
                        raise ValueError("session_epoch must be a non-negative integer")
                    progress = PlaybackProgress(
                        identity=connection.session.identity,
                        generation_id=payload_int("generation_id"),
                        received_sequence=payload_int("received_sequence"),
                        rendered_sample_end=payload_int("rendered_sample_end"),
                        client_monotonic_ms=payload_int("client_monotonic_ms"),
                        approximate=approximate,
                        turn_id=payload_int("turn_id"),
                        tool_epoch=payload_int("tool_epoch"),
                        session_epoch=int(session_epoch),
                        event_type=PlaybackEventType(
                            payload.get("event_type", PlaybackEventType.WATERMARK.value)
                        ),
                    )
                except (TypeError, ValueError) as exc:
                    await self._error(connection, "invalid_playback_progress", str(exc))
                    return
                if not connection.session.accept_client_progress(envelope):
                    await self._error(
                        connection, "stale_playback_progress", "playback progress rejected"
                    )
                    return
                if self.on_playback_progress is not None:
                    await self.on_playback_progress(connection.session, progress)
            elif self.on_client_event is not None:
                await self.on_client_event(connection.session, envelope, 0)
            return
        if event_name == "playback":
            event = request.playback
            self._require_identity(connection, event.identity)
            if self.on_playback_progress is not None:
                await self.on_playback_progress(
                    connection.session,
                    PlaybackProgress(
                        identity=connection.session.identity,
                        generation_id=int(event.generation_id),
                        received_sequence=int(event.received_sequence),
                        rendered_sample_end=int(event.rendered_sample_end),
                        client_monotonic_ms=int(event.client_monotonic_ms),
                        approximate=bool(event.approximate),
                        turn_id=int(event.turn_id),
                        tool_epoch=int(event.tool_epoch),
                        session_epoch=int(event.session_epoch),
                        event_type=_playback_event_type_from_proto(int(event.event_type)),
                    ),
                )
            return
        if event_name == "metric":
            return
        if event_name == "hello":
            await self._error(connection, "duplicate_hello", "hello is only valid once")

    @staticmethod
    def _require_identity(connection: _Connection, identity: Any) -> None:
        if _identity_from_proto(identity) != connection.session.identity:
            raise ValueError("media event identity does not match the session")

    async def _error(self, connection: _Connection, code: str, message: str) -> None:
        fence = connection.session.fence
        await self._enqueue(
            connection,
            media_pb2.CoreToMedia(
                error=media_pb2.CoreError(
                    identity=_identity_to_proto(connection.session.identity),
                    code=code,
                    message=message[:256],
                    retryable=True,
                    turn_id=fence.turn_id,
                    generation_id=fence.generation_id,
                    tool_epoch=fence.tool_epoch,
                    task_epoch=connection.session.task_epoch,
                    context_version=connection.session.context_version,
                )
            ),
        )

    async def _enqueue(self, connection: _Connection, message: media_pb2.CoreToMedia) -> bool:
        if connection.closed:
            return False
        try:
            connection.outgoing.put_nowait(message)
        except asyncio.QueueFull:
            if self._evict_shadow_observation(connection):
                connection.outgoing.put_nowait(message)
                return True
            connection.session.overflow_count += 1
            terminal = self._overflow_cancel_message(connection)
            # Stop publishers and wake the writer before awaiting Voice Core:
            # provider cancellation can wait on remote I/O, but the terminal
            # must remain deliverable while it does.
            self._terminate_outgoing(connection, terminal)
            if terminal is not None and self.on_downlink_overflow is not None:
                try:
                    await self.on_downlink_overflow(connection.session)
                except Exception:
                    logger.exception("Voice Core did not accept downlink overflow cancellation")
            return False
        return True

    @staticmethod
    def _evict_shadow_observation(connection: _Connection) -> bool:
        removed = connection.outgoing.evict_coalescing()
        if removed is None:
            return False
        if removed.WhichOneof("event") == "shadow_observation":
            connection.dropped_shadow_observations += 1
        return True

    @staticmethod
    def _try_enqueue_shadow(
        connection: _Connection,
        message: media_pb2.CoreToMedia,
    ) -> bool:
        if connection.closed:
            return False
        try:
            connection.outgoing.put_nowait(message)
        except asyncio.QueueFull:
            connection.dropped_shadow_observations += 1
            return False
        return True

    @staticmethod
    def _terminate_outgoing(
        connection: _Connection,
        terminal: media_pb2.CoreToMedia | None = None,
    ) -> None:
        """Wake the writer immediately when bounded delivery is exhausted."""

        connection.closed = True
        connection.outgoing.clear()
        if terminal is not None:
            connection.outgoing.put_nowait(terminal)
        else:
            connection.outgoing.put_nowait(None)

    def _overflow_cancel_message(
        self,
        connection: _Connection,
    ) -> media_pb2.CoreToMedia | None:
        """Derive the authoritative cancelled fence for a delivery overflow.

        Bounded delivery exhaustion is a transport failure for the current
        generation: the provider output for that fence can no longer be
        delivered, so the generation is cancelled and a reconnect receives
        GENERATION_ACTION_CANCEL instead of a stale RESUME.
        """

        fence = connection.session.generation.current
        if (
            connection.session.state == "closed"
            or not connection.session.generation_active
            or not (fence.turn_id or fence.generation_id or fence.tool_epoch)
        ):
            return None
        next_fence = connection.session.generation.cancel(connection.session.generation.current)
        if next_fence is None:
            return None
        connection.session.generation_active = False
        connection.session.reset_downlink_generation(next_fence)
        return media_pb2.CoreToMedia(
            generation=media_pb2.GenerationControl(
                identity=_identity_to_proto(connection.session.identity),
                turn_id=next_fence.turn_id,
                generation_id=next_fence.generation_id,
                tool_epoch=next_fence.tool_epoch,
                session_epoch=next_fence.session_epoch,
                action=media_pb2.GENERATION_ACTION_CANCEL,
                reason="downlink_queue_full",
                task_epoch=connection.session.task_epoch,
                context_version=connection.session.context_version,
            )
        )

    async def emit_pcm(self, session_id: str, frame: PCMFrame) -> bool:
        connection = self._connections.get(session_id)
        if connection is None or not connection.session.accept_downlink(frame):
            return False
        task_epoch, context_version = connection.session.observe_versions(
            frame.task_epoch,
            frame.context_version,
        )
        enqueued = await self._enqueue(
            connection,
            media_pb2.CoreToMedia(
                audio=media_pb2.AssistantAudioFrame(
                    identity=_identity_to_proto(frame.identity),
                    turn_id=frame.turn_id,
                    generation_id=frame.generation_id,
                    tool_epoch=frame.tool_epoch,
                    session_epoch=frame.session_epoch,
                    sequence=frame.sequence,
                    source_start_sample=frame.source_start_sample,
                    frame_samples=frame.frame_samples,
                    pcm_s16le=frame.pcm_s16le,
                    first_frame=frame.first,
                    final_frame=frame.final,
                    task_epoch=task_epoch,
                    context_version=context_version,
                )
            ),
        )
        if enqueued:
            # The gRPC outgoing queue now owns the serialized frame.  Release
            # the bridge's pending PCM slot; client-side playback progress is
            # validated independently by PlaybackLedger.
            connection.session.pop_downlink(frame.sequence)
            connection.session.ack_downlink(frame.sequence)
        return enqueued

    async def emit_pcm_when_connected(
        self,
        session_id: str,
        frame: PCMFrame,
        *,
        timeout_s: float,
    ) -> bool:
        """Backpressure one provider frame across a bounded reconnect gap.

        No audio is queued while no gRPC transport owns the Session. The
        caller keeps exactly its current frame and retries only after a newer
        stream epoch is connected, so old WSS queues are never replayed.
        """

        if timeout_s <= 0:
            return await self.emit_pcm(session_id, frame)
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            connection = self._connections.get(session_id)
            candidate = (
                replace(frame, identity=connection.session.identity)
                if connection is not None and not connection.closed
                else frame
            )
            if await self.emit_pcm(session_id, candidate):
                return True
            if connection is not None and not connection.closed:
                # A stable current transport rejected this frame for a real
                # gate/sequence/queue reason. Waiting cannot make it valid;
                # only retry when this attempt raced a replacement epoch.
                if self._connections.get(session_id) is connection and not connection.closed:
                    return False
            session = self.bridge.get(session_id)
            if session is None or session.state == "closed" or not session.generation_active:
                return False
            # A session inserted directly into the state gate (unit/local
            # adapter paths) was never attached to a gRPC transport. Preserve
            # the historical immediate rejection; reconnect waiting begins
            # only after a real connection existed and then detached.
            if connection is None and session_id not in self._transport_sessions_seen:
                return False
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.02, remaining))

    async def emit_generation(
        self,
        session_id: str,
        fence: GenerationFence,
        *,
        action: int,
        reason: str = "",
        task_epoch: int = 0,
        context_version: int = 0,
    ) -> bool:
        connection = self._connections.get(session_id)
        if connection is None:
            logger.warning(
                "media generation control rejected session=%s action=%s reason=%s "
                "cause=connection_missing fence=turn=%s/gen=%s/epoch=%s",
                session_id,
                action,
                reason,
                fence.turn_id,
                fence.generation_id,
                fence.tool_epoch,
            )
            return False
        task_epoch, context_version = connection.session.observe_versions(
            task_epoch,
            context_version,
        )
        try:
            connection.session.generation.advance(fence)
        except ValueError as exc:
            logger.warning(
                "media generation control rejected session=%s action=%s reason=%s "
                "cause=fence_advance_error error=%s fence=turn=%s/gen=%s/epoch=%s "
                "current_fence=%s",
                session_id,
                action,
                reason,
                exc,
                fence.turn_id,
                fence.generation_id,
                fence.tool_epoch,
                connection.session.generation.current,
            )
            return False
        if not connection.session.reset_downlink_generation(fence):
            logger.warning(
                "media generation control rejected session=%s action=%s reason=%s "
                "cause=downlink_reset_rejected fence=turn=%s/gen=%s/epoch=%s",
                session_id,
                action,
                reason,
                fence.turn_id,
                fence.generation_id,
                fence.tool_epoch,
            )
            return False
        connection.session.generation_active = action != media_pb2.GENERATION_ACTION_CANCEL
        enqueued = await self._enqueue(
            connection,
            media_pb2.CoreToMedia(
                generation=media_pb2.GenerationControl(
                    identity=_identity_to_proto(connection.session.identity),
                    turn_id=fence.turn_id,
                    generation_id=fence.generation_id,
                    tool_epoch=fence.tool_epoch,
                    session_epoch=fence.session_epoch,
                    action=action,
                    reason=reason[:256],
                    task_epoch=task_epoch,
                    context_version=context_version,
                )
            ),
        )
        if not enqueued:
            logger.warning(
                "media generation control rejected session=%s action=%s reason=%s "
                "cause=enqueue_rejected connection_closed=%s fence=turn=%s/gen=%s/epoch=%s",
                session_id,
                action,
                reason,
                connection.closed,
                fence.turn_id,
                fence.generation_id,
                fence.tool_epoch,
            )
        return enqueued

    async def emit_realtime_effect(
        self,
        session_id: str,
        effect_kind: int,
        fence: GenerationFence,
        *,
        source_event_id: str,
        payload: dict[str, Any],
        task_epoch: int = 0,
        context_version: int = 0,
    ) -> bool:
        """Publish one Python-authoritative, fully fenced media effect."""

        connection = self._connections.get(session_id)
        if (
            connection is None
            or connection.closed
            or not source_event_id.strip()
            or not can_execute_realtime_effect(
                connection.session.interaction_authority,
                producer=InteractionRuntime.PYTHON,
                candidate_only=False,
            )
        ):
            return False
        if effect_kind not in {
            media_pb2.REALTIME_EFFECT_KIND_DUCK_OUTPUT,
            media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
            media_pb2.REALTIME_EFFECT_KIND_PAUSE_OUTPUT,
            media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT,
        }:
            return False
        try:
            encoded_payload = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            return False
        if len(encoded_payload) > 4 * 1024:
            return False

        if effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION:
            try:
                connection.session.generation.advance(fence)
            except ValueError:
                return False
            if not connection.session.reset_downlink_generation(fence):
                return False
            connection.session.generation_active = False
        elif not connection.session.generation_active or not connection.session.generation.accept(
            fence
        ):
            return False

        task_epoch, context_version = connection.session.observe_versions(
            task_epoch,
            context_version,
        )
        return await self._enqueue(
            connection,
            media_pb2.CoreToMedia(
                realtime_effect=media_pb2.RealtimeEffect(
                    session_id=connection.session.identity.session_id,
                    stream_epoch=connection.session.identity.stream_epoch,
                    effect_kind=effect_kind,
                    source_event_id=source_event_id[:128],
                    turn_id=fence.turn_id,
                    generation_id=fence.generation_id,
                    tool_epoch=fence.tool_epoch,
                    session_epoch=fence.session_epoch,
                    task_epoch=task_epoch,
                    context_version=context_version,
                    candidate_only=False,
                    payload=encoded_payload,
                    identity=_identity_to_proto(connection.session.identity),
                )
            ),
        )

    async def emit_floor_effect(
        self,
        session_id: str,
        floor_state: int,
        *,
        floor_epoch: int,
        fence: GenerationFence,
        source_event_id: str,
        task_epoch: int = 0,
        context_version: int = 0,
    ) -> bool:
        """Publish one typed, Python-authoritative Floor state update."""

        connection = self._connections.get(session_id)
        if (
            connection is None
            or connection.closed
            or floor_epoch <= 0
            or not source_event_id.strip()
            or floor_state
            not in {
                media_pb2.FLOOR_STATE_USER_HOLDS_FLOOR,
                media_pb2.FLOOR_STATE_ASSISTANT_HOLDS_FLOOR,
                media_pb2.FLOOR_STATE_OVERLAP,
                media_pb2.FLOOR_STATE_UNCERTAIN,
                media_pb2.FLOOR_STATE_SILENCE,
            }
            or not can_execute_realtime_effect(
                connection.session.interaction_authority,
                producer=InteractionRuntime.PYTHON,
                candidate_only=False,
            )
            # A floor update may describe the cancelled current fence, but it
            # must never revive a prior generation.
            or not connection.session.generation.accept(fence)
        ):
            return False
        task_epoch, context_version = connection.session.observe_versions(
            task_epoch,
            context_version,
        )
        return await self._enqueue(
            connection,
            media_pb2.CoreToMedia(
                floor_effect=media_pb2.FloorEffect(
                    identity=_identity_to_proto(connection.session.identity),
                    floor_state=floor_state,
                    floor_epoch=floor_epoch,
                    source_event_id=source_event_id[:128],
                    turn_id=fence.turn_id,
                    generation_id=fence.generation_id,
                    tool_epoch=fence.tool_epoch,
                    session_epoch=fence.session_epoch,
                    task_epoch=task_epoch,
                    context_version=context_version,
                    candidate_only=False,
                    expires_at_ms=max(0, int(time.time() * 1_000)) + FLOOR_EFFECT_TTL_MS,
                )
            ),
        )

    async def emit_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        turn_id: int = 0,
        generation_id: int = 0,
        tool_epoch: int = 0,
        task_epoch: int = 0,
        context_version: int = 0,
    ) -> bool:
        connection = self._connections.get(session_id)
        if connection is None:
            return False
        task_epoch, context_version = connection.session.observe_versions(
            task_epoch,
            context_version,
        )
        current_fence = connection.session.fence
        session_epoch = (
            current_fence.session_epoch
            if (turn_id, generation_id, tool_epoch)
            == (current_fence.turn_id, current_fence.generation_id, current_fence.tool_epoch)
            else 0
        )
        enqueued = await self._enqueue(
            connection,
            media_pb2.CoreToMedia(
                client=media_pb2.ClientEvent(
                    identity=_identity_to_proto(connection.session.identity),
                    type=event_type,
                    json_payload=json.dumps(
                        {
                            "v": 1,
                            "protocol": "media-v1",
                            "type": event_type,
                            "session_id": connection.session.identity.session_id,
                            "stream_epoch": connection.session.identity.stream_epoch,
                            "payload": payload,
                            "turn_id": turn_id,
                            "generation_id": generation_id,
                            "tool_epoch": tool_epoch,
                            "session_epoch": session_epoch,
                            "task_epoch": task_epoch,
                            "context_version": context_version,
                            "server_monotonic_ms": max(0, time.monotonic_ns() // 1_000_000),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    turn_id=turn_id,
                    generation_id=generation_id,
                    tool_epoch=tool_epoch,
                    task_epoch=task_epoch,
                    context_version=context_version,
                )
            ),
        )
        if enqueued and event_type == "assistant_state":
            self._emit_floor_decision_shadow(
                connection,
                payload=payload,
                turn_id=turn_id,
                generation_id=generation_id,
                tool_epoch=tool_epoch,
            )
        return enqueued

    async def emit_conversation_state(
        self,
        session_id: str,
        state: int,
        *,
        fence: GenerationFence,
        reason: str,
        task_epoch: int = 0,
        context_version: int = 0,
    ) -> bool:
        """Project one typed authoritative conversation state to Media Edge."""

        connection = self._connections.get(session_id)
        if (
            connection is None
            or not reason
            or len(reason) > 128
            or not connection.session.generation.accept(fence)
        ):
            return False
        task_epoch, context_version = connection.session.observe_versions(
            task_epoch,
            context_version,
        )
        return await self._enqueue(
            connection,
            media_pb2.CoreToMedia(
                state=media_pb2.StateEvent(
                    identity=_identity_to_proto(connection.session.identity),
                    state=state,
                    turn_id=fence.turn_id,
                    generation_id=fence.generation_id,
                    reason=reason,
                    tool_epoch=fence.tool_epoch,
                    task_epoch=task_epoch,
                    context_version=context_version,
                )
            ),
        )

    def _emit_floor_decision_shadow(
        self,
        connection: _Connection,
        *,
        payload: dict[str, Any],
        turn_id: int,
        generation_id: int,
        tool_epoch: int,
    ) -> bool:
        if (
            connection.closed
            or connection.session.interaction_authority is not InteractionAuthority.GO_SHADOW
        ):
            return False
        phase = str(payload.get("phase") or payload.get("state") or "")
        mapping = {
            "connecting": (
                media_pb2.FLOOR_STATE_SILENCE,
                media_pb2.REALTIME_EFFECT_KIND_PAUSE_OUTPUT,
            ),
            "ready": (
                media_pb2.FLOOR_STATE_SILENCE,
                media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT,
            ),
            "speaker_enroll": (
                media_pb2.FLOOR_STATE_SILENCE,
                media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT,
            ),
            "listening": (
                media_pb2.FLOOR_STATE_SILENCE,
                media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT,
            ),
            "user_speaking": (
                media_pb2.FLOOR_STATE_USER_HOLDS_FLOOR,
                media_pb2.REALTIME_EFFECT_KIND_DUCK_OUTPUT,
            ),
            "backchannel": (
                media_pb2.FLOOR_STATE_OVERLAP,
                media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT,
            ),
            "thinking": (
                media_pb2.FLOOR_STATE_SILENCE,
                media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT,
            ),
            "thinking_silent": (
                media_pb2.FLOOR_STATE_SILENCE,
                media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT,
            ),
            "tool_waiting": (
                media_pb2.FLOOR_STATE_SILENCE,
                media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT,
            ),
            "speaking": (
                media_pb2.FLOOR_STATE_ASSISTANT_HOLDS_FLOOR,
                media_pb2.REALTIME_EFFECT_KIND_ENQUEUE_OUTPUT_INTENT,
            ),
            "interrupted": (
                media_pb2.FLOOR_STATE_USER_HOLDS_FLOOR,
                media_pb2.REALTIME_EFFECT_KIND_DROP_STALE_EVENT,
            ),
            "recovering": (
                media_pb2.FLOOR_STATE_SILENCE,
                media_pb2.REALTIME_EFFECT_KIND_DROP_STALE_EVENT,
            ),
            "closed": (
                media_pb2.FLOOR_STATE_SILENCE,
                media_pb2.REALTIME_EFFECT_KIND_DROP_STALE_EVENT,
            ),
        }.get(phase)
        if mapping is None:
            return False
        floor_state, effect_kind = mapping
        observation = self._new_shadow_observation(
            connection,
            kind=media_pb2.SHADOW_OBSERVATION_KIND_FLOOR_DECISION,
            authoritative_accepted=True,
            authoritative_reason=phase,
            observed_at_ms=max(0, int(time.time() * 1_000)),
        )
        observation.floor_decision.CopyFrom(
            media_pb2.ShadowFloorDecision(
                floor_state=floor_state,
                effect_kind=effect_kind,
                turn_id=max(0, int(turn_id)),
                generation_id=max(0, int(generation_id)),
                tool_epoch=max(0, int(tool_epoch)),
            )
        )
        return self._try_enqueue_shadow(
            connection,
            media_pb2.CoreToMedia(shadow_observation=observation),
        )

    async def emit_transcript(
        self,
        session_id: str,
        segment: SpeechSegment,
        *,
        turn_id: int | None = None,
        speaker_class: str = "",
        task_epoch: int = 0,
        context_version: int = 0,
    ) -> bool:
        """Publish one range-stamped transcript under the current fence."""

        connection = self._connections.get(session_id)
        if connection is None:
            return False
        fence = connection.session.fence
        if (
            segment.session_id != session_id
            or segment.stream_epoch != connection.session.identity.stream_epoch
        ):
            return False
        task_epoch, context_version = connection.session.observe_versions(
            task_epoch,
            context_version,
        )
        return await self._enqueue(
            connection,
            media_pb2.CoreToMedia(
                transcript=media_pb2.TranscriptEvent(
                    identity=_identity_to_proto(connection.session.identity),
                    turn_id=fence.turn_id if turn_id is None else turn_id,
                    revision=segment.revision,
                    capture_start_sample=segment.capture_start_sample,
                    capture_end_sample=segment.capture_end_sample,
                    text=segment.text,
                    final=segment.final,
                    confidence=segment.confidence or 0.0,
                    speaker_class=speaker_class or segment.speaker_class or "",
                    generation_id=fence.generation_id,
                    tool_epoch=fence.tool_epoch,
                    task_epoch=task_epoch,
                    context_version=context_version,
                    loss_concealed=segment.loss_concealed,
                )
            ),
        )

    async def emit_speech_task_started(
        self,
        session_id: str,
        task_epoch: int,
        timeline: SpeechTimeline,
    ) -> bool:
        return self._emit_speech_shadow_observation(
            session_id,
            kind=media_pb2.SHADOW_OBSERVATION_KIND_SPEECH_TASK_STARTED,
            input_value=media_pb2.ShadowSpeechTaskStarted(task_epoch=task_epoch),
            authoritative_accepted=True,
            authoritative_reason="task_started",
            timeline=timeline,
            latest_task_epoch=task_epoch,
        )

    async def emit_speech_segment_decision(
        self,
        session_id: str,
        segment: SpeechSegment,
        *,
        authoritative_accepted: bool,
        authoritative_reason: str,
        timeline: SpeechTimeline,
        latest_task_epoch: int,
    ) -> bool:
        return self._emit_speech_shadow_observation(
            session_id,
            kind=media_pb2.SHADOW_OBSERVATION_KIND_SPEECH_SEGMENT,
            input_value=self._shadow_speech_segment(segment),
            authoritative_accepted=authoritative_accepted,
            authoritative_reason=authoritative_reason,
            timeline=timeline,
            latest_task_epoch=latest_task_epoch,
        )

    async def emit_speech_commit(
        self,
        session_id: str,
        committed_sample: int,
        timeline: SpeechTimeline,
        *,
        latest_task_epoch: int,
    ) -> bool:
        return self._emit_speech_shadow_observation(
            session_id,
            kind=media_pb2.SHADOW_OBSERVATION_KIND_SPEECH_COMMIT,
            input_value=media_pb2.ShadowSpeechCommit(committed_sample=committed_sample),
            authoritative_accepted=True,
            authoritative_reason="committed",
            timeline=timeline,
            latest_task_epoch=latest_task_epoch,
        )

    async def emit_context_activated(
        self,
        session_id: str,
        context_version: int,
    ) -> bool:
        connection = self._shadow_connection(session_id)
        if connection is None or context_version < 0:
            return False
        observation = self._new_shadow_observation(
            connection,
            kind=media_pb2.SHADOW_OBSERVATION_KIND_CONTEXT_ACTIVATED,
            authoritative_accepted=True,
            authoritative_reason="context_activated",
            observed_at_ms=max(0, int(time.time() * 1_000)),
            authoritative_context_version=context_version,
        )
        observation.context_activated.context_version = context_version
        return self._try_enqueue_shadow(
            connection,
            media_pb2.CoreToMedia(shadow_observation=observation),
        )

    def emit_output_intent_decision(self, session_id: str, admission: Any) -> bool:
        connection = self._shadow_connection(session_id)
        intent = getattr(admission, "intent", None)
        if connection is None or intent is None or str(intent.session_id) != session_id:
            return False
        consumed = bool(getattr(admission, "consumed", False))
        if consumed and bool(admission.accepted):
            return False
        observation = self._new_shadow_observation(
            connection,
            kind=media_pb2.SHADOW_OBSERVATION_KIND_OUTPUT_INTENT,
            authoritative_accepted=bool(admission.accepted),
            authoritative_reason=str(admission.reason),
            observed_at_ms=int(admission.observed_at_ms),
            authoritative_context_version=int(admission.current_context_version),
        )
        observation.authoritative_consumed = consumed

        def shadow_intent(candidate: Any) -> Any:
            return media_pb2.ShadowOutputIntent(
                intent_id=str(candidate.intent_id),
                turn_id=int(candidate.turn_id),
                generation_id=int(candidate.generation_id),
                tool_epoch=int(candidate.tool_epoch),
                kind=int(candidate.kind),
                priority=int(candidate.priority),
                created_at_ms=int(candidate.created_at_ms),
                expires_at_ms=int(candidate.expires_at_ms),
                floor_requirement=int(candidate.floor_requirement),
                context_version=int(candidate.context_version),
            )

        observation.output_intent.CopyFrom(shadow_intent(intent))
        candidate = getattr(admission, "authoritative_candidate", None)
        arbiter = media_pb2.ShadowOutputArbiterState(
            context_version=int(admission.current_context_version),
            active_candidates_complete=True,
        )
        if candidate is not None:
            arbiter.candidate.CopyFrom(shadow_intent(candidate))
        candidates = tuple(getattr(admission, "authoritative_candidates", ()))
        if not candidates and candidate is not None:
            candidates = (candidate,)
        arbiter.active_candidates.extend(shadow_intent(item) for item in candidates)
        observation.authoritative_output_arbiter.CopyFrom(arbiter)
        return self._try_enqueue_shadow(
            connection,
            media_pb2.CoreToMedia(shadow_observation=observation),
        )

    def _shadow_connection(self, session_id: str) -> _Connection | None:
        connection = self._connections.get(session_id)
        if (
            connection is None
            or connection.session.interaction_authority is not InteractionAuthority.GO_SHADOW
        ):
            return None
        return connection

    def _new_shadow_observation(
        self,
        connection: _Connection,
        *,
        kind: int,
        authoritative_accepted: bool,
        authoritative_reason: str,
        observed_at_ms: int = 0,
        authoritative_context_version: int = 0,
    ) -> Any:
        return media_pb2.ShadowObservation(
            identity=_identity_to_proto(connection.session.identity),
            contract_version="media-v1-a6a",
            candidate_only=True,
            kind=kind,
            authoritative_accepted=authoritative_accepted,
            authoritative_reason=authoritative_reason[:64],
            observed_at_ms=observed_at_ms,
            authoritative_context_version=authoritative_context_version,
        )

    def _emit_speech_shadow_observation(
        self,
        session_id: str,
        *,
        kind: int,
        input_value: Any,
        authoritative_accepted: bool,
        authoritative_reason: str,
        timeline: SpeechTimeline,
        latest_task_epoch: int,
    ) -> bool:
        connection = self._shadow_connection(session_id)
        if connection is None or timeline.stream_epoch != connection.session.identity.stream_epoch:
            return False
        input_field = {
            media_pb2.SHADOW_OBSERVATION_KIND_SPEECH_TASK_STARTED: "speech_task_started",
            media_pb2.SHADOW_OBSERVATION_KIND_SPEECH_SEGMENT: "speech_segment",
            media_pb2.SHADOW_OBSERVATION_KIND_SPEECH_COMMIT: "speech_commit",
        }.get(kind)
        if input_field is None:
            return False
        observation = self._new_shadow_observation(
            connection,
            kind=kind,
            authoritative_accepted=authoritative_accepted,
            authoritative_reason=authoritative_reason[:64],
        )
        observation.authoritative_timeline.CopyFrom(
            self._shadow_timeline_state(
                timeline,
                latest_task_epoch=latest_task_epoch,
            )
        )
        getattr(observation, input_field).CopyFrom(input_value)
        return self._try_enqueue_shadow(
            connection,
            media_pb2.CoreToMedia(shadow_observation=observation),
        )

    @staticmethod
    def _shadow_speech_segment(segment: SpeechSegment) -> Any:
        return media_pb2.ShadowSpeechSegment(
            segment_id=segment.segment_id,
            capture_start_sample=segment.capture_start_sample,
            capture_end_sample=segment.capture_end_sample,
            task_epoch=segment.provider_task_epoch,
            revision=segment.revision,
            text_sha256=hashlib.sha256(segment.text.encode("utf-8")).digest(),
            final=segment.final,
        )

    def _shadow_timeline_state(
        self,
        timeline: SpeechTimeline,
        *,
        latest_task_epoch: int,
    ) -> Any:
        segments = (
            segment
            for segment in timeline.pending
            if segment.kind in {SegmentKind.ASR_PARTIAL, SegmentKind.ASR_FINAL}
        )
        return media_pb2.ShadowSpeechTimelineState(
            committed_sample=timeline.committed_sample,
            latest_task_epoch=max(
                latest_task_epoch,
                timeline.latest_task_epoch(timeline.stream_epoch or 0),
            ),
            segments=(self._shadow_speech_segment(segment) for segment in segments),
        )


__all__ = [
    "AudioFrameHandler",
    "ClientEventHandler",
    "MediaBridgeGrpcServer",
    "MediaBridgeTLS",
    "PlaybackProgressHandler",
    "SessionClosedHandler",
    "SpeechSegmentHandler",
]
