"""Chinese interruption guard and backchannel rules (ch.16)."""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum


class InterruptDecision(StrEnum):
    DUCK = "duck"
    CONFIRM_INTERRUPT = "confirm_interrupt"
    RESUME = "resume"
    UNCERTAIN = "uncertain"
    FALSE_INTERRUPTION = "false_interruption"


class PlaybackInputDecision(StrEnum):
    WAIT = "wait"
    ACCEPT = "accept"
    IGNORE = "ignore"


BACKCHANNEL_WHITELIST = frozenset(
    {
        "嗯",
        "嗯嗯",
        "对",
        "对的",
        "是",
        "是的",
        "好",
        "好的",
        "行",
        "可以",
        "明白",
        "知道了",
        "哦",
        "啊",
    }
)

# Explicit interrupt phrases (any of these → treat as real owner interrupt).
INTERRUPT_PREFIXES = (
    "等等",
    "等一下",
    "停",
    "停一下",
    "停下",
    "暂停",
    "先停",
    "别说了",
    "不要说了",
    "别讲了",
    "先别说",
    "闭嘴",
    "安静",
    "你先停",
    "听我说",
    "不是",
    "不对",
    "先别",
    "你听我说",
    "我的意思是",
    "我问的是",
    "换一个",
)

# Semantic ack after interrupt (order: longer / stop-intent first).
# stop_talking → AI should just acknowledge and stay quiet ("好的。")
# yield_floor → AI hands the floor to the user ("嗯，你说。")
_STOP_TALKING_PHRASES = (
    "别说了",
    "不要说了",
    "别讲了",
    "先别说",
    "闭嘴",
    "安静",
    "暂停",
    "停下",
    "先停",
    "你先停",
)
_YIELD_FLOOR_PHRASES = (
    "停一下",
    "等一下",
    "等下",
    "等等",
    "你听我说",
    "听我说",
    "我的意思是",
    "我问的是",
    "换一个",
    "不是",
    "不对",
    "先别",
)

_NON_TARGET_SCRIPT = re.compile(r"[\u3040-\u30ff\uac00-\ud7af]")
_CANTONESE_MARKERS = frozenset("佢嘅咁冇喺啲咗嚟噉唔仲俾")
_LANGUAGE_ACTION = r"(?:学|教|练|说|用|翻译|切换|作为|充当|培训|教学|老师)"
_MULTILINGUAL_LANGUAGE = r"(?:韩语|朝鲜语|粤语|日语)"
_ZH_EN_LANGUAGE = r"(?:中文|普通话|英语|英文)"
_MULTILINGUAL_REQUEST = re.compile(
    rf"(?:{_LANGUAGE_ACTION}.{{0,8}}{_MULTILINGUAL_LANGUAGE}|"
    rf"{_MULTILINGUAL_LANGUAGE}.{{0,8}}{_LANGUAGE_ACTION})"
)
_ZH_EN_REQUEST = re.compile(
    rf"(?:{_LANGUAGE_ACTION}.{{0,8}}{_ZH_EN_LANGUAGE}|"
    rf"{_ZH_EN_LANGUAGE}.{{0,8}}{_LANGUAGE_ACTION})"
)
_PUNCTUATION = " \t\r\n。！？.!?，,；;：:\"'“”‘’（）()【】[]"


def normalize_short(text: str) -> str:
    return text.strip().replace(" ", "").replace("　", "").strip("。！？!?，,；;：:")


def is_backchannel(text: str, *, duration_ms: int) -> bool:
    t = normalize_short(text)
    if not t or duration_ms > 900:
        return False
    return t in BACKCHANNEL_WHITELIST


def is_explicit_interrupt(text: str) -> bool:
    t = normalize_short(text)
    if not t:
        return False
    return any(t.startswith(p) or p in t for p in INTERRUPT_PREFIXES)


def interrupt_ack_phrase(text: str) -> str:
    """Pick a natural interrupt ack from user wording.

    - yield floor (停一下 / 等等 / 听我说…):「嗯，你说。」
    - stop talking (别说了 / 暂停 / 停下…):「好的。」
    - unknown / empty: default yield-floor style.
    """
    t = normalize_short(text)
    if not t:
        return "嗯，你说。"
    for p in _STOP_TALKING_PHRASES:
        if t.startswith(p) or p in t:
            return "好的。"
    for p in _YIELD_FLOOR_PHRASES:
        if t.startswith(p) or p in t:
            return "嗯，你说。"
    # Bare「停」without「一下」is ambiguous; prefer quiet ack.
    if t == "停" or t.startswith("停") and "一下" not in t:
        return "好的。"
    return "嗯，你说。"


_INTERRUPT_FILLERS = (
    "嗯",
    "啊",
    "呃",
    "那个",
    "那个啥",
    "就是",
    "我",
    "你",
)

_CONTROL_ACK_FILLERS = _INTERRUPT_FILLERS + (
    "好",
    "好的",
    "可以",
    "行",
    "你说",
    "我在听",
)

