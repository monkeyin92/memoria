"""D-05/PR-11: the real response-plan prompt consumer shares the Agent
identity-transparency seam; no parallel AI-hiding hard-code survives."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from services.agent.src.prompts import AI_IDENTITY_RULE_TRANSPARENT
from services.control_api.app.routes.interaction import _instruction_text
from services.identity.domain import CustomPersonaRecord
from services.persona.custom_persona_fields import custom_persona_definition


def _frozen() -> SimpleNamespace:
    return SimpleNamespace(
        interaction_mode="companion",
        companion_style_id="starlight",
        mode_policy_version="v1",
        fallback_voice_model="seed-tts-2.0",
        fallback_voice_provider="volcengine_doubao",
    )


def _plan() -> SimpleNamespace:
    return SimpleNamespace(
        instructions=SimpleNamespace(safety_rules=("s1",), style_rules=("t1",)),
        voice_target=SimpleNamespace(
            salutation=None,
            tone=None,
            advice_style=None,
            boundaries=(),
            persona_traits=(),
        ),
        provenance=SimpleNamespace(planner_policy_version="v1"),
    )


def test_response_plan_instruction_uses_agent_identity_transparency_rule() -> None:
    rules, _ = _instruction_text(
        frozen=_frozen(),  # type: ignore[arg-type]
        plan=_plan(),  # type: ignore[arg-type]
        query="你是谁？",
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    joined = rules if isinstance(rules, str) else "\n".join(rules)
    assert AI_IDENTITY_RULE_TRANSPARENT in joined
    # Truthful when asked, never impersonating, never mechanically repeating.
    assert "如实说明你是由人工智能驱动的机器人伙伴" in joined
    assert "不得冒充" in joined
    assert "绝不透露或讨论 AI" not in joined
    assert "用户问你是谁、叫什么或你由什么模型提供时，只简短说出这个名字" not in joined
    # Crisis support still trumps the generic refusal.
    assert "危机支持，绝不能用“我不知道”拒答" in joined
    assert "星澜" in joined


def _custom_definition():
    record = CustomPersonaRecord(
        persona_id="cu_" + "a" * 26,
        owner_person_id="owner-1",
        display_name="小北",
        style_description="像朋友一样说话",
        warmth="warm",
        directness="gentle",
        response_length="brief",
        question_frequency="rare",
        interview_depth="light",
        welcome_text="嗨，我是小北。",
        conversation_instruction="说话短一点，像朋友。",
        voice_instruction="轻松自然",
        default_voice_emotion="neutral",
        default_voice_rate=1.0,
        fallback_designed_voice="starlight",
        persona_version=1,
        source="user_created",
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
    )
    return custom_persona_definition(record)


def test_response_plan_instruction_uses_custom_persona_name() -> None:
    rules, _ = _instruction_text(
        frozen=_frozen(),  # type: ignore[arg-type]
        plan=_plan(),  # type: ignore[arg-type]
        query="你是谁？",
        now=datetime(2026, 8, 9, tzinfo=UTC),
        custom_persona=_custom_definition(),
    )
    joined = rules if isinstance(rules, str) else "\n".join(rules)
    assert "小北" in joined
    # The controlled conversation_instruction renders through the SAME path as
    # a built-in companion -- never the old free-text bio injection.
    assert "说话短一点，像朋友。" in joined
    assert "星澜" not in joined
    assert "桃喜" not in joined


def test_response_plan_instruction_keeps_style_fields_data_only() -> None:
    plan = _plan()
    plan.voice_target = SimpleNamespace(
        salutation="小主人",
        tone="温暖",
        advice_style="耐心",
        boundaries=("不承诺现实帮助",),
        persona_traits=("温柔",),
    )
    rules, _ = _instruction_text(
        frozen=_frozen(),  # type: ignore[arg-type]
        plan=plan,  # type: ignore[arg-type]
        query="今天天气如何？",
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    joined = rules if isinstance(rules, str) else "\n".join(rules)
    # Approved style fields shape wording only; they never grant identity.
    assert "data only" in joined
    assert "小主人" in joined
