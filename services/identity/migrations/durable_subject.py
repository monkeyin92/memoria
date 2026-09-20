"""Operator-invoked migration from account-keyed evidence to durable subjects.

This module is deliberately SQLite-only.  It is a bridge for local/control
databases while the authoritative PostgreSQL lineage is being rolled out; it
does not run as part of application startup.  The migration is conservative:
only an owner event with an independently registered account and an active
identity person whose id is that account can be backfilled automatically.
Every scanned row receives a receipt, while only a safe ``mapped`` row whose
subject actually changes is written.

``dry_run`` and ``plan`` use read-only SQLite connections.  ``apply`` takes a
write lock, rechecks the source and identity digests inside the transaction,
and records enough information to verify target drift before rollback.

The code intentionally does not use account ids as subjects by convention:
the equality is accepted only when both Control registration evidence and an
active Identity authority person prove it.

.. note::
   Production PostgreSQL remediation remains a separate operator procedure.
   This seam is for SQLite development/legacy stores only.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

Outcome = Literal["mapped", "omitted", "quarantined"]

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KNOWN_EVIDENCE_TABLES = frozenset({"evidence_events", "archive_evidence_events"})
_SUPPORT_TABLE_PREFIX = "durable_subject_"
_IDENTITY_SCHEMA = "identity_authority"
_MIGRATION_TABLE = "durable_subject_migrations"
_RECEIPT_TABLE = "durable_subject_row_receipts"
_BACKUP_TABLE = "durable_subject_backups"
_AUDIT_TABLE = "durable_subject_audit_events"


class DurableSubjectMigrationError(RuntimeError):
    """Raised when the migration cannot prove a safe, non-drifting plan."""


@dataclass(frozen=True, slots=True)
class _TableInfo:
    name: str
    columns: tuple[str, ...]
    payload_column: str | None


@dataclass(frozen=True, slots=True)
class _SourceRow:
    table: str
    columns: tuple[str, ...]
    values: dict[str, Any]
    row_id: str
    row_digest: str
    before_subject: str | None


@dataclass(frozen=True, slots=True)
class _IdentityState:
    schema: str
    persons: dict[str, dict[str, Any]]
    relationships: tuple[dict[str, Any], ...]
    bindings: tuple[dict[str, Any], ...]
    binding_roles: tuple[dict[str, Any], ...]
    digest: str
    tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Classification:
    outcome: Outcome
    after_subject: str | None
    reason: str


def _now(value: datetime | None) -> datetime:
    return value.astimezone(UTC) if value is not None else datetime.now(UTC)


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise DurableSubjectMigrationError(f"unsafe SQLite identifier: {value!r}")
    return f'"{value}"'


def _qualified(schema: str, table: str) -> str:
    return f"{_quote_identifier(schema)}.{_quote_identifier(table)}"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    return str(value)


def _normalise_subject(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _same_path(first: Path, second: Path) -> bool:
    return first.expanduser().resolve() == second.expanduser().resolve()


def _read_only_uri(path: Path) -> str:
    return f"{path.expanduser().resolve().as_uri()}?mode=ro"


def _connect(
    control_path: str | Path,
    identity_path: str | Path | None,
    *,
    read_only: bool,
) -> tuple[sqlite3.Connection, str]:
    control = Path(control_path).expanduser().resolve()
    identity = Path(identity_path).expanduser().resolve() if identity_path is not None else control
    if read_only:
        connection = sqlite3.connect(_read_only_uri(control), uri=True)
    else:
        # ATTACH DATABASE must parse the read-only file URI below as a URI
        # rather than treating it as a literal filename.
        connection = sqlite3.connect(control, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    if _same_path(control, identity):
        return connection, "main"
    if not identity.exists():
        connection.close()
        raise DurableSubjectMigrationError(f"identity database does not exist: {identity}")
    try:
        connection.execute("ATTACH DATABASE ? AS identity_authority", (_read_only_uri(identity),))
    except Exception:
        connection.close()
        raise
    return connection, _IDENTITY_SCHEMA


def _table_names(connection: sqlite3.Connection, schema: str) -> tuple[str, ...]:
    rows = connection.execute(
        f"SELECT name FROM {_quote_identifier(schema)}.sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return tuple(str(row["name"]) for row in rows)


def _table_exists(connection: sqlite3.Connection, schema: str, table: str) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {_quote_identifier(schema)}.sqlite_master "
        "WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(connection: sqlite3.Connection, schema: str, table: str) -> tuple[str, ...]:
    rows = connection.execute(
        f"PRAGMA {_quote_identifier(schema)}.table_info({_quote_identifier(table)})"
    ).fetchall()
    return tuple(str(row["name"]) for row in rows)


def _require_columns(
    connection: sqlite3.Connection,
    schema: str,
    table: str,
    required: Iterable[str],
) -> tuple[str, ...]:
    present = _columns(connection, schema, table)
    missing = sorted(set(required).difference(present))
    if missing:
        raise DurableSubjectMigrationError(
            f"{schema}.{table} is missing required columns: {', '.join(missing)}"
        )
    return present


def _source_tables(connection: sqlite3.Connection) -> tuple[_TableInfo, ...]:
    result: list[_TableInfo] = []
    for table in _table_names(connection, "main"):
        if table.startswith(_SUPPORT_TABLE_PREFIX):
            continue
        present = set(_columns(connection, "main", table))
        if table in _KNOWN_EVIDENCE_TABLES:
            missing = sorted({"event_id", "account_id", "subject_id"}.difference(present))
            if missing:
                raise DurableSubjectMigrationError(
                    f"known evidence table main.{table} is missing required columns: "
                    f"{', '.join(missing)}"
                )
        if not {"event_id", "account_id", "subject_id"}.issubset(present):
            continue
        payload_column = next(
            (name for name in ("payload_json", "payload") if name in present),
            None,
        )
        result.append(
            _TableInfo(
                name=table,
                columns=tuple(_columns(connection, "main", table)),
                payload_column=payload_column,
            )
        )
    if not result:
        raise DurableSubjectMigrationError(
            "no known evidence source found in the control database"
        )
    return tuple(result)


def _row_values(row: sqlite3.Row, columns: tuple[str, ...]) -> dict[str, Any]:
    return {column: row[column] for column in columns}


def _row_digest(table: str, columns: tuple[str, ...], values: Mapping[str, object]) -> str:
    return _sha256(
        {
            "table": table,
            "columns": [
                {"name": column, "value": _json_safe(values.get(column))}
                for column in columns
            ],
        }
    )


def _read_source_rows(
    connection: sqlite3.Connection, tables: tuple[_TableInfo, ...]
) -> tuple[_SourceRow, ...]:
    result: list[_SourceRow] = []
    for table in tables:
        qualified = _qualified("main", table.name)
        rows = connection.execute(
            f"SELECT * FROM {qualified} ORDER BY {_quote_identifier('event_id')}"
        ).fetchall()
        seen_ids: set[str] = set()
        for row in rows:
            values = _row_values(row, table.columns)
            raw_id = values.get("event_id")
            if raw_id is None:
                raise DurableSubjectMigrationError(
                    f"source table main.{table.name} contains a row without event_id"
                )
            row_id = str(raw_id)
            if row_id in seen_ids:
                raise DurableSubjectMigrationError(
                    f"source table main.{table.name} has duplicate event_id {row_id!r}"
                )
            seen_ids.add(row_id)
            result.append(
                _SourceRow(
                    table=table.name,
                    columns=table.columns,
                    values=values,
                    row_id=row_id,
                    row_digest=_row_digest(table.name, table.columns, values),
                    before_subject=_normalise_subject(values.get("subject_id")),
                )
            )
    return tuple(sorted(result, key=lambda item: (item.table, item.row_id)))


def _source_digest(
    tables: tuple[_TableInfo, ...],
    rows: Iterable[_SourceRow],
    replacements: Mapping[tuple[str, str], str | None] | None = None,
) -> str:
    by_key = dict(replacements or {})
    rows_by_table: dict[str, list[dict[str, object]]] = {table.name: [] for table in tables}
    for row in rows:
        values = dict(row.values)
        key = (row.table, row.row_id)
        if key in by_key:
            values["subject_id"] = by_key[key]
        rows_by_table[row.table].append(
            {
                "row_id": row.row_id,
                "row_digest": _row_digest(row.table, row.columns, values),
            }
        )
    payload = [
        {
            "table": table.name,
            "columns": list(table.columns),
            "rows": rows_by_table[table.name],
        }
        for table in tables
    ]
    return _sha256(payload)


def _load_table_rows(
    connection: sqlite3.Connection, schema: str, table: str, required: Iterable[str]
) -> tuple[dict[str, Any], ...]:
    if not _table_exists(connection, schema, table):
        return ()
    columns = _require_columns(connection, schema, table, required)
    rows = connection.execute(
        f"SELECT * FROM {_qualified(schema, table)} ORDER BY rowid"
    ).fetchall()
    return tuple(_row_values(row, columns) for row in rows)


def _identity_digest(
    connection: sqlite3.Connection,
    schema: str,
    table_names: Iterable[str],
) -> str:
    payload: list[dict[str, object]] = []
    for table in table_names:
        if not _table_exists(connection, schema, table):
            payload.append({"table": table, "columns": [], "rows": []})
            continue
        columns = _columns(connection, schema, table)
        rows = connection.execute(
            f"SELECT * FROM {_qualified(schema, table)} ORDER BY rowid"
        ).fetchall()
        canonical_rows = [
            [
                {"name": column, "value": _json_safe(row[column])}
                for column in columns
            ]
            for row in rows
        ]
        canonical_rows.sort(key=_canonical)
        payload.append(
            {
                "table": table,
                "columns": list(columns),
                "rows": canonical_rows,
            }
        )
    return _sha256(payload)


def _load_identity(connection: sqlite3.Connection, schema: str) -> _IdentityState:
    if not _table_exists(connection, schema, "identity_persons"):
        raise DurableSubjectMigrationError(
            f"identity authority {schema} is missing required table identity_persons"
        )
    _require_columns(connection, schema, "identity_persons", ("person_id", "status"))
    person_rows = _load_table_rows(connection, schema, "identity_persons", ("person_id", "status"))
    persons: dict[str, dict[str, Any]] = {}
    for row in person_rows:
        person_id = _normalise_subject(row.get("person_id"))
        if person_id is None:
            raise DurableSubjectMigrationError("identity_persons contains a NULL/empty person_id")
        if person_id in persons:
            raise DurableSubjectMigrationError(f"identity_persons contains duplicate person_id {person_id!r}")
        persons[person_id] = row

    relationships = _load_table_rows(
        connection,
        schema,
        "identity_relationships",
        ("source_person_id", "target_person_id", "relation_type", "status"),
    )
    bindings = _load_table_rows(
        connection,
        schema,
        "identity_device_bindings",
        ("binding_id", "account_owner_person_id", "status"),
    )
    binding_roles = _load_table_rows(
        connection,
        schema,
        "identity_device_binding_roles",
        ("binding_id", "person_id", "role", "status"),
    )
    digest_tables = (
        "identity_persons",
        "identity_relationships",
        "identity_device_bindings",
        "identity_device_binding_roles",
    )
    return _IdentityState(
        schema=schema,
        persons=persons,
        relationships=relationships,
        bindings=bindings,
        binding_roles=binding_roles,
        digest=_identity_digest(connection, schema, digest_tables),
        tables=tuple(table for table in digest_tables if _table_exists(connection, schema, table)),
    )


def _registered_accounts(connection: sqlite3.Connection) -> set[str]:
    candidates: set[str] = set()
    sources = (("accounts", "user_id"), ("profiles", "user_id"), ("device_identities", "account_id"))
    for table, column in sources:
        if not _table_exists(connection, "main", table):
            continue
        present = set(_columns(connection, "main", table))
        if column not in present:
            continue
        rows = connection.execute(
            f"SELECT DISTINCT {_quote_identifier(column)} AS account_id FROM {_qualified('main', table)} "
            f"WHERE {_quote_identifier(column)} IS NOT NULL"
        ).fetchall()
        candidates.update(str(row["account_id"]) for row in rows)
    return candidates


def _payload(
    row: _SourceRow, table: _TableInfo
) -> tuple[dict[str, Any] | None, str | None]:
    column = table.payload_column
    if column is None:
        return {}, None
    raw = row.values.get(column)
    if raw is None:
        return None, "invalid_payload"
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, "invalid_payload"
    if not isinstance(raw, str):
        raw = str(raw)
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "invalid_payload"
    if not isinstance(decoded, dict):
        return None, "invalid_payload"
    return cast(dict[str, Any], decoded), None


def _payload_conflict(payload: Mapping[str, Any] | None, subject: str | None) -> bool:
    if payload is None or "subject_id" not in payload:
        return False
    payload_subject = payload["subject_id"]
    if payload_subject is not None and not isinstance(payload_subject, str):
        return True
    return payload_subject != subject


def _subject_accounts(
    subject_id: str,
    identity: _IdentityState,
    registered_accounts: set[str],
) -> set[str]:
    owners: set[str] = set()
    for relationship in identity.relationships:
        if str(relationship.get("status")) != "active":
            continue
        source = _normalise_subject(relationship.get("source_person_id"))
        target = _normalise_subject(relationship.get("target_person_id"))
        if source in registered_accounts and target == subject_id:
            owners.add(source)
        if target in registered_accounts and source == subject_id:
            owners.add(target)
    role_by_binding: dict[str, set[str]] = {}
    for role in identity.binding_roles:
        if str(role.get("status")) == "active" and _normalise_subject(role.get("person_id")) == subject_id:
            binding_id = _normalise_subject(role.get("binding_id"))
            if binding_id is not None:
                role_by_binding.setdefault(binding_id, set()).add(str(role.get("role")))
    for binding in identity.bindings:
        if str(binding.get("status")) != "active":
            continue
        binding_id = _normalise_subject(binding.get("binding_id"))
        owner = _normalise_subject(binding.get("account_owner_person_id"))
        if binding_id is None or owner is None or owner not in registered_accounts:
            continue
        if binding_id in role_by_binding:
            owners.add(owner)
    return owners


def _classify(
    row: _SourceRow,
    table: _TableInfo,
    identity: _IdentityState,
    registered_accounts: set[str],
) -> _Classification:
    payload, payload_error = _payload(row, table)
    if payload_error is not None:
        return _Classification("quarantined", row.before_subject, payload_error)
    if _payload_conflict(payload, row.before_subject):
        return _Classification("quarantined", row.before_subject, "payload_subject_conflict")

    account_id = _normalise_subject(row.values.get("account_id"))
    speaker_class = str(row.values.get("speaker_class")) if row.values.get("speaker_class") is not None else None
    if account_id is None:
        return _Classification("quarantined", row.before_subject, "account_id_missing")

    if row.before_subject is None:
        if speaker_class != "owner":
            return _Classification("omitted", None, "no_subject_evidence")
        person = identity.persons.get(account_id)
        if account_id not in registered_accounts:
            return _Classification("quarantined", None, "owner_account_unregistered")
        if person is None:
            return _Classification("quarantined", None, "owner_person_missing")
        if str(person.get("status")) != "active":
            return _Classification("quarantined", None, "owner_person_inactive")
        return _Classification("mapped", account_id, "owner_account_identity")

    subject = row.before_subject
    person = identity.persons.get(subject)
    if person is None:
        return _Classification("quarantined", subject, "subject_unknown")
    if str(person.get("status")) != "active":
        return _Classification("quarantined", subject, "subject_inactive")
    if speaker_class == "owner" and subject != account_id:
        return _Classification("quarantined", subject, "owner_speaker_subject_conflict")
    if subject == account_id:
        if account_id not in registered_accounts:
            return _Classification("quarantined", subject, "owner_account_unregistered")
        return _Classification("mapped", subject, "valid_owner_subject")

    owners = _subject_accounts(subject, identity, registered_accounts)
    if len(owners) > 1:
        return _Classification("quarantined", subject, "subject_binding_ambiguous")
    if account_id not in owners:
        return _Classification("quarantined", subject, "foreign_subject")
    return _Classification("mapped", subject, "valid_member_subject")


def _table_map(tables: tuple[_TableInfo, ...]) -> dict[str, _TableInfo]:
    return {table.name: table for table in tables}


def _manifest_report(
    *,
    control_path: Path,
    identity_path: Path,
    tables: tuple[_TableInfo, ...],
    source_rows: tuple[_SourceRow, ...],
    identity: _IdentityState,
    registered_accounts: set[str],
    excluded_keys: set[tuple[str, str]],
    now: datetime,
) -> dict[str, Any]:
    table_by_name = _table_map(tables)
    entries: list[dict[str, Any]] = []
    replacements: dict[tuple[str, str], str | None] = {}
    counts: dict[Outcome, int] = {"mapped": 0, "omitted": 0, "quarantined": 0}
    changed_count = 0
    for row in source_rows:
        key = (row.table, row.row_id)
        if key in excluded_keys:
            continue
        classification = _classify(
            row, table_by_name[row.table], identity, registered_accounts
        )
        counts[classification.outcome] += 1
        if classification.outcome == "mapped" and classification.after_subject != row.before_subject:
            changed_count += 1
            replacements[key] = classification.after_subject
        entries.append(
            {
                "table": row.table,
                "source_row_id": row.row_id,
                "before_row_digest": row.row_digest,
                "before_subject": row.before_subject,
                "after_subject": classification.after_subject,
                "reason": classification.reason,
                "outcome": classification.outcome,
            }
        )
    entries.sort(key=lambda item: (str(item["table"]), str(item["source_row_id"])))
    source_digest_before = _source_digest(tables, source_rows)
    source_digest_after = _source_digest(tables, source_rows, replacements)
    for entry in entries:
        if entry["outcome"] == "mapped":
            key = (str(entry["table"]), str(entry["source_row_id"]))
            row = next(item for item in source_rows if (item.table, item.row_id) == key)
            values = dict(row.values)
            values["subject_id"] = entry["after_subject"]
            entry["after_row_digest"] = _row_digest(row.table, row.columns, values)
        else:
            entry["after_row_digest"] = entry["before_row_digest"]

    core: dict[str, Any] = {
        "version": 1,
        "kind": "durable_subject_migration",
        "source": {
            "digest_before": source_digest_before,
            "digest_after": source_digest_after,
            "tables": [
                {"table": table.name, "columns": list(table.columns)} for table in tables
            ],
        },
        "identity": {
            "digest": identity.digest,
            "tables": list(identity.tables),
        },
        "account_evidence": {"registered_account_count": len(registered_accounts)},
        "rows": entries,
        "statistics": {
            "row_count": len(entries),
            "mapped": counts["mapped"],
            "omitted": counts["omitted"],
            "quarantined": counts["quarantined"],
            "changed": changed_count,
        },
    }
    manifest_sha256 = _sha256(core)
    manifest = dict(core)
    manifest["manifest_sha256"] = manifest_sha256
    return {
        "dry_run": True,
        "applied": False,
        "control_path": str(control_path),
        "identity_path": str(identity_path),
        "migration_id": None,
        "manifest_sha256": manifest_sha256,
        "source_digest_before": source_digest_before,
        "source_digest_after": source_digest_after,
        "identity_digest": identity.digest,
        "tables": [table.name for table in tables],
        "rows": entries,
        "statistics": core["statistics"],
        "manifest": manifest,
        "planned_at": now.isoformat(),
    }


def _build_report(
    control_path: str | Path,
    identity_path: str | Path | None,
    *,
    now: datetime,
    read_only: bool,
    excluded_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    control = Path(control_path).expanduser().resolve()
    identity = Path(identity_path).expanduser().resolve() if identity_path is not None else control
    connection, identity_schema = _connect(control, identity, read_only=read_only)
    try:
        tables = _source_tables(connection)
        source_rows = _read_source_rows(connection, tables)
        _assert_no_rolled_back_receipts(connection, source_rows)
        authority = _load_identity(connection, identity_schema)
        accounts = _registered_accounts(connection)
        return _manifest_report(
            control_path=control,
            identity_path=identity,
            tables=tables,
            source_rows=source_rows,
            identity=authority,
            registered_accounts=accounts,
            excluded_keys=excluded_keys or set(),
            now=now,
        )
    finally:
        connection.close()


def plan(control_path: str | Path, identity_path: str | Path | None = None) -> dict[str, Any]:
    """Build a stable read-only migration manifest."""
    return _build_report(
        control_path, identity_path, now=_now(None), read_only=True
    )


def dry_run(control_path: str | Path, identity_path: str | Path | None = None) -> dict[str, Any]:
    """Return the migration plan without creating support tables or writing rows."""
    report = plan(control_path, identity_path)
    report["dry_run"] = True
    return report


def _support_tables(connection: sqlite3.Connection) -> None:
    # Keep every DDL statement inside the caller's transaction.  SQLite's
    # executescript() issues an implicit COMMIT, which would leave source
    # writes and their receipts outside the apply/rollback fence.
    statements = (
        f"""
        CREATE TABLE IF NOT EXISTS {_MIGRATION_TABLE} (
            migration_id TEXT PRIMARY KEY,
            manifest_sha256 TEXT NOT NULL,
            source_digest_before TEXT NOT NULL,
            source_digest_after TEXT NOT NULL,
            identity_digest TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('applied', 'rolled_back')),
            manifest_json TEXT NOT NULL,
            statistics_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            rolled_back_at TEXT,
            rollback_source_digest TEXT,
            control_path TEXT,
            identity_path TEXT,
            planned_at TEXT
        )
        """,
        f"""
        CREATE INDEX IF NOT EXISTS idx_durable_subject_migrations_status
        ON {_MIGRATION_TABLE}(status, applied_at DESC)
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {_RECEIPT_TABLE} (
            migration_id TEXT NOT NULL REFERENCES {_MIGRATION_TABLE}(migration_id),
            table_name TEXT NOT NULL,
            source_row_id TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('mapped', 'omitted', 'quarantined')),
            before_subject TEXT,
            after_subject TEXT,
            before_row_digest TEXT NOT NULL,
            after_row_digest TEXT NOT NULL,
            reason TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (migration_id, table_name, source_row_id)
        )
        """,
        f"""
        CREATE INDEX IF NOT EXISTS idx_durable_subject_receipts_row
        ON {_RECEIPT_TABLE}(table_name, source_row_id)
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {_BACKUP_TABLE} (
            migration_id TEXT NOT NULL REFERENCES {_MIGRATION_TABLE}(migration_id),
            table_name TEXT NOT NULL,
            source_row_id TEXT NOT NULL,
            before_subject TEXT,
            after_subject TEXT,
            before_row_json TEXT NOT NULL,
            backed_up_at TEXT NOT NULL,
            PRIMARY KEY (migration_id, table_name, source_row_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {_AUDIT_TABLE} (
            audit_id TEXT PRIMARY KEY,
            migration_id TEXT NOT NULL REFERENCES {_MIGRATION_TABLE}(migration_id),
            table_name TEXT NOT NULL,
            source_row_id TEXT NOT NULL,
            action TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (migration_id, table_name, source_row_id, action)
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)

    additions = {
        "rollback_source_digest": "TEXT",
        "control_path": "TEXT",
        "identity_path": "TEXT",
        "planned_at": "TEXT",
    }
    present = set(_columns(connection, "main", _MIGRATION_TABLE))
    for name, definition in additions.items():
        if name not in present:
            connection.execute(
                f"ALTER TABLE {_quote_identifier(_MIGRATION_TABLE)} "
                f"ADD COLUMN {_quote_identifier(name)} {definition}"
            )
            present.add(name)


def _active_summary(connection: sqlite3.Connection) -> tuple[list[str], int]:
    active = _active_migrations(connection)
    ids = [str(row["migration_id"]) for row in active]
    return ids, len(ids)


def _with_active_summary(
    connection: sqlite3.Connection, report: dict[str, Any]
) -> dict[str, Any]:
    ids, count = _active_summary(connection)
    report["active_migration_ids"] = ids
    report["active_migration_count"] = count
    return report


def _migration_id(manifest_sha256: str) -> str:
    return f"durable-subject-v1-{manifest_sha256}"


def _assert_no_rolled_back_receipts(
    connection: sqlite3.Connection,
    source_rows: Iterable[_SourceRow],
) -> None:
    """Do not silently re-activate a source row revoked by an earlier run."""

    if not _table_exists(connection, "main", _RECEIPT_TABLE):
        return
    if not _table_exists(connection, "main", _MIGRATION_TABLE):
        raise DurableSubjectMigrationError(
            "no durable subject migration receipt found for existing row receipts"
        )
    source_keys = {(row.table, row.row_id) for row in source_rows}
    rows = connection.execute(
        f"SELECT r.table_name, r.source_row_id, r.migration_id, m.status "
        f"FROM {_quote_identifier(_RECEIPT_TABLE)} AS r "
        f"LEFT JOIN {_quote_identifier(_MIGRATION_TABLE)} AS m "
        "ON m.migration_id = r.migration_id "
        "ORDER BY r.migration_id, r.table_name, r.source_row_id"
    ).fetchall()
    for row in rows:
        status = row["status"]
        if status is None:
            raise DurableSubjectMigrationError(
                f"row receipt {row['migration_id']} has no durable subject migration row"
            )
        if str(status) not in {"applied", "rolled_back"}:
            raise DurableSubjectMigrationError(
                f"unsupported durable subject migration status: {status}"
            )
        key = (str(row["table_name"]), str(row["source_row_id"]))
        if str(status) == "rolled_back" and key in source_keys:
            raise DurableSubjectMigrationError(
                "source row belongs to rolled-back migration "
                f"{row['migration_id']} and cannot be reactivated: "
                f"{key[0]}:{key[1]}"
            )


def _stored_manifest(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    manifest = cast(dict[str, Any], json.loads(str(row["manifest_json"])))
    source = manifest.get("source")
    raw_tables = source.get("tables", []) if isinstance(source, Mapping) else []
    tables = [
        str(item.get("table"))
        for item in raw_tables
        if isinstance(item, Mapping) and item.get("table") is not None
    ]
    keys = set(row.keys())
    control_path = (
        row["control_path"]
        if "control_path" in keys
        else manifest.get("control_path")
    )
    identity_path = (
        row["identity_path"]
        if "identity_path" in keys
        else manifest.get("identity_path")
    )
    planned_at = (
        row["planned_at"]
        if "planned_at" in keys
        else manifest.get("planned_at")
    )
    result: dict[str, Any] = {
        "dry_run": False,
        "applied": str(row["status"]) == "applied",
        "idempotent": True,
        "control_path": str(control_path) if control_path is not None else None,
        "identity_path": str(identity_path) if identity_path is not None else None,
        "migration_id": str(row["migration_id"]),
        "manifest_sha256": str(row["manifest_sha256"]),
        "source_digest_before": str(row["source_digest_before"]),
        "source_digest_after": str(row["source_digest_after"]),
        "identity_digest": str(row["identity_digest"]),
        "tables": tables,
        "rows": manifest.get("rows", []),
        "statistics": manifest.get("statistics", {}),
        "manifest": manifest,
        "planned_at": str(planned_at) if planned_at is not None else None,
        "status": str(row["status"]),
    }
    return _with_active_summary(connection, result)


def _active_migrations(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        f"SELECT * FROM {_MIGRATION_TABLE} WHERE status = 'applied' "
        "ORDER BY applied_at, migration_id"
    ).fetchall()


def _find_migration(
    connection: sqlite3.Connection,
    *,
    migration_id: str | None = None,
    manifest_sha256: str | None = None,
) -> sqlite3.Row:
    if migration_id is None and manifest_sha256 is None:
        raise DurableSubjectMigrationError(
            "rollback requires migration_id or manifest_sha256"
        )
    by_id: sqlite3.Row | None = None
    by_manifest: sqlite3.Row | None = None
    if migration_id is not None:
        rows = connection.execute(
            f"SELECT * FROM {_MIGRATION_TABLE} WHERE migration_id = ?",
            (migration_id,),
        ).fetchall()
        if len(rows) > 1:
            raise DurableSubjectMigrationError(
                f"migration selector is ambiguous: {migration_id}"
            )
        by_id = cast(sqlite3.Row, rows[0]) if rows else None
    if manifest_sha256 is not None:
        rows = connection.execute(
            f"SELECT * FROM {_MIGRATION_TABLE} WHERE manifest_sha256 = ?",
            (manifest_sha256,),
        ).fetchall()
        if len(rows) > 1:
            raise DurableSubjectMigrationError(
                f"manifest selector is ambiguous: {manifest_sha256}"
            )
        by_manifest = cast(sqlite3.Row, rows[0]) if rows else None
    if by_id is None and by_manifest is None:
        raise DurableSubjectMigrationError(
            "no matching durable subject migration receipt found"
        )
    if migration_id is not None and manifest_sha256 is not None:
        if by_id is None or by_manifest is None or str(by_id["migration_id"]) != str(by_manifest["migration_id"]):
            raise DurableSubjectMigrationError(
                "migration selectors identify different durable subject migrations"
            )
    selected = by_id if by_id is not None else by_manifest
    if selected is None:  # pragma: no cover - guarded by the checks above
        raise DurableSubjectMigrationError(
            "no matching durable subject migration receipt found"
        )
    return selected


def _expected_receipt_count(row: sqlite3.Row) -> int | None:
    raw_statistics = row["statistics_json"]
    try:
        statistics = json.loads(str(raw_statistics))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DurableSubjectMigrationError(
            f"stored migration statistics are invalid for {row['migration_id']}"
        ) from exc
    if not isinstance(statistics, Mapping) or "row_count" not in statistics:
        return None
    try:
        count = int(statistics["row_count"])
    except (TypeError, ValueError) as exc:
        raise DurableSubjectMigrationError(
            f"stored migration row_count is invalid for {row['migration_id']}"
        ) from exc
    if count < 0:
        raise DurableSubjectMigrationError(
            f"stored migration row_count is negative for {row['migration_id']}"
        )
    return count


def _active_receipt_keys(connection: sqlite3.Connection) -> set[tuple[str, str]]:
    rows = connection.execute(
        f"SELECT r.table_name, r.source_row_id FROM {_RECEIPT_TABLE} r "
        f"JOIN {_MIGRATION_TABLE} m ON m.migration_id = r.migration_id "
        "WHERE m.status = 'applied'"
    ).fetchall()
    return {(str(row["table_name"]), str(row["source_row_id"])) for row in rows}


def _current_row(
    connection: sqlite3.Connection, table: str, row_id: str, columns: tuple[str, ...]
) -> dict[str, Any]:
    rows = connection.execute(
        f"SELECT * FROM {_qualified('main', table)} WHERE {_quote_identifier('event_id')} = ?",
        (row_id,),
    ).fetchall()
    if len(rows) != 1:
        raise DurableSubjectMigrationError(
            f"source row {table}:{row_id} is missing or not uniquely addressable"
        )
    return _row_values(rows[0], columns)


def _validate_active_receipts(
    connection: sqlite3.Connection,
    tables: tuple[_TableInfo, ...],
) -> None:
    table_by_name = _table_map(tables)
    receipts = connection.execute(
        f"SELECT r.* FROM {_RECEIPT_TABLE} r "
        f"JOIN {_MIGRATION_TABLE} m ON m.migration_id = r.migration_id "
        "WHERE m.status = 'applied' ORDER BY r.migration_id, r.table_name, r.source_row_id"
    ).fetchall()
    for receipt in receipts:
        table_name = str(receipt["table_name"])
        table = table_by_name.get(table_name)
        if table is None:
            raise DurableSubjectMigrationError(
                f"previous migration source table disappeared: {table_name}"
            )
        values = _current_row(connection, table_name, str(receipt["source_row_id"]), table.columns)
        digest = _row_digest(table_name, table.columns, values)
        if digest != str(receipt["after_row_digest"]):
            raise DurableSubjectMigrationError(
                f"target drift detected for {table_name}:{receipt['source_row_id']}"
            )


def _insert_audit(
    connection: sqlite3.Connection,
    migration_id: str,
    table_name: str,
    source_row_id: str,
    action: str,
    payload: Mapping[str, object],
    now_iso: str,
) -> None:
    audit_id = str(uuid.uuid4())
    connection.execute(
        f"INSERT OR IGNORE INTO {_AUDIT_TABLE} ("
        "audit_id, migration_id, table_name, source_row_id, action, payload_json, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (audit_id, migration_id, table_name, source_row_id, action, _canonical(payload), now_iso),
    )


def _apply_rows(
    connection: sqlite3.Connection,
    report: Mapping[str, Any],
    migration_id: str,
    now_iso: str,
) -> None:
    tables = {
        table.name: table
        for table in _source_tables(connection)
    }
    for raw_entry in cast(list[dict[str, Any]], report["rows"]):
        table_name = str(raw_entry["table"])
        row_id = str(raw_entry["source_row_id"])
        table = tables.get(table_name)
        if table is None:
            raise DurableSubjectMigrationError(f"source table disappeared during apply: {table_name}")
        values = _current_row(connection, table_name, row_id, table.columns)
        current_digest = _row_digest(table_name, table.columns, values)
        if current_digest != str(raw_entry["before_row_digest"]):
            raise DurableSubjectMigrationError(
                f"source drift detected during apply for {table_name}:{row_id}"
            )
        outcome = str(raw_entry["outcome"])
        before_subject = _normalise_subject(values.get("subject_id"))
        after_subject = _normalise_subject(raw_entry.get("after_subject"))
        if outcome == "mapped" and before_subject != after_subject:
            connection.execute(
                f"UPDATE {_qualified('main', table_name)} SET {_quote_identifier('subject_id')} = ? "
                f"WHERE {_quote_identifier('event_id')} = ?",
                (after_subject, row_id),
            )
            values["subject_id"] = after_subject
            actual_after_digest = _row_digest(table_name, table.columns, values)
            if actual_after_digest != str(raw_entry["after_row_digest"]):
                raise DurableSubjectMigrationError(
                    f"post-write digest mismatch for {table_name}:{row_id}"
                )
            connection.execute(
                f"INSERT INTO {_BACKUP_TABLE} ("
                "migration_id, table_name, source_row_id, before_subject, after_subject, "
                "before_row_json, backed_up_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    migration_id,
                    table_name,
                    row_id,
                    before_subject,
                    after_subject,
                    _canonical({column: _json_safe(value) for column, value in values.items() if column != "subject_id"} | {"subject_id": before_subject}),
                    now_iso,
                ),
            )
            action = "mapped"
        else:
            action = outcome
        connection.execute(
            f"INSERT INTO {_RECEIPT_TABLE} ("
            "migration_id, table_name, source_row_id, outcome, before_subject, after_subject, "
            "before_row_digest, after_row_digest, reason, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                migration_id,
                table_name,
                row_id,
                outcome,
                before_subject,
                after_subject,
                str(raw_entry["before_row_digest"]),
                str(raw_entry["after_row_digest"]),
                str(raw_entry["reason"]),
                now_iso,
            ),
        )
        _insert_audit(
            connection,
            migration_id,
            table_name,
            row_id,
            f"migration.durable_subject.{action}",
            {
                "before_subject": before_subject,
                "after_subject": after_subject,
                "reason": str(raw_entry["reason"]),
                "outcome": outcome,
            },
            now_iso,
        )


def apply(
    control_path: str | Path,
    identity_path: str | Path | None = None,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply the current plan once, or return the prior receipt idempotently."""
    if dry_run:
        return dry_run_report(control_path, identity_path)
    timestamp = _now(now)
    initial = _build_report(
        control_path, identity_path, now=timestamp, read_only=True
    )
    control = Path(control_path).expanduser().resolve()
    identity = Path(identity_path).expanduser().resolve() if identity_path is not None else control
    connection, identity_schema = _connect(control, identity, read_only=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _build_report_on_connection(connection, control, identity, identity_schema, timestamp)
        if (
            current["source_digest_before"] != initial["source_digest_before"]
            or current["identity_digest"] != initial["identity_digest"]
        ):
            raise DurableSubjectMigrationError("source or identity drifted before apply")
        _support_tables(connection)
        active = _active_migrations(connection)
        for migration in active:
            if str(migration["identity_digest"]) != str(current["identity_digest"]):
                raise DurableSubjectMigrationError("identity authority drifted after a prior apply")
        _validate_active_receipts(connection, _source_tables(connection))
        excluded = _active_receipt_keys(connection)
        if not any(
            (str(entry["table"]), str(entry["source_row_id"])) not in excluded
            for entry in cast(list[dict[str, Any]], current["rows"])
        ):
            if active:
                result = _stored_manifest(connection, active[-1])
                connection.commit()
                return result
            connection.commit()
            return _with_active_summary(connection, initial)

        report = _build_report_on_connection(
            connection, control, identity, identity_schema, timestamp, excluded_keys=excluded
        )
        migration_id = _migration_id(str(report["manifest_sha256"]))
        recorded = connection.execute(
            f"SELECT * FROM {_MIGRATION_TABLE} WHERE migration_id = ? "
            "OR manifest_sha256 = ? LIMIT 1",
            (migration_id, str(report["manifest_sha256"])),
        ).fetchone()
        if recorded is not None:
            status = str(recorded["status"])
            if status == "rolled_back":
                raise DurableSubjectMigrationError(
                    f"migration {migration_id} was rolled back and cannot be reactivated"
                )
            if status != "applied":
                raise DurableSubjectMigrationError(
                    f"unsupported stored migration status: {status}"
                )
            raise DurableSubjectMigrationError(
                "migration is already recorded but its row receipts do not cover "
                f"the planned rows: {migration_id}"
            )
        now_iso = timestamp.isoformat()
        manifest = cast(dict[str, Any], report["manifest"])
        connection.execute(
            f"INSERT INTO {_MIGRATION_TABLE} ("
            "migration_id, manifest_sha256, source_digest_before, source_digest_after, "
            "identity_digest, status, manifest_json, statistics_json, created_at, applied_at, "
            "control_path, identity_path, planned_at"
            ") VALUES (?, ?, ?, ?, ?, 'applied', ?, ?, ?, ?, ?, ?, ?)",
            (
                migration_id,
                str(report["manifest_sha256"]),
                str(report["source_digest_before"]),
                str(report["source_digest_after"]),
                str(report["identity_digest"]),
                _canonical(manifest),
                _canonical(report["statistics"]),
                now_iso,
                now_iso,
                str(report["control_path"]),
                str(report["identity_path"]),
                str(report["planned_at"]),
            ),
        )
        _apply_rows(connection, report, migration_id, now_iso)
        connection.commit()
        report["dry_run"] = False
        report["applied"] = True
        report["migration_id"] = migration_id
        report["status"] = "applied"
        report["idempotent"] = False
        return _with_active_summary(connection, report)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _build_report_on_connection(
    connection: sqlite3.Connection,
    control_path: Path,
    identity_path: Path,
    identity_schema: str,
    now: datetime,
    *,
    excluded_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    tables = _source_tables(connection)
    source_rows = _read_source_rows(connection, tables)
    _assert_no_rolled_back_receipts(connection, source_rows)
    authority = _load_identity(connection, identity_schema)
    accounts = _registered_accounts(connection)
    return _manifest_report(
        control_path=control_path,
        identity_path=identity_path,
        tables=tables,
        source_rows=source_rows,
        identity=authority,
        registered_accounts=accounts,
        excluded_keys=excluded_keys or set(),
        now=now,
    )


def dry_run_report(
    control_path: str | Path, identity_path: str | Path | None = None
) -> dict[str, Any]:
    return dry_run(control_path, identity_path)


def _migration_receipts(
    connection: sqlite3.Connection, migration_id: str
) -> list[sqlite3.Row]:
    if not _table_exists(connection, "main", _RECEIPT_TABLE):
        raise DurableSubjectMigrationError(
            "no durable subject migration receipt found for migration "
            f"{migration_id}"
        )
    receipts = connection.execute(
        f"SELECT * FROM {_quote_identifier(_RECEIPT_TABLE)} "
        "WHERE migration_id = ? ORDER BY table_name, source_row_id",
        (migration_id,),
    ).fetchall()
    if not receipts:
        raise DurableSubjectMigrationError(
            "no durable subject migration receipt found for migration "
            f"{migration_id}"
        )
    migration = connection.execute(
        f"SELECT statistics_json FROM {_quote_identifier(_MIGRATION_TABLE)} "
        "WHERE migration_id = ?",
        (migration_id,),
    ).fetchone()
    if migration is None:
        raise DurableSubjectMigrationError(
            f"no durable subject migration receipt found for migration {migration_id}"
        )
    expected_count = _expected_receipt_count(migration)
    if expected_count is not None and len(receipts) != expected_count:
        raise DurableSubjectMigrationError(
            f"incomplete durable subject migration receipts for {migration_id}: "
            f"expected {expected_count}, found {len(receipts)}"
        )
    return receipts


def _require_backup(
    connection: sqlite3.Connection,
    receipt: sqlite3.Row,
    table: _TableInfo,
) -> sqlite3.Row:
    row = connection.execute(
        f"SELECT * FROM {_quote_identifier(_BACKUP_TABLE)} "
        "WHERE migration_id = ? AND table_name = ? AND source_row_id = ?",
        (
            str(receipt["migration_id"]),
            str(receipt["table_name"]),
            str(receipt["source_row_id"]),
        ),
    ).fetchone()
    if row is None:
        raise DurableSubjectMigrationError(
            "backup receipt is missing for "
            f"{receipt['table_name']}:{receipt['source_row_id']}"
        )
    if row["before_subject"] != receipt["before_subject"] or row["after_subject"] != receipt[
        "after_subject"
    ]:
        raise DurableSubjectMigrationError(
            "backup receipt disagrees with row receipt for "
            f"{receipt['table_name']}:{receipt['source_row_id']}"
        )
    try:
        before_values = json.loads(str(row["before_row_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DurableSubjectMigrationError(
            "backup receipt contains invalid row JSON for "
            f"{receipt['table_name']}:{receipt['source_row_id']}"
        ) from exc
    if not isinstance(before_values, Mapping):
        raise DurableSubjectMigrationError(
            "backup receipt row JSON is not an object for "
            f"{receipt['table_name']}:{receipt['source_row_id']}"
        )
    if set(before_values) != set(table.columns):
        raise DurableSubjectMigrationError(
            "backup receipt columns disagree with source table for "
            f"{receipt['table_name']}:{receipt['source_row_id']}"
        )
    if before_values.get("subject_id") != receipt["before_subject"]:
        raise DurableSubjectMigrationError(
            "backup receipt subject disagrees with row receipt for "
            f"{receipt['table_name']}:{receipt['source_row_id']}"
        )
    if _row_digest(table.name, table.columns, before_values) != str(receipt["before_row_digest"]):
        raise DurableSubjectMigrationError(
            "backup receipt digest disagrees with row receipt for "
            f"{receipt['table_name']}:{receipt['source_row_id']}"
        )
    return cast(sqlite3.Row, row)


def _verify_rollback_source(
    connection: sqlite3.Connection,
    receipts: Iterable[sqlite3.Row],
    table_by_name: Mapping[str, _TableInfo],
    *,
    restored: bool,
) -> list[str]:
    """Verify only the rows owned by one migration's receipts."""

    restored_rows: list[str] = []
    for receipt in receipts:
        table_name = str(receipt["table_name"])
        table = table_by_name.get(table_name)
        if table is None:
            raise DurableSubjectMigrationError(
                f"rollback source table disappeared: {table_name}"
            )
        row_id = str(receipt["source_row_id"])
        values = _current_row(connection, table_name, row_id, table.columns)
        current_digest = _row_digest(table_name, table.columns, values)
        expected_digest = (
            str(receipt["before_row_digest"])
            if restored
            else str(receipt["after_row_digest"])
        )
        if current_digest != expected_digest:
            detail = "rollback source drift detected" if restored else "target drift detected"
            suffix = " after rollback" if restored else "; refusing rollback"
            raise DurableSubjectMigrationError(
                f"{detail} for {table_name}:{row_id}{suffix}"
            )
        if (
            str(receipt["outcome"]) == "mapped"
            and receipt["before_subject"] != receipt["after_subject"]
        ):
            _require_backup(connection, receipt, table)
            restored_rows.append(f"{table_name}:{row_id}")
    return restored_rows


def _current_source_digest(
    connection: sqlite3.Connection, tables: tuple[_TableInfo, ...]
) -> str:
    return _source_digest(tables, _read_source_rows(connection, tables))


def _rolled_back_report(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    receipts: list[sqlite3.Row],
    table_by_name: Mapping[str, _TableInfo],
) -> dict[str, Any]:
    restored_rows = _verify_rollback_source(
        connection, receipts, table_by_name, restored=True
    )
    tables = tuple(table_by_name.values())
    rollback_source_digest = (
        str(row["rollback_source_digest"])
        if row["rollback_source_digest"] is not None
        else _current_source_digest(connection, tables)
    )
    result = {
        "dry_run": False,
        "rolled_back": True,
        "idempotent": True,
        "migration_id": str(row["migration_id"]),
        "manifest_sha256": str(row["manifest_sha256"]),
        "restored_count": len(restored_rows),
        "restored_rows": restored_rows,
        "source_digest_after": str(row["source_digest_after"]),
        "rollback_source_digest": rollback_source_digest,
        "status": "rolled_back",
    }
    return _with_active_summary(connection, result)


def rollback(
    control_path: str | Path,
    identity_path: str | Path | None = None,
    *,
    migration_id: str | None = None,
    manifest_sha256: str | None = None,
    expected_source_after_digest: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rollback one applied migration after verifying every target row digest."""
    del identity_path  # kept in the public API for symmetric operator tooling
    if migration_id is None and manifest_sha256 is None:
        raise DurableSubjectMigrationError(
            "rollback requires migration_id or manifest_sha256"
        )
    timestamp = _now(now)
    control = Path(control_path).expanduser().resolve()
    connection, _ = _connect(control, control, read_only=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if not _table_exists(connection, "main", _MIGRATION_TABLE):
            raise DurableSubjectMigrationError("no durable subject migration receipt found")
        # This is an in-transaction, repeatable schema upgrade.  In
        # particular, old receipts gain rollback_source_digest without
        # committing a partially restored source.
        _support_tables(connection)
        row = _find_migration(
            connection,
            migration_id=migration_id,
            manifest_sha256=manifest_sha256,
        )
        if expected_source_after_digest is not None and str(row["source_digest_after"]) != expected_source_after_digest:
            raise DurableSubjectMigrationError("expected source-after digest does not match migration receipt")
        tables = _source_tables(connection)
        table_by_name = _table_map(tables)
        receipts = _migration_receipts(connection, str(row["migration_id"]))
        status = str(row["status"])
        if status == "rolled_back":
            result = _rolled_back_report(connection, row, receipts, table_by_name)
            connection.commit()
            return result
        if status != "applied":
            raise DurableSubjectMigrationError(
                f"unsupported durable subject migration status: {status}"
            )

        _verify_rollback_source(connection, receipts, table_by_name, restored=False)
        restored: list[str] = []
        now_iso = timestamp.isoformat()
        for receipt in receipts:
            if str(receipt["outcome"]) != "mapped" or receipt["before_subject"] == receipt["after_subject"]:
                continue
            table_name = str(receipt["table_name"])
            row_id = str(receipt["source_row_id"])
            connection.execute(
                f"UPDATE {_qualified('main', table_name)} SET {_quote_identifier('subject_id')} = ? "
                f"WHERE {_quote_identifier('event_id')} = ?",
                (receipt["before_subject"], row_id),
            )
            values = _current_row(connection, table_name, row_id, table_by_name[table_name].columns)
            restored_digest = _row_digest(table_name, table_by_name[table_name].columns, values)
            if restored_digest != str(receipt["before_row_digest"]):
                raise DurableSubjectMigrationError(f"rollback restore digest mismatch for {table_name}:{row_id}")
            _insert_audit(
                connection,
                str(row["migration_id"]),
                table_name,
                row_id,
                "migration.durable_subject.rollback",
                {
                    "restored_subject": receipt["before_subject"],
                    "from_subject": receipt["after_subject"],
                },
                now_iso,
            )
            restored.append(f"{table_name}:{row_id}")
        rollback_source_digest = _current_source_digest(connection, tables)
        connection.execute(
            f"UPDATE {_MIGRATION_TABLE} SET status = 'rolled_back', rolled_back_at = ?, "
            "rollback_source_digest = ? WHERE migration_id = ?",
            (now_iso, rollback_source_digest, str(row["migration_id"])),
        )
        connection.commit()
        result = {
            "dry_run": False,
            "rolled_back": True,
            "idempotent": False,
            "migration_id": str(row["migration_id"]),
            "manifest_sha256": str(row["manifest_sha256"]),
            "restored_count": len(restored),
            "restored_rows": restored,
            "source_digest_after": str(row["source_digest_after"]),
            "rollback_source_digest": rollback_source_digest,
            "status": "rolled_back",
        }
        return _with_active_summary(connection, result)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


_SUBJECT_READ_LIMIT = 50


def read_subject(
    control_path: str | Path,
    identity_path: str | Path | None = None,
    *,
    subject_id: str,
    limit: int = _SUBJECT_READ_LIMIT,
) -> dict[str, Any]:
    """Read what one subject owns after the in-place attribution (read-only).

    The migration rewrites ``subject_id`` on the evidence rows themselves, so
    the read path answers from those rows: every evidence table that carries
    the subject plus the receipts the journal wrote for it.  A missing database
    or journal answers with zero rows, and a row whose ``subject_id`` is NULL or
    another subject is never reported for this one.
    """

    subject = subject_id.strip()
    if not subject:
        raise DurableSubjectMigrationError("read_subject requires a subject_id")
    if limit < 1:
        raise DurableSubjectMigrationError("read_subject limit must be positive")
    control = Path(control_path).expanduser()
    report: dict[str, Any] = {
        "scope": "durable_subject",
        "control_path": str(control),
        "identity_path": (
            str(Path(identity_path).expanduser()) if identity_path is not None else None
        ),
        "subject_id": subject,
        "journal_present": False,
        "tables": {},
        "receipts": {"total": 0, "by_outcome": {}, "rows": []},
        "truncated": False,
    }
    if not control.exists():
        return {**report, "reason": "database_missing"}
    connection, _ = _connect(control, identity_path, read_only=True)
    try:
        try:
            source_tables = _source_tables(connection)
        except DurableSubjectMigrationError:
            return {**report, "reason": "no_evidence_source"}
        tables: dict[str, Any] = {}
        truncated = False
        for info in source_tables:
            table = _quote_identifier(info.name)
            total = int(
                connection.execute(
                    f"SELECT count(*) FROM {table} WHERE subject_id = ?",
                    (subject,),
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT event_id, account_id FROM {table} WHERE subject_id = ? "
                "ORDER BY event_id LIMIT ?",
                (subject, limit),
            ).fetchall()
            tables[info.name] = {
                "count": total,
                "rows": [
                    {
                        "event_id": str(row["event_id"]),
                        "account_id": str(row["account_id"]),
                    }
                    for row in rows
                ],
            }
            truncated = truncated or total > len(rows)
        report["tables"] = tables
        report["truncated"] = truncated
        if not _table_exists(connection, "main", _RECEIPT_TABLE):
            return report
        report["journal_present"] = _table_exists(connection, "main", _MIGRATION_TABLE)
        by_outcome = {
            str(row["outcome"]): int(row["n"])
            for row in connection.execute(
                f"SELECT outcome, count(*) AS n FROM {_quote_identifier(_RECEIPT_TABLE)} "
                "WHERE after_subject = ? GROUP BY outcome",
                (subject,),
            ).fetchall()
        }
        receipts = connection.execute(
            "SELECT migration_id, table_name, source_row_id, outcome, before_subject, "
            f"after_subject, reason, recorded_at FROM {_quote_identifier(_RECEIPT_TABLE)} "
            "WHERE after_subject = ? "
            "ORDER BY recorded_at DESC, table_name, source_row_id LIMIT ?",
            (subject, limit),
        ).fetchall()
        report["receipts"] = {
            "total": sum(by_outcome.values()),
            "by_outcome": by_outcome,
            "rows": [dict(row) for row in receipts],
        }
        return report
    finally:
        connection.close()


__all__ = [
    "DurableSubjectMigrationError",
    "apply",
    "dry_run",
    "plan",
    "read_subject",
    "rollback",
]
