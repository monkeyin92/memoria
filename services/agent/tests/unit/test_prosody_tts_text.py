"""Provider-neutral delivery-mode TTS rewrites."""

from __future__ import annotations

from services.agent.src.orchestration.prosody import (
    prepare_tts_text,
    speech_plan_for_turn,
    strip_paralinguistic_markup,
)


def test_light_laughter_prefixes_first_segment_once() -> None:
    plan = speech_plan_for_turn(
        label="neutral",
        provider_label="happy",
        text="哈哈，太好笑了",
    )
    first = prepare_tts_text("那确实好笑。", plan, is_first_segment=True)
    second = prepare_tts_text("我们继续练。", plan, is_first_segment=False)
    already = prepare_tts_text("呵，我懂。", plan, is_first_segment=True)

    assert first == "呵，那确实好笑。"
    assert second == "我们继续练。"
    assert already == "呵，我懂。"


def test_markup_tags_and_supportive_strip() -> None:
    plan = speech_plan_for_turn(
        label="neutral",
        provider_label="happy",
        text="哈哈，太好笑了",
        use_markup_tags=True,
    )
    assert prepare_tts_text("好呀。", plan, is_first_segment=True) == "[laughter]好呀。"

    serious = speech_plan_for_turn(
        label="sad",
        provider_label="sad",
        text="我今天很难过",
    )
    assert serious.strip_paralinguistic is True
    assert (
        prepare_tts_text(
            "[laughter]哈哈，别担心。",
            serious,
            is_first_segment=True,
        )
        == "别担心。"
    )
    assert strip_paralinguistic_markup("<laughter>呵</laughter>你好") == "呵你好"


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
    first = prepare_tts_text("好，我们可以这样安排。", deliberative, is_first_segment=True)
    assert first.startswith("[breath]")

    serious = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="刚才出车祸了很害怕",
        use_markup_tags=True,
    )
    assert serious.delivery_mode == "supportive"
    assert serious.strip_paralinguistic is True
    assert "禁止" in serious.llm_instruction
    stripped = prepare_tts_text(
        "[breath][laughter]<strong>没事</strong>的",
        serious,
        is_first_segment=True,
        use_markup_tags=True,
    )
    assert "[breath]" not in stripped
    assert "[laughter]" not in stripped
    assert "<strong>" not in stripped
