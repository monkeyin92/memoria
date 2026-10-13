"""Session-end child recap: one summary plus optional whitelist archive facts.

Built from the in-process transcript at session end, then the transcript is
discarded.  Never persists raw utterances.  Product rules (authoritative):
generate once per session; skip short or story-only sessions; parent voice;
redact teacher/classmate names and health; archive only on separate consent.
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
_NAME_HEALTH_REDACTIONS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"(老师|同学|班主任|校长)[\u4e00-\u9fff]{0,3}"), ""),
    (re.compile(r"[\u4e00-\u9fff]{1,3}(老师|同学)"), ""),
    (re.compile(r"(生病|发烧|感冒|医院|吃药|打针|咳嗽)"), ""),
)


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


def redact_parent_summary_text(text: str) -> str:
    cleaned = text
    for pattern, replacement in _NAME_HEALTH_REDACTIONS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned.strip("，。、；; ")


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


def draft_parent_summary_text(turns: Sequence[SessionTurn]) -> str:
    """One to three parent-perspective sentences from the session (no quotes)."""

    users = user_turns(turns)
    topics: list[str] = []
    for needle, _label in INTEREST_TOPICS:
        if any(needle in turn.text for turn in users) and needle not in topics:
            topics.append(needle)
    if not topics:
        for turn in users:
            snippet = redact_parent_summary_text(turn.text)
            if snippet and snippet not in topics:
                topics.append(snippet[:12])
            if len(topics) >= 3:
                break
    topics = [redact_parent_summary_text(topic) for topic in topics if topic]
    topics = [topic for topic in topics if topic][:3]
    if not topics:
        return "孩子和机器人聊了一会儿"
    if len(topics) == 1:
        text = f"聊了{topics[0]}"
    elif len(topics) == 2:
        text = f"聊了{topics[0]}和{topics[1]}"
    else:
        text = f"聊了{topics[0]}、{topics[1]}和{topics[2]}"
    text = redact_parent_summary_text(text)
    if quotes_speech(text):
        text = text.replace('"', "").replace("“", "").replace("”", "")
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[: SUMMARY_MAX_CHARS - 1] + "…"
    return text or "孩子和机器人聊了一会儿"


def draft_session_recap(
    *,
    subject_id: str,
    session_id: str,
    source_event_id: str,
    turns: Sequence[SessionTurn],
    archive_consent: bool,
) -> SessionRecapDraft:
    """Build the session-end draft and leave transcript ownership to the caller to discard."""

    users = user_turns(turns)
    skip = skip_session_recap(turns)
    if skip is not None:
        return SessionRecapDraft(
            summary=None,
            archive_facts=(),
            skipped_reason=skip,
            user_turn_count=len(users),
        )
    summary_text = draft_parent_summary_text(turns)
    try:
        summary = DialogueSummary(
            subject_id=subject_id,
            text=summary_text,
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
    "SessionRecapDraft",
    "SessionTurn",
    "draft_parent_summary_text",
    "draft_session_recap",
    "extract_interest_facts",
    "is_story_playback_only",
    "redact_parent_summary_text",
    "skip_session_recap",
    "user_turns",
]
