"""Semantic completeness pre-judgment for dynamic VAD tail pruning (D-07).

Analyzes streaming ASR partial and final transcripts to classify utterance completeness:
- COMPLETE: Short questions, commands, confirmations, or self-contained clauses.
  Allows pruning endpoint grace from 800-1000ms down to 500ms (saves 300-500ms TTFB).
- INCOMPLETE: Utterances ending in conjunctions, hesitations, fillers, or dangling prepositions.
  Extends grace to 1200ms to avoid cutting off children mid-utterance.
- NEUTRAL: Longer or ambiguous utterances; falls back to standard adaptive grace (0.9s).
"""

from __future__ import annotations

import re
from enum import StrEnum

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
