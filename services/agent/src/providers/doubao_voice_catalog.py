"""Approved Doubao TTS 2.0 voices for Memoria companions."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DOUBAO_TTS_MODEL = "seed-tts-2.0"
DEFAULT_VOICE_PROFILE = "warm_companion"


@dataclass(frozen=True, slots=True)
class DoubaoVoiceSpec:
    profile_id: str
    display_name: str
    speaker_id: str
    role: str
    description: str
    preview_text: str


DOUBAO_VOICE_CATALOG: tuple[DoubaoVoiceSpec, ...] = (
    DoubaoVoiceSpec(
        profile_id="warm_companion",
        display_name="阳光青年 2.0",
        speaker_id="zh_male_yangguangqingnian_uranus_bigtts",
        role="default_companion",
        description="明亮、自然，像可靠的同龄朋友",
        preview_text="嗨，我是星澜。我会认真听，也会陪你把想法变成下一步。",
    ),
    DoubaoVoiceSpec(
        profile_id="bright_peer",
        display_name="甜美桃子 2.0",
        speaker_id="zh_female_tianmeitaozi_uranus_bigtts",
        role="energetic_peer",
        description="清甜、灵动，回应里带一点自然上扬",
        preview_text="嗨，我是桃喜。普通的一天，也值得多一点亮晶晶的好心情。",
    ),
    DoubaoVoiceSpec(
        profile_id="soft_confidante",
        display_name="温柔小雅 2.0",
        speaker_id="zh_female_wenrouxiaoya_uranus_bigtts",
        role="empathic_companion",
        description="温柔、细腻，适合慢慢说和认真倾听",
        preview_text="嗨，我是绵绵。你不用急着变好，慢慢说，我会好好听着。",
    ),
    DoubaoVoiceSpec(
        profile_id="calm_guide",
        display_name="高冷沉稳 2.0",
        speaker_id="zh_male_gaolengchenwen_uranus_bigtts",
        role="planner_guide",
        description="沉着、清晰，分析事情利落但不生硬",
        preview_text="嗨，我是阿序。复杂的事情，我们可以一件一件理清楚。",
    ),
    DoubaoVoiceSpec(
        profile_id="low_magnetic",
        display_name="深夜播客 2.0",
        speaker_id="zh_male_shenyeboke_uranus_bigtts",
        role="grounded_companion",
        description="低沉、克制，安静里有让人放松的力量",
        preview_text="嗨，我是玄墨。别急，先站稳一点，我会在这里陪着你。",
    ),
)


def catalog_by_id() -> dict[str, DoubaoVoiceSpec]:
    return {spec.profile_id: spec for spec in DOUBAO_VOICE_CATALOG}


def default_registry_path() -> Path:
    configured = os.environ.get("DOUBAO_TTS_VOICE_REGISTRY")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[4] / "infra" / "voices" / "doubao_voice_ids.json"


def load_voice_registry(path: Path | None = None) -> dict[str, Any]:
    registry_path = path or default_registry_path()
    if not registry_path.is_file():
        return {"provider": "doubao", "model": DOUBAO_TTS_MODEL, "voices": {}}
    value = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("voices"), dict):
        raise ValueError(f"invalid Doubao voice registry: {registry_path}")
    return value


def resolve_approved_voice(
    *,
    profile_id: str,
    model: str = DOUBAO_TTS_MODEL,
    registry_path: Path | None = None,
) -> str | None:
    spec = catalog_by_id().get(profile_id)
    if spec is None:
        return None
    registry = load_voice_registry(registry_path)
    if registry.get("provider") != "doubao" or registry.get("model") != model:
        return None
    entry = registry["voices"].get(profile_id)
    if not isinstance(entry, dict):
        return None
    speaker_id = entry.get("speaker_id")
    if speaker_id != spec.speaker_id or entry.get("model", model) != model:
        return None
    return spec.speaker_id


def catalog_as_json() -> list[dict[str, str]]:
    return [asdict(spec) for spec in DOUBAO_VOICE_CATALOG]
