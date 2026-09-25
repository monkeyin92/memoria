"""Subject erasure for the append-only memory scope store (governance).

``SubjectMemoryScopePort`` for one bound subject (a child or elder with no
account of their own).  Erasure is the ONLY path that removes rows from this
store, and each adapter opens it for exactly one call:

* PostgreSQL: the ``memory_subject_erase`` / ``memory_subject_remaining``
  SECURITY DEFINER ports owned by ``memoria_memory_owner``, executable only
  by the ``memoria_memory_maintenance`` login (no table privilege).  The
  append-only triggers honour a DELETE only inside that function and only
  for rows naming the subject.
* SQLite (dev/test): the delete triggers consult
  ``memory_subject_erase_permits``, a function registered on the store's own
  connection that allows exactly the rows one erase planned inside its
  ``BEGIN IMMEDIATE`` transaction.  Any other connection has no such
  function, so its DELETE fails closed.

What is removed (both adapters): records naming the subject as subject,
owner, author or co-subject, or promoted from one of their proposals (a
shared record is deleted whole - its jointly confirmed text cannot be split
per person), their status events, proposals naming the subject (deleted, not
scrubbed: a scrubbed pending proposal could still be promoted) with every
vote on them, and the subject's own votes.  Outbox and audit rows keep their
ids but lose their payload; pending outbox rows are marked processed so a
stub is never dispatched.  Copies already streamed to Redis cannot be
recalled and are counted as ``memory_outbox_dispatched``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit

import asyncpg

__all__ = [
    "MAINTENANCE_ROLE",
    "SQLITE_ERASE_PERMITS_FUNCTION",
    "PostgresSubjectMemoryScope",
    "SqliteSubjectErasePlan",
    "apply_sqlite_subject_erasure",
    "plan_sqlite_subject_erasure",
    "sqlite_subject_remaining",
]

MAINTENANCE_ROLE = "memoria_memory_maintenance"
SQLITE_ERASE_PERMITS_FUNCTION = "memory_subject_erase_permits"
_ERASED_AUDIT_ACTION = "memory.subject.erased"


def _require_subject(subject_id: str) -> None:
    if not subject_id.strip() or len(subject_id) > 128:
        raise ValueError("subject_id must be a bounded non-empty id")


def _names(value: object, subject_id: str) -> bool:
    """True when ``subject_id`` appears as any string value in decoded JSON."""
    if isinstance(value, str):
        return value == subject_id
    if isinstance(value, dict):
        return any(_names(item, subject_id) for item in value.values())
    if isinstance(value, list):
        return any(_names(item, subject_id) for item in value)
    return False


def _nonzero(counts: dict[str, int]) -> dict[str, int]:
    return {table: count for table, count in counts.items() if count}


# -- SQLite (dev/test) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SqliteSubjectErasePlan:
    """The exact rows one erase may delete; the delete triggers check it."""

    subject_id: str
    record_ids: frozenset[str]
    proposal_ids: frozenset[str]

    def permits(self, kind: str, key: str | None, subject_id: str | None) -> bool:
        if kind == "record":
            return key in self.record_ids
        if kind == "vote":
            return subject_id == self.subject_id or key in self.proposal_ids
        return False


_SQLITE_SUBJECT_PROPOSALS = """
SELECT proposal_id FROM memory_shared_proposals p
WHERE p.proposer_subject_id = :s
   OR EXISTS (SELECT 1 FROM json_each(p.co_subject_ids) WHERE value = :s)
"""

_SQLITE_SUBJECT_RECORDS = f"""
SELECT record_id FROM memory_records r
WHERE r.subject_id = :s OR r.resource_owner_id = :s
   OR r.created_by_actor_id = :s
   OR EXISTS (SELECT 1 FROM json_each(r.co_subject_ids) WHERE value = :s)
   OR r.shared_proposal_id IN ({_SQLITE_SUBJECT_PROPOSALS})
