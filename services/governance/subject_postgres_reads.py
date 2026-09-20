"""PostgreSQL read path for the four account→subject migrations (P2-03).

The four seams stay SQLite-only bridges for the development and legacy stores,
but the subject lineage itself is authoritative in PostgreSQL: archive evidence
rows carry ``subject_id`` and the Memory Scope ``memory_records`` /
``memory_status_events`` are keyed by subject.  This module answers the same
operator question against a PostgreSQL DSN, so the read path has a PostgreSQL
side too:

* read-only: every read runs in a read-only transaction, no schema is created
  and no row is written (the Memory Scope read only sets its row-context GUCs,
  exactly like the store does);
* the report shape and the fail-closed reasons match the SQLite read path
  (``no_evidence_source``, ``projection_missing``, ``target_missing``), and a
  subject without rows never falls back to account-keyed rows;
* rows are decoded by the migration's own decoders, so the operator and the
  store cannot disagree on the shape;
* the archive read refuses loudly when the DSN's role cannot see the evidence
  rows at all (FORCE RLS without a bypass): answering "zero rows" for data the
  role cannot see would misread a privacy fence as an empty subject.
"""

from __future__ import annotations

from typing import Any, Final
from urllib.parse import urlsplit

import asyncpg

from services.memory_scope.migrations.legacy_archive import (
    _record_from_target_row,
    _status_event_from_target_row,
)
from services.persona.subject_projection import PROJECTED_TABLES

#: The four P2-03 seams, in lineage order (mirrors ``subject_migrations``).
POSTGRES_MIGRATIONS: Final[tuple[str, ...]] = (
    "durable_subject",
    "digital_self",
    "persona",
    "memory_scope",
)

#: Evidence tables that can carry the durable-subject lineage, in seam order.
_DURABLE_EVIDENCE_TABLES: Final[tuple[str, ...]] = (
    "evidence_events",
    "archive_evidence_events",
)

#: Projected Digital Self tables and their read order (the migration's own).
_DIGITAL_SELF_TABLES: Final[tuple[tuple[str, str], ...]] = (
    ("digital_self_subject_versions", "version_number DESC, version_id"),
    ("digital_self_subject_lifecycle_audit_events", "occurred_at DESC, event_id"),
)

#: Persona projection tables in the product read's order.
_PERSONA_TABLES: Final[tuple[tuple[str, str], ...]] = (
    (PROJECTED_TABLES["persona_versions"], "version_number DESC, version_id"),
    (PROJECTED_TABLES["persona_traits"], "updated_at DESC, trait_id"),
    (PROJECTED_TABLES["speech_style_stats"], "updated_at DESC, scene"),
    (PROJECTED_TABLES["persona_learning_consents"], "granted_at DESC"),
)

_MEMORY_SCOPE: Final[str] = "legacy_archive"
_MEMORY_RECORDS: Final[str] = "memory_records"
_MEMORY_STATUS_EVENTS: Final[str] = "memory_status_events"

_CONNECT_TIMEOUT_S: Final[float] = 15.0


class PostgresSubjectReadError(RuntimeError):
    """The PostgreSQL subject read refused (fail closed)."""


def _target_label(dsn: str) -> str:
    """The DSN without credentials, so a receipt never carries a secret."""

    parsed = urlsplit(dsn)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.path.lstrip("/")
    if not host and not database:
        return "postgresql"
    return f"postgresql://{host}{port}/{database}"


def _require_postgres_dsn(dsn: str) -> str:
    value = dsn.strip()
    parsed = urlsplit(value)
    if not value or parsed.scheme not in {"postgres", "postgresql"} or not parsed.netloc:
        raise PostgresSubjectReadError("a PostgreSQL DSN is required")
    return value


async def _table_exists(connection: asyncpg.Connection, table: str) -> bool:
    return await connection.fetchval("SELECT to_regclass($1)", table) is not None


