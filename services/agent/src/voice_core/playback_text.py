"""Text rules for spoken playback stops and content barge-in.

Pure, session-free vocabulary and timing rules used by
``MediaPlaybackStopMixin``; the mixin keeps the session state and the ASR
pipeline wiring.
"""

from __future__ import annotations

import logging
import os
from difflib import SequenceMatcher

from services.agent.src.conversation_close_router import lexical_playback_control_only
from services.agent.src.orchestration.interruption_guard import (
    count_cjk_chars,
    is_conversation_close_only,
)
from services.common.fixed_safety_scripts import spoken_prefix_for_display

# UtteranceRouter rule 4 (pure stop / completion acknowledgement) reports this
# reason when ``accept_user_turn`` suppresses the control turn.
logger = logging.getLogger(__name__)

PLAYBACK_STOP_ROUTE_REASON = "interrupt_command_only"
_PLAYBACK_STOP_CAUSE = "media_voice_stop"
# Field 2026-10-29 soak (t017/t018): the owner asked a whole new question and said 「嗯。」
# while a story played.  Only a lexical stop was accepted, so both played through and the
# question was dropped.  Content endpoints the reply when it is not a backchannel, not the
# reply's own echo, and either four characters long or carrying a question/command word.
CONTENT_BARGE_IN_CAUSE = "media_content_barge_in"
# Soak 2026-10-30 t017: tail finals from pre-playback audio overlapped the
# fresh reply by at most 0.64 s, while the shortest genuine question that
# began before a first frame ran 2.16 s into it.  1.5 s separates the two, and
# the same run past the range a turn already handled separates its own ASR
# revisions from the owner's next question (soak 2026-10-30 t019).
_BARGE_MIN_LIVE_RUN_SAMPLES = 24_000
_CONTENT_MIN_CJK = 4
_BACKCHANNEL_FILLERS = tuple(
    sorted(
        (
            "嗯", "啊", "哦", "噢", "喔", "对", "好", "行",
            "嗯嗯", "对对", "好的", "对啊", "对呀", "好啊", "行的",
        ),
        key=len,
        reverse=True,
    )
)
_CONTENT_MARKERS = (
    "等一下",
    "等等",
    "我想问",
    "我想知道",
    "我问你",
    "问你",
    "问一下",
    "为什么",
    "怎么回事",
    "怎么",
    "什么",
    "哪儿",
    "哪里",
    "谁",
    "多少",
    "能不能",
    "可不可以",
    "不要",
    "别说了",
    "别看",
    "别动",
    "别讲",
    "别吵",
    "帮我",
    "告诉我",
)
# A fuzzy echo is only accepted when almost all of the candidate is covered by
# one contiguous part of the played line. Comparing against the whole reply
# makes a short echo look unrelated as soon as the reply is longer than it.
_FUZZY_ECHO_COVERAGE = 0.85
_SPEECH_MARKUP = str.maketrans("", "", "，。！？、,.!?;；:：…~～\"'“”‘’（）() \t\r\n　")
# A request to keep talking is the opposite of a barge-in; 「不要停」 must never stop the reply.
_KEEP_PLAYING = ("不要停", "别停", "不用停", "继续", "接着讲", "接着说")
# Window of recent assistant speech checked when separating a leaked playback
# echo prefix from a trailing stop command (covers ~5.5 s ASR playback segments
# plus buffered TTS audio).
_PLAYBACK_ECHO_PREFIX_WINDOW_CHARS = 200
_ECHO_PREFIX_FUZZY_COVERAGE = 0.70
_ECHO_PREFIX_STOP_NEGATORS = ("不想", "不能", "不会", "不用", "没有", "不要", "别", "不", "没")
_ECHO_TYPO_FORBIDDEN_CHARS = frozenset("不没别莫非无未否哪谁什怎几吗呢要")
_ECHO_PREFIX_TRAILING_FORBIDDEN_CHARS = _ECHO_TYPO_FORBIDDEN_CHARS | frozenset("想会能敢该")
_UNPUNCTUATED_DEGREE_PREFIXES = (
    "太",
    "很",
    "真",
    "非常",
    "特别",
    "挺",
    "好",
    "更",
    "最",
    "这么",
    "那么",
)
_UNPUNCTUATED_SOFT_STOPS = frozenset({"好了", "好啦"})
_CLAUSE_BOUNDARY_CHARS = frozenset("，。！？、,.!?;；:：…~～\"'“”‘’（）() \t\r\n　")
_SENTENCE_BOUNDARY_CHARS = frozenset("。！？.!?;；")
_SAME_CLAUSE_EXTRA_INQUIRY_SUFFIXES = ("能不能", "可不可以", "为啥", "如何", "咋", "该不该", "要不要")