"""


def plan_sqlite_subject_erasure(
    connection: sqlite3.Connection, subject_id: str
) -> SqliteSubjectErasePlan:
    _require_subject(subject_id)
    params = {"s": subject_id}
    return SqliteSubjectErasePlan(
        subject_id=subject_id,
        record_ids=frozenset(
            str(row[0]) for row in connection.execute(_SQLITE_SUBJECT_RECORDS, params)
        ),
        proposal_ids=frozenset(
            str(row[0]) for row in connection.execute(_SQLITE_SUBJECT_PROPOSALS, params)
        ),
    )


def _in_clause(values: frozenset[str]) -> tuple[str, tuple[str, ...]]:
    ordered = tuple(sorted(values))
    return "(" + ", ".join("?" for _ in ordered) + ")", ordered


def apply_sqlite_subject_erasure(
    connection: sqlite3.Connection, plan: SqliteSubjectErasePlan
) -> dict[str, int]:
    """Delete/scrub inside the caller's transaction while ``plan`` is active."""
    records_in, record_ids = _in_clause(plan.record_ids)
    proposals_in, proposal_ids = _in_clause(plan.proposal_ids)
    counts = {
        "memory_status_events": connection.execute(
            f"DELETE FROM memory_status_events WHERE record_id IN {records_in}",  # noqa: S608
            record_ids,
        ).rowcount,
        "memory_records": connection.execute(
            f"DELETE FROM memory_records WHERE record_id IN {records_in}",  # noqa: S608
            record_ids,
        ).rowcount,
        "memory_shared_votes": connection.execute(
            "DELETE FROM memory_shared_votes WHERE subject_id = ?"
            f" OR proposal_id IN {proposals_in}",  # noqa: S608
            (plan.subject_id, *proposal_ids),
        ).rowcount,
        "memory_shared_proposals": connection.execute(
            f"DELETE FROM memory_shared_proposals WHERE proposal_id IN {proposals_in}",  # noqa: S608
            proposal_ids,
        ).rowcount,
    }

    def _linked(payload: object) -> bool:
        if _names(payload, plan.subject_id):
            return True
        if not isinstance(payload, dict):
            return False
        return (
            payload.get("record_id") in plan.record_ids
            or payload.get("proposal_id") in plan.proposal_ids
        )

    outbox = dispatched = 0
    for row in connection.execute(
        "SELECT outbox_id, status, payload FROM memory_outbox WHERE payload <> '{}'"
    ).fetchall():
        if not _linked(json.loads(row[2])):
            continue
        connection.execute(
            "UPDATE memory_outbox SET payload = '{}', status = 'processed'"
            " WHERE outbox_id = ?",
            (row[0],),
        )
        outbox += 1
        dispatched += row[1] == "processed"
    audit = 0
    for row in connection.execute(
        "SELECT event_id, actor_subject_id, subject_id, record_id, proposal_id,"
        " payload FROM memory_audit_events WHERE payload <> '{}'"
    ).fetchall():
        if not (
            plan.subject_id in (row[1], row[2])
            or row[3] in plan.record_ids
            or row[4] in plan.proposal_ids
            or _names(json.loads(row[5]), plan.subject_id)
        ):
            continue
        connection.execute(
            "UPDATE memory_audit_events SET payload = '{}' WHERE event_id = ?",
            (row[0],),
        )
        audit += 1
    connection.execute(
        "INSERT OR IGNORE INTO memory_audit_events (event_id, action,"
        " actor_subject_id, subject_id, record_id, proposal_id, payload,"
        " created_at) VALUES (?, ?, 'governance', ?, NULL, NULL, '{}', ?)",
        (
            f"{_ERASED_AUDIT_ACTION}:{plan.subject_id}",
            _ERASED_AUDIT_ACTION,
            plan.subject_id,
            datetime.now(UTC).isoformat(),
        ),
    )
    counts.update(
        memory_outbox=outbox, memory_outbox_dispatched=dispatched, memory_audit_events=audit
    )
    return counts


