"""Semantic completeness pre-judgment for dynamic VAD tail pruning (D-07).

Analyzes streaming ASR partial and final transcripts to classify utterance completeness:
- COMPLETE: Short questions, commands, confirmations, or self-contained clauses.
  Allows pruning endpoint grace from 800-1000ms down to 500ms (saves 300-500ms TTFB).
- INCOMPLETE: Utterances ending in conjunctions, hesitations, fillers, or dangling prepositions.
  Extends grace to 1200ms to avoid cutting off children mid-utterance.
- NEUTRAL: Longer or ambiguous utterances; falls back to standard adaptive grace (0.9s).
"""

from __future__ import annotations

import logging
import re
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

logger = logging.getLogger(__name__)

_QUESTION_PARTICLES = ("吗", "呢", "吧", "呀", "么", "嘛", "啦", "啊")
_QUESTION_PUNCTUATION = ("？", "?")

_INTERROGATIVE_WORDS = (
    "谁",
    "什么",
    "哪",
    "哪里",
    "哪个",
    "几",
    "多少",
    "怎么",
    "怎样",
    "为什么",
    "干嘛",
    "何",
    "行不行",
    "好不好",
    "可以吗",
    "对不对",
    "是不是",
    "想不想",
    "去不去",
    "能不能",
)

_COMPLETE_SINGLE_CHARS = frozenset({"停", "对", "行", "好"})

_COMPLETE_EXACT_PHRASES = frozenset(
    {
        # Commands & interruptions
        "停",
        "别说了",
        "闭嘴",
        "暂停",
        "安静",
        "好了",
        # Content requests
        "讲个故事",
        "讲故事",
        "讲个故事吧",
        "唱首歌",
        "再讲一个",
        "换一个",
        "继续",
        "接着讲",
        # Assistance & memory
        "帮我",
        "帮我记一下",
        "帮我查一下",
        "记住这个",
        "提醒我",
        # Greetings
        "你好",
        "早上好",
        "中午好",
        "下午好",
        "晚上好",
        "晚安",
        "再见",
        "拜拜",
        # Confirmations & short answers
        "对",
        "对的",
        "是的",
        "不是",
        "不用",
        "不用了",
        "可以",
        "可以的",
        "行",
        "行的",
        "好的",
        "好吧",
        "没问题",
        "谢谢",
        "谢谢你",
        "不知道",
    }
)

_INCOMPLETE_SUFFIXES = (
    # Conjunctions / transitional
    "然后",
    "而且",
    "还有",
    "因为",
    "但是",
    "不过",
    "如果",
    "所以",
    "或者",
    "接着",
    "虽然",
    "就是说",
    "而且还",
    # Fillers & hesitations
    "我想想",
    "让我想想",
    "想一想",
    "那个",
    "就是",
    "嗯",
    "呃",
    "啊这个",
    "emm",
    # Prepositions & dangling verbs
    "在",
    "给",
    "和",
    "跟",
    "把",
    "被",
    "往",
    "向",
    "从",
    "听",
    "我想",
    "想要",
    "打算",
    "准备",
)

# "我想说" / "我要去": a desire verb plus an action verb with no object yet.
_DANGLING_DESIRE_VERB = re.compile(
    r"(?<!不)(想|要)(说|讲|问|看|玩|吃|唱|画|做|去|买|找|学|写|读|拿)$"
)

_SHORT_SENTENCE_MAX_LEN = 18


