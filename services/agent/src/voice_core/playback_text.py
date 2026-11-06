"""Text rules for spoken playback stops and content barge-in.

Pure, session-free vocabulary and timing rules used by
``MediaPlaybackStopMixin``; the mixin keeps the session state and the ASR
pipeline wiring.
"""

from __future__ import annotations

import logging
import os

from services.agent.src.orchestration.interruption_guard import count_cjk_chars

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
_SPEECH_MARKUP = str.maketrans("", "", "，。！？、,.!?;；:：…~～\"'“”‘’（）() \t\r\n　")
# A request to keep talking is the opposite of a barge-in; 「不要停」 must never stop the reply.
_KEEP_PLAYING = ("不要停", "别停", "不用停", "继续", "接着讲", "接着说")


def content_barge_in_wanted(text: str) -> bool:
    """True when words heard over playback are the owner's next question.

    A backchannel alone (「嗯」「对」「好」) is agreement, never a stop.  What is left after
    the backchannel needs four characters or a question/command word to endpoint the reply.
    """

    if any(phrase in text for phrase in _KEEP_PLAYING):
        return False
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
    if not remainder:
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
