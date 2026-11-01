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
from dataclasses import replace
from typing import TYPE_CHECKING

from services.agent.src.voice_core.speech_timeline import ASRResult

if TYPE_CHECKING:
    from services.agent.src.voice_core.asr_stream_supervisor import ASRAcceptDecision
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

logger = logging.getLogger(__name__)


class MediaFinalMissingFallbackMixin:
    if TYPE_CHECKING:
        turn_final_missing_fallback_s: float
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

        def _turn_commit_retry_matches(
            self, context: _MediaVoiceSession, stream_epoch: int, endpoint_sample: int
        ) -> bool: ...

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