def _strip_edge_backchannels(text: str) -> str:
    remainder = text.translate(_SPEECH_MARKUP)
    stripping = True
    while stripping and remainder:
        stripping = False
        for filler in _BACKCHANNEL_FILLERS:
            if remainder.startswith(filler):
                remainder = remainder[len(filler) :]
                stripping = True
            elif remainder.endswith(filler):
                remainder = remainder[: -len(filler)]
                stripping = True
    return remainder


def _candidate_has_question_signal(text: str) -> bool:
    """Keep a question from being suppressed by a near-copy playback echo.

    Only a closing particle or question punctuation counts. A reply line that
    happens to carry 「什么」 or 「哪」 must not make its own echo look like an
    owner question.
    """

    stripped = text.strip().rstrip("。.!！…~～ \t\r\n　")
    if stripped.endswith(("吗", "呢")):
        return True
    return any(punctuation in text for punctuation in "？?")


def playback_text_is_echo(text: str, playback_text: str) -> bool:
    """Match a candidate to the current line with a conservative fuzzy bound."""

    heard = _compact(_strip_edge_backchannels(text))
    spoken = _compact(playback_text)
    if not heard or not spoken:
        return False
    if heard in spoken:
        return True
    if len(heard) < 8 or len(spoken) < 8:
        return False
    # A near-copy question is still the owner's turn: 「公园吗」 versus the
    # played 「公园吧」 shares almost the whole candidate, but treating that
    # coverage as echo would suppress a real question.
    if _candidate_has_question_signal(text):
        return False
    matcher = SequenceMatcher(None, heard, spoken, autojunk=False)
    if len(heard) >= 10 and len(spoken) >= 10:
        match = matcher.find_longest_match(0, len(heard), 0, len(spoken))
        if match.size / len(heard) >= _FUZZY_ECHO_COVERAGE:
            return True
    # Tolerate a single homophone substitution inside an 8+ char echo (e.g.
    # 「小土子轻轻地说安静」 vs 「小兔子轻轻地说安静地听一会儿」) when both edges
    # anchor inside one tight span. Reject pure insertions (e.g. user inserting
    # 「不」 into 「这只小熊喜欢吃蜂蜜」) and any unaligned negation/inquiry char.
    blocks = [block for block in matcher.get_matching_blocks() if block.size > 0]
    max_span = len(heard) + 2
    for start_idx, first in enumerate(blocks):
        if first.a > 1 or first.a > first.b:
            continue
        span_blocks = [first]
        covered = first.size
        max_block = first.size
        valid_gaps = True
        for block in blocks[start_idx + 1 :]:
            if (block.b + block.size) - first.b > max_span:
                break
            prev = span_blocks[-1]
            gap_a = block.a - (prev.a + prev.size)
            gap_b = block.b - (prev.b + prev.size)
            if gap_a > gap_b:
                valid_gaps = False
                break
            span_blocks.append(block)
            covered += block.size
            if block.size > max_block:
                max_block = block.size
        if not valid_gaps:
            continue
        last = span_blocks[-1]
        trailing_gap = len(heard) - (last.a + last.size)
        if (
            max_block < 4
            or trailing_gap > 1
            or last.b + last.size + trailing_gap > len(spoken)
            or covered / len(heard) < _FUZZY_ECHO_COVERAGE
        ):
            continue
        unaligned_parts = [heard[: first.a], heard[last.a + last.size :]]
        for prev, cur in zip(span_blocks, span_blocks[1:], strict=False):
            unaligned_parts.append(heard[prev.a + prev.size : cur.a])
        unaligned = "".join(unaligned_parts)
        if any(char in _ECHO_TYPO_FORBIDDEN_CHARS for char in unaligned):
            continue
        return True
    return False


