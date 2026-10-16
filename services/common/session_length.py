"""Parent-chosen length of one chat, shared by policy and the bind form."""

from __future__ import annotations

from typing import Final

SESSION_MINUTES_MIN: Final = 10
SESSION_MINUTES_MAX: Final = 30
SESSION_MINUTES_DEFAULT: Final = 15
DEFAULT_MAX_SESSION_SECONDS: Final = SESSION_MINUTES_DEFAULT * 60


def session_seconds_from_minutes(minutes: object) -> int | None:
    """Minutes inside the parent range, or None so the policy default applies."""

    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return None
    if minutes < SESSION_MINUTES_MIN or minutes > SESSION_MINUTES_MAX:
        return None
    return minutes * 60


def issued_session_seconds(stored: object) -> int:
    """Seconds the device enforces. Outside 10 to 30 minutes, issue 15."""

    if (
        isinstance(stored, int)
        and not isinstance(stored, bool)
        and SESSION_MINUTES_MIN * 60 <= stored <= SESSION_MINUTES_MAX * 60
    ):
        return stored
    return DEFAULT_MAX_SESSION_SECONDS
