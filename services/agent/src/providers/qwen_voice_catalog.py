"""Approved Qwen-Audio 3.1 system voices for Memoria companions."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from services.common.companions import DESIGNED_VOICE_SPEAKERS
from services.common.voice_identity import TTS_MODEL

DEFAULT_VOICE_PROFILE = "warm_companion"


@dataclass(frozen=True, slots=True)
class QwenVoiceSpec:
    profile_id: str
    display_name: str
    speaker_id: str
    role: str
    description: str
    preview_text: str


QWEN_VOICE_CATALOG: tuple[QwenVoiceSpec, ...] = (
    QwenVoiceSpec(
        profile_id="warm_companion",
        display_name="龙安洋",
        speaker_id=DESIGNED_VOICE_SPEAKERS["warm_companion"],
        role="default_companion",
        description="明亮、自然，像可靠的同龄朋友",
        preview_text="嗨，我是星澜。我会认真听，也会陪你把想法变成下一步。",
    ),
    QwenVoiceSpec(
        profile_id="bright_peer",
        display_name="龙华",
        speaker_id=DESIGNED_VOICE_SPEAKERS["bright_peer"],
        role="energetic_peer",
        description="清甜、灵动，回应里带一点自然上扬",
        preview_text="嗨，我是桃喜。普通的一天，也值得多一点亮晶晶的好心情。",
    ),
    QwenVoiceSpec(
        profile_id="soft_confidante",
        display_name="龙婉",
        speaker_id=DESIGNED_VOICE_SPEAKERS["soft_confidante"],
        role="empathic_companion",
        description="温柔、细腻，适合慢慢说和认真倾听",
        preview_text="嗨，我是绵绵。你不用急着变好，慢慢说，我会好好听着。",
    ),
    QwenVoiceSpec(
        profile_id="calm_guide",
        display_name="龙安智",
        speaker_id=DESIGNED_VOICE_SPEAKERS["calm_guide"],
        role="planner_guide",
        description="沉着、清晰，分析事情利落但不生硬",
        preview_text="嗨，我是阿序。复杂的事情，我们可以一件一件理清楚。",
    ),
    QwenVoiceSpec(
        profile_id="low_magnetic",
        display_name="龙三叔",
        speaker_id=DESIGNED_VOICE_SPEAKERS["low_magnetic"],
        role="grounded_companion",
        description="低沉、克制，安静里有让人放松的力量",
        preview_text="嗨，我是玄墨。别急，先站稳一点，我会在这里陪着你。",
    ),
)


def catalog_by_id() -> dict[str, QwenVoiceSpec]:
    return {spec.profile_id: spec for spec in QWEN_VOICE_CATALOG}


def resolve_approved_voice(*, profile_id: str, model: str = TTS_MODEL) -> str | None:
    """Return the system voice for an approved persona profile, else None.

    System voices are fixed by the vendor, so the catalog itself is the
    approval record; a different model never inherits these ids.
    """

    if model != TTS_MODEL:
        return None
    spec = catalog_by_id().get(profile_id)
    return spec.speaker_id if spec is not None else None


def catalog_as_json() -> list[dict[str, str]]:
    return [asdict(spec) for spec in QWEN_VOICE_CATALOG]