def playback_stop_is_echo_for_line(text: str, current_line: str) -> bool:
    """True when a standalone lexical stop matches the line currently playing."""

    if not lexical_playback_control_only(text):
        return False
    compact = _compact(text)
    line = _compact(current_line)
    if not compact or not line:
        return False
    return compact in line or ("停" in compact and "停" in line)


def _compact_with_boundaries(raw_window: str) -> tuple[str, set[int], set[int]]:
    """Compact ``raw_window`` and record compact offsets at clause and sentence boundaries."""

    chars: list[str] = []
    clause_ends: set[int] = set()
    sentence_ends: set[int] = set()
    for char in raw_window:
        if char.isalnum():
            chars.append(char)
        elif char in _CLAUSE_BOUNDARY_CHARS and chars:
            offset = len(chars)
            clause_ends.add(offset)
            if char in _SENTENCE_BOUNDARY_CHARS:
                sentence_ends.add(offset)
    if chars:
        offset = len(chars)
        clause_ends.add(offset)
        sentence_ends.add(offset)
    return "".join(chars), clause_ends, sentence_ends


def _match_playback_echo_prefix_ends(
    prefix_compact: str,
    spoken: str,
    *,
    allow_trailing_typo: bool,
) -> list[int]:
    """Return end offsets in ``spoken`` where ``prefix_compact`` aligns as echo.

    Short prefixes (3–5 chars) require a verbatim match with no inquiry cue.
    Longer prefixes (>= 6 chars) also accept local-window fuzzy overlap (to
    tolerate internal ASR numeral/homophone variance such as 「12」 vs 「十二」)
    only when the match anchors to both edges (requiring exact alignment at the
    tail when no punctuation separates prefix and stop word, so modal words like
    「要」 in 「森林里的小动物要安静」 are never swallowed) and introduces no
    inquiry or negation cue absent from ``spoken``.
    """

    if len(prefix_compact) < 3 or not spoken:
        return []
    if len(prefix_compact) < 6:
        if any(marker in prefix_compact for marker in _CONTENT_INQUIRY_MARKERS):
            return []
        ends: list[int] = []
        pos = spoken.find(prefix_compact)
        while pos != -1:
            ends.append(pos + len(prefix_compact))
            pos = spoken.find(prefix_compact, pos + 1)
        return ends
    if any(
        marker in prefix_compact and marker not in spoken
        for marker in _CONTENT_INQUIRY_MARKERS
    ):
        return []
    exact_ends: list[int] = []
    pos = spoken.find(prefix_compact)
    while pos != -1:
        exact_ends.append(pos + len(prefix_compact))
        pos = spoken.find(prefix_compact, pos + 1)
    if exact_ends:
        return exact_ends
    blocks = [
        block
        for block in SequenceMatcher(
            None, prefix_compact, spoken, autojunk=False
        ).get_matching_blocks()
        if block.size > 0
    ]
    if not blocks:
        return []
    max_span = int(len(prefix_compact) * 1.6) + 4
    fuzzy_ends: list[int] = []
    for start_idx, first in enumerate(blocks):
        if first.a > 1 or first.a > first.b:
            continue
        if first.a == 1 and prefix_compact[0] in _ECHO_TYPO_FORBIDDEN_CHARS:
            continue
        span_blocks = [first]
        covered = first.size
        max_block = first.size
        valid_gaps = True
        for block in blocks[start_idx + 1 :]:
            if (block.b + block.size) - first.b > max_span:
                break
            prev = span_blocks[-1]
            unaligned_a = prefix_compact[prev.a + prev.size : block.a]
            unaligned_b = spoken[prev.b + prev.size : block.b]
            if any(
                char in _ECHO_TYPO_FORBIDDEN_CHARS and char not in unaligned_b
                for char in unaligned_a
            ):
                valid_gaps = False
                break
            span_blocks.append(block)
            covered += block.size
            if block.size > max_block:
                max_block = block.size
        if not valid_gaps:
            continue
        last = span_blocks[-1]
        trailing_gap = len(prefix_compact) - (last.a + last.size)
        if trailing_gap > (1 if allow_trailing_typo else 0):
            continue
        if trailing_gap == 1 and (
            last.b + last.size + 1 > len(spoken)
            or prefix_compact[-1] in _ECHO_PREFIX_TRAILING_FORBIDDEN_CHARS
        ):
            continue
        if max_block >= 3 and covered / len(prefix_compact) >= _ECHO_PREFIX_FUZZY_COVERAGE:
            fuzzy_ends.append(last.b + last.size + trailing_gap)
    return fuzzy_ends


