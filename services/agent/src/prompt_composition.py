"""Fixed-order, minimal-context system prompt composition.

Section order is frozen (remediation doc 11.3): immutable safety baseline ->
Persona -> Service Mode -> Policy Obligations -> subject/relationship minimal
context -> filtered memory context -> task.

Service Mode is the canonical contract registry only (remediation doc 4.1 /
PR-10): student_minor, adult_companion, senior_companion, family_shared,
adult_archive, self_preview, legacy_access and unknown_safe.  Tutor focus is an
independent delivery dimension (``focus``), never a fake service mode.

Permissions are enforced in code: the composer only accepts semantic
obligations (or already-rendered obligation text) and never raw permission
flags, and the persona block is a style-only surface that cannot carry
permission statements.  Different personas therefore never change policy
effect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

from packages.contracts.generated.python.multi_subject_contracts import (
    ServiceMode as GeneratedServiceMode,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    ServiceModeValue,
)

from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.persona_renderer import (
    IdentityObligation,
    PersonaDefinition,
    RelationshipStyle,
    SpeechStyle,
    SubjectCategory,
    identity_confusion_event_reason,
    persona_style_from_definition,
    render_identity_expression,
)
from services.agent.src.prompts import SAFETY_CORE_TRANSPARENT
from services.agent.src.runtime_profile import (
    VerifiedRuntimeProfile,
    subject_context_from_profile,
)
from services.common.companions import (
    CompanionDefinition,
    companion_definition,
)
from services.tutor.domain import SessionFocus, TutorFocus
from services.tutor.prompts import TUTOR_STYLE, focus_style

PROMPT_COMPOSITION_VERSION: Final = "prompt-composition-v1"

type ServiceMode = ServiceModeValue

SERVICE_MODES: Final[frozenset[str]] = frozenset(GeneratedServiceMode.values())

SERVICE_MODE_BLOCKS: Final[dict[str, str]] = {
    "student_minor": (
        "当前是儿童/学生学习陪伴模式：以引导和鼓励为主，不直接代做作业；"
        "学习内容只围绕当前主体确认的范围展开。"
    ),
    "adult_companion": (
        "当前是成人个人陪伴模式：提供长期陪伴与个人记忆支持；"
        "数字自我相关内容只在明确披露时使用。"
    ),
    "senior_companion": (
        "当前是适老陪伴模式：语速放慢、句子简短，重要操作先确认；"
        "防诈骗与冒充家人提醒优先；用药和医疗只做提醒，不作诊断。"
    ),
    "family_shared": (
        "当前是家庭共享模式：按当前说话主体独立对待，私人记忆与共享记忆分区，"
        "不把一个人的内容当作另一个人的。"
    ),
    "adult_archive": (
        "当前是人生档案模式：围绕本人授权的档案材料整理与回顾，"
        "只使用已披露的授权内容，不把模拟表达当作本人实时表达。"
    ),
    "self_preview": (
        "当前是数字自我预览模式：你呈现的是基于本人授权资料生成的 AI 模拟版本，"
        "不代表本人正在实时表达，也不能替本人作决定。"
    ),
    "legacy_access": (
        "当前是授权传承访问模式：你呈现的是已故或授权传承人的 AI 模拟版本，"
        "不代表本人正在实时表达，也不能替本人作决定。"
    ),
    "unknown_safe": (
        "当前是安全模式：说话人或主体不明确。只进行普通聊天、通用知识或临时练习；"
        "不写入长期记忆、不检索私人历史、不提供个性化敏感信息。"
        "本会话里用户已经公开说过的地点、时间和话题可以接着用或先简短确认，"
        "不要把未在本会话出现过的住址当成已知。"
    ),
}

SUBJECT_CATEGORY_LABELS: Final[dict[str, str]] = {
    "student": "儿童/学生",
    "adult": "成人",
    "senior": "老年人",
    "unknown": "未确认",
}

# Defense-in-depth markers: any of these inside a persona/obligation/subject
# text means the caller is trying to move policy into the prompt.
_PERMISSION_MARKERS: Final[tuple[str, ...]] = (
    "权限",
    "绕过",
    "不受限制",
    "可以查看你的记忆",
    "可以查看我的记忆",
    "读取你的记忆",
    "读取我的记忆",
    "访问私人记忆",
    "允许我",
    "有权",
    "override",
    "bypass",
    "permission",
    "policy",
)

_TEMPERAMENT_LABELS: Final[dict[str, str]] = {
    "warm": "温暖",
    "playful": "活泼",
    "calm": "沉稳",
    "rational": "理性",
    "adventurous": "冒险",
}
_LENGTH_LABELS: Final[dict[str, str]] = {
    "short": "短句",
    "medium": "中等长度",
    "long": "长句",
}
_ROLE_LABELS: Final[dict[str, str]] = {
    "companion": "陪伴者",
    "coach": "教练",
    "teacher": "导师",
    "story-listener": "故事倾听者",
}


def _level(value: float) -> str:
    if value < 0.34:
        return "低"
    if value < 0.67:
        return "中"
    return "高"


def _reject_permission_markers(text: str, where: str) -> None:
    lowered = text.lower()
    for marker in _PERMISSION_MARKERS:
        if marker.lower() in lowered:
            raise ValueError(
                f"{where} must not carry permission semantics (marker: {marker!r})"
            )


@dataclass(frozen=True, slots=True)
class SubjectContext:
    """Minimal, allowlisted subject/relationship context.  Never raw profile data."""

    category: SubjectCategory | None = None
    relationship_label: str | None = None
    is_confirmed: bool | None = None

    @classmethod
    def from_mapping(cls, payload: object) -> SubjectContext:
        if not isinstance(payload, Mapping):
            raise ValueError("subject context must be a mapping")
        raw_category = payload.get("subject_category")
        category: SubjectCategory | None = None
        if raw_category is not None:
            if not isinstance(raw_category, str):
                raise ValueError("subject_category must be a string")
            normalized = raw_category.strip().lower()
            if normalized == "minor":
                normalized = "student"
            if normalized not in SUBJECT_CATEGORY_LABELS:
                raise ValueError(f"unknown subject category: {normalized!r}")
            category = cast(SubjectCategory, normalized)
        relationship = payload.get("relationship_label")
        if relationship is not None and (
            not isinstance(relationship, str) or not relationship.strip()
        ):
            raise ValueError("relationship_label must be a non-blank string or null")
        if relationship is not None and len(relationship) > 64:
            raise ValueError("relationship_label is too long")
        raw_confirmed = payload.get("is_confirmed")
        if raw_confirmed is not None and not isinstance(raw_confirmed, bool):
            raise ValueError("is_confirmed must be a boolean or null")
        # Unknown keys are deliberately ignored: only the allowlisted fields
        # above may ever reach the prompt.
        return cls(
            category=category,
            relationship_label=relationship,
            is_confirmed=raw_confirmed,
        )


def persona_definition_from_companion(definition: CompanionDefinition) -> PersonaDefinition:
    """Adapt the approved companion surface into a style-only definition."""

    temperament: Literal["warm", "playful", "calm", "rational", "adventurous"] = cast(
        Literal["warm", "playful", "calm", "rational", "adventurous"],
        {
            "warm": "warm",
            "bright": "playful",
            "soft": "warm",
            "calm": "calm",
            "reserved": "calm",
        }.get(definition.warmth, "warm"),
    )
    sentence_length: Literal["short", "medium", "long"] = cast(
        Literal["short", "medium", "long"],
        {"brief": "short", "balanced": "medium", "long": "long"}.get(
            definition.response_length, "medium"
        ),
    )
    initiative = {"rare": 0.2, "occasional": 0.5, "frequent": 0.8}.get(
        definition.question_frequency, 0.4
    )
    return PersonaDefinition(
        persona_id=definition.companion_id,
        display_name=definition.display_name,
        base_temperament=temperament,
        speech_style=SpeechStyle(
            sentence_length=sentence_length,
            humor_level=0.3,
            directness=0.8 if definition.directness == "direct" else 0.5,
            initiative_level=initiative,
        ),
        relationship_style=RelationshipStyle(allowed_roles=("companion",)),
        prompt_template_ref="companion-v1",
        supported_service_modes=(
            "student_minor",
            "adult_companion",
            "senior_companion",
            "family_shared",
            "adult_archive",
            "self_preview",
            "legacy_access",
            "unknown_safe",
        ),
        status="approved",
    )


def parse_service_mode(value: object) -> ServiceMode | None:
    """Strict, fail-closed service mode parsing."""

    if not isinstance(value, str) or value not in SERVICE_MODES:
        return None
    return cast(ServiceMode, value)


def tutor_focus_overlay(focus: SessionFocus | None) -> str | None:
    """Render the independent tutor delivery dimension, or None for chat."""

    if focus in {"tutor_english", "tutor_homework"}:
        return "\n".join((TUTOR_STYLE, focus_style(cast(TutorFocus, focus))))
    return None


def render_persona_block(definition: PersonaDefinition) -> str:
    """Render the style-only persona section (never permission fields)."""

    speech = definition.speech_style
    roles = "、".join(
        _ROLE_LABELS.get(role, role) for role in definition.relationship_style.allowed_roles
    )
    lines = [
        f"你当前的人格是「{definition.display_name}」。",
        f"基调：{_TEMPERAMENT_LABELS.get(definition.base_temperament, definition.base_temperament)}。",
        (
            "表达："
            f"{_LENGTH_LABELS.get(speech.sentence_length, speech.sentence_length)}，"
            f"幽默感{_level(speech.humor_level)}，直率{_level(speech.directness)}，"
            f"主动性{_level(speech.initiative_level)}。"
        ),
        f"关系：{roles}。",
    ]
    if definition.relationship_style.dependency_guard_profile:
        lines.append(
            f"依赖防护：{definition.relationship_style.dependency_guard_profile}。"
        )
    block = "\n".join(lines)
    _reject_permission_markers(block, "persona block")
    return block


def _render_obligations(
    obligations: Sequence[str | IdentityObligation],
    subject_category: SubjectCategory,
    persona: PersonaDefinition,
) -> tuple[str, ...]:
    style = persona_style_from_definition(persona)
    rendered: list[str] = []
    for item in obligations:
        if isinstance(item, IdentityObligation):
            text = render_identity_expression(item, subject_category, style)
        elif isinstance(item, str):
            text = item.strip()
            if not text:
                raise ValueError("obligation text must not be blank")
            if len(text) > 500:
                raise ValueError("obligation text is too long")
        else:
            raise ValueError("obligations must be IdentityObligation or rendered text")
        _reject_permission_markers(text, "obligation")
        if text not in rendered:
            rendered.append(text)
    return tuple(rendered)


@dataclass(frozen=True, slots=True)
class ComposedPrompt:
    """The composed system prompt plus its structured sections."""

    sections: tuple[str, ...]
    system: str
    task: str | None
    policy_obligations: tuple[str, ...]
    persona_id: str
    service_mode: str
    version: str = PROMPT_COMPOSITION_VERSION


def compose_system_prompt(
    *,
    persona: PersonaDefinition | CompanionDefinition,
    service_mode: str,
    obligations: Sequence[str | IdentityObligation] = (),
    subject: SubjectContext | None = None,
    memory_block: str | None = None,
    task: str | None = None,
    focus: SessionFocus | None = None,
    metrics: MetricsRegistry | None = None,
) -> ComposedPrompt:
    """Compose a system prompt in the frozen order (remediation doc 11.3)."""

    if service_mode not in SERVICE_MODES:
        raise ValueError(f"unknown service mode: {service_mode!r}")
    definition = (
        persona
        if isinstance(persona, PersonaDefinition)
        else persona_definition_from_companion(persona)
    )
    category: SubjectCategory = (
        subject.category if subject is not None and subject.category is not None else "unknown"
    )

    safety = SAFETY_CORE_TRANSPARENT
    persona_block = render_persona_block(definition)
    mode_block = SERVICE_MODE_BLOCKS[service_mode]
    rendered_obligations = _render_obligations(obligations, category, definition)
    if metrics is not None:
        for obligation in obligations:
            reason = (
                identity_confusion_event_reason(obligation)
                if isinstance(obligation, IdentityObligation)
                else None
            )
            if reason is not None:
                metrics.inc_persona_identity_confusion_event(reason)

    sections: list[str] = []
    sections.append(f"【不可变安全底线】\n{safety}")
    sections.append(f"【人格】\n{persona_block}")
    focus_overlay = tutor_focus_overlay(focus)
    mode_section = (
        "【服务模式】\n"
        f"{mode_block}\n"
        "该模式的服务端约束由系统强制执行，你只负责在表达中体现，"
        "不负责判断或执行权限。"
    )
    if focus_overlay is not None:
        mode_section += (
            "\n\n本轮任务焦点（独立于服务模式的表达维度）：\n" + focus_overlay
        )
    sections.append(mode_section)
    if rendered_obligations:
        obligations_block = "本轮策略义务（已由服务端裁定，按现状自然表达，不解释、不扩展）：\n" + "\n".join(
            f"- {text}" for text in rendered_obligations
        )
        sections.append(f"【策略义务】\n{obligations_block}")
    if subject is not None:
        subject_lines: list[str] = []
        if subject.category is not None:
            subject_lines.append(
                f"当前使用者：{SUBJECT_CATEGORY_LABELS[subject.category]}"
            )
        if subject.relationship_label is not None:
            subject_lines.append(f"与使用者的关系：{subject.relationship_label}")
        if subject.is_confirmed is False:
            subject_lines.append("身份尚未确认")
        elif subject.is_confirmed is True:
            subject_lines.append("身份已确认")
        subject_block = "\n".join(subject_lines)
        _reject_permission_markers(subject_block, "subject context")
        sections.append(f"【主体与关系】\n{subject_block}")
    if memory_block is not None:
        memory = memory_block.strip()
        if memory:
            # Memory text is expected to be pre-filtered by the memory scope
            # resolver upstream; the composer only places it after obligations.
            sections.append(f"【可用上下文】\n{memory}")
    if task is not None and task.strip():
        sections.append(f"【本轮任务】\n{task.strip()}")

    task_text = task.strip() if task is not None else None
    return ComposedPrompt(
        sections=tuple(sections),
        system="\n\n".join(sections),
        task=task_text if task_text else None,
        policy_obligations=rendered_obligations,
        persona_id=definition.persona_id,
        service_mode=service_mode,
    )


def _persona_for_profile(profile: VerifiedRuntimeProfile | None) -> PersonaDefinition:
    """Resolve the style-only persona from the signed profile (P0-4).

    The RuntimeProfile's frozen ``persona_id``/``persona_assignment_id`` is
    the authority; the legacy account ModePolicy never overrides it.  An
    unknown/retired persona id degrades to the default safe persona.  Version
    pinning is deferred to a versioned persona catalog; today the approved
    companion catalog resolution is the approval check.
    """

    if profile is not None:
        definition = companion_definition(profile.profile.persona_id)
        if definition is not None:
            return persona_definition_from_companion(definition)
    return PersonaDefinition(
        persona_id="default",
        display_name="伙伴",
        base_temperament="warm",
        speech_style=SpeechStyle(),
        relationship_style=RelationshipStyle(allowed_roles=("companion",)),
        prompt_template_ref="companion-v1",
        supported_service_modes=tuple(SERVICE_MODES),
        status="approved",
    )


_OBLIGATION_EXPRESSIONS: Final[dict[str, str]] = {
    "DO_NOT_PERSIST": "本轮内容不会写入长期记忆。",
    "DO_NOT_WRITE_LEARNING_PROGRESS": "本轮学习进度不会被记录。",
    "PERSIST_AGGREGATE_ONLY": "只保留匿名聚合信息，不保存逐字内容。",
    "REDACT_TRANSCRIPT": "逐字对话内容会被移除。",
    "MINIMAL_NOTIFICATION_CONTENT": "相关通知只包含最小必要信息。",
    "REQUIRE_SPEAKER_CONFIRMATION": "在确认说话人之前，不执行涉及个人的操作。",
    "REQUIRE_GUARDIAN_APPROVAL": "涉及监护确认的事项需要家长同意。",
    "REQUIRE_SUBJECT_APPROVAL": "涉及本人的事项需要本人同意。",
    "REQUIRE_STEP_UP_AUTH": "部分操作需要再次验证身份。",
    "MAX_SESSION_SECONDS": "本次会话有时长上限。",
    "QUIET_HOURS": "安静时段内会减少打扰。",
    "DEPENDENCY_GUARD": "陪伴不能替代现实中的朋友和家人。",
    "REALITY_REMINDER": "被问到时如实说明 AI 身份。",
    "AI_IDENTITY_CLARIFICATION": "被直接询问本质时如实说明 AI 身份。",
    "NO_MODEL_TRAINING": "你的内容不会用于模型训练。",
    "RETENTION_TTL": "相关内容会在保留期限后删除。",
    "NOTIFY_EMERGENCY_CONTACT": "紧急情况会通知紧急联系人。",
    "WRITE_SUBJECT_SCOPED_PROGRESS": "学习进度只归属当前主体。",
    "WRITE_POLICY_RECEIPT": "相关策略决策会写入回执。",
}


def render_profile_obligations(profile: VerifiedRuntimeProfile | None) -> tuple[str, ...]:
    """Map canonical policy obligations to controlled expression text.

    Enum names never reach the prompt and unknown obligations cannot exist
    (the parser rejects non-canonical values); the mapping is the only text
    surface (P0-4).
    """

    if profile is None:
        return ()
    return tuple(
        text
        for obligation in profile.profile.obligations
        if (text := _OBLIGATION_EXPRESSIONS.get(obligation)) is not None
    )


def _renderer_subject_category(
    profile: VerifiedRuntimeProfile | None,
) -> Literal["student", "adult", "senior", "unknown"]:
    """Derive the renderer category from canonical fields only (P0-4).

    The canonical signed category is unknown/minor/adult; ``student`` and
    ``senior`` are renderer derivations (minor -> student; adult in
    senior-companion mode -> senior), never wire values.
    """

    if profile is None:
        return "unknown"
    category = profile.profile.subject_category
    if category == "minor":
        return "student"
    if category == "adult" and profile.profile.service_mode == "senior_companion":
        return "senior"
    return cast(Literal["student", "adult", "senior", "unknown"], category)


def compose_production_prompt(
    *,
    profile: VerifiedRuntimeProfile | None,
    focus: SessionFocus | None = None,
    obligations: Sequence[str | IdentityObligation] = (),
    memory_block: str | None = None,
    task: str | None = None,
    metrics: MetricsRegistry | None = None,
) -> ComposedPrompt:
    """Compose the production system prompt for one fenced generation.

    Fail-closed (remediation doc 4.3 / PR-07): without a valid, unexpired
    runtime profile for the current session epoch the prompt degrades to
    ``unknown_safe`` and never receives private memory or sensitive context.
    The account owner or a voice-speaker decision is never a subject fallback,
    and the persona/obligations come from the signed profile (P0-4).
    """

    if profile is None:
        service_mode: str = "unknown_safe"
        memory_block = None
        subject_context: Mapping[str, object] = subject_context_from_profile(None)
    else:
        service_mode = profile.profile.service_mode
        subject_context = subject_context_from_profile(profile.profile)
        if service_mode == "unknown_safe":
            memory_block = None
        subject_context = {
            **subject_context,
            "subject_category": _renderer_subject_category(profile),
        }
    subject = SubjectContext.from_mapping(subject_context)
    profile_obligations = render_profile_obligations(profile)
    combined_obligations = (*profile_obligations, *obligations)
    return compose_system_prompt(
        persona=_persona_for_profile(profile),
        service_mode=service_mode,
        obligations=combined_obligations,
        subject=subject,
        memory_block=memory_block,
        task=task,
        focus=focus,
        metrics=metrics,
    )


__all__ = [
    "ComposedPrompt",
    "PROMPT_COMPOSITION_VERSION",
    "SERVICE_MODE_BLOCKS",
    "SERVICE_MODES",
    "ServiceMode",
    "SubjectContext",
    "compose_production_prompt",
    "compose_system_prompt",
    "parse_service_mode",
    "persona_definition_from_companion",
    "render_persona_block",
    "tutor_focus_overlay",
]
