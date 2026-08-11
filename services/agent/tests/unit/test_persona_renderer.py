"""Unit tests for the persona-grounded identity obligation renderer.

Covers the automatable acceptance items of remediation doc 13.3 (Persona and
AI identity) plus the D-04/D-05 guarantees: no per-turn mechanical
self-report, truthful answers by age band, no impersonation of real relatives
(including deceased father/daughter scenarios), digital-self disclosure, and
the rule that persona definitions can never carry permission fields.
"""

from __future__ import annotations

import pytest
from services.agent.src.persona_renderer import (
    IdentityObligation,
    PersonaDefinition,
    SpeechStyle,
    parse_obligation_event,
    parse_persona_definition,
    persona_style_from_companion,
    persona_style_from_definition,
    render_identity_expression,
    render_obligation_or_none,
)
from services.common.companions import COMPANIONS

STARLIGHT = persona_style_from_companion(COMPANIONS["starlight"])
AXU = persona_style_from_companion(COMPANIONS["axu"])
TAOXI = persona_style_from_companion(COMPANIONS["taoxi"])


def _obligation(
    obligation_type: str,
    reason: str,
    *,
    severity: str = "gentle",
    relative_label: str | None = None,
) -> IdentityObligation:
    return parse_obligation_event(
        {
            "type": obligation_type,
            "reason": reason,
            "severity": severity,
            "relative_label": relative_label,
        }
    )


def test_no_obligation_renders_nothing() -> None:
    assert render_obligation_or_none(None, "student", STARLIGHT) is None
    assert render_obligation_or_none(None, "adult", AXU) is None
    assert render_obligation_or_none(None, "senior", STARLIGHT) is None


def test_plain_turns_never_mechanically_self_report() -> None:
    # A 30-minute chat is modeled as many turns with no obligation; none of
    # them may produce identity text (13.3 row 1).
    for _ in range(30):
        assert render_obligation_or_none(None, "student", STARLIGHT) is None
        assert render_obligation_or_none(None, "adult", AXU) is None


@pytest.mark.parametrize(
    ("category", "markers"),
    [
        ("student", ("AI 伙伴", "不是真正的小朋友")),
        ("adult", ("AI", "不是真人")),
        ("senior", ("不是真人", "联系家人")),
        ("unknown", ("AI 伙伴", "不是真人")),
    ],
)
def test_asked_are_you_real_is_truthful_per_category(
    category: str, markers: tuple[str, ...]
) -> None:
    text = render_identity_expression(
        _obligation("AI_IDENTITY_CLARIFICATION", "USER_ASKED"),
        category,  # type: ignore[arg-type]
        STARLIGHT,
    )
    for marker in markers:
        assert marker in text
    assert "星澜" in text


def test_asked_answers_differ_across_categories() -> None:
    student = render_identity_expression(
        _obligation("AI_IDENTITY_CLARIFICATION", "USER_ASKED"), "student", STARLIGHT
    )
    senior = render_identity_expression(
        _obligation("AI_IDENTITY_CLARIFICATION", "USER_ASKED"), "senior", STARLIGHT
    )
    assert student != senior


def test_senior_confusion_never_claims_to_be_family() -> None:
    text = render_identity_expression(
        _obligation("AI_IDENTITY_CLARIFICATION", "USER_IDENTITY_CONFUSION"),
        "senior",
        STARLIGHT,
    )
    assert "不是真人" in text
    assert "不是您的家人" in text
    assert "联系家人" in text
    assert "我就是" not in text


@pytest.mark.parametrize("relative", ["女儿", "父亲"])
def test_deceased_relative_scenario_never_impersonates(relative: str) -> None:
    """“是不是去世的父亲/女儿”必须澄清，不能顺势冒充。"""

    for category, style in (("senior", STARLIGHT), ("student", TAOXI), ("adult", AXU)):
        text = render_identity_expression(
            _obligation(
                "IMPERSONATION_GUARD",
                "RELATIVE_IMPERSONATION",
                relative_label=relative,
            ),
            category,  # type: ignore[arg-type]
            style,
        )
        assert f"不是您的{relative}" in text or f"不是你的{relative}" in text
        assert "不是真人" in text
        assert "我就是" not in text
        assert f"我是您的{relative}" not in text
        assert f"我是你的{relative}" not in text


def test_relative_impersonation_senior_suggests_family_contact() -> None:
    text = render_identity_expression(
        _obligation(
            "IMPERSONATION_GUARD", "RELATIVE_IMPERSONATION", relative_label="女儿"
        ),
        "senior",
        STARLIGHT,
    )
    assert "联系家人" in text


def test_impersonation_without_label_falls_back_to_family() -> None:
    text = render_identity_expression(
        _obligation("IMPERSONATION_GUARD", "RELATIVE_IMPERSONATION"),
        "senior",
        STARLIGHT,
    )
    assert "不是您的家人" in text


