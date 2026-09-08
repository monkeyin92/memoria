"""ASR acceptance and sample-range turn commit for Media Voice."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from services.agent.src.clock_fact_queries import is_clock_fact_query
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.conversation_projection import (
    CommitEvidence,
    CommittedTurn,
    ProjectionPatch,
    ProjectionRejectReason,
    SpeakerEvidence,
    TurnPhase,
)
from services.agent.src.orchestration.conversation_projection_range import (
    align_provisional_range,
)
from services.agent.src.orchestration.interaction_plane import (
    InteractionEvent,
    InteractionSnapshot,
)
from services.agent.src.voice_core.asr_stream_supervisor import (
    ASRAcceptDecision,
    ASRDecisionReason,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.interruption import (
    InterruptionPolicy,
    evidence_from_speech_segment,
)
from services.agent.src.voice_core.media_protocol import should_pause_asr_for_playback
from services.agent.src.voice_core.media_session_state import (
    MediaVoiceSessionState as _MediaVoiceSession,
)
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
    asr_result_to_segment,
)

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer

logger = logging.getLogger(__name__)
media_pb2: Any = _media_pb2


def _preferred_clock_fact_text(
    context: _MediaVoiceSession,
    *,
    stream_epoch: int,
    start_sample: int,
    end_sample: int,
) -> str | None:
    forced = context.clock_fact_forced_text
    if forced and is_clock_fact_query(forced):
        return forced
    candidates = [
        segment.text.strip()
        for segment in context.runtime.speech_timeline.segments_in_range(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        if segment.text.strip() and is_clock_fact_query(segment.text)
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


def _preferred_live_query_text(context: _MediaVoiceSession) -> str | None:
    forced = context.live_query_forced_text
    if forced and context.runtime.live_lookup_needed(forced):
        return forced
    return None


def _resolve_media_turn_text(
    context: _MediaVoiceSession,
    *,
    stream_epoch: int,
    start_sample: int,
    end_sample: int,
) -> str | None:
    """Resolve authoritative media commit text for one sample range."""

    text = context.runtime.project_media_user_turn(
        stream_epoch=stream_epoch,
        start_sample=start_sample,
        end_sample=end_sample,
    )
    preferred_clock = _preferred_clock_fact_text(
        context,
        stream_epoch=stream_epoch,
        start_sample=start_sample,
        end_sample=end_sample,
    )
    preferred_live = _preferred_live_query_text(context)
    if preferred_live:
        # An authoritative forced text means the in-range timeline text comes
        # from a blocking interval (e.g. playback echo that overlapped the
        # user final), so length comparison against it is meaningless.
        if context.live_query_forced_authoritative:
            return preferred_live
        if not text or len(preferred_live.strip()) > len(text.strip()):
            return preferred_live
    if not preferred_clock:
        return text
    if not text or not is_clock_fact_query(text):
        return preferred_clock
    if text.strip() != preferred_clock:
        return preferred_clock
    return text


class MediaSessionCommitMixin:
    """Commit a single sample range through Projection and Runtime fences."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        interruption_policy: InterruptionPolicy
        _sessions: dict[str, _MediaVoiceSession]

        def _stream_epoch_is_current(
            self, context: _MediaVoiceSession, stream_epoch: int
        ) -> bool: ...

        async def _apply_projection_segment(
            self, context: _MediaVoiceSession, segment: SpeechSegment
        ) -> None: ...

        async def _discard_projection(self, context: _MediaVoiceSession, reason: str) -> None: ...

        def _event_versions(
            self, context: _MediaVoiceSession, fence: GenerationFence
        ) -> tuple[int, int]: ...

        def _projection_speaker_evidence(self, context: _MediaVoiceSession) -> SpeakerEvidence: ...

        async def _emit_projection_patch(
            self, context: _MediaVoiceSession, patch: ProjectionPatch
        ) -> None: ...

        def _observe_final_asr_result(
            self, context: _MediaVoiceSession, result: ASRResult
        ) -> None: ...

        def _finish_owner_silence_turn(
            self, context: _MediaVoiceSession, *, accepted: bool
        ) -> None: ...

        def _nudge_missed_hearing(
            self,
            context: _MediaVoiceSession,
            *,
            endpoint_sample: int | None = None,
        ) -> None: ...

        def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None: ...

        def _arm_live_query_forced_endpoint(
            self, context: _MediaVoiceSession, result: ASRResult
        ) -> None: ...

        @staticmethod
        def _reply_in_flight(context: _MediaVoiceSession) -> bool: ...

        def _maybe_early_commit_clock_fact(
            self, context: _MediaVoiceSession, result: ASRResult
        ) -> None: ...

        def _maybe_early_commit_live_lookup(
            self, context: _MediaVoiceSession, result: ASRResult
        ) -> None: ...

        def _maybe_early_commit_conversation_close(
            self, context: _MediaVoiceSession, result: ASRResult
        ) -> None: ...

        def _maybe_early_commit_stable_clock_fact_partial(
            self, context: _MediaVoiceSession
        ) -> None: ...

        def _maybe_early_commit_stable_live_lookup_partial(
            self, context: _MediaVoiceSession
        ) -> None: ...

        def _maybe_early_commit_stable_conversation_close_partial(
            self, context: _MediaVoiceSession
        ) -> None: ...

        def _observe_committed_conversation_turn(
            self,
            context: _MediaVoiceSession,
            committed: CommittedTurn,
            previous_phase: TurnPhase,
        ) -> None: ...

        async def _request_device_standby(
            self, context: _MediaVoiceSession, *, reason: str
        ) -> bool: ...

        def _observe_partial_asr_result(
            self, context: _MediaVoiceSession, result: ASRResult
        ) -> None: ...

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
        if context is None or context.closed or context.standby_requested:
            return ASRAcceptDecision(None, ASRDecisionReason.SESSION_NOT_FOUND)
        preview = context.asr.preview_result(result)
        candidate = preview.accepted
        if candidate is None:
            if result.is_final:
                self.metrics.inc_media_stale_asr_final()
            self._log_asr_rejection(session_id, result, preview.reason, stage="preview")
            if result.is_final:
                await self._recover_rejected_semantic_final(
                    context,
                    session_id=session_id,
                    result=result,
                    reason=preview.reason,
                )
            return preview
        candidate_segment = asr_result_to_segment(candidate, session_id=session_id)
        if not context.runtime.speech_timeline.can_add(candidate_segment):
            runtime_task_epoch = context.runtime.speech_timeline.latest_task_epoch(
                candidate.stream_epoch
            )
            if runtime_task_epoch > 0:
                context.asr.observe_task(runtime_task_epoch)
            self._log_asr_rejection(
                session_id, result, ASRDecisionReason.INTERVAL_CONFLICT, stage="timeline"
            )
            return ASRAcceptDecision(None, ASRDecisionReason.INTERVAL_CONFLICT)
        decision = context.asr.accept_result(result, session_id=session_id)
        accepted = decision.accepted
        if accepted is None:
            if result.is_final:
                self.metrics.inc_media_stale_asr_final()
            self._log_asr_rejection(session_id, result, decision.reason, stage="accept")
            if result.is_final:
                await self._recover_rejected_semantic_final(
                    context,
                    session_id=session_id,
                    result=result,
                    reason=decision.reason,
                )
            return decision
        if decision.evicted_sentence_ids:
            context.runtime.speech_timeline.evict_segment_ids(
                set(decision.evicted_sentence_ids)
            )
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
        if accepted.is_final:
            self._observe_final_asr_result(context, accepted)
        else:
            self._observe_partial_asr_result(context, accepted)
            self._maybe_early_commit_stable_clock_fact_partial(context)
            self._maybe_early_commit_stable_live_lookup_partial(context)
            self._maybe_early_commit_stable_conversation_close_partial(context)
        return decision

    async def _recover_rejected_semantic_final(
        self,
        context: _MediaVoiceSession,
        *,
        session_id: str,
        result: ASRResult,
        reason: ASRDecisionReason,
    ) -> None:
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
        close_needed = await context.runtime.resolve_conversation_close_needed(text)
        live_lookup_needed = await context.runtime.resolve_live_lookup_needed(text)
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
        segment = asr_result_to_segment(result, session_id=session_id)
        if context.runtime.speech_timeline.can_add(segment):
            if context.runtime.ingest_media_speech_segment(segment):
                await self._apply_projection_segment(context, segment)
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
            context.clock_fact_forced_text = result.text.strip()
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
        text = result.text.strip()
        if not text:
            return
        close_needed = await context.runtime.resolve_conversation_close_needed(text)
        live_lookup_needed = await context.runtime.resolve_live_lookup_needed(text)
        if not (close_needed or live_lookup_needed):
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
        adjusted_start = max(committed, result.capture_start_sample)
        adjusted = replace(
            result,
            capture_start_sample=adjusted_start,
        )
        segment = asr_result_to_segment(adjusted, session_id=session_id)
        if context.runtime.speech_timeline.can_add(segment):
            if context.runtime.ingest_media_speech_segment(segment):
                await self._apply_projection_segment(context, segment)
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
        if close_needed and not live_lookup_needed:
            self._observe_final_asr_result(context, adjusted)
            self._maybe_early_commit_conversation_close(context, adjusted)
            if context.turn_endpoint_sample is not None:
                self._schedule_turn_commit(context)
            return
        context.live_query_forced_text = text
        if reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP:
            # The blocking interval's text (e.g. playback echo) stays on the
            # in-range timeline, so the recovered text must win commit-time
            # resolution unconditionally rather than by length.
            context.live_query_forced_authoritative = True
        # Forced text is already the authoritative commit text. Always observe
        # turn bounds; re-arm/commit only when no reply is already in flight.
        self._observe_final_asr_result(context, adjusted)
        if context.live_query_forced_authoritative:
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

    def _log_asr_rejection(
        self,
        session_id: str,
        result: ASRResult,
        reason: ASRDecisionReason,
        *,
        stage: str,
    ) -> None:
        # Diagnostics: a dropped provider result is otherwise metric-only.  A
        # final rejection is loud; partial revisions stay at INFO because they
        # are expected churn on a healthy stream.
        level = logging.WARNING if result.is_final else logging.INFO
        logger.log(
            level,
            "media ASR result rejected session=%s stage=%s reason=%s is_final=%s "
            "text_len=%s task_epoch=%s stream_epoch=%s samples=%s-%s",
            session_id,
            stage,
            reason.value,
            result.is_final,
            len(result.text),
            result.task_epoch,
            result.stream_epoch,
            result.capture_start_sample,
            result.capture_end_sample,
        )

    async def commit_user_turn(
        self,
        session_id: str,
        *,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
        retire_sample: int | None = None,
        provider_final_missing: bool = False,
    ) -> tuple[GenerationFence | None, str | None]:
        """Commit one explicit sample range, then create its authoritative turn."""

        context = self._sessions.get(session_id)
        if context is None or context.closed or context.standby_requested:
            return None, "session_not_found"
        if start_sample < 0 or end_sample <= start_sample:
            return None, "invalid_media_range"
        async with context.turn_commit_lock:
            if not self._stream_epoch_is_current(context, stream_epoch):
                return None, "stale_stream_epoch"
            result = await self._commit_user_turn_locked(
                context,
                session_id=session_id,
                stream_epoch=stream_epoch,
                start_sample=start_sample,
                end_sample=end_sample,
                retire_sample=retire_sample,
                provider_final_missing=provider_final_missing,
            )
        fence, reason = result
        if reason == "conversation_end_explicit":
            await self._request_device_standby(context, reason=reason)
        else:
            self._finish_owner_silence_turn(context, accepted=fence is not None)
        return result

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

    async def _reproject_timeline_range(
        self,
        context: _MediaVoiceSession,
        *,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
    ) -> None:
        """Patch the live provisional from pending timeline facts in range."""

        for segment in context.runtime.speech_timeline.segments_in_range(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        ):
            await self._apply_projection_segment(context, segment)

    async def _commit_user_turn_locked(
        self,
        context: _MediaVoiceSession,
        *,
        session_id: str,
        stream_epoch: int,
        start_sample: int,
        end_sample: int,
        retire_sample: int | None,
        provider_final_missing: bool = False,
    ) -> tuple[GenerationFence | None, str | None]:
        """Prepare and project one turn while its transport epoch is stable."""

        # Compatibility callers may have populated the authoritative Timeline
        # directly before invoking this seam. Re-project those already-
        # accepted facts rather than letting a valid turn bypass Projection.
        await self._reproject_timeline_range(
            context,
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        text = _resolve_media_turn_text(
            context,
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        if not text:
            context.runtime.on_user_voice_stopped()
            await self._commit_media_input_range(
                context,
                session_id=session_id,
                stream_epoch=stream_epoch,
                start_sample=start_sample,
                end_sample=end_sample,
                retire_end=end_sample,
            )
            await self._discard_projection(context, "empty_media_turn")
            # No speaker classification has been awaited yet on this path, so
            # owner authority cannot be established and _nudge_missed_hearing
            # would refuse. The post-classification empty check below is the
            # only place an empty turn may prompt.
            return None, "empty_media_turn"
        retire_end = end_sample if retire_sample is None else retire_sample
        if retire_end < end_sample:
            raise ValueError("media retire sample cannot precede the logical endpoint")
        was_assistant_speaking = context.runtime.assistant_speaking
        context.runtime.on_user_voice_stopped()
        await context.runtime.await_speaker_classification()
        # Speaker classify yields. A late ASR final can land on the timeline
        # (or a VAD-first empty provisional can still be stale vs timeline
        # text). Refresh both sides from the same range before validate, or
        # commit dies as projection_text_mismatch with a usable transcript.
        await self._reproject_timeline_range(
            context,
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        text = _resolve_media_turn_text(
            context,
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
            self._nudge_missed_hearing(context)
            return None, "empty_media_turn"
        aligned = context.projection.align_provisional_text(text)
        if aligned is not None:
            logger.info(
                "media provisional text aligned session=%s stream_epoch=%s timeline_text_len=%s",
                session_id,
                stream_epoch,
                len(text),
            )
            await self._emit_projection_patch(context, aligned)
        range_aligned = align_provisional_range(context.projection, start_sample, end_sample)
        if range_aligned is not None:
            logger.info(
                "media provisional range aligned session=%s stream_epoch=%s "
                "commit=%s-%s provisional=%s-%s",
                session_id,
                stream_epoch,
                start_sample,
                end_sample,
                range_aligned.turn.capture_start_sample,
                range_aligned.turn.capture_end_sample,
            )
            await self._emit_projection_patch(context, range_aligned)
        speaker_evidence = self._projection_speaker_evidence(context)
        history_eligible = context.runtime.current_history_eligible
        commit_evidence = CommitEvidence(
            session_id=session_id,
            stream_epoch=stream_epoch,
            capture_start_sample=start_sample,
            capture_end_sample=end_sample,
            text=text,
            fence=context.runtime.fence.bump_turn(),
            speaker_evidence=speaker_evidence,
            history_eligible=history_eligible,
            provider_final_missing=provider_final_missing,
        )
        projection_rejection = context.projection.validate_commit(commit_evidence)
        if projection_rejection is not None:
            await self._discard_projection(context, projection_rejection.value)
            if projection_rejection is ProjectionRejectReason.TEXT_MISMATCH:
                self._nudge_missed_hearing(context)
            return None, projection_rejection.value
        speaker_patch = context.projection.apply_speaker_evidence(speaker_evidence)
        if speaker_patch is not None:
            await self._emit_projection_patch(context, speaker_patch)
        elapsed_ms = (end_sample - start_sample) * 1_000 // 16_000
        await context.runtime.resolve_conversation_close_needed(text)
        route = context.runtime.route_user_turn(text)
        guarded_reason = context.runtime.playback_guarded_reason(
            text,
            duration_ms=elapsed_ms,
        )
        interaction = context.runtime.decide_interaction(
            InteractionSnapshot(
                event=InteractionEvent.TRANSCRIPT,
                assistant_speaking=context.runtime.assistant_speaking,
                text=text,
                elapsed_ms=elapsed_ms,
                final=True,
                has_speech_energy=True,
                guarded_reason=guarded_reason,
                semantic_evidence=True,
                utterance_route=route,
                turn_phase=context.projection.phase,
            )
        )
        if was_assistant_speaking and guarded_reason is None:
            asr_segments = [
                segment
                for segment in context.runtime.speech_timeline.segments_in_range(
                    stream_epoch=stream_epoch,
                    start_sample=start_sample,
                    end_sample=end_sample,
                )
                if segment.kind in {SegmentKind.ASR_PARTIAL, SegmentKind.ASR_FINAL}
            ]
            if asr_segments:
                evidence_segment = max(
                    asr_segments,
                    key=lambda segment: (
                        segment.provider_task_epoch,
                        segment.revision,
                        segment.final,
                    ),
                )
                interruption = self.interruption_policy.evaluate(
                    evidence_from_speech_segment(
                        evidence_segment,
                        active_generation_id=max(
                            1,
                            (context.playback.current_fence or context.runtime.fence).generation_id,
                        ),
                        duration_ms=elapsed_ms,
                    ),
                    asr_text=text,
                    speaker_profile=(
                        "child"
                        if context.runtime.mode_policy.runtime_profile is not None
                        and context.runtime.mode_policy.runtime_profile.profile.subject_category
                        == "minor"
                        else "adult"
                    ),
                )
                interaction = replace(
                    interaction,
                    reason=interruption.reason,
                    duck_output=interruption.duck_output,
                    cancel_generation=interruption.cancel_generation,
                    continue_output=interruption.continue_output,
                    backchannel=interruption.backchannel,
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
                    previous_phase = context.projection.phase
                    recovered = context.projection.commit_turn(
                        replace(
                            commit_evidence,
                            fence=prepared_fence,
                            context_version=context_version,
                        )
                    )
                    if isinstance(recovered, CommittedTurn):
                        self._observe_committed_conversation_turn(
                            context,
                            recovered,
                            previous_phase,
                        )
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
        previous_phase = context.projection.phase
        commit_text = (
            _resolve_media_turn_text(
                context,
                stream_epoch=stream_epoch,
                start_sample=start_sample,
                end_sample=end_sample,
            )
            or commit_evidence.text
        )
        final_align = context.projection.align_provisional_text(commit_text)
        if final_align is not None:
            logger.info(
                "media provisional text aligned before commit session=%s stream_epoch=%s "
                "timeline_text_len=%s",
                session_id,
                stream_epoch,
                len(commit_text),
            )
            await self._emit_projection_patch(context, final_align)
        final_range = align_provisional_range(context.projection, start_sample, end_sample)
        if final_range is not None:
            logger.info(
                "media provisional range aligned before commit session=%s stream_epoch=%s "
                "commit=%s-%s",
                session_id,
                stream_epoch,
                start_sample,
                end_sample,
            )
            await self._emit_projection_patch(context, final_range)
        projection_result = context.projection.commit_turn(
            replace(
                commit_evidence,
                text=commit_text,
                fence=fence,
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
        self._observe_committed_conversation_turn(
            context,
            committed,
            previous_phase,
        )
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
        context.tts_started_ns = None
        context.first_audio_observed = False
        if should_pause_asr_for_playback(context.identity):
            await context.provider.pause_asr_for_playback(context.identity)
        task_epoch, context_version = self._event_versions(context, fence)
        generation_started = await self.bridge.emit_generation(
            session_id,
            fence,
            action=media_pb2.GENERATION_ACTION_START,
            reason="user_turn_committed",
            task_epoch=task_epoch,
            context_version=context_version,
        )
        if not generation_started:
            # Without an accepted START the transport generation gate stays
            # closed and every reply PCM frame is rejected downstream.  Make
            # that failure loud instead of surfacing only as transport_rejected.
            logger.warning(
                "media generation START not accepted session=%s turn=%s generation=%s "
                "tool_epoch=%s stream_epoch=%s",
                session_id,
                fence.turn_id,
                fence.generation_id,
                fence.tool_epoch,
                context.stream_epoch,
            )
        return fence, None
