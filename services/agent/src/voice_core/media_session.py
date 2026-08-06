"""Voice Core session registry for the media-v1 bridge.

This module is the narrow seam between transport and the existing
``DuplexRuntime``.  It deliberately does not implement a second LLM/TTS
stack: a deployment injects its ASR/LLM/TTS provider adapter, while this
registry owns session identity, sample-clock ASR acceptance, generation
fencing and downlink PCM delivery.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from uuid import uuid4

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import GLOBAL_METRICS, MetricsRegistry
from services.agent.src.orchestration.conversation_projection import (
    CommitEvidence,
    CommittedTurn,
    ConversationProjection,
    ProjectionPatch,
    ProjectionRejectReason,
    SpeakerEvidence,
)
from services.agent.src.orchestration.delegation_coordinator import (
    DelegationRequest,
    OutputIntentAdmission,
    SideEffectPolicy,
)
from services.agent.src.orchestration.interaction_plane import (
    InteractionEvent,
    InteractionSnapshot,
)
from services.agent.src.orchestration.state_machine import ConversationState, InteractionPhase
from services.agent.src.orchestration.task_manager import ToolSpec
from services.agent.src.prompts import BRIDGE_PHRASES
from services.agent.src.voice_core.asr_stream_supervisor import (
    ASRAcceptDecision,
    ASRDecisionReason,
    ASRStreamSupervisor,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.grpc_bridge import (
    MediaBridgeGrpcServer,
    floor_state_for_phase,
)
from services.agent.src.voice_core.media_bridge_server import (
    MediaBridgeSession,
    PCMFrame,
)
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    MediaEnvelope,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.playback_ledger import PlaybackLedger, PlaybackSpan
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
    asr_result_to_segment,
)
from services.common.realtime_information import requires_realtime_lookup

media_pb2: Any = _media_pb2
logger = logging.getLogger(__name__)
_CONVERSATION_REPLY_TTL_MS = 120_000
# Other wire kinds remain valid shadow/reserved metadata until they have a real producer.
_STREAMCORE_EXECUTABLE_OUTPUT_KINDS = frozenset(
    {
        int(media_pb2.OUTPUT_INTENT_KIND_CONVERSATION_REPLY),
        int(media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT),
        int(media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT),
    }
)


def _default_runtime_factory(session_id: str) -> DuplexRuntime:
    return DuplexRuntime.create(session_id=session_id)


@dataclass(frozen=True, slots=True)
class MediaTextSpan:
    """Provider-aligned text and its authoritative audio interval."""

    text: str
    audio_start_sample: int
    audio_end_sample: int

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("media text span must not be empty")
        if self.audio_start_sample < 0 or self.audio_end_sample <= self.audio_start_sample:
            raise ValueError("media text span audio range must be positive")


@dataclass(frozen=True, slots=True)
class MediaReplyChunk:
    """One provider-produced PCM chunk bound to a generation fence."""

    pcm_s16le: bytes
    source_start_sample: int
    text: str = ""
    # Provider-visible text and playback-ledger text have different clocks:
    # announce a phrase on its first PCM frame, then attach ``text`` with the
    # complete audio range once the phrase boundary is known. ``None`` keeps
    # legacy chunks using ``text`` for both roles; ``""`` suppresses a second
    # announcement on the later ledger-metadata frame.
    assistant_text_delta: str | None = None
    first: bool = False
    final: bool = False
    # A fixed-frame provider may put a phrase's text on its first frame while
    # the phrase spans several frames. Keep the complete audio span explicit.
    text_audio_start_sample: int | None = None
    text_audio_end_sample: int | None = None
    # A streaming provider can deliver word-level timestamps only after it has
    # finished audio. Attach those facts to any already-valid PCM chunk rather
    # than guessing phrase boundaries from LLM scheduling.
    text_spans: tuple[MediaTextSpan, ...] = ()

    def __post_init__(self) -> None:
        if not self.pcm_s16le or len(self.pcm_s16le) % 2:
            raise ValueError("media reply PCM must be non-empty 16-bit audio")
        if self.source_start_sample < 0:
            raise ValueError("media reply sample range must be non-negative")
        if (self.text_audio_start_sample is None) != (self.text_audio_end_sample is None):
            raise ValueError("text audio bounds must be provided together")
        text_audio_start = self.text_audio_start_sample
        text_audio_end = self.text_audio_end_sample
        if text_audio_start is not None and text_audio_end is not None:
            if text_audio_start < 0:
                raise ValueError("text audio start must be non-negative")
            if text_audio_end <= text_audio_start:
                raise ValueError("text audio range must be positive")
        previous_end = -1
        for span in self.text_spans:
            if span.audio_start_sample < previous_end:
                raise ValueError("media text spans must be ordered")
            previous_end = span.audio_end_sample

    @property
    def frame_samples(self) -> int:
        return len(self.pcm_s16le) // 2


class MediaVoiceProvider(Protocol):
    """Provider-neutral adapter implemented by FunASR/Qwen/Doubao wiring."""

    async def ingest_audio(
        self,
        identity: SessionIdentity,
        frame: AudioFrame,
    ) -> Sequence[ASRResult]: ...

    def generate_reply(
        self,
        identity: SessionIdentity,
        user_text: str,
        fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]: ...

    async def close(self, identity: SessionIdentity) -> None: ...


ProviderFactory = Callable[[SessionIdentity], MediaVoiceProvider]
RuntimeFactory = Callable[[str], DuplexRuntime]


@dataclass(frozen=True, slots=True)
class MediaSessionResources:
    """One session-scoped runtime and provider built from the same authority context."""

    runtime: DuplexRuntime
    provider: MediaVoiceProvider


@dataclass(frozen=True, slots=True)
class _OutputOwnerLease:
    intent: Any
    fence: GenerationFence
    task: asyncio.Task[Any]


@dataclass(frozen=True, slots=True)
class _OutputWork:
    """One fenced source that the Registry may render through its sole owner."""

    intent: Any
    conversation_text: str | None = None

    @property
    def intent_id(self) -> str:
        return str(self.intent.intent_id)

    @property
    def fence(self) -> GenerationFence:
        return GenerationFence(
            session_id=str(self.intent.session_id),
            turn_id=int(self.intent.turn_id),
            generation_id=int(self.intent.generation_id),
            tool_epoch=int(self.intent.tool_epoch),
        )


SessionFactory = Callable[
    [SessionIdentity],
    MediaSessionResources | Awaitable[MediaSessionResources],
]


@dataclass(slots=True)
class _MediaVoiceSession:
    identity: SessionIdentity
    runtime: DuplexRuntime
    provider: MediaVoiceProvider
    asr: ASRStreamSupervisor
    projection: ConversationProjection
    playback: PlaybackLedger = field(default_factory=PlaybackLedger)
    output_sequence: int = 0
    output_text_offset: int = 0
    assistant_text: str = ""
    stream_epoch: int = 0
    floor_epoch: int = 0
    turn_started_ns: int | None = None
    first_audio_observed: bool = False
    provider_complete: bool = False
    output_complete_emitted: bool = False
    reply_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Reconnect must not replace the transport identity halfway through the
    # provider-prepare/Projection-commit transaction.
    turn_commit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reply_task: asyncio.Task[bool] | None = None
    output_owner: _OutputOwnerLease | None = None
    output_work: dict[str, _OutputWork] = field(default_factory=dict)
    output_dispatch_task: asyncio.Task[bool] | None = None
    delegation_owns_realtime_output: bool = False
    committed_asr_keys: set[tuple[int, str, int, int]] = field(default_factory=set)
    turn_start_sample: int | None = None
    turn_end_sample: int | None = None
    turn_endpoint_sample: int | None = None
    turn_retire_sample: int | None = None
    turn_endpoint_task: asyncio.Task[None] | None = None
    closed: bool = False


@dataclass(slots=True)
class MediaVoiceCoreRegistry:
    """Attach one provider-neutral Voice Core session to each media session."""

    bridge: MediaBridgeGrpcServer
    provider_factory: ProviderFactory | None = None
    runtime_factory: RuntimeFactory = field(default=_default_runtime_factory)
    session_factory: SessionFactory | None = None
    metrics: MetricsRegistry = field(default_factory=lambda: GLOBAL_METRICS)
    max_sessions: int = 256
    reconnect_grace_s: float = 30.0
    # Child speech commonly contains 500-800 ms within-turn pauses.  The VAD
    # edge is therefore only a candidate endpoint until this quiescence
    # window passes and final ASR covers the same sample-clock position.
    turn_endpoint_grace_s: float = 0.9
    _sessions: dict[str, _MediaVoiceSession] = field(default_factory=dict, init=False)
    _cleanup_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        if self.provider_factory is None and self.session_factory is None:
            raise ValueError("media provider_factory or session_factory is required")
        if self.max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        if self.reconnect_grace_s <= 0:
            raise ValueError("reconnect_grace_s must be positive")
        if self.turn_endpoint_grace_s < 0:
            raise ValueError("turn_endpoint_grace_s must be non-negative")

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
                current.committed_asr_keys.clear()
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

    async def _get_or_create(self, identity: SessionIdentity) -> _MediaVoiceSession:
        current = self._sessions.get(identity.session_id)
        if current is not None:
            return await self._reuse_session(current, identity)
        await self._lock.acquire()
        lock_held = True
        runtime: DuplexRuntime | None = None
        provider: MediaVoiceProvider | None = None
        published = False
        try:
            current = self._sessions.get(identity.session_id)
            if current is not None:
                lock_held = False
                self._lock.release()
                return await self._reuse_session(current, identity)
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError("Voice Core media session limit reached")
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
            self._sessions[identity.session_id] = current
            await self._emit_floor_effect(current, source_event_id="media_session_ready")
            published = True
            self.metrics.inc_media_session_started()
            self.metrics.set_media_active_sessions(len(self._sessions))
            return current
        finally:
            if not published:
                await self._close_unpublished_resources(runtime, provider, identity)
            if lock_held:
                self._lock.release()

    async def _run_media_delegation(
        self,
        context: _MediaVoiceSession,
        *,
        text: str,
        fence: GenerationFence,
        output_intent_acceptor: Callable[[Any], Any],
    ) -> None:
        runtime = context.runtime
        if not runtime.fence.matches(fence):
            return
        coordinator = runtime.orchestrator.delegation
        try:
            context_version = runtime.orchestrator.context_version_for_fence(fence)
            handle = await coordinator.delegate(
                DelegationRequest(
                    tool_name="media_deep_response",
                    arguments={"text": text, "fence": fence},
                    fence=fence,
                    task_epoch=coordinator.next_task_epoch(fence.session_id),
                    context_version=context_version,
                    expires_at_ms=int(time.time() * 1_000) + 20_000,
                    side_effect_policy=SideEffectPolicy.READ_ONLY,
                    committed=True,
                    relevance=lambda: runtime.fence.matches(fence),
                    output_kind=media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT,
                )
            )
        except (KeyError, PermissionError, ValueError):
            logger.warning("media delegation rejected", exc_info=True)
            return
        if not handle.record.task.done():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(handle.record.task), timeout=0.02)
        if not handle.record.task.done() and runtime.fence.matches(fence):
            now_ms = int(time.time() * 1_000)
            acknowledgement = coordinator.bridge_acknowledgement(
                BRIDGE_PHRASES[1],
                fence=fence,
                context_version=context_version,
                expires_at_ms=now_ms + 5_000,
                now_ms=now_ms,
            )
            coordinator.admit_output_intent(
                acknowledgement,
                current_fence=runtime.fence,
                current_context_version=coordinator.current_context_version(fence.session_id),
                floor_allows_output=runtime.output_floor_allows_assistant,
                now_ms=now_ms,
            )
            if coordinator.output_intent_is_active(
                acknowledgement,
                current_fence=runtime.fence,
                current_context_version=coordinator.current_context_version(fence.session_id),
                floor_allows_output=runtime.output_floor_allows_assistant,
                now_ms=now_ms,
            ):
                await self._enqueue_output_work(context, _OutputWork(acknowledgement))
        async for _event in coordinator.events(handle):
            pass
        intent = coordinator.output_intent(
            handle,
            current_fence=runtime.fence,
            current_task_epoch=handle.request.task_epoch,
            current_context_version=coordinator.current_context_version(fence.session_id),
            relevant=runtime.fence.matches(fence),
        )
        if intent is None:
            if runtime.fence.matches(fence):
                await runtime.on_assistant_reply_aborted(
                    fence,
                    cause="media_delegation_no_result",
                )
            return
        coordinator.admit_output_intent(
            intent,
            current_fence=runtime.fence,
            current_context_version=coordinator.current_context_version(fence.session_id),
            floor_allows_output=runtime.output_floor_allows_assistant,
        )
        if not coordinator.output_intent_is_active(
            intent,
            current_fence=runtime.fence,
            current_context_version=coordinator.current_context_version(fence.session_id),
            floor_allows_output=runtime.output_floor_allows_assistant,
        ):
            return
        accepted = output_intent_acceptor(intent)
        if inspect.isawaitable(accepted):
            await accepted
        await self._enqueue_output_work(context, _OutputWork(intent))

    async def _publish_runtime_event(
        self,
        context: _MediaVoiceSession,
        event: dict[str, Any],
    ) -> None:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return
        payload = {key: value for key, value in event.items() if key != "type"}

        def event_int(key: str) -> int:
            value = event.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        fence = GenerationFence(
            context.identity.session_id,
            event_int("turn_id"),
            event_int("generation_id"),
            event_int("tool_epoch"),
        )
        task_epoch, context_version = self._event_versions(context, fence)
        if event_type == "assistant_state":
            phase = str(event.get("phase") or event.get("state") or "")
            await self._emit_floor_effect(
                context,
                source_event_id=f"assistant_state:{phase}",
                phase=phase,
                fence=fence,
                task_epoch=task_epoch,
                context_version=context_version,
            )
        if event_type == "assistant_audio":
            effect_kind = {
                "duck": media_pb2.REALTIME_EFFECT_KIND_DUCK_OUTPUT,
                "restore": media_pb2.REALTIME_EFFECT_KIND_RESUME_OUTPUT,
            }.get(str(event.get("action") or ""))
            if effect_kind is not None:
                await self.bridge.emit_realtime_effect(
                    context.identity.session_id,
                    effect_kind,
                    fence,
                    source_event_id=f"assistant_audio:{event['action']}",
                    payload=payload,
                    task_epoch=task_epoch,
                    context_version=context_version,
                )
            return
        await self.bridge.emit_event(
            context.identity.session_id,
            event_type,
            payload,
            turn_id=event_int("turn_id"),
            generation_id=event_int("generation_id"),
            tool_epoch=event_int("tool_epoch"),
            task_epoch=task_epoch,
            context_version=context_version,
        )

    async def _emit_floor_effect(
        self,
        context: _MediaVoiceSession,
        *,
        source_event_id: str,
        phase: str | None = None,
        fence: GenerationFence | None = None,
        task_epoch: int | None = None,
        context_version: int | None = None,
    ) -> bool:
        if not self._stream_epoch_is_current(context, context.stream_epoch):
            return False
        floor_state = floor_state_for_phase(
            phase or context.runtime.interaction_phase.value
        )
        if floor_state is None:
            return False
        current_fence = fence or context.runtime.fence
        if task_epoch is None or context_version is None:
            task_epoch, context_version = self._event_versions(context, current_fence)
        context.floor_epoch += 1
        return await self.bridge.emit_floor_effect(
            context.identity.session_id,
            floor_state,
            floor_epoch=context.floor_epoch,
            fence=current_fence,
            source_event_id=source_event_id,
            task_epoch=task_epoch,
            context_version=context_version,
        )

    @staticmethod
    def _event_versions(
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> tuple[int, int]:
        coordinator = context.runtime.orchestrator.delegation
        try:
            context_version = context.runtime.orchestrator.context_version_for_fence(fence)
        except ValueError:
            context_version = coordinator.current_context_version(context.identity.session_id)
        return coordinator.current_task_epoch(context.identity.session_id), context_version

    @staticmethod
    def _projection_speaker_evidence(context: _MediaVoiceSession) -> SpeakerEvidence:
        return SpeakerEvidence(
            speaker_class=context.runtime.current_speaker_class,
            reason_code=context.runtime.current_speaker_reason_code,
            authority_verified=context.runtime.current_speaker_authority_verified,
        )

    async def _emit_projection_patch(
        self,
        context: _MediaVoiceSession,
        patch: ProjectionPatch,
    ) -> None:
        fence = context.runtime.fence
        task_epoch, context_version = self._event_versions(context, fence)
        await self.bridge.emit_event(
            context.identity.session_id,
            patch.kind.value,
            patch.to_payload(),
            turn_id=fence.turn_id,
            generation_id=fence.generation_id,
            tool_epoch=fence.tool_epoch,
            task_epoch=task_epoch,
            context_version=context_version,
        )

    async def _apply_projection_segment(
        self,
        context: _MediaVoiceSession,
        segment: SpeechSegment,
    ) -> None:
        patch = context.projection.apply_continuous_event(
            segment,
            turn_id_hint=context.runtime.fence.turn_id + 1,
            speaker_evidence=self._projection_speaker_evidence(context),
        )
        if patch is not None:
            await self._emit_projection_patch(context, patch)

    async def _discard_projection(
        self,
        context: _MediaVoiceSession,
        reason: str,
    ) -> None:
        patch = context.projection.discard_provisional(None, reason)
        if patch is not None:
            await self._emit_projection_patch(context, patch)

    async def on_audio_frame(
        self,
        session: MediaBridgeSession,
        frame: AudioFrame,
    ) -> None:
        context = await self._get_or_create(session.identity)
        callback_stream_epoch = context.stream_epoch
        if (
            frame.identity.session_id != context.identity.session_id
            or frame.identity.stream_epoch != callback_stream_epoch
            or not self._stream_epoch_is_current(context, callback_stream_epoch)
        ):
            return
        if not context.asr.record_audio(
            start_sample=frame.capture_start_sample,
            frame_samples=frame.frame_samples,
        ):
            # Media Edge already performs the transport sample gate; this
            # second watermark protects the provider replay window if a
            # callback is duplicated or reordered before it reaches Core.
            return
        try:
            results = await context.provider.ingest_audio(context.identity, frame)
        except Exception:
            self.metrics.inc_media_session_failed()
            raise
        # Provider callbacks can outlive the media epoch that supplied their
        # frame.  Do not let a late result update speaker evidence, shadow
        # state, or the authoritative timeline after reconnect.
        if not self._stream_epoch_is_current(context, callback_stream_epoch):
            return
        context.runtime.feed_speaker_pcm(frame.payload)
        provider_task_epoch = getattr(context.provider, "current_asr_task_epoch", 0)
        if provider_task_epoch:
            if isinstance(provider_task_epoch, bool) or not isinstance(provider_task_epoch, int):
                raise RuntimeError("media provider returned an invalid ASR task epoch")
            previous_task_epoch = context.asr.latest_authoritative_task_epoch
            if not context.asr.observe_task(provider_task_epoch):
                raise RuntimeError("media provider ASR task epoch moved backwards")
            if provider_task_epoch > previous_task_epoch:
                await self.bridge.emit_speech_task_started(
                    context.identity.session_id,
                    provider_task_epoch,
                    context.runtime.speech_timeline,
                )
        for result in results:
            if not self._stream_epoch_is_current(context, callback_stream_epoch):
                return
            decision = await self._accept_asr_result_decision(context.identity.session_id, result)
            if not self._stream_epoch_is_current(context, callback_stream_epoch):
                return
            shadow_result = decision.accepted or result
            await self.bridge.emit_speech_segment_decision(
                context.identity.session_id,
                asr_result_to_segment(shadow_result, session_id=context.identity.session_id),
                authoritative_accepted=decision.accepted is not None,
                authoritative_reason=decision.reason.value,
                timeline=context.runtime.speech_timeline,
                latest_task_epoch=context.asr.latest_authoritative_task_epoch,
            )
            accepted = decision.accepted
            if accepted is not None and accepted.is_final:
                self._observe_final_asr_result(context, accepted)

    def _observe_final_asr_result(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
    ) -> None:
        """Buffer a provider final until the sample-clock endpoint is stable."""

        key = (
            result.stream_epoch,
            result.sentence_id,
            result.capture_start_sample,
            result.capture_end_sample,
        )
        if key in context.committed_asr_keys:
            return
        context.committed_asr_keys.add(key)
        if len(context.committed_asr_keys) > 256:
            # Keep the fence/idempotency memory bounded across long sessions.
            context.committed_asr_keys = set(list(context.committed_asr_keys)[-128:])
        context.turn_start_sample = min(
            result.capture_start_sample,
            context.turn_start_sample
            if context.turn_start_sample is not None
            else result.capture_start_sample,
        )
        context.turn_end_sample = max(result.capture_end_sample, context.turn_end_sample or 0)
        # A provider final is evidence, never the endpoint itself. If VAD has
        # already ended, a late final re-arms the same logical-turn commit.
        if context.turn_endpoint_sample is not None:
            self._schedule_turn_commit(context)

    def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None:
        task = context.turn_endpoint_task
        if task is not None and not task.done():
            task.cancel()
        endpoint_sample = context.turn_endpoint_sample
        if endpoint_sample is None:
            return
        context.turn_endpoint_task = asyncio.create_task(
            self._commit_pending_turn_after_grace(
                context.identity.session_id,
                context.stream_epoch,
                endpoint_sample,
            ),
            name=f"media-turn-endpoint-{context.identity.session_id}-{endpoint_sample}",
        )

    async def _commit_pending_turn_after_grace(
        self,
        session_id: str,
        stream_epoch: int,
        endpoint_sample: int,
    ) -> None:
        try:
            if self.turn_endpoint_grace_s:
                await asyncio.sleep(self.turn_endpoint_grace_s)
            context = self._sessions.get(session_id)
            current_endpoint = bool(
                context is not None
                and not context.closed
                and context.stream_epoch == stream_epoch
                and context.turn_endpoint_sample == endpoint_sample
            )
            if not current_endpoint or context is None:
                return
            if (
                context.turn_end_sample is None
                # A provider final is evidence, not an endpoint.  If ASR has
                # not covered the VAD end yet, leave the buffered turn open;
                # the late final will re-arm this same commit in
                # ``_observe_final_asr_result``.
                or context.turn_end_sample < endpoint_sample
            ):
                if context.runtime.assistant_speaking:
                    context.runtime.publish_assistant_audio("restore", gain=1.0)
                return
            await self._commit_pending_turn(context)
            if (
                context.turn_endpoint_sample == endpoint_sample
                and context.runtime.assistant_speaking
            ):
                context.runtime.publish_assistant_audio("restore", gain=1.0)
        except asyncio.CancelledError:
            return
        finally:
            context = self._sessions.get(session_id)
            if context is not None and context.turn_endpoint_task is asyncio.current_task():
                context.turn_endpoint_task = None

    async def _commit_pending_turn(self, context: _MediaVoiceSession) -> None:
        """Commit one VAD/ASR-coordinated logical turn through UtteranceRouter."""

        start_sample = context.turn_start_sample
        end_sample = context.turn_end_sample
        endpoint_sample = context.turn_endpoint_sample
        retire_sample = context.turn_retire_sample
        if (
            start_sample is None
            or end_sample is None
            or endpoint_sample is None
            or retire_sample is None
            or endpoint_sample <= start_sample
            or end_sample <= start_sample
            or retire_sample < endpoint_sample
        ):
            return
        previous_fence = context.playback.current_fence or context.runtime.fence
        fence, reason = await self.commit_user_turn(
            context.identity.session_id,
            stream_epoch=context.stream_epoch,
            start_sample=start_sample,
            end_sample=endpoint_sample,
            retire_sample=retire_sample,
        )
        # The range was consumed even when Router turns it into a low-risk
        # control action rather than a chat generation.
        if reason != "empty_media_turn":
            context.turn_start_sample = None
            context.turn_end_sample = None
            context.turn_endpoint_sample = None
            context.turn_retire_sample = None
            context.committed_asr_keys.clear()
        if fence is None:
            # Pure control/enrol/guarded utterances are intentionally not sent
            # to the LLM; ``accept_user_turn`` already routed those centrally.
            if reason not in {"empty_media_turn", "session_not_found"}:
                logger.info(
                    "media final did not start reply session=%s reason=%s",
                    context.identity.session_id,
                    reason,
                )
            return
        # A final ASR result can arrive while the previous answer is still
        # synthesizing.  Wait for that task to release the per-session reply
        # lock before scheduling the new turn; otherwise ``generate_reply``
        # sees a locked session and silently drops a valid user turn.
        await self._cancel_reply_task(context, previous_fence)
        task = asyncio.create_task(
            self.generate_reply(
                context.identity.session_id,
                next(
                    (
                        turn.content
                        for turn in reversed(context.runtime.orchestrator.context.turns)
                        if turn.role == "user" and turn.content
                    ),
                    "",
                ),
                fence,
            ),
            name=f"media-reply-{context.identity.session_id}-{fence.turn_id}",
        )

        def _observe(done: asyncio.Task[bool]) -> None:
            if done.cancelled():
                return
            try:
                done.result()
            except Exception:
                logger.exception(
                    "media reply failed session=%s fence=%s",
                    context.identity.session_id,
                    fence,
                )

        task.add_done_callback(_observe)

    async def on_speech_segment(
        self,
        session: MediaBridgeSession,
        segment: SpeechSegment,
        detected_monotonic_ms: int = 0,
    ) -> None:
        context = await self._get_or_create(session.identity)
        if not context.runtime.ingest_media_speech_segment(segment):
            return
        if segment.kind is SegmentKind.VAD:
            await self._apply_projection_segment(context, segment)
            if segment.final:
                voiced_end_sample = (
                    segment.voiced_end_sample
                    if segment.voiced_end_sample is not None
                    else segment.capture_start_sample
                )
                # Late/replayed VAD finals may arrive out of callback order.
                # Never move a pending endpoint backwards, or an older tail
                # event could truncate the logical turn before ASR coverage.
                context.turn_endpoint_sample = max(
                    context.turn_endpoint_sample or 0,
                    voiced_end_sample,
                )
                context.turn_retire_sample = max(
                    context.turn_retire_sample or 0,
                    segment.capture_start_sample,
                )
                if context.turn_start_sample is None:
                    context.turn_start_sample = min(
                        segment.capture_start_sample,
                        context.turn_end_sample
                        if context.turn_end_sample is not None
                        else segment.capture_start_sample,
                    )
                self._schedule_turn_commit(context)
            else:
                if context.turn_start_sample is None:
                    # One accepted range-stamped start opens the Runtime's
                    # speaker fence. Resumed VAD segments keep the same fence;
                    # sample ranges still decide the eventual turn boundary.
                    context.runtime.on_user_voice_started()
                interaction = context.runtime.decide_interaction(
                    InteractionSnapshot(
                        event=InteractionEvent.VAD_START,
                        assistant_speaking=context.runtime.assistant_speaking,
                        has_speech_energy=True,
                    )
                )
                if interaction.duck_output:
                    context.runtime.publish_assistant_audio("duck", gain=0.0)
                context.runtime.apply_interaction_decision(interaction)
                task = context.turn_endpoint_task
                if task is not None and not task.done():
                    task.cancel()
                context.turn_endpoint_sample = None
                context.turn_retire_sample = None
                context.turn_start_sample = min(
                    segment.capture_start_sample,
                    context.turn_start_sample
                    if context.turn_start_sample is not None
                    else segment.capture_start_sample,
                )
        # Sample ranges remain authoritative for media turn boundaries.
        if segment.kind is SegmentKind.KWS and segment.final:
            route = context.runtime.route_user_turn(segment.text)
            interaction = context.runtime.decide_interaction(
                InteractionSnapshot(
                    event=InteractionEvent.KEYWORD,
                    assistant_speaking=context.runtime.assistant_speaking,
                    text=segment.text,
                    utterance_route=route,
                    keyword_hard_stop=segment.hard_stop,
                    keyword_confidence=segment.confidence or 0.0,
                )
            )
            if interaction.cancel_generation:
                # Never subtract an Edge wall-clock timestamp from Core's
                # monotonic clock. The only trustworthy local measurement is
                # this handler's own work; end-to-end timing is fail-closed
                # until distributed tracing is available.
                _ = detected_monotonic_ms
                stop_started_ns = time.monotonic_ns()
                previous_fence = context.playback.current_fence or context.runtime.fence
                cancelled = (
                    session.fence
                    if not session.fence.matches(previous_fence)
                    else session.generation.cancel(previous_fence)
                )
                if cancelled is not None:
                    if not previous_fence.matches(
                        cancelled
                    ) and context.runtime.orchestrator.state.name in (
                        "SPEAKING",
                        "INTERRUPTION_PENDING",
                    ):
                        await self._record_interrupted_timed_spans(context, previous_fence)
                        heard = context.playback.actual_heard_text(previous_fence)
                        interrupted_fence = await context.runtime.on_real_interrupt(
                            cause="media_keyword_interrupt",
                            create_user_turn=False,
                            synchronized_transcript=heard,
                            force_generation_bump=True,
                        )
                        if not interrupted_fence.matches(cancelled):
                            raise ValueError(
                                "Voice Core keyword stop generation diverged from Media Edge"
                            )
                        await context.runtime.on_media_playback_interrupted(
                            interrupted_from=previous_fence,
                            synchronized_transcript=heard,
                        )
                    accepted = await context.runtime.accept_media_generation(
                        cancelled,
                        cause="media_keyword_interrupt",
                    )
                    if accepted:
                        context.playback.start(cancelled)
                        context.provider_complete = False
                        context.output_complete_emitted = False
                        await self._cancel_reply_task(context, previous_fence)
                        task_epoch, context_version = self._event_versions(context, cancelled)
                        await self.bridge.emit_realtime_effect(
                            context.identity.session_id,
                            media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
                            cancelled,
                            source_event_id="keyword_interrupt",
                            payload={"reason": "keyword_interrupt"},
                            task_epoch=task_epoch,
                            context_version=context_version,
                        )
                        self.metrics.observe_voice_latency(
                            "interrupt_core_stop",
                            (time.monotonic_ns() - stop_started_ns) / 1_000_000_000,
                        )
            fence = context.runtime.fence
            task_epoch, context_version = self._event_versions(context, fence)
            await self.bridge.emit_event(
                context.identity.session_id,
                "keyword.hit",
                {
                    "keyword": segment.text,
                    "confidence": segment.confidence,
                    "hard_stop": segment.hard_stop,
                    "start_sample": segment.capture_start_sample,
                    "end_sample": segment.capture_end_sample,
                },
                turn_id=fence.turn_id,
                generation_id=fence.generation_id,
                tool_epoch=fence.tool_epoch,
                task_epoch=task_epoch,
                context_version=context_version,
            )

    async def on_client_event(
        self,
        session: MediaBridgeSession,
        event: MediaEnvelope,
        detected_monotonic_ms: int = 0,
    ) -> None:
        context = await self._get_or_create(session.identity)
        if event.type != "client.stop_assistant":
            return
        # The edge timestamp is wall-clock time on another host. It is useful
        # trace metadata, but never a subtraction operand in this process.
        # This metric therefore measures only Core-side stop handling; the
        # end-to-end SLO stays unavailable until trace/clock synchronization is
        # deployed and remains fail-closed in the rollout gate.
        _ = detected_monotonic_ms
        stop_started_ns = time.monotonic_ns()
        # MediaBridgeSession has already advanced its authoritative generation
        # before this callback runs. The Voice Core consumes that exact fence.
        previous_fence = context.playback.current_fence or context.runtime.fence
        if not previous_fence.matches(
            session.fence
        ) and context.runtime.orchestrator.state.name in ("SPEAKING", "INTERRUPTION_PENDING"):
            await self._record_interrupted_timed_spans(context, previous_fence)
            heard = context.playback.actual_heard_text(previous_fence)
            interrupted_fence = await context.runtime.on_real_interrupt(
                cause="client_stop_assistant",
                create_user_turn=False,
                synchronized_transcript=heard,
                force_generation_bump=True,
            )
            if not interrupted_fence.matches(session.fence):
                raise ValueError("Voice Core stop generation diverged from Media Edge")
            await context.runtime.on_media_playback_interrupted(
                interrupted_from=previous_fence,
                synchronized_transcript=heard,
            )
        accepted = await context.runtime.accept_media_generation(
            session.fence,
            cause="client_stop_assistant",
        )
        if not accepted:
            raise ValueError("Voice Core rejected authoritative stop generation")
        context.playback.start(session.fence)
        context.provider_complete = False
        context.output_complete_emitted = False
        if not previous_fence.matches(session.fence):
            await self._cancel_reply_task(context, previous_fence)
        self.metrics.observe_voice_latency(
            "interrupt_core_stop",
            (time.monotonic_ns() - stop_started_ns) / 1_000_000_000,
        )

    async def on_downlink_overflow(self, session: MediaBridgeSession) -> None:
        """Cancel the authoritative runtime when transport delivery is lost."""

        context = self._sessions.get(session.identity.session_id)
        if context is None or context.closed:
            return
        previous_fence = context.playback.current_fence or context.runtime.fence
        cancelled = session.fence
        if previous_fence.matches(cancelled):
            return
        if context.runtime.orchestrator.state.name in ("SPEAKING", "INTERRUPTION_PENDING"):
            await self._record_interrupted_timed_spans(context, previous_fence)
            heard = context.playback.actual_heard_text(previous_fence)
            interrupted_fence = await context.runtime.on_real_interrupt(
                cause="downlink_queue_full",
                create_user_turn=False,
                synchronized_transcript=heard,
                force_generation_bump=True,
            )
            if not interrupted_fence.matches(cancelled):
                raise ValueError("Voice Core overflow generation diverged from Media Edge")
            await context.runtime.on_media_playback_interrupted(
                interrupted_from=previous_fence,
                synchronized_transcript=heard,
            )
        if not await context.runtime.accept_media_generation(
            cancelled,
            cause="downlink_queue_full",
        ):
            raise ValueError("Voice Core rejected overflow cancellation")
        context.playback.start(cancelled)
        context.provider_complete = False
        context.output_complete_emitted = False
        await self._cancel_reply_task(context, previous_fence)

    @staticmethod
    async def _cancel_provider_generation(
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> None:
        """Propagate a transport cancel into adapters that support it.

        ``MediaVoiceProvider`` stays provider-neutral, but the existing
        adapter exposes a cooperative cancellation hook. The registry revokes
        the local lease and task first; this hook is best-effort remote cleanup
        and must not delay local ownership transfer.
        """

        cancel = getattr(context.provider, "cancel_generation", None)
        if not callable(cancel):
            cancel = getattr(context.provider, "cancel", None)
        if not callable(cancel):
            return
        result = cancel(fence)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _release_output_owner(
        context: _MediaVoiceSession,
        fence: GenerationFence,
        *,
        reason: str,
    ) -> bool:
        lease = context.output_owner
        if lease is None or not lease.fence.matches(fence):
            return False
        context.output_owner = None
        context.provider_complete = False
        context.output_work.pop(str(lease.intent.intent_id), None)
        coordinator = context.runtime.orchestrator.delegation
        coordinator.complete_output_intent(
            lease.intent,
            current_fence=context.runtime.fence,
            current_context_version=coordinator.current_context_version(fence.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
            reason=reason,
        )
        return True

    @staticmethod
    def _output_owner_is_current(
        context: _MediaVoiceSession,
        lease: _OutputOwnerLease,
    ) -> bool:
        if (
            context.output_owner is not lease
            or lease.task is not asyncio.current_task()
            or not context.runtime.fence.matches(lease.fence)
        ):
            return False
        coordinator = context.runtime.orchestrator.delegation
        return coordinator.output_intent_is_selected(
            lease.intent,
            current_fence=context.runtime.fence,
            current_context_version=coordinator.current_context_version(lease.fence.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
        )

    @staticmethod
    def _acquire_output_owner(
        context: _MediaVoiceSession,
        fence: GenerationFence,
        intent: Any | None = None,
    ) -> _OutputOwnerLease | None:
        if context.output_owner is not None:
            return None
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - every async call has a task
            return None
        coordinator = context.runtime.orchestrator.delegation
        context_version = context.runtime.orchestrator.context_version_for_fence(fence)
        now_ms = int(time.time() * 1_000)
        if intent is None:
            intent = coordinator.conversation_reply(
                fence=fence,
                context_version=context_version,
                expires_at_ms=now_ms + _CONVERSATION_REPLY_TTL_MS,
                now_ms=now_ms,
            )
            coordinator.admit_output_intent(
                intent,
                current_fence=context.runtime.fence,
                current_context_version=coordinator.current_context_version(fence.session_id),
                floor_allows_output=context.runtime.output_floor_allows_assistant,
                now_ms=now_ms,
            )
        if not coordinator.output_intent_is_selected(
            intent,
            current_fence=context.runtime.fence,
            current_context_version=coordinator.current_context_version(fence.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
            now_ms=now_ms,
        ):
            return None
        lease = _OutputOwnerLease(intent=intent, fence=fence, task=task)
        context.output_owner = lease
        return lease

    @staticmethod
    async def _record_interrupted_timed_spans(
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> None:
        """Attach a provider's already-verified subtitle prefix before cancel.

        A live stream can expose timed subtitle facts before it reaches its
        normal completion.  This preserves only spans whose audio is already
        known to the provider adapter; absent or malformed metadata remains a
        deliberate no-op.
        """

        getter = getattr(context.provider, "interrupted_timed_text_spans", None)
        if not callable(getter):
            return
        spans = getter(fence)
        if inspect.isawaitable(spans):
            spans = await spans
        if not isinstance(spans, (tuple, list)):
            return
        for span in spans:
            text = getattr(span, "text", None)
            start = getattr(span, "audio_start_sample", None)
            end = getattr(span, "audio_end_sample", None)
            if (
                not isinstance(text, str)
                or not text
                or not isinstance(start, int)
                or not isinstance(end, int)
            ):
                return
            text_start = context.output_text_offset
            context.output_text_offset += len(text)
            context.playback.add_span(
                PlaybackSpan(
                    fence=fence,
                    text_start=text_start,
                    text_end=context.output_text_offset,
                    audio_start_sample=start,
                    audio_end_sample=end,
                    text=text,
                )
            )

    @classmethod
    async def _cancel_reply_task(
        cls,
        context: _MediaVoiceSession,
        fence: GenerationFence,
        *,
        reason: str = "cancelled",
    ) -> None:
        """Cancel provider work and drain the old reply task before reuse."""

        task = context.reply_task
        cls._release_output_owner(context, fence, reason=reason)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        else:
            task = None
        # A provider can own a remote stream after its local task has already
        # completed. Transport cancellation must still reach that provider.
        try:
            await cls._cancel_provider_generation(context, fence)
        except Exception:
            logger.exception("media provider cancellation failed")
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("media reply cancellation observed a failed task")

    async def on_session_closed(self, session: MediaBridgeSession) -> None:
        session_id = session.identity.session_id
        if session.state == "closed":
            await self._finalize_session(session_id)
            return
        # A gRPC stream closing is normally a transport reconnect, not a
        # conversation close. Keep the runtime/provider alive briefly so a
        # higher stream epoch can reclaim the same context without losing the
        # turn, pending reply, or generation fence.
        previous = self._cleanup_tasks.pop(session_id, None)
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(
            self._expire_disconnected_session(session_id, session.identity.stream_epoch),
            name=f"media-reconnect-grace-{session_id}",
        )
        self._cleanup_tasks[session_id] = task

    async def _expire_disconnected_session(self, session_id: str, stream_epoch: int) -> None:
        try:
            await asyncio.sleep(self.reconnect_grace_s)
            current = self._sessions.get(session_id)
            bridge_session = self.bridge.bridge.get(session_id)
            if current is None or current.closed or current.identity.stream_epoch != stream_epoch:
                return
            # A replacement gRPC stream claims the Edge session before the
            # first application event reaches this registry. Inspect that
            # authoritative epoch as well, otherwise the old grace task could
            # close the newly reconnected transport/context.
            if bridge_session is None or bridge_session.identity.stream_epoch != stream_epoch:
                return
            await self._finalize_session(session_id)
        except asyncio.CancelledError:
            return
        finally:
            if self._cleanup_tasks.get(session_id) is asyncio.current_task():
                self._cleanup_tasks.pop(session_id, None)

    async def _finalize_session(self, session_id: str) -> None:
        cleanup = self._cleanup_tasks.pop(session_id, None)
        current_task = asyncio.current_task()
        if cleanup is not None and cleanup is not current_task and not cleanup.done():
            cleanup.cancel()
        context = self._sessions.get(session_id)
        if context is None:
            return
        await context.turn_commit_lock.acquire()
        try:
            if self._sessions.get(session_id) is not context or context.closed:
                return
            self._sessions.pop(session_id, None)
            context_stream_epoch = context.stream_epoch
            context.closed = True
            context.projection.discard_provisional(None, "session_closed")
        finally:
            context.turn_commit_lock.release()
        self.metrics.set_media_active_sessions(len(self._sessions))
        output_fence = (
            context.output_owner.fence
            if context.output_owner is not None
            else context.playback.current_fence or context.runtime.fence
        )
        await self._cancel_reply_task(context, output_fence)
        context.runtime.orchestrator.delegation.set_output_intent_observer(None)
        if context.turn_endpoint_task is not None and not context.turn_endpoint_task.done():
            context.turn_endpoint_task.cancel()
        await context.runtime.close()
        await context.provider.close(context.identity)
        # The registry owns the transport session created by the bridge. Once
        # reconnect grace expires there is no replacement epoch left to claim
        # it, so remove it as well; otherwise stale identities accumulate in
        # ``MediaBridgeServer.sessions`` and can block a future open.
        self.bridge.bridge.close_if_epoch(session_id, context_stream_epoch)

    async def on_playback_progress(
        self,
        session: MediaBridgeSession,
        progress: PlaybackProgress,
    ) -> None:
        context = self._sessions.get(session.identity.session_id)
        if context is None or context.closed:
            return
        fence = GenerationFence(
            session_id=context.identity.session_id,
            turn_id=progress.turn_id,
            generation_id=progress.generation_id,
            tool_epoch=progress.tool_epoch,
        )
        acknowledged = context.playback.acknowledge(
            fence,
            progress.rendered_sample_end,
            received_sequence=progress.received_sequence,
            approximate=progress.approximate,
        )
        # Publish the cumulative acknowledged prefix under one turn/revision;
        # publishing only the newly acknowledged span would make clients
        # replace a complete answer with its last phrase.
        heard = context.playback.actual_heard_text(fence)
        if (
            context.provider_complete
            and context.playback.is_fully_acknowledged(fence)
            and context.runtime.fence.matches(fence)
        ):
            await self._finish_completed_output(context, fence)
        # An empty acknowledged tuple only means no new publishable text span;
        # it must not skip the playback-completion check above. Transcript
        # publication itself still requires a newly acknowledged span so a
        # duplicate ACK cannot re-emit the same text.
        if acknowledged and heard and context.runtime.fence.matches(fence):
            # Commit actual-heard history before publishing the final event;
            # consumers must never observe a "heard" transcript while the
            # authoritative runtime is still SPEAKING.
            context.runtime.publish_transcript(
                speaker="assistant",
                text=heard,
                final=True,
                heard=True,
                text_delivered=True,
                fence=fence,
            )

    async def accept_asr_result(self, session_id: str, result: ASRResult) -> bool:
        """Compatibility bool seam; use the normalized decision internally."""

        decision = await self._accept_asr_result_decision(session_id, result)
        return decision.accepted is not None

    async def _accept_asr_result_decision(
        self,
        session_id: str,
        result: ASRResult,
    ) -> ASRAcceptDecision:
        context = self._sessions.get(session_id)
        if context is None or context.closed:
            return ASRAcceptDecision(None, ASRDecisionReason.SESSION_NOT_FOUND)
        preview = context.asr.preview_result(result)
        candidate = preview.accepted
        if candidate is None:
            if result.is_final:
                self.metrics.inc_media_stale_asr_final()
            return preview
        candidate_segment = asr_result_to_segment(candidate, session_id=session_id)
        if not context.runtime.speech_timeline.can_add(candidate_segment):
            runtime_task_epoch = context.runtime.speech_timeline.latest_task_epoch(
                candidate.stream_epoch
            )
            if runtime_task_epoch > 0:
                context.asr.observe_task(runtime_task_epoch)
            return ASRAcceptDecision(None, ASRDecisionReason.INTERVAL_CONFLICT)
        decision = context.asr.accept_result(result, session_id=session_id)
        accepted = decision.accepted
        if accepted is None:
            if result.is_final:
                self.metrics.inc_media_stale_asr_final()
            return decision
        # The provider result is never forwarded after supervisor policy has
        # normalized it (e.g. a committed-watermark tail).
        segment = asr_result_to_segment(accepted, session_id=session_id)
        if not context.runtime.ingest_media_speech_segment(segment):
            raise RuntimeError("ASR runtime timeline changed during atomic acceptance")
        await self._apply_projection_segment(context, segment)
        task_epoch, context_version = self._event_versions(context, context.runtime.fence)
        await self.bridge.emit_transcript(
            session_id,
            segment,
            task_epoch=task_epoch,
            context_version=context_version,
        )
        return decision

    async def commit_user_turn(
        self,
        session_id: str,
        *,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
        retire_sample: int | None = None,
    ) -> tuple[GenerationFence | None, str | None]:
        """Commit one explicit sample range, then create its authoritative turn."""

        context = self._sessions.get(session_id)
        if context is None or context.closed:
            return None, "session_not_found"
        if start_sample < 0 or end_sample <= start_sample:
            return None, "invalid_media_range"
        async with context.turn_commit_lock:
            if not self._stream_epoch_is_current(context, stream_epoch):
                return None, "stale_stream_epoch"
            return await self._commit_user_turn_locked(
                context,
                session_id=session_id,
                stream_epoch=stream_epoch,
                start_sample=start_sample,
                end_sample=end_sample,
                retire_sample=retire_sample,
            )

    async def _commit_media_input_range(
        self,
        context: _MediaVoiceSession,
        *,
        session_id: str,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
        retire_end: int,
    ) -> None:
        context.runtime.commit_media_speech_range(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        if retire_end > end_sample:
            context.runtime.commit_media_speech_range(
                stream_epoch=stream_epoch,
                start_sample=end_sample,
                end_sample=retire_end,
            )
        context.asr.mark_committed(retire_end)
        await self.bridge.emit_speech_commit(
            session_id,
            retire_end,
            context.runtime.speech_timeline,
            latest_task_epoch=context.asr.latest_authoritative_task_epoch,
        )

    async def _commit_user_turn_locked(
        self,
        context: _MediaVoiceSession,
        *,
        session_id: str,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
        retire_sample: int | None,
    ) -> tuple[GenerationFence | None, str | None]:
        """Prepare and project one turn while its transport epoch is stable."""

        # Compatibility callers may have populated the authoritative Timeline
        # directly before invoking this seam. Re-project those already-
        # accepted facts rather than letting a valid turn bypass Projection.
        if context.projection.provisional is None:
            for segment in context.runtime.speech_timeline.segments_in_range(
                stream_epoch=stream_epoch,
                start_sample=start_sample,
                end_sample=end_sample,
            ):
                await self._apply_projection_segment(context, segment)
        text = context.runtime.project_media_user_turn(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        if not text:
            await self._commit_media_input_range(
                context,
                session_id=session_id,
                stream_epoch=stream_epoch,
                start_sample=start_sample,
                end_sample=end_sample,
                retire_end=end_sample,
            )
            await self._discard_projection(context, "empty_media_turn")
            return None, "empty_media_turn"
        retire_end = end_sample if retire_sample is None else retire_sample
        if retire_end < end_sample:
            raise ValueError("media retire sample cannot precede the logical endpoint")
        context.runtime.on_user_voice_stopped()
        await context.runtime.await_speaker_classification()
        interaction = context.runtime.decide_interaction(
            InteractionSnapshot(
                event=InteractionEvent.TRANSCRIPT,
                assistant_speaking=context.runtime.assistant_speaking,
                text=text,
                elapsed_ms=(end_sample - start_sample) * 1_000 // 16_000,
                final=True,
                has_speech_energy=True,
                guarded_reason=context.runtime.playback_guarded_reason(
                    text,
                    duration_ms=(end_sample - start_sample) * 1_000 // 16_000,
                ),
                semantic_evidence=True,
                utterance_route=context.runtime.route_user_turn(text),
            )
        )
        if interaction.backchannel:
            await self._commit_media_input_range(
                context,
                session_id=session_id,
                stream_epoch=stream_epoch,
                start_sample=start_sample,
                end_sample=end_sample,
                retire_end=retire_end,
            )
            await self._discard_projection(context, interaction.reason)
            context.runtime.publish_assistant_audio("restore", gain=1.0)
            return None, interaction.reason
        if context.runtime.assistant_speaking and not interaction.cancel_generation:
            await self._commit_media_input_range(
                context,
                session_id=session_id,
                stream_epoch=stream_epoch,
                start_sample=start_sample,
                end_sample=end_sample,
                retire_end=retire_end,
            )
            await self._discard_projection(context, interaction.reason)
            context.runtime.publish_assistant_audio("restore", gain=1.0)
            return None, interaction.reason
        accepted, reason = context.runtime.accept_user_turn(
            text,
            input_modality="audio",
            speech_anchored=True,
            canonical_speech_epoch=context.runtime.consumed_canonical_speech_epoch,
            canonical_snapshot_bound=True,
        )
        if not accepted:
            await self._commit_media_input_range(
                context,
                session_id=session_id,
                stream_epoch=stream_epoch,
                start_sample=start_sample,
                end_sample=end_sample,
                retire_end=retire_end,
            )
            await self._discard_projection(context, reason or "user_turn_rejected")
            if context.runtime.assistant_speaking:
                context.runtime.publish_assistant_audio("restore", gain=1.0)
            return None, reason or "user_turn_rejected"
        if context.runtime.assistant_speaking:
            context.runtime.publish_assistant_audio("restore", gain=1.0)
        speaker_evidence = self._projection_speaker_evidence(context)
        speaker_patch = context.projection.apply_speaker_evidence(speaker_evidence)
        if speaker_patch is not None:
            await self._emit_projection_patch(context, speaker_patch)
        history_eligible = context.runtime.current_history_eligible
        before_prepare_fence = context.runtime.fence
        prepare_turn = getattr(context.provider, "prepare_committed_turn", None)
        try:
            if callable(prepare_turn) and bool(
                getattr(context.provider, "supports_turn_preparation", True)
            ):
                prepared = prepare_turn(context.identity, text)
                fence = await prepared if inspect.isawaitable(prepared) else prepared
            else:
                fence = await context.runtime.on_turn_committed(
                    text,
                    input_modality="audio",
                )
            if not isinstance(fence, GenerationFence) or not context.runtime.fence.matches(fence):
                raise RuntimeError("media provider returned an invalid prepared turn fence")
        except asyncio.CancelledError:
            raise
        except Exception:
            prepared_fence = context.runtime.fence
            if not prepared_fence.matches(before_prepare_fence):
                if self._stream_epoch_is_current(context, stream_epoch):
                    await self._commit_media_input_range(
                        context,
                        session_id=session_id,
                        stream_epoch=stream_epoch,
                        start_sample=start_sample,
                        end_sample=end_sample,
                        retire_end=retire_end,
                    )
                    context_version = context.runtime.orchestrator.context_version_for_fence(
                        prepared_fence
                    )
                    recovered = context.projection.commit_turn(
                        CommitEvidence(
                            session_id=session_id,
                            stream_epoch=stream_epoch,
                            capture_start_sample=start_sample,
                            capture_end_sample=end_sample,
                            text=text,
                            fence=prepared_fence,
                            speaker_evidence=speaker_evidence,
                            history_eligible=history_eligible,
                            context_version=context_version,
                        )
                    )
                    if isinstance(recovered, CommittedTurn):
                        await self.bridge.emit_context_activated(session_id, context_version)
                        if self._stream_epoch_is_current(context, stream_epoch):
                            task_epoch, _ = self._event_versions(context, prepared_fence)
                            await self.bridge.emit_event(
                                session_id,
                                "turn.committed",
                                recovered.to_payload(),
                                turn_id=prepared_fence.turn_id,
                                generation_id=prepared_fence.generation_id,
                                tool_epoch=prepared_fence.tool_epoch,
                                task_epoch=task_epoch,
                                context_version=context_version,
                            )
                        context.runtime.publish_transcript(
                            speaker="user",
                            text=recovered.text,
                            final=True,
                            fence=prepared_fence,
                            turn_revision=recovered.revision,
                        )
                    else:
                        await self._discard_projection(context, recovered.value)
                await context.runtime.on_assistant_reply_aborted(
                    prepared_fence,
                    cause="media_turn_prepare_failed",
                )
            logger.warning(
                "media turn preparation failed session=%s stream_epoch=%s",
                session_id,
                stream_epoch,
                exc_info=True,
            )
            # Projection remains provisional so the client does not lose a
            # visible user turn merely because provider preparation failed.
            return None, "provider_prepare_failed"
        if not self._stream_epoch_is_current(context, stream_epoch):
            await context.runtime.on_assistant_reply_aborted(
                fence,
                cause="stale_media_stream_epoch",
            )
            return None, "stale_stream_epoch"
        await self._commit_media_input_range(
            context,
            session_id=session_id,
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
            retire_end=retire_end,
        )
        context_version = context.runtime.orchestrator.context_version_for_fence(fence)
        projection_result = context.projection.commit_turn(
            CommitEvidence(
                session_id=session_id,
                stream_epoch=stream_epoch,
                capture_start_sample=start_sample,
                capture_end_sample=end_sample,
                text=text,
                fence=fence,
                speaker_evidence=speaker_evidence,
                history_eligible=history_eligible,
                context_version=context_version,
            )
        )
        if isinstance(projection_result, ProjectionRejectReason):
            await context.runtime.on_assistant_reply_aborted(
                fence,
                cause="projection_commit_rejected",
            )
            await self._discard_projection(context, projection_result.value)
            return None, projection_result.value
        committed: CommittedTurn = projection_result
        if not self._stream_epoch_is_current(context, stream_epoch):
            await context.runtime.on_assistant_reply_aborted(
                fence,
                cause="stale_media_stream_epoch",
            )
            return None, "stale_stream_epoch"
        await self.bridge.emit_context_activated(session_id, context_version)
        if not self._stream_epoch_is_current(context, stream_epoch):
            await context.runtime.on_assistant_reply_aborted(
                fence,
                cause="stale_media_stream_epoch",
            )
            return None, "stale_stream_epoch"
        task_epoch, _ = self._event_versions(context, fence)
        await self.bridge.emit_event(
            session_id,
            "turn.committed",
            committed.to_payload(),
            turn_id=fence.turn_id,
            generation_id=fence.generation_id,
            tool_epoch=fence.tool_epoch,
            task_epoch=task_epoch,
            context_version=context_version,
        )
        if not self._stream_epoch_is_current(context, stream_epoch):
            await context.runtime.on_assistant_reply_aborted(
                fence,
                cause="stale_media_stream_epoch",
            )
            return None, "stale_stream_epoch"
        context.runtime.publish_transcript(
            speaker="user",
            text=committed.text,
            final=True,
            fence=fence,
            turn_revision=committed.revision,
        )
        context.playback.start(fence)
        context.output_sequence = 0
        context.output_text_offset = 0
        context.assistant_text = ""
        context.provider_complete = False
        context.output_complete_emitted = False
        context.turn_started_ns = time.monotonic_ns()
        context.first_audio_observed = False
        task_epoch, context_version = self._event_versions(context, fence)
        await self.bridge.emit_generation(
            session_id,
            fence,
            action=media_pb2.GENERATION_ACTION_START,
            reason="user_turn_committed",
            task_epoch=task_epoch,
            context_version=context_version,
        )
        return fence, None

    async def generate_reply(
        self,
        session_id: str,
        user_text: str,
        fence: GenerationFence,
    ) -> bool:
        """Submit the normal reply through the same owner as every source."""

        context = self._sessions.get(session_id)
        if context is None or context.closed or not context.runtime.fence.matches(fence):
            return False
        if (
            context.delegation_owns_realtime_output
            and requires_realtime_lookup(user_text)
        ):
            return True
        coordinator = context.runtime.orchestrator.delegation
        now_ms = int(time.time() * 1_000)
        intent = coordinator.conversation_reply(
            fence=fence,
            context_version=self._output_context_version(context, fence),
            expires_at_ms=now_ms + _CONVERSATION_REPLY_TTL_MS,
            now_ms=now_ms,
        )
        coordinator.admit_output_intent(
            intent,
            current_fence=context.runtime.fence,
            current_context_version=coordinator.current_context_version(fence.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
            now_ms=now_ms,
        )
        work = _OutputWork(intent, conversation_text=user_text)
        if not coordinator.output_intent_is_active(
            intent,
            current_fence=context.runtime.fence,
            current_context_version=coordinator.current_context_version(fence.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
            now_ms=now_ms,
        ):
            return False
        context.output_work[work.intent_id] = work
        if (
            context.output_owner is not None
            or context.reply_lock.locked()
            or context.runtime.orchestrator.state is ConversationState.LISTENING
        ):
            return await self._enqueue_output_work(context, work)
        return await self._run_output_work(context, work)

    async def _generate_reply(
        self,
        session_id: str,
        user_text: str,
        fence: GenerationFence,
    ) -> bool:
        # Compatibility seam retained for callers that exercised the old
        # private method directly.
        return await self.generate_reply(session_id, user_text, fence)

    async def _stream_output(
        self,
        context: _MediaVoiceSession,
        session_id: str,
        fence: GenerationFence,
        lease: _OutputOwnerLease,
        chunks: AsyncIterator[MediaReplyChunk],
    ) -> bool:
        """Send one selected source through the shared owner and PCM ledger."""

        emitted_audio = False
        try:
            async for chunk in chunks:
                if not self._output_owner_is_current(context, lease):
                    self.metrics.inc_media_stale_generation()
                    await self._cancel_reply_task(context, fence, reason="superseded")
                    return False
                announcement = (
                    chunk.text if chunk.assistant_text_delta is None else chunk.assistant_text_delta
                )
                if announcement:
                    context.assistant_text += announcement
                    # Keep the runtime's heard-text tracker aligned with the
                    # complete provider text, while the ledger still decides
                    # whether that text was actually rendered.
                    speaking_started = await context.runtime.on_assistant_speaking(
                        context.assistant_text,
                        expected_fence=fence,
                        precondition=lambda: self._output_owner_is_current(context, lease),
                    )
                    if not speaking_started or not self._output_owner_is_current(context, lease):
                        self.metrics.inc_media_stale_generation()
                        await self._cancel_reply_task(context, fence, reason="superseded")
                        return False
                    # ``assistant_text_delta`` is incremental at the provider
                    # boundary, but transcript consumers replace one fenced
                    # turn by revision. Publish the cumulative text so a
                    # second phrase cannot make the UI/history seam regress
                    # to only that phrase. This remains non-final until the
                    # playback ledger supplies an actual-heard watermark.
                    context.runtime.publish_transcript(
                        speaker="assistant",
                        text=context.assistant_text,
                        final=False,
                        text_delivered=True,
                        fence=fence,
                    )
                gated = context.runtime.gate_tts_audio(fence, chunk.pcm_s16le)
                if gated is None:
                    self.metrics.inc_media_stale_generation()
                    await self._cancel_reply_task(context, fence, reason="stale_generation")
                    return False
                task_epoch, context_version = self._event_versions(context, fence)
                frame = PCMFrame(
                    identity=context.identity,
                    turn_id=fence.turn_id,
                    generation_id=fence.generation_id,
                    tool_epoch=fence.tool_epoch,
                    sequence=context.output_sequence,
                    source_start_sample=chunk.source_start_sample,
                    frame_samples=len(gated) // 2,
                    pcm_s16le=gated,
                    first=chunk.first,
                    final=chunk.final,
                    task_epoch=task_epoch,
                    context_version=context_version,
                )
                if not await self.bridge.emit_pcm(session_id, frame):
                    self.metrics.inc_media_stale_generation()
                    await self._cancel_reply_task(context, fence, reason="transport_rejected")
                    return False
                if not context.playback.register_audio(
                    fence,
                    frame.sequence,
                    frame.source_start_sample,
                    frame.frame_samples,
                ):
                    self.metrics.inc_media_stale_generation()
                    await self._cancel_reply_task(context, fence, reason="playback_rejected")
                    return False
                emitted_audio = True
                if not context.first_audio_observed and context.turn_started_ns is not None:
                    self.metrics.observe_voice_latency(
                        "first_audio",
                        (time.monotonic_ns() - context.turn_started_ns) / 1_000_000_000,
                    )
                    context.first_audio_observed = True
                context.output_sequence += 1
                if chunk.text:
                    text_start = context.output_text_offset
                    context.output_text_offset += len(chunk.text)
                    context.playback.add_span(
                        PlaybackSpan(
                            fence=fence,
                            text_start=text_start,
                            text_end=context.output_text_offset,
                            audio_start_sample=(
                                chunk.text_audio_start_sample
                                if chunk.text_audio_start_sample is not None
                                else chunk.source_start_sample
                            ),
                            audio_end_sample=(
                                chunk.text_audio_end_sample
                                if chunk.text_audio_end_sample is not None
                                else chunk.source_start_sample + len(gated) // 2
                            ),
                            text=chunk.text,
                        )
                    )
                for span in chunk.text_spans:
                    text_start = context.output_text_offset
                    context.output_text_offset += len(span.text)
                    context.playback.add_span(
                        PlaybackSpan(
                            fence=fence,
                            text_start=text_start,
                            text_end=context.output_text_offset,
                            audio_start_sample=span.audio_start_sample,
                            audio_end_sample=span.audio_end_sample,
                            text=span.text,
                        )
                    )
        except asyncio.CancelledError:
            self._release_output_owner(context, fence, reason="cancelled")
            raise
        except Exception:
            self.metrics.inc_media_session_failed()
            await self._cancel_reply_task(context, fence, reason="provider_failed")
            raise
        if emitted_audio and context.runtime.fence.matches(fence):
            # Provider completion is not playback completion.  Keep the
            # runtime speaking until a client PlaybackProgress watermark
            # covers all emitted audio and every mapped text span; otherwise
            # interrupted/undelivered text could enter history as if heard.
            context.provider_complete = True
            if not context.output_complete_emitted:
                task_epoch, context_version = self._event_versions(context, fence)
                context.output_complete_emitted = await self.bridge.emit_generation(
                    fence.session_id,
                    fence,
                    action=media_pb2.GENERATION_ACTION_COMPLETE,
                    reason="provider_reply_complete",
                    task_epoch=task_epoch,
                    context_version=context_version,
                )
            # A very fast client may acknowledge the last frame before the
            # provider iterator yields its completion. Re-check the ledger at
            # provider completion so the runtime cannot remain SPEAKING until
            # a second, unnecessary ACK arrives.
            if context.playback.is_fully_acknowledged(fence):
                await self._finish_completed_output(context, fence)
        else:
            self._release_output_owner(context, fence, reason="provider_completed_without_audio")
            if not await self._start_selected_output(context):
                await context.runtime.on_assistant_reply_aborted(
                    fence,
                    cause="provider_completed_without_audio",
                )
        return True

    @staticmethod
    def _output_context_version(
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> int:
        return context.runtime.orchestrator.context_version_for_fence(fence)

    @staticmethod
    def _output_work_is_active(
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> bool:
        fence = work.fence
        coordinator = context.runtime.orchestrator.delegation
        return bool(
            context.runtime.fence.matches(fence)
            and coordinator.output_intent_is_active(
                work.intent,
                current_fence=context.runtime.fence,
                current_context_version=coordinator.current_context_version(fence.session_id),
                floor_allows_output=context.runtime.output_floor_allows_assistant,
            )
        )

    @staticmethod
    def _output_work_is_current(
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> bool:
        fence = work.fence
        coordinator = context.runtime.orchestrator.delegation
        return bool(
            context.runtime.fence.matches(fence)
            and coordinator.output_intent_is_selected(
                work.intent,
                current_fence=context.runtime.fence,
                current_context_version=coordinator.current_context_version(fence.session_id),
                floor_allows_output=context.runtime.output_floor_allows_assistant,
            )
        )

    @staticmethod
    def _rebind_output_work(
        work: _OutputWork,
        fence: GenerationFence,
        *,
        context_version: int,
    ) -> _OutputWork | None:
        now_ms = int(time.time() * 1_000)
        if int(work.intent.expires_at_ms) <= now_ms:
            return None
        rebound = media_pb2.OutputIntent()
        rebound.CopyFrom(work.intent)
        rebound.intent_id = str(uuid4())
        rebound.session_id = fence.session_id
        rebound.turn_id = fence.turn_id
        rebound.generation_id = fence.generation_id
        rebound.tool_epoch = fence.tool_epoch
        rebound.created_at_ms = now_ms
        rebound.context_version = context_version
        return _OutputWork(rebound, conversation_text=work.conversation_text)

    async def _enqueue_output_work(
        self,
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> bool:
        """Retain an admitted source and start it only when it owns playback."""

        coordinator = context.runtime.orchestrator.delegation
        if int(work.intent.kind) not in _STREAMCORE_EXECUTABLE_OUTPUT_KINDS:
            context.output_work.pop(work.intent_id, None)
            coordinator.complete_output_intent(
                work.intent,
                current_fence=context.runtime.fence,
                current_context_version=coordinator.current_context_version(
                    work.fence.session_id
                ),
                floor_allows_output=context.runtime.output_floor_allows_assistant,
                reason="unsupported_streamcore_output_kind",
            )
            return False
        if context.closed or not self._output_work_is_active(context, work):
            return False
        context.output_work[work.intent_id] = work
        owner = context.output_owner
        if owner is not None:
            if (
                str(owner.intent.intent_id) != work.intent_id
                and self._output_work_is_current(context, work)
            ):
                return await self._preempt_output_owner(context, work)
            return True
        return await self._start_selected_output(context)

    async def _start_selected_output(self, context: _MediaVoiceSession) -> bool:
        """Start the admitted winner, or discard an unbound candidate safely."""

        if context.closed or context.output_owner is not None:
            return False
        pending = context.output_dispatch_task
        if pending is not None and not pending.done():
            return True
        coordinator = context.runtime.orchestrator.delegation
        session_id = context.identity.session_id
        while True:
            fence = context.runtime.fence
            candidate = coordinator.current_output_intent(
                session_id,
                current_fence=fence,
                current_context_version=coordinator.current_context_version(session_id),
                floor_allows_output=context.runtime.output_floor_allows_assistant,
            )
            if candidate is None:
                return False
            work = context.output_work.get(str(candidate.intent_id))
            if work is None:
                coordinator.complete_output_intent(
                    candidate,
                    current_fence=fence,
                    current_context_version=coordinator.current_context_version(session_id),
                    floor_allows_output=context.runtime.output_floor_allows_assistant,
                    reason="missing_output_work",
                )
                continue
            if context.runtime.orchestrator.state is ConversationState.LISTENING:
                work = await self._promote_auxiliary_output(context, work)
                if work is None:
                    return False
                continue
            task = asyncio.create_task(
                self._run_output_work(context, work),
                name=f"media-output-{session_id}-{work.intent_id}",
            )
            context.output_dispatch_task = task

            def clear_dispatch(done: asyncio.Task[bool]) -> None:
                if context.output_dispatch_task is done:
                    context.output_dispatch_task = None

            task.add_done_callback(clear_dispatch)
            return True

    async def _promote_auxiliary_output(
        self,
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> _OutputWork | None:
        """Move a late selected source to a new audible generation."""

        old_fence = work.fence
        next_fence = await context.runtime.begin_media_auxiliary_output(old_fence)
        if next_fence is None:
            return None
        coordinator = context.runtime.orchestrator.delegation
        rebound = self._rebind_output_work(
            work,
            next_fence,
            context_version=coordinator.current_context_version(next_fence.session_id),
        )
        if rebound is None:
            return None
        coordinator.reset_output_intent_state(next_fence.session_id)
        context.output_work.clear()
        coordinator.admit_output_intent(
            rebound.intent,
            current_fence=next_fence,
            current_context_version=coordinator.current_context_version(next_fence.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
        )
        if not self._output_work_is_current(context, rebound):
            return None
        context.output_work[rebound.intent_id] = rebound
        context.playback.start(next_fence)
        context.output_sequence = 0
        context.output_text_offset = 0
        context.assistant_text = ""
        context.provider_complete = False
        context.output_complete_emitted = False
        task_epoch, context_version = self._event_versions(context, next_fence)
        if not await self.bridge.emit_generation(
            next_fence.session_id,
            next_fence,
            action=media_pb2.GENERATION_ACTION_START,
            reason="auxiliary_output",
            task_epoch=task_epoch,
            context_version=context_version,
        ):
            coordinator.complete_output_intent(
                rebound.intent,
                current_fence=next_fence,
                current_context_version=context_version,
                floor_allows_output=context.runtime.output_floor_allows_assistant,
                reason="generation_start_rejected",
            )
            context.output_work.pop(rebound.intent_id, None)
            return None
        return rebound

    async def _preempt_output_owner(
        self,
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> bool:
        """Flush a lower-priority owner before starting the selected source."""

        owner = context.output_owner
        if owner is None or not context.runtime.fence.matches(owner.fence):
            return False
        old_fence = owner.fence
        heard = context.playback.actual_heard_text(old_fence)
        await self._cancel_reply_task(context, old_fence, reason="preempted")
        cancelled = await context.runtime.preempt_media_output(
            cause="output_preempted",
            synchronized_transcript=heard,
        )
        if cancelled.matches(old_fence):
            return False
        await context.runtime.on_media_playback_interrupted(
            interrupted_from=old_fence,
            synchronized_transcript=heard,
        )
        context.playback.discard(old_fence)
        task_epoch, context_version = self._event_versions(context, cancelled)
        if not await self.bridge.emit_realtime_effect(
            cancelled.session_id,
            media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
            cancelled,
            source_event_id="output_preempted",
            payload={"reason": "output_preempted"},
            task_epoch=task_epoch,
            context_version=context_version,
        ):
            return False
        coordinator = context.runtime.orchestrator.delegation
        context.runtime.set_interaction_phase(
            InteractionPhase.THINKING_SILENT,
            cause="media_output_preempt",
        )
        staged = self._rebind_output_work(
            work,
            cancelled,
            context_version=coordinator.current_context_version(cancelled.session_id),
        )
        if staged is None:
            return False
        coordinator.reset_output_intent_state(cancelled.session_id)
        context.output_work.clear()
        coordinator.admit_output_intent(
            staged.intent,
            current_fence=cancelled,
            current_context_version=coordinator.current_context_version(cancelled.session_id),
            floor_allows_output=context.runtime.output_floor_allows_assistant,
        )
        if not self._output_work_is_current(context, staged):
            return False
        context.output_work[staged.intent_id] = staged
        rebound = await self._promote_auxiliary_output(context, staged)
        if rebound is None:
            return False
        return await self._start_selected_output(context)

    async def _run_output_work(
        self,
        context: _MediaVoiceSession,
        work: _OutputWork,
    ) -> bool:
        fence = work.fence
        async with context.reply_lock:
            if context.closed or not self._output_work_is_current(context, work):
                return False
            lease = self._acquire_output_owner(context, fence, work.intent)
            if lease is None:
                return False
            context.output_complete_emitted = False
            task = asyncio.current_task()
            if task is not None:
                context.reply_task = task
            try:
                source_start_sample = context.playback.renderable_sample_end(fence)
                return await self._stream_output(
                    context,
                    fence.session_id,
                    fence,
                    lease,
                    self._output_chunks(context, work, source_start_sample),
                )
            finally:
                if context.reply_task is task:
                    context.reply_task = None

    async def _output_chunks(
        self,
        context: _MediaVoiceSession,
        work: _OutputWork,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        if work.conversation_text is not None:
            async for chunk in context.provider.generate_reply(
                context.identity,
                work.conversation_text,
                work.fence,
            ):
                if source_start_sample:
                    yield replace(
                        chunk,
                        source_start_sample=chunk.source_start_sample + source_start_sample,
                        text_audio_start_sample=(
                            None
                            if chunk.text_audio_start_sample is None
                            else chunk.text_audio_start_sample + source_start_sample
                        ),
                        text_audio_end_sample=(
                            None
                            if chunk.text_audio_end_sample is None
                            else chunk.text_audio_end_sample + source_start_sample
                        ),
                        text_spans=tuple(
                            MediaTextSpan(
                                span.text,
                                span.audio_start_sample + source_start_sample,
                                span.audio_end_sample + source_start_sample,
                            )
                            for span in chunk.text_spans
                        ),
                    )
                else:
                    yield chunk
            return
        renderer = getattr(context.provider, "generate_output", None)
        if callable(renderer):
            produced = renderer(
                context.identity,
                work.intent,
                work.fence,
                work_id=work.intent_id,
                source_start_sample=source_start_sample,
            )
            if inspect.isawaitable(produced):
                produced = await produced
            async for chunk in produced:
                yield chunk
            return
        if getattr(work.intent, "WhichOneof", lambda _name: None)("source") != "pcm_s16le":
            raise RuntimeError("media provider cannot render an output text source")
        pcm = bytes(getattr(work.intent, "pcm_s16le", b""))
        if not pcm or len(pcm) % 2:
            raise ValueError("output PCM source must be non-empty 16-bit audio")
        frame_samples = int(getattr(context.provider, "output_frame_samples", 480))
        if frame_samples <= 0:
            raise RuntimeError("media provider has an invalid output frame size")
        frame_bytes = frame_samples * 2
        sample = source_start_sample
        for offset in range(0, len(pcm), frame_bytes):
            frame = pcm[offset : offset + frame_bytes]
            final = offset + frame_bytes >= len(pcm)
            if len(frame) < frame_bytes:
                frame += b"\x00" * (frame_bytes - len(frame))
            yield MediaReplyChunk(
                pcm_s16le=frame,
                source_start_sample=sample,
                first=offset == 0,
                final=final,
            )
            sample += frame_samples

    async def _finish_completed_output(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> None:
        if (
            not context.provider_complete
            or not context.playback.is_fully_acknowledged(fence)
            or not context.runtime.fence.matches(fence)
        ):
            return
        owner = context.output_owner
        if owner is not None and not owner.fence.matches(fence):
            return
        context.provider_complete = False
        if owner is not None:
            self._release_output_owner(context, fence, reason="playback_completed")
        if await self._start_selected_output(context):
            return
        if not context.output_complete_emitted:
            task_epoch, context_version = self._event_versions(context, fence)
            context.output_complete_emitted = await self.bridge.emit_generation(
                fence.session_id,
                fence,
                action=media_pb2.GENERATION_ACTION_COMPLETE,
                reason="provider_reply_complete",
                task_epoch=task_epoch,
                context_version=context_version,
            )
        await context.runtime.on_media_playback_done(
            fence,
            context.playback.actual_heard_text(fence),
        )

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
