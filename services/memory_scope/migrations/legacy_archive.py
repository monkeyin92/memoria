"""Migrate account-keyed Archive projections into Memory Scope records.

This is an operator-only, SQLite-only bridge. It is intentionally not called
from application startup and does not use MemoryScopeService: the target rows
are written in one operator transaction together with immutable receipts and
audit evidence.

The migration is fail-closed. account_id is never used as subject_id. A
projection row is migrated only when every source event in its lineage exists,
belongs to the same account, has one non-null subject, and the four Memory
Scope authority fields are explicitly provable.

Runs are incremental. The row receipts written by earlier runs decide which
source rows are already in the target, so a later run writes only the rows the
target does not have yet. Every fence is scoped to those receipts: a
receipt-covered source row that changed, a target record that no longer matches
its receipt, or a status chain that grew after the fact is refused, while
unrelated new Archive rows and unrelated target rows neither block an apply nor
a rollback. The target tables ``memory_records``/``memory_status_events`` are
created with the schema of ``services.memory_scope.sqlite_store``, so a
database first created by this seam stays usable by the real adapter.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

Outcome = Literal["migrated", "quarantined"]

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EVIDENCE_TABLE = "evidence_events"
_CONSENT_TABLE = "consent_grants"
_ASSOCIATION_TABLES = ("episode_evidence", "memory_search_document_sources")
_PROJECTION_TABLES = (
    "memory_claims",
    "memory_search_documents",
    "memory_vector_documents",
    "knowledge_items",
    "life_episodes",
    "person_aliases",
    "person_entities",
    "relationships",
    "timeline_entries",
)
_AUTHORITY_TABLES = (
    "legacy_archive_authority",
    "legacy_archive_authority_mapping",
)
_MEMORY_SCOPE = "legacy_archive"
_TARGET_TABLES = ("memory_records", "memory_status_events")
_SCOPE_VALUES = "('unknown', 'session_ephemeral', 'personal_private', 'guardian_summary', 'family_shared', 'legacy_archive')"
_STATUS_VALUES = "('candidate', 'confirmed', 'disputed', 'revoked')"
_AUTHORITY_FIELDS = (
    "policy_receipt_id",
    "consent_snapshot_id",
    "resource_owner_id",
    "created_by_actor_id",
)
_STATUS_MAP = {
    "candidate": "candidate",
    "confirmed": "confirmed",
    "disputed": "disputed",
    "retracted": "revoked",
}
_MEMORY_TYPES = {"semantic", "episodic", "procedural", "relationship"}
_RETENTION_VALUES = {"session_only", "ttl", "indefinite"}

_MIGRATION_TABLE = "legacy_archive_migrations"
_RECEIPT_TABLE = "legacy_archive_row_receipts"
_QUARANTINE_TABLE = "legacy_archive_quarantine"
_BACKUP_TABLE = "legacy_archive_backups"
_AUDIT_TABLE = "legacy_archive_audit_events"
_TARGET_RECORD_COLUMNS = (
    "record_id",
    "scope",
    "subject_id",
    "resource_owner_id",
    "family_space_id",
    "co_subject_ids",
    "source_evidence_ids",
    "policy_receipt_id",
    "promotion_receipt_id",
    "promotion_fence_context_hash",
    "approval_evidence_refs",
    "consent_snapshot_id",
    "memory_type",
    "confidence",
    "retention",
    "retention_expires_at",
    "payload",
    "created_by_actor_id",
    "created_at",
    "shared_proposal_id",
)
_TARGET_STATUS_COLUMNS = (
    "event_id",
    "record_id",
    "status",
    "reason_code",
    "created_at",
)


class LegacyArchiveMigrationError(RuntimeError):
    """Raised when the legacy Archive cannot be migrated safely."""


@dataclass(frozen=True, slots=True)
class _TableInfo:
    name: str
    columns: tuple[str, ...]
    key_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _RawRow:
    table: str
    columns: tuple[str, ...]
    key_columns: tuple[str, ...]
    row_id: str
    values: dict[str, Any]
    row_digest: str


@dataclass(frozen=True, slots=True)
class _AuthorityResult:
    values: dict[str, str]
    matches: tuple[_RawRow, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ReceiptState:
    """One row receipt of an earlier run plus the status of its migration."""

    migration_id: str
    migration_status: str
    table: str
    row_id: str
    record_id: str
    outcome: str
    before_row_digest: str
    record_digest: str
    status_event_digest: str
    status_chain_digest: str
    source_snapshot_digest: str


@dataclass(frozen=True, slots=True)
class _PlanEntry:
    table: str
    row_id: str
    row_digest: str
    source_snapshot_digest: str
    outcome: Outcome
    record_id: str
    account_id: str | None
    subject_id: str | None
    source_evidence_ids: tuple[str, ...]
    authority: dict[str, str]
    status: str | None
    memory_type: str | None
    reason: str
    details: tuple[str, ...]
    source_row: dict[str, Any]
    lineage_rows: tuple[dict[str, Any], ...]
    evidence_rows: tuple[dict[str, Any], ...]
    consent_rows: tuple[dict[str, Any], ...]
    authority_rows: tuple[dict[str, Any], ...]
    record: dict[str, Any] | None
    status_event: dict[str, Any] | None
    record_digest: str | None
    status_event_digest: str | None


@dataclass(frozen=True, slots=True)
class _RunPlan:
    """The receipt-scoped split of the Archive inventory for one run."""

    rows: tuple[dict[str, Any], ...]
    pending: tuple[_PlanEntry, ...]
    already_migrated: tuple[dict[str, Any], ...]
    covering_migration_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ArchiveState:
    schema: str
    evidence_table: _TableInfo
    evidence_rows: tuple[_RawRow, ...]
    projection_tables: tuple[_TableInfo, ...]
    projection_rows: tuple[_RawRow, ...]
    association_rows: tuple[_RawRow, ...]
    consent_rows: tuple[_RawRow, ...]
    authority_tables: tuple[_TableInfo, ...]
    authority_rows: tuple[_RawRow, ...]
    source_tables: tuple[str, ...]
    source_digest: str


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("migration timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise LegacyArchiveMigrationError(f"unsafe SQLite identifier: {value!r}")
    return f'"{value}"'


def _qualified(schema: str, table: str) -> str:
    return f"{_quote_identifier(schema)}.{_quote_identifier(table)}"


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


def _canonical(value: object) -> str:
    return json.dumps(
        _json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _normalise_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _same_path(first: Path, second: Path) -> bool:
    return first.expanduser().resolve() == second.expanduser().resolve()


def _read_only_uri(path: Path) -> str:
    return f"{path.expanduser().resolve().as_uri()}?mode=ro"


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise LegacyArchiveMigrationError(f"Archive SQLite database does not exist: {path}")
    connection = sqlite3.connect(_read_only_uri(path), uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _connect_target(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, uri=True, isolation_level=None, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _attach_archive(
    connection: sqlite3.Connection, archive_path: Path, target_path: Path
) -> str:
    if _same_path(archive_path, target_path):
        return "main"
    schema = "legacy_archive_source"
    connection.execute(
        f"ATTACH DATABASE ? AS {_quote_identifier(schema)}",
        (_read_only_uri(archive_path),),
    )
    return schema


def _table_exists(connection: sqlite3.Connection, schema: str, table: str) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {_quote_identifier(schema)}.sqlite_master "
        "WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_info(
    connection: sqlite3.Connection,
    schema: str,
    table: str,
    *,
    required: Iterable[str] = (),
) -> _TableInfo:
    rows = connection.execute(
        f"PRAGMA {_quote_identifier(schema)}.table_info({_quote_identifier(table)})"
    ).fetchall()
    if not rows:
        raise LegacyArchiveMigrationError(f"missing SQLite table {schema}.{table}")
    columns = tuple(str(row["name"]) for row in rows)
    missing = sorted(set(required).difference(columns))
    if missing:
        raise LegacyArchiveMigrationError(
            f"{schema}.{table} is missing required columns: {', '.join(missing)}"
        )
    pk_rows = sorted(
        (row for row in rows if int(row["pk"]) > 0), key=lambda row: int(row["pk"])
    )
    key_columns = tuple(str(row["name"]) for row in pk_rows)
    if not key_columns:
        candidates = {
            "memory_claims": ("claim_id",),
            "memory_search_documents": ("document_id",),
            "memory_vector_documents": (
                "item_id",
                "embedding_model",
                "embedding_dimensions",
            ),
            "knowledge_items": ("knowledge_id",),
            "life_episodes": ("episode_id",),
            "person_aliases": ("person_id", "alias", "source_event_id"),
            "person_entities": ("person_id",),
            "relationships": ("relationship_id",),
            "timeline_entries": ("timeline_id",),
            "episode_evidence": ("episode_id", "source_event_id"),
            "memory_search_document_sources": ("document_id", "source_event_id"),
        }.get(table, ())
        if candidates and set(candidates).issubset(columns):
            key_columns = candidates
    if not key_columns:
        raise LegacyArchiveMigrationError(
            f"{schema}.{table} has no stable primary/compound key; rowid is not allowed"
        )
    return _TableInfo(table, columns, key_columns)


def _row_id(values: Mapping[str, Any], key_columns: tuple[str, ...]) -> str:
    if len(key_columns) == 1:
        value = _normalise_text(values.get(key_columns[0]))
        if value is None:
            return "<null>"
        return value
    return _canonical(
        [{"column": column, "value": _json_safe(values.get(column))} for column in key_columns]
    )


def _row_digest(table: str, columns: tuple[str, ...], values: Mapping[str, Any]) -> str:
    return _sha256(
        {
            "table": table,
            "columns": list(columns),
            "values": [
                {"name": column, "value": _json_safe(values.get(column))}
                for column in columns
            ],
        }
    )


def _read_rows(
    connection: sqlite3.Connection, schema: str, info: _TableInfo
) -> tuple[_RawRow, ...]:
    rows = connection.execute(
        f"SELECT * FROM {_qualified(schema, info.name)}"
    ).fetchall()
    result: list[_RawRow] = []
    seen: set[str] = set()
    for row in rows:
        values = {column: row[column] for column in info.columns}
        if any(_normalise_text(values.get(key)) is None for key in info.key_columns):
            raise LegacyArchiveMigrationError(
                f"{schema}.{info.name} has a missing stable key; rowid is not allowed"
            )
        row_id = _row_id(values, info.key_columns)
        if row_id in seen:
            raise LegacyArchiveMigrationError(
                f"{schema}.{info.name} has duplicate stable key {row_id!r}"
            )
        seen.add(row_id)
        result.append(
            _RawRow(
                table=info.name,
                columns=info.columns,
                key_columns=info.key_columns,
                row_id=row_id,
                values=values,
                row_digest=_row_digest(info.name, info.columns, values),
            )
        )
    return tuple(sorted(result, key=lambda item: item.row_id))


def _table_digest(table: _TableInfo, rows: Iterable[_RawRow]) -> dict[str, Any]:
    return {
        "table": table.name,
        "columns": list(table.columns),
        "key_columns": list(table.key_columns),
        "rows": [
            {"source_row_id": row.row_id, "row_digest": row.row_digest}
            for row in sorted(rows, key=lambda item: item.row_id)
        ],
    }


def _state_digest(
    tables: Iterable[_TableInfo], rows_by_table: Mapping[str, tuple[_RawRow, ...]]
) -> str:
    return _sha256(
        [
            _table_digest(table, rows_by_table.get(table.name, ()))
            for table in sorted(tables, key=lambda item: item.name)
        ]
    )


def _read_archive(connection: sqlite3.Connection, schema: str) -> _ArchiveState:
    if not _table_exists(connection, schema, _EVIDENCE_TABLE):
        raise LegacyArchiveMigrationError(
            f"{schema}.{_EVIDENCE_TABLE} is required for lineage migration"
        )
    evidence_table = _table_info(
        connection,
        schema,
        _EVIDENCE_TABLE,
        required=("event_id", "account_id", "subject_id"),
    )
    projection_tables = tuple(
        _table_info(connection, schema, table)
        for table in sorted(_PROJECTION_TABLES)
        if _table_exists(connection, schema, table)
    )
    association_tables = tuple(
        _table_info(
            connection,
            schema,
            table,
            required=(
                "source_event_id",
                "account_id",
                "episode_id" if table == "episode_evidence" else "document_id",
            ),
        )
        for table in _ASSOCIATION_TABLES
        if _table_exists(connection, schema, table)
    )
    consent_table = (
        _table_info(connection, schema, _CONSENT_TABLE)
        if _table_exists(connection, schema, _CONSENT_TABLE)
        else None
    )
    authority_tables = tuple(
        _table_info(connection, schema, table)
        for table in _AUTHORITY_TABLES
        if _table_exists(connection, schema, table)
    )

    evidence_rows = _read_rows(connection, schema, evidence_table)
    projection_rows = tuple(
        row
        for table in projection_tables
        for row in _read_rows(connection, schema, table)
    )
    association_rows = tuple(
        row
        for table in association_tables
        for row in _read_rows(connection, schema, table)
    )
    consent_rows = _read_rows(connection, schema, consent_table) if consent_table else ()
    authority_rows = tuple(
        row
        for table in authority_tables
        for row in _read_rows(connection, schema, table)
    )
    all_tables = [evidence_table, *projection_tables, *association_tables]
    if consent_table is not None:
        all_tables.append(consent_table)
    all_tables.extend(authority_tables)
    rows_by_table: dict[str, tuple[_RawRow, ...]] = {
        evidence_table.name: evidence_rows,
        **{
            table.name: tuple(row for row in projection_rows if row.table == table.name)
            for table in projection_tables
        },
        **{
            table.name: tuple(row for row in association_rows if row.table == table.name)
            for table in association_tables
        },
        **({consent_table.name: consent_rows} if consent_table is not None else {}),
        **{
            table.name: tuple(row for row in authority_rows if row.table == table.name)
            for table in authority_tables
        },
    }
    return _ArchiveState(
        schema=schema,
        evidence_table=evidence_table,
        evidence_rows=evidence_rows,
        projection_tables=projection_tables,
        projection_rows=projection_rows,
        association_rows=association_rows,
        consent_rows=consent_rows,
        authority_tables=authority_tables,
        authority_rows=authority_rows,
        source_tables=tuple(sorted(table.name for table in all_tables)),
        source_digest=_state_digest(all_tables, rows_by_table),
    )


def _records_by_table(state: _ArchiveState, table: str) -> tuple[_RawRow, ...]:
    if table == _EVIDENCE_TABLE:
        return state.evidence_rows
    if table == _CONSENT_TABLE:
        return state.consent_rows
    if table in _AUTHORITY_TABLES:
        return tuple(row for row in state.authority_rows if row.table == table)
    return tuple(row for row in state.association_rows if row.table == table) + tuple(
        row for row in state.projection_rows if row.table == table
    )


def _value_rows(rows: Iterable[_RawRow]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "table": row.table,
            "source_row_id": row.row_id,
            **{column: _json_safe(value) for column, value in row.values.items()},
        }
        for row in rows
    )


@dataclass(frozen=True, slots=True)
class _LineageResult:
    account_id: str | None
    subject_id: str | None
    source_evidence_ids: tuple[str, ...]
    lineage_rows: tuple[_RawRow, ...]
    evidence_rows: tuple[_RawRow, ...]
    consent_rows: tuple[_RawRow, ...]
    authority_rows: tuple[_RawRow, ...]
    reasons: tuple[str, ...]


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _row_matches(row: _RawRow, **expected: str) -> bool:
    for column, value in expected.items():
        if _normalise_text(row.values.get(column)) != value:
            return False
    return True


def _rows_matching(
    state: _ArchiveState, table: str, **expected: str
) -> tuple[_RawRow, ...]:
    return tuple(
        row for row in _records_by_table(state, table) if _row_matches(row, **expected)
    )


def _record_id(table: str, row_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memoria:memory-scope:legacy-archive:{table}:{row_id}",
        )
    )


def _initial_status_event_id(record_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memoria:memory-scope:legacy-archive:status:{record_id}:initial",
        )
    )


def _rollback_status_event_id(migration_id: str, record_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memoria:memory-scope:legacy-archive:rollback:{migration_id}:{record_id}",
        )
    )


def _raw_key(row: _RawRow) -> tuple[str, str]:
    return row.table, row.row_id


def _lineage_rows_for(
    state: _ArchiveState, row: _RawRow
) -> tuple[tuple[_RawRow, ...], tuple[str, ...]]:
    """Follow explicit projection links and return structural warnings."""

    collected: dict[tuple[str, str], _RawRow] = {}
    reasons: list[str] = []

    def add(candidate: _RawRow) -> None:
        collected.setdefault(_raw_key(candidate), candidate)

    def add_source(candidate: _RawRow) -> None:
        add(candidate)
        if "source_event_id" not in candidate.values:
            reasons.append(
                f"source_event_id_column_missing:{candidate.table}:{candidate.row_id}"
            )
        elif _normalise_text(candidate.values.get("source_event_id")) is None:
            reasons.append(f"source_event_id_missing:{candidate.table}:{candidate.row_id}")

    add(row)
    table = row.table

    if table != "memory_vector_documents":
        add_source(row)

    if table == "memory_vector_documents":
        item_id = _normalise_text(row.values.get("item_id"))
        if item_id is None:
            reasons.append("vector_item_id_missing")
        else:
            documents = _rows_matching(state, "memory_search_documents", item_id=item_id)
            if not documents:
                reasons.append("vector_search_document_missing")
            for document in documents:
                add_source(document)
                document_id = _normalise_text(document.values.get("document_id"))
                if document_id is not None:
                    for source in _rows_matching(
                        state, "memory_search_document_sources", document_id=document_id
                    ):
                        add_source(source)
        if "source_event_id" in row.values:
            add_source(row)

    elif table == "memory_search_documents":
        document_id = _normalise_text(row.values.get("document_id"))
        if document_id is not None:
            for source in _rows_matching(
                state, "memory_search_document_sources", document_id=document_id
            ):
                add_source(source)

    elif table == "life_episodes":
        episode_id = _normalise_text(row.values.get("episode_id"))
        if episode_id is not None:
            for evidence in _rows_matching(
                state, "episode_evidence", episode_id=episode_id
            ):
                add_source(evidence)

    elif table == "timeline_entries":
        episode_id = _normalise_text(row.values.get("episode_id"))
        if episode_id is None:
            reasons.append("timeline_episode_id_missing")
        else:
            episodes = _rows_matching(state, "life_episodes", episode_id=episode_id)
            if not episodes:
                reasons.append("timeline_episode_missing")
            for episode in episodes:
                add_source(episode)
            for evidence in _rows_matching(
                state, "episode_evidence", episode_id=episode_id
            ):
                add_source(evidence)

    elif table in {"person_entities", "person_aliases", "relationships"}:
        person_id = _normalise_text(row.values.get("person_id"))
        if person_id is None:
            reasons.append(f"{table}_person_id_missing")
        else:
            entities = _rows_matching(state, "person_entities", person_id=person_id)
            if not entities:
                reasons.append("person_entity_missing")
            for entity in entities:
                add_source(entity)
            for alias in _rows_matching(state, "person_aliases", person_id=person_id):
                add_source(alias)
            for relationship in _rows_matching(
                state, "relationships", person_id=person_id
            ):
                add_source(relationship)

    return (
        tuple(sorted(collected.values(), key=_raw_key)),
        _unique(reasons),
    )


def _authority_rows_for(
    state: _ArchiveState,
    projection: _RawRow,
    record_id: str,
    source_evidence_ids: tuple[str, ...],
) -> tuple[_RawRow, ...]:
    """Select explicit row-scoped authority mappings only."""

    matched: list[_RawRow] = []
    table_keys = ("table_name", "projection_table", "source_table")
    row_keys = ("source_row_id", "projection_row_id", "row_id")
    source_keys = ("source_event_id", "evidence_event_id")
    record_keys = ("record_id", "memory_record_id")

    def first_non_empty(keys: tuple[str, ...]) -> str | None:
        for key in keys:
            if key not in values:
                continue
            value = _normalise_text(values.get(key))
            if value is not None:
                return value
        return None

    for candidate in state.authority_rows:
        values = candidate.values
        table_value = first_non_empty(table_keys)
        row_value = first_non_empty(row_keys)
        source_value = first_non_empty(source_keys)
        record_value = first_non_empty(record_keys)
        if not (
            (table_value is not None and row_value is not None)
            or source_value is not None
            or record_value is not None
        ):
            continue
        if table_value is not None and table_value != projection.table:
            continue
        if row_value is not None and row_value != projection.row_id:
            continue
        if source_value is not None and source_value not in source_evidence_ids:
            continue
        if record_value is not None and record_value != record_id:
            continue
        matched.append(candidate)
    return tuple(sorted(matched, key=_raw_key))


def _linked_consent_rows(
    state: _ArchiveState,
    projection: _RawRow,
    evidence_rows: tuple[_RawRow, ...],
) -> tuple[tuple[_RawRow, ...], tuple[str, ...]]:
    grant_ids = _unique(
        _normalise_text(values.get("consent_grant_id")) or ""
        for values in [projection.values, *(row.values for row in evidence_rows)]
        if "consent_grant_id" in values
    )
    if not grant_ids:
        return (), ()
    if not state.consent_rows:
        return (), tuple(f"consent_row_missing:{grant_id}" for grant_id in grant_ids)
    rows: list[_RawRow] = []
    reasons: list[str] = []
    for grant_id in grant_ids:
        matches = _rows_matching(state, _CONSENT_TABLE, consent_grant_id=grant_id)
        if not matches:
            reasons.append(f"consent_row_missing:{grant_id}")
        rows.extend(matches)
    deduped = {(_raw_key(row)): row for row in rows}
    return tuple(sorted(deduped.values(), key=_raw_key)), _unique(reasons)


def _lineage_for(state: _ArchiveState, projection: _RawRow) -> _LineageResult:
    lineage_rows, structure_reasons = _lineage_rows_for(state, projection)
    reasons = list(structure_reasons)
    account_id = _normalise_text(projection.values.get("account_id"))
    if account_id is None:
        reasons.append("projection_account_id_missing")

    for linked in lineage_rows:
        linked_account = _normalise_text(linked.values.get("account_id"))
        if linked_account is None:
            reasons.append(f"lineage_account_id_missing:{linked.table}:{linked.row_id}")
        elif account_id is not None and linked_account != account_id:
            reasons.append(f"lineage_account_mismatch:{linked.table}:{linked.row_id}")

    source_ids: list[str] = []
    for linked in lineage_rows:
        if "source_event_id" not in linked.values:
            continue
        source_id = _normalise_text(linked.values.get("source_event_id"))
        if source_id is None:
            reasons.append(f"source_event_id_missing:{linked.table}:{linked.row_id}")
        else:
            source_ids.append(source_id)
    source_evidence_ids = tuple(sorted(set(source_ids)))

    evidence_by_id = {
        _normalise_text(event.values.get("event_id")): event
        for event in state.evidence_rows
        if _normalise_text(event.values.get("event_id")) is not None
    }
    evidence_rows: list[_RawRow] = []
    for source_id in source_evidence_ids:
        event = evidence_by_id.get(source_id)
        if event is None:
            reasons.append(f"source_event_missing:{source_id}")
            continue
        evidence_rows.append(event)
        event_account = _normalise_text(event.values.get("account_id"))
        if account_id is not None and event_account != account_id:
            reasons.append(f"source_event_account_mismatch:{source_id}")
        event_subject = _normalise_text(event.values.get("subject_id"))
        if event_subject is None:
            reasons.append(f"source_subject_missing:{source_id}")
        elif len(event_subject) > 128:
            reasons.append(f"source_subject_too_long:{source_id}")

    subjects = {
        subject
        for subject in (
            _normalise_text(event.values.get("subject_id")) for event in evidence_rows
        )
        if subject is not None
    }
    if len(subjects) == 0:
        reasons.append("source_subject_missing")
    elif len(subjects) > 1:
        reasons.append("multiple_source_subjects")
    subject_id = next(iter(subjects)) if len(subjects) == 1 else None

    projection_subject = _normalise_text(projection.values.get("subject_id"))
    if projection_subject is not None and subject_id is not None and projection_subject != subject_id:
        reasons.append("projection_subject_mismatch")
    if projection_subject is not None and subject_id is None:
        reasons.append("projection_subject_without_evidence_subject")

    consent_rows, consent_reasons = _linked_consent_rows(
        state, projection, tuple(evidence_rows)
    )
    reasons.extend(consent_reasons)
    for consent in consent_rows:
        consent_account = _normalise_text(consent.values.get("account_id"))
        if consent_account is None:
            reasons.append(f"consent_account_id_missing:{consent.row_id}")
        elif account_id is not None and consent_account != account_id:
            reasons.append(f"consent_account_mismatch:{consent.row_id}")
    authority_rows = _authority_rows_for(
        state,
        projection,
        _record_id(projection.table, projection.row_id),
        source_evidence_ids,
    )
    return _LineageResult(
        account_id=account_id,
        subject_id=subject_id,
        source_evidence_ids=source_evidence_ids,
        lineage_rows=lineage_rows,
        evidence_rows=tuple(sorted(evidence_rows, key=_raw_key)),
        consent_rows=consent_rows,
        authority_rows=authority_rows,
        reasons=_unique(reasons),
    )


def _candidate_values(
    rows: Iterable[_RawRow], aliases: Iterable[str]
) -> tuple[tuple[str, str], ...]:
    alias_set = tuple(aliases)
    values: list[tuple[str, str]] = []
    for row in rows:
        for alias in alias_set:
            if alias not in row.values:
                continue
            value = _normalise_text(row.values.get(alias))
            if value is not None:
                values.append((alias, value))
    return tuple(values)


def _distinct_values(values: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    return _unique(value for _, value in values)


def _parse_timestamp(value: object, reason: str) -> tuple[str | None, tuple[str, ...]]:
    if value is None:
        return None, (reason,)
    if isinstance(value, datetime):
        timestamp = value
    else:
        raw = _normalise_text(value)
        if raw is None:
            return None, (reason,)
        try:
            timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None, (f"invalid_{reason}",)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return None, (f"naive_{reason}",)
    return timestamp.astimezone(UTC).isoformat(), ()


def _authority_for(
    state: _ArchiveState,
    projection: _RawRow,
    lineage: _LineageResult,
    record_id: str,
) -> _AuthorityResult:
    matches = list(
        _authority_rows_for(
            state,
            projection,
            record_id,
            lineage.source_evidence_ids,
        )
    )
    reasons: list[str] = []

    # A mapping row may identify a separate authority row.  The join is only
    # followed after the mapping has explicitly matched this projection; an
    # account-only authority row is never accepted.
    references: set[str] = set()
    for row in matches:
        for key in (
            "authority_id",
            "authority_record_id",
            "authority_ref_id",
            "mapped_authority_id",
        ):
            value = _normalise_text(row.values.get(key))
            if value is not None:
                references.add(value)
    if references:
        for row in state.authority_rows:
            for key in ("authority_id", "authority_record_id", "mapping_id"):
                value = _normalise_text(row.values.get(key))
                if value in references:
                    matches.append(row)
                    break

    # Keep mismatches in the snapshot and explicitly quarantine the row.
    for row in matches:
        mapped_account = _normalise_text(row.values.get("account_id"))
        if (
            mapped_account is not None
            and lineage.account_id is not None
            and mapped_account != lineage.account_id
        ):
            reasons.append(f"authority_account_mismatch:{row.table}:{row.row_id}")
        mapped_subject = _normalise_text(row.values.get("subject_id"))
        if mapped_subject is not None and mapped_subject != lineage.subject_id:
            reasons.append(f"authority_subject_mismatch:{row.table}:{row.row_id}")
    found_references = {
        value
        for row in matches
        for key in ("authority_id", "authority_record_id", "mapping_id")
        if (value := _normalise_text(row.values.get(key))) is not None
        and any(_normalise_text(row.values.get(field)) for field in _AUTHORITY_FIELDS)
    }
    for reference in sorted(references - found_references):
        reasons.append(f"authority_reference_missing:{reference}")

    source_rows = (projection, *lineage.evidence_rows, *matches)
    values_by_field: dict[str, set[str]] = {field: set() for field in _AUTHORITY_FIELDS}
    for row in source_rows:
        for field in _AUTHORITY_FIELDS:
            if field not in row.values:
                continue
            value = _normalise_text(row.values.get(field))
            if value is not None:
                values_by_field[field].add(value)

    resolved: dict[str, str] = {}
    for field in _AUTHORITY_FIELDS:
        values = values_by_field[field]
        if not values:
            reasons.append(f"authority_missing:{field}")
            continue
        if len(values) > 1:
            reasons.append(
                f"authority_conflict:{field}:{','.join(sorted(values))}"
            )
            continue
        value = next(iter(values))
        if len(value) > 128:
            reasons.append(f"authority_too_long:{field}")
            continue
        resolved[field] = value

    deduped = {(_raw_key(row)): row for row in matches}
    return _AuthorityResult(
        values=resolved,
        matches=tuple(sorted(deduped.values(), key=_raw_key)),
        reasons=_unique(reasons),
    )


def _status_for(
    state: _ArchiveState, projection: _RawRow
) -> tuple[str | None, tuple[str, ...]]:
    rows: tuple[_RawRow, ...] = (projection,)
    if projection.table == "memory_vector_documents":
        item_id = _normalise_text(projection.values.get("item_id"))
        linked = (
            _rows_matching(state, "memory_search_documents", item_id=item_id)
            if item_id is not None
            else ()
        )
        rows = linked
    values = _candidate_values(rows, ("status", "memory_status", "review_status"))
    distinct = _distinct_values(values)
    if not distinct:
        return None, ("status_missing",)
    if len(distinct) > 1:
        return None, (f"status_conflict:{','.join(sorted(distinct))}",)
    mapped = _STATUS_MAP.get(distinct[0].lower())
    if mapped is None:
        return None, (f"status_invalid:{distinct[0]}",)
    return mapped, ()


def _memory_type_for(
    state: _ArchiveState, projection: _RawRow
) -> tuple[str | None, tuple[str, ...]]:
    rows: tuple[_RawRow, ...] = (projection,)
    if projection.table == "memory_vector_documents":
        item_id = _normalise_text(projection.values.get("item_id"))
        rows = (
            _rows_matching(state, "memory_search_documents", item_id=item_id)
            if item_id is not None
            else ()
        )
    values = _candidate_values(rows, ("memory_type", "memory_kind"))
    distinct = _distinct_values(values)
    if not distinct:
        kind = _normalise_text(projection.values.get("kind"))
        if kind is not None:
            kind_map = {
                "claim": "semantic",
                "episode": "episodic",
                "timeline": "episodic",
                "knowledge": "procedural",
                "skill": "procedural",
                "person": "relationship",
                "relationship": "relationship",
            }
            mapped_kind = kind_map.get(kind.lower())
            if mapped_kind is not None:
                return mapped_kind, ()
        defaults = {
            "memory_claims": "semantic",
            "life_episodes": "episodic",
            "timeline_entries": "episodic",
            "knowledge_items": "procedural",
            "person_entities": "relationship",
            "person_aliases": "relationship",
            "relationships": "relationship",
        }
        default = defaults.get(projection.table)
        if default is not None:
            return default, ()
        return None, ("memory_type_missing",)
    if len(distinct) > 1:
        return None, (f"memory_type_conflict:{','.join(sorted(distinct))}",)
    value = distinct[0].lower()
    if value not in _MEMORY_TYPES:
        return None, (f"memory_type_invalid:{distinct[0]}",)
    return value, ()


def _confidence_for(
    state: _ArchiveState, projection: _RawRow
) -> tuple[float | None, tuple[str, ...]]:
    rows: tuple[_RawRow, ...] = (projection,)
    if projection.table == "memory_vector_documents":
        item_id = _normalise_text(projection.values.get("item_id"))
        rows = (
            _rows_matching(state, "memory_search_documents", item_id=item_id)
            if item_id is not None
            else ()
        )
    # Stability measures consolidation, not confidence; prefer the actual
    # confidence when a projection has both (as real memory_claims do).
    values = _candidate_values(rows, ("confidence",)) or _candidate_values(rows, ("stability",))
    if not values:
        return 0.5, ()
    parsed: list[float] = []
    for alias, value in values:
        try:
            number = float(value)
        except ValueError:
            return None, (f"confidence_invalid:{alias}",)
        if not math.isfinite(number) or number < 0.0 or number > 1.0:
            return None, (f"confidence_out_of_range:{alias}",)
        parsed.append(number)
    if len({round(number, 12) for number in parsed}) > 1:
        return None, ("confidence_conflict",)
    return parsed[0], ()


def _retention_for(
    projection: _RawRow, evidence_rows: tuple[_RawRow, ...]
) -> tuple[str | None, str | None, tuple[str, ...]]:
    values = _candidate_values(
        (projection,),
        ("retention", "retention_policy"),
    )
    distinct = _distinct_values(values)
    if len(distinct) > 1:
        return None, None, (f"retention_conflict:{','.join(sorted(distinct))}",)
    raw = distinct[0].lower() if distinct else "indefinite"
    retention_map = {
        "session": "session_only",
        "session_only": "session_only",
        "session_ephemeral": "session_only",
        "ttl": "ttl",
        "temporary": "ttl",
        "time_limited": "ttl",
        "expires": "ttl",
        "indefinite": "indefinite",
        "account_lifetime": "indefinite",
        "persistent": "indefinite",
    }
    retention = retention_map.get(raw)
    if retention is None or retention not in _RETENTION_VALUES:
        return None, None, (f"retention_invalid:{raw}",)

    expiration_values = _candidate_values(
        (projection,),
        ("retention_expires_at", "expires_at"),
    )
    expiration: str | None = None
    if expiration_values:
        distinct_expiration = _distinct_values(expiration_values)
        if len(distinct_expiration) > 1:
            return None, None, ("retention_expiration_conflict",)
        expiration, expiration_reasons = _parse_timestamp(
            distinct_expiration[0], "retention_expires_at"
        )
        if expiration_reasons:
            return None, None, expiration_reasons
    if retention == "ttl" and expiration is None:
        return None, None, ("retention_expiration_missing",)
    if expiration is not None and retention != "ttl":
        if distinct:
            return None, None, ("retention_expiration_conflict",)
        retention = "ttl"
    del evidence_rows  # consent retention never substitutes for memory retention
    return retention, expiration, ()


def _created_at_for(
    projection: _RawRow, evidence_rows: tuple[_RawRow, ...]
) -> tuple[str | None, tuple[str, ...]]:
    # These are different moments, not synonyms that must agree. Preserve
    # creation/observation time before falling back to event time.
    for field in ("created_at", "observed_at", "occurred_at", "event_start", "valid_at"):
        value = projection.values.get(field)
        if value is not None:
            return _parse_timestamp(value, "created_at")

    fallback_values = _candidate_values(
        evidence_rows,
        ("recorded_at", "occurred_at", "created_at"),
    )
    if not fallback_values:
        return None, ("created_at_missing",)
    parsed: list[str] = []
    for _, value in fallback_values:
        timestamp, reasons = _parse_timestamp(value, "created_at")
        if reasons or timestamp is None:
            return None, reasons or ("created_at_missing",)
        parsed.append(timestamp)
    return min(parsed), ()


def _payload_for(projection: _RawRow) -> dict[str, Any]:
    payload = {
        column: _json_safe(value) for column, value in projection.values.items()
    }
    payload["_legacy_archive"] = {
        "table": projection.table,
        "source_row_id": projection.row_id,
        "source_row_digest": projection.row_digest,
    }
    return payload


def _source_snapshot_digest(
    projection: _RawRow, lineage: _LineageResult, authority: _AuthorityResult
) -> str:
    return _sha256(
        {
            "projection": {
                "table": projection.table,
                "source_row_id": projection.row_id,
                "row_digest": projection.row_digest,
            },
            "lineage": [
                {"table": row.table, "source_row_id": row.row_id, "row_digest": row.row_digest}
                for row in lineage.lineage_rows
            ],
            "evidence": [
                {"table": row.table, "source_row_id": row.row_id, "row_digest": row.row_digest}
                for row in lineage.evidence_rows
            ],
            "consent": [
                {"table": row.table, "source_row_id": row.row_id, "row_digest": row.row_digest}
                for row in lineage.consent_rows
            ],
            "authority": [
                {"table": row.table, "source_row_id": row.row_id, "row_digest": row.row_digest}
                for row in authority.matches
            ],
        }
    )


def _record_for(
    projection: _RawRow,
    lineage: _LineageResult,
    authority: _AuthorityResult,
    memory_type: str,
    confidence: float,
    retention: str,
    retention_expires_at: str | None,
    created_at: str,
    record_id: str,
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "scope": _MEMORY_SCOPE,
        "subject_id": cast(str, lineage.subject_id),
        "resource_owner_id": authority.values["resource_owner_id"],
        "family_space_id": None,
        "co_subject_ids": [],
        "source_evidence_ids": list(lineage.source_evidence_ids),
        "policy_receipt_id": authority.values["policy_receipt_id"],
        "promotion_receipt_id": "",
        "promotion_fence_context_hash": "",
        "approval_evidence_refs": [],
        "consent_snapshot_id": authority.values["consent_snapshot_id"],
        "memory_type": memory_type,
        "confidence": confidence,
        "retention": retention,
        "retention_expires_at": retention_expires_at,
        "payload": _payload_for(projection),
        "created_by_actor_id": authority.values["created_by_actor_id"],
        "created_at": created_at,
        "shared_proposal_id": None,
    }


def _plan_entry(state: _ArchiveState, projection: _RawRow) -> _PlanEntry:
    record_id = _record_id(projection.table, projection.row_id)
    lineage = _lineage_for(state, projection)
    authority = _authority_for(state, projection, lineage, record_id)
    status, status_reasons = _status_for(state, projection)
    memory_type, type_reasons = _memory_type_for(state, projection)
    confidence, confidence_reasons = _confidence_for(state, projection)
    retention, retention_expires_at, retention_reasons = _retention_for(
        projection, lineage.evidence_rows
    )
    created_at, created_at_reasons = _created_at_for(
        projection, lineage.evidence_rows
    )
    details = _unique(
        (
            *lineage.reasons,
            *authority.reasons,
            *status_reasons,
            *type_reasons,
            *confidence_reasons,
            *retention_reasons,
            *created_at_reasons,
        )
    )
    source_snapshot_digest = _source_snapshot_digest(projection, lineage, authority)
    record: dict[str, Any] | None = None
    status_event: dict[str, Any] | None = None
    record_digest: str | None = None
    status_event_digest: str | None = None
    if not details:
        if (
            status is None
            or memory_type is None
            or confidence is None
            or retention is None
            or created_at is None
            or lineage.subject_id is None
        ):
            raise LegacyArchiveMigrationError(
                f"internal planning invariant failed for {projection.table}:{projection.row_id}"
            )
        record = _record_for(
            projection,
            lineage,
            authority,
            memory_type,
            confidence,
            retention,
            retention_expires_at,
            created_at,
            record_id,
        )
        status_event = {
            "event_id": _initial_status_event_id(record_id),
            "record_id": record_id,
            "status": status,
            "reason_code": "legacy_archive_migration",
            "created_at": created_at,
        }
        record_digest = _sha256(record)
        status_event_digest = _sha256(status_event)
    outcome: Outcome = "migrated" if not details else "quarantined"
    # A quarantined row must not expose a subject attribution in the operator
    # report.  The lineage details remain in the backup/quarantine evidence,
    # but the externally consumable classification is fail-closed.
    reported_subject_id = lineage.subject_id if outcome == "migrated" else None
    return _PlanEntry(
        table=projection.table,
        row_id=projection.row_id,
        row_digest=projection.row_digest,
        source_snapshot_digest=source_snapshot_digest,
        outcome=outcome,
        record_id=record_id,
        account_id=lineage.account_id,
        subject_id=reported_subject_id,
        source_evidence_ids=lineage.source_evidence_ids,
        authority=dict(authority.values),
        status=status,
        memory_type=memory_type,
        reason=details[0] if details else "safe_lineage_and_authority",
        details=details,
        source_row=_value_rows((projection,))[0],
        lineage_rows=_value_rows(lineage.lineage_rows),
        evidence_rows=_value_rows(lineage.evidence_rows),
        consent_rows=_value_rows(lineage.consent_rows),
        authority_rows=_value_rows(authority.matches),
        record=record,
        status_event=status_event,
        record_digest=record_digest,
        status_event_digest=status_event_digest,
    )


def _entry_dict(entry: _PlanEntry) -> dict[str, Any]:
    return {
        "table": entry.table,
        "source_row_id": entry.row_id,
        "before_row_digest": entry.row_digest,
        "source_snapshot_digest": entry.source_snapshot_digest,
        "outcome": entry.outcome,
        "record_id": entry.record_id,
        "account_id": entry.account_id,
        "subject_id": entry.subject_id,
        "source_evidence_ids": list(entry.source_evidence_ids),
        "authority": dict(entry.authority),
        "status": entry.status,
        "memory_type": entry.memory_type,
        "reason": entry.reason,
        "details": list(entry.details),
        "source_row": entry.source_row,
        "lineage_rows": list(entry.lineage_rows),
        "evidence_rows": list(entry.evidence_rows),
        "consent_rows": list(entry.consent_rows),
        "authority_rows": list(entry.authority_rows),
        "record": entry.record,
        "status_event": entry.status_event,
        "record_digest": entry.record_digest,
        "status_event_digest": entry.status_event_digest,
    }


def _json_value(value: object, field: str) -> object:
    if value is None:
        raise LegacyArchiveMigrationError(f"target column {field} contains NULL JSON")
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise LegacyArchiveMigrationError(
            f"target column {field} contains invalid JSON"
        ) from exc
    return decoded


def _json_list(value: object, field: str) -> list[Any]:
    decoded = _json_value(value, field)
    if not isinstance(decoded, list):
        raise LegacyArchiveMigrationError(f"target column {field} must contain a JSON list")
    return decoded


def _json_object(value: object, field: str) -> dict[str, Any]:
    decoded = _json_value(value, field)
    if not isinstance(decoded, Mapping):
        raise LegacyArchiveMigrationError(f"target column {field} must contain a JSON object")
    return {str(key): item for key, item in decoded.items()}


def _record_from_target_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        record = {
            "record_id": str(row["record_id"]),
            "scope": str(row["scope"]),
            "subject_id": str(row["subject_id"]),
            "resource_owner_id": str(row["resource_owner_id"]),
            "family_space_id": row["family_space_id"],
            "co_subject_ids": _json_list(row["co_subject_ids"], "co_subject_ids"),
            "source_evidence_ids": _json_list(
                row["source_evidence_ids"], "source_evidence_ids"
            ),
            "policy_receipt_id": str(row["policy_receipt_id"]),
            "promotion_receipt_id": str(row["promotion_receipt_id"]),
            "promotion_fence_context_hash": str(row["promotion_fence_context_hash"]),
            "approval_evidence_refs": _json_list(
                row["approval_evidence_refs"], "approval_evidence_refs"
            ),
            "consent_snapshot_id": str(row["consent_snapshot_id"]),
            "memory_type": str(row["memory_type"]),
            "confidence": float(row["confidence"]),
            "retention": str(row["retention"]),
            "retention_expires_at": row["retention_expires_at"],
            "payload": _json_object(row["payload"], "payload"),
            "created_by_actor_id": str(row["created_by_actor_id"]),
            "created_at": str(row["created_at"]),
            "shared_proposal_id": row["shared_proposal_id"],
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise LegacyArchiveMigrationError(
            "target memory_records row cannot be decoded"
        ) from exc
    return record


def _status_event_from_target_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        return {
            "event_id": str(row["event_id"]),
            "record_id": str(row["record_id"]),
            "status": str(row["status"]),
            "reason_code": str(row["reason_code"]),
            "created_at": str(row["created_at"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise LegacyArchiveMigrationError(
            "target memory_status_events row cannot be decoded"
        ) from exc


def _target_digest_payload(
    connection: sqlite3.Connection | None,
    *,
    records: Iterable[dict[str, Any]] = (),
    status_events: Iterable[dict[str, Any]] = (),
    force_schema: bool = False,
) -> str:
    predicted_records = tuple(records)
    predicted_status_events = tuple(status_events)
    tables: list[dict[str, Any]] = []
    for table_name, expected_columns, parser, predicted in (
        (
            "memory_records",
            _TARGET_RECORD_COLUMNS,
            _record_from_target_row,
            predicted_records,
        ),
        (
            "memory_status_events",
            _TARGET_STATUS_COLUMNS,
            _status_event_from_target_row,
            predicted_status_events,
        ),
    ):
        exists = connection is not None and _table_exists(connection, "main", table_name)
        if not exists and not force_schema and not predicted:
            tables.append({"table": table_name, "exists": False})
            continue

        columns = list(expected_columns)
        actual_rows: list[dict[str, Any]] = []
        if exists:
            if connection is None:
                raise LegacyArchiveMigrationError(
                    "target digest cannot read an existing table without a connection"
                )
            info = _table_info(connection, "main", table_name)
            columns = list(info.columns)
            actual_rows = [
                parser(row)
                for row in connection.execute(
                    f"SELECT * FROM {_qualified('main', table_name)}"
                ).fetchall()
            ]
        actual_rows.extend(predicted)
        key = "record_id" if table_name == "memory_records" else "event_id"
        actual_rows.sort(key=lambda row: str(row[key]))
        tables.append(
            {
                "table": table_name,
                "exists": True,
                "columns": columns,
                "rows": actual_rows,
            }
        )
    return _sha256(tables)


def _target_digest(connection: sqlite3.Connection | None) -> str:
    return _target_digest_payload(connection)


def _target_digest_with_entries(
    connection: sqlite3.Connection | None, entries: Iterable[_PlanEntry]
) -> str:
    records: list[dict[str, Any]] = []
    status_events: list[dict[str, Any]] = []
    for entry in entries:
        if entry.outcome != "migrated":
            continue
        if entry.record is None or entry.status_event is None:
            raise LegacyArchiveMigrationError(
                f"migrated entry has no target payload: {entry.table}:{entry.row_id}"
            )
        records.append(entry.record)
        status_events.append(entry.status_event)
    return _target_digest_payload(
        connection,
        records=records,
        status_events=status_events,
        force_schema=True,
    )


def _read_receipts(
    connection: sqlite3.Connection | None,
) -> dict[tuple[str, str], _ReceiptState]:
    """Read the row receipts that fence an incremental run.

    Receipts are the only authority on "this source row is already in the
    target".  A receipt without its migration row, two receipts for one source
    row, or a migration status this seam cannot interpret is a torn target and
    must be investigated instead of migrated around.
    """

    if connection is None or not _table_exists(connection, "main", _RECEIPT_TABLE):
        return {}
    _validate_support_schema(connection)
    rows = connection.execute(
        f"SELECT r.*, m.status AS migration_status "
        f"FROM {_quote_identifier(_RECEIPT_TABLE)} AS r "
        f"LEFT JOIN {_quote_identifier(_MIGRATION_TABLE)} AS m "
        "ON m.migration_id = r.migration_id"
    ).fetchall()
    receipts: dict[tuple[str, str], _ReceiptState] = {}
    for row in rows:
        migration_id = str(row["migration_id"])
        status = _normalise_text(row["migration_status"])
        if status is None:
            raise LegacyArchiveMigrationError(
                f"row receipt {migration_id} has no migration row"
            )
        if status not in ("applied", "rolled_back"):
            raise LegacyArchiveMigrationError(
                f"row receipt {migration_id} has unsupported migration status: {status}"
            )
        key = (str(row["table_name"]), str(row["source_row_id"]))
        if key in receipts:
            raise LegacyArchiveMigrationError(
                f"duplicate row receipts for {key[0]}:{key[1]}"
            )
        receipts[key] = _ReceiptState(
            migration_id=migration_id,
            migration_status=status,
            table=key[0],
            row_id=key[1],
            record_id=str(row["record_id"]),
            outcome=str(row["outcome"]),
            before_row_digest=str(row["before_row_digest"]),
            record_digest=str(row["record_digest"]),
            status_event_digest=str(row["status_event_digest"]),
            status_chain_digest=str(row["status_chain_digest"]),
            source_snapshot_digest=str(row["source_snapshot_digest"]),
        )
    return receipts


def _receipts_for_migration(
    connection: sqlite3.Connection, migration_id: str
) -> tuple[_ReceiptState, ...]:
    receipts = [
        receipt
        for receipt in _read_receipts(connection).values()
        if receipt.migration_id == migration_id
    ]
    return tuple(sorted(receipts, key=lambda item: (item.table, item.row_id)))


def _verify_receipts_against_source(
    state: _ArchiveState, receipts: Iterable[_ReceiptState]
) -> None:
    """Fence the receipt-covered source rows of one migration."""

    current = {_raw_key(row): row.row_digest for row in state.projection_rows}
    for receipt in receipts:
        digest = current.get((receipt.table, receipt.row_id))
        if digest is None:
            raise LegacyArchiveMigrationError(
                f"migrated source row is missing from the Archive: "
                f"{receipt.table}:{receipt.row_id}"
            )
        if digest != receipt.before_row_digest:
            raise LegacyArchiveMigrationError(
                f"migrated source row drifted since it was migrated: "
                f"{receipt.table}:{receipt.row_id}"
            )


def _verify_receipts_against_target(
    connection: sqlite3.Connection, receipts: Iterable[_ReceiptState]
) -> None:
    """Fence the migrated target rows and status chains of one migration."""

    for receipt in receipts:
        if receipt.outcome != "migrated":
            continue
        row = connection.execute(
            "SELECT * FROM memory_records WHERE record_id = ?", (receipt.record_id,)
        ).fetchone()
        if row is None:
            raise LegacyArchiveMigrationError(
                f"migrated target record is missing: {receipt.record_id}"
            )
        if _sha256(_record_from_target_row(row)) != receipt.record_digest:
            raise LegacyArchiveMigrationError(
                f"migrated target record drifted: {receipt.record_id}"
            )
        if _status_chain_digest(connection, receipt.record_id) != receipt.status_chain_digest:
            raise LegacyArchiveMigrationError(
                f"migrated status chain drifted: {receipt.record_id}"
            )


def _run_plan(
    state: _ArchiveState, target_connection: sqlite3.Connection | None
) -> _RunPlan:
    """Split the Archive inventory into rows to write and rows already written.

    The split is fenced by the earlier runs' row receipts: an already-migrated
    row whose receipt no longer matches the Archive (row digest, classification
    or owning migration) is refused instead of being written a second time.
    """

    receipts = _read_receipts(target_connection)
    pending: list[_PlanEntry] = []
    already_migrated: list[dict[str, Any]] = []
    covering: list[str] = []
    for projection in sorted(state.projection_rows, key=_raw_key):
        entry = _plan_entry(state, projection)
        receipt = receipts.get((entry.table, entry.row_id))
        if receipt is None:
            pending.append(entry)
            continue
        if receipt.migration_status == "rolled_back":
            raise LegacyArchiveMigrationError(
                f"source row belongs to rolled-back migration "
                f"{receipt.migration_id} and cannot be reactivated: "
                f"{entry.table}:{entry.row_id}"
            )
        if receipt.before_row_digest != entry.row_digest:
            raise LegacyArchiveMigrationError(
                f"source row drifted since it was migrated: {entry.table}:{entry.row_id}"
            )
        if (
            receipt.outcome != entry.outcome
            or receipt.source_snapshot_digest != entry.source_snapshot_digest
            or receipt.record_digest != (entry.record_digest or "")
        ):
            raise LegacyArchiveMigrationError(
                f"source row no longer classifies like its receipt: "
                f"{entry.table}:{entry.row_id}"
            )
        already_migrated.append(
            {
                "table": entry.table,
                "source_row_id": entry.row_id,
                "before_row_digest": entry.row_digest,
                "outcome": entry.outcome,
                "record_id": entry.record_id,
                "record_digest": receipt.record_digest,
                "migration_id": receipt.migration_id,
            }
        )
        covering.append(receipt.migration_id)
    return _RunPlan(
        rows=tuple(_entry_dict(entry) for entry in pending),
        pending=tuple(pending),
        already_migrated=tuple(already_migrated),
        covering_migration_ids=_unique(covering),
    )


# Byte-for-byte the ``memory_records``/``memory_status_events`` fragment of
# ``services.memory_scope.sqlite_store`` (same columns, defaults, constraints,
# indexes and append-only triggers).  A target created here must behave exactly
# like one created by the real adapter, so the schema is copied instead of
# re-derived; ``memory_type`` is validated by the planner, not by a CHECK the
# adapter does not have.
_TARGET_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS memory_records (
    record_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN {_SCOPE_VALUES}),
    subject_id TEXT NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 128),
    resource_owner_id TEXT NOT NULL CHECK (length(resource_owner_id) BETWEEN 1 AND 128),
    family_space_id TEXT,
    co_subject_ids TEXT NOT NULL DEFAULT '[]',
    source_evidence_ids TEXT NOT NULL DEFAULT '[]',
    policy_receipt_id TEXT NOT NULL DEFAULT '' CHECK (length(policy_receipt_id) BETWEEN 1 AND 128),
    promotion_receipt_id TEXT NOT NULL DEFAULT '' CHECK (length(promotion_receipt_id) BETWEEN 0 AND 128),
    promotion_fence_context_hash TEXT NOT NULL DEFAULT ''
        CHECK (length(promotion_fence_context_hash) BETWEEN 0 AND 128),
    approval_evidence_refs TEXT NOT NULL DEFAULT '[]',
    consent_snapshot_id TEXT NOT NULL DEFAULT '' CHECK (length(consent_snapshot_id) BETWEEN 1 AND 128),
    memory_type TEXT NOT NULL DEFAULT 'semantic',
    confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    retention TEXT NOT NULL DEFAULT 'indefinite'
        CHECK (retention IN ('session_only', 'ttl', 'indefinite')),
    retention_expires_at TEXT,
    payload TEXT NOT NULL DEFAULT '{{}}',
    created_by_actor_id TEXT NOT NULL DEFAULT '' CHECK (length(created_by_actor_id) BETWEEN 1 AND 128),
    created_at TEXT NOT NULL,
    shared_proposal_id TEXT,
    CHECK (scope <> 'family_shared' OR family_space_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_memory_records_subject
ON memory_records(subject_id, scope, created_at);
CREATE INDEX IF NOT EXISTS idx_memory_records_family
ON memory_records(family_space_id, scope, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_records_shared_proposal
ON memory_records(shared_proposal_id) WHERE shared_proposal_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_status_events (
    event_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES memory_records(record_id),
    status TEXT NOT NULL CHECK (status IN {_STATUS_VALUES}),
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_status_events_record
ON memory_status_events(record_id, created_at);

CREATE TRIGGER IF NOT EXISTS trg_memory_records_no_update
BEFORE UPDATE ON memory_records
BEGIN
    SELECT RAISE(ABORT, 'memory_records is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_records_no_delete
BEFORE DELETE ON memory_records
BEGIN
    SELECT RAISE(ABORT, 'memory_records is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_status_events_no_update
BEFORE UPDATE ON memory_status_events
BEGIN
    SELECT RAISE(ABORT, 'memory_status_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_status_events_no_delete
BEFORE DELETE ON memory_status_events
BEGIN
    SELECT RAISE(ABORT, 'memory_status_events is append-only');
END;
"""


