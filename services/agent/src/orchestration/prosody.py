"""Lightweight prosody / speaking style adaptation (ch.19 stage 4)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal

from services.common.companion_response_safety import companion_safety_decision
from services.common.companions import companion_definition

VoiceEmotion = Literal[
    "neutral",
    "happy",
    "sad",
    "surprised",
    "angry",
    "fearful",
    "disgusted",
]
DeliveryMode = Literal["direct", "deliberative", "light_laughter", "supportive"]
VoiceDialect = Literal["standard", "sichuan", "beijing"]
VoiceTone = Literal["natural", "coquettish", "intimate", "argumentative", "sweet"]
MascotExpression = Literal["neutral", "happy", "sad", "surprised", "curious", "caring"]

_LAUGHTER_MARKERS = ("哈哈", "呵呵", "嘿嘿")
_MARKUP_TAG = re.compile(
    r"\[(?:laughter|breath|cough|sigh|inhale)\]|"
    r"</?(?:laughter|strong|emphasis)>",
    re.IGNORECASE,
)
_HAS_DIGIT = re.compile(r"\d")
_LEADING_LAUGH_TEXT = re.compile(r"^(?:哈{2,}|呵{2,}|嘿{2,}|[（(]笑[)）])[，,、\s]*")
_SOFT_LAUGH_PREFIXES = ("呵，", "呵,", "呵呵，", "呵呵,", "[laughter]")
_SUPPORTIVE_LLM_INSTRUCTION = (
    "本轮语境严肃。像熟人一样直接、温和地承接，语气关切但不要客服腔。"
    "绝对不要笑、咳嗽、吸气戏或使用轻佻的思考填充词。"
    "禁止输出 [laughter]/[breath]/[inhale]/<strong> 等副语言/强调标签。"
)
_SERIOUS_CONTEXT_MARKERS = (
    "车祸",
    "事故",
    "受伤",
    "流血",
    "医院",
    "急救",
    "报警",
    "危险",
    "救命",
    "去世",
    "死亡",
    "葬礼",
    "被骗",
    "诈骗",
    "钱骗",
    "难过",
    "伤心",
    "生气",
    "害怕",
    "恐惧",
    "疼",
    "不舒服",
    "抑郁",
    "家暴",
    "骚扰",
)
_DELIBERATIVE_MARKERS = (
    "安排",
    "规划",
    "计划",
    "制定",
    "设计一套",
    "训练方案",
    "训练计划",
    "学习方案",
    "行程",
    "步骤",
)
_ASSISTANT_HAPPY_MARKERS = (
    "太好了",
    "真好",
    "恭喜",
    "好消息",
    "值得庆祝",
    "开心",
    "高兴",
)
_ASSISTANT_SAD_MARKERS = (
    "很难过",
    "伤心",
    "抱歉",
    "对不起",
    "遗憾",
    "心疼",
)
_ASSISTANT_SURPRISED_MARKERS = (
    "没想到",
    "竟然",
    "真的吗",
    "哇",
    "天哪",
    "这么巧",
)
_ASSISTANT_CARING_MARKERS = (
    "听起来",
    "不容易",
    "辛苦",
    "难过",
    "心疼",
    "理解你",
    "陪着你",
    "担心",
    "先照顾好自己",
    "注意安全",
)
_NATURAL_DIRECT_INSTRUCTION = (
    "用自然口语回答，像熟人面对面聊天；句子长短有变化，允许自然停顿，"
    "不要使用播报腔、客服套话或公文式编号。"
)


@dataclass(frozen=True)
class SpeechPlan:
    voice_emotion: VoiceEmotion
    rate: float
    delivery_mode: DeliveryMode = "direct"
    llm_instruction: str = ""
    # Injected once at the head of the first TTS segment (not into chat history).
    tts_prefix: str = ""
    strip_paralinguistic: bool = False
    dialect: VoiceDialect = "standard"
    tone: VoiceTone = "natural"
    pitch: int = 0
    # Trusted provider instruction assembled only from the whitelist above.
    tts_instruction: str = ""

    @property
    def instruction(self) -> str:
        # Fixed system-voice format (longanyang). TTS may re-map for freeform.
        return cosyvoice_instruction(self.voice_emotion, freeform=False)


def cosyvoice_instruction(emotion: str, *, freeform: bool = False) -> str:
    """Build CosyVoice `instruction` for the given emotion label.

    - freeform=False: Alibaba fixed template for system voices like longanyang.
    - freeform=True: natural-language control for v3.5 designed / cloned voices
      (keep short: CosyVoice instruction length limit is tight).
    """
    if not freeform:
        return f"你正在进行闲聊互动，你说话的情感是{emotion}。"

    # Freeform: concrete, multi-dim, ≤ ~40 Chinese chars (count as 2 each).
    freeform_by_emotion: dict[str, str] = {
        "neutral": "一对一陪伴闲聊，语气平和自然，语速中等，吐字清晰。",
        "happy": "一对一陪伴闲聊，语气轻松愉快，带一点笑意，吐字清晰。",
        "sad": "一对一陪伴闲聊，语气温柔共情，语速略慢，吐字清晰。",
        "surprised": "一对一陪伴闲聊，语气轻快略惊讶，吐字清晰，不要夸张。",
        "angry": "一对一陪伴闲聊，语气认真坚定，语速中等，吐字清晰。",
        "fearful": "一对一陪伴闲聊，语气温和安抚，语速略慢，吐字清晰。",
        "disgusted": "一对一陪伴闲聊，语气平静克制，语速中等，吐字清晰。",
    }
    return freeform_by_emotion.get(emotion, freeform_by_emotion["neutral"])


def soft_laugh_prefix(*, use_markup_tags: bool) -> str:
    """Prefer CosyVoice markup when enabled; otherwise a natural short laugh."""
    return "[laughter]" if use_markup_tags else "呵，"


def breath_prefix(*, use_markup_tags: bool) -> str:
    """Short inhale before deliberative speech when markup is enabled."""
    return "[breath]" if use_markup_tags else ""


def strip_paralinguistic_markup(text: str) -> str:
    cleaned = _MARKUP_TAG.sub("", text)
    cleaned = _LEADING_LAUGH_TEXT.sub("", cleaned)
    return cleaned


def prepare_tts_text(
    text: str,
    plan: SpeechPlan,
    *,
    is_first_segment: bool,
    use_markup_tags: bool = False,
) -> str:
    """Apply delivery-mode TTS rewrites without changing chat history text."""
    body = text if text else ""
    if plan.strip_paralinguistic or plan.delivery_mode == "supportive":
        body = strip_paralinguistic_markup(body)
    if not is_first_segment or not body.strip():
        return body

    prefix = plan.tts_prefix
    if not prefix and plan.delivery_mode == "light_laughter":
        prefix = soft_laugh_prefix(use_markup_tags=use_markup_tags)
    if not prefix:
        return body

    stripped = body.lstrip()
    if any(stripped.startswith(existing) for existing in _SOFT_LAUGH_PREFIXES):
        return body
    # Keep a single soft onset; LLM may already have written a longer laugh.
    if stripped.startswith(("哈哈", "呵呵", "嘿嘿")):
        return body
    return f"{prefix}{body}"


def speech_plan_for_emotion(label: str) -> SpeechPlan:
    """Map input observation to the deliberately smaller safe output subset.

    Output labels stay conservative; only small rate changes and provider-neutral
    delivery instructions vary within one generation.
    """
    if label == "happy":
        return SpeechPlan(
            "happy",
            1.02,
            llm_instruction=(
                "用轻松、有笑意的自然口语回答，像熟人分享好消息；"
                "不要夸张，不要连续发笑，也不要使用播报腔。"
            ),
        )
    if label == "surprised":
        return SpeechPlan(
            "neutral",
            1.01,
            llm_instruction=(
                "用自然、略带好奇的口语承接，像熟人听到意外消息；不要夸张或使用播报腔。"
            ),
        )
    # sad / angry / fearful / disgusted are handled by the supportive mode below.
    return SpeechPlan("neutral", 1.0, llm_instruction=_NATURAL_DIRECT_INSTRUCTION)


def _semantic_speech_plan_for_turn(
    *,
    label: str,
    provider_label: str,
    text: str,
    evidence: tuple[str, ...] = (),
    use_markup_tags: bool = False,
) -> SpeechPlan:
    """Choose one conservative delivery style for the current spoken turn."""
    delivery_label = label if label != "neutral" else provider_label
    base = speech_plan_for_emotion(delivery_label)
    has_laughter = any(marker in text for marker in _LAUGHTER_MARKERS)
    serious = (
        label in {"sad", "angry", "fearful", "disgusted"}
        or provider_label in {"sad", "angry", "fearful", "disgusted"}
        or companion_safety_decision(text) in {"crisis_self", "support_request"}
        or any(marker in text.lower() for marker in _SERIOUS_CONTEXT_MARKERS)
    )
    if serious:
        return SpeechPlan(
            "neutral",
            0.98,
            "supportive",
            _SUPPORTIVE_LLM_INSTRUCTION,
            strip_paralinguistic=True,
        )
    acoustic_laughter = "acoustic:qwen3-asr:laughter" in evidence
    if has_laughter and (provider_label == "happy" or acoustic_laughter):
        laugh_hint = (
            "开头只轻笑一次：优先输出 TTS 标签 [laughter]，紧接正文；"
            if use_markup_tags
            else "开头只轻笑一次：优先输出可被 TTS 自然读出的短笑音“呵，”或“呵呵，”，"
        )
        return SpeechPlan(
            "happy",
            1.02,
            "light_laughter",
            "本轮是轻松且声学上明确的笑声。"
            f"{laugh_hint}"
            "禁止连续多个“哈”，不要写旁白式“（笑）”，不要反复笑。",
            tts_prefix=soft_laugh_prefix(use_markup_tags=use_markup_tags),
        )
    if any(marker in text for marker in _DELIBERATIVE_MARKERS):
        deliberative_prefix = breath_prefix(use_markup_tags=use_markup_tags)
        emphasis_hint = ""
        if use_markup_tags and _HAS_DIGIT.search(text):
            emphasis_hint = "若出现关键数字或时间点，可用一次 <strong>…</strong> 做轻强调，勿滥用。"
        breath_hint = (
            "衔接前允许一次 [breath] 短吸气（系统也可能已注入），" if use_markup_tags else ""
        )
        return SpeechPlan(
            base.voice_emotion,
            0.99,
            "deliberative",
            "本轮需要安排或组织内容。"
            f"{breath_hint}"
            "先用四到十二个字的自然短衔接（可含一个“嗯”“好”或“可以”），"
            "该小段以逗号结束，再继续同一段正文；整轮只生成一次，"
            "不要拆成两段回答，不要披露推理或照抄固定模板。"
            f"整体像熟人边想边说，不要播报腔。{emphasis_hint}",
            tts_prefix=deliberative_prefix,
        )
    # Non-serious short happy turns may lightly emphasize digits once.
    if (
        use_markup_tags
        and base.voice_emotion == "happy"
        and _HAS_DIGIT.search(text)
        and not has_laughter
    ):
        return SpeechPlan(
            "happy",
            base.rate,
            "direct",
            "语气轻松。若有关键数字可用一次 <strong>…</strong> 强调；"
            "不要笑、不要吸气戏、不要旁白。",
        )
    return base


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _tts_instruction_for_plan(plan: SpeechPlan) -> str:
    emotion = {
        "neutral": "情绪自然平和",
        "happy": "情绪轻松愉快，带一点自然笑意",
        "sad": "整体带悲伤和低落感，但吐字清楚",
        "surprised": "略带自然惊讶，不要夸张",
        "angry": "整体带生气和不满，但不要吼叫或辱骂",
        "fearful": "略带紧张不安，但保持清楚",
        "disgusted": "带克制的反感，不要夸张",
    }[plan.voice_emotion]
    if plan.tone == "argumentative" and plan.voice_emotion == "neutral":
        emotion = "情绪带克制的不满"
    dialect = {
        "standard": "",
        "sichuan": "，使用自然四川话",
        "beijing": "，使用自然北京话",
    }[plan.dialect]
    tone = {
        "natural": "",
        "coquettish": "，语气撒娇但不过分做作",
        "intimate": "，语气暧昧亲近但保持边界",
        "argumentative": "，像有火气地争辩，但不辱骂、不威胁",
        "sweet": "，使用自然夹子音，不幼态化、不夸张",
    }[plan.tone]
    delivery = {
        "direct": "，像熟人面对面自然聊天，不要播报腔或客服腔",
        "deliberative": "，边想边自然说，允许短暂停顿，不要播报腔",
        "light_laughter": "，只轻笑一次，不要连续发笑",
        "supportive": "，温和关切地承接，不要笑，不要客服腔",
    }[plan.delivery_mode]
    # Rate and pitch use provider numeric fields; duplicating them here can conflict.
    return f"{emotion}{dialect}{tone}{delivery}。"[:240]


def _style_command_body(text: str) -> str | None:
    normalized = text.strip(" ，,。！？!?；;")
    for _ in range(3):
        prefix = next(
            (
                candidate
                for candidate in ("请", "麻烦", "你", "给我", "能不能", "可以", "接下来", "以后")
                if normalized.startswith(candidate)
            ),
            None,
        )
        if prefix is None:
            break
        normalized = normalized[len(prefix) :].lstrip(" ，,：:")
    style_prefixes = (
        "用悲伤",
        "用生气",
        "用四川话",
        "用北京话",
        "用撒娇",
        "用暧昧",
        "用吵架",
        "用争吵",
        "用夹子音",
        "用ASMR",
        "悲伤一点说",
        "生气一点说",
        "四川话说",
        "北京话说",
        "撒娇一点说",
        "暧昧一点说",
        "夹子音说",
        "说四川话",
        "说北京话",
        "说快",
        "说慢",
        "语速快",
        "语速慢",
        "快一点说",
        "慢一点说",
        "把语速",
        "把音调",
        "把声音",
        "语速调",
        "音调调",
        "声音调",
        "都用",
    )
    if normalized.startswith(style_prefixes):
        return normalized
    if normalized.startswith(("讲", "读", "回答", "告诉", "解释", "继续", "接着")):
        return next(
            (
                clause.strip()
                for clause in re.split(r"[，,；;]", normalized)[1:]
                if clause.strip().startswith(style_prefixes)
            ),
            None,
        )
    return None


def _apply_explicit_voice_style(plan: SpeechPlan, text: str) -> SpeechPlan:
    command = _style_command_body(text)
    if command is None:
        return replace(plan, tts_instruction=_tts_instruction_for_plan(plan))

    emotion = plan.voice_emotion
    dialect: VoiceDialect = "standard"
    tone: VoiceTone = "natural"
    rate = plan.rate
    pitch = 0
    explicit_emotion = False

    if _contains_any(command, ("用悲伤的语气", "用悲伤语气", "悲伤一点说", "语气悲伤一点")):
        emotion = "sad"
        explicit_emotion = True
    elif _contains_any(command, ("用生气的语气", "用生气语气", "生气一点说", "语气生气一点")):
        emotion = "angry"
        explicit_emotion = True

    if _contains_any(command, ("用四川话", "四川话说", "说四川话")):
        dialect = "sichuan"
    elif _contains_any(command, ("用北京话", "北京话说", "说北京话")):
        dialect = "beijing"

    if _contains_any(command, ("用撒娇的语气", "撒娇一点说", "语气撒娇")):
        tone = "coquettish"
    elif _contains_any(command, ("用暧昧的语气", "暧昧一点说", "用ASMR", "用asmr")):
        tone = "intimate"
    elif _contains_any(command, ("用吵架的语气", "像吵架一样说", "用争吵的语气")):
        tone = "argumentative"
    elif _contains_any(command, ("用夹子音", "夹子音说")):
        tone = "sweet"

    if _contains_any(
        command,
        ("快一点说", "说快点", "说快一点", "语速调快", "把语速调快", "语速快一些", "说得快一点"),
    ):
        rate = 1.10
    elif _contains_any(
        command,
        ("慢一点说", "说慢点", "说慢一点", "语速调慢", "把语速调慢", "语速慢一些", "说得慢一点"),
    ):
        rate = 0.90

    if _contains_any(
        command,
        ("音调调高", "音调提高", "音调高一点", "声音调高", "声音提高", "声音高一点"),
    ):
        pitch = 2
    elif _contains_any(
        command,
        ("音调调低", "音调降低", "音调低一点", "声音调低", "声音降低", "声音低一点"),
    ):
        pitch = -2

    styled = replace(
        plan,
        voice_emotion=emotion,
        delivery_mode="direct" if explicit_emotion else plan.delivery_mode,
        strip_paralinguistic=False if explicit_emotion else plan.strip_paralinguistic,
        dialect=dialect,
        tone=tone,
        rate=1.0 if explicit_emotion and rate == plan.rate else rate,
        pitch=pitch,
    )
    return replace(styled, tts_instruction=_tts_instruction_for_plan(styled))


def speech_plan_for_turn(
    *,
    label: str,
    provider_label: str,
    text: str,
    evidence: tuple[str, ...] = (),
    use_markup_tags: bool = False,
    companion_id: str | None = None,
) -> SpeechPlan:
    """Choose one bounded provider-neutral style, including explicit requests."""
    plan = _semantic_speech_plan_for_turn(
        label=label,
        provider_label=provider_label,
        text=text,
        evidence=evidence,
        use_markup_tags=use_markup_tags,
    )
    styled = _apply_explicit_voice_style(plan, text)
    if companion_safety_decision(text) not in {"crisis_self", "support_request"}:
        companion = companion_definition(companion_id)
        if companion is None or _style_command_body(text) is not None:
            return styled
        if styled.delivery_mode == "direct" and styled.voice_emotion == "neutral":
            styled = replace(
                styled,
                voice_emotion=companion.default_voice_emotion,
                rate=companion.default_voice_rate,
            )
            styled = replace(styled, tts_instruction=_tts_instruction_for_plan(styled))
        return replace(
            styled,
            tts_instruction=(
                styled.tts_instruction.rstrip("。")
                + "；"
                + companion.voice_instruction
            )[:240],
        )
    supportive = replace(
        plan,
        voice_emotion="neutral",
        rate=0.98,
        delivery_mode="supportive",
        llm_instruction=_SUPPORTIVE_LLM_INSTRUCTION,
        strip_paralinguistic=True,
        tts_prefix="",
        dialect="standard",
        tone="natural",
        pitch=0,
    )
    return replace(supportive, tts_instruction=_tts_instruction_for_plan(supportive))


def mascot_expression_for_reply(
    *,
    plan: SpeechPlan,
    text: str,
) -> MascotExpression:
    """Resolve one small, safe face state from the assistant's delivery and reply."""
    if plan.delivery_mode == "supportive":
        return "caring"
    if plan.delivery_mode == "light_laughter":
        return "happy"
    if plan.delivery_mode == "deliberative":
        return "curious"

    spoken = strip_paralinguistic_markup(text)
    if any(marker in spoken for marker in _ASSISTANT_CARING_MARKERS):
        return "caring"
    if any(marker in spoken for marker in _ASSISTANT_SAD_MARKERS):
        return "sad"
    if any(marker in spoken for marker in _ASSISTANT_SURPRISED_MARKERS):
        return "surprised"
    if any(marker in spoken for marker in _ASSISTANT_HAPPY_MARKERS):
        return "happy"
    if "？" in spoken or "?" in spoken:
        return "curious"
    return "neutral"


