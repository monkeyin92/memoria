"""ASR acceptance and sample-range turn commit for Media Voice."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.conversation_projection import (
    CommitEvidence,
    CommittedTurn,
    ProjectionPatch,
    ProjectionRejectReason,
    SpeakerEvidence,
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

        async def _discard_projection(
            self, context: _MediaVoiceSession, reason: str
        ) -> None: ...

        def _event_versions(
            self, context: _MediaVoiceSession, fence: GenerationFence
        ) -> tuple[int, int]: ...

        def _projection_speaker_evidence(
            self, context: _MediaVoiceSession
        ) -> SpeakerEvidence: ...

        async def _emit_projection_patch(
            self, context: _MediaVoiceSession, patch: ProjectionPatch
        ) -> None: ...

        def _observe_final_asr_result(
            self, context: _MediaVoiceSession, result: ASRResult
        ) -> None: ...

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
        if accepted.is_final:
            self._observe_final_asr_result(context, accepted)
        else:
            self._observe_partial_asr_result(context, accepted)
        return decision

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
                provider_final_missing=provider_final_missing,
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
        provider_final_missing: bool = False,
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
        was_assistant_speaking = context.runtime.assistant_speaking
        context.runtime.on_user_voice_stopped()
        await context.runtime.await_speaker_classification()
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
            return None, projection_rejection.value
        speaker_patch = context.projection.apply_speaker_evidence(speaker_evidence)
        if speaker_patch is not None:
            await self._emit_projection_patch(context, speaker_patch)
        elapsed_ms = (end_sample - start_sample) * 1_000 // 16_000
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
                            (
                                context.playback.current_fence or context.runtime.fence
                            ).generation_id,
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
                    recovered = context.projection.commit_turn(
                        replace(
                            commit_evidence,
                            fence=prepared_fence,
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
            replace(
                commit_evidence,
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
