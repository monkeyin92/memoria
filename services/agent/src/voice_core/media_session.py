"""Voice Core session registry for the media-v1 bridge.

This module is the narrow seam between transport and the existing
``DuplexRuntime``.  It deliberately does not implement a second LLM/TTS
stack: a deployment injects its ASR/LLM/TTS provider adapter, while this
registry owns session identity, sample-clock ASR acceptance, generation
fencing and downlink PCM delivery.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import GLOBAL_METRICS, MetricsRegistry
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.grpc_bridge import (
    MediaBridgeGrpcServer,
)
from services.agent.src.voice_core.interruption import InterruptionPolicy
from services.agent.src.voice_core.media_audio_ingress import (
    MediaAudioIngress,
)
from services.agent.src.voice_core.media_session_commit import (
    MediaSessionCommitMixin,
)
from services.agent.src.voice_core.media_session_connection import (
    MediaSessionConnectionMixin,
)
from services.agent.src.voice_core.media_session_input import MediaSessionInputMixin
from services.agent.src.voice_core.media_session_lifecycle import (
    MediaSessionLifecycleMixin,
)
from services.agent.src.voice_core.media_session_output import MediaOutputMixin
from services.agent.src.voice_core.media_session_projection import (
    MediaSessionProjectionMixin,
)
from services.agent.src.voice_core.media_session_state import (
    MediaVoiceSessionState as _MediaVoiceSession,
)
from services.agent.src.voice_core.media_session_turns import MediaTurnEndpointMixin
from services.agent.src.voice_core.media_session_types import (
    MediaReplyChunk,
    MediaSessionResources,
    MediaTextSpan,
    MediaVoiceProvider,
    ProviderFactory,
    RuntimeFactory,
    SessionFactory,
)
from services.agent.src.voice_core.media_session_types import (
    OutputWork as _OutputWork,  # noqa: F401
)

media_pb2: Any = _media_pb2
logger = logging.getLogger(__name__)


def _default_runtime_factory(session_id: str) -> DuplexRuntime:
    return DuplexRuntime.create(session_id=session_id)


@dataclass(slots=True)
class MediaVoiceCoreRegistry(
    MediaSessionLifecycleMixin,
    MediaSessionConnectionMixin,
    MediaSessionProjectionMixin,
    MediaSessionInputMixin,
    MediaSessionCommitMixin,
    MediaTurnEndpointMixin,
    MediaOutputMixin,
):
    """Attach one provider-neutral Voice Core session to each media session."""

    bridge: MediaBridgeGrpcServer
    provider_factory: ProviderFactory | None = None
    runtime_factory: RuntimeFactory = field(default=_default_runtime_factory)
    session_factory: SessionFactory | None = None
    metrics: MetricsRegistry = field(default_factory=lambda: GLOBAL_METRICS)
    interruption_policy: InterruptionPolicy = field(default_factory=InterruptionPolicy)
    max_sessions: int = 256
    session_creation_limit: int = 32
    # ~5.1s of 20 ms frames: a provider task rotation or a slow ASR send must
    # not destroy admitted speech while the pump is briefly blocked.  Overflow
    # beyond this window drops only the oldest frame and logs a warning.
    audio_ingress_max_frames: int = 256
    reconnect_grace_s: float = 30.0
    # Child speech commonly contains 500-800 ms within-turn pauses.  The VAD
    # edge is therefore only a candidate endpoint until this quiescence
    # window passes and final ASR covers the same sample-clock position.
    turn_endpoint_grace_s: float = 0.9
    turn_endpoint_min_grace_s: float = 0.7
    turn_endpoint_max_grace_s: float = 1.1
    turn_endpoint_absolute_timeout_s: float = 2.5
    output_generation_timeout_s: float = 45.0
    delegation_initial_decision_timeout_s: float = 0.5
    _sessions: dict[str, _MediaVoiceSession] = field(default_factory=dict, init=False)
    _cleanup_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _creation_futures: dict[str, asyncio.Future[_MediaVoiceSession]] = field(
        default_factory=dict,
        init=False,
    )
    _creation_semaphore: asyncio.Semaphore = field(init=False)
    _audio_ingress: MediaAudioIngress = field(init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)



    def context(self, session_id: str) -> DuplexRuntime | None:
        current = self._sessions.get(session_id)
        return current.runtime if current is not None and not current.closed else None


__all__ = [
    "MediaTextSpan",
    "MediaReplyChunk",
    "MediaSessionResources",
    "MediaVoiceCoreRegistry",
    "MediaVoiceProvider",
]
