"""Subject-keyed session recall (P0-04 vertical slice).

Within-session owner utterances are indexed by ``(subject_id, session_id,
session_epoch)`` so a subject switch, consent revoke, or erasure cannot leak
another person's recent turns.  Production durable memory still flows through
``MemoryScopeService.capture``; this module holds the ephemeral recall buffer
only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol


class SubjectSessionRecallDenied(PermissionError):
    """The actor may not read or write this subject's session recall."""


@dataclass(frozen=True, slots=True)
class SubjectSessionRecallKey:
    subject_id: str
    session_id: str
    session_epoch: int


@dataclass(frozen=True, slots=True)
class SubjectSessionRecallEntry:
    turn_id: int
    text: str


class SubjectSessionRecallStore(Protocol):
    async def append(
        self,
        *,
        subject_id: str,
        session_id: str,
        session_epoch: int,
        turn_id: int,
        text: str,
        actor_subject_id: str,
    ) -> None: ...

    async def list_recent(
        self,
        *,
        subject_id: str,
        session_id: str,
        session_epoch: int,
        actor_subject_id: str,
        limit: int,
    ) -> tuple[str, ...]: ...

    async def erase_subject(self, *, subject_id: str) -> int: ...


def _require_actor(actor_subject_id: str, subject_id: str) -> None:
    if not actor_subject_id.strip() or actor_subject_id != subject_id:
        raise SubjectSessionRecallDenied("session recall is subject-scoped")


def _normalize_text(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("session recall text must be non-empty")
    return cleaned[:240]


class InMemorySubjectSessionRecallStore:
    """Dev/test adapter; mirrors fail-closed subject isolation semantics."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[SubjectSessionRecallKey, list[SubjectSessionRecallEntry]] = {}

    async def append(
        self,
        *,
        subject_id: str,
        session_id: str,
        session_epoch: int,
        turn_id: int,
        text: str,
        actor_subject_id: str,
    ) -> None:
        _require_actor(actor_subject_id, subject_id)
        if session_epoch < 1 or turn_id < 0:
            raise ValueError("invalid session recall fence")
        key = SubjectSessionRecallKey(subject_id, session_id, session_epoch)
        entry = SubjectSessionRecallEntry(turn_id=turn_id, text=_normalize_text(text))
        with self._lock:
            bucket = self._entries.setdefault(key, [])
            bucket.append(entry)
            bucket.sort(key=lambda item: item.turn_id)

    async def list_recent(
        self,
        *,
        subject_id: str,
        session_id: str,
        session_epoch: int,
        actor_subject_id: str,
        limit: int,
    ) -> tuple[str, ...]:
        _require_actor(actor_subject_id, subject_id)
        if limit <= 0:
            return ()
        key = SubjectSessionRecallKey(subject_id, session_id, session_epoch)
        with self._lock:
            bucket = list(self._entries.get(key, ()))
        selected = bucket[-limit:]
        return tuple(item.text for item in selected)

    async def erase_subject(self, *, subject_id: str) -> int:
        if not subject_id.strip():
            return 0
        removed = 0
        with self._lock:
            for key in tuple(self._entries):
                if key.subject_id == subject_id:
                    removed += len(self._entries.pop(key))
        return removed