def _tail_continues_spoken_echo(
    tail_compact: str,
    spoken: str,
    prefix_ends: list[int],
    clause_ends: set[int],
    *,
    punctuated: bool,
) -> bool:
    """True when ``tail_compact`` continues ``spoken`` verbatim or via a 1-char ASR typo."""

    tail_len = len(tail_compact)
    for end in prefix_ends:
        window = spoken[max(0, end - 1) : end + tail_len + 4]
        if tail_compact in window:
            return True
        aligned = spoken[end : end + tail_len]
        if len(aligned) != tail_len:
            continue
        shared = sum(a == b for a, b in zip(tail_compact, aligned, strict=True))
        if tail_len >= 3 and shared >= tail_len - 1:
            return True
        if (
            tail_len == 2
            and not punctuated
            and end not in clause_ends
            and (end + 2) in clause_ends
            and shared >= 1
        ):
            return True
    return False


def _single_char_tail_replaces_spoken_char(
    tail_compact: str,
    prefix_ends: list[int],
    clause_ends: set[int],
    *,
    punctuated: bool,
) -> bool:
    """True when a 1-char stop like 「停」 likely replaces a mid-clause homophone (e.g. 凉亭→凉停)."""

    if len(tail_compact) != 1:
        return False
    for end in prefix_ends:
        if end not in clause_ends and (not punctuated or (end + 1) in clause_ends):
            return True
    return False


def _longest_trailing_lexical_stop(raw: str) -> tuple[int, str, str] | None:
    """Return ``(idx, tail, tail_compact)`` for the longest trailing control phrase."""

    for idx in range(1, len(raw)):
        if raw[idx] in _CLAUSE_BOUNDARY_CHARS:
            continue
        tail = raw[idx:].strip()
        tail_compact = _compact(tail)
        if tail_compact and len(tail_compact) <= 6 and lexical_playback_control_only(tail):
            return idx, tail, tail_compact
    return None


