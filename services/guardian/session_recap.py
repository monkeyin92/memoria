"""Session-end child recap: one summary plus optional whitelist archive facts.

Built from the in-process transcript at session end, then the transcript is
discarded. Never persists raw utterances. Product rules (authoritative):
generate once per session; skip short or story-only sessions; parent voice via
LLM; reject raw-transcript echoes and name/health leaks; archive only on
separate consent.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from services.guardian.child_memory import (
    SUMMARY_MAX_CHARS,
    ArchiveFact,
    DialogueSummary,
    forbidden_topic,
    quotes_speech,
)

Role = Literal["user", "assistant"]

# Interest topics that may become preference archive facts when the child names them.
INTEREST_TOPICS: Final[tuple[tuple[str, str], ...]] = (
    ("霸王龙", "喜欢霸王龙"),
    ("恐龙", "喜欢恐龙"),
    ("太空", "喜欢太空"),
    ("火箭", "喜欢火箭"),
    ("画画", "喜欢画画"),
    ("积木", "喜欢积木"),
    ("赛车", "喜欢赛车"),
    ("火车", "喜欢火车"),
    ("小狗", "喜欢小狗"),
    ("小猫", "喜欢小猫"),
    ("公园", "喜欢去公园"),
)

_STORY_ONLY_USER = re.compile(
    r"^(再)?(讲|说|读)(一)?(个|则|段)?(故事|童话|绘本).{0,8}$"
)
# Named people only: a short name attached to the role word. Skip plurals like 同学们.
_NAMED_ROLE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:[\u4e00-\u9fff]{1,3}(?:老师|同学|班主任|校长)(?!们)"
    r"|(?:老师|同学|班主任|校长)(?!们)[\u4e00-\u9fff]{1,3})"
)
_HEALTH_TERMS: Final[tuple[str, ...]] = (
    "生病",
    "发烧",
    "感冒",
    "医院",
    "吃药",
    "打针",
    "咳嗽",
)
#: Same window as guardian daily recap: this many consecutive chars is a quote.
VERBATIM_WINDOW: Final = 10

SESSION_SUMMARY_SYSTEM_PROMPT: Final = (
    "你是严谨的中文助手。只输出家长视角的会话摘要正文，不要 Markdown、标题或引号。"
)

SESSION_SUMMARY_USER_PROMPT_PREFIX: Final = """
请用家长的视角，用 1 到 3 句中文概括孩子和陪伴机器人这一次聊天。
硬性规则：
- 用“孩子”做主语或隐含主语，例如“聊了公园里的黄色小狗”。
- 不得引用或复述孩子的原话，不得截取原文片段。
- 不要出现老师、同学、班主任、校长的姓名，也不要写具体健康细节（生病、医院、吃药等）。
- 只写聊天里出现过的话题，不编造。
- 全文不超过 200 字。

孩子说过的话（只供你理解，勿照搬）：
""".strip()


@dataclass(frozen=True, slots=True)
class SessionTurn:
    role: Role
    text: str


@dataclass(frozen=True, slots=True)
class SessionRecapDraft:
    summary: DialogueSummary | None
    archive_facts: tuple[ArchiveFact, ...]
    skipped_reason: str | None
    user_turn_count: int


def user_turns(turns: Sequence[SessionTurn]) -> list[SessionTurn]:
    return [turn for turn in turns if turn.role == "user" and turn.text.strip()]


def is_story_playback_only(turns: Sequence[SessionTurn]) -> bool:
    """True when every user turn is only a story-request phrase."""

    users = user_turns(turns)
    if not users:
        return True
    return all(_STORY_ONLY_USER.match(turn.text.strip()) is not None for turn in users)


def skip_session_recap(turns: Sequence[SessionTurn]) -> str | None:
    """Return a skip reason, or None when a recap should be produced."""

    users = user_turns(turns)
    if len(users) < 2:
        return "fewer_than_two_turns"
    if is_story_playback_only(turns):
        return "story_playback_only"
    return None


def session_summary_prompt(turns: Sequence[SessionTurn]) -> str:
    """User prompt for the summary model. Includes child lines only for context."""

    lines: list[str] = []
    for turn in user_turns(turns)[:40]:
        lines.append("- " + " ".join(turn.text.split())[:200])
    return SESSION_SUMMARY_USER_PROMPT_PREFIX + "\n" + "\n".join(lines)


def _compact(text: str) -> str:
    return "".join(text.split())


def copied_transcript_fragments(summary: str, turns: Sequence[SessionTurn]) -> list[str]:
    """Runs of the child's words that the summary repeats in VERBATIM_WINDOW pieces."""

    haystack = _compact(summary)
    fragments: list[str] = []
    for turn in user_turns(turns):
        words = _compact(turn.text)
        if len(words) < VERBATIM_WINDOW:
            continue
        covered = [False] * len(words)
        for start in range(len(words) - VERBATIM_WINDOW + 1):
            if words[start : start + VERBATIM_WINDOW] in haystack:
                covered[start : start + VERBATIM_WINDOW] = [True] * VERBATIM_WINDOW
        run = ""
        for char, hit in zip(words, covered, strict=True):
            if hit:
                run += char
            elif run:
                fragments.append(run)
                run = ""
        if run:
            fragments.append(run)
    return fragments


