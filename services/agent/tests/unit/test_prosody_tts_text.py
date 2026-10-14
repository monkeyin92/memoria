"""Provider-neutral delivery-mode TTS rewrites."""

from __future__ import annotations

from services.agent.src.orchestration.prosody import (
    speech_plan_for_turn,
    strip_paralinguistic_markup,
)


def test_markup_tags_and_supportive_strip() -> None:
    serious = speech_plan_for_turn(
        label="sad",
        provider_label="sad",
        text="我今天很难过",
    )
    assert serious.strip_paralinguistic is True
    assert strip_paralinguistic_markup("<laughter>呵</laughter>你好") == "呵你好"
    assert strip_paralinguistic_markup("[laughter]哈哈，别担心。") == "别担心。"


def test_deliberative_breath_and_serious_hard_ban_markup() -> None:
    deliberative = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="帮我安排十五分钟口语训练",
        use_markup_tags=True,
    )
    assert deliberative.delivery_mode == "deliberative"
    assert deliberative.tts_prefix == "[breath]"
    assert "[breath]" in deliberative.llm_instruction or "吸气" in deliberative.llm_instruction

    serious = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="刚才出车祸了很害怕",
        use_markup_tags=True,
    )
    assert serious.delivery_mode == "supportive"
    assert serious.strip_paralinguistic is True
    assert "禁止" in serious.llm_instruction