def extract_playback_stop_after_echo(text: str, playback_text: str) -> str | None:
    """Recover a trailing stop command glued behind leaked playback echo.

    Full-duplex AEC (build 39 ``AEC_MODE_FD_HIGH_PERF``) preserves double-talk
    onsets but can occasionally concatenate a few seconds of assistant echo
    ahead of the owner's stop word in one ASR final (e.g.
    「霸王龙大概12米长。停一下。」).  When the whole utterance is not verbatim
    echo, the user-side tail carries no question or keep-playing signal, the
    longest trailing control phrase is not negated or a continuation/homophone
    replacement of ``playback_text``, and the prefix aligns as echo up to the
    command boundary, return the trailing lexical stop.
    """

    raw = text.strip()
    if not raw or not playback_text or lexical_playback_control_only(raw):
        return None
    recent_raw = playback_text[-_PLAYBACK_ECHO_PREFIX_WINDOW_CHARS:]
    script_spoken = spoken_prefix_for_display(playback_text.strip())
    raw_windows = (
        (recent_raw, script_spoken[-_PLAYBACK_ECHO_PREFIX_WINDOW_CHARS:])
        if script_spoken
        else (recent_raw,)
    )
    spoken_windows = [
        (window, spoken, clause_ends, sentence_ends)
        for window in raw_windows
        for spoken, clause_ends, sentence_ends in (_compact_with_boundaries(window),)
        if spoken
    ]
    if not spoken_windows:
        return None
    raw_compact = _compact(raw)
    if any(raw_compact in spoken for _, spoken, _, _ in spoken_windows):
        return None
    # Pick the longest trailing control phrase once; if its context rejects it
    # (e.g. 「不想暂停」「不能先安静」), never chop into a shorter sub-suffix.
    candidate = _longest_trailing_lexical_stop(raw)
    if candidate is None:
        return None
    idx, tail, tail_compact = candidate
    if _candidate_has_question_signal(tail) or any(phrase in tail for phrase in _KEEP_PLAYING):
        return None
    prefix_raw = raw[:idx]
    punctuated = bool(prefix_raw) and prefix_raw[-1] in _CLAUSE_BOUNDARY_CHARS
    sentence_boundary = (
        bool(prefix_raw.rstrip()) and prefix_raw.rstrip()[-1] in _SENTENCE_BOUNDARY_CHARS
    )
    prefix_compact = _compact(prefix_raw)
    if len(prefix_compact) < 3:
        return None
    boundary_text = prefix_compact[-3:] + tail_compact
    needs_sentence_boundary = prefix_compact.endswith(_ECHO_PREFIX_STOP_NEGATORS) or any(
        phrase in boundary_text and phrase not in prefix_compact for phrase in _KEEP_PLAYING
    )
    if needs_sentence_boundary and not sentence_boundary:
        return None
    if not punctuated:
        if tail_compact in _UNPUNCTUATED_SOFT_STOPS:
            return None
        if prefix_compact.endswith(_UNPUNCTUATED_DEGREE_PREFIXES):
            return None
        if prefix_compact.endswith(_CONTENT_INQUIRY_MARKERS) or prefix_compact.endswith(
            _SAME_CLAUSE_EXTRA_INQUIRY_SUFFIXES
        ):
            return None
        glued = prefix_compact[-1:] + tail_compact
        if any(glued in spoken for _, spoken, _, _ in spoken_windows):
            return None
    prefix_has_qmark = any(punctuation in prefix_raw for punctuation in "？?")
    matched_any = False
    for raw_window, spoken, clause_ends, sentence_ends in spoken_windows:
        if prefix_has_qmark and not any(punctuation in raw_window for punctuation in "？?"):
            continue
        if any(phrase in prefix_compact and phrase not in spoken for phrase in _KEEP_PLAYING):
            continue
        ends = _match_playback_echo_prefix_ends(
            prefix_compact, spoken, allow_trailing_typo=punctuated
        )
        if not ends:
            continue
        if needs_sentence_boundary and not any(end in sentence_ends for end in ends):
            continue
        if _tail_continues_spoken_echo(
            tail_compact, spoken, ends, clause_ends, punctuated=punctuated
        ):
            return None
        if _single_char_tail_replaces_spoken_char(
            tail_compact, ends, clause_ends, punctuated=punctuated
        ):
            return None
        matched_any = True
    return tail if matched_any else None


def resolve_playback_stop_text(text: str, *, playback_text: str) -> str | None:
    """Return the pure lexical stop text directly or after stripping echo."""

    cleaned = text.strip()
    if not cleaned:
        return None
    if lexical_playback_control_only(cleaned):
        return cleaned
    return extract_playback_stop_after_echo(cleaned, playback_text)