def _ensure_target_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(_TARGET_SCHEMA)
    _validate_target_schema(connection)


def _validate_target_schema(connection: sqlite3.Connection) -> None:
    required = {
        "memory_records": set(_TARGET_RECORD_COLUMNS),
        "memory_status_events": set(_TARGET_STATUS_COLUMNS),
    }
    for table, columns in required.items():
        info = _table_info(connection, "main", table)
        missing = sorted(columns.difference(info.columns))
        if missing:
            raise LegacyArchiveMigrationError(
                f"target table main.{table} is missing required columns: {', '.join(missing)}"
            )


_SUPPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS legacy_archive_migrations (
    migration_id TEXT PRIMARY KEY,
    migration_key TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    source_digest_before TEXT NOT NULL,
    source_digest_after TEXT NOT NULL,
    target_digest_before TEXT NOT NULL,
    target_digest_after TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('applied', 'rolled_back')),
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    rolled_back_at TEXT,
    rollback_target_digest TEXT
);
CREATE TABLE IF NOT EXISTS legacy_archive_row_receipts (
    migration_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    source_row_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('migrated', 'quarantined')),
    before_row_digest TEXT NOT NULL,
    source_snapshot_digest TEXT NOT NULL,
    record_digest TEXT NOT NULL DEFAULT '',
    status_event_digest TEXT NOT NULL DEFAULT '',
    status_chain_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (migration_id, table_name, source_row_id)
);
CREATE INDEX IF NOT EXISTS idx_legacy_archive_receipts_record
ON legacy_archive_row_receipts(record_id);
CREATE TABLE IF NOT EXISTS legacy_archive_quarantine (
    migration_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    source_row_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL,
    source_snapshot_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (migration_id, table_name, source_row_id)
);
CREATE TABLE IF NOT EXISTS legacy_archive_backups (
    migration_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    source_row_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    source_snapshot_json TEXT NOT NULL,
    source_snapshot_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (migration_id, table_name, source_row_id)
);
CREATE TABLE IF NOT EXISTS legacy_archive_audit_events (
    event_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL,
    action TEXT NOT NULL,
    table_name TEXT,
    source_row_id TEXT,
    record_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_legacy_archive_audit_migration
ON legacy_archive_audit_events(migration_id, created_at, event_id);
"""

_SUPPORT_COLUMNS: dict[str, set[str]] = {
    _MIGRATION_TABLE: {
        "migration_id",
        "migration_key",
        "scope",
        "manifest_sha256",
        "source_digest_before",
        "source_digest_after",
        "target_digest_before",
        "target_digest_after",
        "status",
        "manifest_json",
        "created_at",
        "applied_at",
        "rolled_back_at",
        "rollback_target_digest",
    },
    _RECEIPT_TABLE: {
        "migration_id",
        "table_name",
        "source_row_id",
        "record_id",
        "outcome",
        "before_row_digest",
        "source_snapshot_digest",
        "record_digest",
        "status_event_digest",
        "status_chain_digest",
        "created_at",
    },
    _QUARANTINE_TABLE: {
        "migration_id",
        "table_name",
        "source_row_id",
        "record_id",
        "reason",
        "details_json",
        "source_snapshot_digest",
        "created_at",
    },
    _BACKUP_TABLE: {
        "migration_id",
        "table_name",
        "source_row_id",
        "record_id",
        "source_snapshot_json",
        "source_snapshot_digest",
        "created_at",
    },
    _AUDIT_TABLE: {
        "event_id",
        "migration_id",
        "action",
        "table_name",
        "source_row_id",
        "record_id",
        "payload_json",
        "created_at",
    },
}


def _ensure_support_schema(connection: sqlite3.Connection) -> None:
    # This function is deliberately called outside the business transaction.
    # SQLite's executescript() may implicitly commit an active transaction.
    connection.executescript(_SUPPORT_SCHEMA)
    _validate_support_schema(connection)


def _validate_support_schema(connection: sqlite3.Connection) -> None:
    for table, columns in _SUPPORT_COLUMNS.items():
        info = _table_info(connection, "main", table)
        missing = sorted(columns.difference(info.columns))
        if missing:
            raise LegacyArchiveMigrationError(
                f"target table main.{table} is missing required columns: {', '.join(missing)}"
            )


def _db_json(value: object) -> str:
    return json.dumps(
        _json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _target_path(archive_path: str | Path, target_path: str | Path | None) -> Path:
    archive = Path(archive_path).expanduser()
    return archive if target_path is None else Path(target_path).expanduser()


def _build_report_on_connections(
    source_connection: sqlite3.Connection,
    target_connection: sqlite3.Connection | None,
) -> dict[str, Any]:
    state = _read_archive(source_connection, "main")
    target_digest_before = _target_digest(target_connection)
    run = _run_plan(state, target_connection)
    entries = run.pending
    rows = list(run.rows)
    migrated = sum(entry.outcome == "migrated" for entry in entries)
    quarantined = sum(entry.outcome == "quarantined" for entry in entries)
    reason_counts: dict[str, int] = {}
    for entry in entries:
        for reason in entry.details:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    statistics: dict[str, Any] = {
        "projection_table_count": len(state.projection_tables),
        "source_table_count": len(state.source_tables),
        "row_count": len(entries),
        "source_row_count": len(state.projection_rows),
        "migrated": migrated,
        "quarantined": quarantined,
        "already_migrated": len(run.already_migrated),
        "quarantine_reasons": dict(sorted(reason_counts.items())),
    }
    migration_rows = [
        {
            "table": entry.table,
            "source_row_id": entry.row_id,
            "before_row_digest": entry.row_digest,
            "source_snapshot_digest": entry.source_snapshot_digest,
            "outcome": entry.outcome,
            "record_id": entry.record_id,
            "record_digest": entry.record_digest or "",
            "status_event_digest": entry.status_event_digest or "",
            "details": list(entry.details),
        }
        for entry in entries
    ]
    migration_key = _sha256(
        {
            "scope": _MEMORY_SCOPE,
            "source_digest": state.source_digest,
            "rows": migration_rows,
            "already_migrated": list(run.already_migrated),
            "statistics": statistics,
        }
    )
    migration_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memoria:memory-scope:legacy-archive:migration:{migration_key}",
        )
    )
    target_digest_after = _target_digest_with_entries(target_connection, entries)
    manifest_body: dict[str, Any] = {
        "scope": _MEMORY_SCOPE,
        "migration_key": migration_key,
        "source": {
            "digest_before": state.source_digest,
            "digest_after": state.source_digest,
            "tables": list(state.source_tables),
        },
        "target": {
            "digest_before": target_digest_before,
            "digest_after": target_digest_after,
            "tables": list(_TARGET_TABLES),
        },
        "rows": rows,
        "already_migrated": list(run.already_migrated),
        "statistics": statistics,
    }
    manifest = dict(manifest_body)
    manifest["manifest_sha256"] = _sha256(manifest_body)
    return {
        "scope": _MEMORY_SCOPE,
        "migration_id": migration_id,
        "migration_key": migration_key,
        "source": manifest["source"],
        "target": manifest["target"],
        "source_digest": state.source_digest,
        "target_digest_before": target_digest_before,
        "target_digest_after": target_digest_after,
        "row_count": len(entries),
        "migrated": migrated,
        "quarantined": quarantined,
        "already_migrated": list(run.already_migrated),
        "already_migrated_count": len(run.already_migrated),
        "statistics": statistics,
        "rows": rows,
        "manifest": manifest,
    }


def _build_report(
    archive_path: str | Path,
    target_path: str | Path | None,
) -> dict[str, Any]:
    archive = Path(archive_path).expanduser()
    target = _target_path(archive, target_path)
    with ExitStack() as stack:
        source_connection = _connect_read_only(archive)
        stack.callback(source_connection.close)
        if _same_path(archive, target):
            target_connection = source_connection
        elif target.exists():
            target_connection = _connect_read_only(target)
            stack.callback(target_connection.close)
        else:
            target_connection = None
        return _build_report_on_connections(source_connection, target_connection)


def _source_state(path: Path) -> _ArchiveState:
    connection = _connect_read_only(path)
    try:
        return _read_archive(connection, "main")
    finally:
        connection.close()


def _manifest_sha256(report: Mapping[str, Any]) -> str:
    manifest = report.get("manifest")
    if not isinstance(manifest, Mapping):
        raise LegacyArchiveMigrationError("migration report has no manifest")
    value = _normalise_text(manifest.get("manifest_sha256"))
    if value is None:
        raise LegacyArchiveMigrationError("migration manifest has no SHA-256")
    return value


def _manifest_from_row(row: sqlite3.Row) -> dict[str, Any]:
    value = _json_value(row["manifest_json"], "manifest_json")
    if not isinstance(value, Mapping):
        raise LegacyArchiveMigrationError("stored migration manifest is not an object")
    return {str(key): item for key, item in value.items()}


def _manifest_rows(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw_rows = manifest.get("rows")
    if not isinstance(raw_rows, list):
        raise LegacyArchiveMigrationError("stored migration manifest has no rows")
    result: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            raise LegacyArchiveMigrationError("stored migration row is not an object")
        result.append({str(key): item for key, item in raw_row.items()})
    return tuple(result)


def _manifest_already_migrated(
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """The inventory rows a stored manifest skipped because a receipt covered them."""

    raw_rows = manifest.get("already_migrated", [])
    if not isinstance(raw_rows, list):
        raise LegacyArchiveMigrationError(
            "stored migration manifest has invalid already-migrated rows"
        )
    result: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            raise LegacyArchiveMigrationError(
                "stored already-migrated row is not an object"
            )
        result.append({str(key): item for key, item in raw_row.items()})
    return tuple(result)


def _recorded_migration(
    connection: sqlite3.Connection, migration_id: str, migration_key: str
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(
        f"SELECT * FROM {_quote_identifier(_MIGRATION_TABLE)} "
        "WHERE migration_id = ? OR migration_key = ? LIMIT 1",
        (migration_id, migration_key),
    ).fetchone()
    return row


def _assert_manifest_matches_receipts(
    manifest: Mapping[str, Any], receipts: Iterable[_ReceiptState]
) -> None:
    """Refuse a migration whose stored manifest and row receipts disagree."""

    manifest_rows = {
        (str(row.get("table")), str(row.get("source_row_id")), str(row.get("outcome")))
        for row in _manifest_rows(manifest)
    }
    receipt_rows = {(receipt.table, receipt.row_id, receipt.outcome) for receipt in receipts}
    if manifest_rows != receipt_rows:
        raise LegacyArchiveMigrationError(
            "stored migration manifest and row receipts disagree"
        )


def _find_migration(
    connection: sqlite3.Connection,
    *,
    migration_id: str | None = None,
    migration_key: str | None = None,
    manifest_sha256: str | None = None,
) -> sqlite3.Row:
    candidates: list[sqlite3.Row] = []
    if migration_id is not None:
        row = connection.execute(
            f"SELECT * FROM {_quote_identifier(_MIGRATION_TABLE)} WHERE migration_id = ?",
            (migration_id,),
        ).fetchone()
        if row is not None:
            candidates.append(row)
    if migration_key is not None:
        row = connection.execute(
            f"SELECT * FROM {_quote_identifier(_MIGRATION_TABLE)} WHERE migration_key = ?",
            (migration_key,),
        ).fetchone()
        if row is not None:
            candidates.append(row)
    if manifest_sha256 is not None:
        row = connection.execute(
            f"SELECT * FROM {_quote_identifier(_MIGRATION_TABLE)} WHERE manifest_sha256 = ?",
            (manifest_sha256,),
        ).fetchone()
        if row is not None:
            candidates.append(row)
    unique = {str(row["migration_id"]): row for row in candidates}
    if not unique:
        raise LegacyArchiveMigrationError("requested legacy Archive migration was not found")
    if len(unique) != 1:
        raise LegacyArchiveMigrationError("migration selectors identify different migrations")
    return next(iter(unique.values()))


def _audit_event_id(
    migration_id: str,
    action: str,
    table_name: str | None,
    source_row_id: str | None,
    record_id: str | None,
    payload: Mapping[str, Any],
) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "memoria:memory-scope:legacy-archive:audit:"
            f"{migration_id}:{action}:{table_name or ''}:{source_row_id or ''}:"
            f"{record_id or ''}:{_sha256(payload)}",
        )
    )


def _insert_audit_event(
    connection: sqlite3.Connection,
    *,
    migration_id: str,
    action: str,
    table_name: str | None,
    source_row_id: str | None,
    record_id: str | None,
    payload: Mapping[str, Any],
    created_at: str,
) -> None:
    event_id = _audit_event_id(
        migration_id, action, table_name, source_row_id, record_id, payload
    )
    connection.execute(
        f"INSERT INTO {_quote_identifier(_AUDIT_TABLE)} "
        "(event_id, migration_id, action, table_name, source_row_id, record_id, "
        "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            migration_id,
            action,
            table_name,
            source_row_id,
            record_id,
            _db_json(payload),
            created_at,
        ),
    )


def _status_chain_digest(connection: sqlite3.Connection, record_id: str) -> str:
    rows = connection.execute(
        "SELECT * FROM memory_status_events "
        "WHERE record_id = ? ORDER BY created_at, event_id",
        (record_id,),
    ).fetchall()
    return _sha256([_status_event_from_target_row(row) for row in rows])


def _record_insert_values(record: Mapping[str, Any]) -> tuple[Any, ...]:
    json_columns = {
        "co_subject_ids",
        "source_evidence_ids",
        "approval_evidence_refs",
        "payload",
    }
    values: list[Any] = []
    for column in _TARGET_RECORD_COLUMNS:
        if column not in record:
            raise LegacyArchiveMigrationError(f"record is missing target column {column}")
        value = record[column]
        values.append(_db_json(value) if column in json_columns else value)
    return tuple(values)


def _insert_record_and_status(
    connection: sqlite3.Connection,
    entry: _PlanEntry,
) -> None:
    if entry.record is None or entry.status_event is None:
        raise LegacyArchiveMigrationError(
            f"migrated entry has no record/status event: {entry.table}:{entry.row_id}"
        )
    columns = ", ".join(_TARGET_RECORD_COLUMNS)
    placeholders = ", ".join("?" for _ in _TARGET_RECORD_COLUMNS)
    connection.execute(
        f"INSERT INTO memory_records ({columns}) VALUES ({placeholders})",
        _record_insert_values(entry.record),
    )
    connection.execute(
        "INSERT INTO memory_status_events "
        "(event_id, record_id, status, reason_code, created_at) VALUES (?, ?, ?, ?, ?)",
        tuple(entry.status_event[field] for field in _TARGET_STATUS_COLUMNS),
    )


def _entry_backup(entry: _PlanEntry) -> dict[str, Any]:
    return {
        "source_row": entry.source_row,
        "lineage_rows": list(entry.lineage_rows),
        "evidence_rows": list(entry.evidence_rows),
        "consent_rows": list(entry.consent_rows),
        "authority_rows": list(entry.authority_rows),
        "outcome": entry.outcome,
        "reason": entry.reason,
        "details": list(entry.details),
    }


def _insert_receipt_and_backup(
    connection: sqlite3.Connection,
    *,
    migration_id: str,
    entry: _PlanEntry,
    created_at: str,
) -> None:
    status_chain_digest = (
        _status_chain_digest(connection, entry.record_id)
        if entry.outcome == "migrated"
        else _sha256([])
    )
    connection.execute(
        f"INSERT INTO {_quote_identifier(_RECEIPT_TABLE)} "
        "(migration_id, table_name, source_row_id, record_id, outcome, "
        "before_row_digest, source_snapshot_digest, record_digest, "
        "status_event_digest, status_chain_digest, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            migration_id,
            entry.table,
            entry.row_id,
            entry.record_id,
            entry.outcome,
            entry.row_digest,
            entry.source_snapshot_digest,
            entry.record_digest or "",
            entry.status_event_digest or "",
            status_chain_digest,
            created_at,
        ),
    )
    backup = _entry_backup(entry)
    connection.execute(
        f"INSERT INTO {_quote_identifier(_BACKUP_TABLE)} "
        "(migration_id, table_name, source_row_id, record_id, source_snapshot_json, "
        "source_snapshot_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            migration_id,
            entry.table,
            entry.row_id,
            entry.record_id,
            _db_json(backup),
            entry.source_snapshot_digest,
            created_at,
        ),
    )
    if entry.outcome == "quarantined":
        connection.execute(
            f"INSERT INTO {_quote_identifier(_QUARANTINE_TABLE)} "
            "(migration_id, table_name, source_row_id, record_id, reason, details_json, "
            "source_snapshot_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                migration_id,
                entry.table,
                entry.row_id,
                entry.record_id,
                entry.reason,
                _db_json(list(entry.details)),
                entry.source_snapshot_digest,
                created_at,
            ),
        )


def _assert_no_target_conflicts(
    connection: sqlite3.Connection,
    migration_id: str,
    entries: Iterable[_PlanEntry],
) -> None:
    orphan_receipts = connection.execute(
        f"SELECT 1 FROM {_quote_identifier(_RECEIPT_TABLE)} "
        "WHERE migration_id = ? LIMIT 1",
        (migration_id,),
    ).fetchone()
    if orphan_receipts is not None:
        raise LegacyArchiveMigrationError(
            f"migration {migration_id} has receipts but no applied migration row"
        )
    for entry in entries:
        record = connection.execute(
            "SELECT record_id FROM memory_records WHERE record_id = ?",
            (entry.record_id,),
        ).fetchone()
        if record is not None:
            raise LegacyArchiveMigrationError(
                "target record already exists without a matching migration receipt: "
                f"{entry.record_id}"
            )
        status_event = connection.execute(
            "SELECT event_id FROM memory_status_events WHERE event_id = ?",
            (
                entry.status_event["event_id"]
                if entry.status_event is not None
                else _initial_status_event_id(entry.record_id),
            ),
        ).fetchone()
        if status_event is not None:
            raise LegacyArchiveMigrationError(
                "target status event already exists without a matching migration receipt: "
                f"{entry.record_id}"
            )
        receipt = connection.execute(
            f"SELECT migration_id FROM {_quote_identifier(_RECEIPT_TABLE)} "
            "WHERE table_name = ? AND source_row_id = ? LIMIT 1",
            (entry.table, entry.row_id),
        ).fetchone()
        if receipt is None:
            receipt = connection.execute(
                f"SELECT migration_id FROM {_quote_identifier(_RECEIPT_TABLE)} "
                "WHERE record_id = ? LIMIT 1",
                (entry.record_id,),
            ).fetchone()
        if receipt is not None:
            raise LegacyArchiveMigrationError(
                f"target receipt already exists for {entry.table}:{entry.row_id}"
            )


def _stored_report(
    report: Mapping[str, Any], row: sqlite3.Row, *, idempotent: bool
) -> dict[str, Any]:
    manifest = _manifest_from_row(row)
    rows = list(_manifest_rows(manifest))
    already_migrated = list(_manifest_already_migrated(manifest))
    statistics = manifest.get("statistics")
    if not isinstance(statistics, Mapping):
        raise LegacyArchiveMigrationError("stored migration manifest has no statistics")
    source = manifest.get("source")
    target = manifest.get("target")
    result = dict(report)
    result.update(
        {
            "migration_id": str(row["migration_id"]),
            "migration_key": str(row["migration_key"]),
            "manifest": manifest,
            "rows": rows,
            "row_count": len(rows),
            "migrated": int(statistics.get("migrated", 0)),
            "quarantined": int(statistics.get("quarantined", 0)),
            "already_migrated": already_migrated,
            "already_migrated_count": len(already_migrated),
            "statistics": dict(statistics),
            "source": dict(source) if isinstance(source, Mapping) else report.get("source"),
            "target": dict(target) if isinstance(target, Mapping) else report.get("target"),
            "source_digest": str(row["source_digest_after"]),
            "target_digest_before": str(row["target_digest_before"]),
            "target_digest_after": str(row["target_digest_after"]),
            "applied": False,
            "idempotent": idempotent,
            "status": str(row["status"]),
        }
    )
    return result


def plan(
    archive_path: str | Path,
    target_path: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a deterministic migration manifest without writing either database.

    The manifest covers the rows an apply would write now and lists the
    inventory rows an earlier run already migrated, so an operator approves
    exactly the write set.
    """

    del now  # Planning must not depend on wall-clock time.
    return _build_report(archive_path, target_path)


def dry_run(
    archive_path: str | Path,
    target_path: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Alias for plan kept explicit for operator scripts."""

    return plan(archive_path, target_path, now=now)


def apply(
    archive_path: str | Path,
    target_path: str | Path | None = None,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Apply one deterministic, incremental migration in a single transaction.

    Source rows whose receipts are already in the target are skipped, so a
    later run only writes the rows the target does not have yet.  A run with
    nothing to write is idempotent when one stored migration already covers
    every row it found; otherwise it journals the empty run, so no apply goes
    unrecorded.
    """

    report = _build_report(archive_path, target_path)
    actual_manifest_sha256 = _manifest_sha256(report)
    if (
        expected_manifest_sha256 is not None
        and expected_manifest_sha256 != actual_manifest_sha256
    ):
        raise LegacyArchiveMigrationError(
            "expected migration manifest SHA-256 does not match the current plan"
        )
    if dry_run:
        return report

    archive = Path(archive_path).expanduser()
    target = _target_path(archive, target_path)
    connection = _connect_target(target)
    try:
        target_before = _target_digest(connection)
        if target_before != str(report["target_digest_before"]):
            raise LegacyArchiveMigrationError(
                "target changed after planning and before apply"
            )

        _ensure_target_schema(connection)
        _ensure_support_schema(connection)
        current_state = _source_state(archive)
        if current_state.source_digest != str(report["source_digest"]):
            raise LegacyArchiveMigrationError(
                "Archive source changed after planning and before apply"
            )

        migration_id = str(report["migration_id"])
        migration_key = str(report["migration_key"])

        timestamp = _now(now).isoformat()
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = _run_plan(current_state, connection)
            planned_rows = report.get("rows")
            if (
                not isinstance(planned_rows, list)
                or planned_rows != list(current.rows)
                or list(current.already_migrated) != report.get("already_migrated")
            ):
                raise LegacyArchiveMigrationError(
                    "current Archive rows do not match the migration plan"
                )
            recorded = _recorded_migration(connection, migration_id, migration_key)
            if not current.pending:
                stored = (
                    _find_migration(
                        connection, migration_id=current.covering_migration_ids[0]
                    )
                    if len(current.covering_migration_ids) == 1
                    else recorded
                )
                if stored is not None:
                    status = str(stored["status"])
                    if status != "applied":
                        raise LegacyArchiveMigrationError(
                            f"unsupported stored migration status: {status}"
                        )
                    _verify_receipts_against_target(
                        connection,
                        _receipts_for_migration(connection, str(stored["migration_id"])),
                    )
                    connection.rollback()
                    return _stored_report(report, stored, idempotent=True)
            elif recorded is not None:
                raise LegacyArchiveMigrationError(
                    "migration is already recorded but its row receipts do not "
                    f"cover the planned rows: {migration_id}"
                )
            # The rows this run skips are fenced too: a migrated record that
            # no longer matches its receipt is drift, not something to migrate
            # past while writing the new rows.
            for covering_id in current.covering_migration_ids:
                _verify_receipts_against_target(
                    connection, _receipts_for_migration(connection, covering_id)
                )

            entries = current.pending
            _assert_no_target_conflicts(connection, migration_id, entries)
            for entry in entries:
                if entry.outcome == "migrated":
                    _insert_record_and_status(connection, entry)
                _insert_receipt_and_backup(
                    connection,
                    migration_id=migration_id,
                    entry=entry,
                    created_at=timestamp,
                )
                _insert_audit_event(
                    connection,
                    migration_id=migration_id,
                    action=(
                        "legacy_archive_migrated"
                        if entry.outcome == "migrated"
                        else "legacy_archive_quarantined"
                    ),
                    table_name=entry.table,
                    source_row_id=entry.row_id,
                    record_id=entry.record_id,
                    payload={
                        "outcome": entry.outcome,
                        "source_snapshot_digest": entry.source_snapshot_digest,
                        "details": list(entry.details),
                    },
                    created_at=timestamp,
                )

            target_after = _target_digest(connection)
            if target_after != str(report["target_digest_after"]):
                raise LegacyArchiveMigrationError(
                    "target digest after apply does not match the planned manifest"
                )
            manifest = report["manifest"]
            connection.execute(
                f"INSERT INTO {_quote_identifier(_MIGRATION_TABLE)} "
                "(migration_id, migration_key, scope, manifest_sha256, "
                "source_digest_before, source_digest_after, target_digest_before, "
                "target_digest_after, status, manifest_json, created_at, applied_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?, ?)",
                (
                    migration_id,
                    migration_key,
                    _MEMORY_SCOPE,
                    actual_manifest_sha256,
                    str(report["source"]["digest_before"]),
                    str(report["source"]["digest_after"]),
                    str(report["target"]["digest_before"]),
                    str(report["target"]["digest_after"]),
                    _db_json(manifest),
                    timestamp,
                    timestamp,
                ),
            )
            _insert_audit_event(
                connection,
                migration_id=migration_id,
                action="legacy_archive_applied",
                table_name=None,
                source_row_id=None,
                record_id=None,
                payload={
                    "manifest_sha256": actual_manifest_sha256,
                    "target_digest_after": target_after,
                },
                created_at=timestamp,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        result = dict(report)
        result.update(
            {
                "migration_id": migration_id,
                "applied": True,
                "idempotent": False,
                "status": "applied",
            }
        )
        return result
    finally:
        connection.close()


def _rolled_back_report(
    connection: sqlite3.Connection,
    stored: sqlite3.Row,
    receipts: Iterable[_ReceiptState],
) -> dict[str, Any]:
    """Re-verify an already rolled-back migration and report it again.

    The revocation of this migration's own records is re-checked row by row, so
    a later incremental migration - or any other unrelated target row - does
    not make the second rollback call fail, while a changed record, a changed
    rollback event or any further status event on that record still does.  The
    record was created by the migration with exactly one initial status event,
    so the revoked chain is the initial event plus the rollback event and
    nothing else; that check does not depend on event timestamps.
    """

    migration_id = str(stored["migration_id"])
    rolled_back_at = _normalise_text(stored["rolled_back_at"])
    if rolled_back_at is None:
        raise LegacyArchiveMigrationError(
            f"rolled-back migration {migration_id} has no rollback timestamp"
        )
    revoked: list[dict[str, str]] = []
    for receipt in receipts:
        if receipt.outcome != "migrated":
            continue
        row = connection.execute(
            "SELECT * FROM memory_records WHERE record_id = ?", (receipt.record_id,)
        ).fetchone()
        if row is None or _sha256(_record_from_target_row(row)) != receipt.record_digest:
            raise LegacyArchiveMigrationError(
                f"migrated target record drifted after the rollback: {receipt.record_id}"
            )
        event_id = _rollback_status_event_id(migration_id, receipt.record_id)
        event_row = connection.execute(
            "SELECT * FROM memory_status_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if event_row is None:
            raise LegacyArchiveMigrationError(
                f"rollback status event is missing: {receipt.record_id}"
            )
        expected_event = {
            "event_id": event_id,
            "record_id": receipt.record_id,
            "status": "revoked",
            "reason_code": "legacy_archive_rollback",
            "created_at": rolled_back_at,
        }
        if _status_event_from_target_row(event_row) != expected_event:
            raise LegacyArchiveMigrationError(
                f"rollback status event drifted: {receipt.record_id}"
            )
        chain = connection.execute(
            "SELECT * FROM memory_status_events WHERE record_id = ?",
            (receipt.record_id,),
        ).fetchall()
        digests = sorted(_sha256(_status_event_from_target_row(row)) for row in chain)
        expected_digests = sorted(
            (receipt.status_event_digest, _sha256(expected_event))
        )
        if len(chain) != 2 or digests != expected_digests:
            raise LegacyArchiveMigrationError(
                f"status chain changed after the rollback: {receipt.record_id}"
            )
        revoked.append(
            {
                "table": receipt.table,
                "source_row_id": receipt.row_id,
                "record_id": receipt.record_id,
            }
        )
    return {
        "scope": _MEMORY_SCOPE,
        "migration_id": migration_id,
        "migration_key": str(stored["migration_key"]),
        "manifest_sha256": str(stored["manifest_sha256"]),
        "status": "rolled_back",
        "target_digest_after": _target_digest(connection),
        "revoked": revoked,
        "revoked_count": len(revoked),
        "idempotent": True,
    }


def rollback(
    archive_path: str | Path,
    target_path: str | Path | None = None,
    *,
    migration_id: str | None = None,
    manifest_sha256: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append revocation events for one applied migration without deleting data.

    The fence is scoped to the row receipts of that migration: the source rows
    it migrated must still match their ``before_row_digest`` and the records it
    wrote must still match their receipt digests.  Rows written by other runs
    (including later, incremental ones) neither block the rollback nor get
    revoked by it; the whole-target digest stays behind as evidence only.
    """

    if migration_id is None and manifest_sha256 is None:
        raise LegacyArchiveMigrationError(
            "rollback requires migration_id or manifest_sha256"
        )
    archive = Path(archive_path).expanduser()
    target = _target_path(archive, target_path)
    if not target.exists():
        raise LegacyArchiveMigrationError(f"target SQLite database does not exist: {target}")

    with ExitStack() as stack:
        source_connection = _connect_read_only(archive)
        stack.callback(source_connection.close)
        if _same_path(archive, target):
            target_read_connection = source_connection
        else:
            target_read_connection = _connect_read_only(target)
            stack.callback(target_read_connection.close)
        state = _read_archive(source_connection, "main")
        _validate_target_schema(target_read_connection)
        _validate_support_schema(target_read_connection)
        stored = _find_migration(
            target_read_connection,
            migration_id=migration_id,
            manifest_sha256=manifest_sha256,
        )
        stored_migration_id = str(stored["migration_id"])
        stored_manifest_sha256 = str(stored["manifest_sha256"])
        if (
            manifest_sha256 is not None
            and manifest_sha256 != stored_manifest_sha256
        ):
            raise LegacyArchiveMigrationError(
                "rollback manifest SHA-256 does not match the stored migration"
            )
        receipts = _receipts_for_migration(target_read_connection, stored_migration_id)
        _assert_manifest_matches_receipts(_manifest_from_row(stored), receipts)
        status = str(stored["status"])
        if status == "rolled_back":
            return _rolled_back_report(target_read_connection, stored, receipts)
        if status != "applied":
            raise LegacyArchiveMigrationError(f"unsupported migration status: {status}")
        _verify_receipts_against_source(state, receipts)
        _verify_receipts_against_target(target_read_connection, receipts)

    connection = _connect_target(target)
    try:
        _validate_target_schema(connection)
        _validate_support_schema(connection)
        _verify_receipts_against_source(_source_state(archive), receipts)
        _verify_receipts_against_target(connection, receipts)

        timestamp = _now(now).isoformat()
        revoked: list[dict[str, str]] = []
        connection.execute("BEGIN IMMEDIATE")
        try:
            for receipt in receipts:
                if receipt.outcome != "migrated":
                    continue
                rollback_event_id = _rollback_status_event_id(
                    stored_migration_id, receipt.record_id
                )
                if connection.execute(
                    "SELECT 1 FROM memory_status_events WHERE event_id = ?",
                    (rollback_event_id,),
                ).fetchone() is not None:
                    raise LegacyArchiveMigrationError(
                        "rollback event already exists while migration is marked applied: "
                        f"{receipt.record_id}"
                    )
                connection.execute(
                    "INSERT INTO memory_status_events "
                    "(event_id, record_id, status, reason_code, created_at) "
                    "VALUES (?, ?, 'revoked', 'legacy_archive_rollback', ?)",
                    (rollback_event_id, receipt.record_id, timestamp),
                )
                _insert_audit_event(
                    connection,
                    migration_id=stored_migration_id,
                    action="legacy_archive_rollback",
                    table_name=receipt.table,
                    source_row_id=receipt.row_id,
                    record_id=receipt.record_id,
                    payload={
                        "rollback_event_id": rollback_event_id,
                        "reason_code": "legacy_archive_rollback",
                    },
                    created_at=timestamp,
                )
                revoked.append(
                    {
                        "table": receipt.table,
                        "source_row_id": receipt.row_id,
                        "record_id": receipt.record_id,
                    }
                )
            rollback_target_digest = _target_digest(connection)
            _insert_audit_event(
                connection,
                migration_id=stored_migration_id,
                action="legacy_archive_rolled_back",
                table_name=None,
                source_row_id=None,
                record_id=None,
                payload={"rollback_target_digest": rollback_target_digest},
                created_at=timestamp,
            )
            connection.execute(
                f"UPDATE {_quote_identifier(_MIGRATION_TABLE)} "
                "SET status = 'rolled_back', rolled_back_at = ?, "
                "rollback_target_digest = ? WHERE migration_id = ?",
                (timestamp, rollback_target_digest, stored_migration_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return {
            "scope": _MEMORY_SCOPE,
            "migration_id": stored_migration_id,
            "migration_key": str(stored["migration_key"]),
            "manifest_sha256": stored_manifest_sha256,
            "status": "rolled_back",
            "target_digest_after": rollback_target_digest,
            "revoked": revoked,
            "revoked_count": len(revoked),
            "idempotent": False,
        }
    finally:
        connection.close()


_SUBJECT_READ_LIMIT = 50


def read_subject(
    target_path: str | Path,
    subject_id: str,
    *,
    limit: int = _SUBJECT_READ_LIMIT,
) -> dict[str, Any]:
    """Read the legacy-archive records one subject received (read-only).

    The read path of the migration: the rows are the same ``memory_records``
    and ``memory_status_events`` the Memory Scope store serves, decoded by the
    migration's own decoders, so operator and store cannot disagree on the
    shape.  A missing database or an un-migrated target answers with zero rows
    instead of being created or falling back to the legacy Archive tables.
    """

    subject = subject_id.strip()
    if not subject:
        raise LegacyArchiveMigrationError("read_subject requires a subject_id")
    if limit < 1:
        raise LegacyArchiveMigrationError("read_subject limit must be positive")
    path = Path(target_path).expanduser()
    report: dict[str, Any] = {
        "scope": _MEMORY_SCOPE,
        "target_path": str(path),
        "subject_id": subject,
        "target_present": False,
        "records": {"count": 0, "rows": []},
        "status_events": {"count": 0, "rows": []},
        "truncated": False,
    }
    if not path.exists():
        return {**report, "reason": "database_missing"}
    connection = _connect_read_only(path)
    try:
        if not _table_exists(connection, "main", _TARGET_TABLES[0]):
            return {**report, "reason": "target_missing"}
        report["target_present"] = True
        total = int(
            connection.execute(
                "SELECT count(*) FROM memory_records WHERE scope = ? AND subject_id = ?",
                (_MEMORY_SCOPE, subject),
            ).fetchone()[0]
        )
        rows = connection.execute(
            "SELECT * FROM memory_records WHERE scope = ? AND subject_id = ? "
            "ORDER BY created_at DESC, record_id LIMIT ?",
            (_MEMORY_SCOPE, subject, limit),
        ).fetchall()
        records = [_record_from_target_row(row) for row in rows]
        report["records"] = {"count": total, "rows": records}
        truncated = total > len(records)
        record_ids = [str(record["record_id"]) for record in records]
        if record_ids and _table_exists(connection, "main", _TARGET_TABLES[1]):
            placeholders = ", ".join("?" for _ in record_ids)
            events_total = int(
                connection.execute(
                    "SELECT count(*) FROM memory_status_events "
                    f"WHERE record_id IN ({placeholders})",
                    tuple(record_ids),
                ).fetchone()[0]
            )
            events = connection.execute(
                "SELECT * FROM memory_status_events "
                f"WHERE record_id IN ({placeholders}) "
                "ORDER BY created_at, event_id LIMIT ?",
                (*record_ids, limit),
            ).fetchall()
            report["status_events"] = {
                "count": events_total,
                "rows": [_status_event_from_target_row(row) for row in events],
            }
            truncated = truncated or events_total > len(events)
        report["truncated"] = truncated
        return report
    finally:
        connection.close()
