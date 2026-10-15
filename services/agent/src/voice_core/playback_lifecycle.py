"""Playback endings that are not a listening turn, and crisis-script stops.

A terminal fixed phrase (the daily chat limit and the same standby lines)
finishes in standby. A crisis script accepts 「停」 only after the guardian
pinned record is confirmed written. Neither path stores or logs ASR text.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

STANDBY_AFTER_PLAYBACK_REASON = "standby_after_playback"
SAFETY_PIN_UNCONFIRMED_REASON = "safety_pin_unconfirmed"

_CRISIS_TEMPLATE_IDS = frozenset(
    {"crisis-self-v1", "victim-support-v1", "support-other-v1"}
)


def crisis_script_may_stop(*, template_id: str | None, pin_confirmed: bool) -> bool:
    """A fixed crisis script stops only after the guardian pin write succeeded."""

    if template_id not in _CRISIS_TEMPLATE_IDS:
        return True
    return pin_confirmed


class SafetyInterruptArgs(TypedDict):
    safety_reply: bool
    safety_pin_confirmed: bool


def safety_interrupt_args(runtime: object) -> SafetyInterruptArgs:
    return {
        "safety_reply": bool(getattr(runtime, "safety_reply_playing", False)),
        "safety_pin_confirmed": bool(getattr(runtime, "guardian_pin_confirmed", False)),
    }


def generation_control_reason(context: Any, reason: str, *, terminal: bool) -> str:
    """Rewrite a generation control reason for the two playback endings.

    ``terminal`` is generation.completed. A start while the guardian pin is
    still unconfirmed tells the device not to arm 「停」. A completed terminal
    phrase tells the device to go idle instead of opening a listening turn.
    """

    runtime = getattr(context, "runtime", None)
    if not terminal and bool(getattr(runtime, "playback_stop_suppressed", False)):
        return SAFETY_PIN_UNCONFIRMED_REASON
    if terminal and bool(getattr(context, "standby_after_playback", False)):
        return STANDBY_AFTER_PLAYBACK_REASON
    return reason


def crisis_playback_stop_blocked(context: Any) -> bool:
    """True while a crisis script is playing and the guardian pin is unconfirmed."""

    runtime = getattr(context, "runtime", None)
    if not bool(getattr(runtime, "safety_reply_playing", False)):
        return False
    return not crisis_script_may_stop(
        template_id=getattr(runtime, "guardian_crisis_template_id", None),
        pin_confirmed=bool(getattr(runtime, "guardian_pin_confirmed", False)),
    )


async def mark_played_crisis_script_interrupted(context: Any) -> None:
    """Record that a confirmed crisis script was cut off. Ids only, no ASR text."""

    runtime = getattr(context, "runtime", None)
    if crisis_playback_stop_blocked(context) or not bool(
        getattr(runtime, "safety_reply_playing", False)
    ):
        return
    if not bool(getattr(runtime, "guardian_pin_confirmed", False)):
        return
    template_id = str(getattr(runtime, "guardian_crisis_template_id", "") or "")
    event_id = getattr(runtime, "guardian_crisis_event_id", None)
    session_id = getattr(getattr(context, "identity", None), "session_id", "")
    logger.info(
        "crisis_script_interrupted reply_template_id=%s crisis_event_id=%s session=%s",
        template_id,
        event_id or "",
        session_id,
    )
    reporter = getattr(runtime, "report_crisis_script_interrupted", None)
    if event_id and callable(reporter):
        try:
            await reporter(str(event_id))
        except Exception:
            logger.exception(
                "crisis script interrupt mark failed reply_template_id=%s",
                template_id,
            )
    clear = getattr(runtime, "clear_safety_reply_playing", None)
    if callable(clear):
        clear()


def resume_listening_after_playback(*, tools_active: bool, resume_listening: bool) -> bool:
    """True when playback completion should open a user listening turn."""

    if tools_active:
        return False
    return resume_listening


class CrisisPlaybackMixin:
    """Crisis-script stop state. Kept off DuplexRuntime so that file stays in budget."""

    safety_reply_playing: bool
    guardian_pin_confirmed: bool
    guardian_crisis_event_id: str | None
    guardian_crisis_template_id: str
    playback_stop_suppressed: bool
    report_crisis_script_interrupted: Callable[[str], Awaitable[bool]] | None

    def note_guardian_crisis_playback(
        self,
        *,
        pin_confirmed: bool,
        crisis_event_id: str | None,
        template_id: str,
    ) -> None:
        self.safety_reply_playing = True
        self.guardian_pin_confirmed = bool(pin_confirmed)
        self.guardian_crisis_event_id = crisis_event_id if pin_confirmed else None
        self.guardian_crisis_template_id = template_id
        self.playback_stop_suppressed = not pin_confirmed

    def clear_safety_reply_playing(self) -> None:
        self.safety_reply_playing = False
        self.playback_stop_suppressed = False

    def set_crisis_script_interrupt_reporter(
        self,
        reporter: Callable[[str], Awaitable[bool]],
    ) -> None:
        self.report_crisis_script_interrupted = reporter
