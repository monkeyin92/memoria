"""Stable companion identifiers shared by profile and voice resolution APIs."""

from __future__ import annotations

from typing import Final

DEFAULT_COMPANION_ID: Final = "starlight"
DESIGNED_VOICE_MODEL: Final = "cosyvoice-v3.5-flash"

COMPANION_VOICE_PROFILES: Final[dict[str, str]] = {
    "starlight": "warm_companion",
    "taoxi": "bright_peer",
    "mianmian": "soft_confidante",
    "axu": "calm_guide",
    "xuanmo": "low_magnetic",
}

COMPANION_IDS: Final[frozenset[str]] = frozenset(COMPANION_VOICE_PROFILES)


def designed_voice_profile(companion_id: object) -> str | None:
    """Return the approved catalog key for a persisted companion selection."""

    if not isinstance(companion_id, str):
        return None
    return COMPANION_VOICE_PROFILES.get(companion_id)