async def _rls_hides_rows(connection: asyncpg.Connection, tables: tuple[str, ...]) -> bool:
    """True when FORCE RLS is on and this role cannot bypass it.

    The read sets no ``app.*`` context, so such a role would see zero rows for
    every subject -- indistinguishable from an empty subject unless refused.
    """

    forced = False
    for table in tables:
        forced = forced or bool(
            await connection.fetchval(
                "SELECT relforcerowsecurity FROM pg_class WHERE oid = to_regclass($1)",
                table,
            )
        )
    if not forced:
        return False
    bypasses = await connection.fetchval(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    return not bool(bypasses)


def _decode_record(row: Any) -> dict[str, Any]:
    # asyncpg records offer the same keyed access as ``sqlite3.Row``, and the
    # seam's decoder is the single definition of the operator-visible shape.
    return _record_from_target_row(row)


def _decode_status_event(row: Any) -> dict[str, Any]:
    return _status_event_from_target_row(row)


async def _read_durable_subject(
    connection: asyncpg.Connection,
    target: str,
    subject: str,
    limit: int,
    account_id: str | None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "scope": "durable_subject",
        "engine": "postgresql",
        "target": target,
        "subject_id": subject,
        "account_id": account_id,
        "journal_present": False,
        "tables": {},
        "receipts": {"total": 0, "by_outcome": {}, "rows": []},
        "truncated": False,
    }
    present = tuple(
        [
            table
            for table in _DURABLE_EVIDENCE_TABLES
            if await _table_exists(connection, table)
        ]
    )
    if not present:
        return {**report, "reason": "no_evidence_source"}
    if account_id is not None:
        # The archive policies read the account context, so an account-scoped
        # read is the same access model the archive store itself uses: the rows
        # still have to belong to that account (RLS stands).
        await connection.execute(
            "SELECT set_config('app.account_id', $1, true)", account_id
        )
    elif await _rls_hides_rows(connection, present):
        raise PostgresSubjectReadError(
            "this role cannot read subject evidence rows under row-level "
            "security; pass the owning account (--account) or use a DSN whose "
            "role may read the archive store"
        )
    tables: dict[str, Any] = {}
    truncated = False
    for table in present:
        try:
            total = int(
                await connection.fetchval(
                    f"SELECT count(*) FROM {table} WHERE subject_id = $1", subject
                )
            )
            rows = await connection.fetch(
                f"SELECT event_id, account_id FROM {table} WHERE subject_id = $1 "
                "ORDER BY event_id LIMIT $2",
                subject,
                limit,
            )
        except asyncpg.UndefinedColumnError as exc:
            raise PostgresSubjectReadError(
                f"{table} does not carry the durable-subject columns"
            ) from exc
        tables[table] = {
            "count": total,
            "rows": [
                {"event_id": str(row["event_id"]), "account_id": str(row["account_id"])}
                for row in rows
            ],
        }
        truncated = truncated or total > len(rows)
    report["tables"] = tables
    report["truncated"] = truncated
    # The migration journal never exists in PostgreSQL: receipts stay zero and
    # ``journal_present`` stays False, exactly like an unmigrated SQLite store.
    return report


async def _read_projection(
    connection: asyncpg.Connection,
    target: str,
    subject: str,
    limit: int,
    *,
    scope: str,
    tables: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "scope": scope,
        "engine": "postgresql",
        "target": target,
        "subject_id": subject,
        "projection_present": False,
        "tables": {},
        "truncated": False,
    }
    primary = tables[0][0]
    if not await _table_exists(connection, primary):
        # PostgreSQL has no subject-keyed projection yet; the answer must not
        # fall back to the account-keyed tables of the same database.
        return {**report, "reason": "projection_missing"}
    report["projection_present"] = True
    projected: dict[str, Any] = {}
    truncated = False
    for table, order in tables:
        if not await _table_exists(connection, table):
            projected[table] = {"count": 0, "rows": []}
            continue
        total = int(
            await connection.fetchval(
                f"SELECT count(*) FROM {table} WHERE subject_id = $1", subject
            )
        )
        rows = await connection.fetch(
            f"SELECT * FROM {table} WHERE subject_id = $1 ORDER BY {order} LIMIT $2",
            subject,
            limit,
        )
        projected[table] = {"count": total, "rows": [dict(row) for row in rows]}
        truncated = truncated or total > len(rows)
    report["tables"] = projected
    report["truncated"] = truncated
    return report


async def _read_memory_scope(
    connection: asyncpg.Connection, target: str, subject: str, limit: int
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "scope": _MEMORY_SCOPE,
        "engine": "postgresql",
        "target": target,
        "subject_id": subject,
        "target_present": False,
        "records": {"count": 0, "rows": []},
        "status_events": {"count": 0, "rows": []},
        "truncated": False,
    }
    if not await _table_exists(connection, _MEMORY_RECORDS):
        return {**report, "reason": "target_missing"}
    report["target_present"] = True
    # The store's own row context: an API-role DSN then sees exactly what this
    # subject may see, while an operator DSN ignores the policies.
    await connection.execute(
        "SELECT set_config('app.memory.actor_subject_id', $1, true)", subject
    )
    await connection.execute(
        "SELECT set_config('app.memory.subject_id', $1, true)", subject
    )
    total = int(
        await connection.fetchval(
            f"SELECT count(*) FROM {_MEMORY_RECORDS} WHERE scope = $1 AND subject_id = $2",
            _MEMORY_SCOPE,
            subject,
        )
    )
    rows = await connection.fetch(
        f"SELECT * FROM {_MEMORY_RECORDS} WHERE scope = $1 AND subject_id = $2 "
        "ORDER BY created_at DESC, record_id LIMIT $3",
        _MEMORY_SCOPE,
        subject,
        limit,
    )
    records = [_decode_record(row) for row in rows]
    report["records"] = {"count": total, "rows": records}
    truncated = total > len(records)
    record_ids = [str(record["record_id"]) for record in records]
    if record_ids and await _table_exists(connection, _MEMORY_STATUS_EVENTS):
        events_total = int(
            await connection.fetchval(
                f"SELECT count(*) FROM {_MEMORY_STATUS_EVENTS} "
                "WHERE record_id = ANY($1::text[])",
                record_ids,
            )
        )
        events = await connection.fetch(
            f"SELECT * FROM {_MEMORY_STATUS_EVENTS} WHERE record_id = ANY($1::text[]) "
            "ORDER BY created_at, event_id LIMIT $2",
            record_ids,
            limit,
        )
        report["status_events"] = {
            "count": events_total,
            "rows": [_decode_status_event(row) for row in events],
        }
        truncated = truncated or events_total > len(events)
    report["truncated"] = truncated
    return report


async def read_subject(
    migration: str,
    *,
    dsn: str,
    subject_id: str,
    limit: int,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Read one subject's rows from PostgreSQL (read-only, fail closed).

    ``durable_subject`` reads the evidence rows that carry the subject in the
    archive store -- optionally scoped to the owning account, which is the only
    way the archive row-level policies let an application role see them --
    ``memory_scope`` reads the subject's ``legacy_archive`` Memory Scope rows,
    and the two projection reads answer ``projection_missing`` until a
    subject-keyed projection exists in PostgreSQL, never the account-keyed rows
    of the same database.
    """

    subject = subject_id.strip()
    if not subject:
        raise PostgresSubjectReadError("read requires a subject_id")
    if limit < 1:
        raise PostgresSubjectReadError("limit must be positive")
    if migration not in POSTGRES_MIGRATIONS:
        raise PostgresSubjectReadError(f"unknown migration: {migration}")
    scope_account = account_id.strip() if account_id is not None else None
    if scope_account is not None and migration != "durable_subject":
        raise PostgresSubjectReadError(
            "an account scope only applies to the durable_subject read"
        )
    resolved = _require_postgres_dsn(dsn)
    target = _target_label(resolved)
    connection = await asyncpg.connect(resolved, timeout=_CONNECT_TIMEOUT_S)
    try:
        async with connection.transaction(readonly=True):
            if migration == "durable_subject":
                return await _read_durable_subject(
                    connection, target, subject, limit, scope_account
                )
            if migration == "digital_self":
                return await _read_projection(
                    connection,
                    target,
                    subject,
                    limit,
                    scope="digital_self_subject_projection",
                    tables=_DIGITAL_SELF_TABLES,
                )
            if migration == "persona":
                return await _read_projection(
                    connection,
                    target,
                    subject,
                    limit,
                    scope="persona_subject_projection",
                    tables=_PERSONA_TABLES,
                )
            return await _read_memory_scope(connection, target, subject, limit)
    finally:
        await connection.close()


__all__ = [
    "POSTGRES_MIGRATIONS",
    "PostgresSubjectReadError",
    "read_subject",
]
