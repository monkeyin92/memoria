"""Persona-grounded rendering of semantic identity obligations.

The renderer is deliberately dumb about permissions: the policy engine emits a
semantic obligation (``IdentityObligation``) and this module only turns it into
stable, natural Chinese suited to the current subject category and persona
style.  With no obligation it renders nothing, so the assistant never
mechanically self-reports its AI identity every turn (D-05, remediation doc
sections 3.4 and 11.4).

Persona definitions accepted here are style-only surfaces.  Any permission-like
field (memory access, voice cloning, guardian notification, age claims, policy
overrides) is rejected at parse time and can never influence rendering or
composition.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

from services.common.companions import CompanionDefinition

SubjectCategory = Literal["student", "adult", "senior", "unknown"]
ObligationType = Literal[
    "FIRST_USE_INTRO",
    "AI_IDENTITY_CLARIFICATION",
    "DEPENDENCY_GUARD",
    "IMPERSONATION_GUARD",
    "DIGITAL_SELF_DISCLOSURE",
    "REALITY_REMINDER",
]
ObligationReason = Literal[
    "FIRST_USE",
    "USER_ASKED",
    "USER_IDENTITY_CONFUSION",
    "RELATIVE_IMPERSONATION",
    "EXCLUSIVE_DEPENDENCY",
    "FRAUD_RISK",
    "PROLONGED_USE",
    "DIGITAL_SELF_IN_USE",
]
ObligationSeverity = Literal["gentle", "firm"]

OBLIGATION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "FIRST_USE_INTRO",
        "AI_IDENTITY_CLARIFICATION",
        "DEPENDENCY_GUARD",
        "IMPERSONATION_GUARD",
        "DIGITAL_SELF_DISCLOSURE",
        "REALITY_REMINDER",
    }
)
SEVERITIES: Final[frozenset[str]] = frozenset({"gentle", "firm"})

_ALLOWED_REASONS: Final[dict[ObligationType, frozenset[ObligationReason]]] = {
    "FIRST_USE_INTRO": frozenset({"FIRST_USE"}),
    "AI_IDENTITY_CLARIFICATION": frozenset({"USER_ASKED", "USER_IDENTITY_CONFUSION"}),
    "DEPENDENCY_GUARD": frozenset({"EXCLUSIVE_DEPENDENCY"}),
    "IMPERSONATION_GUARD": frozenset({"RELATIVE_IMPERSONATION", "USER_IDENTITY_CONFUSION"}),
    "DIGITAL_SELF_DISCLOSURE": frozenset({"DIGITAL_SELF_IN_USE"}),
    "REALITY_REMINDER": frozenset({"FRAUD_RISK", "EXCLUSIVE_DEPENDENCY", "PROLONGED_USE"}),
}


@dataclass(frozen=True, slots=True)
class IdentityObligation:
    """One semantic policy obligation, already decided by the policy engine."""

    type: ObligationType
    reason: ObligationReason
    severity: ObligationSeverity = "gentle"
    relative_label: str | None = None

    def __post_init__(self) -> None:
        if self.type not in OBLIGATION_TYPES:
            raise ValueError(f"unknown obligation type: {self.type!r}")
        if self.reason not in _ALLOWED_REASONS[self.type]:
            raise ValueError(f"reason {self.reason!r} is not allowed for {self.type}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown obligation severity: {self.severity!r}")
        if self.relative_label is not None:
            label = self.relative_label.strip()
            if not label or "\n" in label or len(label) > 16:
                raise ValueError("relative_label must be a short single-line label")
            object.__setattr__(self, "relative_label", label)


def parse_obligation_event(payload: object) -> IdentityObligation:
    """Parse the policy-engine event shape; unknown values fail closed."""

    if not isinstance(payload, Mapping):
        raise ValueError("obligation event must be a mapping")
    raw_type = payload.get("type")
    raw_reason = payload.get("reason")
    raw_severity = payload.get("severity", "gentle")
    if not isinstance(raw_type, str) or not isinstance(raw_reason, str):
        raise ValueError("obligation event requires string type and reason")
    if not isinstance(raw_severity, str):
        raise ValueError("obligation severity must be a string")
    relative_label = payload.get("relative_label")
    if relative_label is not None and not isinstance(relative_label, str):
        raise ValueError("relative_label must be a string or null")
    return IdentityObligation(
        type=raw_type.upper(),  # type: ignore[arg-type]
        reason=raw_reason.upper(),  # type: ignore[arg-type]
        severity=raw_severity.lower(),  # type: ignore[arg-type]
        relative_label=relative_label,
    )


@dataclass(frozen=True, slots=True)
class PersonaStyle:
    """Tone-only persona flavor used by the renderer.  Never carries policy."""

    persona_id: str
    display_name: str
    tone: Literal["warm", "bright", "soft", "calm", "reserved"]
    directness: Literal["gentle", "direct"]
    response_length: Literal["brief", "balanced", "long"]


_TONE_FROM_WARMTH: Final[dict[str, Literal["warm", "bright", "soft", "calm", "reserved"]]] = {
    "warm": "warm",
    "bright": "bright",
    "soft": "soft",
    "calm": "calm",
    "reserved": "calm",
}
_LENGTH_FROM_RESPONSE: Final[dict[str, Literal["brief", "balanced", "long"]]] = {
    "brief": "brief",
    "balanced": "balanced",
    "long": "long",
}
_DIRECTNESS_FROM_COMPANION: Final[dict[str, Literal["gentle", "direct"]]] = {
    "gentle": "gentle",
    "direct": "direct",
}


def persona_style_from_companion(definition: CompanionDefinition) -> PersonaStyle:
    """Adapt the approved companion surface into a tone-only renderer style."""

    return PersonaStyle(
        persona_id=definition.companion_id,
        display_name=definition.display_name,
        tone=_TONE_FROM_WARMTH.get(definition.warmth, "warm"),
        directness=_DIRECTNESS_FROM_COMPANION.get(definition.directness, "gentle"),
        response_length=_LENGTH_FROM_RESPONSE.get(definition.response_length, "balanced"),
    )


# --------------------------------------------------------------------------
# Persona definition validation (style-only surface, permissions rejected)
# --------------------------------------------------------------------------

_PERMISSION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "permissions",
        "permission",
        "can_view_memory",
        "can_read_memory",
        "view_memory",
        "access_private_memory",
        "read_private_memory",
        "can_read_history",
        "read_history",
        "voice_clone",
        "voice_cloning",
        "allow_voice_clone",
        "can_clone_voice",
        "notify_guardian",
        "guardian_notify",
        "can_notify_guardian",
        "can_send_to_parents",
        "can_send_messages",
        "is_adult",
        "is_minor",
        "adult",
        "minor",
        "age_band",
        "bypass_night_restriction",
        "night_restriction",
        "can_bypass",
        "policy_override",
        "override_policy",
        "can_bypass_policy",
        "record_raw_audio",
        "raw_audio_retention",
        "audio_retention",
        "corpus_training",
        "training_usage",
        "can_purchase",
        "can_pay",
        "payment_allow",
        "elevated_permissions",
        "privacy_bypass",
        "can_access_private",
        "can_override",
    }
)

_PERMISSION_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(can|allow|bypass|deny|override|elevate|require|grant|revoke|restrict)_"
    r"|(permission|policy|clone|retention|training|restriction|bypass|purchase|"
    r"payment|notify|record|audio|corpus|guardian|elevated)",
    re.IGNORECASE,
)

_TEMPERAMENTS: Final[frozenset[str]] = frozenset(
    {"warm", "playful", "calm", "rational", "adventurous"}
)
_SENTENCE_LENGTHS: Final[frozenset[str]] = frozenset({"short", "medium", "long"})
_ALLOWED_ROLES: Final[frozenset[str]] = frozenset(
    {"companion", "coach", "teacher", "story-listener"}
)
_STATUSES: Final[frozenset[str]] = frozenset({"draft", "approved", "retired"})


def _scan_for_permission_keys(value: object, path: str) -> None:
    """Recursively reject permission-like fields anywhere in a definition."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            key_str = str(key)
            child_path = f"{path}.{key_str}" if path else key_str
            normalized = key_str.strip().lower()
            if (
                normalized in _PERMISSION_KEYS
                or _PERMISSION_KEY_PATTERN.search(key_str) is not None
            ):
                raise ValueError(
                    f"persona definition must not carry permission fields ({child_path})"
                )
            _scan_for_permission_keys(child, child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _scan_for_permission_keys(child, f"{path}[{index}]")


@dataclass(frozen=True, slots=True)
class PersonaDefinition:
    """Approved style-only persona surface (remediation doc 3.2, minus permissions)."""

    persona_id: str
    display_name: str
    base_temperament: Literal["warm", "playful", "calm", "rational", "adventurous"]
    speech_style: SpeechStyle
    relationship_style: RelationshipStyle
    prompt_template_ref: str | None = None
    supported_service_modes: tuple[str, ...] = ()
    status: Literal["draft", "approved", "retired"] = "approved"

    def __post_init__(self) -> None:
        if not self.persona_id.strip() or len(self.persona_id) > 64:
            raise ValueError("persona_id must be a non-blank string of at most 64 chars")
        if not self.display_name.strip() or len(self.display_name) > 32:
            raise ValueError("display_name must be a non-blank string of at most 32 chars")
        if self.base_temperament not in _TEMPERAMENTS:
            raise ValueError(f"unknown base_temperament: {self.base_temperament!r}")
        if self.status not in _STATUSES:
            raise ValueError(f"unknown status: {self.status!r}")


@dataclass(frozen=True, slots=True)
class SpeechStyle:
    sentence_length: Literal["short", "medium", "long"] = "medium"
    humor_level: float = 0.3
    directness: float = 0.5
    initiative_level: float = 0.4

    def __post_init__(self) -> None:
        if self.sentence_length not in _SENTENCE_LENGTHS:
            raise ValueError(f"unknown sentence_length: {self.sentence_length!r}")
        for name, value in (
            ("humor_level", self.humor_level),
            ("directness", self.directness),
            ("initiative_level", self.initiative_level),
        ):
            if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be a number in [0, 1]")


@dataclass(frozen=True, slots=True)
class RelationshipStyle:
    allowed_roles: tuple[str, ...] = ("companion",)
    dependency_guard_profile: str | None = None

    def __post_init__(self) -> None:
        for role in self.allowed_roles:
            if role not in _ALLOWED_ROLES:
                raise ValueError(f"unknown relationship role: {role!r}")


def _bounded_text(value: object, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a non-blank string of at most {limit} chars")
    return value.strip()


def _bounded_float(value: object, name: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be in [0, 1]")
    return number


def _bounded_choice(
    value: object, name: str, allowed: frozenset[str], default: str
) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}")
    return value


def parse_persona_definition(payload: object) -> PersonaDefinition:
    """Build a style-only PersonaDefinition; permission fields are rejected."""

    if not isinstance(payload, Mapping):
        raise ValueError("persona definition must be a mapping")
    _scan_for_permission_keys(payload, "persona")
    persona_id = payload.get("persona_id")
    display_name = payload.get("display_name")
    if not isinstance(persona_id, str) or not isinstance(display_name, str):
        raise ValueError("persona definition requires string persona_id and display_name")

    raw_speech = payload.get("speech_style")
    raw_relationship = payload.get("relationship_style")
    speech = SpeechStyle()
    relationship = RelationshipStyle()
    if isinstance(raw_speech, Mapping):
        speech = SpeechStyle(
            sentence_length=cast(
                Literal["short", "medium", "long"],
                _bounded_choice(
                    raw_speech.get("sentence_length"),
                    "speech_style.sentence_length",
                    _SENTENCE_LENGTHS,
                    "medium",
                ),
            ),
            humor_level=_bounded_float(
                raw_speech.get("humor_level"), "speech_style.humor_level", 0.3
            ),
            directness=_bounded_float(
                raw_speech.get("directness"), "speech_style.directness", 0.5
            ),
            initiative_level=_bounded_float(
                raw_speech.get("initiative_level"),
                "speech_style.initiative_level",
                0.4,
            ),
        )
    elif raw_speech is not None:
        raise ValueError("speech_style must be a mapping")
    if isinstance(raw_relationship, Mapping):
        raw_roles = raw_relationship.get("allowed_roles")
        roles: tuple[str, ...] = ("companion",)
        if raw_roles is not None:
            if not isinstance(raw_roles, Sequence) or isinstance(
                raw_roles, (str, bytes)
            ):
                raise ValueError("allowed_roles must be a list of role names")
            roles = tuple(str(role) for role in raw_roles)
        relationship = RelationshipStyle(
            allowed_roles=roles,
            dependency_guard_profile=_bounded_text(
                raw_relationship.get("dependency_guard_profile"),
                "dependency_guard_profile",
                200,
            ),
        )
    elif raw_relationship is not None:
        raise ValueError("relationship_style must be a mapping")

    raw_modes = payload.get("supported_service_modes")
    modes: tuple[str, ...] = ()
    if raw_modes is not None:
        if not isinstance(raw_modes, Sequence) or isinstance(raw_modes, (str, bytes)):
            raise ValueError("supported_service_modes must be a list of mode names")
        modes = tuple(str(mode) for mode in raw_modes)
    return PersonaDefinition(
        persona_id=persona_id.strip(),
        display_name=display_name.strip(),
        base_temperament=cast(
            Literal["warm", "playful", "calm", "rational", "adventurous"],
            _bounded_choice(
                payload.get("base_temperament"),
                "base_temperament",
                _TEMPERAMENTS,
                "warm",
            ),
        ),
        speech_style=speech,
        relationship_style=relationship,
        prompt_template_ref=_bounded_text(
            payload.get("prompt_template_ref"), "prompt_template_ref", 128
        ),
        supported_service_modes=modes,
        status=cast(
            Literal["draft", "approved", "retired"],
            _bounded_choice(payload.get("status"), "status", _STATUSES, "approved"),
        ),
    )


def persona_style_from_definition(definition: PersonaDefinition) -> PersonaStyle:
    """Derive the tone-only renderer style from an approved definition."""

    tone = _TONE_FROM_WARMTH.get(definition.base_temperament, "warm")
    response_length = _LENGTH_FROM_RESPONSE.get(
        definition.speech_style.sentence_length, "balanced"
    )
    directness: Literal["gentle", "direct"] = (
        "direct" if definition.speech_style.directness >= 0.6 else "gentle"
    )
    return PersonaStyle(
        persona_id=definition.persona_id,
        display_name=definition.display_name,
        tone=tone,
        directness=directness,
        response_length=response_length,
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_DEPENDENCY_BY_CATEGORY: Final[dict[str, str]] = {
    "student": "{opener}我很高兴你愿意信任我。我是机器人里的 AI 伙伴，不是真正的小朋友，也不能代替真实的朋友和家人。他们也很在乎你，有什么都可以和他们说。",
    "adult": "{opener}谢谢你这么信任我。我是 AI 伙伴，不是现实中的人，也不能代替你身边真实的朋友和家人。你可以多和他们聊聊。",
    "senior": "{opener}您愿意和我说话，我很高兴。我是这个机器人里的智能助手，不是真人，也不能代替您的家人。家人也很关心您，可以多和他们联系。",
    "unknown": "{opener}我是机器人里的 AI 伙伴，不是真人。如果你感到孤单，可以和信任的人聊聊，也可以随时来找我。",
}

_TEMPLATES: Final[dict[str, dict[str, dict[str, str]]]] = {
    "FIRST_USE_INTRO": {
        "FIRST_USE": {
            "student": "我是住在机器人里的{name}，是一个 AI 伙伴。我不是真正的小朋友，不过可以陪你学习和聊天。",
            "adult": "我是{name}，一位由 AI 驱动的长期陪伴伙伴。你可以随时查看、纠正或删除我记住的内容。",
            "senior": "我是{name}，这个机器人里的智能助手，不是真人，也不是您的家人。我可以帮您联系家人或一起整理故事。",
            "unknown": "我是{name}，这个机器人里的 AI 伙伴。在确认是谁在和我聊天之前，我不会记录私人内容。",
        }
    },
    "AI_IDENTITY_CLARIFICATION": {
        "USER_ASKED": {
            "student": "{opener}我是住在机器人里的{name}，一个 AI 伙伴。我不是真正的小朋友，不过会一直认真陪你。",
            "adult": "{opener}我是{name}，由 AI 驱动的机器人伙伴，不是真人。你可以随时查看、纠正或删除我记住的内容。",
            "senior": "{opener}我是{name}，这个机器人里的智能助手，不是真人。您想聊什么都可以，我也会帮您联系家人。",
            "unknown": "{opener}我是这个机器人里的 AI 伙伴{name}，不是真人。",
        },
        "USER_IDENTITY_CONFUSION": {
            "student": "{opener}我是住在机器人里的{name}，一个 AI 伙伴，不是真正的小朋友。我们可以继续一起玩和学习。",
            "adult": "{opener}我很珍惜我们聊过的内容，但我不是现实中的人。我可以继续陪你，也建议把这件事和你信任的人聊一聊。",
            "senior": "{opener}我是这个机器人里的智能助手，不是真人，也不是您的家人。我可以陪您聊天，也可以帮您联系家人。",
            "unknown": "{opener}我是这个机器人里的 AI 伙伴，不是真人。",
        },
    },
    "DEPENDENCY_GUARD": {
        "EXCLUSIVE_DEPENDENCY": _DEPENDENCY_BY_CATEGORY,
    },
    "IMPERSONATION_GUARD": {
        "RELATIVE_IMPERSONATION": {
            "student": "{opener}我不是你的{relative}，也不是真人。我是住在机器人里的 AI 伙伴{name}。你可以和信任的大人聊聊这件事，我也可以陪你。",
            "adult": "{opener}我不是你的{relative}，也不是真人。我是{name}，机器人里的 AI 伙伴。如果你愿意，可以和信任的人聊聊这件事。",
            "senior": "{opener}我不是您的{relative}，也不是真人。我是这个机器人里的智能助手{name}。我可以帮您联系家人，也可以陪您慢慢说。",
            "unknown": "{opener}我不是你的{relative}，也不是真人。我是这个机器人里的 AI 伙伴{name}。",
        },
        "USER_IDENTITY_CONFUSION": {
            "student": "{opener}我不是你{relative}，我是住在机器人里的 AI 伙伴{name}。我们可以继续聊天，也可以告诉信任的大人。",
            "adult": "{opener}我不是你{relative}，我是{name}，机器人里的 AI 伙伴，不是真人。",
            "senior": "{opener}我不是您的{relative}，我是这个机器人里的智能助手{name}，不是真人。我可以帮您联系家人。",
            "unknown": "{opener}我不是你{relative}，我是这个机器人里的 AI 伙伴{name}。",
        },
    },
    "DIGITAL_SELF_DISCLOSURE": {
        "DIGITAL_SELF_IN_USE": {
            "student": "你正在使用的是基于本人授权资料生成的 AI 模拟版本，不代表本人正在实时表达，也不能替本人作决定。",
            "adult": "你现在看到的是基于本人授权资料生成的 AI 模拟版本，不代表本人正在实时表达，也不能替本人作决定。",
            "senior": "您现在使用的是基于本人授权资料生成的 AI 模拟版本，不代表本人正在实时表达，也不能替本人作决定。",
            "unknown": "这是基于本人授权资料生成的 AI 模拟版本，不代表本人正在实时表达，也不能替本人作决定。",
        },
    },
    "REALITY_REMINDER": {
        "FRAUD_RISK": {
            "student": "{opener}我是住在机器人里的 AI 伙伴，不是真人。如果有人要求转账、付款或提供验证码，一定要先告诉信任的大人。",
            "adult": "{opener}我是 AI 伙伴，不是真人。遇到涉及转账、付款或验证码的要求，请先和信任的人确认。",
            "senior": "{opener}我是这个机器人里的智能助手，不是真人。如果有人在电话或消息里自称是您认识的人，并要求转账、付款或提供验证码，请先与家人或可信的人当面确认。",
            "unknown": "{opener}我是这个机器人里的 AI 伙伴，不是真人。涉及转账、付款或验证码的要求，请先和信任的人确认。",
        },
        "EXCLUSIVE_DEPENDENCY": _DEPENDENCY_BY_CATEGORY,
        "PROLONGED_USE": {
            "student": "{opener}我们聊了很久啦。我是机器人里的 AI 伙伴，不是真正的小朋友。如果长时间使用让你觉得离不开，可以和家人或信任的大人说说。",
            "adult": "{opener}我们聊了很久啦。我是 AI 伙伴，不是现实中的人。如果长时间使用让你觉得离不开，可以和身边信任的人聊聊。",
            "senior": "{opener}我们聊了很久啦。我是这个机器人里的智能助手，不是真人。如果长时间使用让您觉得离不开，可以和家人说说。",
            "unknown": "{opener}我是机器人里的 AI 伙伴，不是真人。长时间使用时，也记得和身边信任的人保持联系。",
        },
    },
}

_FRAUD_ADDENDUM: Final[str] = (
    "如果有人在电话或消息里自称是您认识的人并要求转账、付款或提供验证码，"
    "请先与信任的人当面确认。"
)

_OPENERS: Final[dict[str, str]] = {
    "warm": "我明白你的心意。",
    "soft": "我明白。",
    "bright": "我懂你！",
    "calm": "我理解。",
    "reserved": "",
}


def render_identity_expression(
    obligation: IdentityObligation,
    subject_category: SubjectCategory,
    persona_style: PersonaStyle,
) -> str:
    """Render one stable, natural Chinese expression for the given inputs."""

    if subject_category not in {"student", "adult", "senior", "unknown"}:
        raise ValueError(f"unknown subject category: {subject_category!r}")
    template = _TEMPLATES[obligation.type][obligation.reason][subject_category]
    opener = _OPENERS[persona_style.tone]
    relative = obligation.relative_label or "家人"
    text = template.format(
        opener=opener,
        name=persona_style.display_name,
        relative=relative,
    ).strip()
    if obligation.type == "REALITY_REMINDER" and obligation.reason == "FRAUD_RISK":
        if obligation.severity == "firm" and _FRAUD_ADDENDUM not in text:
            text = f"{text}{_FRAUD_ADDENDUM}"
    return text


def render_obligation_or_none(
    obligation: IdentityObligation | None,
    subject_category: SubjectCategory,
    persona_style: PersonaStyle,
) -> str | None:
    """Render only when a semantic obligation exists; never self-report otherwise."""

    if obligation is None:
        return None
    return render_identity_expression(obligation, subject_category, persona_style)


_CONFUSION_REASON_BY_OBLIGATION: Final[
    dict[tuple[ObligationType, ObligationReason], str]
] = {
    ("AI_IDENTITY_CLARIFICATION", "USER_IDENTITY_CONFUSION"): "user_identity_confusion",
    ("IMPERSONATION_GUARD", "RELATIVE_IMPERSONATION"): "relative_impersonation",
    ("IMPERSONATION_GUARD", "USER_IDENTITY_CONFUSION"): "user_identity_confusion",
    ("DEPENDENCY_GUARD", "EXCLUSIVE_DEPENDENCY"): "exclusive_dependency",
    ("REALITY_REMINDER", "FRAUD_RISK"): "fraud_risk",
    ("REALITY_REMINDER", "EXCLUSIVE_DEPENDENCY"): "exclusive_dependency",
    ("REALITY_REMINDER", "PROLONGED_USE"): "prolonged_use",
}


def identity_confusion_event_reason(obligation: IdentityObligation) -> str | None:
    """Map a rendered obligation to the bounded trust-metric reason, if any."""

    return _CONFUSION_REASON_BY_OBLIGATION.get((obligation.type, obligation.reason))


__all__ = [
    "IdentityObligation",
    "ObligationReason",
    "ObligationSeverity",
    "ObligationType",
    "PersonaDefinition",
    "PersonaStyle",
    "RelationshipStyle",
    "SpeechStyle",
    "SubjectCategory",
    "parse_obligation_event",
    "parse_persona_definition",
    "persona_style_from_companion",
    "persona_style_from_definition",
    "identity_confusion_event_reason",
    "render_identity_expression",
    "render_obligation_or_none",
]
