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

The identity step clears or erases the person row through the identity
authority's controlled port (issue #283). Whether the row is cleared (a device
still serves them) or erased is the identity side's decision, so the saga only
asks for it on the path that requested it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
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

_LEDGER_SNAPSHOT_VERSION: Final = 1

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
    "chat_usage_purged",
    "identity_redacted",
    "verified_empty",
    "completed",
)
_RANK: Final = {step: index for index, step in enumerate(_STEPS)}


class SubjectDeletionIncompleteError(RuntimeError):
    """A store still holds the subject's rows; the deletion resumes on retry."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot_text(value: object, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"snapshot {field} must be a bounded non-empty string")
    return value


def _snapshot_json_object(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"snapshot {field} must be a JSON object string")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"snapshot {field} must be a JSON object string") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"snapshot {field} must be a JSON object string")
    return value


def _snapshot_row(value: object) -> dict[str, Any]:
    """Validate one exported row; the snapshot is written by trusted code."""

    if not isinstance(value, dict):
        raise ValueError("snapshot rows must be objects")
    row: dict[str, Any] = {
        "account_id": _snapshot_text(value.get("account_id"), "account_id", maximum=128),
        "subject_id": _snapshot_text(value.get("subject_id"), "subject_id", maximum=128),
        "request_id": _snapshot_text(value.get("request_id"), "request_id", maximum=128),
        "lineage_json": _snapshot_json_object(value.get("lineage_json", "{}"), "lineage_json"),
        "progress_json": _snapshot_json_object(
            value.get("progress_json", "{}"), "progress_json"
        ),
        "started_at": _snapshot_text(value.get("started_at"), "started_at", maximum=64),
        "updated_at": _snapshot_text(value.get("updated_at"), "updated_at", maximum=64),
    }
    redact = value.get("redact_identity")
    if isinstance(redact, bool):
        row["redact_identity"] = redact
    elif isinstance(redact, int) and redact in (0, 1):
        row["redact_identity"] = bool(redact)
    else:
        raise ValueError("snapshot redact_identity must be a boolean")
    completed = value.get("completed_at")
    row["completed_at"] = (
        _snapshot_text(completed, "completed_at", maximum=64)
        if completed is not None
        else row["updated_at"]
    )
    return row


class DbConnection(Protocol):
    def execute(self, sql: str, parameters: Sequence[Any] = ..., /) -> Any: ...


class ControlStore(Protocol):
    """The control store the ledger shares when it runs on PostgreSQL."""

    def initialize(self) -> None: ...

    def connection(self) -> AbstractContextManager[DbConnection]: ...


class SubjectDeletionLedger:
    """Durable checkpoint and fence for subject deletions (control SQLite)."""

    @classmethod
    def beside(cls, store: ControlStore, sqlite_path: str | Path) -> SubjectDeletionLedger:
        """Live wherever the control store lives: its PostgreSQL schema or file."""

        return cls(sqlite_path, store=store if getattr(store, "is_postgres", False) else None)

    def __init__(self, path: str | Path, *, store: ControlStore | None = None) -> None:
        # With a PostgreSQL control store the ledger lives in its schema;
        # otherwise it shares the control SQLite file.
        self._path = Path(path).expanduser().resolve()
        self._store = store

    @contextmanager
    def _connect(self) -> Iterator[DbConnection]:
        if self._store is not None:
            with self._store.connection() as connection:
                yield connection
            return
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._store is not None:
            self._store.initialize()  # the table is part of the store's schema
            return
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS account_deletions (
                    user_id_hash TEXT PRIMARY KEY,
                    user_id TEXT UNIQUE,
                    request_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK (status IN ('deleting', 'completed')),
                    step TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    last_error TEXT,
                    deleted_counts_json TEXT NOT NULL DEFAULT '{}'
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
                    INSERT INTO subject_deletions (
                        account_id_hash, subject_id_hash, account_id, subject_id,
                        request_id, status, step, redact_identity, started_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'deleting', 'started', ?, ?, ?)
                    ON CONFLICT (account_id_hash, subject_id_hash) DO UPDATE SET
                        account_id = excluded.account_id,
                        subject_id = excluded.subject_id,
                        request_id = excluded.request_id,
                        status = 'deleting', step = 'started',
                        redact_identity = excluded.redact_identity,
                        lineage_json = '{}', progress_json = '{}', last_error = NULL,
                        started_at = excluded.started_at,
                        updated_at = excluded.updated_at, completed_at = NULL
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
                    completed_at = CASE WHEN ? = 1 THEN ? ELSE completed_at END
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
        if self._store is None and not self._path.is_file():
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

    def completed(self, *, limit: int = 1000) -> tuple[tuple[str, str, bool], ...]:
        """Completed deletions, oldest first, for re-application after a restore."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT account_id, subject_id, redact_identity FROM subject_deletions "
                "WHERE status = 'completed' ORDER BY completed_at, updated_at LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            (str(row["account_id"]), str(row["subject_id"]), bool(row["redact_identity"]))
            for row in rows
        )

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

    def completed_rows(self, *, limit: int = 100_000) -> tuple[dict[str, Any], ...]:
        """Completed deletions with the identifiers a restore cannot roll back."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT account_id, subject_id, request_id, status, step, "
                "redact_identity, lineage_json, progress_json, started_at, "
                "updated_at, completed_at "
                "FROM subject_deletions WHERE status = 'completed' "
                "ORDER BY completed_at, updated_at LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def completed_account_rows(self, *, limit: int = 100_000) -> tuple[dict[str, Any], ...]:
        """Completed account deletions for post-restore replay."""

        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT user_id_hash, user_id, request_id, status, step, "
                    "started_at, updated_at, completed_at, deleted_counts_json "
                    "FROM account_deletions WHERE status = 'completed' AND user_id IS NOT NULL "
                    "ORDER BY completed_at, updated_at LIMIT ?",
                    (limit,),
                ).fetchall()
            return tuple(dict(row) for row in rows)
        except Exception as exc:
            if "account_deletions" in str(exc) and (
                "no such table" in str(exc).lower() or "does not exist" in str(exc).lower()
            ):
                return ()
            raise

    def export_completed(self, path: str | Path, *, limit: int = 100_000) -> int:
        """Write completed deletions to a root-only 0600 snapshot file.

        The file contains subject and account identifiers, so callers create it
        outside the repository, keep it 0600, and delete it after the replay.
        """

        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.completed_rows(limit=limit)
        account_rows = self.completed_account_rows(limit=limit)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": _LEDGER_SNAPSHOT_VERSION,
                        "rows": rows,
                        "account_rows": account_rows,
                    },
                    handle,
                    sort_keys=True,
                )
                handle.write("\n")
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return len(rows) + len(account_rows)

    def merge_completed(self, path: str | Path) -> dict[str, int]:
        """Add snapshot rows missing from the restored ledger; never overwrite.

        A row already present under the same account/subject key (or the same
        request id) is skipped, so merging the same snapshot twice is a no-op.
        """

        source = Path(path).expanduser().resolve()
        document = json.loads(source.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or document.get("version") != _LEDGER_SNAPSHOT_VERSION
        ):
            raise ValueError("not a subject-deletion ledger snapshot")
        raw_rows = document.get("rows")
        if not isinstance(raw_rows, list):
            raise ValueError("snapshot rows must be a list")

        merged = existing = 0
        account_merged = account_existing = 0
        with self._connect() as connection:
            for raw in raw_rows:
                row = _snapshot_row(raw)
                cursor = connection.execute(
                    """
                    INSERT INTO subject_deletions (
                        account_id_hash, subject_id_hash, account_id, subject_id,
                        request_id, status, step, redact_identity, lineage_json,
                        progress_json, started_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, 'completed', 'completed', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        _hash(row["account_id"]),
                        _hash(row["subject_id"]),
                        row["account_id"],
                        row["subject_id"],
                        row["request_id"],
                        int(row["redact_identity"]),
                        row["lineage_json"],
                        row["progress_json"],
                        row["started_at"],
                        row["updated_at"],
                        row["completed_at"],
                    ),
                )
                if cursor.rowcount:
                    merged += 1
                else:
                    existing += 1
            account_raw_rows = document.get("account_rows")
            if isinstance(account_raw_rows, list):
                for raw in account_raw_rows:
                    if not isinstance(raw, dict):
                        continue
                    user_id = str(raw.get("user_id") or "")
                    if not user_id:
                        continue
                    cursor = connection.execute(
                        """
                        INSERT INTO account_deletions (
                            user_id_hash, user_id, request_id, status, step,
                            started_at, updated_at, completed_at, deleted_counts_json
                        ) VALUES (?, ?, ?, 'completed', 'completed', ?, ?, ?, ?)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            _hash(user_id),
                            user_id,
                            str(raw["request_id"]),
                            str(raw.get("started_at") or ""),
                            str(raw.get("updated_at") or ""),
                            str(raw.get("completed_at") or ""),
                            str(raw.get("deleted_counts_json") or "{}"),
                        ),
                    )
                    if cursor.rowcount:
                        account_merged += 1
                    else:
                        account_existing += 1
        result = {"merged": merged, "existing": existing}
        if account_merged or account_existing:
            result.update({"account_merged": account_merged, "account_existing": account_existing})
        return result


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
type ChatUsagePurger = Callable[[str], Awaitable[int]]
type IdentityRedactor = Callable[[str, str], Awaitable[None]]


class PersonaForgetter(Protocol):
    """The persona learns per person, so a subject's persona goes with them."""

    async def forget_subject(self, *, account_id: str, subject_id: str) -> int: ...

    async def remaining_subject_rows(
        self, *, account_id: str, subject_id: str
    ) -> dict[str, int]: ...


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
        purge_chat_usage: ChatUsagePurger | None = None,
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
        self._purge_chat_usage = purge_chat_usage
        self._redact_identity = redact_identity
        self._persona = persona
        self._lock = asyncio.Lock()

    @property
    def ledger(self) -> SubjectDeletionLedger:
        return self._ledger

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

    async def replay_completed_deletions(self, *, limit: int = 1000) -> dict[str, int]:
        """Re-apply every completed deletion, e.g. after a database restore.

        The ledger lives in the control database while subject data is in the
        archive/persona/memory stores, so restoring those stores from a backup
        brings erased rows back while the ledger still says ``completed``. The
        same backup also rolls the ledger back, which is why the restore
        procedure exports the online ledger first and merges it afterwards.
        Each replay recaptures lineage from the restored evidence and runs the
        whole saga again, which is idempotent for data that is already gone.
        Run it after a restore and before traffic and projection rebuilds
        resume.
        """

        replayed = incomplete = 0
        for account_id, subject_id, redact in await asyncio.to_thread(
            self._ledger.completed, limit=limit
        ):
            try:
                await self.delete_subject(
                    SubjectScope(account_id=account_id, subject_id=subject_id),
                    redact_identity=redact,
                )
                # #326 deletes vectors with the recall and the deletion itself;
                # this covers a vector restored from a backup older than that,
                # which can cite evidence outside the recaptured lineage.
                # Delete whatever the subject's evidence still reaches.
                await self._archive.delete_subject_vectors(
                    account_id=account_id, subject_id=subject_id
                )
            except Exception as exc:
                incomplete += 1
                logger.warning(
                    "subject deletion replay remains incomplete subject_hash=%s reason=%s",
                    _hash(subject_id)[:16],
                    type(exc).__name__,
                )
            else:
                replayed += 1
        return {"replayed": replayed, "incomplete": incomplete}

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
                # The owner call also collects claims about the child whose
                # evidence is the parent's; it must run even when the child has
                # no evidence of their own left.
                for account_id, events, subject_id in (
                    (scope.account_id, lineage.owner_events, scope.subject_id),
                    (scope.subject_id, lineage.subject_account_events, None),
                ):
                    if not events and subject_id is None:
                        continue
                    counts = await self._archive.delete_events(
                        account_id=account_id, event_ids=events, subject_id=subject_id
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

            if due("chat_usage_purged"):
                progress["control.daily_chat_usage"] = (
                    await self._purge_chat_usage(scope.subject_id)
                    if self._purge_chat_usage is not None
                    else 0
                )
                await checkpoint("chat_usage_purged")

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
        if self._persona is not None:
            persona = await self._persona.remaining_subject_rows(
                account_id=scope.account_id, subject_id=scope.subject_id
            )
            remaining.update({f"persona.{key}": value for key, value in persona.items()})
        if self._memory_scope is not None:
            memory = await self._memory_scope.remaining_subject_rows(subject_id=scope.subject_id)
            remaining.update({f"memory_scope.{key}": value for key, value in memory.items()})
        return {key: value for key, value in remaining.items() if value}
