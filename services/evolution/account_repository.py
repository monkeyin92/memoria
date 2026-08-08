"""Account export and erasure for owner-private evolution material.

Evolution evidence deliberately lives outside the archive database in local
development and can use a dedicated PostgreSQL role in production.  It still
belongs to the account lifecycle: this module follows foreign-key ownership
from owner-private signals/candidates to their validation, activation and
lifecycle evidence without ever selecting or deleting global-redacted rows.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg

from services.archive.object_store import ObjectRef
from services.evolution.store import EvolutionStore, _connect_evolution_sqlite

EVOLUTION_ACCOUNT_TABLES = (
    "evolution_learning_signals",
    "evolution_candidates",
    "evolution_validations",
    "evolution_activation_events",
    "evolution_lifecycle_events",
    "evolution_sleep_signal_receipts",
)

_SQLITE_JSON_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "evolution_learning_signals": ("payload_json",),
    "evolution_candidates": (
        "payload_json",
        "source_signal_ids_json",
        "regression_guards_json",
    ),
    "evolution_validations": ("payload_json",),
}

_POSTGRES_EXPORT_QUERIES: Mapping[str, str] = {
    "evolution_learning_signals": """
        SELECT signal_id, task_family, scope, account_id,
               payload::text AS payload_json, payload_hash, created_at::text AS created_at
        FROM evolution_learning_signals
        WHERE scope = 'owner_private' AND account_id = $1
        ORDER BY created_at, signal_id
    """,
    "evolution_candidates": """
        SELECT candidate_id, task_family, kind, scope, account_id, version, status,
               payload::text AS payload_json, artifact_hash,
               source_signal_ids::text AS source_signal_ids_json, expected_behavior,
               regression_guards::text AS regression_guards_json, risk, trusted_root_sha256,
               reason, created_at::text AS created_at, updated_at::text AS updated_at
        FROM evolution_candidates
        WHERE scope = 'owner_private' AND account_id = $1
        ORDER BY created_at, candidate_id
    """,
    "evolution_validations": """
        SELECT validation.validation_id, validation.candidate_id,
               validation.payload::text AS payload_json, validation.passed,
               validation.created_at::text AS created_at
        FROM evolution_validations validation
        JOIN evolution_candidates candidate ON candidate.candidate_id = validation.candidate_id
        WHERE candidate.scope = 'owner_private' AND candidate.account_id = $1
        ORDER BY validation.created_at, validation.validation_id
    """,
    "evolution_activation_events": """
        SELECT activation_id::text AS activation_id, activation.candidate_id, task_id,
               activated, adhered, outcome_passed, evidence_event_id,
               activation.created_at::text AS created_at
        FROM evolution_activation_events activation
        JOIN evolution_candidates candidate ON candidate.candidate_id = activation.candidate_id
        WHERE candidate.scope = 'owner_private' AND candidate.account_id = $1
        ORDER BY activation.created_at, activation_id
    """,
    "evolution_lifecycle_events": """
        SELECT lifecycle.sequence, lifecycle.event_id::text AS event_id, lifecycle.candidate_id,
               event_type, from_status, to_status, reason, related_candidate_id,
               lifecycle.created_at::text AS created_at
        FROM evolution_lifecycle_events lifecycle
        JOIN evolution_candidates candidate ON candidate.candidate_id = lifecycle.candidate_id
        WHERE candidate.scope = 'owner_private' AND candidate.account_id = $1
        ORDER BY lifecycle.sequence
    """,
    "evolution_sleep_signal_receipts": """
        SELECT receipt.signal_id, receipt.processed_at::text AS processed_at
        FROM evolution_sleep_signal_receipts receipt
        JOIN evolution_learning_signals signal ON signal.signal_id = receipt.signal_id
        WHERE signal.scope = 'owner_private' AND signal.account_id = $1
        ORDER BY receipt.processed_at, receipt.signal_id
    """,
}

_POSTGRES_JSON_COLUMNS: Mapping[str, tuple[str, ...]] = {
    table: _SQLITE_JSON_COLUMNS.get(table, ()) for table in EVOLUTION_ACCOUNT_TABLES
}

_SQLITE_EXPORT_QUERIES: Mapping[str, str] = {
    "evolution_learning_signals": """
        SELECT * FROM evolution_learning_signals
        WHERE scope = 'owner_private' AND account_id = ?
        ORDER BY created_at, signal_id
    """,
    "evolution_candidates": """
        SELECT * FROM evolution_candidates
        WHERE scope = 'owner_private' AND account_id = ?
        ORDER BY created_at, candidate_id
    """,
    "evolution_validations": """
        SELECT validation.* FROM evolution_validations validation
        JOIN evolution_candidates candidate ON candidate.candidate_id = validation.candidate_id
        WHERE candidate.scope = 'owner_private' AND candidate.account_id = ?
        ORDER BY validation.created_at, validation.validation_id
    """,
    "evolution_activation_events": """
        SELECT activation.* FROM evolution_activation_events activation
        JOIN evolution_candidates candidate ON candidate.candidate_id = activation.candidate_id
        WHERE candidate.scope = 'owner_private' AND candidate.account_id = ?
        ORDER BY activation.created_at, activation.activation_id
    """,
    "evolution_lifecycle_events": """
        SELECT lifecycle.* FROM evolution_lifecycle_events lifecycle
        JOIN evolution_candidates candidate ON candidate.candidate_id = lifecycle.candidate_id
        WHERE candidate.scope = 'owner_private' AND candidate.account_id = ?
        ORDER BY lifecycle.sequence
    """,
    "evolution_sleep_signal_receipts": """
        SELECT receipt.* FROM evolution_sleep_signal_receipts receipt
        JOIN evolution_learning_signals signal ON signal.signal_id = receipt.signal_id
        WHERE signal.scope = 'owner_private' AND signal.account_id = ?
        ORDER BY receipt.processed_at, receipt.signal_id
    """,
}

_SQLITE_DELETE_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "evolution_lifecycle_events",
        """
        DELETE FROM evolution_lifecycle_events
        WHERE candidate_id IN (
            SELECT candidate_id FROM evolution_candidates
            WHERE scope = 'owner_private' AND account_id = ?
        )
        """,
    ),
    (
        "evolution_activation_events",
        """
        DELETE FROM evolution_activation_events
        WHERE candidate_id IN (
            SELECT candidate_id FROM evolution_candidates
            WHERE scope = 'owner_private' AND account_id = ?
        )
        """,
    ),
    (
        "evolution_validations",
        """
        DELETE FROM evolution_validations
        WHERE candidate_id IN (
            SELECT candidate_id FROM evolution_candidates
            WHERE scope = 'owner_private' AND account_id = ?
        )
        """,
    ),
    (
        "evolution_sleep_signal_receipts",
        """
        DELETE FROM evolution_sleep_signal_receipts
        WHERE signal_id IN (
            SELECT signal_id FROM evolution_learning_signals
            WHERE scope = 'owner_private' AND account_id = ?
        )
        """,
    ),
    (
        "evolution_candidates",
        """
        DELETE FROM evolution_candidates
        WHERE scope = 'owner_private' AND account_id = ?
        """,
    ),
    (
        "evolution_learning_signals",
        """
        DELETE FROM evolution_learning_signals
        WHERE scope = 'owner_private' AND account_id = ?
        """,
    ),
)

_POSTGRES_DELETE_QUERIES = tuple(
    (table, query.replace("?", "$1")) for table, query in _SQLITE_DELETE_QUERIES
)

_SQLITE_COUNT_QUERIES: tuple[tuple[str, str], ...] = tuple(
    (table, query.replace("DELETE FROM", "SELECT count(*) FROM", 1))
    for table, query in _SQLITE_DELETE_QUERIES
)
_POSTGRES_COUNT_QUERIES = tuple(
    (table, query.replace("DELETE FROM", "SELECT count(*) FROM", 1))
    for table, query in _POSTGRES_DELETE_QUERIES
)


class SqliteEvolutionAccountRepository:
    """Account lifecycle adapter for a local evolution SQLite database."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser().resolve()
        # Keep standalone governance jobs safe to start before the control API.
        EvolutionStore(self._path)

    async def export_account(self, account_id: str) -> dict[str, list[dict[str, Any]]]:
        return await asyncio.to_thread(self._export_account, account_id)

    async def object_references(self, account_id: str) -> tuple[ObjectRef, ...]:
        del account_id
        return ()

    async def mark_account_deleting(
        self,
        account_id: str,
        *,
        started_at: str,
    ) -> None:
        await asyncio.to_thread(
            EvolutionStore(self._path).mark_account_deleting,
            account_id,
            started_at=datetime.fromisoformat(started_at),
        )

    async def is_account_deleting(self, account_id: str) -> bool:
        return await asyncio.to_thread(EvolutionStore(self._path).is_account_deleting, account_id)

    async def delete_account(self, account_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self._delete_account, account_id)

    async def remaining_account_rows(self, account_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self._remaining_account_rows, account_id)

    def _export_account(self, account_id: str) -> dict[str, list[dict[str, Any]]]:
        if not self._path.is_file():
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        with _connect_evolution_sqlite(self._path) as connection:
            for table in EVOLUTION_ACCOUNT_TABLES:
                if not _sqlite_table_exists(connection, table):
                    continue
                rows = connection.execute(_SQLITE_EXPORT_QUERIES[table], (account_id,)).fetchall()
                values = [
                    _portable_sqlite_row(row, _SQLITE_JSON_COLUMNS.get(table, ())) for row in rows
                ]
                values.sort(key=_canonical)
                result[table] = values
        return result

    def _delete_account(self, account_id: str) -> dict[str, int]:
        if not self._path.is_file():
            return {}
        with _connect_evolution_sqlite(
            self._path,
            account_deletion_authorized=True,
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            counts: dict[str, int] = {}
            for table, query in _SQLITE_DELETE_QUERIES:
                if not _sqlite_table_exists(connection, table):
                    continue
                cursor = connection.execute(query, (account_id,))
                counts[table] = max(0, cursor.rowcount)
            return counts

    def _remaining_account_rows(self, account_id: str) -> dict[str, int]:
        if not self._path.is_file():
            return {}
        with _connect_evolution_sqlite(self._path) as connection:
            counts: dict[str, int] = {}
            for table, query in _SQLITE_COUNT_QUERIES:
                if not _sqlite_table_exists(connection, table):
                    continue
                count = int(connection.execute(query, (account_id,)).fetchone()[0])
                if count:
                    counts[table] = count
            return counts


class PostgresEvolutionAccountRepository:
    """Account lifecycle adapter for the privileged evolution PostgreSQL store."""

    def __init__(self, dsn: str) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("evolution account governance DSN must use PostgreSQL")
        self._dsn = dsn

    async def export_account(self, account_id: str) -> dict[str, list[dict[str, Any]]]:
        connection = await asyncpg.connect(self._dsn, command_timeout=15)
        try:
            async with connection.transaction():
                present = await _postgres_tables_present(connection)
                result: dict[str, list[dict[str, Any]]] = {}
                for table in EVOLUTION_ACCOUNT_TABLES:
                    if table not in present:
                        continue
                    rows = await connection.fetch(_POSTGRES_EXPORT_QUERIES[table], account_id)
                    values = [
                        _portable_postgres_record(row, _POSTGRES_JSON_COLUMNS.get(table, ()))
                        for row in rows
                    ]
                    values.sort(key=_canonical)
                    result[table] = values
                return result
        finally:
            await connection.close()

    async def object_references(self, account_id: str) -> tuple[ObjectRef, ...]:
        del account_id
        return ()

    async def mark_account_deleting(
        self,
        account_id: str,
        *,
        started_at: str,
    ) -> None:
        timestamp = datetime.fromisoformat(started_at)
        if timestamp.tzinfo is None:
            raise ValueError("account deletion fence timestamp must be timezone-aware")
        connection = await asyncpg.connect(self._dsn, command_timeout=15)
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    account_id,
                )
                await connection.execute(
                    """
                    INSERT INTO evolution_account_deletion_fences(account_id, started_at)
                    VALUES ($1, $2)
                    ON CONFLICT(account_id) DO NOTHING
                    """,
                    account_id,
                    timestamp,
                )
        finally:
            await connection.close()

    async def is_account_deleting(self, account_id: str) -> bool:
        connection = await asyncpg.connect(self._dsn, command_timeout=15)
        try:
            return bool(
                await connection.fetchval(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM evolution_account_deletion_fences
                        WHERE account_id = $1
                    )
                    """,
                    account_id,
                )
            )
        finally:
            await connection.close()

    async def delete_account(self, account_id: str) -> dict[str, int]:
        connection = await asyncpg.connect(self._dsn, command_timeout=15)
        try:
            async with connection.transaction():
                present = await _postgres_tables_present(connection)
                await connection.execute(
                    "SELECT set_config('app.evolution_account_deletion', '1', true)"
                )
                counts: dict[str, int] = {}
                for table, query in _POSTGRES_DELETE_QUERIES:
                    if table not in present:
                        continue
                    status = await connection.execute(query, account_id)
                    counts[table] = _deleted_count(status)
                return counts
        finally:
            await connection.close()

    async def remaining_account_rows(self, account_id: str) -> dict[str, int]:
        connection = await asyncpg.connect(self._dsn, command_timeout=15)
        try:
            async with connection.transaction():
                present = await _postgres_tables_present(connection)
                counts: dict[str, int] = {}
                for table, query in _POSTGRES_COUNT_QUERIES:
                    if table not in present:
                        continue
                    count = int(await connection.fetchval(query, account_id))
                    if count:
                        counts[table] = count
                return counts
        finally:
            await connection.close()


def _sqlite_table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


async def _postgres_tables_present(connection: asyncpg.Connection) -> frozenset[str]:
    rows = await connection.fetch(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_name = ANY($1::text[])
        """,
        list(EVOLUTION_ACCOUNT_TABLES),
    )
    return frozenset(str(row["table_name"]) for row in rows)


