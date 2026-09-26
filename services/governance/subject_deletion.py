"""Delete one bound subject's data inside their binding owner's account.

A device serves one person (2026-09-25). A child or elder with no account has
their turns stored in the owner's account, attributed by ``subject_id``;
account deletion would erase the parent's own data, so this saga removes
exactly the subject's rows, store by store, and proves the stores are empty.

Every step is checkpointed in ``subject_deletions`` so a crash or a failing
store resumes where it stopped. While a deletion runs, the subject is fenced:
new evidence for them is refused (``is_subject_deleting``). Content-free audit
stays: consent evidence and revoked consents, policy receipts, identity
relationships/bindings, session-runtime profiles.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from services.archive.object_store import ObjectNotFoundError, ObjectStore
from services.governance.subject_ports import (
    SubjectArchivePort,
    SubjectGuardianPort,
    SubjectMemoryScopePort,
    SubjectScope,
)

__all__ = [
    "SubjectDeletionIncompleteError",
    "SubjectDeletionLedger",
    "SubjectDeletionService",
]

logger = logging.getLogger(__name__)

_STEPS: Final[tuple[str, ...]] = (
    "started",
    "sessions_terminated",
    "lineage_captured",
    "objects_deleted",
    "archive_rows_deleted",
    "persona_forgotten",
    "guardian_rows_deleted",
    "memory_scope_erased",
    "corpus_purged",
    "identity_redacted",
    "verified_empty",
    "completed",
)
_RANK: Final = {step: index for index, step in enumerate(_STEPS)}


class SubjectDeletionIncompleteError(RuntimeError):
    """A store still holds the subject's rows; the deletion resumes on retry."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SubjectDeletionLedger:
    """Durable checkpoint and fence for subject deletions (control SQLite)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS subject_deletions (
                    account_id_hash TEXT NOT NULL,
                    subject_id_hash TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK (status IN ('deleting', 'completed')),
                    step TEXT NOT NULL,
                    redact_identity INTEGER NOT NULL CHECK (redact_identity IN (0, 1)),
                    lineage_json TEXT NOT NULL DEFAULT '{}',
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    last_error TEXT,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY (account_id_hash, subject_id_hash)
                )
                """
            )

    def begin(
        self, scope: SubjectScope, *, redact_identity: bool, now: str
    ) -> dict[str, Any]:
        key = (_hash(scope.account_id), _hash(scope.subject_id))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM subject_deletions WHERE account_id_hash = ? AND subject_id_hash = ?",
                key,
            ).fetchone()
            if row is None or row["status"] == "completed":
                # A completed deletion of a still-served subject may run again:
                # the device kept collecting after the last erase.
                connection.execute(
                    """
                    INSERT OR REPLACE INTO subject_deletions (
                        account_id_hash, subject_id_hash, account_id, subject_id,
                        request_id, status, step, redact_identity, started_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'deleting', 'started', ?, ?, ?)
                    """,
                    (
                        *key,
                        scope.account_id,
                        scope.subject_id,
                        str(uuid.uuid4()),
                        int(redact_identity),
                        now,
                        now,
                    ),
                )
            elif redact_identity and not row["redact_identity"]:
                connection.execute(
                    "UPDATE subject_deletions SET redact_identity = 1 "
                    "WHERE account_id_hash = ? AND subject_id_hash = ?",
                    key,
                )
            row = connection.execute(
                "SELECT * FROM subject_deletions WHERE account_id_hash = ? AND subject_id_hash = ?",
                key,
            ).fetchone()
        return dict(row)

    def update(
        self,
        scope: SubjectScope,
        *,
        step: str,
        progress: dict[str, int],
        lineage: dict[str, list[str]],
        now: str,
        last_error: str | None = None,
    ) -> None:
        completed = step == "completed"
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE subject_deletions
                SET step = ?, status = ?, progress_json = ?, lineage_json = ?,
                    last_error = ?, updated_at = ?,
                    completed_at = CASE WHEN ? THEN ? ELSE completed_at END
                WHERE account_id_hash = ? AND subject_id_hash = ?
                """,
                (
                    step,
                    "completed" if completed else "deleting",
                    json.dumps(progress, sort_keys=True),
                    json.dumps(lineage, sort_keys=True),
                    last_error,
                    now,
                    int(completed),
                    now,
                    _hash(scope.account_id),
                    _hash(scope.subject_id),
                ),
            )

    def is_deleting(self, *, account_id: str, subject_id: str) -> bool:
        if not self._path.is_file():
            return False
        with self._connect() as connection:
            try:
                row = connection.execute(
                    "SELECT 1 FROM subject_deletions WHERE account_id_hash = ? "
                    "AND subject_id_hash = ? AND status = 'deleting'",
                    (_hash(account_id), _hash(subject_id)),
                ).fetchone()
            except sqlite3.OperationalError:
                return False
        return row is not None

    def pending(self, *, limit: int = 100) -> tuple[tuple[str, str, bool], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT account_id, subject_id, redact_identity FROM subject_deletions "
                "WHERE status = 'deleting' ORDER BY updated_at LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            (str(row["account_id"]), str(row["subject_id"]), bool(row["redact_identity"]))
            for row in rows
        )


@dataclass(frozen=True, slots=True)
class _Lineage:
    owner_events: tuple[str, ...]
    subject_account_events: tuple[str, ...]

    def to_json(self) -> dict[str, list[str]]:
        return {
            "owner_events": list(self.owner_events),
            "subject_account_events": list(self.subject_account_events),
        }

    @classmethod
    def from_json(cls, value: str) -> _Lineage | None:
        data = json.loads(value or "{}")
        if not data:
            return None
        return cls(
            owner_events=tuple(data.get("owner_events", ())),
            subject_account_events=tuple(data.get("subject_account_events", ())),
        )


type SessionTerminator = Callable[[str], Awaitable[int]]
type CorpusPurger = Callable[[str], Awaitable[int]]
type IdentityRedactor = Callable[[str, str], Awaitable[None]]


class PersonaForgetter(Protocol):
    """The persona learns per person, so a subject's persona goes with them."""

    async def forget_subject(self, *, account_id: str, subject_id: str) -> int: ...