class SemanticCompleteness(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    NEUTRAL = "neutral"


def classify_semantic_completeness(text: str) -> SemanticCompleteness:
    """Classify the semantic completeness of an utterance for dynamic VAD tail pruning."""
    if not text:
        return SemanticCompleteness.NEUTRAL
    stripped = text.strip()
    if not stripped:
        return SemanticCompleteness.NEUTRAL

    has_question_punct = stripped.endswith(_QUESTION_PUNCTUATION)
    has_exclamation_punct = stripped.endswith(("！", "!"))

    cleaned = re.sub(r"[，。！？；：、“”‘’（）《》…—,.!?;:\'\"()\-]+$", "", stripped).strip()
    if not cleaned:
        return SemanticCompleteness.NEUTRAL

    if cleaned in _COMPLETE_EXACT_PHRASES:
        return SemanticCompleteness.COMPLETE

    # If it ends with question punctuation or question particle, it is a question
    if has_question_punct or cleaned.endswith(_QUESTION_PARTICLES):
        return SemanticCompleteness.COMPLETE

    # Check incomplete suffixes
    if any(cleaned.endswith(suffix) for suffix in _INCOMPLETE_SUFFIXES):
        return SemanticCompleteness.INCOMPLETE
    if _DANGLING_DESIRE_VERB.search(cleaned):
        return SemanticCompleteness.INCOMPLETE

    # Single character checks
    if len(cleaned) == 1:
        if cleaned in _COMPLETE_SINGLE_CHARS:
            return SemanticCompleteness.COMPLETE
        return SemanticCompleteness.INCOMPLETE

    # Interrogatives in short/medium sentences
    if any(q in cleaned for q in _INTERROGATIVE_WORDS) and len(cleaned) <= _SHORT_SENTENCE_MAX_LEN:
        return SemanticCompleteness.COMPLETE

    # Exclamation mark at end of short clause
    if has_exclamation_punct and len(cleaned) <= _SHORT_SENTENCE_MAX_LEN:
        return SemanticCompleteness.COMPLETE

    # Short self-contained declarative clause
    if len(cleaned) <= _SHORT_SENTENCE_MAX_LEN:
        return SemanticCompleteness.COMPLETE

    return SemanticCompleteness.NEUTRAL


def is_semantically_complete(text: str) -> bool:
    return classify_semantic_completeness(text) == SemanticCompleteness.COMPLETE


def is_semantically_incomplete(text: str) -> bool:
    return classify_semantic_completeness(text) == SemanticCompleteness.INCOMPLETE


def current_pending_turn_text(context: _MediaVoiceSession) -> str:
    """Best-effort extraction of the pending turn's text for semantic pre-judgment."""
    partial = context.pending.pending_partial
    partial_text = partial.text.strip() if partial is not None and partial.text else ""

    timeline_text = ""
    start = context.pending.turn_start_sample
    end = context.pending.turn_endpoint_sample or context.pending.turn_end_sample
    if start is not None and end is not None and end > start:
        try:
            timeline_text = context.runtime.speech_timeline.projected_text(
                stream_epoch=context.stream_epoch,
                start_sample=start,
                end_sample=end,
            ).strip()
        except Exception:
            timeline_text = ""

    if partial_text and len(partial_text) >= len(timeline_text):
        return partial_text
    if timeline_text:
        return timeline_text
    return partial_text


DTLN_SPEECH_END_SCORE_HIGH_CUT = 0.5


def _semantic_tail_enabled(registry: Any) -> bool:
    """Semantic pruning only engages for production-range grace settings.

    A configured grace shorter than the complete-utterance grace is already tighter
    than any pruning target, and a zero or sub-minimum grace is a deliberate
    fast-path (tests, drills), so neither may be lengthened by the incomplete rule.
    """
    configured = float(registry.turn_endpoint_grace_s)
    if configured <= 0:
        return False
    floor = max(
        float(registry.turn_endpoint_min_grace_s),
        float(registry.turn_endpoint_grace_complete_s),
    )
    return configured >= floor


def _neutral_tail_grace(registry: Any, observed_pause_s: float | None) -> float:
    configured = float(registry.turn_endpoint_grace_s)
    minimum = float(registry.turn_endpoint_min_grace_s)
    maximum = float(registry.turn_endpoint_max_grace_s)
    if observed_pause_s is None:
        return min(configured, maximum)
    return min(
        maximum,
        max(minimum, observed_pause_s + 0.15),
    )


def _complete_prune_withheld(registry: Any, context: _MediaVoiceSession) -> bool:
    if not registry.dtln_tail_confidence_enabled:
        return False
    pending = context.pending
    if (
        pending.clock_fact_endpoint_pinned is not None
        or pending.live_query_endpoint_pinned is not None
        or pending.conversation_close_endpoint_pinned is not None
        or pending.playback_followup_endpoint_sample is not None
    ):
        return False
    latch = pending.speech_end_denoise_latch
    if latch is None or not latch.applies_to(pending.turn_endpoint_sample):
        return True
    return latch.score.summary < DTLN_SPEECH_END_SCORE_HIGH_CUT


def adaptive_endpoint_grace(
    registry: Any,
    context: _MediaVoiceSession,
    *,
    text: str | None = None,
) -> float:
    configured = float(registry.turn_endpoint_grace_s)
    minimum = float(registry.turn_endpoint_min_grace_s)
    if configured <= 0 or configured < minimum:
        return configured

    if _semantic_tail_enabled(registry):
        eval_text = text if text is not None else current_pending_turn_text(context)
        completeness = classify_semantic_completeness(eval_text)
        if (
            completeness == SemanticCompleteness.COMPLETE
            and not _complete_prune_withheld(registry, context)
        ):
            return float(registry.turn_endpoint_grace_complete_s)
        if completeness == SemanticCompleteness.INCOMPLETE:
            return float(registry.turn_endpoint_grace_incomplete_s)

    return _neutral_tail_grace(registry, context.observed_within_turn_pause_s)


def maybe_rearm_grace_on_evidence(
    registry: Any,
    context: _MediaVoiceSession,
    text: str,
) -> bool:
    """Prune or extend the pending turn's grace deadline based on semantic evidence.

    Returns True when the deadline moved while a commit task is already waiting on
    it, i.e. the caller must reschedule that task.
    """
    if context.pending.turn_endpoint_sample is None:
        return False
    if not _semantic_tail_enabled(registry):
        return False

    completeness = classify_semantic_completeness(text)
    if completeness == SemanticCompleteness.NEUTRAL:
        return False

    now = time.monotonic()
    base_time = context.pending.child_speech_ended_mono or now
    current_deadline = context.pending.turn_endpoint_grace_deadline
    target_grace = adaptive_endpoint_grace(registry, context, text=text)

    if completeness == SemanticCompleteness.COMPLETE:
        target_deadline = max(now, base_time + target_grace)
        if current_deadline is not None and target_deadline >= current_deadline:
            return False
        context.pending.turn_endpoint_grace_deadline = target_deadline
        logger.info(
            "media dynamic vad tail pruned session=%s completeness=complete "
            "text_len=%s grace=%.2fs",
            context.identity.session_id,
            len(text.strip()),
            target_grace,
        )
    elif completeness == SemanticCompleteness.INCOMPLETE:
        target_deadline = base_time + target_grace
        if current_deadline is not None and target_deadline <= current_deadline:
            return False
        context.pending.turn_endpoint_grace_deadline = target_deadline
        logger.info(
            "media dynamic vad tail extended session=%s completeness=incomplete "
            "text_len=%s grace=%.2fs",
            context.identity.session_id,
            len(text.strip()),
            target_grace,
        )

    task = context.turn_endpoint_task
    return task is not None and not task.done()
