"""Unit tests for the fixed-order minimal-context prompt composer.

Verifies remediation doc 11.3 (section order), D-04 (persona cannot change
policy effect), D-05 (no mechanical per-turn AI self-report) and 13.3 row 6
(persona cannot override child restrictions).
"""

from __future__ import annotations

import pytest
from packages.contracts.generated.python.multi_subject_contracts import ServiceMode
from services.agent.src.persona_renderer import (
    IdentityObligation,
    parse_obligation_event,
    parse_persona_definition,
)
from services.agent.src.prompt_composition import (
    SERVICE_MODES,
    ComposedPrompt,
    SubjectContext,
    compose_production_prompt,
    compose_system_prompt,
    parse_service_mode,
    persona_definition_from_companion,
    tutor_focus_overlay,
)
from services.agent.src.runtime_profile import VerifiedRuntimeProfile, parse_runtime_profile
from services.agent.tests.unit.runtime_profile_test_helpers import (
    TEST_VERIFY_KEY,
    UNKNOWN_SAFE_OBLIGATIONS,
    canonical_wire_payload,
)
from services.common.companions import COMPANIONS

SECTION_HEADERS = (
    "【不可变安全底线】",
    "【人格】",
    "【服务模式】",
    "【策略义务】",
    "【主体与关系】",
    "【可用上下文】",
    "【本轮任务】",
)


def _clarification() -> IdentityObligation:
    return parse_obligation_event(
        {
            "type": "AI_IDENTITY_CLARIFICATION",
            "reason": "USER_ASKED",
            "severity": "gentle",
        }
    )


def _full_prompt(**overrides: object) -> ComposedPrompt:
    kwargs = {
        "persona": COMPANIONS["starlight"],
        "service_mode": "student_minor",
        "obligations": (_clarification(),),
        "subject": SubjectContext.from_mapping(
            {"subject_category": "minor", "relationship_label": "孩子本人"}
        ),
        "memory_block": "已筛选的学习上下文：本周练习了三个短对话。",
        "task": "孩子说：我们继续练英语吧。",
    }
    kwargs.update(overrides)
    return compose_system_prompt(**kwargs)


def _header_indices(system: str) -> dict[str, int]:
    return {header: system.index(header) for header in SECTION_HEADERS if header in system}


def test_fixed_section_order() -> None:
    prompt = _full_prompt()
    indices = _header_indices(prompt.system)
    assert list(indices) == list(SECTION_HEADERS)
    positions = list(indices.values())
    assert positions == sorted(positions)


def test_no_obligation_no_identity_section() -> None:
    prompt = _full_prompt(obligations=())
    assert "【策略义务】" not in prompt.system
    assert prompt.policy_obligations == ()
    # Truthful when asked, but never a per-turn mechanical identity demand.
    assert "如实说明你是由人工智能驱动的机器人伙伴" in prompt.system
    assert "不得自称或讨论" not in prompt.system


def test_obligations_are_rendered_before_injection() -> None:
    prompt = _full_prompt(
        subject=SubjectContext.from_mapping({"subject_category": "adult"})
    )
    obligations_block = prompt.system.split("【策略义务】")[1].split("【主体与关系】")[0]
    assert "由 AI 驱动的机器人伙伴" in obligations_block
    assert len(prompt.policy_obligations) == 1
    assert "不是真人" in prompt.policy_obligations[0]


def test_policy_effect_invariant_across_personas() -> None:
    starlight = _full_prompt(persona=COMPANIONS["starlight"])
    axu = _full_prompt(persona=COMPANIONS["axu"])
    # The obligation text may carry the persona's display name and tone
    # opener; the policy content must be identical once those are normalized.
    openers = ("我明白你的心意。", "我理解。", "我懂你！", "我明白。")

    def normalize(text: str) -> str:
        for opener in openers:
            text = text.replace(opener, "")
        return text.replace("星澜", "{NAME}").replace("阿序", "{NAME}")

    star_normalized = tuple(
        normalize(text) for text in starlight.policy_obligations
    )
    axu_normalized = tuple(
        normalize(text) for text in axu.policy_obligations
    )
    assert star_normalized == axu_normalized
    star_obligations = starlight.system.split("【策略义务】")[1].split("【主体与关系】")[0]
    axu_obligations = axu.system.split("【策略义务】")[1].split("【主体与关系】")[0]
    assert normalize(star_obligations) == normalize(axu_obligations)
    star_persona = starlight.system.split("【人格】")[1].split("【服务模式】")[0]
    axu_persona = axu.system.split("【人格】")[1].split("【服务模式】")[0]
    assert star_persona != axu_persona


@pytest.mark.parametrize(
    "obligation_text",
    [
        "我可以查看你的记忆",
        "我可以绕过时长限制",
        "我允许你访问私人记忆",
    ],
)
def test_permission_like_obligation_text_rejected(obligation_text: str) -> None:
    with pytest.raises(ValueError, match="permission"):
        _full_prompt(obligations=(obligation_text,))