_CONTENT_INQUIRY_MARKERS = (
    "我想问",
    "我想知道",
    "我问你",
    "问你",
    "问一下",
    "告诉我",
    "为什么",
    "怎么",
    "什么",
    "哪儿",
    "哪里",
    "哪个",
    "哪些",
    "谁",
    "多少",
    "几点",
    "几岁",
    "多久",
    "多大",
    "多长",
    "多高",
)
_CONTENT_BARGE_CLOSE_HINTS = (
    "再见",
    "拜拜",
    "晚安",
    "明天见",
    "明天再",
    "改天",
    "走了",
    "睡了",
    "睡觉",
    "该睡",
    "去睡",
    "困了",
    "累了",
    "太晚",
    "不聊",
    "不玩",
    "不想聊",
    "不想听",
    "不听了",
    "不说了",
    "关机",
    "到这里",
    "到这儿",
    "到这",
    "先这样",
    "就这样",
    "休息",
    "结束",
    "待命",
    "退下",
    "下次",
    "下回",
    "以后再",
    "不用了",
    "不用陪",
    "别陪",
    "不要陪",
    "不需要",
    "不用管",
    "别管",
    "走开",
    "闭嘴",
    "别吵",
    "吵死了",
    "好吵",
    "别说",
    "不要说",
    "别讲",
    "不要讲",
    "别念",
    "不要念",
    "停下",
    "快停",
    "怎么还在",
    "怎么还",
    "怎么没停",
    "怎么不停",
    "你别",
    "你不要",
    "算了",
    "没什么",
    "没有什么",
    "没啥",
    "没问题",
    "没有问题",
    "不问",
    "不想问",
    "不用问",
    "别问",
    "不怎么",
    "怎么样",
    "什么都不",
    "什么也没",
)


def content_barge_in_skips_semantic_close(text: str) -> bool:
    """True when a content barge-in positively carries a content inquiry.

    R02 2026-11-21 suite c: every content barge-in waited ~720-780 ms for the
    remote semantic conversation-close classifier before stopping playback and
    starting the new answer. Polite closing questions (「我们今天聊到这里好吗？」)
    have question marks without an inquiry cue and must still check the remote
    classifier; explicit inquiries without leave-taking cues skip that wait.
    """

    if is_conversation_close_only(text):
        return False
    if any(hint in text for hint in _CONTENT_BARGE_CLOSE_HINTS):
        return False
    return any(marker in text for marker in _CONTENT_INQUIRY_MARKERS)


def content_barge_in_wanted(text: str, *, playback_text: str = "") -> bool:
    """True when words heard over playback are the owner's next question.

    A backchannel alone (「嗯」「对」「好」) is agreement, never a stop.  What is left after
    the backchannel needs four characters or a question/command word to endpoint the reply.
    """

    if any(phrase in text for phrase in _KEEP_PLAYING):
        return False
    remainder = _strip_edge_backchannels(text)
    if not remainder:
        return False
    if playback_text and playback_text_is_echo(text, playback_text):
        return False
    if count_cjk_chars(remainder) >= _CONTENT_MIN_CJK:
        return True
    return any(marker in remainder for marker in _CONTENT_MARKERS)


