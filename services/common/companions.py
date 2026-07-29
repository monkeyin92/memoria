"""Stable, deliberately light companion definitions.

Companion styles are product delivery settings, never a source of an account
owner's Persona or Digital Self material.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

DEFAULT_COMPANION_ID: Final = "starlight"
DESIGNED_VOICE_MODEL: Final = "seed-tts-2.0"

COMPANION_STYLE_VERSION: Final = "companion-v1"


@dataclass(frozen=True, slots=True)
class CompanionDefinition:
    """The complete permitted companion surface from ADR-0016."""

    companion_id: str
    display_name: str
    style_description: str
    warmth: str
    directness: str
    response_length: str
    question_frequency: str
    interview_depth: str
    designed_voice_profile: str


COMPANIONS: Final[dict[str, CompanionDefinition]] = {
    "starlight": CompanionDefinition(
        "starlight",
        "星澜",
        "温暖回应，偶尔陪用户把想法理清一层",
        "warm",
        "gentle",
        "balanced",
        "occasional",
        "light",
        "warm_companion",
    ),
    "taoxi": CompanionDefinition(
        "taoxi",
        "桃喜",
        "轻快回应，回复偏短，只做少量追问",
        "bright",
        "direct",
        "brief",
        "occasional",
        "light",
        "bright_peer",
    ),
    "mianmian": CompanionDefinition(
        "mianmian",
        "绵绵",
        "耐心倾听，留出更多表达空间，不急着追问",
        "soft",
        "gentle",
        "balanced",
        "rare",
        "light",
        "soft_confidante",
    ),
    "axu": CompanionDefinition(
        "axu",
        "阿序",
        "直接清晰地梳理信息，但不替用户做决定",
        "calm",
        "direct",
        "balanced",
        "rare",
        "light",
        "calm_guide",
    ),
    "xuanmo": CompanionDefinition(
        "xuanmo",
        "玄墨",
        "克制回应，保留留白，只在被邀请时深入",
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

DESIGNED_VOICE_SPEAKERS: Final[dict[str, str]] = {
    "warm_companion": "zh_male_yangguangqingnian_uranus_bigtts",
    "bright_peer": "zh_female_tianmeitaozi_uranus_bigtts",
    "soft_confidante": "zh_female_wenrouxiaoya_uranus_bigtts",
    "calm_guide": "zh_male_gaolengchenwen_uranus_bigtts",
    "low_magnetic": "zh_male_shenyeboke_uranus_bigtts",
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


def designed_voice_speaker(profile_id: object) -> str | None:
    """Return the one canonical provider speaker for a designed profile."""

    if not isinstance(profile_id, str):
        return None
    return DESIGNED_VOICE_SPEAKERS.get(profile_id)


def designed_voice_speaker_sha256(profile_id: object) -> str | None:
    speaker = designed_voice_speaker(profile_id)
    if speaker is None:
        return None
    return hashlib.sha256(speaker.encode()).hexdigest()
