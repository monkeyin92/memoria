"""VAD/ASR endpoint coordination for one Media Voice session."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.voice_core.asr_stream_supervisor import ASRAcceptDecision
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.speech_timeline import ASRResult

if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

logger = logging.getLogger(__name__)


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

        async def _cancel_reply_task(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
            *,
            reason: str = "cancelled",
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

        async def generate_reply(
            self,
            session_id: str,
            user_text: str,
            fence: GenerationFence,
        ) -> bool: ...

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
        if context.pending_partial is not None and (
            context.pending_partial.sentence_id == result.sentence_id
            and context.pending_partial.task_epoch <= result.task_epoch
        ):
            context.pending_partial = None
        # A provider final is evidence, never the endpoint itself. If VAD has
        # already ended, a late final re-arms the same logical-turn commit.
        if context.turn_endpoint_sample is not None:
            self._schedule_turn_commit(context)

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
        partial = context.pending_partial
        if (
            partial is not None
            and partial.text.strip()
            and partial.capture_end_sample >= endpoint_sample
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

        context.asr.mark_committed(endpoint_sample)
        await self._discard_projection(context, "provider_final_missing")
        context.turn_start_sample = None
        context.turn_end_sample = None
        context.turn_endpoint_sample = None
        context.turn_retire_sample = None
        context.turn_endpoint_grace_deadline = None
        context.turn_endpoint_tail_deadline = None
        context.committed_asr_keys.clear()
        context.pending_partial = None
        if context.runtime.assistant_speaking:
            context.runtime.publish_assistant_audio("restore", gain=1.0)
        logger.warning(
            "media turn discarded after ASR tail timeout session=%s stream_epoch=%s endpoint=%s",
            session_id,
            stream_epoch,
            endpoint_sample,
        )

    def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None:
        task = context.turn_endpoint_task
        if task is not None and not task.done():
            task.cancel()
        endpoint_sample = context.turn_endpoint_sample
        if endpoint_sample is None:
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

    async def _commit_pending_turn(
        self,
        context: _MediaVoiceSession,
        *,
        provider_final_missing: bool = False,
    ) -> None:
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
            provider_final_missing=provider_final_missing,
        )
        # The range was consumed even when Router turns it into a low-risk
        # control action rather than a chat generation.
        if reason != "empty_media_turn":
            if context.turn_endpoint_timeout_handle is not None:
                context.turn_endpoint_timeout_handle.cancel()
                context.turn_endpoint_timeout_handle = None
            context.turn_start_sample = None
            context.turn_end_sample = None
            context.turn_endpoint_sample = None
            context.turn_retire_sample = None
            context.turn_endpoint_grace_deadline = None
            context.turn_endpoint_tail_deadline = None
            context.committed_asr_keys.clear()
            context.pending_partial = None
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
