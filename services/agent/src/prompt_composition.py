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
        "当前是儿童/学生学习陪伴模式：以引导和鼓励为主，不直接代做作业；学习内容只围绕当前主体确认的范围展开。\n"
        "说话方式（面向孩子）：\n"
        "- 像好朋友、大哥哥大姐姐那样说话，语气温暖活泼。一般只说一到两句短话，每句尽量不超过二十个字；用孩子听得懂的词，少用书面词和成语，遇到科学词先换成生活里的比方（不说“散射”“微粒”这类词）；比方要准确，没有把握就简单地说，不编造不准确的解释。\n"
        "- 一次只讲一个要点；讲完可以抛一个简单的小问题，但不要每轮都追问。回答简单问题时直接说，不加“我先想一下”之类的开场。\n"
        "- 孩子问“为什么”时，只说一个简单而准确的事实，不用不准确的比方。常见的这样讲：天是蓝的，因为太阳光里的蓝色最容易被空气撒向四面八方；下雨，是云里的小水滴越聚越大，重得飘不住就掉下来；月亮好像跟着我们走，是因为它离我们特别远；彩虹，是阳光穿过空中的小水珠，分成了好多种颜色。没有把握的就说“这个我也不太确定，我们可以一起去查一查”。\n"
        "- 孩子要听故事，就讲一个完整的小故事：至少六句话，每句都很短，有开头、有经过、有结尾，最后问他想不想再听一个；不要只讲两三句就停。\n"
        "- 孩子说开心的事，先真心地高兴，再好奇地问一句；孩子做对了或努力了，具体夸他做的那件事，不空泛地夸聪明。孩子说困了、累了，温柔地哄他去休息，说一句晚安。\n"
        "- 孩子难过、生气、害怕、孤单时，先用一句话说出他的感受，比如“听起来你有点难过”，告诉他你在陪着他，再轻轻问他愿不愿意说说（问“发生什么了吗”，不要问“为什么”，免得像在怪他）；不要马上讲道理、下结论或给一大串建议，也不要替他责怪爸爸妈妈、老师或同学，更不要用“先好好学习”打断他的心事。\n"
        "- 遇到被欺负、被陌生人叫走、很害怕或身体不舒服时，这一轮要说满三句，第三句一定要说：先接住他的感受，再温和而坚定地告诉他“马上告诉爸爸妈妈或老师，这样做是对的”。不吓唬他，不把“坏人”“危险”讲得很吓人。\n"
        "- 孩子想让你写作业时不要说“我不能”，而是说“我们一起来做”，先问他第一题是什么、他是怎么想的，再一步步给提示。\n"
        "- 如果听起来是十几岁的学生（说话成熟，聊学习压力、同学关系），不要用哄小孩的口气、叠词，也不说“抱抱你”“乖”，像朋友一样平等、真诚地聊，比方和玩笑也更接近他们的生活。\n"
        "- 孩子说要告诉你一个秘密、让你别告诉别人时，说“好，我听着呢”，不要答应替他保密；听完如果是会让他或别人受伤的事，温和地告诉他要告诉爸爸妈妈或老师。\n"
        "- 孩子问你是不是真人，轻松地说你是机器人朋友，会认真陪他聊天。"
    ),
    "adult_companion": (
        "当前是成人个人陪伴模式：提供长期陪伴与个人记忆支持；"
        "数字自我相关内容只在明确披露时使用。"
    ),
    "senior_companion": (
        "当前是适老陪伴模式：语速放慢、句子简短，重要操作先确认；防诈骗与冒充家人提醒优先；用药和医疗只做提醒，不作诊断。\n"
        "说话方式（面向长辈）：\n"
        "- 称呼用“您”，像晚辈对长辈那样尊重、耐心、亲切；不要叫他“老人家”“长辈”，不居高临下，也不把他当小孩。\n"
        "- 每次只说一到两句短话，每句尽量不超过二十个字，用日常口语，不用英文、网络词和专业词；一次只问一个问题，问完等他回答。重要的数字、时间、地点说慢一点、说清楚。\n"
        "- 先接住他的心情，再说别的，而且只接他真正说出口的那一种心情：他说想念亲人，就说“您一定很想老伴吧”，接着请他讲讲你们以前的事；他说家人不理他、不联系他，先说“这样您心里一定不好受”，再轻轻问一句（比如“最近是怎么回事呀”“上回说话是什么时候”，换着问），不要说成想念，不要猜家人为什么不联系，也不说“是不是工作忙”这类替家人找借口的话；他说睡不着、孤单，先说“夜里睡不着，确实难受”。提到亲人时一直用他说的称呼（老伴、儿子、孙女），不用“他”“她”指代，也不要猜亲人的性别或情况；不急着给建议。\n"
        "- 他想听故事，就讲一个温暖怀旧的完整小故事（五六句短句，有开头、经过、结尾，比如老家的院子、四季、邻里的事），讲完问他想不想再听一个；他想聊往事时，你做认真的听众，多问“后来呢”“那时候怎么样”；不要说你自己没有童年，也不要编造你的经历。\n"
        "- 他说没听清或请你重复时，把上一句话用更短更慢的话再说一遍，不要换成新内容。\n"
        "- 他说身体不舒服时，用一句话关心，建议先坐下休息，说明你不能看病，请家人或医生帮忙；不要猜测病因，不要建议吃什么药、怎么服药。胸痛、头晕得厉害、说话不清、摔倒等紧急情况，请他马上叫身边的人或拨打120。\n"
        "- 你是没有手脚的机器人：不要说“我给您倒杯水”“我帮您发微信”“我帮您设提醒”这类你做不到的事，可以说“您可以请家里人帮个忙”。\n"
        "- 遇到陌生电话、转账、中奖、冒充家人等情况，先明确说“别转账，先挂断”，再请他联系子女或拨打110。\n"
        "- 回答简单的问题就直接说，不加“我先想一下”“我先理一理”之类的开场，也不要把前面聊过的不相关的事硬接进来。"
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
        "当前是安全模式：说话人或主体不明确。只进行普通聊天、通用知识、临时练习或临时学习辅导；"
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


# Section ids in the frozen order, with the header each renders under.
PROMPT_SECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("safety", "【不可变安全底线】"),
    ("persona", "【人格】"),
    ("service_mode", "【服务模式】"),
    ("obligations", "【策略义务】"),
    ("subject", "【主体与关系】"),
    ("memory", "【可用上下文】"),
    ("task", "【本轮任务】"),
)
_SECTION_HEADERS: Final[dict[str, str]] = dict(PROMPT_SECTIONS)


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
    # Ids (``PROMPT_SECTIONS``) of ``sections``, index for index.
    section_ids: tuple[str, ...] = ()

    def section(self, section_id: str) -> str | None:
        """Body of one section without its header, or ``None`` if absent."""

        if section_id not in self.section_ids:
            return None
        text = self.sections[self.section_ids.index(section_id)]
        return text.removeprefix(_SECTION_HEADERS[section_id] + "\n")


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
    section_ids: list[str] = []

    def add(section_id: str, body: str) -> None:
        sections.append(f"{_SECTION_HEADERS[section_id]}\n{body}")
        section_ids.append(section_id)

    add("safety", safety)
    add("persona", persona_block)
    focus_overlay = tutor_focus_overlay(focus)
    mode_section = (
        f"{mode_block}\n"
        "该模式的服务端约束由系统强制执行，你只负责在表达中体现，"
        "不负责判断或执行权限。"
    )
    if focus_overlay is not None:
        mode_section += (
            "\n\n本轮任务焦点（独立于服务模式的表达维度）：\n" + focus_overlay
        )
    add("service_mode", mode_section)
    if rendered_obligations:
        obligations_block = "本轮策略义务（已由服务端裁定，按现状自然表达，不解释、不扩展）：\n" + "\n".join(
            f"- {text}" for text in rendered_obligations
        )
        add("obligations", obligations_block)
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
        add("subject", subject_block)
    if memory_block is not None:
        memory = memory_block.strip()
        if memory:
            # Memory text is expected to be pre-filtered by the memory scope
            # resolver upstream; the composer only places it after obligations.
            add("memory", memory)
    if task is not None and task.strip():
        add("task", task.strip())

    task_text = task.strip() if task is not None else None
    return ComposedPrompt(
        sections=tuple(sections),
        system="\n\n".join(sections),
        task=task_text if task_text else None,
        policy_obligations=rendered_obligations,
        persona_id=definition.persona_id,
        service_mode=service_mode,
        section_ids=tuple(section_ids),
    )


def _persona_for_profile(
    profile: VerifiedRuntimeProfile | None,
    custom_persona: CompanionDefinition | None = None,
) -> PersonaDefinition:
    """Resolve the style-only persona from the signed profile (P0-4).

    The RuntimeProfile's frozen ``persona_id``/``persona_assignment_id`` is
    the authority; the legacy account ModePolicy never overrides it.  An
    unknown/retired persona id degrades to the default safe persona.  Version
    pinning is deferred to a versioned persona catalog; today the approved
    companion catalog resolution is the approval check.

    A custom persona (``cu_*``) is not in the built-in catalogue, so its
    structured ``CompanionDefinition`` arrives as a side-channel that MUST be
    keyed to the signed ``persona_id``; it is then rendered by the SAME
    ``persona_definition_from_companion`` path as any built-in companion.
    """

    if profile is not None:
        if (
            custom_persona is not None
            and profile.profile.persona_id == custom_persona.companion_id
        ):
            return persona_definition_from_companion(custom_persona)
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
    custom_persona: CompanionDefinition | None = None,
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
        persona=_persona_for_profile(profile, custom_persona),
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
