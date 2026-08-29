"""Session creation and reconnect lifecycle for Media Voice."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
from collections.abc import Coroutine
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
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
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
    DelegationOutputClaim,
    MediaVoiceProvider,
    ProviderFactory,
    RuntimeFactory,
    SessionFactory,
)
from services.agent.src.voice_core.speech_timeline import ASRResult, SpeechSegment
from services.common.realtime_information import requires_realtime_lookup

media_pb2: Any = _media_pb2


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
        output_generation_timeout_s: float
        delegation_initial_decision_timeout_s: float
        owner_silence_timeout_s: float
        max_user_speech_duration_s: float
        _sessions: dict[str, _MediaVoiceSession]
        _cleanup_tasks: dict[str, asyncio.Task[None]]
        _creation_futures: dict[str, asyncio.Future[_MediaVoiceSession]]
        _creation_semaphore: asyncio.Semaphore
        _audio_ingress: MediaAudioIngress
        _lock: asyncio.Lock

        async def _accept_asr_result_decision(
            self, session_id: str, result: ASRResult
        ) -> ASRAcceptDecision: ...

        async def _discard_projection(self, context: _MediaVoiceSession, reason: str) -> None: ...

        def _clear_pending_turn_state(self, context: _MediaVoiceSession) -> None: ...

        def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None: ...

        async def on_audio_frame(self, session: MediaBridgeSession, frame: AudioFrame) -> None: ...

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

        async def _finalize_session(self, session_id: str) -> None: ...

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

        async def _cancel_reply_task(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
            *,
            reason: str = "cancelled",
            cancel_timeout_s: float = 5.0,
        ) -> None: ...

        def _event_versions(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
        ) -> tuple[int, int]: ...

        async def _publish_runtime_event(
            self, context: _MediaVoiceSession, event: dict[str, Any]
        ) -> None: ...

        def _arm_owner_silence_timer(
            self, context: _MediaVoiceSession, *, reset: bool
        ) -> None: ...

        def _resume_owner_silence_after_reconnect(
            self, context: _MediaVoiceSession
        ) -> None: ...

        def _cancel_max_user_speech_watchdog(
            self, context: _MediaVoiceSession
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
        if (
            not math.isfinite(self.output_generation_timeout_s)
            or self.output_generation_timeout_s <= 0
        ):
            raise ValueError("output_generation_timeout_s must be finite and positive")
        if (
            not math.isfinite(self.delegation_initial_decision_timeout_s)
            or self.delegation_initial_decision_timeout_s <= 0
        ):
            raise ValueError("delegation_initial_decision_timeout_s must be finite and positive")
        if not math.isfinite(self.owner_silence_timeout_s) or self.owner_silence_timeout_s < 0:
            raise ValueError("owner_silence_timeout_s must be finite and non-negative")
        if (
            not math.isfinite(self.max_user_speech_duration_s)
            or self.max_user_speech_duration_s < 0
        ):
            raise ValueError("max_user_speech_duration_s must be finite and non-negative")
        self._creation_semaphore = asyncio.Semaphore(self.session_creation_limit)
        self._audio_ingress = MediaAudioIngress(self)

    def install(self) -> None:
        """Connect this registry to a ``MediaBridgeGrpcServer`` instance."""

        self.bridge.on_audio_frame = self.on_audio_frame
        self.bridge.on_speech_segment = self.on_speech_segment
        self.bridge.on_client_event = self.on_client_event
        self.bridge.on_session_closed = self.on_session_closed
        self.bridge.on_session_connected = self.on_session_connected
        self.bridge.on_playback_progress = self.on_playback_progress
        self.bridge.on_downlink_overflow = self.on_downlink_overflow

    async def on_session_connected(self, session: MediaBridgeSession) -> None:
        """Install a replacement epoch before resumed downlink can flow."""

        session_id = session.identity.session_id
        current = self._sessions.get(session_id)
        if current is not None:
            try:
                await self._reuse_session(current, session.identity)
            except Exception:
                # The bridge has already accepted the replacement transport
                # epoch before this callback runs.  If the provider cannot
                # rotate with it, the old Registry context and new bridge
                # session no longer share one authority fence.  Retire both
                # sides instead of leaving an uncollectable split epoch.
                with contextlib.suppress(Exception):
                    await self._finalize_session(session_id)
                # ``_finalize_session`` closes only the Registry-owned old
                # epoch.  The bridge is already on the replacement epoch, so
                # its guarded close intentionally cannot remove this session.
                self.bridge.bridge.close(session_id)
                raise

    def _stream_epoch_is_current(
        self,
        context: _MediaVoiceSession,
        stream_epoch: int,
    ) -> bool:
        bridge_session = self.bridge.bridge.get(context.identity.session_id)
        return bool(
            not context.closed
            and not context.standby_requested
            and context.stream_epoch == stream_epoch
            and context.identity.stream_epoch == stream_epoch
            and (
                bridge_session is None
                or (
                    bridge_session.accepts_input()
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
        if current.closed or current.standby_requested:
            raise ValueError("media conversation is already closed")
        if current.identity.account_id != identity.account_id:
            raise ValueError("media session account identity changed")
        if (
            current.identity.participant_id != identity.participant_id
            or current.identity.device_id != identity.device_id
            or current.identity.client_type != identity.client_type
        ):
            raise ValueError("media session device identity changed")
        if not current.identity.has_same_reconnect_authority(identity):
            raise ValueError("media session runtime authority fence changed")
        if identity.stream_epoch < current.stream_epoch:
            raise ValueError("media session stream epoch moved backwards")
        cleanup = self._cleanup_tasks.pop(identity.session_id, None)
        if cleanup is not None and not cleanup.done():
            cleanup.cancel()
        discarded: ProjectionPatch | None = None
        reconnected = False
        if identity.stream_epoch > current.stream_epoch:
            # The pending turn is discarded at an epoch boundary.  Do not let
            # its absolute speech watchdog fire against the replacement
            # transport while the old provider task is being rotated.
            self._cancel_max_user_speech_watchdog(current)
            await self._cancel_audio_pump(current)
            async with current.ingress.finalize_lock:
                async with current.turn_commit_lock:
                    # A pending commit or VAD final may have completed while this
                    # lookup waited. Re-check before mutating the session.
                    if identity.stream_epoch < current.stream_epoch:
                        raise ValueError("media session stream epoch moved backwards")
                    if not current.identity.has_same_reconnect_authority(identity):
                        raise ValueError("media session runtime authority fence changed")
                    if identity.stream_epoch == current.stream_epoch:
                        return current
                    reset_provider = getattr(current.provider, "reset_for_stream_epoch", None)
                    if callable(reset_provider):
                        result = reset_provider(identity)
                        if inspect.isawaitable(result):
                            await result
                    discarded = current.projection.discard_provisional(
                        None,
                        "stream_epoch_changed",
                    )
                    current.projection.reset_phase(stream_epoch=identity.stream_epoch)
                    current.identity = identity
                    current.stream_epoch = identity.stream_epoch
                    current.floor_epoch = 0
                    current.runtime.start_media_stream_epoch(identity.stream_epoch)
                    # A transport reconnect preserves the current Generation and
                    # its selected output lease. Candidate output is fenced by
                    # Generation/context, not stream_epoch; clearing it here
                    # would cancel the very reply the new epoch is resuming.
                    if not current.asr.reconnect(stream_epoch=identity.stream_epoch):
                        raise ValueError("ASR stream epoch did not advance")
                    align_provider_task_epoch = getattr(
                        current.provider,
                        "set_asr_task_epoch_floor",
                        None,
                    )
                    if callable(align_provider_task_epoch):
                        align_provider_task_epoch(
                            current.asr.latest_authoritative_task_epoch,
                        )
                    endpoint_task = current.turn_endpoint_task
                    if endpoint_task is not None and not endpoint_task.done():
                        endpoint_task.cancel()
                    self._clear_pending_turn_state(current)
                    self._audio_ingress.reset_for_reconnect(current)
                    reconnected = True
        if discarded is not None:
            await self._emit_projection_patch(current, discarded)
        if reconnected:
            await self._emit_floor_effect(current, source_event_id="media_session_reconnected")
        self._resume_owner_silence_after_reconnect(current)
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
            runtime.set_device_conversation_controls(identity.client_type == "device")

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

                def _start_delegation(
                    text: str,
                    fence: GenerationFence,
                ) -> Coroutine[Any, Any, None] | None:
                    if not requires_realtime_lookup(text) or not current.runtime.fence.matches(
                        fence
                    ):
                        return None
                    existing = current.delegation_output_claims.get(fence)
                    if existing is not None:
                        return None
                    for old_fence, old_claim in tuple(current.delegation_output_claims.items()):
                        if old_fence.matches(fence):
                            continue
                        old_claim.release()
                        current.delegation_output_claims.pop(old_fence, None)
                    claim = DelegationOutputClaim(fence)
                    current.delegation_output_claims[fence] = claim
                    return self._run_media_delegation(
                        current,
                        text=text,
                        fence=fence,
                        claim=claim,
                        output_intent_acceptor=output_intent_acceptor,
                    )

                runtime.set_delegation_starter(_start_delegation)

            async def publish_runtime_event(event: dict[str, Any]) -> None:
                await self._publish_runtime_event(current, event)

            runtime.set_event_publisher(publish_runtime_event)

            async def stop_direct_playback() -> None:
                """Drain the Registry-owned physical output during epoch rotation."""

                if current.closed:
                    return
                owner = current.output_owner
                old_fence = owner.fence if owner is not None else current.playback.current_fence
                if old_fence is None:
                    return

                await self._cancel_reply_task(
                    current,
                    old_fence,
                    reason="identity_epoch_rotated",
                )

                runtime_fence = current.runtime.fence
                cancelled = await current.runtime.preempt_media_output(
                    cause="identity_epoch_rotated",
                    # The runtime identity already rotated before this drain.
                    # Old-fence playback evidence must never be rebound to the
                    # new subject; only ACKs published before rotation survive.
                    synchronized_transcript="",
                )
                if cancelled.matches(runtime_fence):
                    raise RuntimeError(
                        "Direct playback stop did not advance the runtime generation"
                    )
                await current.runtime.on_media_playback_interrupted(
                    interrupted_from=runtime_fence,
                    synchronized_transcript="",
                )
                current.playback.discard(old_fence)
                current.output_work.clear()
                task_epoch, context_version = self._event_versions(current, cancelled)
                if not await self.bridge.emit_realtime_effect(
                    cancelled.session_id,
                    media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
                    cancelled,
                    source_event_id="identity_epoch_rotated",
                    payload={"reason": "identity_epoch_rotated"},
                    task_epoch=task_epoch,
                    context_version=context_version,
                ):
                    raise RuntimeError("Direct playback stop did not reach the Media Edge")

            runtime.set_playback_stop_seam(stop_direct_playback)
            return current
        except BaseException:
            await self._close_unpublished_resources(runtime, provider, identity)
            raise

    async def _get_or_create(self, identity: SessionIdentity) -> _MediaVoiceSession:
        bridge_session = self.bridge.bridge.get(identity.session_id)
        if self.bridge.bridge.is_terminal(
            identity.session_id,
            stream_epoch=identity.stream_epoch,
        ) or (
            bridge_session is not None and not bridge_session.accepts_input()
        ):
            # A terminal bridge session may already have been removed from its
            # live map.  Refuse creation before factories run so stale input
            # cannot recreate a runtime under the same session id.
            raise ValueError("media session is terminal")
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
                bridge_session = self.bridge.bridge.get(identity.session_id)
                if self.bridge.bridge.is_terminal(
                    identity.session_id,
                    stream_epoch=identity.stream_epoch,
                ) or (
                    bridge_session is not None and not bridge_session.accepts_input()
                ):
                    raise ValueError("media session is terminal")
                self._sessions[identity.session_id] = created
                self._creation_futures.pop(identity.session_id, None)
            future.set_result(created)
            self.metrics.inc_media_session_started()
            self.metrics.set_media_active_sessions(len(self._sessions))
            self._arm_owner_silence_timer(created, reset=True)
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
