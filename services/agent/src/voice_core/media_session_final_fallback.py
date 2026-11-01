"""A soft endpoint tail: commit what ASR has heard when it never covers a late device VAD end.

``MediaFinalMissingFallbackMixin`` is composed into ``MediaVoiceCoreRegistry``.  A commit waits while ASR does not
cover the device VAD end, for a covering final that may still come; only the 6 s endpoint tail then committed
the accepted final or the stable partial, and a shorter tail closed the conversation under questions whose
commit was still preparing (turn_prepare_timeout, 2026-10-01).  On 2026-11-01 (device soak t023) the device
ended its VAD 4.2 s after the child stopped, so ASR never covered it: the turn sat until that tail, its commit
started there, and the session closed with the child's whole sentence unanswered.  This check runs
``turn_final_missing_fallback_s`` after the endpoint and commits what is there when no commit is running; the
hard tail and its standby are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from services.agent.src.voice_core.speech_timeline import ASRResult

if TYPE_CHECKING:
    from services.agent.src.voice_core.asr_stream_supervisor import ASRAcceptDecision
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

logger = logging.getLogger(__name__)

# Device VAD and provider word timestamps are independent clocks around the
# same 16 kHz capture. After the provider task-finished boundary proves no
# later sentence can arrive, permit at most 1.5 seconds of tail skew after
# firmware removes its known 900 ms AFE hangover. Real-device evidence still
# showed 1.14 seconds between FunASR's last word and the device voiced end;
# larger gaps keep the endpoint commit waiting for a covering final. The tail
# timeout then commits this turn's accepted final instead of dropping it, and
# the turn's own sample range is what stops an earlier sentence reaching a
# later turn.
_ENDPOINT_ASR_COVERAGE_TOLERANCE_SAMPLES = 24_000


class MediaFinalMissingFallbackMixin:
    if TYPE_CHECKING:
        turn_final_missing_fallback_s: float
        turn_endpoint_absolute_timeout_s: float

        def _arm_endpoint_tail_timeout(
            self, context: _MediaVoiceSession, endpoint_sample: int
        ) -> None: ...

        _sessions: dict[str, _MediaVoiceSession]

        async def _accept_asr_result_decision(
            self, session_id: str, result: ASRResult
        ) -> ASRAcceptDecision: ...

        async def _commit_pending_turn(
            self,
            context: _MediaVoiceSession,
            *,
            provider_final_missing: bool = False,
            schedule_prepare_retry: bool = True,
        ) -> str | None: ...

        async def _commit_final_before_tail(self, context: _MediaVoiceSession) -> bool: ...

    @staticmethod
    def _turn_commit_retry_matches(
        context: _MediaVoiceSession,
        stream_epoch: int,
        endpoint_sample: int,
    ) -> bool:
        task = context.pending.turn_commit_retry_task
        return bool(
            task is not None
            and not task.done()
            and context.pending.turn_commit_retry_stream_epoch == stream_epoch
            and context.pending.turn_commit_retry_endpoint_sample == endpoint_sample
        )

    @staticmethod
    def _words_wait_past_endpoint(
        context: _MediaVoiceSession, endpoint_sample: int, *, provider_final_missing: bool
    ) -> bool:
        """The endpoint range held no words but the child's words run on past it.

        Device soak 2026-11-01 t007: 0.2 s of noise right after a reply pinned the endpoint, the child's
        question landed after it with no new device VAD, and the endpoint commit found nothing.  The turn
        then sat until the 6 s tail of the noise's endpoint, whose commit started as the tail closed the
        session.  Committing to the end of the words right away is what that tail would have done.
        """

        return (
            not provider_final_missing
            and context.pending.turn_endpoint_sample == endpoint_sample
            and (context.pending.turn_end_sample or 0) > endpoint_sample
        )

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

    def _restart_tail_for_words_past_endpoint(
        self, context: _MediaVoiceSession, result: ASRResult
    ) -> None:
        """Start the absolute tail over while the child's words keep landing past the pinned endpoint.

        Device soak 2026-11-01 (t023 twice, t007): echo or noise pinned the endpoint a few seconds before the
        child spoke and the device sent no new VAD, so the 6 s tail still counted from that noise.  It came
        while the child's own commit was preparing and put the device on standby with the sentence
        unanswered.  The tail now counts from the child's last words; a prepare that hangs after them is
        still stopped by it.
        """

        pending = context.pending
        endpoint = pending.turn_endpoint_sample
        deadline = pending.turn_endpoint_tail_deadline
        if (
            endpoint is None
            or deadline is None
            or not result.text.strip()
            or result.capture_end_sample <= endpoint + _ENDPOINT_ASR_COVERAGE_TOLERANCE_SAMPLES
        ):
            return
        restarted = time.monotonic() + self.turn_endpoint_absolute_timeout_s
        if restarted <= deadline:
            return
        pending.turn_endpoint_tail_deadline = restarted
        handle = pending.turn_endpoint_timeout_handle
        if handle is not None:
            handle.cancel()
        pending.turn_endpoint_timeout_handle = None
        self._arm_endpoint_tail_timeout(context, endpoint)
        if pending.tail_restarted_endpoint != endpoint:
            pending.tail_restarted_endpoint = endpoint
            logger.info(
                "media endpoint tail restarted for words past the endpoint session=%s stream_epoch=%s "
                "endpoint=%s words_end=%s",
                context.identity.session_id,
                context.stream_epoch,
                endpoint,
                result.capture_end_sample,
            )

    def _arm_final_missing_fallback(
        self, context: _MediaVoiceSession, endpoint_sample: int
    ) -> None:
        pending = context.pending
        if (
            pending.final_missing_fallback_handle is not None
            or self.turn_final_missing_fallback_s <= 0
        ):
            return
        session_id = context.identity.session_id
        stream_epoch = context.stream_epoch
        pending.final_missing_fallback_handle = asyncio.get_running_loop().call_later(
            self.turn_final_missing_fallback_s,
            lambda: asyncio.create_task(
                self._commit_partial_when_final_missing(session_id, stream_epoch, endpoint_sample),
                name=f"media-final-missing-{session_id}-{endpoint_sample}",
            ),
        )

    async def _commit_partial_when_final_missing(
        self, session_id: str, stream_epoch: int, endpoint_sample: int
    ) -> None:
        context = self._sessions.get(session_id)
        if (
            context is None
            or context.closed
            or context.standby_requested
            or context.stream_epoch != stream_epoch
            or context.pending.turn_endpoint_sample != endpoint_sample
        ):
            return
        context.pending.final_missing_fallback_handle = None
        commit_task = context.turn_commit_task
        if (commit_task is not None and not commit_task.done()) or self._turn_commit_retry_matches(
            context, stream_epoch, endpoint_sample
        ):
            return
        committed = await self._commit_stable_partial_fallback(context, endpoint_sample)
        if not committed and context.pending.turn_endpoint_sample == endpoint_sample:
            committed = await self._commit_final_before_tail(context)
        if committed:
            logger.warning(
                "media turn committed at the soft endpoint tail session=%s stream_epoch=%s "
                "endpoint=%s waited_s=%.1f",
                session_id,
                stream_epoch,
                endpoint_sample,
                self.turn_final_missing_fallback_s,
            )

    async def _commit_stable_partial_fallback(
        self, context: _MediaVoiceSession, endpoint_sample: int
    ) -> bool:
        """Commit the turn from its stable partial; True once the commit moved past ``endpoint_sample``."""

        partial = context.pending.pending_partial
        if not (
            partial is not None
            and partial.text.strip()
            and partial.capture_end_sample > (context.pending.turn_start_sample or 0)
        ):
            return False
        # A stable partial inside this turn is safer than dropping a complete utterance when
        # the final marker is lost; a device VAD end can trail the last word (t014: 3.5 s).
        fallback_confidence = (
            0.35 if partial.confidence is None else max(0.2, min(0.75, partial.confidence * 0.75))
        )
        fallback = replace(
            partial,
            is_final=True,
            revision=partial.revision + 1,
            confidence=fallback_confidence,
        )
        decision = await self._accept_asr_result_decision(context.identity.session_id, fallback)
        if decision.accepted is None:
            return False
        context.pending.turn_end_sample = max(
            decision.accepted.capture_end_sample,
            context.pending.turn_end_sample or 0,
        )
        await self._commit_pending_turn(context, provider_final_missing=True)
        return context.pending.turn_endpoint_sample != endpoint_sample