def redact_name_and_health_terms(text: str) -> str:
    """Strip named roles and health terms without chopping unrelated words."""

    cleaned = _NAMED_ROLE_RE.sub("", text)
    for term in _HEALTH_TERMS:
        cleaned = cleaned.replace(term, "")
    cleaned = re.sub(r"[，,]{2,}", "，", cleaned)
    return cleaned.strip("，。、；; \t\n")


def accept_parent_summary(text: str, turns: Sequence[SessionTurn]) -> str | None:
    """Return a cleaned summary, or None when it still leaks raw speech or PII."""

    original = " ".join(text.split())
    if not original or quotes_speech(original):
        return None
    cleaned = redact_name_and_health_terms(original)
    if len(_compact(cleaned)) < 6:
        return None
    if any(term in cleaned for term in _HEALTH_TERMS):
        return None
    if _NAMED_ROLE_RE.search(cleaned) is not None:
        return None
    if copied_transcript_fragments(cleaned, turns):
        return None
    if len(cleaned) > SUMMARY_MAX_CHARS:
        cleaned = cleaned[: SUMMARY_MAX_CHARS - 1] + "…"
    return cleaned


def extract_interest_facts(subject_id: str, turns: Sequence[SessionTurn]) -> tuple[ArchiveFact, ...]:
    """Whitelist preference hits from the session transcript (e.g. 恐龙 → 喜欢恐龙)."""

    blob = "".join(turn.text for turn in user_turns(turns))
    found: list[ArchiveFact] = []
    seen: set[str] = set()
    for needle, label in INTEREST_TOPICS:
        if needle not in blob or label in seen:
            continue
        if forbidden_topic(label) or quotes_speech(label):
            continue
        try:
            fact = ArchiveFact(subject_id=subject_id, field="preference", text=label)
        except ValueError:
            continue
        found.append(fact)
        seen.add(label)
    return tuple(found)


def draft_session_recap(
    *,
    subject_id: str,
    session_id: str,
    source_event_id: str,
    turns: Sequence[SessionTurn],
    archive_consent: bool,
    summary_text: str | None,
    skipped_reason: str | None = None,
) -> SessionRecapDraft:
    """Build the session-end draft. ``summary_text`` must already be LLM-checked."""

    users = user_turns(turns)
    skip = skip_session_recap(turns)
    if skip is not None:
        return SessionRecapDraft(
            summary=None,
            archive_facts=(),
            skipped_reason=skip,
            user_turn_count=len(users),
        )
    if skipped_reason is not None:
        return SessionRecapDraft(
            summary=None,
            archive_facts=(),
            skipped_reason=skipped_reason,
            user_turn_count=len(users),
        )
    if not summary_text:
        return SessionRecapDraft(
            summary=None,
            archive_facts=(),
            skipped_reason="summary_model_failed",
            user_turn_count=len(users),
        )
    accepted = accept_parent_summary(summary_text, turns)
    if accepted is None:
        return SessionRecapDraft(
            summary=None,
            archive_facts=(),
            skipped_reason="summary_rejected",
            user_turn_count=len(users),
        )
    try:
        summary = DialogueSummary(
            subject_id=subject_id,
            text=accepted,
            source_event_id=source_event_id,
            session_id=session_id,
        )
    except ValueError:
        return SessionRecapDraft(
            summary=None,
            archive_facts=(),
            skipped_reason="invalid_summary",
            user_turn_count=len(users),
        )
    facts = extract_interest_facts(subject_id, turns) if archive_consent else ()
    return SessionRecapDraft(
        summary=summary,
        archive_facts=facts,
        skipped_reason=None,
        user_turn_count=len(users),
    )


__all__ = [
    "INTEREST_TOPICS",
    "SESSION_SUMMARY_SYSTEM_PROMPT",
    "VERBATIM_WINDOW",
    "SessionRecapDraft",
    "SessionTurn",
    "accept_parent_summary",
    "copied_transcript_fragments",
    "draft_session_recap",
    "extract_interest_facts",
    "is_story_playback_only",
    "redact_name_and_health_terms",
    "session_summary_prompt",
    "skip_session_recap",
    "user_turns",
]
