"""Stable, deliberately light companion definitions.

Companion styles are product delivery settings, never a source of an account
owner's Persona or Digital Self material.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

DEFAULT_COMPANION_ID: Final = "starlight"
DESIGNED_VOICE_MODEL: Final = "seed-tts-2.0"

COMPANION_STYLE_VERSION: Final = "companion-v1"


@dataclass(frozen=True, slots=True)
class CompanionDefinition:
    """The complete permitted companion surface from ADR-0016."""

    companion_id: str
    warmth: str
    directness: str
    response_length: str
    question_frequency: str
    interview_depth: str
    designed_voice_profile: str


COMPANIONS: Final[dict[str, CompanionDefinition]] = {
    "starlight": CompanionDefinition(
        "starlight", "warm", "gentle", "balanced", "occasional", "light", "warm_companion"
    ),
    "taoxi": CompanionDefinition(
        "taoxi", "bright", "direct", "brief", "occasional", "light", "bright_peer"
    ),
    "mianmian": CompanionDefinition(
        "mianmian", "soft", "gentle", "balanced", "rare", "light", "soft_confidante"
    ),
    "axu": CompanionDefinition(
        "axu", "calm", "direct", "balanced", "rare", "light", "calm_guide"
    ),
    "xuanmo": CompanionDefinition(
        "xuanmo",
        "reserved",
        "direct",
        "brief",
        "rare",
        "on_explicit_invitation",
        "low_magnetic",
    ),
}

COMPANION_VOICE_PROFILES: Final[dict[str, str]] = {
    companion_id: definition.designed_voice_profile
    for companion_id, definition in COMPANIONS.items()
}

COMPANION_IDS: Final[frozenset[str]] = frozenset(COMPANION_VOICE_PROFILES)


def companion_definition(companion_id: object) -> CompanionDefinition | None:
    if not isinstance(companion_id, str):
        return None
    return COMPANIONS.get(companion_id)


def designed_voice_profile(companion_id: object) -> str | None:
    """Return the approved catalog key for a persisted companion selection."""

    if not isinstance(companion_id, str):
        return None
    return COMPANION_VOICE_PROFILES.get(companion_id)
