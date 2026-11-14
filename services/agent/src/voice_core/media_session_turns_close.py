"""Conversation-close early-commit mixin for the media turn endpoint.

``MediaTurnConversationCloseMixin`` decides when a farewell heard while the
reply still plays may endpoint the turn early, and schedules the semantic
evaluation that confirms it.  Session state and the endpoint helpers stay
owned by ``MediaTurnEndpointMixin`` and are declared here for type checking.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from services.agent.src.voice_core.speech_timeline import ASRResult

if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

logger = logging.getLogger(__name__)


class MediaTurnConversationCloseMixin:
    """Early-endpoint a conversation-close farewell heard during playback."""

    if TYPE_CHECKING:
        _sessions: dict[str, _MediaVoiceSession]

        def _pin_conversation_close_endpoint(
            self,
            context: _MediaVoiceSession,
            capture_end_sample: int,
            *,
            text: str,
            source: str,
            result: ASRResult | None = None,
        ) -> None: ...

        def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None: ...


    def _schedule_conversation_close_semantic_evaluation(
        self,
        context: _MediaVoiceSession,
        *,
        text: str,
        capture_end_sample: int,
        source: str,
    ) -> None:
        if (
            context.pending.conversation_close_semantic_task is not None
            and context.pending.conversation_close_semantic_text == text
        ):
            return
        context.pending.cancel_close_semantic()
        stream_epoch = context.stream_epoch
        context.pending.conversation_close_semantic_text = text
        context.pending.conversation_close_semantic_task = asyncio.create_task(
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
        pending_floor = context.pending.pending_turn_onset_floor
        try:
            if (
                context.closed
                or context.stream_epoch != stream_epoch
                or context.pending.turn_endpoint_sample is not None
            ):
                return
            needed = await context.runtime.resolve_conversation_close_needed(text, log_wait=False)
            if not needed:
                return
            if (
                context.closed
                or context.stream_epoch != stream_epoch
                or context.pending.turn_endpoint_sample is not None
                or context.pending.pending_turn_onset_floor != pending_floor
                or context.pending.conversation_close_semantic_task is not asyncio.current_task()
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
                text=text,
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
            if context.pending.conversation_close_semantic_task is asyncio.current_task():
                context.pending.conversation_close_semantic_task = None
                context.pending.conversation_close_semantic_text = None

    def _maybe_early_commit_conversation_close(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
    ) -> None:
        """Commit farewell/standby turns without waiting for a stuck device VAD end."""

        if not result.is_final or context.identity.client_type != "device":
            return
        if context.pending.turn_endpoint_sample is not None:
            return
        text = result.text.strip()
        if not text:
            return
        if context.runtime.conversation_close_needed(text):
            self._pin_conversation_close_endpoint(
                context,
                result.capture_end_sample,
                text=text,
                source="final",
                result=result,
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
        # Stability is not finality: a later ASR revision may turn a farewell
        # prefix into a question. Only provider finals may endpoint a close.
        context.pending.conversation_close_partial_text = None
        context.pending.conversation_close_partial_stable_since = None
        context.pending.cancel_close_semantic()
