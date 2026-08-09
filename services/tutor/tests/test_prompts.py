import pytest
from services.agent.src.prompts import COMPANION_STYLE, SAFETY_CORE, TUTOR_STYLE
from services.agent.src.tutor_session import parse_session_focus, voice_system_prompt


def test_tutor_prompt_replaces_companion_style_but_keeps_crisis_safety_verbatim() -> None:
    prompt = voice_system_prompt("tutor_homework")

    assert SAFETY_CORE in prompt
    assert TUTOR_STYLE in prompt
    assert COMPANION_STYLE not in prompt
    assert "自伤、轻生或正在发生的紧迫危险" in prompt
    assert "不能用引导式教学拖延安全回应" in prompt
    assert "连续两次明确卡住后" in prompt
    assert "永远不要直接报出作业答案" in prompt


def test_english_prompt_uses_gentle_demonstration_based_correction() -> None:
    prompt = voice_system_prompt("tutor_english")

    assert "自然复述正确示范" in prompt
    assert "不要羞辱式指错" in prompt
    assert "一次只纠正一个" in prompt


def test_unknown_focus_fails_closed_instead_of_falling_back_to_chat() -> None:
    assert parse_session_focus("unknown") is None
    with pytest.raises(ValueError, match="session focus is unavailable"):
        voice_system_prompt(None)

