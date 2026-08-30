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
    "等下",
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
    "你先别说",
    "你可以先听我说",
)

_LEADING_INTERRUPT_FILLERS = (
    "嗯",
    "啊",
    "呃",
    "哦",
    "额",
    "哎",
    "喂",
    "那个",
    "就是",
    "唉",
    "欸",
)

_NEGATED_PROPOSITION_PREFIXES = (
    "不是所有",
    "不是每",
    "不是任何",
    "不是因为",
    "不是由于",
    "不是为了",
    "不是说",
    "不对称",
    "不对等",
)

_TRAILING_CONTROL_PARTICLES = (
    "好吗",
    "可以吗",
    "好吧",
    "吗",
    "嘛",
    "呢",
    "吧",
    "呀",
    "啊",
    "哦",
    "好",
    "好的",
)

_INTERRUPT_PUNCTUATION = re.compile(r"""[\s。！？.!?，,；;：:"'“”‘’（）()【】[\]…·~～]""")

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
    "够了",
    "好了",
    "行了",
    "可以了",
    "不用了",
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

# A completion acknowledgement is different from a normal backchannel: while
# the assistant is speaking, phrases such as「好了，知道了」mean “stop here”
# and should receive one fixed acknowledgement instead of another LLM turn.
_COMPLETION_ACK_ONLY = frozenset(
    {
        "好了",
        "好了好了",
        "好了知道了",
        "好了我知道了",
        "够了",
        "够了够了",
        "好的知道了",
        "好的我知道了",
        "行了",
        "行了行了",
        "行了知道了",
        "行了我知道了",
        "可以了知道了",
        "可以了我知道了",
        "可以了",
        "不用了",
        "不用了知道了",
        "不用再说了",
        "我知道了不用说了",
        "够了知道了",
        "知道了好了",
    }
)

