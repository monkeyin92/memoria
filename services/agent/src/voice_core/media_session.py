"""Voice Core session registry for the media-v1 bridge.

This module is the narrow seam between transport and the existing
``DuplexRuntime``.  It deliberately does not implement a second LLM/TTS
stack: a deployment injects its ASR/LLM/TTS provider adapter, while this
registry owns session identity, sample-clock ASR acceptance, generation
fencing and downlink PCM delivery.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import GLOBAL_METRICS, MetricsRegistry
from services.agent.src.voice_core.asr_stream_supervisor import (
    ASRAcceptDecision,
    ASRDecisionReason,
    ASRStreamSupervisor,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
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

media_pb2: Any = _media_pb2
logger = logging.getLogger(__name__)


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


@dataclass(slots=True)
class _MediaVoiceSession:
    identity: SessionIdentity
    runtime: DuplexRuntime
    provider: MediaVoiceProvider
    asr: ASRStreamSupervisor
    playback: PlaybackLedger = field(default_factory=PlaybackLedger)
    output_sequence: int = 0
    output_text_offset: int = 0
    assistant_text: str = ""
    stream_epoch: int = 0
    turn_started_ns: int | None = None
    first_audio_observed: bool = False
    provider_complete: bool = False
    reply_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reply_task: asyncio.Task[bool] | None = None
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
    provider_factory: ProviderFactory
    runtime_factory: RuntimeFactory = field(default=_default_runtime_factory)
    metrics: MetricsRegistry = field(default_factory=lambda: GLOBAL_METRICS)
    max_sessions: int = 256
    reconnect_grace_s: float = 30.0
    # Child speech commonly contains 500-800 ms within-turn pauses.  The VAD
    # edge is therefore only a candidate endpoint until this quiescence
    # window passes and final ASR covers the same sample-clock position.
    turn_endpoint_grace_s: float = 0.9
    kws_hard_stop_min_confidence: float = 0.8
    _sessions: dict[str, _MediaVoiceSession] = field(default_factory=dict, init=False)
    _cleanup_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        if self.max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        if self.reconnect_grace_s <= 0:
            raise ValueError("reconnect_grace_s must be positive")
        if self.turn_endpoint_grace_s < 0:
            raise ValueError("turn_endpoint_grace_s must be non-negative")
        if not 0.0 <= self.kws_hard_stop_min_confidence <= 1.0:
            raise ValueError("kws_hard_stop_min_confidence must be between 0 and 1")

    def install(self) -> None:
        """Connect this registry to a ``MediaBridgeGrpcServer`` instance."""

        self.bridge.on_audio_frame = self.on_audio_frame
        self.bridge.on_speech_segment = self.on_speech_segment
        self.bridge.on_client_event = self.on_client_event
        self.bridge.on_session_closed = self.on_session_closed
        self.bridge.on_playback_progress = self.on_playback_progress
        self.bridge.on_downlink_overflow = self.on_downlink_overflow

    async def _get_or_create(self, identity: SessionIdentity) -> _MediaVoiceSession:
        async with self._lock:
            current = self._sessions.get(identity.session_id)
            if current is not None:
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
                if identity.stream_epoch > current.stream_epoch:
                    current.identity = identity
                    current.stream_epoch = identity.stream_epoch
                    current.runtime.start_media_stream_epoch(identity.stream_epoch)
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
                return current
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError("Voice Core media session limit reached")
            runtime = self.runtime_factory(identity.session_id)
            provider = self.provider_factory(identity)
            asr = ASRStreamSupervisor(stream_epoch=identity.stream_epoch)
            current = _MediaVoiceSession(
                identity=identity,
                runtime=runtime,
                provider=provider,
                asr=asr,
                stream_epoch=identity.stream_epoch,
            )

            async def publish_runtime_event(event: dict[str, Any]) -> None:
                await self._publish_runtime_event(identity.session_id, event)

            runtime.set_event_publisher(publish_runtime_event)
            self._sessions[identity.session_id] = current
            self.metrics.inc_media_session_started()
            self.metrics.set_media_active_sessions(len(self._sessions))
            return current

    async def _publish_runtime_event(
        self,
        session_id: str,
        event: dict[str, Any],
    ) -> None:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return
        payload = {key: value for key, value in event.items() if key != "type"}

        def event_int(key: str) -> int:
            value = event.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        await self.bridge.emit_event(
            session_id,
            event_type,
            payload,
            turn_id=event_int("turn_id"),
            generation_id=event_int("generation_id"),
            tool_epoch=event_int("tool_epoch"),
        )

    async def on_audio_frame(
        self,
        session: MediaBridgeSession,
        frame: AudioFrame,
    ) -> None:
        context = await self._get_or_create(session.identity)
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
        for result in results:
            decision = await self._accept_asr_result_decision(context.identity.session_id, result)
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
            if (
                context is None
                or context.closed
                or context.stream_epoch != stream_epoch
                or context.turn_endpoint_sample != endpoint_sample
                or context.turn_end_sample is None
                # A provider final is evidence, not an endpoint.  If ASR has
                # not covered the VAD end yet, leave the buffered turn open;
                # the late final will re-arm this same commit in
                # ``_observe_final_asr_result``.
                or context.turn_end_sample < endpoint_sample
            ):
                return
            await self._commit_pending_turn(context)
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
        # A media VAD/KWS event is already range-stamped; it must not be
        # converted into a callback-order speech epoch.
        if segment.kind is SegmentKind.KWS and segment.final:
            if (
                segment.hard_stop
                and (segment.confidence or 0.0) >= self.kws_hard_stop_min_confidence
            ):
                # Never subtract an Edge wall-clock timestamp from Core's
                # monotonic clock. The only trustworthy local measurement is
                # this handler's own work; end-to-end timing is fail-closed
                # until distributed tracing is available.
                _ = detected_monotonic_ms
                stop_started_ns = time.monotonic_ns()
                route = context.runtime.route_user_turn(segment.text)
                if route.should_interrupt and not route.enter_chat:
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
                            await self._cancel_reply_task(context, previous_fence)
                            await self.bridge.emit_generation(
                                context.identity.session_id,
                                cancelled,
                                action=media_pb2.GENERATION_ACTION_CANCEL,
                                reason="keyword_interrupt",
                            )
                            self.metrics.observe_voice_latency(
                                "interrupt_core_stop",
                                (time.monotonic_ns() - stop_started_ns) / 1_000_000_000,
                            )
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
                turn_id=context.runtime.fence.turn_id,
                generation_id=context.runtime.fence.generation_id,
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
        await self._cancel_reply_task(context, previous_fence)

    @staticmethod
    async def _cancel_provider_generation(
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> None:
        """Propagate a transport cancel into adapters that support it.

        ``MediaVoiceProvider`` stays provider-neutral, but the existing
        adapter exposes a cooperative cancellation hook.  Calling it before
        cancelling the registry task prevents an in-flight remote TTS request
        from continuing after the authoritative generation has moved on.
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
    ) -> None:
        """Cancel provider work and drain the old reply task before reuse."""

        task = context.reply_task
        # A provider can own a remote stream after its local task has already
        # completed. Transport cancellation must still reach that provider.
        await cls._cancel_provider_generation(context, fence)
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
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
        context = self._sessions.pop(session_id, None)
        if context is None or context.closed:
            return
        context_stream_epoch = context.stream_epoch
        context.closed = True
        self.metrics.set_media_active_sessions(len(self._sessions))
        if context.reply_task is not None and not context.reply_task.done():
            context.reply_task.cancel()
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
            context.provider_complete = False
            await context.runtime.on_media_playback_done(
                fence,
                heard,
            )
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
            return ASRAcceptDecision(None, ASRDecisionReason.INTERVAL_CONFLICT)
        await self.bridge.emit_transcript(session_id, segment)
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
        text = context.runtime.consume_media_user_turn(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        if not text:
            return None, "empty_media_turn"
        retire_end = end_sample if retire_sample is None else retire_sample
        if retire_end < end_sample:
            raise ValueError("media retire sample cannot precede the logical endpoint")
        if retire_end > end_sample:
            context.runtime.commit_media_speech_range(
                stream_epoch=stream_epoch,
                start_sample=end_sample,
                end_sample=retire_end,
            )
        context.asr.mark_committed(retire_end)
        accepted, reason = context.runtime.accept_user_turn(
            text,
            input_modality="audio",
            speech_anchored=True,
            canonical_speech_epoch=context.runtime.consumed_canonical_speech_epoch,
            canonical_snapshot_bound=True,
        )
        if not accepted:
            return None, reason or "user_turn_rejected"
        fence = await context.runtime.on_turn_committed(text, input_modality="audio")
        context.playback.start(fence)
        context.output_sequence = 0
        context.output_text_offset = 0
        context.assistant_text = ""
        context.provider_complete = False
        context.turn_started_ns = time.monotonic_ns()
        context.first_audio_observed = False
        await self.bridge.emit_generation(
            session_id,
            fence,
            action=media_pb2.GENERATION_ACTION_START,
            reason="user_turn_committed",
        )
        return fence, None

    async def generate_reply(
        self,
        session_id: str,
        user_text: str,
        fence: GenerationFence,
    ) -> bool:
        """Serialize one playable provider stream per media session."""

        context = self._sessions.get(session_id)
        if context is None or context.closed or context.reply_lock.locked():
            return False
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - every async call has a task
            return False
        context.reply_task = task
        try:
            async with context.reply_lock:
                return await self._generate_reply(session_id, user_text, fence)
        finally:
            if context.reply_task is task:
                context.reply_task = None

    async def _generate_reply(
        self,
        session_id: str,
        user_text: str,
        fence: GenerationFence,
    ) -> bool:
        """Stream provider PCM through both Voice Core and Media Edge gates."""

        context = self._sessions.get(session_id)
        if context is None or context.closed or not context.runtime.fence.matches(fence):
            self.metrics.inc_media_stale_generation()
            return False
        announced_speaking = False
        try:
            async for chunk in context.provider.generate_reply(context.identity, user_text, fence):
                if not context.runtime.fence.matches(fence):
                    self.metrics.inc_media_stale_generation()
                    return False
                announcement = (
                    chunk.text if chunk.assistant_text_delta is None else chunk.assistant_text_delta
                )
                if announcement:
                    context.assistant_text += announcement
                    # Keep the runtime's heard-text tracker aligned with the
                    # complete provider text, while the ledger still decides
                    # whether that text was actually rendered.
                    await context.runtime.on_assistant_speaking(context.assistant_text)
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
                    announced_speaking = True
                gated = context.runtime.gate_tts_audio(fence, chunk.pcm_s16le)
                if gated is None:
                    self.metrics.inc_media_stale_generation()
                    return False
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
                )
                if not await self.bridge.emit_pcm(session_id, frame):
                    self.metrics.inc_media_stale_generation()
                    return False
                if not context.playback.register_audio(
                    fence,
                    frame.sequence,
                    frame.source_start_sample,
                    frame.frame_samples,
                ):
                    self.metrics.inc_media_stale_generation()
                    return False
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
        except Exception:
            self.metrics.inc_media_session_failed()
            raise
        if announced_speaking and context.runtime.fence.matches(fence):
            # Provider completion is not playback completion.  Keep the
            # runtime speaking until a client PlaybackProgress watermark
            # covers every mapped text span; otherwise interrupted/undelivered
            # text could enter history as if it had been heard.
            context.provider_complete = True
            await self.bridge.emit_generation(
                session_id,
                fence,
                action=media_pb2.GENERATION_ACTION_COMPLETE,
                reason="provider_reply_complete",
            )
            # A very fast client may acknowledge the last frame before the
            # provider iterator yields its completion. Re-check the ledger at
            # provider completion so the runtime cannot remain SPEAKING until
            # a second, unnecessary ACK arrives.
            if context.playback.is_fully_acknowledged(fence):
                context.provider_complete = False
                await context.runtime.on_media_playback_done(
                    fence,
                    context.playback.actual_heard_text(fence),
                )
        return True

    def context(self, session_id: str) -> DuplexRuntime | None:
        current = self._sessions.get(session_id)
        return current.runtime if current is not None and not current.closed else None


__all__ = [
    "MediaTextSpan",
    "MediaReplyChunk",
    "MediaVoiceCoreRegistry",
    "MediaVoiceProvider",
]