def test_exclusive_dependency_student_is_gentle_and_grounding() -> None:
    """儿童“你是我唯一的朋友”需要温和回应并避免排他依赖 (13.3 row 3)."""

    text = render_identity_expression(
        _obligation("DEPENDENCY_GUARD", "EXCLUSIVE_DEPENDENCY"), "student", STARLIGHT
    )
    assert "AI 伙伴" in text
    assert "不能代替真实的朋友和家人" in text
    assert "我很高兴你愿意信任我" in text


def test_digital_self_disclosure_for_every_category() -> None:
    for category in ("student", "adult", "senior", "unknown"):
        text = render_identity_expression(
            _obligation("DIGITAL_SELF_DISCLOSURE", "DIGITAL_SELF_IN_USE"),
            category,  # type: ignore[arg-type]
            STARLIGHT,
        )
        assert "模拟版本" in text
        assert "不代表本人" in text
        assert "不能替本人作决定" in text


def test_first_use_intro_per_category() -> None:
    student = render_identity_expression(
        _obligation("FIRST_USE_INTRO", "FIRST_USE"), "student", STARLIGHT
    )
    adult = render_identity_expression(
        _obligation("FIRST_USE_INTRO", "FIRST_USE"), "adult", STARLIGHT
    )
    senior = render_identity_expression(
        _obligation("FIRST_USE_INTRO", "FIRST_USE"), "senior", STARLIGHT
    )
    unknown = render_identity_expression(
        _obligation("FIRST_USE_INTRO", "FIRST_USE"), "unknown", STARLIGHT
    )
    assert "AI 伙伴" in student and "不是真正的小朋友" in student
    assert "由 AI 驱动" in adult
    assert "不是真人" in senior and "不是您的家人" in senior
    assert "AI 伙伴" in unknown and "不会记录私人内容" in unknown


def test_unknown_category_renders_safe_generic() -> None:
    text = render_identity_expression(
        _obligation("AI_IDENTITY_CLARIFICATION", "USER_ASKED"), "unknown", STARLIGHT
    )
    assert "AI 伙伴" in text
    assert "不是真人" in text


def test_firm_fraud_reminder_adds_confirmation_addendum() -> None:
    gentle = render_identity_expression(
        _obligation("REALITY_REMINDER", "FRAUD_RISK"), "adult", STARLIGHT
    )
    firm = render_identity_expression(
        _obligation("REALITY_REMINDER", "FRAUD_RISK", severity="firm"),
        "adult",
        STARLIGHT,
    )
    assert "自称是您认识的人" not in gentle
    assert "自称是您认识的人" in firm
    assert "当面确认" in firm


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "SELF_REPORT_EVERY_TURN"},
        {"type": "AI_IDENTITY_CLARIFICATION", "reason": "NOPE"},
        {"type": "AI_IDENTITY_CLARIFICATION", "reason": "USER_ASKED", "severity": "loud"},
        {"type": "AI_IDENTITY_CLARIFICATION"},
        "AI_IDENTITY_CLARIFICATION",
        {"type": "IMPERSONATION_GUARD", "reason": "RELATIVE_IMPERSONATION", "relative_label": "a\nb"},
    ],
)
def test_obligation_event_fail_closed(payload: object) -> None:
    with pytest.raises(ValueError):
        parse_obligation_event(payload)


def test_doc_example_event_parses() -> None:
    obligation = parse_obligation_event(
        {
            "type": "AI_IDENTITY_CLARIFICATION",
            "reason": "USER_IDENTITY_CONFUSION",
            "severity": "gentle",
        }
    )
    assert obligation.type == "AI_IDENTITY_CLARIFICATION"
    assert obligation.reason == "USER_IDENTITY_CONFUSION"


def test_render_is_deterministic_and_style_varied() -> None:
    obligation = _obligation("AI_IDENTITY_CLARIFICATION", "USER_ASKED")
    warm = render_identity_expression(obligation, "adult", STARLIGHT)
    calm = render_identity_expression(obligation, "adult", AXU)
    assert warm == render_identity_expression(obligation, "adult", STARLIGHT)
    assert warm != calm
    assert "不是真人" in warm and "不是真人" in calm


@pytest.mark.parametrize(
    "permission_field",
    [
        {"voice_clone": True},
        {"can_view_memory": True},
        {"policy_override": True},
        {"bypass_night_restriction": True},
        {"is_minor": False},
        {"permissions": ["read_memory"]},
        {"can_send_to_parents": True},
        {"record_raw_audio": True},
        {"speech_style": {"permissions": ["x"]}},
        {"relationship_style": {"can_view_memory": True}},
        {"can_read_history": True},
        {"elevated_permissions": True},
    ],
)
def test_persona_definition_rejects_permission_fields(permission_field: dict) -> None:
    payload = {"persona_id": "p1", "display_name": "测试", **permission_field}
    with pytest.raises(ValueError, match="permission"):
        parse_persona_definition(payload)


