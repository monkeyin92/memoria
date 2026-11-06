"""Rejected-final recovery mixin for the committed media turn.

``MediaSessionCommitRecoveryMixin`` retries or refuses finals rejected by the
ASR supervisor (semantic, clock/date, straddling live-query) before the turn
commit path runs.  Session state, session registry and bridge stay owned by
``MediaSessionCommitMixin`` and are declared here for type checking only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from services.agent.src.clock_fact_queries import is_clock_fact_query
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.asr_stream_supervisor import (
    ASRAcceptDecision,
    ASRDecisionReason,
    ASROverlapEvidence,
)
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SpeechSegment,
    asr_result_to_segment,
)

if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

_CLOSE_RECOVERY_MIN_RESCUE_RMS = 1_000
_CLOSE_RECOVERY_MIN_RESCUE_PEAK_ABS = 8_000

logger = logging.getLogger(__name__)


class MediaSessionCommitRecoveryMixin:
    """Retry the finals the supervisor rejected before committing a turn."""

    if TYPE_CHECKING:
        bridge: Any
        _sessions: dict[str, _MediaVoiceSession]

        async def _apply_projection_segment(
            self, context: _MediaVoiceSession, segment: SpeechSegment
        ) -> None: ...

        def _event_versions(
            self, context: _MediaVoiceSession, fence: GenerationFence
        ) -> tuple[int, int]: ...

        def _observe_final_asr_result(
            self, context: _MediaVoiceSession, result: ASRResult
        ) -> None: ...

        @staticmethod
        def _reply_in_flight(context: _MediaVoiceSession) -> bool: ...

        def _maybe_early_commit_conversation_close(
            self, context: _MediaVoiceSession, result: ASRResult
        ) -> None: ...

        def _arm_live_query_forced_endpoint(
            self, context: _MediaVoiceSession, result: ASRResult
        ) -> None: ...

        def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None: ...

        def _stream_epoch_is_current(
            self, context: _MediaVoiceSession, stream_epoch: int
        ) -> bool: ...

        @staticmethod
        def _asr_precedes_pending_turn(
            context: _MediaVoiceSession, result: ASRResult
        ) -> bool: ...

        def _playback_stop_diagnosis(
            self, context: _MediaVoiceSession, result: ASRResult
        ) -> str: ...


    async def _recover_rejected_semantic_final(
        self,
        context: _MediaVoiceSession,
        *,
        session_id: str,
        result: ASRResult,
        reason: ASRDecisionReason,
    ) -> None:
        if self._asr_precedes_pending_turn(context, result):
            return
        text = result.text.strip()
        if not text:
            return
        if is_clock_fact_query(text):
            await self._recover_rejected_clock_fact_final(
                context,
                session_id=session_id,
                result=result,
                reason=reason,
            )
            return
        close_needed, live_lookup_needed = await asyncio.gather(
            context.runtime.resolve_conversation_close_needed(text),
            context.runtime.resolve_live_lookup_needed(text),
        )
        if (
            not self._stream_epoch_is_current(context, result.stream_epoch)
            or self._asr_precedes_pending_turn(context, result)
        ):
            return
        if close_needed or live_lookup_needed:
            await self._recover_straddling_live_query_final(
                context,
                session_id=session_id,
                result=result,
                reason=reason,
            )

    async def _recover_rejected_clock_fact_final(
        self,
        context: _MediaVoiceSession,
        *,
        session_id: str,
        result: ASRResult,
        reason: ASRDecisionReason,
    ) -> None:
        """Keep clock/date turns when overlap policy drops an otherwise valid final."""

        if reason is not ASRDecisionReason.CROSS_SENTENCE_OVERLAP:
            return
        if (
            not self._stream_epoch_is_current(context, result.stream_epoch)
            or self._asr_precedes_pending_turn(context, result)
        ):
            return
        context.pending.admitted_input_stream_epoch = result.stream_epoch
        segment = asr_result_to_segment(result, session_id=session_id)
        if context.runtime.speech_timeline.can_add(segment):
            if context.runtime.ingest_media_speech_segment(segment):
                await self._apply_projection_segment(context, segment)
                if (
                    not self._stream_epoch_is_current(context, result.stream_epoch)
                    or self._asr_precedes_pending_turn(context, result)
                ):
                    return
                task_epoch, context_version = self._event_versions(
                    context, context.runtime.fence
                )
                await self.bridge.emit_transcript(
                    session_id,
                    segment,
                    task_epoch=task_epoch,
                    context_version=context_version,
                )
        else:
            context.pending.clock_fact_forced_text = result.text.strip()
        if (
            not self._stream_epoch_is_current(context, result.stream_epoch)
            or self._asr_precedes_pending_turn(context, result)
        ):
            return
        # Timeline ingest above is intentional; only suppress re-arm/commit while
        # an earlier reply still owns the session (late offline finals).
        if self._reply_in_flight(context):
            logger.warning(
                "media clock-fact recovery commit skipped: reply in flight "
                "session=%s text_len=%s samples=%s-%s",
                session_id,
                len(result.text.strip()),
                result.capture_start_sample,
                result.capture_end_sample,
            )
            return
        # Observe sets turn_start. Pinning the endpoint alone left
        # epoch 1384 as invalid_pending until ASR tail timeout.
        self._observe_final_asr_result(context, result)

    async def _recover_straddling_live_query_final(
        self,
        context: _MediaVoiceSession,
        *,
        session_id: str,
        result: ASRResult,
        reason: ASRDecisionReason,
    ) -> None:
        """Keep weather/live turns when overlap policy drops an otherwise valid final.

        Two rejection shapes are recovered here:
        - STRADDLES_COMMITTED_WITHOUT_TIMING: the final spans an already-
          committed range; trim its start to the committed watermark.
        - CROSS_SENTENCE_OVERLAP: a blocking interval (e.g. playback echo
          transcribed while the assistant was speaking) overlaps the user
          final. The in-range timeline text is then untrustworthy, so the
          forced text is marked authoritative to win commit-time resolution
          regardless of length.
        """

        if reason not in (
            ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING,
            ASRDecisionReason.CROSS_SENTENCE_OVERLAP,
        ):
            return
        if self._asr_precedes_pending_turn(context, result):
            return
        text = result.text.strip()
        if not text:
            return
        close_needed, live_lookup_needed = await asyncio.gather(
            context.runtime.resolve_conversation_close_needed(text),
            context.runtime.resolve_live_lookup_needed(text),
        )
        if (
            not self._stream_epoch_is_current(context, result.stream_epoch)
            or self._asr_precedes_pending_turn(context, result)
        ):
            return
        if not (close_needed or live_lookup_needed):
            return
        if (
            close_needed
            and not live_lookup_needed
            and self._rejected_rescue_is_low_energy(context, result, reason)
        ):
            logger.warning(
                "media close recovery rejected: low-energy rescue final "
                "session=%s text_len=%s reason=%s rescue_rms=%s rescue_peak_abs=%s",
                session_id,
                len(text),
                reason.value,
                result.rescue_rms,
                result.rescue_peak_abs,
            )
            return
        committed = context.asr.last_committed_sample
        if result.capture_end_sample <= committed:
            # Silent early-return here starved a weather turn with zero ERROR
            # (2026-09-03 epoch 1361). Keep the gate, but make it audible.
            logger.warning(
                "media live-query recovery skipped: already committed "
                "session=%s reason=%s text_len=%s samples=%s-%s committed=%s",
                session_id,
                reason.value,
                len(text),
                result.capture_start_sample,
                result.capture_end_sample,
                committed,
            )
            return
        if (
            reason is ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING
            and committed - result.capture_start_sample
            > result.capture_end_sample - committed
        ):
            # The straddles_committed_without_timing shape has no complete word
            # timings, so the text cannot be cut at the watermark: what remains
            # of the result is its audio only if most of that audio is still
            # uncommitted.  When the final is mostly audio that was already
            # committed and answered, adopting its text re-answers that audio as
            # a second turn.  Real session 2026-09-14 epoch 1946 asked the next
            # day's weather once, the offline rescue of that same audio arrived
            # as a second 20-character transcript, and the adopted text
            # preempted the pending first answer.
            logger.warning(
                "media live-query recovery dropped: final is mostly committed "
                "session=%s reason=%s text_len=%s samples=%s-%s committed=%s",
                session_id,
                reason.value,
                len(text),
                result.capture_start_sample,
                result.capture_end_sample,
                committed,
            )
            return
        adjusted_start = max(committed, result.capture_start_sample)
        adjusted = replace(
            result,
            capture_start_sample=adjusted_start,
        )
        context.pending.admitted_input_stream_epoch = adjusted.stream_epoch
        segment = asr_result_to_segment(adjusted, session_id=session_id)
        if context.runtime.speech_timeline.can_add(segment):
            if context.runtime.ingest_media_speech_segment(segment):
                await self._apply_projection_segment(context, segment)
                if (
                    not self._stream_epoch_is_current(context, adjusted.stream_epoch)
                    or self._asr_precedes_pending_turn(context, adjusted)
                ):
                    return
                task_epoch, context_version = self._event_versions(
                    context, context.runtime.fence
                )
                await self.bridge.emit_transcript(
                    session_id,
                    segment,
                    task_epoch=task_epoch,
                    context_version=context_version,
                )
            else:
                logger.warning(
                    "media live-query recovery ingest failed session=%s reason=%s "
                    "text_len=%s samples=%s-%s",
                    session_id,
                    reason.value,
                    len(text),
                    adjusted.capture_start_sample,
                    adjusted.capture_end_sample,
                )
        else:
            logger.warning(
                "media live-query recovery can_add failed session=%s reason=%s "
                "text_len=%s samples=%s-%s",
                session_id,
                reason.value,
                len(text),
                adjusted.capture_start_sample,
                adjusted.capture_end_sample,
            )
        if (
            not self._stream_epoch_is_current(context, adjusted.stream_epoch)
            or self._asr_precedes_pending_turn(context, adjusted)
        ):
            return
        # A farewell is terminal even when overlap/echo makes the same ASR
        # final also resemble a live lookup.  Device shutdown must win over
        # any tool intent so a trailing "再见" cannot trigger another reply.
        if close_needed:
            self._observe_final_asr_result(context, adjusted)
            self._maybe_early_commit_conversation_close(context, adjusted)
            if context.pending.turn_endpoint_sample is not None:
                self._schedule_turn_commit(context)
            return
        context.pending.live_query_forced_text = text
        if reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP:
            # The blocking interval's text (e.g. playback echo) stays on the
            # in-range timeline, so the recovered text must win commit-time
            # resolution unconditionally rather than by length.
            context.pending.live_query_forced_authoritative = True
        # Forced text is already the authoritative commit text. Always observe
        # turn bounds; re-arm/commit only when no reply is already in flight.
        self._observe_final_asr_result(context, adjusted)
        if context.pending.live_query_forced_authoritative:
            if self._reply_in_flight(context):
                logger.warning(
                    "media live-query recovery arm skipped: reply in flight "
                    "session=%s text_len=%s samples=%s-%s",
                    session_id,
                    len(text),
                    adjusted.capture_start_sample,
                    adjusted.capture_end_sample,
                )
            else:
                self._arm_live_query_forced_endpoint(context, adjusted)

    @staticmethod
    def _rejected_rescue_is_low_energy(
        context: _MediaVoiceSession,
        result: ASRResult,
        reason: ASRDecisionReason,
    ) -> bool:
        """Reject rescue farewells whose audio lacks speech evidence.

        Both recovered rejection shapes qualify: a straddling rescue final is
        just as unverified as an overlapping one.  Prefer the energy measured
        on the rescued segment itself; the provider's current task snapshot
        is a fallback that may already describe a later task.
        """

        if (
            reason
            not in (
                ASRDecisionReason.CROSS_SENTENCE_OVERLAP,
                ASRDecisionReason.STRADDLES_COMMITTED_WITHOUT_TIMING,
            )
            or not result.rescue_synthesized
        ):
            return False
        if result.rescue_rms is not None and result.rescue_peak_abs is not None:
            return (
                result.rescue_rms < _CLOSE_RECOVERY_MIN_RESCUE_RMS
                and result.rescue_peak_abs < _CLOSE_RECOVERY_MIN_RESCUE_PEAK_ABS
            )
        snapshot_getter = getattr(context.provider, "current_asr_audio_task_snapshot", None)
        snapshot = snapshot_getter() if callable(snapshot_getter) else None
        if snapshot is None or snapshot.task_epoch != result.task_epoch:
            return False
        rms = snapshot.rms
        peak_abs = snapshot.peak_abs
        if rms is None or peak_abs is None:
            return False
        rescue = getattr(getattr(context.provider, "config", None), "rescue_config", None)
        if rescue is None:
            rescue = getattr(
                getattr(getattr(context.provider, "_asr", None), "config", None),
                "rescue_config",
                None,
            )
        min_rms = int(getattr(rescue, "min_rms", 100))
        min_peak_abs = int(getattr(rescue, "min_peak_abs", 350))
        return bool(rms < min_rms and peak_abs < min_peak_abs)

    def _log_asr_rejection(
        self,
        session_id: str,
        result: ASRResult,
        reason: ASRDecisionReason,
        *,
        stage: str,
        overlap: ASROverlapEvidence | None = None,
    ) -> None:
        # Diagnostics: a dropped provider result is otherwise metric-only.  A
        # final rejection is loud; partial revisions stay at INFO because they
        # are expected churn on a healthy stream.  A rejected final also says
        # whether it was a spoken stop heard during playback (no text).
        level = logging.WARNING if result.is_final else logging.INFO
        context = self._sessions.get(session_id)
        diagnosis = (
            self._playback_stop_diagnosis(context, result)
            if result.is_final and context is not None
            else ""
        )
        logger.log(
            level,
            "media ASR result rejected session=%s stage=%s reason=%s is_final=%s "
            "text_len=%s task_epoch=%s stream_epoch=%s samples=%s-%s "
            "rescue_synthesized=%s %s overlap=%s",
            session_id,
            stage,
            reason.value,
            result.is_final,
            len(result.text),
            result.task_epoch,
            result.stream_epoch,
            result.capture_start_sample,
            result.capture_end_sample,
            result.rescue_synthesized,
            diagnosis,
            json.dumps(asdict(overlap) if overlap is not None else None, separators=(",", ":")),
        )

    def _reject_accepted_final(
        self,
        session_id: str,
        result: ASRResult,
        reason: ASRDecisionReason,
        *,
        stage: str,
    ) -> ASRAcceptDecision:
        """A result the supervisor accepted but the session dropped; finals were silent."""

        if result.is_final:
            self._log_asr_rejection(session_id, result, reason, stage=stage)
        return ASRAcceptDecision(None, reason)