_RESUME_COMMANDS = (
    "继续说",
    "继续讲",
    "接着说",
    "接着讲",
    "往下说",
    "你继续",
    "请继续",
    "继续",
)


def is_interrupt_command_only(text: str) -> bool:
    """True when the utterance is only stop/wait commands (no real question).

    e.g. 「等等」「嗯，等等，等等。」「等一下」→ True
         「等一下我想问下周三」→ False (has content beyond the command)
    """
    t = normalize_short(text)
    if t == "等下":
        return True
    if not t or not is_explicit_interrupt(t):
        return False
    remainder = t
    for p in sorted(INTERRUPT_PREFIXES, key=len, reverse=True):
        remainder = remainder.replace(p, "")
    for filler in sorted(_CONTROL_ACK_FILLERS, key=len, reverse=True):
        remainder = remainder.replace(filler, "")
    remainder = normalize_short(remainder)
    # Allow at most one leftover char (noise from ASR)
    return len(remainder) <= 1


def is_resume_command_only(text: str) -> bool:
    """True for a resume command, including a leaked control ack/pause prefix.

    Production ASR can endpoint「好的，好的。等一下。继续。」as one final
    after the fixed yield ack reaches the microphone. Real content after the
    resume command must remain chat instead of being swallowed as control.
    """

    normalized = normalize_short(text)
    if not normalized or not any(command in normalized for command in _RESUME_COMMANDS):
        return False
    remainder = normalized
    for command in sorted(_RESUME_COMMANDS, key=len, reverse=True):
        remainder = remainder.replace(command, "")
    for interrupt in sorted(INTERRUPT_PREFIXES, key=len, reverse=True):
        remainder = remainder.replace(interrupt, "")
    for filler in sorted(_CONTROL_ACK_FILLERS, key=len, reverse=True):
        remainder = remainder.replace(filler, "")
    return len(normalize_short(remainder)) <= 1


