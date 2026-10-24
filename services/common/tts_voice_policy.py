"""Voice identities the StepFun-primary / Doubao-backup path must never select.

The main/backup mode speaks designed voices only. A personal clone (Volcengine
ICL ``seed-icl-*`` / ``S_*`` speaker, or an Alibaba/CosyVoice clone) is a
different provider contract and must not be applied to the backup voice that
ships to the device.
"""

from __future__ import annotations

from typing import Final

PERSONAL_VOICE_RESOURCE_PREFIX: Final = "seed-icl"
CLONE_SPEAKER_PREFIX: Final = "S_"
CLONE_PROVIDERS: Final = frozenset({"alibaba_model_studio", "cosyvoice"})
PERSONAL_VOICE_KIND: Final = "personal"
MALE_SPEAKER_MARKER: Final = "_male_"


def is_male_speaker(voice: str) -> bool:
    """The companion backup voice is female; a male preset is a product error."""

    return MALE_SPEAKER_MARKER in voice.strip().lower()


def is_clone_or_icl_voice(
    *,
    voice: str,
    voice_kind: str | None = None,
    resource_id: str | None = None,
    provider: str | None = None,
) -> bool:
    """True when the requested identity is a cloned/ICL voice, never a preset."""

    if (voice_kind or "").strip().lower() == PERSONAL_VOICE_KIND:
        return True
    if voice.strip().startswith(CLONE_SPEAKER_PREFIX):
        return True
    if (resource_id or "").strip().lower().startswith(PERSONAL_VOICE_RESOURCE_PREFIX):
        return True
    return (provider or "").strip().lower() in CLONE_PROVIDERS


__all__ = [
    "CLONE_PROVIDERS",
    "CLONE_SPEAKER_PREFIX",
    "MALE_SPEAKER_MARKER",
    "PERSONAL_VOICE_KIND",
    "PERSONAL_VOICE_RESOURCE_PREFIX",
    "is_clone_or_icl_voice",
    "is_male_speaker",
]
