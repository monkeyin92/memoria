"""Bounded Media Edge ↔ Voice Core state gate shared by tests and gRPC.

The gRPC transport in :mod:`grpc_bridge` uses this class for the authoritative
identity, stream-epoch, sample-range and generation checks.  Keeping the state
gate independent of the wire server also lets the existing LiveKit path and
replay tests exercise the same invariants without opening a socket.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.generation_controller import GenerationController
from services.agent.src.voice_core.media_protocol import AudioFrame, MediaEnvelope, SessionIdentity
from services.agent.src.voice_core.speech_timeline import SpeechTimeline

BridgeState = Literal["connected", "reconnecting", "closed"]


@dataclass(frozen=True, slots=True)
class PCMFrame:
    identity: SessionIdentity
    turn_id: int
    generation_id: int
    tool_epoch: int
    sequence: int
    source_start_sample: int
    frame_samples: int
    pcm_s16le: bytes
    first: bool = False
    final: bool = False

    def __post_init__(self) -> None:
        if min(
            self.turn_id,
            self.generation_id,
            self.tool_epoch,
            self.sequence,
            self.source_start_sample,
        ) < 0:
            raise ValueError("PCM frame metadata must be non-negative")
        if (
            self.frame_samples <= 0
            or not self.pcm_s16le
            or len(self.pcm_s16le) % 2
            or len(self.pcm_s16le) // 2 != self.frame_samples
        ):
            raise ValueError("PCM frame must contain 16-bit audio samples")


@dataclass(slots=True)
class MediaBridgeSession:
    identity: SessionIdentity
    max_pending_audio_frames: int = 100
    state: BridgeState = "connected"
    generation: GenerationController = field(init=False)
    timeline: SpeechTimeline = field(default_factory=SpeechTimeline)
    uplink: deque[AudioFrame] = field(default_factory=deque)
    downlink: deque[PCMFrame] = field(default_factory=deque)
    last_uplink_sequence: int = -1
    last_downlink_sequence: int = -1
    _last_capture_end_sample: int = 0
    stale_downlink_count: int = 0
    overflow_count: int = 0
    _stop_keys: deque[str] = field(default_factory=lambda: deque(maxlen=64))

    def __post_init__(self) -> None:
        if self.max_pending_audio_frames <= 0:
            raise ValueError("max_pending_audio_frames must be positive")
        self.generation = GenerationController(session_id=self.identity.session_id)
        self.timeline.start_stream_epoch(self.identity.stream_epoch)

    @property
    def fence(self) -> GenerationFence:
        return self.generation.current

    def accept_uplink(self, frame: AudioFrame) -> bool:
        if self.state == "closed" or frame.identity != self.identity:
            return False
        if frame.sequence <= self.last_uplink_sequence:
            return False
        if self.last_uplink_sequence >= 0 and frame.sequence != self.last_uplink_sequence + 1:
            return False
        if frame.discontinuity:
            # A discontinuity is a transport fence.  The caller must create a
            # new SessionIdentity/stream_epoch before accepting more samples.
            return False
        if frame.capture_start_sample < self.last_capture_end_sample:
            return False
        if self.last_uplink_sequence >= 0 and frame.capture_start_sample > self.last_capture_end_sample:
            return False
        if len(self.uplink) >= self.max_pending_audio_frames:
            self.overflow_count += 1
            return False
        self.last_uplink_sequence = frame.sequence
        self._last_capture_end_sample = frame.capture_end_sample
        self.uplink.append(frame)
        return True

    def accept_client_stop(self, event: MediaEnvelope) -> bool:
        """Apply one idempotent client stop to the authoritative generation."""

        if self.state == "closed" or event.session_id != self.identity.session_id:
            return False
        if event.stream_epoch != self.identity.stream_epoch or event.type != "client.stop_assistant":
            return False
        key = event.payload.get("idempotency_key")
        if not isinstance(key, str) or not key.strip():
            # The envelope event_id is generated once by the client and is
            # already stable across transport retries.  Accepting it as the
            # fallback keeps the low-latency DataChannel path idempotent while
            # still requiring a non-empty, per-request key.
            key = event.event_id
        if not isinstance(key, str) or not key.strip():
            return False
        if key in self._stop_keys:
            return True
        if event.generation_id or event.turn_id or event.tool_epoch:
            expected = GenerationFence(
                session_id=event.session_id,
                turn_id=event.turn_id,
                generation_id=event.generation_id,
                tool_epoch=event.tool_epoch,
            )
            if not self.generation.accept(expected):
                return False
        self.generation.cancel(self.generation.current)
        self._stop_keys.append(key)
        return True

    @property
    def last_capture_end_sample(self) -> int:
        return self._last_capture_end_sample

    def accept_downlink(self, frame: PCMFrame) -> bool:
        if self.state == "closed" or frame.identity != self.identity:
            self.stale_downlink_count += 1
            return False
        if frame.sequence <= self.last_downlink_sequence:
            self.stale_downlink_count += 1
            return False
        expected = self.fence
        actual = GenerationFence(
            session_id=frame.identity.session_id,
            turn_id=frame.turn_id,
            generation_id=frame.generation_id,
            tool_epoch=frame.tool_epoch,
        )
        # A downlink frame may only consume a fence already published by the
        # authoritative generation-control event.  Do not call ``accept`` on
        # a future frame before this comparison: ``accept`` is intentionally a
        # pure exact-match check, but keeping the order explicit prevents a
        # future implementation from accidentally advancing the gate through
        # an unannounced audio frame.
        if actual != expected or not self.generation.accept(actual):
            self.stale_downlink_count += 1
            return False
        if len(self.downlink) >= self.max_pending_audio_frames:
            # Prefer stale-generation rejection to unbounded buffering.  A
            # current frame is rejected explicitly so the caller can apply
            # backpressure or rebuild the media epoch.
            self.overflow_count += 1
            return False
        self.last_downlink_sequence = frame.sequence
        self.downlink.append(frame)
        return True

    def reconnect(self, identity: SessionIdentity) -> bool:
        if self.state == "closed" or identity.session_id != self.identity.session_id:
            return False
        if (
            identity.account_id != self.identity.account_id
            or identity.participant_id != self.identity.participant_id
            or identity.device_id != self.identity.device_id
            or identity.client_type != self.identity.client_type
        ):
            return False
        if identity.stream_epoch <= self.identity.stream_epoch:
            return False
        self.identity = identity
        self.state = "connected"
        self.last_uplink_sequence = -1
        self.last_downlink_sequence = -1
        self._last_capture_end_sample = 0
        self.uplink.clear()
        self.downlink.clear()
        self.timeline.start_stream_epoch(identity.stream_epoch)
        return True

    def close(self) -> None:
        self.state = "closed"
        self.uplink.clear()
        self.downlink.clear()


@dataclass(slots=True)
class MediaBridgeServer:
    max_pending_audio_frames: int = 100
    sessions: dict[str, MediaBridgeSession] = field(default_factory=dict)

    def open(self, identity: SessionIdentity) -> MediaBridgeSession:
        if identity.session_id in self.sessions:
            raise ValueError("media session already exists")
        session = MediaBridgeSession(
            identity=identity,
            max_pending_audio_frames=self.max_pending_audio_frames,
        )
        self.sessions[identity.session_id] = session
        return session

    def get(self, session_id: str) -> MediaBridgeSession | None:
        return self.sessions.get(session_id)

    def accept_client_event(self, event: MediaEnvelope) -> bool:
        session = self.sessions.get(event.session_id)
        return session is not None and session.accept_client_stop(event)

    def close(self, session_id: str) -> bool:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        session.close()
        return True


__all__ = ["MediaBridgeServer", "MediaBridgeSession", "PCMFrame"]