# Exact, control-only phrases that end the active device conversation.  Keep
# this list separate from interruption commands: an interrupt hands the floor
# back and continues listening, while these phrases return the device to
# standby.  Exact matching prevents sentences such as「我知道了怎么做」or
#「再见是什么意思」from closing a live conversation.
_CONVERSATION_CLOSE_ONLY = frozenset(
    {
        "再见",
        "拜拜",
        "拜拜了",
        "下次见",
        "回头见",
        "知道了",
        "我知道了",
        "好的知道了",
        "好的我知道了",
        "好了知道了",
        "好了我知道了",
        "退下",
        "退下吧",
        "你退下吧",
        "先这样",
        "先这样吧",
        "今天先这样",
        "就这样",
        "就这样吧",
        "聊到这里",
        "聊到这吧",
        "待命吧",
        "去待命吧",
        "休息吧",
        "你休息吧",
        "不用陪我了",
    }
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
_WEEKDAY_ECHO = re.compile(r"^(?:(?:今天|现在)(?:是)?)?(?:星期|周|礼拜)([一二三四五六日天])$")
_WEEKDAY_TOKEN = re.compile(r"(?:星期|周|礼拜)([一二三四五六日天])")


def normalize_short(text: str) -> str:
    return text.strip().replace(" ", "").replace("　", "").strip("。！？!?，,；;：:")


def is_primarily_non_chinese_script(text: str) -> bool:
    """True when garbled rescue text is mostly non-Chinese symbols or hangul."""

    compact = normalize_short(text)
    if not compact or len(compact) > 48:
        return False
    cjk = sum(1 for char in compact if "\u4e00" <= char <= "\u9fff")
    hangul = sum(1 for char in compact if "\uac00" <= char <= "\ud7af")
    kana = sum(1 for char in compact if "\u3040" <= char <= "\u30ff")
    latin = sum(1 for char in compact if char.isascii() and char.isalpha())
    if hangul + kana >= 2 and hangul + kana >= cjk:
        return True
    if cjk == 0 and hangul + kana + latin >= max(2, len(compact) // 2):
        return True
    return False


def is_backchannel(text: str, *, duration_ms: int) -> bool:
    t = normalize_short(text)
    if not t or duration_ms > 900:
        return False
    return t in BACKCHANNEL_WHITELIST


def is_explicit_interrupt(text: str) -> bool:
    return _interrupt_prefix(text) is not None or is_completion_ack_only(text)


def is_completion_ack_only(text: str) -> bool:
    """True for an explicit acknowledgement that ends the current reply."""

    return _compact_interrupt_text(text) in _COMPLETION_ACK_ONLY


def is_conversation_close_only(text: str) -> bool:
    """Return whether one exact owner utterance requests device standby."""

    return _compact_interrupt_text(text) in _CONVERSATION_CLOSE_ONLY


def _strip_leading_interrupt_fillers(text: str) -> str:
    remainder = text
    while remainder:
        matched = next(
            (
                filler
                for filler in sorted(
                    _LEADING_INTERRUPT_FILLERS,
                    key=len,
                    reverse=True,
                )
                if remainder.startswith(filler)
            ),
            None,
        )
        if matched is None:
            return remainder
        remainder = remainder[len(matched) :]
    return remainder


def _compact_interrupt_text(text: str) -> str:
    return _INTERRUPT_PUNCTUATION.sub("", text)


def _interrupt_prefix(text: str) -> str | None:
    normalized = _strip_leading_interrupt_fillers(_compact_interrupt_text(text))
    if not normalized:
        return None
    if normalized in {"等下我", "等下你"}:
        return None
    if normalized == "停":
        return "停"
    if any(normalized.startswith(prefix) for prefix in _NEGATED_PROPOSITION_PREFIXES):
        return None
    return next(
        (
            prefix
            for prefix in sorted(INTERRUPT_PREFIXES, key=len, reverse=True)
            if normalized.startswith(prefix)
        ),
        None,
    )


def interrupt_ack_phrase(text: str) -> str:
    """Pick a natural interrupt ack from user wording.

    - yield floor (停一下 / 等等 / 听我说…):「嗯，你说。」
    - stop talking (别说了 / 暂停 / 停下…):「好的。」
    - unknown / empty: default yield-floor style.
    """
    t = normalize_short(text)
    if not t:
        return "嗯，你说。"
    if is_completion_ack_only(t):
        return "好的。"
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
    if is_completion_ack_only(text):
        return True
    remainder = _strip_leading_interrupt_fillers(_compact_interrupt_text(text))
    if not remainder or _interrupt_prefix(remainder) is None:
        return False
    while remainder:
        prefix = _interrupt_prefix(remainder)
        if prefix is None:
            break
        remainder = remainder[len(prefix) :]
        between_commands = _strip_leading_interrupt_fillers(remainder)
        if _interrupt_prefix(between_commands) is None:
            break
        remainder = between_commands
    remainder = _compact_interrupt_text(remainder)
    while remainder:
        particle = next(
            (
                candidate
                for candidate in sorted(
                    _TRAILING_CONTROL_PARTICLES,
                    key=len,
                    reverse=True,
                )
                if remainder.endswith(candidate)
            ),
            None,
        )
        if particle is None:
            break
        remainder = remainder[: -len(particle)]
    return not _compact_interrupt_text(remainder)


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


def _is_low_information_fragment(text: str, *, multilingual: bool) -> bool:
    """Reject likely playback/ASR debris without blocking normal short acks."""

    if multilingual or is_explicit_interrupt(text):
        return False
    raw_content = text.strip(_PUNCTUATION).replace(" ", "")
    content = raw_content.lower()
    if not content or content in BACKCHANNEL_WHITELIST:
        return False
    cjk = count_cjk_chars(content)
    latin_chars = "".join(char for char in raw_content if char.isascii() and char.isalnum())
    latin = len(latin_chars)
    # A single non-ack CJK character (e.g. production's ``其。``) is not a
    # reliable conversational turn after playback. Mixed-script fragments
    # such as ``对谢ght`` are the same echo/decoder failure in another form.
    if cjk == 1 and len(content) == 1:
        return "?" not in text and "？" not in text
    if not (cjk > 0 and latin > 0 and len(content) <= 8 and cjk <= 2):
        return False
    # Short product/model names are legitimate mixed-language content; the
    # production failure was a lowercase decoder tail (``ght``), not ``GPT``.
    if latin_chars.casefold() in {"ai", "api", "app", "gpt", "http", "url", "wifi"}:
        return False
    return not any(char.isupper() for char in latin_chars)


def _looks_like_assistant_echo(text: str, assistant_text: str) -> bool:
    content = _content(text)
    spoken = _content(assistant_text)
    if content == "等下" and ("等下" in spoken or "等一下" in spoken):
        return True
    weekday_echo = _WEEKDAY_ECHO.fullmatch(content)
    if weekday_echo is not None:
        return any(
            token.group(1) == weekday_echo.group(1) for token in _WEEKDAY_TOKEN.finditer(spoken)
        )
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
    if _is_low_information_fragment(text, multilingual=multilingual):
        return "low_information_fragment"
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

    @staticmethod
    def _meaningful_turn(text: str) -> bool:
        content = _content(text)
        return count_cjk_chars(content) >= 3 or len(content) >= 5

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

    def accept_turn(
        self,
        text: str,
        *,
        now_ns: int | None = None,
        assistant_text: str = "",
    ) -> tuple[bool, str | None]:
        now = now_ns if now_ns is not None else time.monotonic_ns()
        decision = self.candidate_decision
        during_playback = self.candidate_active and self.candidate_during_playback
        self.candidate_active = False

        if during_playback:
            # LiveKit can deliver the endpoint callback after the last ASR
            # interim. Re-evaluate the canonical final instead of carrying a
            # short ``好的``/echo decision into a real follow-up question.
            started = self.candidate_started_ns if self.candidate_started_ns is not None else now
            elapsed_ms = max(0, int((now - started) / 1_000_000))
            fresh_reason = self.guarded_reason(
                text,
                duration_ms=elapsed_ms,
                assistant_text=assistant_text,
            )
            stale_reason = self.candidate_reason in {
                "backchannel",
                "assistant_echo",
                "non_target_language",
                "low_information_fragment",
            }
            if (
                stale_reason
                or self.candidate_reason is None
                or decision is PlaybackInputDecision.WAIT
            ):
                self.candidate_reason = fresh_reason
                decision = (
                    PlaybackInputDecision.IGNORE
                    if fresh_reason is not None
                    else PlaybackInputDecision.ACCEPT
                )
                self.candidate_decision = decision

        if during_playback and decision is not PlaybackInputDecision.ACCEPT:
            return False, self.candidate_reason or "playback_noise"
        if during_playback:
            cutoff = now - int(self.feedback_window_s * 1_000_000_000)
            while self._feedback_turns_ns and self._feedback_turns_ns[0] < cutoff:
                self._feedback_turns_ns.popleft()
            if self._meaningful_turn(text):
                # A complete, revalidated final is evidence of a real user
                # turn, not another feedback pulse. It must not consume the
                # short-candidate circuit budget.
                self._feedback_turns_ns.clear()
            else:
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
