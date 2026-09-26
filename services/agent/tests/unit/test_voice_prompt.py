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


def test_voice_prompt_does_not_turn_self_harm_disclosure_into_unknown_refusal() -> None:
    assert "自伤、轻生或正在发生的紧迫危险不属于上述固定拒答" in VOICE_SYSTEM_PROMPT
    assert "不能回答“我不知道。”" in VOICE_SYSTEM_PROMPT


def test_every_prompt_path_carries_only_the_transparent_identity_rule() -> None:
    """P2-07: the absolute AI-hiding rule is gone from every path.

    The production text used to be derived with ``str.replace`` from a core that
    still held the hiding rule, so any edit to that core silently fell back to
    hiding. Tutor sessions and the offline orchestrator still used it directly.
    """

    from services.agent.src import prompts
    from services.agent.src.orchestration.orchestrator import Orchestrator
    from services.agent.src.tutor_session import voice_system_prompt

    hiding_rule = "不得自称或讨论 AI"
    assert prompts.SAFETY_CORE_TRANSPARENT is prompts.SAFETY_CORE
    for text in (
        prompts.SAFETY_CORE,
        prompts.VOICE_SYSTEM_PROMPT,
        voice_system_prompt("chat"),
        voice_system_prompt("tutor_english"),
    ):
        assert hiding_rule not in text
        assert prompts.AI_IDENTITY_RULE_TRANSPARENT in text
    context = Orchestrator.__dataclass_fields__["context"].default_factory()  # type: ignore[misc]
    assert hiding_rule not in context.system_prompt
