"""Session creation and reconnect lifecycle for Media Voice."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.conversation_projection import (
    ConversationProjection,
    ProjectionPatch,
)
from services.agent.src.orchestration.delegation_coordinator import (
    OutputIntentAdmission,
    SideEffectPolicy,
)
from services.agent.src.orchestration.task_manager import ToolSpec
from services.agent.src.voice_core.asr_stream_supervisor import (
    ASRAcceptDecision,
    ASRStreamSupervisor,
)
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_audio_ingress import (
    MediaAudioIngress,
    MediaAudioIngressState,
)
from services.agent.src.voice_core.media_bridge_server import MediaBridgeSession
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    MediaEnvelope,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session_state import (
    MediaVoiceSessionState as _MediaVoiceSession,
)
from services.agent.src.voice_core.media_session_types import (
    MediaVoiceProvider,
    ProviderFactory,
    RuntimeFactory,
    SessionFactory,
)
from services.agent.src.voice_core.speech_timeline import ASRResult, SpeechSegment
from services.common.realtime_information import requires_realtime_lookup


class MediaSessionLifecycleMixin:
    """Build, reuse and retire one Registry-owned session record."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        provider_factory: ProviderFactory | None
        runtime_factory: RuntimeFactory
        session_factory: SessionFactory | None
        metrics: MetricsRegistry
        max_sessions: int
        session_creation_limit: int
        audio_ingress_max_frames: int
        reconnect_grace_s: float
        turn_endpoint_grace_s: float
        turn_endpoint_min_grace_s: float
        turn_endpoint_max_grace_s: float
        turn_endpoint_absolute_timeout_s: float
        _sessions: dict[str, _MediaVoiceSession]
        _cleanup_tasks: dict[str, asyncio.Task[None]]
        _creation_futures: dict[str, asyncio.Future[_MediaVoiceSession]]
        _creation_semaphore: asyncio.Semaphore
        _audio_ingress: MediaAudioIngress
        _lock: asyncio.Lock

        async def _accept_asr_result_decision(
            self, session_id: str, result: ASRResult
        ) -> ASRAcceptDecision: ...

        async def _discard_projection(
            self, context: _MediaVoiceSession, reason: str
        ) -> None: ...

        async def on_audio_frame(
            self, session: MediaBridgeSession, frame: AudioFrame
        ) -> None: ...

        async def on_speech_segment(
            self,
            session: MediaBridgeSession,
            segment: SpeechSegment,
            stream_epoch: int = 0,
        ) -> None: ...

        async def on_client_event(
            self,
            session: MediaBridgeSession,
            event: MediaEnvelope,
            stream_epoch: int = 0,
        ) -> None: ...

        async def on_session_closed(self, session: MediaBridgeSession) -> None: ...

        async def on_playback_progress(
            self, session: MediaBridgeSession, progress: PlaybackProgress
        ) -> None: ...

        async def on_downlink_overflow(self, session: MediaBridgeSession) -> None: ...

        async def _emit_projection_patch(
            self, context: _MediaVoiceSession, patch: ProjectionPatch
        ) -> None: ...

        async def _emit_floor_effect(
            self,
            context: _MediaVoiceSession,
            *,
            source_event_id: str,
            phase: str | None = None,
            fence: GenerationFence | None = None,
            task_epoch: int | None = None,
            context_version: int | None = None,
        ) -> bool: ...

        async def _run_media_delegation(self, *args: Any, **kwargs: Any) -> None: ...

        async def _publish_runtime_event(
            self, context: _MediaVoiceSession, event: dict[str, Any]
        ) -> None: ...

    def __post_init__(self) -> None:
        if self.provider_factory is None and self.session_factory is None:
            raise ValueError("media provider_factory or session_factory is required")
        if self.max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        if self.session_creation_limit <= 0:
            raise ValueError("session_creation_limit must be positive")
        if self.audio_ingress_max_frames <= 0:
            raise ValueError("audio_ingress_max_frames must be positive")
        if self.reconnect_grace_s <= 0:
            raise ValueError("reconnect_grace_s must be positive")
        if self.turn_endpoint_grace_s < 0:
            raise ValueError("turn_endpoint_grace_s must be non-negative")
        if (
            self.turn_endpoint_min_grace_s < 0
            or self.turn_endpoint_max_grace_s < self.turn_endpoint_min_grace_s
            or self.turn_endpoint_absolute_timeout_s <= self.turn_endpoint_max_grace_s
        ):
            raise ValueError("media endpoint grace and tail timeouts are invalid")
        self._creation_semaphore = asyncio.Semaphore(self.session_creation_limit)
        self._audio_ingress = MediaAudioIngress(self)

    def install(self) -> None:
        """Connect this registry to a ``MediaBridgeGrpcServer`` instance."""

        self.bridge.on_audio_frame = self.on_audio_frame
        self.bridge.on_speech_segment = self.on_speech_segment
        self.bridge.on_client_event = self.on_client_event
        self.bridge.on_session_closed = self.on_session_closed
        self.bridge.on_playback_progress = self.on_playback_progress
        self.bridge.on_downlink_overflow = self.on_downlink_overflow

    def _stream_epoch_is_current(
        self,
        context: _MediaVoiceSession,
        stream_epoch: int,
    ) -> bool:
        bridge_session = self.bridge.bridge.get(context.identity.session_id)
        return bool(
            not context.closed
            and context.stream_epoch == stream_epoch
            and context.identity.stream_epoch == stream_epoch
            and (
                bridge_session is None
                or (
                    bridge_session.state != "closed"
                    and bridge_session.identity.stream_epoch == stream_epoch
                )
            )
        )

    async def _cancel_audio_pump(self, context: _MediaVoiceSession) -> None:
        await self._audio_ingress.cancel(context)

    async def _reuse_session(
        self,
        current: _MediaVoiceSession,
        identity: SessionIdentity,
    ) -> _MediaVoiceSession:
        cleanup = self._cleanup_tasks.pop(identity.session_id, None)
        if cleanup is not None and not cleanup.done():
            cleanup.cancel()
        if current.identity.account_id != identity.account_id:
            raise ValueError("media session account identity changed")
        if (
            current.identity.participant_id != identity.participant_id
            or current.identity.device_id != identity.device_id
            or current.identity.client_type != identity.client_type
        ):
            raise ValueError("media session device identity changed")
        if identity.stream_epoch < current.stream_epoch:
            raise ValueError("media session stream epoch moved backwards")
        discarded: ProjectionPatch | None = None
        reconnected = False
        if identity.stream_epoch > current.stream_epoch:
            await self._cancel_audio_pump(current)
            async with current.turn_commit_lock:
                # A pending commit may have completed while this lookup waited
                # for the lock. Re-check the epoch before mutating the session.
                if identity.stream_epoch < current.stream_epoch:
                    raise ValueError("media session stream epoch moved backwards")
                if identity.stream_epoch == current.stream_epoch:
                    return current
                discarded = current.projection.discard_provisional(
                    None,
                    "stream_epoch_changed",
                )
                current.identity = identity
                current.stream_epoch = identity.stream_epoch
                current.floor_epoch = 0
                current.runtime.start_media_stream_epoch(identity.stream_epoch)
                current.runtime.orchestrator.delegation.reset_output_intent_state(
                    identity.session_id
                )
                if not current.asr.reconnect(stream_epoch=identity.stream_epoch):
                    raise ValueError("ASR stream epoch did not advance")
                endpoint_task = current.turn_endpoint_task
                if endpoint_task is not None and not endpoint_task.done():
                    endpoint_task.cancel()
                current.turn_start_sample = None
                current.turn_end_sample = None
                current.turn_endpoint_sample = None
                current.turn_retire_sample = None
                current.turn_endpoint_grace_deadline = None
                current.turn_endpoint_tail_deadline = None
                if current.turn_endpoint_timeout_handle is not None:
                    current.turn_endpoint_timeout_handle.cancel()
                    current.turn_endpoint_timeout_handle = None
                current.committed_asr_keys.clear()
                current.pending_partial = None
                self._audio_ingress.reset_for_reconnect(current)
                reconnected = True
        if discarded is not None:
            await self._emit_projection_patch(current, discarded)
        if reconnected:
            await self._emit_floor_effect(current, source_event_id="media_session_reconnected")
        return current

    @staticmethod
    async def _close_unpublished_resources(
        runtime: DuplexRuntime | None,
        provider: MediaVoiceProvider | None,
        identity: SessionIdentity,
    ) -> None:
        """Release factory resources when session publication did not finish."""

        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.close()
        if provider is not None:
            with contextlib.suppress(Exception):
                result = provider.close(identity)
                if inspect.isawaitable(result):
                    await result

    async def _build_session(self, identity: SessionIdentity) -> _MediaVoiceSession:
        runtime: DuplexRuntime | None = None
        provider: MediaVoiceProvider | None = None
        try:
            if self.session_factory is not None:
                resources = self.session_factory(identity)
                if inspect.isawaitable(resources):
                    resources = await resources
                runtime = resources.runtime
                provider = resources.provider
                if runtime.session_id != identity.session_id:
                    await self._close_unpublished_resources(runtime, provider, identity)
                    runtime = None
                    provider = None
                    raise ValueError("media session factory returned a mismatched runtime")
            else:
                assert self.provider_factory is not None
                runtime = self.runtime_factory(identity.session_id)
                provider = self.provider_factory(identity)
            provider_warmer = getattr(provider, "prewarm", None)
            if callable(provider_warmer):
                runtime.set_fast_model_warmer(provider_warmer)
            asr = ASRStreamSupervisor(stream_epoch=identity.stream_epoch)
            current = _MediaVoiceSession(
                identity=identity,
                runtime=runtime,
                provider=provider,
                asr=asr,
                projection=ConversationProjection(
                    session_id=identity.session_id,
                    timeline=runtime.speech_timeline,
                ),
                stream_epoch=identity.stream_epoch,
                ingress=MediaAudioIngressState.create(self.audio_ingress_max_frames),
            )

            def observe_output_intent(admission: OutputIntentAdmission) -> None:
                self.bridge.emit_output_intent_decision(identity.session_id, admission)

            runtime.orchestrator.delegation.set_output_intent_observer(observe_output_intent)
            delegation_starter = getattr(provider, "start_delegation", None)
            output_intent_acceptor = getattr(provider, "accept_output_intent", None)
            if (
                callable(delegation_starter)
                and callable(output_intent_acceptor)
                and bool(getattr(provider, "supports_delegation", True))
            ):

                async def _run_deep_work(
                    arguments: dict[str, Any],
                    cancel: asyncio.Event,
                ) -> Any:
                    if cancel.is_set():
                        return None
                    result = delegation_starter(
                        str(arguments["text"]),
                        arguments["fence"],
                    )
                    return await result if inspect.isawaitable(result) else result

                runtime.orchestrator.task_manager.register(
                    ToolSpec(
                        name="media_deep_response",
                        description="fenced deep response work for the media runtime",
                        input_schema={"type": "object", "required": ["text"]},
                        cancellable=True,
                        idempotent=True,
                        timeout_s=20.0,
                        side_effect_policy=SideEffectPolicy.READ_ONLY.value,
                    ),
                    _run_deep_work,
                )

                async def _start_delegation(text: str, fence: GenerationFence) -> None:
                    if not requires_realtime_lookup(text):
                        return
                    await self._run_media_delegation(
                        current,
                        text=text,
                        fence=fence,
                        output_intent_acceptor=output_intent_acceptor,
                    )

                current.delegation_owns_realtime_output = True
                runtime.set_delegation_starter(_start_delegation)

            async def publish_runtime_event(event: dict[str, Any]) -> None:
                await self._publish_runtime_event(current, event)

            runtime.set_event_publisher(publish_runtime_event)
            return current
        except BaseException:
            await self._close_unpublished_resources(runtime, provider, identity)
            raise

    async def _get_or_create(self, identity: SessionIdentity) -> _MediaVoiceSession:
        current = self._sessions.get(identity.session_id)
        if current is not None:
            return await self._reuse_session(current, identity)

        owner = False
        async with self._lock:
            current = self._sessions.get(identity.session_id)
            if current is not None:
                future = None
            else:
                future = self._creation_futures.get(identity.session_id)
                if future is None:
                    if len(self._sessions) + len(self._creation_futures) >= self.max_sessions:
                        raise RuntimeError("Voice Core media session limit reached")
                    future = asyncio.get_running_loop().create_future()
                    self._creation_futures[identity.session_id] = future
                    owner = True
        if current is not None:
            return await self._reuse_session(current, identity)
        assert future is not None
        if not owner:
            shared = await asyncio.shield(future)
            return await self._reuse_session(shared, identity)

        created: _MediaVoiceSession | None = None
        try:
            async with self._creation_semaphore:
                created = await self._build_session(identity)
                await self._emit_floor_effect(created, source_event_id="media_session_ready")
            async with self._lock:
                self._sessions[identity.session_id] = created
                self._creation_futures.pop(identity.session_id, None)
            future.set_result(created)
            self.metrics.inc_media_session_started()
            self.metrics.set_media_active_sessions(len(self._sessions))
            return created
        except BaseException as exc:
            if created is not None:
                await self._close_unpublished_resources(
                    created.runtime,
                    created.provider,
                    identity,
                )
            async with self._lock:
                self._creation_futures.pop(identity.session_id, None)
            if not future.done():
                if isinstance(exc, asyncio.CancelledError):
                    future.cancel()
                else:
                    future.set_exception(exc)
                    # The owner may be the only caller. Mark the exception as
                    # observed while preserving it for any singleflight waiter.
                    future.exception()
            raise
