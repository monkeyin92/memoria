from services.agent.src.prompts import VOICE_SYSTEM_PROMPT


def test_voice_prompt_has_bounded_thinking_preambles_and_variety() -> None:
    assert "多步骤" in VOICE_SYSTEM_PROMPT
    assert "直接问题" in VOICE_SYSTEM_PROMPT
    assert "不要连续两轮使用同一个开场" in VOICE_SYSTEM_PROMPT
    assert "不要为了显得自然而每轮都加" in VOICE_SYSTEM_PROMPT
    assert "接受你上一轮的提议" in VOICE_SYSTEM_PROMPT
    assert "不要重复刚说过的安排" in VOICE_SYSTEM_PROMPT


def test_voice_prompt_mirrors_laughter_only_in_safe_contexts() -> None:
    assert "听见用户自然笑出声" in VOICE_SYSTEM_PROMPT
    assert "可以先短促、真诚地轻笑一次" in VOICE_SYSTEM_PROMPT
    assert "难过、生气、害怕、求助或涉及严肃风险" in VOICE_SYSTEM_PROMPT
    assert "不要咳嗽" in VOICE_SYSTEM_PROMPT
