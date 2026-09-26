"""A minor's signed session limits, enforced on the device session (P0-04 D3).

Policy signs MAX_SESSION_SECONDS and QUIET_HOURS for a minor's chat, tutor and
English practice. They used to reach the model only as prompt text; the device
session now enforces them: a wake inside quiet hours is refused with a fixed
phrase, and a session past its limit ends with a fixed goodbye once the
current reply has finished. A session that heard the crisis reply is never
cut short by the time limit.
"""

from __future__ import annotations

from datetime import time

from services.agent.src.runtime_profile import VerifiedRuntimeProfile


def _clock(value: str) -> time:
    hours, minutes = value.split(":", 1)
    return time(int(hours), int(minutes))


def in_quiet_hours(now: time, window: tuple[str, str]) -> bool:
    """Whether ``now`` falls in ``[start, end)``, which may wrap past midnight."""

    start, end = _clock(window[0]), _clock(window[1])
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def minor_limits(profile: VerifiedRuntimeProfile | None) -> tuple[int | None, tuple[str, str] | None]:
    """The session limits to enforce: only for a minor's signed profile."""

    if profile is None or profile.profile.subject_category != "minor":
        return None, None
    return profile.profile.max_session_seconds, profile.profile.quiet_hours


def session_limit_reached(started_at: float, now: float, max_seconds: int | None) -> bool:
    return max_seconds is not None and now - started_at >= max_seconds