class SubjectDeletionService:
    """Run, resume and verify one subject's deletion."""

    def __init__(
        self,
        *,
        ledger: SubjectDeletionLedger,
        archive: SubjectArchivePort,
        object_store: ObjectStore | None,
        guardian: SubjectGuardianPort | None = None,
        memory_scope: SubjectMemoryScopePort | None = None,
        terminate_sessions: SessionTerminator | None = None,
        purge_corpus: CorpusPurger | None = None,
        redact_identity: IdentityRedactor | None = None,
        persona: PersonaForgetter | None = None,
    ) -> None:
        self._ledger = ledger
        self._archive = archive
        self._objects = object_store
        self._guardian = guardian
        self._memory_scope = memory_scope
        self._terminate_sessions = terminate_sessions
        self._purge_corpus = purge_corpus
        self._redact_identity = redact_identity
        self._persona = persona
        self._lock = asyncio.Lock()

    def is_subject_deleting(self, *, account_id: str, subject_id: str) -> bool:
        return self._ledger.is_deleting(account_id=account_id, subject_id=subject_id)

    async def delete_subject(
        self, scope: SubjectScope, *, redact_identity: bool = False
    ) -> dict[str, Any]:
        async with self._lock:
            return await self._run(scope, redact_identity=redact_identity)

    async def retry_pending_deletions(self, *, limit: int = 100) -> int:
        completed = 0
        for account_id, subject_id, redact in await asyncio.to_thread(
            self._ledger.pending, limit=limit
        ):
            try:
                await self.delete_subject(
                    SubjectScope(account_id=account_id, subject_id=subject_id),
                    redact_identity=redact,
                )
            except Exception as exc:
                logger.warning(
                    "subject deletion retry remains incomplete subject_hash=%s reason=%s",
                    _hash(subject_id)[:16],
                    type(exc).__name__,
                )
            else:
                completed += 1
        return completed

    async def _run(self, scope: SubjectScope, *, redact_identity: bool) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        row = await asyncio.to_thread(
            self._ledger.begin, scope, redact_identity=redact_identity, now=now
        )
        step = str(row["step"])
        redact = bool(row["redact_identity"])
        progress: dict[str, int] = {
            str(key): int(value) for key, value in json.loads(row["progress_json"]).items()
        }
        lineage = _Lineage.from_json(str(row["lineage_json"]))

        async def checkpoint(next_step: str, *, error: str | None = None) -> None:
            nonlocal step
            await asyncio.to_thread(
                self._ledger.update,
                scope,
                step=next_step,
                progress=progress,
                lineage=lineage.to_json() if lineage is not None else {},
                now=datetime.now(UTC).isoformat(),
                last_error=error,
            )
            step = next_step

        def due(target: str) -> bool:
            return _RANK[step] < _RANK[target]

        try:
            if due("sessions_terminated"):
                progress["sessions"] = (
                    await self._terminate_sessions(scope.subject_id)
                    if self._terminate_sessions is not None
                    else 0
                )
                await checkpoint("sessions_terminated")

            if due("lineage_captured") or lineage is None:
                # Captured once and persisted: after the evidence is gone the
                # tutor/derived joins that found it are gone too.
                tutor_events: tuple[str, ...] = ()
                if self._guardian is not None:
                    tutor_events = await self._guardian.subject_tutor_event_ids(
                        account_id=scope.account_id, subject_id=scope.subject_id
                    )
                owner_events = await self._archive.subject_event_ids(scope)
                lineage = _Lineage(
                    owner_events=tuple(sorted({*owner_events, *tutor_events})),
                    subject_account_events=await self._archive.subject_account_event_ids(
                        scope.subject_id
                    ),
                )
                progress["lineage.owner_events"] = len(lineage.owner_events)
                progress["lineage.subject_account_events"] = len(
                    lineage.subject_account_events
                )
                await checkpoint("lineage_captured")
            assert lineage is not None

            if due("objects_deleted"):
                progress["archive.objects"] = await self._delete_objects(scope, lineage)
                await checkpoint("objects_deleted")

            if due("archive_rows_deleted"):
                for account_id, events in (
                    (scope.account_id, lineage.owner_events),
                    (scope.subject_id, lineage.subject_account_events),
                ):
                    if not events:
                        continue
                    counts = await self._archive.delete_events(
                        account_id=account_id, event_ids=events
                    )
                    for table, count in counts.items():
                        progress[f"archive.{table}"] = progress.get(f"archive.{table}", 0) + count
                await checkpoint("archive_rows_deleted")

            if due("persona_forgotten"):
                progress["persona.rows"] = (
                    await self._persona.forget_subject(
                        account_id=scope.account_id, subject_id=scope.subject_id
                    )
                    if self._persona is not None
                    else 0
                )
                await checkpoint("persona_forgotten")

            if due("guardian_rows_deleted"):
                if self._guardian is not None:
                    counts = await self._guardian.delete_subject_rows(
                        account_id=scope.account_id, subject_id=scope.subject_id
                    )
                    progress.update({f"guardian.{key}": value for key, value in counts.items()})
                await checkpoint("guardian_rows_deleted")

            if due("memory_scope_erased"):
                if self._memory_scope is not None:
                    counts = await self._memory_scope.erase_subject(subject_id=scope.subject_id)
                    progress.update(
                        {f"memory_scope.{key}": value for key, value in counts.items()}
                    )
                await checkpoint("memory_scope_erased")

            if due("corpus_purged"):
                progress["guardian.corpus_objects"] = (
                    await self._purge_corpus(scope.subject_id)
                    if self._purge_corpus is not None
                    else 0
                )
                await checkpoint("corpus_purged")

            if due("identity_redacted"):
                if redact and self._redact_identity is not None:
                    await self._redact_identity(scope.subject_id, scope.account_id)
                    progress["identity.redacted"] = 1
                await checkpoint("identity_redacted")

            if due("verified_empty"):
                remaining = await self._remaining(scope, lineage)
                if remaining:
                    raise SubjectDeletionIncompleteError(
                        "subject rows remain: " + ", ".join(sorted(remaining))
                    )
                await checkpoint("verified_empty")

            if due("completed"):
                await checkpoint("completed")
        except Exception as exc:
            await checkpoint(step, error=type(exc).__name__)
            raise
        return {
            "request_id": str(row["request_id"]),
            "status": "completed",
            "deleted_counts": dict(sorted(progress.items())),
            "identity_redacted": bool(progress.get("identity.redacted")),
        }

    async def _delete_objects(self, scope: SubjectScope, lineage: _Lineage) -> int:
        deleted = 0
        for account_id, events in (
            (scope.account_id, lineage.owner_events),
            (scope.subject_id, lineage.subject_account_events),
        ):
            if not events:
                continue
            references = await self._archive.object_references_for(
                account_id=account_id, event_ids=events
            )
            if references and self._objects is None:
                raise SubjectDeletionIncompleteError("archive object store is not configured")
            for reference in references:
                assert self._objects is not None
                await self._objects.delete(reference)
                try:
                    await self._objects.get(reference)
                except (ObjectNotFoundError, FileNotFoundError):
                    deleted += 1
                    continue
                raise SubjectDeletionIncompleteError(
                    "archive object remained readable after deletion"
                )
        return deleted

    async def _remaining(self, scope: SubjectScope, lineage: _Lineage) -> dict[str, int]:
        remaining: dict[str, int] = {}
        owner = await self._archive.remaining_rows_for(
            account_id=scope.account_id,
            event_ids=lineage.owner_events,
            subject_id=scope.subject_id,
        )
        remaining.update({f"archive.{key}": value for key, value in owner.items()})
        own = await self._archive.remaining_rows_for(
            account_id=scope.subject_id,
            event_ids=lineage.subject_account_events,
            subject_id=None,
        )
        remaining.update({f"archive.subject.{key}": value for key, value in own.items()})
        if self._guardian is not None:
            guardian = await self._guardian.remaining_subject_rows(
                account_id=scope.account_id, subject_id=scope.subject_id
            )
            remaining.update({f"guardian.{key}": value for key, value in guardian.items()})
        if self._memory_scope is not None:
            memory = await self._memory_scope.remaining_subject_rows(subject_id=scope.subject_id)
            remaining.update({f"memory_scope.{key}": value for key, value in memory.items()})
        return {key: value for key, value in remaining.items() if value}
