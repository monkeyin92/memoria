"""CosyVoice voice-design catalog for v3.5 designed voices.

v3.5 models have no system voices. Memoria uses voice design (text prompt →
voice_id) and stores the returned IDs in a local registry after enrollment.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

# CosyVoice design target for companion mainline (price + latency).
DEFAULT_DESIGN_TARGET_MODEL = "cosyvoice-v3.5-flash"
DEFAULT_VOICE_PROFILE = "warm_companion"

# Enrollment preview text: short companion line, Chinese.
DEFAULT_PREVIEW_TEXT = "嗨，我在这儿。今天想聊点轻松的，还是需要我帮你理一理事情？"

InstructStyle = Literal["auto", "fixed", "freeform"]


@dataclass(frozen=True, slots=True)
class DesignedVoiceSpec:
    """One voice-design profile (prompt only until enrolled)."""

    profile_id: str
    # CosyVoice prefix: digits/letters, max 10 chars.
    prefix: str
    display_name: str
    # Natural-language voice prompt (zh/en, CosyVoice max 500 chars).
    voice_prompt: str
    preview_text: str = DEFAULT_PREVIEW_TEXT
    # Product role for docs / selection UI.
    role: str = "companion"
    recommended: bool = False


# Keep prompts multi-dimensional, concrete, non-celebrity, under 500 chars.
VOICE_DESIGN_CATALOG: tuple[DesignedVoiceSpec, ...] = (
    DesignedVoiceSpec(
        profile_id="warm_companion",
        prefix="warmboy",
        display_name="暖阳青年",
        role="default_companion",
        recommended=True,
        voice_prompt=(
            "青年男性，大约22到28岁，音色中等偏亮、圆润干净，语速中等，"
            "吐字清晰自然。整体像阳光、友善、可靠的同龄朋友，"
            "适合一对一中文陪伴闲聊，不要播音腔，不要浮夸。"
        ),
    ),
    DesignedVoiceSpec(
        profile_id="soft_confidante",
        prefix="softgirl",
        display_name="温柔知己",
        role="empathic_companion",
        voice_prompt=(
            "青年女性，大约24到32岁，中音偏柔、音色圆润温暖，语速略慢于日常，"
            "吐字清楚。情感平静治愈、善于倾听，适合安慰与深夜谈心，"
            "不要娇喘感，不要过度甜美，不要播音腔。"
        ),
    ),
    DesignedVoiceSpec(
        profile_id="calm_guide",
        prefix="calmguide",
        display_name="沉稳向导",
        role="planner_guide",
        voice_prompt=(
            "青年到中年男性，大约28到38岁，中低音、沉稳有磁性但不浑浊，"
            "语速平稳略慢，吐字清晰利落。像靠谱的规划搭档，"
            "适合说明步骤与安排计划，冷静但不生硬，不要新闻播报腔。"
        ),
    ),
    DesignedVoiceSpec(
        profile_id="bright_peer",
        prefix="brightpeer",
        display_name="元气搭子",
        role="energetic_peer",
        voice_prompt=(
            "青年女性，大约20到28岁，音调偏高但不过尖，音色清脆轻快，"
            "语速略快、上扬自然。像活力满满的同龄搭子，"
            "适合轻松吐槽与日常闲聊，不要儿童音，不要喊麦感。"
        ),
    ),
    DesignedVoiceSpec(
        profile_id="low_magnetic",
        prefix="lowmag",
        display_name="低音笃定",
        role="grounded_companion",
        voice_prompt=(
            "青年男性，大约25到35岁，低音、厚实有质感，语速中等偏慢，"
            "气息稳定，吐字清楚。整体笃定、包容、有安全感，"
            "适合认真对话与情绪托底，不要夸张低沉，不要反派感。"
        ),
    ),
)


def catalog_by_id() -> dict[str, DesignedVoiceSpec]:
    return {spec.profile_id: spec for spec in VOICE_DESIGN_CATALOG}


def default_registry_path() -> Path:
    env = os.environ.get("COSYVOICE_VOICE_REGISTRY")
    if env:
        return Path(env)
    # repo_root/infra/voices/designed_voice_ids.json
    return Path(__file__).resolve().parents[4] / "infra" / "voices" / "designed_voice_ids.json"


def load_voice_registry(path: Path | None = None) -> dict[str, Any]:
    registry_path = path or default_registry_path()
    if not registry_path.is_file():
        return {"target_model": DEFAULT_DESIGN_TARGET_MODEL, "voices": {}}
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid voice registry: {registry_path}")
    voices = data.get("voices") or {}
    if not isinstance(voices, dict):
        raise ValueError(f"invalid voices map in registry: {registry_path}")
    return data


def resolve_voice_id(
    *,
    profile_id: str | None = None,
    explicit_voice: str | None = None,
    registry_path: Path | None = None,
) -> str | None:
    """Resolve COSYVOICE_VOICE / profile to a synthesizable voice id.

    Priority: explicit_voice (if set and not a bare profile id) → registry[profile].
    """
    if explicit_voice:
        # Allow profile_id as COSYVOICE_VOICE for convenience.
        catalog = catalog_by_id()
        if explicit_voice in catalog:
            profile_id = explicit_voice
        else:
            return explicit_voice
    pid = profile_id or DEFAULT_VOICE_PROFILE
    registry = load_voice_registry(registry_path)
    voices = registry.get("voices") or {}
    entry = voices.get(pid)
    if not entry:
        return None
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        voice_id = entry.get("voice_id")
        return str(voice_id) if voice_id else None
    return None


def resolve_approved_designed_voice(
    *,
    profile_id: str,
    model: str,
    registry_path: Path | None = None,
) -> str | None:
    """Resolve a production baseline from a known design profile and registry entry."""
    spec = catalog_by_id().get(profile_id)
    if spec is None:
        return None
    registry = load_voice_registry(registry_path)
    if registry.get("target_model") != model:
        return None
    entry = (registry.get("voices") or {}).get(profile_id)
    if isinstance(entry, dict):
        if entry.get("target_model", model) != model:
            return None
        voice_id = entry.get("voice_id")
    else:
        voice_id = entry
    if not isinstance(voice_id, str):
        return None
    expected_prefix = f"{model}-vd-{spec.prefix}-"
    return voice_id if voice_id.startswith(expected_prefix) else None


def uses_freeform_instruct(
    *,
    model: str,
    voice: str,
    instruct_style: InstructStyle = "auto",
) -> bool:
    """v3.5 designed voices accept free-form instructions; longanyang does not."""
    if instruct_style == "freeform":
        return True
    if instruct_style == "fixed":
        return False
    # auto
    if "v3.5" in model:
        return True
    if "-vd-" in voice:
        return True
    if voice == "longanyang":
        return False
    # Other system voices on v3-flash generally use fixed templates.
    return False


def catalog_as_json() -> list[dict[str, Any]]:
    return [asdict(spec) for spec in VOICE_DESIGN_CATALOG]