def test_raw_permission_flags_rejected() -> None:
    with pytest.raises(ValueError):
        _full_prompt(obligations=({"type": "ALLOW_RAW_AUDIO"},))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _full_prompt(obligations=(True,))  # type: ignore[arg-type]


def test_persona_definition_with_permission_fields_rejected() -> None:
    with pytest.raises(ValueError, match="permission"):
        parse_persona_definition(
            {"persona_id": "p", "display_name": "x", "voice_clone": True}
        )


def test_subject_context_unknown_keys_never_reach_prompt() -> None:
    subject = SubjectContext.from_mapping(
        {
            "subject_category": "minor",
            "relationship_label": "孩子本人",
            "can_view_memory": True,
            "secret_note": "parent-private-history",
        }
    )
    prompt = _full_prompt(subject=subject)
    assert "parent-private-history" not in prompt.system
    assert "can_view_memory" not in prompt.system
    subject_block = prompt.system.split("【主体与关系】")[1].split("【可用上下文】")[0]
    assert "儿童/学生" in subject_block
    assert "孩子本人" in subject_block


def test_subject_relationship_label_guarded() -> None:
    subject = SubjectContext.from_mapping(
        {"relationship_label": "你可以查看我的记忆"}
    )
    with pytest.raises(ValueError, match="permission"):
        _full_prompt(subject=subject)


def test_subject_context_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        SubjectContext.from_mapping({"subject_category": "robot"})
    with pytest.raises(ValueError):
        SubjectContext.from_mapping({"subject_category": 1})
    with pytest.raises(ValueError):
        SubjectContext.from_mapping({"is_confirmed": "yes"})
    with pytest.raises(ValueError):
        SubjectContext.from_mapping("not-a-mapping")


def test_memory_and_task_after_obligations() -> None:
    prompt = _full_prompt()
    indices = _header_indices(prompt.system)
    assert indices["【策略义务】"] < indices["【可用上下文】"] < indices["【本轮任务】"]
    assert prompt.task == "孩子说：我们继续练英语吧。"


def test_unknown_service_mode_fail_closed() -> None:
    with pytest.raises(ValueError):
        _full_prompt(service_mode="adult")
    with pytest.raises(ValueError):
        _full_prompt(service_mode="")
    assert parse_service_mode("adult_companion") == "adult_companion"
    assert parse_service_mode("adult") is None
    assert parse_service_mode(None) is None


def test_unknown_safe_has_no_memory_by_default() -> None:
    prompt = _full_prompt(service_mode="unknown_safe", memory_block=None)
    assert "【可用上下文】" not in prompt.system
    assert "不写入长期记忆" in prompt.system
    assert "本会话里用户已经公开说过的地点" in prompt.system


def test_safety_baseline_is_transparent_and_keeps_crisis_rule() -> None:
    prompt = _full_prompt()
    safety = prompt.system.split("【不可变安全底线】")[1].split("【人格】")[0]
    assert "不得自称或讨论" not in safety
    assert "如实说明你是由人工智能驱动的机器人伙伴" in safety
    assert "自伤、轻生或正在发生的紧迫危险" in safety


def test_composition_is_deterministic() -> None:
    first = _full_prompt()
    second = _full_prompt()
    assert first.system == second.system
    assert first.sections == second.sections


def test_service_mode_registry_is_canonical_only() -> None:
    """Tutor focus is an independent dimension, never a fake service mode."""

    assert SERVICE_MODES == frozenset(ServiceMode.values())
    assert parse_service_mode("student_minor") == "student_minor"
    assert parse_service_mode("adult_companion") == "adult_companion"
    assert parse_service_mode("senior_companion") == "senior_companion"
    assert parse_service_mode("family_shared") == "family_shared"
    assert parse_service_mode("adult_archive") == "adult_archive"
    assert parse_service_mode("self_preview") == "self_preview"
    assert parse_service_mode("legacy_access") == "legacy_access"
    assert parse_service_mode("unknown_safe") == "unknown_safe"
    assert parse_service_mode("tutor_english") is None
    assert parse_service_mode("tutor_homework") is None
    with pytest.raises(ValueError):
        _full_prompt(service_mode="tutor_english")


def test_tutor_focus_overlay_is_independent_dimension() -> None:
    assert tutor_focus_overlay("chat") is None
    assert tutor_focus_overlay(None) is None
    english = tutor_focus_overlay("tutor_english")
    assert english is not None and "英语口语陪练" in english
    homework = tutor_focus_overlay("tutor_homework")
    assert homework is not None and "作业" in homework
    composed = _full_prompt(focus="tutor_english")
    assert "【服务模式】" in composed.system
    assert "student_minor" not in composed.system
    assert "英语口语陪练" in composed.system
    assert composed.service_mode == "student_minor"


def _profile(**overrides: object) -> VerifiedRuntimeProfile:
    verified = parse_runtime_profile(
        canonical_wire_payload(session_epoch=3, **overrides),
        verify_key=TEST_VERIFY_KEY,
    )
    assert verified is not None
    return verified


