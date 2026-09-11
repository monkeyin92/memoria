"""Agent adapter for the server-frozen tutor session focus."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from services.agent.src.prompt_composition import compose_production_prompt
from services.agent.src.prompts import (
    COMPANION_STYLE,
    SAFETY_CORE,
    TUTOR_STYLE,
    VOICE_SYSTEM_PROMPT,
)
from services.tutor.domain import SESSION_FOCUSES, SessionFocus
from services.tutor.prompts import focus_style

if TYPE_CHECKING:
    from services.agent.src.duplex_runtime import DuplexRuntime


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


def production_system_prompt(runtime: DuplexRuntime) -> str:
    """Compose the production system prompt for the current fenced generation.

    This is the single seam shared by the cascade (agent.py) and Omni
    (media_agent_factory.py) production paths.  Persona is style-only, the
    service mode is the canonical RuntimeProfile value (``unknown_safe`` when
    the profile is missing/malformed/expired/epoch-mismatched), and tutor focus
    remains an independent delivery dimension.
    """

    fence = runtime.fence
    composed = compose_production_prompt(
        profile=runtime.orchestrator.runtime_profiles.for_fence(
            fence, current_fence=fence
        ),
        focus=runtime.mode_policy.session_focus,
        memory_block=None,
        metrics=runtime.orchestrator.metrics,
        custom_persona=runtime.mode_policy.custom_persona,
    )
    return composed.system


def is_tutor_focus(session_focus: SessionFocus | None) -> bool:
    return session_focus in {"tutor_english", "tutor_homework"}


__all__ = [
    "COMPANION_STYLE",
    "SAFETY_CORE",
    "TUTOR_STYLE",
    "is_tutor_focus",
    "parse_session_focus",
    "production_system_prompt",
    "voice_system_prompt",
]
