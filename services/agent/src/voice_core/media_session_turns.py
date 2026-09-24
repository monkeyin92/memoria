"""VAD/ASR endpoint coordination for one Media Voice session."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from services.agent.src.clock_fact_queries import is_clock_fact_query
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.conversation_projection import ProjectionPatch
from services.agent.src.providers.funasr_empty_accounting import classify_funasr_empty_outcome
from services.agent.src.voice_core.asr_stream_supervisor import ASRAcceptDecision
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputState,
    OutputDispatchResult,
    OutputDispatchStatus,
    OutputWork,
    owned_delegation_holds_turn,
)
from services.agent.src.voice_core.speech_timeline import ASRResult

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
# Conservative sample-gap policy for unanchored device candidates observed
# during a previous reply, not a VAD silence measurement or endpoint timeout.
_PLAYBACK_CANDIDATE_SPLIT_GAP_SAMPLES = 40_000  # 2.5 s at 16 kHz
# A follow-up final that starts wholly after the playback boundary is user
# speech, not echo: endpoint it with a short grace instead of waiting for a
# VAD edge that a stuck post-playback VAD may never emit (run 20260921).
_PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S = 1.2
_CLOCK_FACT_PARTIAL_STABLE_S = 0.6
_CONVERSATION_CLOSE_PARTIAL_STABLE_S = 0.6
_LIVE_LOOKUP_PARTIAL_STABLE_S = 0.6
_DUPLICATE_COMMIT_DELIVERY_GUARD_S = 8.0
# Bound how long admitted output may wait behind a pending turn that has no
# text evidence at all.  Run 2026-09-24 session b18fede9: a ready weather
# answer waited ~8 s because room-noise VAD bursts under 2.5 s apart kept
# re-opening one empty pending turn (each vad.start resets the tail timeout)
# while FunASR and the rescue both heard nothing.  Real speech yields a
# partial well within the per-VAD extension, and any text evidence cancels
# the cap, so only evidence-less holds are retired early.
_EVIDENCE_LESS_HOLD_BASE_S = 3.0
_EVIDENCE_LESS_HOLD_VAD_EXTENSION_S = 1.5
_EVIDENCE_LESS_HOLD_MAX_S = 6.0


class MediaTurnEndpointMixin:
    """Coordinate VAD endpoints with ASR finals for one media session."""

    def _classify_provider_final_missing(self, context: _MediaVoiceSession) -> str:
        rms = 0
        min_rms = 100
        provider = getattr(context, "provider", None)
        asr_session = getattr(provider, "_asr", None)
        rms_raw = getattr(asr_session, "task_pcm_rms", None)
        if rms_raw is not None:
            rms = int(rms_raw)
        rescue = getattr(getattr(asr_session, "config", None), "rescue_config", None)
        if rescue is not None:
            min_rms = int(rescue.min_rms)
        empty_audio = False
        has_empty = getattr(asr_session, "_has_replaceable_empty_audio_failure", None)
        if callable(has_empty):
            empty_audio = bool(has_empty())
        outcome = classify_funasr_empty_outcome(
            rms=rms,
            min_rms=min_rms,
            empty_audio_error=empty_audio,
            pcm_gated=context.turn_start_sample is None and rms == 0,
        )
        self.metrics.inc_funasr_empty_transcript(outcome)
        return outcome

    """Own the bounded VAD/ASR endpoint state behind the registry interface."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        turn_endpoint_grace_s: float
        turn_endpoint_min_grace_s: float
        turn_endpoint_max_grace_s: float
        turn_endpoint_absolute_timeout_s: float
        _sessions: dict[str, _MediaVoiceSession]

        def _schedule_output_retry(self, context: _MediaVoiceSession) -> None: ...

        def _output_work_is_active(
            self, context: _MediaVoiceSession, work: OutputWork
        ) -> bool: ...

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

        async def _request_device_standby(
            self, context: _MediaVoiceSession, *, reason: str,
            expected_endpoint: tuple[int, int] | None = None,
        ) -> bool: ...

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

        def _nudge_missed_hearing(
            self,
            context: _MediaVoiceSession,
            *,
            endpoint_sample: int | None = None,
        ) -> None: ...

        async def _dispatch_reply(
            self,
            session_id: str,
            user_text: str,
            fence: GenerationFence,
        ) -> OutputDispatchResult: ...

        async def _abort_committed_turn(
            self, context: _MediaVoiceSession, fence: GenerationFence, *, reason: str,
        ) -> None: ...

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

        if result.capture_end_sample > context.last_asr_evidence_end_sample:
            # Position evidence only: rejected or abandoned text never flows
            # from this watermark, but the playback boundary snapshot needs
            # the freshest uplink position the ASR chain has proven.
            context.last_asr_evidence_end_sample = result.capture_end_sample
        floor = context.pending_turn_onset_floor
        if floor is not None and result.capture_start_sample < floor:
            return
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
        self._split_pending_turn_at_unvoiced_gap(context, result)
        onset = min(
            result.capture_start_sample,
            context.turn_start_sample
            if context.turn_start_sample is not None
            else result.capture_start_sample,
        )
        floor = context.pending_turn_onset_floor
        context.turn_start_sample = onset if floor is None else max(floor, onset)
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
        self._maybe_early_commit_live_lookup(context, result)
        self._maybe_early_commit_conversation_close(context, result)
        self._maybe_endpoint_playback_followup(context, result)
        # A provider final is evidence, never the endpoint itself. If VAD has
        # already ended, a late final re-arms the same logical-turn commit.
        if context.turn_endpoint_sample is not None:
            self._schedule_turn_commit(context)

    def _maybe_endpoint_playback_followup(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
    ) -> None:
        """Endpoint a post-playback follow-up without waiting for a VAD edge.

        Run 20260921 window-a: both follow-ups were recognized in realtime,
        but the device VAD stayed active across the playback echo, the pending
        turn merged echo and follow-up text, and the offline paragraphs were
        rejected for straddling the committed boundary -- so the endpoint
        waited ~20 s for the next vad.start.  When playback is over and an
        accepted final lies wholly after the playback boundary, it is user
        speech, not echo: pin the endpoint with a short grace instead of
        waiting for a VAD edge that may never arrive.
        """

        if context.identity.client_type != "device":
            return
        playback_end = context.last_playback_end_sample
        if playback_end is None or result.capture_start_sample < playback_end:
            # Echo guard: a final that begins before the playback boundary may
            # still be the reply's tail on the uplink and must never endpoint
            # a turn by itself.
            return
        if self._reply_in_flight(context):
            return
        if context.turn_endpoint_sample is not None:
            # Only an endpoint this path pinned may be extended while the
            # utterance keeps producing post-boundary finals; a VAD end or a
            # clock-fact/live-query pin already owns the tail.
            if (
                context.playback_followup_endpoint_sample
                == context.turn_endpoint_sample
                and result.capture_start_sample >= context.turn_endpoint_sample
            ):
                endpoint = max(result.capture_end_sample, context.turn_end_sample or 0)
                context.turn_endpoint_sample = endpoint
                context.turn_retire_sample = max(context.turn_retire_sample or 0, endpoint)
                context.playback_followup_endpoint_sample = endpoint
                context.turn_endpoint_grace_deadline = (
                    time.monotonic() + _PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S
                )
                logger.info(
                    "media playback-followup endpoint advanced session=%s "
                    "boundary=%s endpoint=%s text_len=%s",
                    context.identity.session_id,
                    playback_end,
                    endpoint,
                    len(result.text.strip()),
                )
                self._schedule_turn_commit(context)
            return
        if (
            context.turn_start_sample is not None
            and context.turn_start_sample < playback_end
        ):
            # The pending window still reaches back into the echo window; the
            # playback-boundary split owns resetting it before this may fire.
            return
        endpoint = max(result.capture_end_sample, context.turn_end_sample or 0)
        context.turn_endpoint_sample = endpoint
        context.turn_retire_sample = max(context.turn_retire_sample or 0, endpoint)
        context.playback_followup_endpoint_sample = endpoint
        context.turn_endpoint_grace_deadline = (
            time.monotonic() + _PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S
        )
        logger.info(
            "media playback-followup endpoint session=%s boundary=%s "
            "endpoint=%s text_len=%s",
            context.identity.session_id,
            playback_end,
            endpoint,
            len(result.text.strip()),
        )
        self._schedule_turn_commit(context)

    def _split_pending_turn_at_unvoiced_gap(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
    ) -> None:
        """Bound an unanchored candidate after the reply ended.

        Two policies close the abandoned echo window: a completed playback
        boundary (a final that starts wholly after it begins a new turn even
        while the echo-holdover VAD is still marked active), and the original
        conservative unvoiced-gap fallback when no boundary was recorded.
        Neither may segment ordinary long pauses or an endpointed turn; the
        resulting sample fence also applies before ASR/rescue/VAD ingest.
        """

        if context.identity.client_type != "device":
            return
        if self._reply_in_flight(context):
            if context.turn_endpoint_sample is None:
                context.pending_turn_playback_overlap = True
            return
        if not context.pending_turn_playback_overlap:
            return
        if context.turn_end_sample is None:
            return
        if (
            context.turn_endpoint_sample is not None
            or context.live_query_forced_text
            or context.clock_fact_forced_text
        ):
            return
        playback_end = context.last_playback_end_sample
        boundary_split = (
            playback_end is not None
            and result.capture_start_sample >= playback_end
        )
        if not boundary_split:
            if context.active_vad_start_sample is not None:
                return
            if (
                result.capture_start_sample - context.turn_end_sample
                <= _PLAYBACK_CANDIDATE_SPLIT_GAP_SAMPLES
            ):
                return
        logger.warning(
            "media pending turn split after reply session=%s "
            "stream_epoch=%s boundary=%s gap_samples=%s pending=%s-%s final=%s-%s",
            context.identity.session_id,
            result.stream_epoch,
            "playback_end" if boundary_split else "unvoiced_gap",
            result.capture_start_sample - context.turn_end_sample,
            context.turn_start_sample,
            context.turn_end_sample,
            result.capture_start_sample,
            result.capture_end_sample,
        )
        # Drop even a cached partial crossing the boundary: its text cannot
        # safely be sliced without word timing. Do not advance commit history.
        timeline = context.runtime.speech_timeline
        timeline.evict_segment_ids({
            segment.segment_id
            for segment in timeline.segments_in_range(
                stream_epoch=result.stream_epoch,
                start_sample=0,
                end_sample=result.capture_start_sample,
            )
        })
        context.turn_start_sample = None
        context.turn_end_sample = None
        context.pending_partial = None
        context.clock_fact_partial_text = None
        context.clock_fact_partial_stable_since = None
        context.live_query_partial_text = None
        context.live_query_partial_stable_since = None
        context.conversation_close_partial_text = None
        context.conversation_close_partial_stable_since = None
        self._cancel_conversation_close_semantic_task(context)
        context.pending_turn_playback_overlap = False
        context.pending_turn_onset_floor = result.capture_start_sample

    @staticmethod
    def _reply_in_flight(context: _MediaVoiceSession) -> bool:
        """True when a reply is synthesizing, locked, or holding output."""

        task = context.reply_task
        return bool(
            context.runtime.assistant_speaking
            or context.output_owner is not None
            or context.reply_lock.locked()
            or (task is not None and not task.done())
        )

    @staticmethod
    def _delegation_output_pending(context: _MediaVoiceSession) -> bool:
        """True while a delegated answer owns output but is not yet delivered.

        Once a live-lookup turn hands its playback to a delegation the cue's
        reply task has already finished, so ``_reply_in_flight`` goes false while
        the deep answer is still being produced.  Re-committing the same text in
        that window supersedes the pending answer (epoch 1912).  A ``PENDING`` or
        ``OWNED`` claim means ownership has not yet resolved into a delivered
        result (``COMPLETED``) or a local fallback (``RELEASED``), so that window
        must count as still in flight.  This is deliberately narrower than
        ``_reply_in_flight`` so the early-commit paths keep their existing
        semantics.
        """

        return any(
            claim.state in {DelegationOutputState.PENDING, DelegationOutputState.OWNED}
            for claim in context.delegation_output_claims.values()
        )

    @staticmethod
    def _reply_or_delegation_pending(context: _MediaVoiceSession) -> bool:
        """True while a reply is audible or a delegated answer is undelivered."""

        return MediaTurnEndpointMixin._reply_in_flight(context) or (
            MediaTurnEndpointMixin._delegation_output_pending(context)
        )

    @staticmethod
    def _same_text_turn_output_pending(
        context: _MediaVoiceSession,
        normalized_text: str,
    ) -> bool:
        """Keep a completed delegation from reopening its undelivered turn."""

        if (
            not context.last_committed_turn_text
            or normalized_text != context.last_committed_turn_text
        ):
            return False
        if MediaTurnEndpointMixin._reply_or_delegation_pending(context):
            return True
        committed_fence = context.last_committed_turn_fence
        committed_at = context.last_committed_turn_at
        if committed_fence is None or committed_at is None:
            return False
        if time.monotonic() - committed_at >= _DUPLICATE_COMMIT_DELIVERY_GUARD_S:
            return False
        delivery = context.reply_delivery.get(committed_fence)
        if delivery is not None:
            if delivery.actual_heard or delivery.terminal:
                return False
            return True
        owner = context.output_owner
        if owner is not None and owner.fence.turn_id == committed_fence.turn_id:
            return True
        return any(
            work.fence.turn_id == committed_fence.turn_id
            for work in context.output_work.values()
        )

    def _arm_live_query_forced_endpoint(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
    ) -> None:
        """Pin and commit a recovered live-query turn before VAD jitter can starve it.

        Mirrors clock-fact early endpoint pinning: authoritative forced text is
        already the commit text, so later VAD start/end must not move the
        endpoint past ASR coverage and silently defer forever.
        """

        if self._reply_in_flight(context):
            logger.warning(
                "media live-query forced endpoint skipped: reply in flight "
                "session=%s text_len=%s",
                context.identity.session_id,
                len(result.text.strip()),
            )
            return
        endpoint = max(
            result.capture_end_sample,
            context.turn_end_sample or 0,
            context.turn_endpoint_sample or 0,
        )
        if context.turn_start_sample is None:
            context.turn_start_sample = result.capture_start_sample
        context.turn_end_sample = max(context.turn_end_sample or 0, endpoint)
        context.turn_endpoint_sample = endpoint
        context.turn_retire_sample = max(context.turn_retire_sample or 0, endpoint)
        context.live_query_endpoint_pinned = endpoint
        context.turn_endpoint_grace_deadline = time.monotonic()
        logger.info(
            "media live-query forced endpoint session=%s endpoint=%s text_len=%s",
            context.identity.session_id,
            endpoint,
            len(result.text.strip()),
        )
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
        if MediaTurnEndpointMixin._reply_in_flight(context):
            logger.warning(
                "media early clock-fact commit skipped: reply in flight "
                "session=%s text_len=%s",
                context.identity.session_id,
                len(text),
            )
            return
        if context.turn_start_sample is None:
            context.turn_start_sample = result.capture_start_sample
        else:
            context.turn_start_sample = min(
                context.turn_start_sample,
                result.capture_start_sample,
            )
        endpoint = max(result.capture_end_sample, context.turn_end_sample or 0)
        context.turn_endpoint_sample = endpoint
        context.turn_end_sample = max(context.turn_end_sample or 0, endpoint)
        context.turn_retire_sample = endpoint
        context.clock_fact_endpoint_pinned = endpoint
        context.turn_endpoint_grace_deadline = time.monotonic()
        logger.info(
            "media early clock-fact endpoint session=%s endpoint=%s text_len=%s",
            context.identity.session_id,
            endpoint,
            len(text),
        )

    @staticmethod
    def _pin_live_lookup_endpoint(
        context: _MediaVoiceSession,
        capture_end_sample: int,
        *,
        text_len: int,
        source: str,
    ) -> None:
        endpoint = max(capture_end_sample, context.turn_end_sample or 0)
        context.turn_endpoint_sample = endpoint
        context.turn_end_sample = max(context.turn_end_sample or 0, endpoint)
        context.turn_retire_sample = endpoint
        context.live_query_endpoint_pinned = endpoint
        context.turn_endpoint_grace_deadline = time.monotonic()
        logger.info(
            "media early live-query endpoint session=%s endpoint=%s text_len=%s source=%s",
            context.identity.session_id,
            endpoint,
            text_len,
            source,
        )

    def _maybe_early_commit_live_lookup(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
    ) -> None:
        """Commit weather/live turns without waiting for a stuck device VAD end."""

        if context.identity.client_type != "device":
            return
        if context.turn_endpoint_sample is not None:
            return
        text = result.text.strip()
        if not text or not context.runtime.live_lookup_needed(text):
            return
        if self._reply_in_flight(context):
            logger.warning(
                "media early live-query commit skipped: reply in flight "
                "session=%s text_len=%s",
                context.identity.session_id,
                len(text),
            )
            return
        self._pin_live_lookup_endpoint(
            context,
            result.capture_end_sample,
            text_len=len(text),
            source="final",
        )

    def _maybe_early_commit_stable_live_lookup_partial(
        self,
        context: _MediaVoiceSession,
    ) -> None:
        partial = context.pending_partial
        if partial is None:
            context.live_query_partial_text = None
            context.live_query_partial_stable_since = None
            return
        text = partial.text.strip()
        if not text or not context.runtime.live_lookup_needed(text):
            context.live_query_partial_text = None
            context.live_query_partial_stable_since = None
            return
        now = time.monotonic()
        if context.live_query_partial_text == text:
            if context.live_query_partial_stable_since is None:
                context.live_query_partial_stable_since = now
            elif (
                context.turn_endpoint_sample is None
                and context.live_query_endpoint_pinned is None
                and now - context.live_query_partial_stable_since >= _LIVE_LOOKUP_PARTIAL_STABLE_S
            ):
                if self._reply_in_flight(context):
                    return
                self._pin_live_lookup_endpoint(
                    context,
                    partial.capture_end_sample,
                    text_len=len(text),
                    source="partial",
                )
                self._schedule_turn_commit(context)
        else:
            context.live_query_partial_text = text
            context.live_query_partial_stable_since = now

    @staticmethod
    def _pin_conversation_close_endpoint(
        context: _MediaVoiceSession,
        capture_end_sample: int,
        *,
        text_len: int,
        source: str,
    ) -> None:
        endpoint = max(capture_end_sample, context.turn_end_sample or 0)
        context.turn_endpoint_sample = endpoint
        context.turn_end_sample = max(context.turn_end_sample or 0, endpoint)
        context.turn_retire_sample = endpoint
        context.conversation_close_endpoint_pinned = endpoint
        context.turn_endpoint_grace_deadline = time.monotonic()
        logger.info(
            "media early conversation-close endpoint session=%s endpoint=%s "
            "text_len=%s source=%s",
            context.identity.session_id,
            endpoint,
            text_len,
            source,
        )

    @staticmethod
    def _cancel_conversation_close_semantic_task(
        context: _MediaVoiceSession,
    ) -> None:
        task = context.conversation_close_semantic_task
        if task is not None and not task.done():
            task.cancel()
        context.conversation_close_semantic_task = None
        context.conversation_close_semantic_text = None

    def _schedule_conversation_close_semantic_evaluation(
        self,
        context: _MediaVoiceSession,
        *,
        text: str,
        capture_end_sample: int,
        source: str,
    ) -> None:
        if (
            context.conversation_close_semantic_task is not None
            and context.conversation_close_semantic_text == text
        ):
            return
        self._cancel_conversation_close_semantic_task(context)
        stream_epoch = context.stream_epoch
        context.conversation_close_semantic_text = text
        context.conversation_close_semantic_task = asyncio.create_task(
            self._evaluate_conversation_close_early_commit(
                context,
                text=text,
                capture_end_sample=capture_end_sample,
                stream_epoch=stream_epoch,
                source=source,
            ),
            name=f"conversation-close-semantic-{context.identity.session_id}",
        )

    async def _evaluate_conversation_close_early_commit(
        self,
        context: _MediaVoiceSession,
        *,
        text: str,
        capture_end_sample: int,
        stream_epoch: int,
        source: str,
    ) -> None:
        pending_floor = context.pending_turn_onset_floor
        try:
            if (
                context.closed
                or context.stream_epoch != stream_epoch
                or context.turn_endpoint_sample is not None
            ):
                return
            needed = await context.runtime.resolve_conversation_close_needed(text)
            if not needed:
                return
            if (
                context.closed
                or context.stream_epoch != stream_epoch
                or context.turn_endpoint_sample is not None
                or context.pending_turn_onset_floor != pending_floor
                or context.conversation_close_semantic_task is not asyncio.current_task()
            ):
                logger.warning(
                    "media early conversation-close semantic skipped after resolve "
                    "session=%s text_len=%s source=%s",
                    context.identity.session_id,
                    len(text),
                    source,
                )
                return
            self._pin_conversation_close_endpoint(
                context,
                capture_end_sample,
                text_len=len(text),
                source=f"semantic_{source}",
            )
            self._schedule_turn_commit(context)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "media early conversation-close semantic failed session=%s text_len=%s source=%s",
                context.identity.session_id,
                len(text),
                source,
                exc_info=True,
            )
        finally:
            # A cancelled old evaluation may finish after a new one for the
            # same text was installed. Only its owning task may clear it.
            if context.conversation_close_semantic_task is asyncio.current_task():
                context.conversation_close_semantic_task = None
                context.conversation_close_semantic_text = None

    def _maybe_early_commit_conversation_close(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
    ) -> None:
        """Commit farewell/standby turns without waiting for a stuck device VAD end."""

        if context.identity.client_type != "device":
            return
        if context.turn_endpoint_sample is not None:
            return
        text = result.text.strip()
        if not text:
            return
        if context.runtime.conversation_close_needed(text):
            self._pin_conversation_close_endpoint(
                context,
                result.capture_end_sample,
                text_len=len(text),
                source="final",
            )
            return
        self._schedule_conversation_close_semantic_evaluation(
            context,
            text=text,
            capture_end_sample=result.capture_end_sample,
            source="final",
        )

    def _maybe_early_commit_stable_conversation_close_partial(
        self,
        context: _MediaVoiceSession,
    ) -> None:
        partial = context.pending_partial
        if partial is None:
            context.conversation_close_partial_text = None
            context.conversation_close_partial_stable_since = None
            self._cancel_conversation_close_semantic_task(context)
            return
        text = partial.text.strip()
        if not text:
            context.conversation_close_partial_text = None
            context.conversation_close_partial_stable_since = None
            self._cancel_conversation_close_semantic_task(context)
            return
        # A deterministic farewell is a terminal device command. Commit it at
        # the first accepted partial instead of waiting for another partial
        # revision and the 600 ms stability window. Real devices can lose the
        # following VAD end (or the provider final) while returning to idle;
        # the partial itself is enough to enter the existing terminal close
        # and playback-interruption path.
        if (
            context.identity.client_type == "device"
            and context.turn_endpoint_sample is None
            and context.runtime.conversation_close_needed(text)
        ):
            self._pin_conversation_close_endpoint(
                context,
                partial.capture_end_sample,
                text_len=len(text),
                source="partial_immediate",
            )
            self._schedule_turn_commit(context)
            return
        now = time.monotonic()
        if context.conversation_close_partial_text == text:
            if context.conversation_close_partial_stable_since is None:
                context.conversation_close_partial_stable_since = now
            elif (
                context.turn_endpoint_sample is None
                and context.conversation_close_endpoint_pinned is None
                and now - context.conversation_close_partial_stable_since
                >= _CONVERSATION_CLOSE_PARTIAL_STABLE_S
            ):
                if context.runtime.conversation_close_needed(text):
                    self._pin_conversation_close_endpoint(
                        context,
                        partial.capture_end_sample,
                        text_len=len(text),
                        source="partial",
                    )
                    self._schedule_turn_commit(context)
                else:
                    self._schedule_conversation_close_semantic_evaluation(
                        context,
                        text=text,
                        capture_end_sample=partial.capture_end_sample,
                        source="partial",
                    )
        else:
            context.conversation_close_partial_text = text
            context.conversation_close_partial_stable_since = now
            self._cancel_conversation_close_semantic_task(context)

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
                and context.clock_fact_endpoint_pinned is None
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
        if result.capture_end_sample > context.last_asr_evidence_end_sample:
            context.last_asr_evidence_end_sample = result.capture_end_sample
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
        if (
            context is None
            or context.closed
            or context.standby_requested
            or context.stream_epoch != stream_epoch
            or context.turn_endpoint_sample != endpoint_sample
        ):
            return
        context.turn_endpoint_timeout_handle = None
        deadline = context.turn_endpoint_tail_deadline
        if deadline is not None and time.monotonic() < deadline:
            # Timer callbacks can run slightly early. Keep the same deadline
            # rather than entering an unbounded wait on the retry task.
            self._arm_endpoint_tail_timeout(context, endpoint_sample)
            return
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
        context.admitted_input_stream_epoch = None
        context.active_vad_stream_epoch = None
        context.active_vad_start_sample = None
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
        context.turn_input_fence = None
        context.turn_end_sample = None
        context.turn_endpoint_sample = None
        context.turn_retire_sample = None
        context.turn_endpoint_grace_deadline = None
        context.turn_endpoint_tail_deadline = None
        context.pending_turn_playback_overlap = False
        context.pending_turn_onset_floor = None
        context.playback_followup_endpoint_sample = None
        context.committed_asr_keys.clear()
        context.pending_partial = None
        context.clock_fact_partial_text = None
        context.clock_fact_partial_stable_since = None
        context.clock_fact_endpoint_pinned = None
        context.conversation_close_partial_text = None
        context.conversation_close_partial_stable_since = None
        context.conversation_close_endpoint_pinned = None
        MediaTurnEndpointMixin._cancel_conversation_close_semantic_task(context)
        context.clock_fact_forced_text = None
        context.live_query_forced_text = None
        context.live_query_partial_text = None
        context.live_query_partial_stable_since = None
        context.live_query_endpoint_pinned = None
        context.live_query_forced_authoritative = False
        context.missed_hearing_nudge_count = 0
        context.last_missed_hearing_nudge_at = None

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
        commit_task = context.turn_commit_task
        deadline = context.turn_endpoint_tail_deadline
        if (
            deadline is not None
            and time.monotonic() >= deadline
            and (
                (commit_task is not None and not commit_task.done())
                or self._turn_commit_retry_matches(context, stream_epoch, endpoint_sample)
            )
        ):
            # Backoff count is not an I/O bound. Revoke admission before
            # cancellation/lock acquisition so even a late provider result
            # cannot publish or start output after the absolute deadline.
            await self._request_device_standby(
                context, reason="turn_prepare_timeout",
                expected_endpoint=(stream_epoch, endpoint_sample),
            )
            return
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
        resume_owned_output = False
        async with context.turn_commit_lock:
            current = self._sessions.get(session_id)
            if (
                current is not context
                or context.closed
                or context.stream_epoch != stream_epoch
                or context.turn_endpoint_sample != endpoint_sample
            ):
                return
            partial = context.pending_partial
            discarded, resume_owned_output = await self._retire_pending_turn_locked(
                context,
                stream_epoch=stream_epoch,
                endpoint_sample=endpoint_sample,
                floor_cause="empty_input_retired",
            )
        if discarded is not None:
            await self._emit_projection_patch(context, discarded)
        logger.warning(
            "media turn discarded after ASR tail timeout session=%s stream_epoch=%s endpoint=%s "
            "partial_present=%s partial_text_len=%s partial_end_sample=%s asr_empty_class=%s",
            session_id,
            stream_epoch,
            endpoint_sample,
            partial is not None,
            len(partial.text.strip()) if partial is not None else 0,
            partial.capture_end_sample if partial is not None else None,
            self._classify_provider_final_missing(context),
        )
        if endpoint_sample > 0 and not resume_owned_output:
            self._nudge_missed_hearing(context, endpoint_sample=endpoint_sample)

    @staticmethod
    def _pending_turn_has_text_evidence(context: _MediaVoiceSession) -> bool:
        partial = context.pending_partial
        provisional = context.projection.provisional
        return any(
            text and text.strip()
            for text in (
                partial.text if partial is not None else None,
                provisional.text if provisional is not None else None,
                context.clock_fact_forced_text,
                context.live_query_forced_text,
            )
        )

    async def _retire_pending_turn_locked(
        self,
        context: _MediaVoiceSession,
        *,
        stream_epoch: int,
        endpoint_sample: int,
        floor_cause: str,
    ) -> tuple[ProjectionPatch | None, bool]:
        """Discard the pending turn; hand the floor back if it held no text.

        Caller holds ``turn_commit_lock``.  Returns the projection discard
        patch and whether queued same-turn output was resumed.
        """

        input_fence = context.turn_input_fence
        empty_input = not self._pending_turn_has_text_evidence(context)
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
        resume_owned_output = False
        # A control/identity/generation change retires this placeholder;
        # only the exact originating fence may restore queued playback.
        if (
            empty_input
            and input_fence is not None
            and input_fence.matches(context.runtime.fence)
            and not context.closed
            and not context.standby_requested
            and not context.runtime.formal_speaker_enrollment_active
        ):
            self._clear_evidence_less_floor_hold(context)
            context.runtime.open_assistant_floor(cause=floor_cause)
            resume_owned_output = owned_delegation_holds_turn(
                context.delegation_output_claims, input_fence
            ) or any(
                self._output_work_is_active(context, work)
                for work in tuple(context.output_work.values())
            )
            self._schedule_output_retry(context)
            logger.info(
                "media empty input retired session=%s fence=%s resume_output=%s cause=%s",
                context.identity.session_id, input_fence, resume_owned_output, floor_cause,
            )
        if context.runtime.assistant_speaking:
            context.runtime.publish_assistant_audio("restore", gain=1.0)
        return discarded, resume_owned_output

    @staticmethod
    def _clear_evidence_less_floor_hold(context: _MediaVoiceSession) -> None:
        handle = context.evidence_less_hold_handle
        if handle is not None:
            handle.cancel()
        context.evidence_less_hold_handle = None
        context.evidence_less_hold_since = None

    def _arm_evidence_less_floor_hold(self, context: _MediaVoiceSession) -> None:
        """Start, or extend on vad.start, the cap on a floor-blocked wait.

        Each vad.start may push the cap to at most 1.5 s after itself, never
        past 6 s after the output first waited.  Expiry re-checks everything:
        text evidence, an open floor or no queued output leaves it inert.
        """

        if context.closed:
            return
        now = time.monotonic()
        since = context.evidence_less_hold_since
        if since is None:
            if context.runtime.output_floor_allows_assistant:
                return
            since = now
            context.evidence_less_hold_since = since
        deadline = min(
            since + _EVIDENCE_LESS_HOLD_MAX_S,
            max(since + _EVIDENCE_LESS_HOLD_BASE_S, now + _EVIDENCE_LESS_HOLD_VAD_EXTENSION_S),
        )
        handle = context.evidence_less_hold_handle
        if handle is not None:
            handle.cancel()
        context.evidence_less_hold_handle = asyncio.get_running_loop().call_later(
            max(0.0, deadline - now),
            self._start_evidence_less_hold_expiry,
            context.identity.session_id,
            context.stream_epoch,
        )

    def _start_evidence_less_hold_expiry(self, session_id: str, stream_epoch: int) -> None:
        context = self._sessions.get(session_id)
        if context is None or context.closed or context.stream_epoch != stream_epoch:
            return
        context.evidence_less_hold_handle = None
        asyncio.create_task(
            self._expire_evidence_less_floor_hold(session_id, stream_epoch),
            name=f"media-evidence-less-hold-{session_id}",
        )

    def _evidence_less_hold_applies(self, context: _MediaVoiceSession) -> bool:
        return (
            not context.closed
            and not context.standby_requested
            and not context.runtime.output_floor_allows_assistant
            and context.turn_start_sample is not None
            and not self._pending_turn_has_text_evidence(context)
            and any(
                self._output_work_is_active(context, work)
                for work in tuple(context.output_work.values())
            )
        )

    async def _expire_evidence_less_floor_hold(
        self,
        session_id: str,
        stream_epoch: int,
    ) -> None:
        context = self._sessions.get(session_id)
        if context is None or context.stream_epoch != stream_epoch:
            return
        if context.evidence_less_hold_handle is not None:
            # A vad.start re-armed the cap while this task was queued.
            return
        since = context.evidence_less_hold_since
        if since is None or not self._evidence_less_hold_applies(context):
            self._clear_evidence_less_floor_hold(context)
            return
        discarded: ProjectionPatch | None = None
        resume_owned_output = False
        endpoint_sample = 0
        async with context.turn_commit_lock:
            if (
                self._sessions.get(session_id) is not context
                or context.stream_epoch != stream_epoch
                or context.evidence_less_hold_handle is not None
                or not self._evidence_less_hold_applies(context)
            ):
                if context.evidence_less_hold_handle is None:
                    self._clear_evidence_less_floor_hold(context)
                return
            start_sample = context.turn_start_sample
            if start_sample is None:
                return
            endpoint_task = context.turn_endpoint_task
            if (
                endpoint_task is not None
                and endpoint_task is not asyncio.current_task()
                and not endpoint_task.done()
            ):
                endpoint_task.cancel()
            # The device VAD may still be open: retire everything uplinked so
            # far.  Later audio of the same burst straddles this watermark and
            # is rejected rather than re-opening the floor.
            endpoint_sample = max(
                start_sample + 1,
                context.turn_end_sample or 0,
                context.turn_endpoint_sample or 0,
                context.asr.last_sent_sample,
            )
            context.turn_end_sample = max(context.turn_end_sample or 0, endpoint_sample)
            context.turn_endpoint_sample = endpoint_sample
            context.turn_retire_sample = max(context.turn_retire_sample or 0, endpoint_sample)
            discarded, resume_owned_output = await self._retire_pending_turn_locked(
                context,
                stream_epoch=stream_epoch,
                endpoint_sample=endpoint_sample,
                floor_cause="evidence_less_hold_capped",
            )
            self._clear_evidence_less_floor_hold(context)
        if discarded is not None:
            await self._emit_projection_patch(context, discarded)
        logger.warning(
            "media evidence-less floor hold capped session=%s stream_epoch=%s "
            "endpoint=%s waited_s=%.2f resume_output=%s",
            session_id,
            stream_epoch,
            endpoint_sample,
            time.monotonic() - since,
            resume_owned_output,
        )

    def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None:
        if context.closed or context.standby_requested:
            return
        task = context.turn_endpoint_task
        if task is not None and not task.done():
            task.cancel()
        endpoint_sample = context.turn_endpoint_sample
        if endpoint_sample is None:
            return
        self._arm_endpoint_tail_timeout(context, endpoint_sample)
        if self._turn_commit_retry_matches(
            context,
            context.stream_epoch,
            endpoint_sample,
        ):
            return
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
                and not context.standby_requested
                and context.stream_epoch == stream_epoch
                and context.turn_endpoint_sample == endpoint_sample
            )
            if not current_endpoint or context is None:
                return
            pinned_clock_fact = (
                context.clock_fact_endpoint_pinned is not None
                and context.clock_fact_endpoint_pinned == endpoint_sample
            )
            pinned_conversation_close = (
                context.conversation_close_endpoint_pinned is not None
                and context.conversation_close_endpoint_pinned == endpoint_sample
            )
            pinned_live_query = (
                context.live_query_endpoint_pinned is not None
                and context.live_query_endpoint_pinned == endpoint_sample
            )
            forced_live_query = bool(
                context.live_query_forced_authoritative and context.live_query_forced_text
            )
            if (
                not pinned_clock_fact
                and not pinned_conversation_close
                and not pinned_live_query
                and not forced_live_query
                and not self._asr_covers_endpoint(
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
                if context.live_query_forced_text or context.clock_fact_forced_text:
                    logger.warning(
                        "media turn commit deferred with forced text "
                        "session=%s stream_epoch=%s endpoint=%s turn_end=%s "
                        "live_forced=%s clock_forced=%s authoritative=%s",
                        session_id,
                        stream_epoch,
                        endpoint_sample,
                        context.turn_end_sample,
                        bool(context.live_query_forced_text),
                        bool(context.clock_fact_forced_text),
                        context.live_query_forced_authoritative,
                    )
                if context.runtime.assistant_speaking:
                    context.runtime.publish_assistant_audio("restore", gain=1.0)
                return
            # Shield past the grace sleep: late offline recovery re-arms the
            # same endpoint via ``_schedule_turn_commit``, which cancels the
            # prior grace task. Cancelling mid-classify/commit left the pin
            # live until ASR tail timeout discarded a weekday clock-fact turn
            # that already had timeline text (2026-09-03 epoch 1366).
            await asyncio.shield(self._commit_pending_turn(context))
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

        if context.closed or context.standby_requested:
            return "session_closed"
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
        # Direct/fallback commits share the same absolute deadline as the
        # scheduled endpoint. Rearming never grants a new retry interval.
        self._arm_endpoint_tail_timeout(context, endpoint_sample)
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
        owner = context.output_owner
        if (
            owner is not None
            and not context.runtime.barge_in_enabled
            and owner.fence.turn_id == fence.turn_id
        ):
            delivery = context.reply_delivery.get(owner.fence)
            if delivery is not None and delivery.first_frame_sent:
                logger.info(
                    "skip same-turn cancel of heard playback session=%s "
                    "owner_gen=%s new_gen=%s",
                    context.identity.session_id,
                    owner.fence.generation_id,
                    fence.generation_id,
                )
                return "heard_output_in_flight"
        # A final ASR result can arrive while the previous answer is still
        # synthesizing.  Wait for that task to release the per-session reply
        # lock before scheduling the new turn; otherwise ``generate_reply``
        # sees a locked session and silently drops a valid user turn.
        await self._cancel_reply_task(context, previous_fence)
        if context.closed or context.standby_requested:
            await self._abort_committed_turn(context, fence, reason="session_closed")
            return "session_closed"
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
        if context.closed or context.standby_requested:
            return
        self._arm_endpoint_tail_timeout(context, endpoint_sample)
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