def _portable_sqlite_row(
    row: sqlite3.Row,
    json_columns: tuple[str, ...],
) -> dict[str, Any]:
    result = dict(row)
    for column in json_columns:
        value = result.get(column)
        if isinstance(value, str):
            try:
                result[column.removesuffix("_json")] = json.loads(value)
                del result[column]
            except json.JSONDecodeError:
                pass
    return result


def _portable_postgres_record(
    row: asyncpg.Record,
    json_columns: tuple[str, ...],
) -> dict[str, Any]:
    result = {key: _portable_value(value) for key, value in row.items()}
    for column in json_columns:
        value = result.get(column)
        if isinstance(value, str):
            try:
                result[column.removesuffix("_json")] = _portable_value(json.loads(value))
                del result[column]
            except json.JSONDecodeError:
                pass
    return result


def _portable_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"binary": "omitted"}
    if isinstance(value, Mapping):
        return {str(key): _portable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_portable_value(item) for item in value]
    return value


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _deleted_count(status: str) -> int:
    try:
        return int(status.rsplit(" ", 1)[-1])
    except ValueError as exc:  # pragma: no cover - asyncpg DML contract
        raise RuntimeError(f"unexpected PostgreSQL deletion status: {status}") from exc


__all__ = [
    "EVOLUTION_ACCOUNT_TABLES",
    "PostgresEvolutionAccountRepository",
    "SqliteEvolutionAccountRepository",
]