def count_cjk_chars(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def _content(text: str) -> str:
    return text.strip(_PUNCTUATION).replace(" ", "").lower()


def _is_short_non_target(text: str) -> bool:
    content = _content(text)
    if not content or len(content) > 4:
        return False
    return bool(_NON_TARGET_SCRIPT.search(content)) or any(
        ch in _CANTONESE_MARKERS for ch in content
    )


def _looks_like_assistant_echo(text: str, assistant_text: str) -> bool:
    content = _content(text)
    spoken = _content(assistant_text)
    if len(content) < 4 or not spoken:
        return False
    if content in spoken:
        return True
    return len(content) >= 12 and SequenceMatcher(None, content, spoken).ratio() >= 0.72


def guarded_input_reason(
    text: str,
    *,
    duration_ms: int,
    assistant_text: str,
    multilingual: bool,
) -> str | None:
    if is_backchannel(text, duration_ms=duration_ms):
        return "backchannel"
    if not multilingual and _is_short_non_target(text):
        return "non_target_language"
    if _looks_like_assistant_echo(text, assistant_text):
        return "assistant_echo"
    return None


@dataclass
class ChineseInterruptionGuard:
    """Three-layer guard for cn_self_hosted / safety supplement."""

    decision_window_ms: int = 250
    false_interruption_timeout_ms: int = 1200

    def on_user_voice_while_speaking(self) -> InterruptDecision:
        return InterruptDecision.DUCK

    def evaluate(
        self,
        *,
        elapsed_ms: int,
        asr_text: str,
        has_speech_energy: bool,
        looks_like_noise_only: bool = False,
    ) -> InterruptDecision:
        text = normalize_short(asr_text)

        # High-confidence keywords: immediate confirm.
        if is_explicit_interrupt(text):
            return InterruptDecision.CONFIRM_INTERRUPT

        if looks_like_noise_only and not text:
            return InterruptDecision.RESUME

        if elapsed_ms < 80:
            return InterruptDecision.DUCK

        if 80 <= elapsed_ms <= 350 or elapsed_ms <= 900:
            if is_backchannel(text, duration_ms=elapsed_ms):
                return InterruptDecision.RESUME
            if count_cjk_chars(text) >= 5:
                return InterruptDecision.CONFIRM_INTERRUPT
            if "？" in asr_text or "?" in asr_text:
                return InterruptDecision.CONFIRM_INTERRUPT
            if not text and has_speech_energy and elapsed_ms < 350:
                return InterruptDecision.DUCK
            if not text and looks_like_noise_only:
                return InterruptDecision.RESUME

        if elapsed_ms >= self.false_interruption_timeout_ms and not text:
            return InterruptDecision.FALSE_INTERRUPTION

        if text and not is_backchannel(text, duration_ms=elapsed_ms):
            # Prefer yielding the floor when uncertain with non-empty speech.
            return InterruptDecision.UNCERTAIN

        return InterruptDecision.UNCERTAIN

    def resolve_uncertain_prefer_yield(self, decision: InterruptDecision) -> InterruptDecision:
        if decision is InterruptDecision.UNCERTAIN:
            return InterruptDecision.CONFIRM_INTERRUPT
        return decision


@dataclass
class PlaybackInputGuard:
    """Validate transcripts captured while assistant audio is playing."""

    enabled: bool = False
    feedback_window_s: float = 15.0
    max_feedback_turns: int = 2
    interruption_guard: ChineseInterruptionGuard = field(default_factory=ChineseInterruptionGuard)
    multilingual: bool = False
    candidate_active: bool = False
    candidate_during_playback: bool = False
    candidate_vad_anchored: bool = True
    candidate_started_ns: int | None = None
    candidate_decision: PlaybackInputDecision = PlaybackInputDecision.ACCEPT
    candidate_text: str = ""
    candidate_reason: str | None = None
    _feedback_turns_ns: deque[int] = field(default_factory=deque)

    def start(
        self,
        *,
        during_playback: bool,
        now_ns: int | None = None,
        vad_anchored: bool = True,
    ) -> None:
        self.candidate_active = True
        self.candidate_during_playback = self.enabled and during_playback
        self.candidate_vad_anchored = vad_anchored
        self.candidate_started_ns = now_ns if now_ns is not None else time.monotonic_ns()
        self.candidate_decision = (
            PlaybackInputDecision.WAIT
            if self.candidate_during_playback
            else PlaybackInputDecision.ACCEPT
        )
        self.candidate_text = ""
        self.candidate_reason = None

    def observe(
        self,
        text: str,
        *,
        final: bool,
        assistant_text: str,
        now_ns: int | None = None,
        during_playback_if_unstarted: bool = False,
    ) -> PlaybackInputDecision:
        if not self.candidate_active:
            self.start(
                during_playback=during_playback_if_unstarted,
                now_ns=now_ns,
                vad_anchored=False,
            )
        self.candidate_text = text
        if not self.candidate_during_playback:
            self.candidate_decision = PlaybackInputDecision.ACCEPT
            return self.candidate_decision

        if not self.candidate_vad_anchored:
            self.candidate_reason = "unanchored_playback_transcript"
            self.candidate_decision = (
                PlaybackInputDecision.IGNORE if final else PlaybackInputDecision.WAIT
            )
            return self.candidate_decision

        content = _content(text)
        now = now_ns if now_ns is not None else time.monotonic_ns()
        started = self.candidate_started_ns if self.candidate_started_ns is not None else now
        elapsed_ms = max(0, int((now - started) / 1_000_000))
        interrupt_decision = self.interruption_guard.evaluate(
            elapsed_ms=elapsed_ms,
            asr_text=text,
            has_speech_energy=True,
        )
        self.candidate_reason = guarded_input_reason(
            text,
            duration_ms=900,
            assistant_text=assistant_text,
            multilingual=self.multilingual,
        )
        if self.candidate_reason is not None:
            self.candidate_decision = (
                PlaybackInputDecision.IGNORE if final else PlaybackInputDecision.WAIT
            )
        elif interrupt_decision is InterruptDecision.CONFIRM_INTERRUPT:
            self.candidate_decision = PlaybackInputDecision.ACCEPT
        elif count_cjk_chars(text) >= 5 or len(content) >= 5:
            self.candidate_decision = PlaybackInputDecision.ACCEPT
        elif final and content:
            self.candidate_decision = PlaybackInputDecision.ACCEPT
        else:
            self.candidate_decision = PlaybackInputDecision.WAIT
        return self.candidate_decision

    def accept_turn(self, text: str, *, now_ns: int | None = None) -> tuple[bool, str | None]:
        now = now_ns if now_ns is not None else time.monotonic_ns()
        decision = self.candidate_decision
        during_playback = self.candidate_active and self.candidate_during_playback
        self.candidate_active = False

        if during_playback and decision is not PlaybackInputDecision.ACCEPT:
            return False, self.candidate_reason or "playback_noise"
        if during_playback:
            cutoff = now - int(self.feedback_window_s * 1_000_000_000)
            while self._feedback_turns_ns and self._feedback_turns_ns[0] < cutoff:
                self._feedback_turns_ns.popleft()
            if len(self._feedback_turns_ns) >= self.max_feedback_turns:
                return False, "feedback_circuit_open"
            self._feedback_turns_ns.append(now)
        else:
            self._feedback_turns_ns.clear()

        if _MULTILINGUAL_REQUEST.search(text):
            self.multilingual = True
        elif _ZH_EN_REQUEST.search(text):
            self.multilingual = False
        return True, None

    def guarded_reason(
        self,
        text: str,
        *,
        duration_ms: int,
        assistant_text: str,
    ) -> str | None:
        return guarded_input_reason(
            text,
            duration_ms=duration_ms,
            assistant_text=assistant_text,
            multilingual=self.multilingual,
        )