class SpeakingStyle(StrEnum):
    NEUTRAL = "neutral"
    CALM = "calm"
    HESITANT = "hesitant"
    URGENT = "urgent"
    EXCITED = "excited"


@dataclass
class ProsodyFeatures:
    rms_dbfs: float = -30.0
    speech_rate_cps: float = 4.0  # chars per second
    pause_ratio: float = 0.2
    f0_median_hz: float | None = None
    f0_var: float | None = None
    recent_interrupt_count: int = 0
    explicit_request: str | None = None  # e.g. "慢一点"


@dataclass
class SpeakingStyleState:
    style: SpeakingStyle = SpeakingStyle.NEUTRAL
    rate: float = 1.0
    pitch: float = 1.0
    confidence: float = 1.0


@dataclass
class ProsodyController:
    """CPU-only style smoothing. No medical/sensitive labels stored."""

    state: SpeakingStyleState = field(default_factory=SpeakingStyleState)
    history: list[SpeakingStyle] = field(default_factory=list)
    alpha: float = 0.5  # exponential smooth over ~3 turns

    def update(self, features: ProsodyFeatures) -> SpeakingStyleState:
        # Explicit user request wins.
        if features.explicit_request:
            req = features.explicit_request
            if any(k in req for k in ("慢", "慢一点", "慢点", "慢些")):
                self.state.style = SpeakingStyle.CALM
                self.state.rate = max(0.90, self.state.rate - 0.05)
                self.state.confidence = 1.0
                self._remember(self.state.style)
                return self._clamp()
            if any(k in req for k in ("快", "快点", "快一点")):
                self.state.style = SpeakingStyle.URGENT
                self.state.rate = min(1.10, self.state.rate + 0.05)
                self.state.confidence = 1.0
                self._remember(self.state.style)
                return self._clamp()

        inferred, conf = self._infer(features)
        if conf < 0.7:
            inferred = SpeakingStyle.NEUTRAL
            conf = 0.7
        self._remember(inferred)
        # Three-turn exponential smooth: majority-ish via last styles.
        style = self._smoothed_style(inferred)
        target_rate = {
            SpeakingStyle.CALM: 0.95,
            SpeakingStyle.HESITANT: 0.92,
            SpeakingStyle.URGENT: 1.08,
            SpeakingStyle.EXCITED: 1.06,
            SpeakingStyle.NEUTRAL: 1.0,
        }[style]
        self.state.style = style
        self.state.rate = self.state.rate * (1 - self.alpha) + target_rate * self.alpha
        self.state.pitch = 1.0  # first version keeps pitch fixed
        self.state.confidence = conf
        return self._clamp()

    def instruction_for_cosyvoice(self) -> str:
        emotion = "happy" if self.state.style is SpeakingStyle.EXCITED else "neutral"
        return cosyvoice_instruction(emotion)

    def _infer(self, f: ProsodyFeatures) -> tuple[SpeakingStyle, float]:
        if f.recent_interrupt_count >= 2:
            return SpeakingStyle.CALM, 0.75
        if f.speech_rate_cps >= 6.0 and f.pause_ratio < 0.15:
            return SpeakingStyle.URGENT, 0.72
        if f.speech_rate_cps <= 2.5 or f.pause_ratio > 0.45:
            return SpeakingStyle.HESITANT, 0.72
        if f.rms_dbfs > -18:
            return SpeakingStyle.EXCITED, 0.65  # below 0.7 → neutral
        return SpeakingStyle.NEUTRAL, 0.8

    def _remember(self, style: SpeakingStyle) -> None:
        self.history.append(style)
        if len(self.history) > 3:
            self.history = self.history[-3:]

    def _smoothed_style(self, latest: SpeakingStyle) -> SpeakingStyle:
        if len(self.history) < 2:
            return latest
        # Prefer continuity: if last two agree, keep; else latest.
        if self.history[-1] == self.history[-2]:
            return self.history[-1]
        return latest

    def _clamp(self) -> SpeakingStyleState:
        self.state.rate = min(1.10, max(0.90, self.state.rate))
        self.state.pitch = 1.0
        return self.state