# The assistant text announced most recently is what the uplink can still be
# echoing: the phrase being played plus the one queued behind it (phrases are
# at most ~28 characters) and the ASR finalization lag.  A stop phrase found
# there is treated as the reply's own voice, never as the owner's command.
_PLAYBACK_STOP_ECHO_TAIL_CHARS = 64
# A follow-up final that begins before the playback boundary (itself 0.8 s past
# the reply's last uplink evidence) must run at least this far past it: the
# reply's echo cannot, only speech that went on after the speaker stopped.
_PLAYBACK_FOLLOWUP_STRADDLE_SAMPLES = 16_000  # 1.0 s at 16 kHz
# A follow-up final that starts wholly after the playback boundary is user
# speech, not echo: endpoint it with a short grace instead of waiting for a
# VAD edge that a stuck post-playback VAD may never emit (run 20260921).
_PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S = 1.2
# Latency experiment (TODOLIST N-14 tier 3): the grace may only be shortened, never lengthened, so
# the turn budget that is sized for the default (test_the_default_turn_budget_covers...) still holds.
PLAYBACK_FOLLOWUP_GRACE_ENV = "MEDIA_PLAYBACK_FOLLOWUP_GRACE_S"
_PLAYBACK_FOLLOWUP_MIN_GRACE_S = 0.3
# How long an incomplete follow-up (「我想听」before 「恐龙的故事」) may keep the commit open while
# later finals arrive.  Matches the unvoiced-gap split threshold so mid-sentence pauses do not mint
# a second turn that later dies on ASR tail timeout (demo: 5 sentences → 7 input turns).
_PLAYBACK_FOLLOWUP_INCOMPLETE_HOLD_S = 2.5
# Conservative sample-gap policy for unanchored device candidates observed
# during a previous reply, not a VAD silence measurement or endpoint timeout.
_PLAYBACK_CANDIDATE_SPLIT_GAP_SAMPLES = 40_000  # 2.5 s at 16 kHz
# A device VAD that began this far past the playback boundary (which already carries 0.8 s over the
# last uplink evidence) is new speech; one closer to it is still the echo's own tail (2026-10-03 round 11:
# a VAD edge 0.08 s past the boundary was the echo, 3 s later the child's question followed).
_PLAYBACK_NEW_SPEECH_VAD_SAMPLES = 12_800  # 0.8 s at 16 kHz
_FIXED_SAFETY_COMMIT_RECHECK_S = 0.5

# Sentence-final particles / punctuation: a follow-up that ends here may commit after one grace.
_FOLLOWUP_COMPLETE_SUFFIXES = (
    "吗",
    "呢",
    "吧",
    "呀",
    "啊",
    "哦",
    "啦",
    "嘛",
    "哈",
    "嗯",
    "谢谢",
    "再见",
    "拜拜",
    "。",
    "！",
    "？",
    "!",
    "?",
)
# Clause-internal endings: FunASR often finalizes here mid-utterance; hold the commit.
_FOLLOWUP_INCOMPLETE_SUFFIXES = (
    "的",
    "了",
    "着",
    "和",
    "跟",
    "在",
    "把",
    "被",
    "是",
    "有",
    "想",
    "要",
    "会",
    "能",
    "可以",
    "因为",
    "所以",
    "但是",
    "然后",
    "如果",
    "虽然",
    "听",
)


def playback_followup_grace_s() -> float:
    """The grace a post-playback follow-up final waits for the next one before it commits.

    1.2 s unless MEDIA_PLAYBACK_FOLLOWUP_GRACE_S asks for a shorter one (0.3-1.2 s).  Anything else
    (unreadable, longer, shorter than 0.3 s) keeps the default: a typo must not silently shorten the
    wait for a child's next word.
    """

    raw = os.environ.get(PLAYBACK_FOLLOWUP_GRACE_ENV, "").strip()
    if not raw:
        return _PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if _PLAYBACK_FOLLOWUP_MIN_GRACE_S <= value <= _PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S:
        return value
    logger.warning(
        "%s=%r is not within 0.3-1.2 s; keeping the default", PLAYBACK_FOLLOWUP_GRACE_ENV, raw
    )
    return _PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S


def followup_utterance_looks_complete(text: str) -> bool:
    """True when a post-playback follow-up may commit after one grace window.

    Incomplete mid-clause finals (「我想听」) must wait for the next final or the
    incomplete-hold budget; complete short follow-ups (「后天呢」) commit as today.
    """

    compact = "".join(character for character in text.strip() if not character.isspace())
    if not compact:
        return False
    if any(compact.endswith(suffix) for suffix in _FOLLOWUP_COMPLETE_SUFFIXES):
        return True
    if any(compact.endswith(suffix) for suffix in _FOLLOWUP_INCOMPLETE_SUFFIXES):
        return False
    return len(compact) >= 6


def _compact(text: str) -> str:
    return "".join(character for character in text if character.isalnum())


_SAMPLES_PER_MS = 16  # the media clock runs at 16 kHz


def _ms_after(sample: int | None, origin: int) -> str:
    """``sample - origin`` in ms for a diagnostics line, ``-`` when unset."""

    return "-" if sample is None else str((sample - origin) // _SAMPLES_PER_MS)