def sqlite_subject_remaining(
    connection: sqlite3.Connection, subject_id: str
) -> dict[str, int]:
    _require_subject(subject_id)
    params = {"s": subject_id}

    def _count(sql: str) -> int:
        return int(connection.execute(sql, params).fetchone()[0])

    counts = {
        "memory_records": _count(f"SELECT count(*) FROM ({_SQLITE_SUBJECT_RECORDS})"),  # noqa: S608
        "memory_status_events": _count(
            "SELECT count(*) FROM memory_status_events"
            f" WHERE record_id IN ({_SQLITE_SUBJECT_RECORDS})"  # noqa: S608
        ),
        "memory_shared_proposals": _count(
            f"SELECT count(*) FROM ({_SQLITE_SUBJECT_PROPOSALS})"  # noqa: S608
        ),
        "memory_shared_votes": _count(
            "SELECT count(*) FROM memory_shared_votes WHERE subject_id = :s"
            f" OR proposal_id IN ({_SQLITE_SUBJECT_PROPOSALS})"  # noqa: S608
        ),
        "memory_outbox": sum(
            _names(json.loads(row[0]), subject_id)
            for row in connection.execute(
                "SELECT payload FROM memory_outbox WHERE payload <> '{}'"
            )
        ),
        "memory_audit_events": sum(
            subject_id in (row[0], row[1]) or _names(json.loads(row[2]), subject_id)
            for row in connection.execute(
                "SELECT actor_subject_id, subject_id, payload"
                " FROM memory_audit_events WHERE payload <> '{}'"
            )
        ),
    }
    return _nonzero(counts)


# -- PostgreSQL (production) ---------------------------------------------------


def _jsonb_counts(value: object) -> dict[str, int]:
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise RuntimeError("memory subject erasure port returned a non-object")
    return {str(table): int(cast(int, count)) for table, count in decoded.items()}


class PostgresSubjectMemoryScope:
    """``SubjectMemoryScopePort`` over the ``memoria_memory_maintenance`` login.

    The login holds no table privilege: both calls go through the narrow
    SECURITY DEFINER ports, so this adapter cannot read or change any other
    memory row.
    """

    def __init__(self, maintenance_dsn: str) -> None:
        if urlsplit(maintenance_dsn).username != MAINTENANCE_ROLE:
            raise ValueError(
                f"memory subject erasure must connect exactly as {MAINTENANCE_ROLE}"
            )
        self._dsn = maintenance_dsn
        self._pool: asyncpg.Pool | None = None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=2, command_timeout=30
        )
        try:
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    "SELECT current_user AS usr, rolsuper, rolbypassrls"
                    " FROM pg_roles WHERE rolname = current_user"
                )
            if (
                row is None
                or row["usr"] != MAINTENANCE_ROLE
                or row["rolsuper"]
                or row["rolbypassrls"]
            ):
                raise RuntimeError(
                    f"memory subject erasure requires the non-superuser, "
                    f"NOBYPASSRLS {MAINTENANCE_ROLE} login"
                )
        except BaseException:
            await pool.close()
            raise
        self._pool = pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _require_pool(self) -> asyncpg.Pool:
        # Opened on first erase: a rare maintenance path must not make Control
        # startup depend on it.
        if self._pool is None:
            async with self._initialize_lock:
                if self._pool is None:
                    await self.initialize()
        assert self._pool is not None
        return self._pool

    async def erase_subject(self, *, subject_id: str) -> dict[str, int]:
        _require_subject(subject_id)
        async with (await self._require_pool()).acquire() as connection:
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT memory_subject_erase($1)", subject_id
                )
        return _jsonb_counts(raw)

    async def remaining_subject_rows(self, *, subject_id: str) -> dict[str, int]:
        _require_subject(subject_id)
        async with (await self._require_pool()).acquire() as connection:
            raw = await connection.fetchval(
                "SELECT memory_subject_remaining($1)", subject_id
            )
        return _nonzero(_jsonb_counts(raw))
