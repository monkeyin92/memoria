"""VAD/ASR endpoint coordination for one Media Voice session."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.conversation_projection import ProjectionPatch
from services.agent.src.voice_core.asr_stream_supervisor import ASRAcceptDecision
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_session_types import (
    OutputDispatchResult,
    OutputDispatchStatus,
)
from services.agent.src.voice_core.speech_timeline import ASRResult
from services.common.realtime_information import is_clock_fact_query

if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

logger = logging.getLogger(__name__)

_PREPARE_RETRY_DELAYS_S = (0.05, 0.15)
_PREPARE_RETRY_EXHAUSTED_REASON = "provider_prepare_retries_exhausted"
_PREPARE_RETRY_SUPERSEDED_REASON = "provider_prepare_retry_superseded_by_new_vad"
# Device VAD and provider word timestamps are independent clocks around the
# same 16 kHz capture. After the provider task-finished boundary proves no
# later sentence can arrive, permit at most 1.5 seconds of tail skew after
# firmware removes its known 900 ms AFE hangover. Real-device evidence still
# showed 1.14 seconds between FunASR's last word and the device voiced end;
# larger gaps remain fail-closed so an earlier provider sentence cannot commit
# a later turn.
_ENDPOINT_ASR_COVERAGE_TOLERANCE_SAMPLES = 24_000
_CLOCK_FACT_PARTIAL_STABLE_S = 0.6


class MediaTurnEndpointMixin:
    """Own the bounded VAD/ASR endpoint state behind the registry interface."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        turn_endpoint_grace_s: float
        turn_endpoint_min_grace_s: float
        turn_endpoint_max_grace_s: float
        turn_endpoint_absolute_timeout_s: float
        _sessions: dict[str, _MediaVoiceSession]

        async def _accept_asr_result_decision(
            self,
            session_id: str,
            result: ASRResult,
        ) -> ASRAcceptDecision: ...

        async def _discard_projection(
            self,
            context: _MediaVoiceSession,
            reason: str,
        ) -> None: ...

        async def _emit_projection_patch(
            self,
            context: _MediaVoiceSession,
            patch: ProjectionPatch,
        ) -> None: ...

        async def _cancel_reply_task(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
            *,
            reason: str = "cancelled",
            cancel_timeout_s: float = 5.0,
        ) -> None: ...

        async def commit_user_turn(
            self,
            session_id: str,
            *,
            stream_epoch: int,
            start_sample: int,
            end_sample: int,
            retire_sample: int | None = None,
            provider_final_missing: bool = False,
        ) -> tuple[GenerationFence | None, str | None]: ...

        async def _commit_media_input_range(
            self,
            context: _MediaVoiceSession,
            *,
            session_id: str,
            stream_epoch: int,
            start_sample: int,
            end_sample: int,
            retire_end: int,
        ) -> None: ...

        async def generate_reply(
            self,
            session_id: str,
            user_text: str,
            fence: GenerationFence,
        ) -> bool: ...

        def _nudge_missed_hearing(self, context: _MediaVoiceSession) -> None: ...

        async def _dispatch_reply(
            self,
            session_id: str,
            user_text: str,
            fence: GenerationFence,
        ) -> OutputDispatchResult: ...

        def _record_output_dispatch_result(
            self,
            context: _MediaVoiceSession | None,
            result: OutputDispatchResult,
        ) -> None: ...

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
        context.committed_asr_keys[key] = None
        while len(context.committed_asr_keys) > 128:
            context.committed_asr_keys.popitem(last=False)
        context.turn_start_sample = min(
            result.capture_start_sample,
            context.turn_start_sample
            if context.turn_start_sample is not None
            else result.capture_start_sample,
        )
        context.turn_end_sample = max(result.capture_end_sample, context.turn_end_sample or 0)
        partial = context.pending_partial
        if partial is not None and (
            partial.sentence_id == result.sentence_id and partial.task_epoch <= result.task_epoch
        ):
            if (
                partial.task_epoch == result.task_epoch
                and partial.capture_end_sample > result.capture_end_sample
            ):
                # FunASR can shorten the final word-timing range after a live
                # partial already covered the device VAD endpoint. Preserve
                # that bounded fallback, but advance its revision baseline so
                # a tail-timeout promotion supersedes this accepted final.
                context.pending_partial = replace(
                    partial,
                    revision=max(partial.revision, result.revision),
                )
            else:
                context.pending_partial = None
        self._maybe_early_commit_clock_fact(context, result)
        # A provider final is evidence, never the endpoint itself. If VAD has
        # already ended, a late final re-arms the same logical-turn commit.
        if context.turn_endpoint_sample is not None:
            self._schedule_turn_commit(context)

    @staticmethod
    def _maybe_early_commit_clock_fact(
        context: _MediaVoiceSession,
        result: ASRResult,
    ) -> None:
        """Commit clock/date facts without waiting for a stuck device VAD end."""

        if context.identity.client_type != "device":
            return
        if context.turn_endpoint_sample is not None:
            return
        text = result.text.strip()
        if not text or not is_clock_fact_query(text):
            return
        endpoint = max(result.capture_end_sample, context.turn_end_sample or 0)
        context.turn_endpoint_sample = endpoint
        context.turn_retire_sample = max(context.turn_retire_sample or 0, endpoint)
        logger.info(
            "media early clock-fact endpoint session=%s endpoint=%s text_len=%s",
            context.identity.session_id,
            endpoint,
            len(text),
        )

    def _maybe_early_commit_stable_clock_fact_partial(
        self,
        context: _MediaVoiceSession,
    ) -> None:
        partial = context.pending_partial
        if partial is None:
            context.clock_fact_partial_text = None
            context.clock_fact_partial_stable_since = None
            return
        text = partial.text.strip()
        if not text or not is_clock_fact_query(text):
            context.clock_fact_partial_text = None
            context.clock_fact_partial_stable_since = None
            return
        now = time.monotonic()
        if context.clock_fact_partial_text == text:
            if context.clock_fact_partial_stable_since is None:
                context.clock_fact_partial_stable_since = now
            elif (
                context.turn_endpoint_sample is None
                and now - context.clock_fact_partial_stable_since
                >= _CLOCK_FACT_PARTIAL_STABLE_S
            ):
                endpoint = max(partial.capture_end_sample, context.turn_end_sample or 0)
                context.turn_endpoint_sample = endpoint
                context.turn_retire_sample = max(context.turn_retire_sample or 0, endpoint)
                logger.info(
                    "media early stable clock-fact partial session=%s endpoint=%s text_len=%s",
                    context.identity.session_id,
                    endpoint,
                    len(text),
                )
                self._schedule_turn_commit(context)
        else:
            context.clock_fact_partial_text = text
            context.clock_fact_partial_stable_since = now

    @staticmethod
    def _observe_partial_asr_result(
        context: _MediaVoiceSession,
        result: ASRResult,
    ) -> None:
        """Keep only the newest usable partial for the bounded tail fallback."""

        if not result.text.strip():
            return
        previous = context.pending_partial
        if previous is None or result.logical_version >= previous.logical_version:
            context.pending_partial = result

    @staticmethod
    def _asr_covers_endpoint(
        context: _MediaVoiceSession,
        result_end_sample: int | None,
        endpoint_sample: int,
    ) -> bool:
        if result_end_sample is None:
            return False
        return result_end_sample >= endpoint_sample or (
            context.ingress.last_finalized_audio_watermark >= endpoint_sample
            and endpoint_sample - result_end_sample <= _ENDPOINT_ASR_COVERAGE_TOLERANCE_SAMPLES
        )

    def _adaptive_endpoint_grace(self, context: _MediaVoiceSession) -> float:
        configured = float(self.turn_endpoint_grace_s)
        minimum = float(self.turn_endpoint_min_grace_s)
        maximum = float(self.turn_endpoint_max_grace_s)
        if configured <= 0 or configured < minimum:
            return configured
        observed = context.observed_within_turn_pause_s
        if observed is None:
            return min(configured, maximum)
        return min(
            maximum,
            max(minimum, observed + 0.15),
        )

    def _arm_endpoint_tail_timeout(
        self,
        context: _MediaVoiceSession,
        endpoint_sample: int,
    ) -> None:
        now = time.monotonic()
        if context.turn_endpoint_grace_deadline is None:
            grace = self._adaptive_endpoint_grace(context)
            context.turn_endpoint_grace_deadline = now + grace
        if context.turn_endpoint_tail_deadline is None:
            context.turn_endpoint_tail_deadline = now + max(
                self.turn_endpoint_absolute_timeout_s,
                self._adaptive_endpoint_grace(context),
            )
        handle = context.turn_endpoint_timeout_handle
        if handle is not None and not handle.cancelled():
            return
        delay = max(0.0, context.turn_endpoint_tail_deadline - now)
        context.turn_endpoint_timeout_handle = asyncio.get_running_loop().call_later(
            delay,
            self._start_endpoint_tail_expiry,
            context.identity.session_id,
            context.stream_epoch,
            endpoint_sample,
        )

    def _start_endpoint_tail_expiry(
        self,
        session_id: str,
        stream_epoch: int,
        endpoint_sample: int,
    ) -> None:
        context = self._sessions.get(session_id)
        if context is None:
            return
        context.turn_endpoint_timeout_handle = None
        asyncio.create_task(
            self._expire_endpoint_tail(session_id, stream_epoch, endpoint_sample),
            name=f"media-turn-tail-{session_id}-{endpoint_sample}",
        )

    @staticmethod
    def _turn_commit_retry_matches(
        context: _MediaVoiceSession,
        stream_epoch: int,
        endpoint_sample: int,
    ) -> bool:
        task = context.turn_commit_retry_task
        return bool(
            task is not None
            and not task.done()
            and context.turn_commit_retry_stream_epoch == stream_epoch
            and context.turn_commit_retry_endpoint_sample == endpoint_sample
        )

    @staticmethod
    def _clear_turn_commit_retry_state(context: _MediaVoiceSession) -> None:
        task = context.turn_commit_retry_task
        current_task = asyncio.current_task()
        if task is not None and task is not current_task and not task.done():
            task.cancel()
        context.turn_commit_retry_task = None
        context.turn_commit_retry_attempt = 0
        context.turn_commit_retry_stream_epoch = None
        context.turn_commit_retry_endpoint_sample = None

    @staticmethod
    def _clear_pending_turn_state(context: _MediaVoiceSession) -> None:
        max_speech_task = context.max_user_speech_task
        context.max_user_speech_task = None
        context.max_user_speech_deadline = None
        if (
            max_speech_task is not None
            and max_speech_task is not asyncio.current_task()
            and not max_speech_task.done()
        ):
            max_speech_task.cancel()
        if context.turn_endpoint_timeout_handle is not None:
            context.turn_endpoint_timeout_handle.cancel()
            context.turn_endpoint_timeout_handle = None
        MediaTurnEndpointMixin._clear_turn_commit_retry_state(context)
        context.turn_start_sample = None
        context.turn_end_sample = None
        context.turn_endpoint_sample = None
        context.turn_retire_sample = None
        context.turn_endpoint_grace_deadline = None
        context.turn_endpoint_tail_deadline = None
        context.committed_asr_keys.clear()
        context.pending_partial = None
        context.clock_fact_partial_text = None
        context.clock_fact_partial_stable_since = None

    async def _retire_pending_turn_input_range(
        self,
        context: _MediaVoiceSession,
        *,
        stream_epoch: int,
        endpoint_sample: int,
    ) -> bool:
        """Consume one discarded turn through its transport hangover boundary."""

        start_sample = context.turn_start_sample
        retire_sample = context.turn_retire_sample
        if (
            start_sample is None
            or retire_sample is None
            or endpoint_sample <= start_sample
            or retire_sample < endpoint_sample
        ):
            return False
        await self._commit_media_input_range(
            context,
            session_id=context.identity.session_id,
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=endpoint_sample,
            retire_end=retire_sample,
        )
        return True

    async def _expire_endpoint_tail(
        self,
        session_id: str,
        stream_epoch: int,
        endpoint_sample: int,
    ) -> None:
        context = self._sessions.get(session_id)
        if (
            context is None
            or context.closed
            or context.stream_epoch != stream_epoch
            or context.turn_endpoint_sample != endpoint_sample
        ):
            return
        retry_task = context.turn_commit_retry_task
        if retry_task is not None and self._turn_commit_retry_matches(
            context, stream_epoch, endpoint_sample
        ):
            try:
                await asyncio.shield(retry_task)
            except asyncio.CancelledError:
                if not retry_task.cancelled():
                    raise
            context = self._sessions.get(session_id)
            if (
                context is None
                or context.closed
                or context.stream_epoch != stream_epoch
                or context.turn_endpoint_sample != endpoint_sample
            ):
                return
        partial = context.pending_partial
        if (
            partial is not None
            and partial.text.strip()
            and self._asr_covers_endpoint(context, partial.capture_end_sample, endpoint_sample)
        ):
            # A stable partial is safer than silently dropping a complete user
            # utterance when the provider's final marker is lost.  Re-submit it
            # as a low-confidence final on the same sample interval; the ASR
            # supervisor still owns all revision and watermark checks.
            fallback_confidence = (
                0.35
                if partial.confidence is None
                else max(0.2, min(0.75, partial.confidence * 0.75))
            )
            fallback = replace(
                partial,
                is_final=True,
                revision=partial.revision + 1,
                confidence=fallback_confidence,
            )
            decision = await self._accept_asr_result_decision(session_id, fallback)
            if decision.accepted is not None:
                context.turn_end_sample = max(
                    decision.accepted.capture_end_sample,
                    context.turn_end_sample or 0,
                )
                await self._commit_pending_turn(
                    context,
                    provider_final_missing=True,
                )
                if context.turn_endpoint_sample != endpoint_sample:
                    return

        discarded: ProjectionPatch | None = None
        async with context.turn_commit_lock:
            current = self._sessions.get(session_id)
            if (
                current is not context
                or context.closed
                or context.stream_epoch != stream_epoch
                or context.turn_endpoint_sample != endpoint_sample
            ):
                return
            if not await self._retire_pending_turn_input_range(
                context,
                stream_epoch=stream_epoch,
                endpoint_sample=endpoint_sample,
            ):
                context.asr.mark_committed(endpoint_sample)
            discarded = context.projection.discard_provisional(
                None,
                "provider_final_missing",
            )
            self._clear_pending_turn_state(context)
            if context.runtime.assistant_speaking:
                context.runtime.publish_assistant_audio("restore", gain=1.0)
        if discarded is not None:
            await self._emit_projection_patch(context, discarded)
        logger.warning(
            "media turn discarded after ASR tail timeout session=%s stream_epoch=%s endpoint=%s "
            "partial_present=%s partial_text_len=%s partial_end_sample=%s",
            session_id,
            stream_epoch,
            endpoint_sample,
            partial is not None,
            len(partial.text.strip()) if partial is not None else 0,
            partial.capture_end_sample if partial is not None else None,
        )
        if endpoint_sample > 0:
            self._nudge_missed_hearing(context)

    def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None:
        task = context.turn_endpoint_task
        if task is not None and not task.done():
            task.cancel()
        endpoint_sample = context.turn_endpoint_sample
        if endpoint_sample is None:
            return
        if self._turn_commit_retry_matches(
            context,
            context.stream_epoch,
            endpoint_sample,
        ):
            return
        self._arm_endpoint_tail_timeout(context, endpoint_sample)
        grace_deadline = context.turn_endpoint_grace_deadline or time.monotonic()
        context.turn_endpoint_task = asyncio.create_task(
            self._commit_pending_turn_after_grace(
                context.identity.session_id,
                context.stream_epoch,
                endpoint_sample,
                max(0.0, grace_deadline - time.monotonic()),
            ),
            name=f"media-turn-endpoint-{context.identity.session_id}-{endpoint_sample}",
        )

    async def _commit_pending_turn_after_grace(
        self,
        session_id: str,
        stream_epoch: int,
        endpoint_sample: int,
        grace_s: float,
    ) -> None:
        try:
            if grace_s:
                await asyncio.sleep(grace_s)
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
                not self._asr_covers_endpoint(
                    context,
                    context.turn_end_sample,
                    endpoint_sample,
                )
                # A provider final is evidence, not an endpoint.  If ASR has
                # not covered the VAD end within bounded clock skew, leave
                # the buffered turn open;
                # the late final will re-arm this same commit in
                # ``_observe_final_asr_result``.
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

    async def _commit_pending_turn(
        self,
        context: _MediaVoiceSession,
        *,
        provider_final_missing: bool = False,
        schedule_prepare_retry: bool = True,
    ) -> str | None:
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
            return "invalid_pending_media_turn"
        previous_fence = context.playback.current_fence or context.runtime.fence
        fence, reason = await self.commit_user_turn(
            context.identity.session_id,
            stream_epoch=context.stream_epoch,
            start_sample=start_sample,
            end_sample=endpoint_sample,
            retire_sample=retire_sample,
            provider_final_missing=provider_final_missing,
        )
        # The range was consumed even when Router turns it into a low-risk
        # control action rather than a chat generation.
        prepare_retryable = (
            reason == "provider_prepare_failed" and context.projection.provisional is not None
        )
        if prepare_retryable and schedule_prepare_retry:
            self._schedule_turn_prepare_retry(
                context,
                stream_epoch=context.stream_epoch,
                endpoint_sample=endpoint_sample,
                provider_final_missing=provider_final_missing,
            )
        if reason != "empty_media_turn" and not prepare_retryable:
            self._clear_pending_turn_state(context)
        if fence is None:
            # Pure control/enrol/guarded utterances are intentionally not sent
            # to the LLM; ``accept_user_turn`` already routed those centrally.
            if reason not in {"empty_media_turn", "session_not_found"}:
                logger.info(
                    "media final did not start reply session=%s reason=%s",
                    context.identity.session_id,
                    reason,
                )
            return reason
        # A final ASR result can arrive while the previous answer is still
        # synthesizing.  Wait for that task to release the per-session reply
        # lock before scheduling the new turn; otherwise ``generate_reply``
        # sees a locked session and silently drops a valid user turn.
        await self._cancel_reply_task(context, previous_fence)
        task = asyncio.create_task(
            self._dispatch_reply(
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

        def _observe(done: asyncio.Task[OutputDispatchResult]) -> None:
            if done.cancelled():
                self._record_output_dispatch_result(
                    context,
                    OutputDispatchResult(
                        fence,
                        OutputDispatchStatus.ABORTED,
                        "reply_task_cancelled",
                    ),
                )
                return
            try:
                result = done.result()
            except Exception:
                self._record_output_dispatch_result(
                    context,
                    OutputDispatchResult(
                        fence,
                        OutputDispatchStatus.ABORTED,
                        "reply_task_exception",
                    ),
                )
                logger.exception(
                    "media reply failed session=%s fence=%s",
                    context.identity.session_id,
                    fence,
                )
                return
            self._record_output_dispatch_result(context, result)

        task.add_done_callback(_observe)
        return reason

    def _schedule_turn_prepare_retry(
        self,
        context: _MediaVoiceSession,
        *,
        stream_epoch: int,
        endpoint_sample: int,
        provider_final_missing: bool,
    ) -> None:
        if self._turn_commit_retry_matches(context, stream_epoch, endpoint_sample):
            return
        self._clear_turn_commit_retry_state(context)
        context.turn_commit_retry_attempt = 0
        context.turn_commit_retry_stream_epoch = stream_epoch
        context.turn_commit_retry_endpoint_sample = endpoint_sample
        context.turn_commit_retry_task = asyncio.create_task(
            self._run_turn_prepare_retries(
                context.identity.session_id,
                stream_epoch,
                endpoint_sample,
                provider_final_missing=provider_final_missing,
            ),
            name=(
                f"media-turn-prepare-retry-{context.identity.session_id}-"
                f"{stream_epoch}-{endpoint_sample}"
            ),
        )
        self.metrics.inc_media_metric(
            "voice_turn_prepare_retry_total",
            labels={"status": "scheduled"},
        )

    async def _run_turn_prepare_retries(
        self,
        session_id: str,
        stream_epoch: int,
        endpoint_sample: int,
        *,
        provider_final_missing: bool,
    ) -> None:
        current_task = asyncio.current_task()
        try:
            for attempt, delay_s in enumerate(_PREPARE_RETRY_DELAYS_S, start=1):
                await asyncio.sleep(delay_s)
                context = self._sessions.get(session_id)
                if (
                    context is None
                    or context.closed
                    or context.turn_commit_retry_task is not current_task
                    or not self._turn_commit_retry_matches(
                        context,
                        stream_epoch,
                        endpoint_sample,
                    )
                    or context.projection.provisional is None
                ):
                    return
                context.turn_commit_retry_attempt = attempt
                self.metrics.inc_media_metric(
                    "voice_turn_prepare_retry_total",
                    labels={"status": "attempt"},
                )
                reason = await self._commit_pending_turn(
                    context,
                    provider_final_missing=provider_final_missing,
                    schedule_prepare_retry=False,
                )
                if reason != "provider_prepare_failed":
                    if reason is None:
                        self.metrics.inc_media_metric(
                            "voice_turn_prepare_retry_total",
                            labels={"status": "succeeded"},
                        )
                    return
                if context.projection.provisional is None:
                    return
            await self._discard_exhausted_turn_prepare_retry(
                session_id,
                stream_epoch,
                endpoint_sample,
            )
        except asyncio.CancelledError:
            return
        finally:
            context = self._sessions.get(session_id)
            if context is not None and context.turn_commit_retry_task is current_task:
                self._clear_turn_commit_retry_state(context)

    async def _discard_exhausted_turn_prepare_retry(
        self,
        session_id: str,
        stream_epoch: int,
        endpoint_sample: int,
    ) -> None:
        context = self._sessions.get(session_id)
        if context is None:
            return
        discarded: ProjectionPatch | None = None
        async with context.turn_commit_lock:
            current = self._sessions.get(session_id)
            if (
                current is not context
                or context.closed
                or context.stream_epoch != stream_epoch
                or context.turn_endpoint_sample != endpoint_sample
                or not self._turn_commit_retry_matches(
                    context,
                    stream_epoch,
                    endpoint_sample,
                )
            ):
                return
            if not await self._retire_pending_turn_input_range(
                context,
                stream_epoch=stream_epoch,
                endpoint_sample=endpoint_sample,
            ):
                context.asr.mark_committed(endpoint_sample)
            discarded = context.projection.discard_provisional(
                None,
                _PREPARE_RETRY_EXHAUSTED_REASON,
            )
            self._clear_pending_turn_state(context)
            if context.runtime.assistant_speaking:
                context.runtime.publish_assistant_audio("restore", gain=1.0)
        if discarded is not None:
            await self._emit_projection_patch(context, discarded)
        self.metrics.inc_media_metric(
            "voice_turn_prepare_retry_total",
            labels={"status": "exhausted"},
        )
        logger.error(
            "media turn preparation retries exhausted session=%s stream_epoch=%s endpoint=%s",
            session_id,
            stream_epoch,
            endpoint_sample,
        )

    async def _retire_prepare_retry_before_new_vad(
        self,
        context: _MediaVoiceSession,
    ) -> None:
        retry_task = context.turn_commit_retry_task
        stream_epoch = context.stream_epoch
        endpoint_sample = context.turn_commit_retry_endpoint_sample
        if (
            retry_task is None
            or endpoint_sample is None
            or not self._turn_commit_retry_matches(
                context,
                stream_epoch,
                endpoint_sample,
            )
        ):
            return
        discarded: ProjectionPatch | None = None
        async with context.turn_commit_lock:
            if not self._turn_commit_retry_matches(
                context,
                stream_epoch,
                endpoint_sample,
            ):
                return
            retry_task = context.turn_commit_retry_task
            if not await self._retire_pending_turn_input_range(
                context,
                stream_epoch=stream_epoch,
                endpoint_sample=endpoint_sample,
            ):
                context.asr.mark_committed(endpoint_sample)
            discarded = context.projection.discard_provisional(
                None,
                _PREPARE_RETRY_SUPERSEDED_REASON,
            )
            self._clear_pending_turn_state(context)
            if context.runtime.assistant_speaking:
                context.runtime.publish_assistant_audio("restore", gain=1.0)
        if retry_task is not None and retry_task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await retry_task
        if discarded is not None:
            await self._emit_projection_patch(context, discarded)
        self.metrics.inc_media_metric(
            "voice_turn_prepare_retry_total",
            labels={"status": "superseded"},
        )
        logger.warning(
            "media turn preparation retry superseded by new VAD session=%s "
            "stream_epoch=%s endpoint=%s",
            context.identity.session_id,
            stream_epoch,
            endpoint_sample,
        )
