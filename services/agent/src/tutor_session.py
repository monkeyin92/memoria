"""Agent adapter for the server-frozen tutor session focus."""

from __future__ import annotations

from typing import cast

from services.agent.src.prompts import (
    COMPANION_STYLE,
    SAFETY_CORE,
    TUTOR_STYLE,
    VOICE_SYSTEM_PROMPT,
)
from services.tutor.domain import SESSION_FOCUSES, SessionFocus
from services.tutor.prompts import focus_style


def parse_session_focus(value: object) -> SessionFocus | None:
    """Parse only the three server-owned values; unknown values fail closed."""

    return cast(SessionFocus, value) if isinstance(value, str) and value in SESSION_FOCUSES else None


def voice_system_prompt(session_focus: SessionFocus | None) -> str:
    """Replace the delivery style while retaining the exact shared safety core."""

    if session_focus == "chat":
        return VOICE_SYSTEM_PROMPT
    if session_focus in {"tutor_english", "tutor_homework"}:
        return "\n\n".join(
            (SAFETY_CORE, TUTOR_STYLE, focus_style(session_focus))
        )
    raise ValueError("session focus is unavailable")


def is_tutor_focus(session_focus: SessionFocus | None) -> bool:
    return session_focus in {"tutor_english", "tutor_homework"}


__all__ = [
    "COMPANION_STYLE",
    "SAFETY_CORE",
    "TUTOR_STYLE",
    "is_tutor_focus",
    "parse_session_focus",
    "voice_system_prompt",
]
