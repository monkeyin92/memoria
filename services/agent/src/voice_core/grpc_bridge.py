"""Bidirectional protobuf bridge between Media Edge and Voice Core.

The bridge is intentionally small and provider-neutral.  It owns transport
authentication, session/epoch fencing and bounded delivery; orchestration and
provider work remain in the existing Voice Core.  No StreamCore/Pion source is
embedded here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import grpc

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.media_bridge_server import (
    MediaBridgeServer,
    MediaBridgeSession,
    PCMFrame,
)
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    MediaEnvelope,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.speech_timeline import SegmentKind, SpeechSegment

media_pb2: Any = _media_pb2
KWS_HARD_STOP_MIN_CONFIDENCE = 0.8
logger = logging.getLogger(__name__)


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
PlaybackProgressHandler = Callable[[MediaBridgeSession, PlaybackProgress], Awaitable[None]]
DownlinkOverflowHandler = Callable[[MediaBridgeSession], Awaitable[None]]


@dataclass(slots=True)
class _Connection:
    session: MediaBridgeSession
    outgoing: asyncio.Queue[media_pb2.CoreToMedia | None]
    next_event_sequence: int = 0
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
    )


def _identity_to_proto(identity: SessionIdentity) -> Any:
    return media_pb2.SessionIdentity(
        session_id=identity.session_id,
        account_id=identity.account_id,
        participant_id=identity.participant_id,
        device_id=identity.device_id,
        client_type=identity.client_type,
        stream_epoch=identity.stream_epoch,
    )


class MediaBridgeGrpcServer:
    """A real asyncio gRPC streaming endpoint over the media-v1 contract."""

    _SERVICE = "memoria.media.v1.VoiceMediaBridge"

    def __init__(
        self,
        *,
        max_pending_audio_frames: int = 100,
        max_pending_messages: int = 128,
        on_client_event: ClientEventHandler | None = None,
        on_audio_frame: AudioFrameHandler | None = None,
        on_speech_segment: SpeechSegmentHandler | None = None,
        on_session_closed: SessionClosedHandler | None = None,
        on_playback_progress: PlaybackProgressHandler | None = None,
        on_downlink_overflow: DownlinkOverflowHandler | None = None,
    ) -> None:
        if max_pending_messages <= 0:
            raise ValueError("max_pending_messages must be positive")
        self.bridge = MediaBridgeServer(max_pending_audio_frames=max_pending_audio_frames)
        self.max_pending_messages = max_pending_messages
        self.on_client_event = on_client_event
        self.on_audio_frame = on_audio_frame
        self.on_speech_segment = on_speech_segment
        self.on_session_closed = on_session_closed
        self.on_playback_progress = on_playback_progress
        self.on_downlink_overflow = on_downlink_overflow
        self._connections: dict[str, _Connection] = {}
        self._closed_session_notifications: set[str] = set()
        self._server: grpc.aio.Server | None = None

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
        if tls is None:
            port = server.add_insecure_port(address)
        else:
            port = server.add_secure_port(address, tls.credentials())
        if port <= 0:
            raise RuntimeError(f"failed to bind media bridge address: {address}")
        await server.start()
        self._server = server
        return cast(int, port)

    async def stop(self, grace_s: float = 1.0) -> None:
        server, self._server = self._server, None
        if server is not None:
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
        outgoing: asyncio.Queue[media_pb2.CoreToMedia | None] = asyncio.Queue(
            maxsize=self.max_pending_messages
        )
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
            yield media_pb2.CoreToMedia(
                accepted=media_pb2.SessionAccepted(
                    identity=_identity_to_proto(connection.session.identity),
                    state=media_pb2.CONVERSATION_STATE_LISTENING,
                    current_generation_id=connection.session.fence.generation_id,
                )
            )
            if connection.session.fence.turn_id or connection.session.fence.generation_id or connection.session.fence.tool_epoch:
                action = (
                    media_pb2.GENERATION_ACTION_RESUME
                    if connection.session.generation_active
                    else media_pb2.GENERATION_ACTION_CANCEL
                )
                yield media_pb2.CoreToMedia(
                    generation=media_pb2.GenerationControl(
                        identity=_identity_to_proto(connection.session.identity),
                        turn_id=connection.session.fence.turn_id,
                        generation_id=connection.session.fence.generation_id,
                        tool_epoch=connection.session.fence.tool_epoch,
                        action=action,
                        reason="stream_reconnected",
                        sequence=self._next_event_sequence(connection),
                    )
                )
            while True:
                message = await outgoing.get()
                if message is None:
                    break
                yield message
                if connection.closed:
                    # An overflow terminal (e.g. a generation cancel) was
                    # enqueued after the writer was closed; stop after it so
                    # the client observes the authoritative terminal event.
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
        outgoing: asyncio.Queue[media_pb2.CoreToMedia | None],
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
                    connection = self._open_connection(_identity_from_proto(request.hello.identity))
                    connection.outgoing = outgoing
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

    def _open_connection(self, identity: SessionIdentity) -> _Connection:
        existing = self._connections.get(identity.session_id)
        if existing is not None:
            if identity.stream_epoch <= existing.session.identity.stream_epoch:
                raise ValueError("media session already has an active bridge")
            # A newer stream epoch owns the session immediately.  Wake the
            # older writer; its late ``finally`` must not tear down the new
            # connection or provider context.
            self._terminate_outgoing(existing)
            self._connections.pop(identity.session_id, None)
        session = self.bridge.get(identity.session_id)
        if session is None:
            session = self.bridge.open(identity)
        elif not session.reconnect(identity):
            raise ValueError("media stream epoch did not advance")
        self._closed_session_notifications.discard(identity.session_id)
        connection = _Connection(
            session=session,
            outgoing=asyncio.Queue(maxsize=self.max_pending_messages),
        )
        self._connections[identity.session_id] = connection
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

    @staticmethod
    def _next_event_sequence(connection: _Connection) -> int:
        sequence = connection.next_event_sequence
        connection.next_event_sequence += 1
        return sequence

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
            if accepted and segment.hard_stop and (segment.confidence or 0.0) >= KWS_HARD_STOP_MIN_CONFIDENCE:
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
                int(event.detected_monotonic_ms)
                if event.HasField("detected_monotonic_ms")
                else 0
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
            if accepted and segment.hard_stop and (segment.confidence or 0.0) >= KWS_HARD_STOP_MIN_CONFIDENCE:
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
                    progress = PlaybackProgress(
                        identity=connection.session.identity,
                        generation_id=payload_int("generation_id"),
                        received_sequence=payload_int("received_sequence"),
                        rendered_sample_end=payload_int("rendered_sample_end"),
                        client_monotonic_ms=payload_int("client_monotonic_ms"),
                        approximate=approximate,
                        turn_id=payload_int("turn_id"),
                        tool_epoch=payload_int("tool_epoch"),
                    )
                except (TypeError, ValueError) as exc:
                    await self._error(connection, "invalid_playback_progress", str(exc))
                    return
                if not connection.session.accept_client_progress(envelope):
                    await self._error(connection, "stale_playback_progress", "playback progress rejected")
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
        await self._enqueue(
            connection,
            media_pb2.CoreToMedia(
                error=media_pb2.CoreError(
                    identity=_identity_to_proto(connection.session.identity),
                    code=code,
                    message=message[:256],
                    retryable=True,
                )
            ),
        )

    async def _enqueue(self, connection: _Connection, message: media_pb2.CoreToMedia) -> bool:
        if connection.closed:
            return False
        try:
            connection.outgoing.put_nowait(message)
        except asyncio.QueueFull:
            connection.session.overflow_count += 1
            terminal = self._overflow_cancel_message(connection)
            # Stop accepting publisher callbacks before notifying the registry:
            # on_real_interrupt may itself publish state, and a full transport
            # must not recurse through another overflow path.
            connection.closed = True
            if terminal is not None and self.on_downlink_overflow is not None:
                try:
                    await self.on_downlink_overflow(connection.session)
                except Exception:
                    logger.exception("Voice Core did not accept downlink overflow cancellation")
            self._terminate_outgoing(
                connection,
                terminal,
            )
            return False
        return True

    @staticmethod
    def _terminate_outgoing(
        connection: _Connection,
        terminal: media_pb2.CoreToMedia | None = None,
    ) -> None:
        """Wake the writer immediately when bounded delivery is exhausted."""

        connection.closed = True
        while True:
            try:
                connection.outgoing.get_nowait()
            except asyncio.QueueEmpty:
                break
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
                action=media_pb2.GENERATION_ACTION_CANCEL,
                reason="downlink_queue_full",
                sequence=self._next_event_sequence(connection),
            )
        )

    async def emit_pcm(self, session_id: str, frame: PCMFrame) -> bool:
        connection = self._connections.get(session_id)
        if connection is None or not connection.session.accept_downlink(frame):
            return False
        enqueued = await self._enqueue(
            connection,
            media_pb2.CoreToMedia(
                audio=media_pb2.AssistantAudioFrame(
                    identity=_identity_to_proto(frame.identity),
                    turn_id=frame.turn_id,
                    generation_id=frame.generation_id,
                    tool_epoch=frame.tool_epoch,
                    sequence=frame.sequence,
                    source_start_sample=frame.source_start_sample,
                    frame_samples=frame.frame_samples,
                    pcm_s16le=frame.pcm_s16le,
                    first_frame=frame.first,
                    final_frame=frame.final,
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

    async def emit_generation(
        self,
        session_id: str,
        fence: GenerationFence,
        *,
        action: int,
        reason: str = "",
    ) -> bool:
        connection = self._connections.get(session_id)
        if connection is None:
            return False
        try:
            connection.session.generation.advance(fence)
        except ValueError:
            return False
        if not connection.session.reset_downlink_generation(fence):
            return False
        connection.session.generation_active = action != media_pb2.GENERATION_ACTION_CANCEL
        return await self._enqueue(
            connection,
            media_pb2.CoreToMedia(
                generation=media_pb2.GenerationControl(
                    identity=_identity_to_proto(connection.session.identity),
                    turn_id=fence.turn_id,
                    generation_id=fence.generation_id,
                    tool_epoch=fence.tool_epoch,
                    action=action,
                    reason=reason[:256],
                    sequence=self._next_event_sequence(connection),
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
    ) -> bool:
        connection = self._connections.get(session_id)
        if connection is None:
            return False
        sequence = self._next_event_sequence(connection)
        event_id = (
            f"{connection.session.identity.session_id}:"
            f"{connection.session.identity.stream_epoch}:{sequence}"
        )
        return await self._enqueue(
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
                            "event_id": event_id,
                            "session_id": connection.session.identity.session_id,
                            "stream_epoch": connection.session.identity.stream_epoch,
                            "sequence": sequence,
                            "payload": payload,
                            "turn_id": turn_id,
                            "generation_id": generation_id,
                            "tool_epoch": tool_epoch,
                            "server_monotonic_ms": max(0, time.monotonic_ns() // 1_000_000),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    turn_id=turn_id,
                    generation_id=generation_id,
                    tool_epoch=tool_epoch,
                    sequence=sequence,
                )
            ),
        )

    async def emit_transcript(
        self,
        session_id: str,
        segment: SpeechSegment,
        *,
        turn_id: int | None = None,
        speaker_class: str = "",
    ) -> bool:
        """Publish one range-stamped transcript under the current fence."""

        connection = self._connections.get(session_id)
        if connection is None:
            return False
        fence = connection.session.fence
        if segment.session_id != session_id or segment.stream_epoch != connection.session.identity.stream_epoch:
            return False
        sequence = self._next_event_sequence(connection)
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
                    sequence=sequence,
                )
            ),
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
