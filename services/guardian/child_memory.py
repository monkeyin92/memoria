"""Two-layer child memory boundary (Scheme A).

Durable child audio and verbatim transcripts never cross this boundary.
Dialogue summaries and whitelist archive facts are the only retained text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

SubjectClass = Literal["minor", "unknown", "adult"]
ChildMemoryKind = Literal["dialogue_summary", "child_archive"]
ArchiveField = Literal["appellation", "preference", "pet_name"]

KIND_DIALOGUE_SUMMARY: Final = "dialogue_summary"
KIND_CHILD_ARCHIVE: Final = "child_archive"
ARCHIVE_FIELDS: Final[frozenset[str]] = frozenset(
    {"appellation", "preference", "pet_name"}
)
FORBIDDEN_ARCHIVE_TOPICS: Final[tuple[str, ...]] = (
    "健康",
    "医院",
    "生病",
    "住址",
    "地址",
    "学校",
    "班级",
    "老师",
    "家庭矛盾",
    "离婚",
    "吵架",
)
SUMMARY_MAX_CHARS: Final = 200
ARCHIVE_MAX_CHARS: Final = 40
SUMMARY_TTL: Final = timedelta(days=30)
ARCHIVE_IDLE_TTL: Final = timedelta(days=180)
TRANSCRIPT_KEYS: Final[tuple[str, ...]] = (
    "text",
    "transcript",
    "user_text",
    "assistant_text",
)

_QUOTE_MARKS = ('"', "“", "”", "「", "」", "『", "』", "'")


class ChildAudioForbidden(PermissionError):
    """Child or unknown subjects may not persist conversation audio."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"child audio forbidden for session {session_id}")
        self.session_id = session_id


@dataclass(frozen=True, slots=True)
class TurnFacts:
    session_id: str
    turn_id: int
    subject_id: str | None
    subject_class: SubjectClass
    pcm: bytes
    transcript: str


@dataclass(frozen=True, slots=True)
class DialogueSummary:
    subject_id: str
    text: str
    source_event_id: str
    session_id: str = ""

    def __post_init__(self) -> None:
        text = self.text.strip()
        if not text or len(text) > SUMMARY_MAX_CHARS:
            raise ValueError("dialogue summary must be 1..200 characters")
        if quotes_speech(text):
            raise ValueError("dialogue summary must not quote speech")
        object.__setattr__(self, "text", text)


@dataclass(frozen=True, slots=True)
class ArchiveFact:
    subject_id: str
    field: ArchiveField
    text: str

    def __post_init__(self) -> None:
        text = self.text.strip()
        if self.field not in ARCHIVE_FIELDS:
            raise ValueError("archive field not on whitelist")
        if not text or len(text) > ARCHIVE_MAX_CHARS:
            raise ValueError("archive fact must be one short sentence")
        if not one_sentence(text):
            raise ValueError("archive fact must be at most one sentence")
        if quotes_speech(text):
            raise ValueError("archive fact must not quote speech")
        if forbidden_topic(text):
            raise ValueError("archive fact topic is forbidden")
        object.__setattr__(self, "text", text)


def is_ephemeral_subject(subject_class: SubjectClass) -> bool:
    return subject_class in {"minor", "unknown"}


def accept_persistent_audio(turn: TurnFacts) -> None:
    if is_ephemeral_subject(turn.subject_class):
        raise ChildAudioForbidden(turn.session_id)


def durable_event(turn: TurnFacts, *, base: Mapping[str, object] | None = None) -> dict[str, object]:
    """Metadata-only event for minor/unknown; adult keeps transcript fields."""

    event = dict(base or {})
    event.setdefault("session_id", turn.session_id)
    event.setdefault("turn_id", turn.turn_id)
    if turn.subject_id is not None:
        event["subject_id"] = turn.subject_id
    event["subject_class"] = turn.subject_class
    if is_ephemeral_subject(turn.subject_class):
        for key in TRANSCRIPT_KEYS:
            event.pop(key, None)
        event["memory_retention"] = "ephemeral_only"
        return event
    event["text"] = turn.transcript
    return event


def strip_transcript_fields(payload: MutableMapping[str, object]) -> dict[str, object]:
    cleaned = dict(payload)
    for key in TRANSCRIPT_KEYS:
        cleaned.pop(key, None)
    return cleaned


def quotes_speech(text: str) -> bool:
    return any(mark in text for mark in _QUOTE_MARKS)


def one_sentence(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    # One clause: no second terminator after content begins.
    body = stripped.rstrip("。.!！?？")
    return not re.search(r"[。.!！?？]", body)


def forbidden_topic(text: str) -> bool:
    return any(token in text for token in FORBIDDEN_ARCHIVE_TOPICS)


def summary_payload(
    summary: DialogueSummary, *, created_at: datetime | None = None
) -> dict[str, object]:
    stamped = created_at or datetime.now(UTC)
    return {
        "kind": KIND_DIALOGUE_SUMMARY,
        "text": summary.text,
        "session_id": summary.session_id,
        "source_event_id": summary.source_event_id,
        "created_at": stamped.isoformat(),
    }


def archive_payload(
    fact: ArchiveFact,
    *,
    created_at: datetime | None = None,
    last_mentioned_at: datetime | None = None,
) -> dict[str, object]:
    stamped = created_at or datetime.now(UTC)
    mentioned = last_mentioned_at or stamped
    return {
        "kind": KIND_CHILD_ARCHIVE,
        "field": fact.field,
        "text": fact.text,
        "created_at": stamped.isoformat(),
        "last_mentioned_at": mentioned.isoformat(),
    }


def parse_child_kind(payload: Mapping[str, object]) -> ChildMemoryKind | None:
    kind = payload.get("kind")
    if kind in {KIND_DIALOGUE_SUMMARY, KIND_CHILD_ARCHIVE}:
        return kind  # type: ignore[return-value]
    return None


def summary_expires_at(created_at: datetime) -> datetime:
    return created_at.astimezone(UTC) + SUMMARY_TTL


def archive_idle_deadline(last_active_at: datetime) -> datetime:
    return last_active_at.astimezone(UTC) + ARCHIVE_IDLE_TTL


__all__ = [
    "ARCHIVE_FIELDS",
    "ARCHIVE_IDLE_TTL",
    "ARCHIVE_MAX_CHARS",
    "ArchiveFact",
    "ArchiveField",
    "ChildAudioForbidden",
    "ChildMemoryKind",
    "DialogueSummary",
    "KIND_CHILD_ARCHIVE",
    "KIND_DIALOGUE_SUMMARY",
    "SUMMARY_MAX_CHARS",
    "SUMMARY_TTL",
    "TRANSCRIPT_KEYS",
    "TurnFacts",
    "accept_persistent_audio",
    "archive_idle_deadline",
    "archive_payload",
    "durable_event",
    "forbidden_topic",
    "is_ephemeral_subject",
    "one_sentence",
    "parse_child_kind",
    "quotes_speech",
    "strip_transcript_fields",
    "summary_expires_at",
    "summary_payload",
]
