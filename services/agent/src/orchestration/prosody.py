"""Lightweight prosody / speaking style adaptation (ch.19 stage 4)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

CosyVoiceEmotion = Literal[
    "neutral",
    "happy",
    "sad",
    "surprised",
    "angry",
    "fearful",
    "disgusted",
]
DeliveryMode = Literal["direct", "deliberative", "light_laughter", "supportive"]

_LAUGHTER_MARKERS = ("哈哈", "呵呵", "嘿嘿")
_MARKUP_TAG = re.compile(
    r"\[(?:laughter|breath|cough|sigh)\]|"
    r"</?(?:laughter|strong)>",
    re.IGNORECASE,
)
_LEADING_LAUGH_TEXT = re.compile(r"^(?:哈{2,}|呵{2,}|嘿{2,}|[（(]笑[)）])[，,、\s]*")
_SOFT_LAUGH_PREFIXES = ("呵，", "呵,", "呵呵，", "呵呵,", "[laughter]")
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
    "自杀",
    "轻生",
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


@dataclass(frozen=True)
class SpeechPlan:
    voice_emotion: CosyVoiceEmotion
    rate: float
    delivery_mode: DeliveryMode = "direct"
    llm_instruction: str = ""
    # Injected once at the head of the first TTS segment (not into chat history).
    tts_prefix: str = ""
    strip_paralinguistic: bool = False

    @property
    def instruction(self) -> str:
        return cosyvoice_instruction(self.voice_emotion)


def cosyvoice_instruction(emotion: str) -> str:
    # longanyang is a system voice: Alibaba requires this exact Instruct format.
    return f"你正在进行闲聊互动，你说话的情感是{emotion}。"


def soft_laugh_prefix(*, use_markup_tags: bool) -> str:
    """Prefer CosyVoice markup when enabled; otherwise a natural short laugh."""
    return "[laughter]" if use_markup_tags else "呵，"


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

    Rate is fixed at 1.0: CosyVoice rate + emotion swings were perceived as
    random loudness / "awkward vs standard Mandarin" quality jumps.
    Non-happy emotions use neutral instruct — sad/surprised instruct often
    muddies longanyang and lowers volume inconsistently.
    """
    if label == "happy":
        return SpeechPlan("happy", 1.0)
    # sad / surprised / angry / … → neutral voice + fixed rate
    return SpeechPlan("neutral", 1.0)


def speech_plan_for_turn(
    *,
    label: str,
    provider_label: str,
    text: str,
    evidence: tuple[str, ...] = (),
    use_markup_tags: bool = False,
) -> SpeechPlan:
    """Choose one conservative delivery style for the current spoken turn."""
    base = speech_plan_for_emotion(label)
    has_laughter = any(marker in text for marker in _LAUGHTER_MARKERS)
    serious = label in {"sad", "angry", "fearful", "disgusted"} or any(
        marker in text for marker in _SERIOUS_CONTEXT_MARKERS
    )
    if serious:
        return SpeechPlan(
            "neutral",
            1.0,
            "supportive",
            "本轮语境严肃。直接、温和地承接用户，绝对不要笑、咳嗽或使用轻佻的思考填充词。",
            strip_paralinguistic=True,
        )
    acoustic_laughter = "acoustic:qwen3-asr:laughter" in evidence
    if has_laughter and (provider_label == "happy" or acoustic_laughter):
        return SpeechPlan(
            "happy",
            1.0,
            "light_laughter",
            "本轮是轻松且声学上明确的笑声。"
            "开头只轻笑一次：优先输出可被 TTS 自然读出的短笑音“呵，”或“呵呵，”，"
            "紧接正文；禁止连续多个“哈”，不要写旁白式“（笑）”，不要反复笑。",
            tts_prefix=soft_laugh_prefix(use_markup_tags=use_markup_tags),
        )
    if any(marker in text for marker in _DELIBERATIVE_MARKERS):
        deliberative_prefix = "[breath]" if use_markup_tags else ""
        return SpeechPlan(
            base.voice_emotion,
            1.0,
            "deliberative",
            "本轮需要安排或组织内容。"
            "先用四到十二个字的自然短衔接（可含一个“嗯”“好”或“可以”），"
            "该小段以逗号结束，再继续同一段正文；整轮只生成一次，"
            "不要拆成两段回答，不要披露推理或照抄固定模板。",
            tts_prefix=deliberative_prefix,
        )
    return base


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