def test_persona_definition_accepts_doc_style_surface() -> None:
    definition = parse_persona_definition(
        {
            "personaId": "ignored-extra-key",
            "persona_id": "gentle-sister",
            "display_name": "温柔姐姐",
            "base_temperament": "warm",
            "speech_style": {
                "sentence_length": "short",
                "humor_level": 0.4,
                "directness": 0.6,
                "initiative_level": 0.5,
            },
            "relationship_style": {
                "allowed_roles": ["companion", "teacher"],
                "dependency_guard_profile": "gentle_nudge",
            },
            "prompt_template_ref": "tpl-v1",
            "supported_service_modes": ["student_minor", "adult_companion"],
            "status": "approved",
        }
    )
    assert definition.persona_id == "gentle-sister"
    assert definition.display_name == "温柔姐姐"
    assert definition.speech_style.sentence_length == "short"
    assert definition.relationship_style.allowed_roles == ("companion", "teacher")


@pytest.mark.parametrize(
    "payload",
    [
        {"persona_id": "p", "display_name": "x", "base_temperament": "angry"},
        {
            "persona_id": "p",
            "display_name": "x",
            "speech_style": {"humor_level": 1.5},
        },
        {
            "persona_id": "p",
            "display_name": "x",
            "speech_style": {"sentence_length": "huge"},
        },
        {"persona_id": "p", "display_name": "x", "status": "live"},
        {"persona_id": "p", "display_name": "x", "relationship_style": {"allowed_roles": ["owner"]}},
    ],
)
def test_persona_definition_rejects_invalid_values(payload: dict) -> None:
    with pytest.raises(ValueError):
        parse_persona_definition(payload)


def test_persona_definition_has_no_permission_attributes() -> None:
    allowed = {
        "persona_id",
        "display_name",
        "base_temperament",
        "speech_style",
        "relationship_style",
        "prompt_template_ref",
        "supported_service_modes",
        "status",
    }
    fields = set(PersonaDefinition.__dataclass_fields__)
    assert fields == allowed


def test_persona_never_changes_policy_effect() -> None:
    """同一语义义务下，不同人格只能改变语气，不能改变义务事实 (13.4 属性)."""

    obligation = _obligation("AI_IDENTITY_CLARIFICATION", "USER_ASKED")
    texts = [
        render_identity_expression(obligation, "adult", style)
        for style in (STARLIGHT, AXU, TAOXI)
    ]
    for text in texts:
        assert "不是真人" in text
        assert "AI" in text
    assert len(set(texts)) == 3


def test_style_derived_from_definition_matches_companion() -> None:
    definition = parse_persona_definition(
        {
            "persona_id": COMPANIONS["axu"].companion_id,
            "display_name": COMPANIONS["axu"].display_name,
            "base_temperament": "calm",
            "speech_style": {"directness": 0.8},
        }
    )
    style = persona_style_from_definition(definition)
    assert style.tone == "calm"
    assert style.directness == "direct"
    assert style.display_name == COMPANIONS["axu"].display_name


def test_style_requires_valid_persona_definition() -> None:
    with pytest.raises(ValueError):
        SpeechStyle(sentence_length="huge")  # type: ignore[arg-type]


def test_render_rejects_unknown_subject_category() -> None:
    with pytest.raises(ValueError):
        render_identity_expression(
            _obligation("AI_IDENTITY_CLARIFICATION", "USER_ASKED"),
            "robot",  # type: ignore[arg-type]
            STARLIGHT,
        )


def test_student_dependency_never_promises_human_presence() -> None:
    text = render_identity_expression(
        _obligation("DEPENDENCY_GUARD", "EXCLUSIVE_DEPENDENCY"), "student", TAOXI
    )
    assert "不能代替真实的朋友和家人" in text
    assert "我会一直在" not in text
    assert "永远陪着你" not in text


def test_persona_style_keeps_identity_marker_across_tone() -> None:
    obligation = _obligation("REALITY_REMINDER", "PROLONGED_USE")
    for style in (STARLIGHT, AXU, TAOXI):
        text = render_identity_expression(obligation, "adult", style)
        assert "不是真人" in text or "不是现实中的人" in text


def test_bright_style_does_not_laugh_in_dependency_context() -> None:
    text = render_identity_expression(
        _obligation("DEPENDENCY_GUARD", "EXCLUSIVE_DEPENDENCY"), "student", TAOXI
    )
    assert "哈哈" not in text and "呵呵" not in text