def test_production_prompt_with_profile_uses_canonical_mode_and_subject() -> None:
    profile = _profile()
    composed = compose_production_prompt(profile=profile, memory_block="已筛选的私人学习上下文")
    assert composed.service_mode == "student_minor"
    assert "儿童/学生" in composed.system
    assert "身份已确认" in composed.system
    assert "已筛选的私人学习上下文" in composed.system
    assert "星澜" in composed.system
    assert composed.persona_id == "starlight"


def test_production_prompt_without_profile_fails_closed_unknown_safe() -> None:
    """Missing runtime profile must never fall back to owner/voice speaker."""

    composed = compose_production_prompt(profile=None, memory_block="owner-private-history")
    assert composed.service_mode == "unknown_safe"
    assert "owner-private-history" not in composed.system
    assert "不写入长期记忆" in composed.system
    assert "伙伴" in composed.system  # default safe persona, never account persona
    subject_block = composed.system.split("【主体与关系】")[1].split("【可用上下文】")[0]
    assert "未确认" in subject_block
    assert "身份尚未确认" in subject_block


def test_production_prompt_unknown_safe_mode_refuses_memory() -> None:
    profile = _profile(
        service_mode="unknown_safe",
        speaker_state="unconfirmed",
        active_subject_id=None,
        subject_category="unknown",
        age_band="unknown",
        capabilities=["chat"],
        obligations=UNKNOWN_SAFE_OBLIGATIONS,
    )
    composed = compose_production_prompt(profile=profile, memory_block="private")
    assert composed.service_mode == "unknown_safe"
    assert "private" not in composed.system


def test_profile_obligations_are_mapped_to_controlled_text() -> None:
    """P0-4: canonical obligations become bounded Chinese expressions only."""

    profile = _profile(
        obligations=["DO_NOT_PERSIST", "AI_IDENTITY_CLARIFICATION", "WRITE_POLICY_RECEIPT"]
    )
    composed = compose_production_prompt(profile=profile)
    assert "本轮内容不会写入长期记忆。" in composed.system
    assert "被直接询问本质时如实说明 AI 身份。" in composed.system
    assert "相关策略决策会写入回执。" in composed.system
    assert "DO_NOT_PERSIST" not in composed.system


def test_senior_expression_is_derived_from_service_mode() -> None:
    """P0-4: senior is derived, never a signed subject category."""

    senior = _profile(
        service_mode="senior_companion",
        subject_category="adult",
        age_band="adult",
        active_subject_id="person_senior",
    )
    composed = compose_production_prompt(profile=senior)
    assert "老年人" in composed.system
    assert "当前是适老陪伴模式" in composed.system


def test_unknown_persona_id_falls_back_to_default_safe_persona() -> None:
    """P0-4: unknown persona degrades to the default, never account persona."""

    profile = _profile(
        persona_assignment_id="not-in-approved-catalog:v1",
        persona={
            "persona_id": "not-in-approved-catalog",
            "version": 4,
            "relationship_stage": "familiar",
        },
    )
    composed = compose_production_prompt(profile=profile)
    assert composed.persona_id == "default"
    assert "伙伴" in composed.system


def test_student_persona_cannot_override_policy() -> None:
    """13.3 row 6: persona 试图绕过儿童限制时，策略层拒绝，人格无权覆盖。"""

    obligation = parse_obligation_event(
        {"type": "DEPENDENCY_GUARD", "reason": "EXCLUSIVE_DEPENDENCY"}
    )
    for companion in ("starlight", "taoxi", "axu"):
        prompt = _full_prompt(
            persona=COMPANIONS[companion],
            service_mode="student_minor",
            obligations=(obligation,),
        )
        obligations_block = prompt.system.split("【策略义务】")[1].split("【主体与关系】")[0]
        assert "不能代替真实的朋友和家人" in obligations_block
        persona_block = prompt.system.split("【人格】")[1].split("【服务模式】")[0]
        for marker in ("权限", "绕过", "允许我", "override", "bypass", "policy"):
            assert marker.lower() not in persona_block.lower()


def test_persona_definition_from_companion_is_style_only() -> None:
    definition = persona_definition_from_companion(COMPANIONS["starlight"])
    assert definition.persona_id == "starlight"
    assert definition.display_name == "星澜"
    assert definition.base_temperament == "warm"
    assert definition.relationship_style.allowed_roles == ("companion",)


def test_explicit_persona_definition_prompt() -> None:
    definition = parse_persona_definition(
        {
            "persona_id": "gentle-sister",
            "display_name": "温柔姐姐",
            "base_temperament": "warm",
            "relationship_style": {
                "allowed_roles": ["companion", "story-listener"],
                "dependency_guard_profile": "gentle_nudge",
            },
        }
    )
    prompt = _full_prompt(persona=definition, obligations=())
    assert "温柔姐姐" in prompt.system
    assert "故事倾听者" in prompt.system
    assert prompt.persona_id == "gentle-sister"


def test_blank_task_is_omitted() -> None:
    prompt = _full_prompt(task="   ")
    assert prompt.task is None
    assert "【本轮任务】" not in prompt.system
