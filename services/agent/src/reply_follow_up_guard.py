"""Post-safety follow-up guard: drop reply text that dilutes a fixed safety script, and ask again when nothing was said.

Field incident 2026-11-18 safety card run ``devicetest/safety-20261118-225243-card5-overlay``, turn t010
(22:58 CST): after nine fixed safety scripts in the session, a misheard 「我想拿刀划自己」 (ASR slip, 8 chars)
routed ``none``.  The model reply came back as seven segments, and every one was dropped by the post-safety
follow-up filter in ``ReplyPipeline`` as repeating or diluting the safety script.  The reply text was not
logged; seven is the number of segments the escalation script itself splits into, so the model most likely
copied it.  Nothing was left, so the turn ended ``provider_completed_without_audio`` and the child heard
silence.  A follow-up made only of soothing words (「没关系，我陪着你。别担心。」) fails the same way.

``PostSafetyFollowUpGuard`` keeps the old closure's behavior inside ``admit``: when the guard is inactive the
text passes unchanged; when it is active, each segment is stripped of soothing filler (when ``block_soothing``)
and dropped when what is left repeats or dilutes the fixed script.  ``repeat_request`` ends the silence: when
the filter dropped at least one segment and the reply spoke nothing, the device says the approved
missed-hearing line ``REPEAT_REQUEST`` (``BRIDGE_PHRASES[2]``) so the child gets a prompt instead of quiet.
No new copy, no change to crisis routing, no change to which segments are dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from services.agent.src.prompts import BRIDGE_PHRASES
from services.common.companion_response_safety import (
    assistant_text_repeats_or_dilutes_safety,
    strip_soothing_from_follow_up,
)

logger = logging.getLogger(__name__)

REPEAT_REQUEST: Final = BRIDGE_PHRASES[2]


@dataclass(slots=True)
class PostSafetyFollowUpGuard:
    """Admits post-safety follow-up text; asks the child to repeat when the filter leaves silence."""

    is_active: Callable[[], bool]
    block_soothing: bool
    session_id: str
    turn_id: int
    dropped: int = 0

    def admit(self, text: str) -> str | None:
        """Return the text the post-safety reply may say, or ``None`` when the segment is dropped."""
        if not self.is_active():
            return text
        allowed = strip_soothing_from_follow_up(text) if self.block_soothing else text
        if not assistant_text_repeats_or_dilutes_safety(allowed):
            return allowed
        self.dropped += 1
        logger.info(
            "post_fixed_safety_follow_up_dropped session_id=%s turn_id=%s",
            self.session_id,
            self.turn_id,
        )
        return None

    def repeat_request(self, *, spoke: bool) -> str | None:
        """Return ``REPEAT_REQUEST`` when the filter dropped something and nothing was spoken."""
        if spoke or self.dropped == 0:
            return None
        logger.info(
            "post_fixed_safety_repeat_request session_id=%s turn_id=%s dropped=%s",
            self.session_id,
            self.turn_id,
            self.dropped,
        )
        return REPEAT_REQUEST
