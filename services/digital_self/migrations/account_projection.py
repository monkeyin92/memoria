"""Operator-invoked projection of account-keyed Digital Self rows onto subjects.

Digital Self versions and their lifecycle audit trail are keyed by
``account_id`` (:mod:`services.digital_self.registry`); the multi-subject model
keys memory and persona data by a durable ``subject_id``.  This module copies
both source tables into subject-keyed projection tables and never touches the
source rows: the projected row keeps ``manifest_json`` byte for byte and
repeats every digest, parent and rollback reference, and every lifecycle audit
event with its action, status transition and timestamp.

The seam is deliberately SQLite-only and operator-invoked.  It is not called
from application startup and it does not change any read path: the product
keeps reading the account-keyed tables until a separate slice switches reads.

A version is projected only when the whole evidence chain holds:

* the account is registered in the Control store (``accounts``/``profiles``);
* ``identity_persons[account_id]`` exists and is ``active``;
* owner evidence exists - an active binding owned by that person, an active
  ``account_owner`` role for that person, or a ``self`` relationship for it;
* the stored manifest decodes through
  :func:`services.digital_self.compiler.decode_manifest` with both stored
  digests and both stored references, which also proves the copied bytes are
  the canonical bytes the digests were taken over;
* every evidence id referenced by the manifest resolves to
  ``evidence_events.subject_id`` and all of them are that same owner.

Rows that fail a lower bar are ``quarantined`` (evidence contradicts the owner,
for example a member subject inside the manifest) or ``omitted`` (no subject
evidence at all yet).  Every scanned row receives a receipt, so a later run
skips it until the operator rolls that receipt back.  Rolling a projection
back only removes derived rows, which is why - unlike the in-place
``services.identity.migrations.durable_subject`` migration - a rolled-back
receipt may be re-projected here.

``status`` is the single mutable field of a version (the source trigger allows
exactly that), so a receipt-covered row whose content is unchanged but whose
status moved is *refreshed* in place; a changed content digest is drift and
refuses the run instead of silently rewriting the projection.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from services.digital_self.compiler import decode_manifest
from services.digital_self.domain import (
    CognitiveClaimManifestEntry,
    DecisionCaseManifestEntry,
    DigitalSelfManifest,
    ManifestIntegrityError,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    RelationshipProfileManifestEntry,
)

Outcome = Literal[
    "mapped", "refreshed", "omitted", "quarantined", "already_projected"
]

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SCOPE = "digital_self_subject_projection"
_RULES_VERSION = 1
_VERSION_TABLE = "digital_self_versions"
_AUDIT_TABLE = "digital_self_lifecycle_audit_events"
_EVIDENCE_TABLE = "evidence_events"
_PROJECTED_VERSION_TABLE = "digital_self_subject_versions"
_PROJECTED_AUDIT_TABLE = "digital_self_subject_lifecycle_audit_events"
_MIGRATION_TABLE = "digital_self_projection_migrations"
_RECEIPT_TABLE = "digital_self_projection_receipts"
_QUARANTINE_TABLE = "digital_self_projection_quarantine"
_BACKUP_TABLE = "digital_self_projection_backups"
_AUDIT_EVENT_TABLE = "digital_self_projection_audit_events"
_IDENTITY_SCHEMA = "identity_authority"
_CONTROL_SCHEMA = "control_accounts"
_VERSION_STATUSES = ("draft", "testing", "approved", "frozen", "revoked")
_ACTIONS = ("build", "begin_testing", "approve", "freeze", "revoke", "rollback")
_STATUS_VALUES = "('draft', 'testing', 'approved', 'frozen', 'revoked')"
_ACTION_VALUES = "('build', 'begin_testing', 'approve', 'freeze', 'revoke', 'rollback')"
_VERSION_COLUMNS = (
    "version_id",
    "account_id",
    "version_number",
    "status",
    "manifest_json",
    "manifest_sha256",
    "source_summary_sha256",
    "parent_version_id",
    "rollback_target_version_id",
    "created_at",
)
_VERSION_CONTENT_FIELDS = (
    "version_id",
    "subject_id",
    "account_id",
    "version_number",
    "manifest_json",
    "manifest_sha256",
    "source_summary_sha256",
    "parent_version_id",
    "rollback_target_version_id",
    "created_at",
)
_AUDIT_COLUMNS = (
    "event_id",
    "account_id",
    "actor_account_id",
    "action",
    "version_id",
    "manifest_sha256",
    "from_status",
    "to_status",
    "target_version_id",
    "new_version_id",
    "occurred_at",
)
_AUDIT_CONTENT_FIELDS = ("subject_id", *_AUDIT_COLUMNS)
_PROJECTED_VERSION_COLUMNS = (
    "version_id",
    "subject_id",
    "account_id",
    "version_number",
    "status",
    "manifest_json",
    "manifest_sha256",
    "source_summary_sha256",
    "parent_version_id",
    "rollback_target_version_id",
    "created_at",
    "projection_migration_id",
    "projected_at",
)
_PROJECTED_AUDIT_COLUMNS = (
    "event_id",
    "subject_id",
    "account_id",
    "actor_account_id",
    "action",
    "version_id",
    "manifest_sha256",
    "from_status",
    "to_status",
    "target_version_id",
    "new_version_id",
    "occurred_at",
    "projection_migration_id",
    "projected_at",
)
_PROJECTED_VERSION_CONTENT_FIELDS = (
    "version_id",
    "subject_id",
    "account_id",
    "version_number",
    "manifest_json",
    "manifest_sha256",
    "source_summary_sha256",
    "parent_version_id",
    "rollback_target_version_id",
    "created_at",
)
_PROJECTED_VERSION_STATE_COLUMNS = (*_PROJECTED_VERSION_CONTENT_FIELDS, "status")
_REGISTRATION_TABLES = (("accounts", "user_id"), ("profiles", "user_id"))
_IDENTITY_TABLES = (
    ("identity_persons", ("person_id", "status")),
    (
        "identity_device_bindings",
        ("binding_id", "account_owner_person_id", "status"),
    ),
    (
        "identity_device_binding_roles",
        ("binding_id", "person_id", "role", "status"),
    ),
    (
        "identity_relationships",
        ("source_person_id", "target_person_id", "relation_type", "status"),
    ),
)
_MIGRATION_STATUSES = "('applied', 'rolling_back', 'rolled_back')"
_RECEIPT_OUTCOMES = "('mapped', 'refreshed', 'omitted', 'quarantined')"


class DigitalSelfProjectionError(RuntimeError):
    """Raised when the projection cannot prove a safe, non-drifting plan."""


@dataclass(frozen=True, slots=True)
class _SourceState:
    """The Digital Self rows of one database plus their evidence subjects."""

    versions: tuple[dict[str, Any], ...]
    audit_events: tuple[dict[str, Any], ...]
    manifests: Mapping[str, DigitalSelfManifest | None]
    manifest_errors: Mapping[str, str]
    evidence_subjects: Mapping[str, str | None]
    missing_evidence_ids: frozenset[str]
    digest: str


@dataclass(frozen=True, slots=True)
class _IdentityState:
    """The Identity rows that decide whether an account may become a subject."""

    persons: Mapping[str, Mapping[str, Any]]
    bindings: tuple[Mapping[str, Any], ...]
    binding_roles: tuple[Mapping[str, Any], ...]
    relationships: tuple[Mapping[str, Any], ...]
    tables: tuple[str, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class _Coverage:
    """Receipts of earlier runs, split by the status of their migration."""

    applied: Mapping[tuple[str, str], Mapping[str, Any]]
    rolled_back: Mapping[tuple[str, str], Mapping[str, Any]]
    subject_by_version: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Entry:
    """One classified source row and its planned projection."""

    table: str
    row_id: str
    account_id: str | None
    subject_id: str | None
    outcome: Outcome
    reason: str
    details: tuple[str, ...]
    source_digest: str
    target_digest: str | None
    source_row: dict[str, Any]
    target_row: dict[str, Any] | None
    evidence_ids: tuple[str, ...] = ()
    before_status: str | None = None
    after_status: str | None = None


@dataclass(frozen=True, slots=True)
class _RunPlan:
    """The classification of every source row for one migration run."""

    entries: tuple[_Entry, ...]
    pending: tuple[_Entry, ...]
    statistics: dict[str, Any]


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise DigitalSelfProjectionError(f"unsafe SQLite identifier: {value!r}")
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


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("migration timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _same_path(first: Path, second: Path) -> bool:
    return first.expanduser().resolve() == second.expanduser().resolve()


def _digest_payload(table: str, columns: Sequence[str], values: Mapping[str, Any]) -> str:
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


_SUPPORT_SCHEMA = (
    f"""
    CREATE TABLE IF NOT EXISTS {_MIGRATION_TABLE} (
        migration_id TEXT PRIMARY KEY,
        manifest_sha256 TEXT NOT NULL,
        scope TEXT NOT NULL,
        source_digest_before TEXT NOT NULL,
        source_digest_after TEXT NOT NULL,
        target_digest_before TEXT NOT NULL,
        target_digest_after TEXT NOT NULL,
        identity_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN {_MIGRATION_STATUSES}),
        manifest_json TEXT NOT NULL,
        statistics_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        rolled_back_at TEXT,
        rollback_target_digest TEXT,
        source_path TEXT,
        identity_path TEXT,
        control_path TEXT,
        planned_at TEXT NOT NULL
    )
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_digital_self_projection_migrations_status
    ON {_MIGRATION_TABLE}(status, applied_at DESC)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_RECEIPT_TABLE} (
        migration_id TEXT NOT NULL REFERENCES {_MIGRATION_TABLE}(migration_id),
        table_name TEXT NOT NULL,
        source_row_id TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN {_RECEIPT_OUTCOMES}),
        subject_id TEXT,
        before_status TEXT,
        after_status TEXT,
        source_row_digest TEXT NOT NULL,
        target_row_digest TEXT,
        reason TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY (migration_id, table_name, source_row_id)
    )
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_digital_self_projection_receipts_row
    ON {_RECEIPT_TABLE}(table_name, source_row_id)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_QUARANTINE_TABLE} (
        migration_id TEXT NOT NULL,
        table_name TEXT NOT NULL,
        source_row_id TEXT NOT NULL,
        account_id TEXT,
        subject_id TEXT,
        outcome TEXT NOT NULL,
        reason TEXT NOT NULL,
        details_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY (migration_id, table_name, source_row_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_BACKUP_TABLE} (
        migration_id TEXT NOT NULL,
        table_name TEXT NOT NULL,
        source_row_id TEXT NOT NULL,
        source_row_json TEXT NOT NULL,
        source_row_digest TEXT NOT NULL,
        target_row_json TEXT,
        target_row_digest TEXT,
        evidence_ids_json TEXT NOT NULL DEFAULT '[]',
        recorded_at TEXT NOT NULL,
        PRIMARY KEY (migration_id, table_name, source_row_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_AUDIT_EVENT_TABLE} (
        audit_id TEXT PRIMARY KEY,
        migration_id TEXT NOT NULL,
        table_name TEXT NOT NULL,
        source_row_id TEXT NOT NULL,
        action TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (migration_id, table_name, source_row_id, action)
    )
    """,
)


def _version_content_digest(values: Mapping[str, Any]) -> str:
    return _digest_payload(_VERSION_TABLE, _VERSION_CONTENT_FIELDS, values)


def _audit_content_digest(values: Mapping[str, Any]) -> str:
    return _digest_payload(_AUDIT_TABLE, _AUDIT_CONTENT_FIELDS, values)


def _source_digest(
    versions: Iterable[Mapping[str, Any]], audit_events: Iterable[Mapping[str, Any]]
) -> str:
    return _sha256(
        {
            "versions": [
                {
                    "version_id": str(row["version_id"]),
                    "digest": _version_content_digest(row),
                    "status": str(row["status"]),
                }
                for row in sorted(versions, key=lambda item: str(item["version_id"]))
            ],
            "audit_events": [
                {
                    "event_id": str(row["event_id"]),
                    "digest": _audit_content_digest(row),
                }
                for row in sorted(audit_events, key=lambda item: str(item["event_id"]))
            ],
        }
    )

_PROJECTION_SCHEMA = (
    f"""
    CREATE TABLE IF NOT EXISTS {_PROJECTED_VERSION_TABLE} (
        version_id TEXT PRIMARY KEY,
        subject_id TEXT NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 128),
        account_id TEXT NOT NULL,
        version_number INTEGER NOT NULL CHECK (version_number > 0),
        status TEXT NOT NULL CHECK (status IN {_STATUS_VALUES}),
        manifest_json TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
        source_summary_sha256 TEXT NOT NULL
            CHECK (length(source_summary_sha256) = 64),
        parent_version_id TEXT,
        rollback_target_version_id TEXT,
        created_at TEXT NOT NULL,
        projection_migration_id TEXT NOT NULL,
        projected_at TEXT NOT NULL,
        UNIQUE (subject_id, version_id),
        UNIQUE (subject_id, version_number)
    )
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_digital_self_subject_versions_status
    ON {_PROJECTED_VERSION_TABLE}(subject_id, status, version_number DESC)
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS digital_self_subject_versions_immutable
    BEFORE UPDATE OF version_id, subject_id, account_id, version_number,
                     manifest_json, manifest_sha256, source_summary_sha256,
                     parent_version_id, rollback_target_version_id, created_at,
                     projection_migration_id
    ON {_PROJECTED_VERSION_TABLE}
    BEGIN
        SELECT RAISE(ABORT, 'projected digital self manifest is immutable');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS digital_self_subject_versions_delete_guard
    BEFORE DELETE ON {_PROJECTED_VERSION_TABLE}
    WHEN NOT EXISTS (
        SELECT 1 FROM {_MIGRATION_TABLE}
        WHERE migration_id = OLD.projection_migration_id
          AND status = 'rolling_back'
    )
    BEGIN
        SELECT RAISE(ABORT, 'projected rows are removed only by their own rollback');
    END
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_PROJECTED_AUDIT_TABLE} (
        event_id TEXT PRIMARY KEY,
        subject_id TEXT NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 128),
        account_id TEXT NOT NULL,
        actor_account_id TEXT NOT NULL CHECK (actor_account_id = account_id),
        action TEXT NOT NULL CHECK (action IN {_ACTION_VALUES}),
        version_id TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
        from_status TEXT CHECK (
            from_status IS NULL OR from_status IN {_STATUS_VALUES}
        ),
        to_status TEXT NOT NULL CHECK (to_status IN {_STATUS_VALUES}),
        target_version_id TEXT,
        new_version_id TEXT,
        occurred_at TEXT NOT NULL,
        projection_migration_id TEXT NOT NULL,
        projected_at TEXT NOT NULL
    )
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_digital_self_subject_audit_subject
    ON {_PROJECTED_AUDIT_TABLE}(subject_id, occurred_at, event_id)
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS digital_self_subject_audit_immutable
    BEFORE UPDATE ON {_PROJECTED_AUDIT_TABLE}
    BEGIN
        SELECT RAISE(ABORT, 'projected lifecycle audit events are immutable');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS digital_self_subject_audit_delete_guard
    BEFORE DELETE ON {_PROJECTED_AUDIT_TABLE}
    WHEN NOT EXISTS (
        SELECT 1 FROM {_MIGRATION_TABLE}
        WHERE migration_id = OLD.projection_migration_id
          AND status = 'rolling_back'
    )
    BEGIN
        SELECT RAISE(ABORT, 'projected rows are removed only by their own rollback');
    END
    """,
)

_SUPPORT_MIGRATION_ADDITIONS: Mapping[str, str] = {
    "source_path": "TEXT",
    "identity_path": "TEXT",
    "control_path": "TEXT",
    "planned_at": "TEXT",
}


def _ensure_projection_schema(connection: sqlite3.Connection) -> None:
    """Create the projection tables inside the caller's transaction.

    ``executescript`` is deliberately avoided: it issues an implicit COMMIT
    and would push the DDL outside the apply/rollback fence.
    """

    for statement in _SUPPORT_SCHEMA:
        connection.execute(statement)
    present = set(_columns(connection, "main", _MIGRATION_TABLE))
    for name, definition in _SUPPORT_MIGRATION_ADDITIONS.items():
        if name not in present:
            connection.execute(
                f"ALTER TABLE {_quote_identifier(_MIGRATION_TABLE)} "
                f"ADD COLUMN {_quote_identifier(name)} {definition}"
            )
            present.add(name)
    for statement in _PROJECTION_SCHEMA:
        connection.execute(statement)


def _read_only_uri(path: Path) -> str:
    return f"{path.expanduser().resolve().as_uri()}?mode=ro"


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise DigitalSelfProjectionError(
            f"Digital Self SQLite database does not exist: {path}"
        )
    connection = sqlite3.connect(_read_only_uri(path), uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _connect_write(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, uri=True, isolation_level=None, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _attach_read_only(
    connection: sqlite3.Connection, path: Path, schema: str, current: Path
) -> str:
    """Attach ``path`` read-only unless it already is the current database."""

    if _same_path(path, current):
        return "main"
    connection.execute(
        f"ATTACH DATABASE ? AS {_quote_identifier(schema)}", (_read_only_uri(path),)
    )
    return schema


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
    columns = _columns(connection, schema, table)
    if not columns:
        raise DigitalSelfProjectionError(f"missing SQLite table {schema}.{table}")
    missing = sorted(set(required).difference(columns))
    if missing:
        raise DigitalSelfProjectionError(
            f"{schema}.{table} is missing required columns: {', '.join(missing)}"
        )
    return columns


def _read_rows(
    connection: sqlite3.Connection,
    schema: str,
    table: str,
    columns: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(f"SELECT * FROM {_qualified(schema, table)}").fetchall()
    return tuple({column: row[column] for column in columns} for row in rows)


def _manifest_evidence_ids(manifest: DigitalSelfManifest) -> tuple[str, ...]:
    """Every archive evidence id a stored manifest is built from."""

    ids: list[str] = []
    for entry in manifest.entries:
        if isinstance(entry, MemoryClaimManifestEntry):
            ids.append(entry.source_event_id)
        elif isinstance(entry, PersonaTraitManifestEntry):
            ids.extend(entry.source_event_ids)
        elif isinstance(entry, DecisionCaseManifestEntry):
            ids.extend(entry.support_source_event_ids)
            ids.extend(entry.counterexample_source_event_ids)
        elif isinstance(entry, CognitiveClaimManifestEntry) or isinstance(
            entry, RelationshipProfileManifestEntry
        ):
            ids.extend(entry.support_source_event_ids)
            ids.extend(entry.counterexample_source_event_ids)
    return tuple(sorted({value.strip() for value in ids if value.strip()}))


def _decode_stored_manifest(
    row: Mapping[str, Any],
) -> tuple[DigitalSelfManifest | None, str | None]:
    """Re-run the product's own manifest integrity gate on the stored bytes."""

    raw = row.get("manifest_json")
    if not isinstance(raw, str):
        return None, "manifest_json_not_text"
    try:
        manifest = decode_manifest(
            raw.encode("utf-8"),
            expected_manifest_sha256=str(row.get("manifest_sha256") or ""),
            expected_source_summary_sha256=str(
                row.get("source_summary_sha256") or ""
            ),
            expected_parent_version_id=_normalise_text(row.get("parent_version_id")),
            expected_rollback_target_version_id=_normalise_text(
                row.get("rollback_target_version_id")
            ),
        )
    except (ManifestIntegrityError, TypeError, ValueError):
        return None, "manifest_integrity_failed"
    return manifest, None


def _load_evidence_subjects(
    connection: sqlite3.Connection, schema: str, event_ids: Iterable[str]
) -> tuple[dict[str, str | None], frozenset[str]]:
    """Resolve archive subject ids for the evidence a manifest references."""

    required = tuple(sorted(set(event_ids)))
    _require_columns(connection, schema, _EVIDENCE_TABLE, ("event_id", "subject_id"))
    subjects: dict[str, str | None] = {}
    missing: set[str] = set()
    for start in range(0, len(required), 400):
        chunk = required[start : start + 400]
        placeholders = ", ".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT event_id, subject_id FROM {_qualified(schema, _EVIDENCE_TABLE)} "
            f"WHERE event_id IN ({placeholders})",
            chunk,
        ).fetchall()
        found = {
            str(row["event_id"]): _normalise_text(row["subject_id"]) for row in rows
        }
        for event_id in chunk:
            if event_id in found:
                subjects[event_id] = found[event_id]
            else:
                missing.add(event_id)
                subjects[event_id] = None
    return subjects, frozenset(missing)


def _load_source(connection: sqlite3.Connection, schema: str = "main") -> _SourceState:
    _require_columns(connection, schema, _VERSION_TABLE, _VERSION_COLUMNS)
    _require_columns(connection, schema, _AUDIT_TABLE, _AUDIT_COLUMNS)
    versions = _read_rows(connection, schema, _VERSION_TABLE, _VERSION_COLUMNS)
    audit_events = _read_rows(connection, schema, _AUDIT_TABLE, _AUDIT_COLUMNS)
    manifests: dict[str, DigitalSelfManifest | None] = {}
    manifest_errors: dict[str, str] = {}
    evidence_ids: list[str] = []
    for row in versions:
        version_id = str(row["version_id"])
        manifest, reason = _decode_stored_manifest(row)
        manifests[version_id] = manifest
        if reason is not None:
            manifest_errors[version_id] = reason
        if manifest is not None:
            evidence_ids.extend(_manifest_evidence_ids(manifest))
    evidence_subjects, missing = _load_evidence_subjects(
        connection, schema, evidence_ids
    )
    return _SourceState(
        versions=versions,
        audit_events=audit_events,
        manifests=manifests,
        manifest_errors=manifest_errors,
        evidence_subjects=evidence_subjects,
        missing_evidence_ids=missing,
        digest=_source_digest(versions, audit_events),
    )


def _load_identity(connection: sqlite3.Connection, schema: str) -> _IdentityState:
    """Read the Identity rows that may prove owner evidence."""

    for table, required in _IDENTITY_TABLES:
        _require_columns(connection, schema, table, required)
    persons = {
        str(row["person_id"]): {"person_id": str(row["person_id"]), "status": str(row["status"])}
        for row in _read_rows(
            connection, schema, "identity_persons", ("person_id", "status")
        )
    }
    bindings = _read_rows(
        connection,
        schema,
        "identity_device_bindings",
        ("binding_id", "account_owner_person_id", "status"),
    )
    binding_roles = _read_rows(
        connection,
        schema,
        "identity_device_binding_roles",
        ("binding_id", "person_id", "role", "status"),
    )
    relationships = _read_rows(
        connection,
        schema,
        "identity_relationships",
        ("source_person_id", "target_person_id", "relation_type", "status"),
    )
    digest = _sha256(
        {
            "persons": [_json_safe(row) for row in persons.values()],
            "bindings": [_json_safe(row) for row in bindings],
            "binding_roles": [_json_safe(row) for row in binding_roles],
            "relationships": [_json_safe(row) for row in relationships],
        }
    )
    return _IdentityState(
        persons=persons,
        bindings=bindings,
        binding_roles=binding_roles,
        relationships=relationships,
        tables=tuple(table for table, _ in _IDENTITY_TABLES),
        digest=digest,
    )


def _registered_accounts(connection: sqlite3.Connection, schema: str) -> frozenset[str]:
    """Account ids the Control store considers registered."""

    accounts: set[str] = set()
    found_source = False
    for table, column in _REGISTRATION_TABLES:
        if not _table_exists(connection, schema, table):
            continue
        if column not in _columns(connection, schema, table):
            continue
        found_source = True
        rows = connection.execute(
            f"SELECT DISTINCT {_quote_identifier(column)} AS account_id "
            f"FROM {_qualified(schema, table)} "
            f"WHERE {_quote_identifier(column)} IS NOT NULL"
        ).fetchall()
        accounts.update(str(row["account_id"]) for row in rows)
    if not found_source:
        raise DigitalSelfProjectionError(
            "control account tables are required to prove account registration"
        )
    return frozenset(accounts)


def _load_coverage(connection: sqlite3.Connection, schema: str = "main") -> _Coverage:
    """Read the receipts of earlier runs, split by their migration status."""

    if not _table_exists(connection, schema, _RECEIPT_TABLE):
        return _Coverage(applied={}, rolled_back={}, subject_by_version={})
    if not _table_exists(connection, schema, _MIGRATION_TABLE):
        raise DigitalSelfProjectionError(
            "row receipts exist without a projection migration journal"
        )
    rows = connection.execute(
        f"SELECT r.*, m.status AS migration_status "
        f"FROM {_qualified(schema, _RECEIPT_TABLE)} AS r "
        f"LEFT JOIN {_qualified(schema, _MIGRATION_TABLE)} AS m "
        "ON m.migration_id = r.migration_id"
    ).fetchall()
    applied: dict[tuple[str, str], dict[str, Any]] = {}
    rolled_back: dict[tuple[str, str], dict[str, Any]] = {}
    subject_by_version: dict[str, str] = {}
    for row in rows:
        migration_id = str(row["migration_id"])
        status = _normalise_text(row["migration_status"])
        if status is None:
            raise DigitalSelfProjectionError(
                f"row receipt {migration_id} has no migration row"
            )
        if status not in {"applied", "rolled_back"}:
            raise DigitalSelfProjectionError(
                f"projection migration {migration_id} is mid-rollback: "
                "finish or inspect it before planning again"
            )
        key = (str(row["table_name"]), str(row["source_row_id"]))
        receipt = {str(column): row[column] for column in row.keys()}
        bucket = applied if status == "applied" else rolled_back
        if key in bucket:
            raise DigitalSelfProjectionError(
                f"duplicate row receipts for {key[0]}:{key[1]}"
            )
        bucket[key] = receipt
        if (
            status == "applied"
            and key[0] == _VERSION_TABLE
            and str(receipt["outcome"]) in {"mapped", "refreshed"}
        ):
            subject = _normalise_text(receipt["subject_id"])
            if subject is not None:
                subject_by_version[key[1]] = subject
    return _Coverage(
        applied=applied, rolled_back=rolled_back, subject_by_version=subject_by_version
    )


def _owner_evidence(
    account_id: str, identity: _IdentityState
) -> tuple[str | None, tuple[str, ...]]:
    """Owner evidence for one account person, or ``(None, ())``."""

    active_bindings = {
        str(row["binding_id"]): _normalise_text(row["account_owner_person_id"])
        for row in identity.bindings
        if _normalise_text(row["status"]) == "active"
    }
    owned = sorted(
        binding_id for binding_id, owner in active_bindings.items() if owner == account_id
    )
    if owned:
        return "owner_binding", tuple(owned)
    role_bindings = sorted(
        {
            str(row["binding_id"])
            for row in identity.binding_roles
            if _normalise_text(row["person_id"]) == account_id
            and _normalise_text(row["role"]) == "account_owner"
            and _normalise_text(row["status"]) == "active"
            and str(row["binding_id"]) in active_bindings
        }
    )
    if role_bindings:
        return "owner_binding_role", tuple(role_bindings)
    self_relationships = sorted(
        {
            str(row["source_person_id"])
            for row in identity.relationships
            if _normalise_text(row["relation_type"]) == "self"
            and _normalise_text(row["status"]) == "active"
            and _normalise_text(row["source_person_id"]) == account_id
            and _normalise_text(row["target_person_id"]) == account_id
        }
    )
    if self_relationships:
        return "owner_relationship", (account_id,)
    return None, ()


def _account_owners_of_subject(subject_id: str, identity: _IdentityState) -> frozenset[str]:
    """Registered accounts that can claim this subject as their member."""

    owners: set[str] = set()
    for row in identity.relationships:
        if _normalise_text(row["status"]) != "active":
            continue
        source = _normalise_text(row["source_person_id"])
        target = _normalise_text(row["target_person_id"])
        if source == subject_id and target is not None:
            owners.add(target)
        if target == subject_id and source is not None:
            owners.add(source)
    active_owners = {
        str(row["binding_id"]): _normalise_text(row["account_owner_person_id"])
        for row in identity.bindings
        if _normalise_text(row["status"]) == "active"
    }
    for row in identity.binding_roles:
        if _normalise_text(row["status"]) != "active":
            continue
        if _normalise_text(row["person_id"]) != subject_id:
            continue
        owner = active_owners.get(str(row["binding_id"]))
        if owner is not None:
            owners.add(owner)
    owners.discard(subject_id)
    return frozenset(owners)


def _projected_version_payload(
    row: Mapping[str, Any], subject_id: str, *, status: str | None = None
) -> dict[str, Any]:
    """The subject-keyed copy of one version row, source values verbatim."""

    return {
        "version_id": str(row["version_id"]),
        "subject_id": subject_id,
        "account_id": str(row["account_id"]),
        "version_number": int(row["version_number"]),
        "status": status if status is not None else str(row["status"]),
        "manifest_json": str(row["manifest_json"]),
        "manifest_sha256": str(row["manifest_sha256"]),
        "source_summary_sha256": str(row["source_summary_sha256"]),
        "parent_version_id": row["parent_version_id"],
        "rollback_target_version_id": row["rollback_target_version_id"],
        "created_at": str(row["created_at"]),
    }


def _projected_audit_payload(row: Mapping[str, Any], subject_id: str) -> dict[str, Any]:
    """The subject-keyed copy of one lifecycle audit event, values verbatim."""

    payload: dict[str, Any] = {"subject_id": subject_id}
    for column in _AUDIT_COLUMNS:
        payload[column] = row[column]
    return payload


@dataclass(frozen=True, slots=True)
class _Session:
    """The three SQLite databases one run reads (target is always the first)."""

    db_path: Path
    identity_path: Path
    control_path: Path


@dataclass(frozen=True, slots=True)
class _Context:
    """Everything one run needs to classify the source rows."""

    source: _SourceState
    identity: _IdentityState
    registered_accounts: frozenset[str]
    coverage: _Coverage
    versions_by_id: Mapping[str, Mapping[str, Any]]
    audit_by_id: Mapping[str, Mapping[str, Any]]


def _sources(
    db_path: str | Path,
    identity_path: str | Path | None,
    control_path: str | Path | None,
) -> _Session:
    db = Path(db_path).expanduser().resolve()
    identity = (
        Path(identity_path).expanduser().resolve() if identity_path is not None else db
    )
    control = (
        Path(control_path).expanduser().resolve() if control_path is not None else db
    )
    return _Session(db_path=db, identity_path=identity, control_path=control)


def _load_context(connection: sqlite3.Connection, session: _Session) -> _Context:
    identity_schema = _attach_read_only(
        connection, session.identity_path, _IDENTITY_SCHEMA, session.db_path
    )
    control_schema = _attach_read_only(
        connection, session.control_path, _CONTROL_SCHEMA, session.db_path
    )
    source = _load_source(connection, "main")
    return _Context(
        source=source,
        identity=_load_identity(connection, identity_schema),
        registered_accounts=_registered_accounts(connection, control_schema),
        coverage=_load_coverage(connection, "main"),
        versions_by_id={str(row["version_id"]): row for row in source.versions},
        audit_by_id={str(row["event_id"]): row for row in source.audit_events},
    )


def _current_projection(
    connection: sqlite3.Connection,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Projection content rows (bookkeeping columns excluded) or empty tuples."""

    if not _table_exists(connection, "main", _PROJECTED_VERSION_TABLE):
        return (), ()
    versions = _read_rows(
        connection, "main", _PROJECTED_VERSION_TABLE, _PROJECTED_VERSION_STATE_COLUMNS
    )
    audit: tuple[dict[str, Any], ...] = ()
    if _table_exists(connection, "main", _PROJECTED_AUDIT_TABLE):
        audit = _read_rows(
            connection, "main", _PROJECTED_AUDIT_TABLE, _AUDIT_CONTENT_FIELDS
        )
    return versions, audit


def _projection_state_digest(
    versions: Iterable[Mapping[str, Any]], audit_events: Iterable[Mapping[str, Any]]
) -> str:
    return _sha256(
        {
            "versions": [
                {
                    "version_id": str(row["version_id"]),
                    "digest": _version_content_digest(row),
                    "status": str(row["status"]),
                }
                for row in sorted(versions, key=lambda item: str(item["version_id"]))
            ],
            "audit_events": [
                {
                    "event_id": str(row["event_id"]),
                    "digest": _audit_content_digest(row),
                }
                for row in sorted(audit_events, key=lambda item: str(item["event_id"]))
            ],
        }
    )


def _planned_projection(
    entries: Iterable[_Entry],
    current_versions: Iterable[Mapping[str, Any]],
    current_audit: Iterable[Mapping[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """The projection content this run would leave behind."""

    versions = {str(row["version_id"]): dict(row) for row in current_versions}
    audit = {str(row["event_id"]): dict(row) for row in current_audit}
    for entry in entries:
        if entry.outcome not in {"mapped", "refreshed"} or entry.target_row is None:
            continue
        if entry.table == _VERSION_TABLE:
            merged = versions.get(entry.row_id, {})
            merged.update(entry.target_row)
            versions[entry.row_id] = merged
        else:
            audit[entry.row_id] = dict(entry.target_row)
    return tuple(versions.values()), tuple(audit.values())


def _verify_applied_receipts(connection: sqlite3.Connection, context: _Context) -> None:
    """Fence the target rows earlier runs already wrote."""

    if not context.coverage.applied:
        return
    if not _table_exists(connection, "main", _PROJECTED_VERSION_TABLE) or not _table_exists(
        connection, "main", _PROJECTED_AUDIT_TABLE
    ):
        raise DigitalSelfProjectionError(
            "projection receipts exist without projection tables"
        )
    statuses: dict[tuple[str, str], set[str]] = {}
    for key, receipt in context.coverage.applied.items():
        after_status = _normalise_text(receipt["after_status"])
        if after_status is not None:
            statuses.setdefault(key, set()).add(after_status)
    for key, receipt in sorted(context.coverage.applied.items()):
        table, row_id = key
        source_row = (
            context.versions_by_id.get(row_id)
            if table == _VERSION_TABLE
            else context.audit_by_id.get(row_id)
        )
        if source_row is None:
            raise DigitalSelfProjectionError(
                f"source row is missing for an applied receipt: {table}:{row_id}"
            )
        outcome = str(receipt["outcome"])
        if outcome not in {"mapped", "refreshed"}:
            continue
        expected = _normalise_text(receipt["target_row_digest"])
        if expected is None:
            raise DigitalSelfProjectionError(
                f"receipt {receipt['migration_id']} has no target row digest for {table}:{row_id}"
            )
        if table == _VERSION_TABLE:
            row = connection.execute(
                f"SELECT * FROM {_quote_identifier(_PROJECTED_VERSION_TABLE)} "
                "WHERE version_id = ?",
                (row_id,),
            ).fetchone()
            if row is None:
                raise DigitalSelfProjectionError(f"projected version is missing: {row_id}")
            values = {column: row[column] for column in _PROJECTED_VERSION_STATE_COLUMNS}
            if _version_content_digest(values) != expected:
                raise DigitalSelfProjectionError(f"projected version drifted: {row_id}")
            if str(values["status"]) not in statuses.get(key, set()):
                raise DigitalSelfProjectionError(
                    f"projected version status drifted: {row_id}"
                )
        else:
            row = connection.execute(
                f"SELECT * FROM {_quote_identifier(_PROJECTED_AUDIT_TABLE)} "
                "WHERE event_id = ?",
                (row_id,),
            ).fetchone()
            if row is None:
                raise DigitalSelfProjectionError(
                    f"projected lifecycle audit event is missing: {row_id}"
                )
            values = {column: row[column] for column in _AUDIT_CONTENT_FIELDS}
            if _audit_content_digest(values) != expected:
                raise DigitalSelfProjectionError(
                    f"projected lifecycle audit event drifted: {row_id}"
                )


def _entry_dict(entry: _Entry) -> dict[str, Any]:
    return {
        "table": entry.table,
        "source_row_id": entry.row_id,
        "account_id": entry.account_id,
        "subject_id": entry.subject_id,
        "outcome": entry.outcome,
        "reason": entry.reason,
        "details": list(entry.details),
        "source_row_digest": entry.source_digest,
        "target_row_digest": entry.target_digest,
        "before_status": entry.before_status,
        "after_status": entry.after_status,
        "evidence_count": len(entry.evidence_ids),
        "evidence_digest": _sha256(list(entry.evidence_ids)),
    }


def _classify_version(row: Mapping[str, Any], context: _Context) -> _Entry:
    """Classify one source version row against the whole evidence chain."""

    version_id = str(row["version_id"])
    account_id = _normalise_text(row["account_id"])
    status = str(row["status"])
    source_digest = _version_content_digest(row)
    source_row = {column: _json_safe(row.get(column)) for column in _VERSION_COLUMNS}

    def blocked(
        outcome: Outcome,
        reason: str,
        *,
        details: Sequence[str] = (),
        evidence: Sequence[str] = (),
    ) -> _Entry:
        return _Entry(
            table=_VERSION_TABLE,
            row_id=version_id,
            account_id=account_id,
            subject_id=None,
            outcome=outcome,
            reason=reason,
            details=tuple(details),
            source_digest=source_digest,
            target_digest=None,
            source_row=source_row,
            target_row=None,
            evidence_ids=tuple(evidence),
            after_status=status,
        )

    if account_id is None:
        return blocked("quarantined", "account_id_missing")
    if account_id not in context.registered_accounts:
        return blocked("quarantined", "owner_account_unregistered")
    person = context.identity.persons.get(account_id)
    if person is None:
        return blocked("quarantined", "owner_person_missing")
    if str(person["status"]) != "active":
        return blocked("quarantined", "owner_person_inactive")
    owner_reason, owner_details = _owner_evidence(account_id, context.identity)
    if owner_reason is None:
        return blocked("quarantined", "owner_evidence_missing")
    manifest_error = context.source.manifest_errors.get(version_id)
    if manifest_error is not None:
        return blocked("quarantined", manifest_error)
    manifest = context.source.manifests.get(version_id)
    if manifest is None:
        return blocked("quarantined", "manifest_unavailable")
    evidence_ids = _manifest_evidence_ids(manifest)
    if not evidence_ids:
        return blocked("omitted", "no_evidence_ids")
    subjects = {
        context.source.evidence_subjects.get(event_id) for event_id in evidence_ids
    }
    unproven = tuple(
        sorted(
            event_id
            for event_id in evidence_ids
            if context.source.evidence_subjects.get(event_id) is None
        )
    )
    if subjects == {None}:
        return blocked(
            "omitted", "no_subject_evidence", details=unproven[:5], evidence=evidence_ids
        )
    if unproven:
        return blocked(
            "quarantined",
            "partial_subject_evidence",
            details=unproven[:5],
            evidence=evidence_ids,
        )
    others = sorted(
        subject for subject in subjects if subject is not None and subject != account_id
    )
    if others:
        member_owned = all(
            _account_owners_of_subject(subject, context.identity)
            == frozenset({account_id})
            for subject in others
        )
        return blocked(
            "quarantined",
            "mixed_subject_evidence" if member_owned else "foreign_subject_evidence",
            details=others[:5],
            evidence=evidence_ids,
        )
    payload = _projected_version_payload(row, account_id)
    return _Entry(
        table=_VERSION_TABLE,
        row_id=version_id,
        account_id=account_id,
        subject_id=account_id,
        outcome="mapped",
        reason=owner_reason,
        details=tuple(owner_details),
        source_digest=source_digest,
        target_digest=_version_content_digest(payload),
        source_row=source_row,
        target_row=payload,
        evidence_ids=evidence_ids,
        after_status=status,
    )


def _covered_version_entry(
    row: Mapping[str, Any], receipt: Mapping[str, Any]
) -> _Entry:
    """A version row an earlier applied run already decided about."""

    version_id = str(row["version_id"])
    source_digest = _version_content_digest(row)
    if str(receipt["source_row_digest"]) != source_digest:
        raise DigitalSelfProjectionError(
            "source content drifted under an applied receipt: "
            f"{_VERSION_TABLE}:{version_id}"
        )
    source_row = {column: _json_safe(row.get(column)) for column in _VERSION_COLUMNS}
    account_id = _normalise_text(row["account_id"])
    status = str(row["status"])
    outcome = str(receipt["outcome"])
    migration_id = str(receipt["migration_id"])
    subject = _normalise_text(receipt["subject_id"])
    if outcome not in {"mapped", "refreshed"}:
        return _Entry(
            table=_VERSION_TABLE,
            row_id=version_id,
            account_id=account_id,
            subject_id=subject,
            outcome="already_projected",
            reason=f"covered_by_{outcome}",
            details=(f"migration:{migration_id}", str(receipt["reason"])),
            source_digest=source_digest,
            target_digest=None,
            source_row=source_row,
            target_row=None,
            after_status=status,
        )
    if subject is None:
        raise DigitalSelfProjectionError(
            f"receipt {migration_id} projects {version_id} without a subject"
        )
    projected_status = _normalise_text(receipt["after_status"])
    if projected_status is None:
        raise DigitalSelfProjectionError(
            f"receipt {migration_id} projects {version_id} without a status"
        )
    if projected_status == status:
        return _Entry(
            table=_VERSION_TABLE,
            row_id=version_id,
            account_id=account_id,
            subject_id=subject,
            outcome="already_projected",
            reason="subject_and_status_current",
            details=(f"migration:{migration_id}",),
            source_digest=source_digest,
            target_digest=None,
            source_row=source_row,
            target_row=None,
            after_status=status,
        )
    payload = _projected_version_payload(row, subject)
    return _Entry(
        table=_VERSION_TABLE,
        row_id=version_id,
        account_id=account_id,
        subject_id=subject,
        outcome="refreshed",
        reason="status_refresh",
        details=(f"migration:{migration_id}",),
        source_digest=source_digest,
        target_digest=_version_content_digest(payload),
        source_row=source_row,
        target_row=payload,
    before_status=projected_status,
    after_status=status,
    )


def _covered_audit_entry(row: Mapping[str, Any], receipt: Mapping[str, Any]) -> _Entry:
    """A lifecycle audit row an earlier applied run already projected."""

    event_id = str(row["event_id"])
    source_digest = _audit_content_digest(row)
    if str(receipt["source_row_digest"]) != source_digest:
        raise DigitalSelfProjectionError(
            f"source content drifted under an applied receipt: {_AUDIT_TABLE}:{event_id}"
        )
    return _Entry(
        table=_AUDIT_TABLE,
        row_id=event_id,
        account_id=_normalise_text(row["account_id"]),
        subject_id=_normalise_text(receipt["subject_id"]),
        outcome="already_projected",
        reason="covered_by_audit_projection",
        details=(f"migration:{receipt['migration_id']}",),
        source_digest=source_digest,
        target_digest=None,
        source_row={column: _json_safe(row.get(column)) for column in _AUDIT_COLUMNS},
        target_row=None,
    )


def _classify_audit(
    row: Mapping[str, Any], context: _Context, projected: Mapping[str, str]
) -> _Entry:
    """Classify one lifecycle audit event against its projected version."""

    event_id = str(row["event_id"])
    account_id = _normalise_text(row["account_id"])
    actor = _normalise_text(row["actor_account_id"])
    source_digest = _audit_content_digest(row)
    source_row = {column: _json_safe(row.get(column)) for column in _AUDIT_COLUMNS}

    def blocked(
        reason: str, *, details: Sequence[str] = (), subject: str | None = None
    ) -> _Entry:
        return _Entry(
            table=_AUDIT_TABLE,
            row_id=event_id,
            account_id=account_id,
            subject_id=subject,
            outcome="quarantined",
            reason=reason,
            details=tuple(details),
            source_digest=source_digest,
            target_digest=None,
            source_row=source_row,
            target_row=None,
        )

    if account_id is None:
        return blocked("account_id_missing")
    if actor is None or actor != account_id:
        return blocked("audit_actor_account_mismatch")
    action = _normalise_text(row["action"])
    if action not in _ACTIONS:
        return blocked("audit_action_unsupported")
    for column in ("from_status", "to_status"):
        value = _normalise_text(row[column])
        if value is None and column == "from_status":
            continue
        if value not in _VERSION_STATUSES:
            return blocked("audit_status_unsupported", details=(f"column:{column}",))
    version_id = _normalise_text(row["version_id"])
    if version_id is None:
        return blocked("audit_version_missing")
    version_row = context.versions_by_id.get(version_id)
    if version_row is None:
        return blocked("audit_version_missing", details=(version_id,))
    subject = projected.get(version_id)
    if subject is None:
        return blocked("audit_version_not_projected", details=(version_id,))
    for column in ("target_version_id", "new_version_id"):
        reference = _normalise_text(row[column])
        if reference is None:
            continue
        if reference not in context.versions_by_id:
            return blocked(f"audit_{column}_missing", details=(reference,), subject=subject)
        if projected.get(reference) != subject:
            return blocked(
                f"audit_{column}_not_projected", details=(reference,), subject=subject
            )
    if str(row["manifest_sha256"]) != str(version_row["manifest_sha256"]):
        return blocked("audit_manifest_mismatch", details=(version_id,), subject=subject)
    if str(row["account_id"]) != str(version_row["account_id"]):
        return blocked("audit_account_mismatch", details=(version_id,), subject=subject)
    payload = _projected_audit_payload(row, subject)
    return _Entry(
        table=_AUDIT_TABLE,
        row_id=event_id,
        account_id=account_id,
        subject_id=subject,
        outcome="mapped",
        reason=f"lifecycle_{action}",
        details=(),
        source_digest=source_digest,
        target_digest=_audit_content_digest(payload),
        source_row=source_row,
        target_row=payload,
    )


def _version_reference_reason(
    entry: _Entry, context: _Context, projected: Mapping[str, str]
) -> str | None:
    """Why a mapped version may not be projected: its references are not."""

    for column in ("parent_version_id", "rollback_target_version_id"):
        reference = _normalise_text(entry.source_row.get(column))
        if reference is None:
            continue
        if reference not in context.versions_by_id:
            return f"{column}_reference_missing"
        if projected.get(reference) != entry.subject_id:
            return f"{column}_reference_not_projected"
    return None


def _downgrade(entry: _Entry, reason: str) -> _Entry:
    """Turn a planned projection into a fail-closed quarantine."""

    return _Entry(
        table=entry.table,
        row_id=entry.row_id,
        account_id=entry.account_id,
        subject_id=None,
        outcome="quarantined",
        reason=reason,
        details=entry.details,
        source_digest=entry.source_digest,
        target_digest=None,
        source_row=entry.source_row,
        target_row=None,
        evidence_ids=entry.evidence_ids,
        before_status=entry.before_status,
        after_status=entry.after_status,
    )


def _statistics(entries: Sequence[_Entry]) -> dict[str, Any]:
    outcomes = ("mapped", "refreshed", "omitted", "quarantined", "already_projected")

    def counts(table: str) -> dict[str, int]:
        return {
            outcome: sum(
                1 for entry in entries if entry.table == table and entry.outcome == outcome
            )
            for outcome in outcomes
        }

    return {
        "version_row_count": sum(1 for e in entries if e.table == _VERSION_TABLE),
        "audit_row_count": sum(1 for e in entries if e.table == _AUDIT_TABLE),
        "versions": counts(_VERSION_TABLE),
        "audit_events": counts(_AUDIT_TABLE),
        "subjects": sorted(
            {
                entry.subject_id
                for entry in entries
                if entry.subject_id
                and entry.outcome in {"mapped", "refreshed", "already_projected"}
            }
        ),
        "writes": sum(1 for e in entries if e.outcome in {"mapped", "refreshed"}),
    }


def _run_plan(connection: sqlite3.Connection, context: _Context) -> _RunPlan:
    """Classify every source row, then fence the projected references."""

    _verify_applied_receipts(connection, context)
    entries: list[_Entry] = []
    projected: dict[str, str] = dict(context.coverage.subject_by_version)
    for row in sorted(context.source.versions, key=lambda item: str(item["version_id"])):
        row_id = str(row["version_id"])
        receipt = context.coverage.applied.get((_VERSION_TABLE, row_id))
        if receipt is not None:
            entry = _covered_version_entry(row, receipt)
        else:
            rolled_back = context.coverage.rolled_back.get((_VERSION_TABLE, row_id))
            if rolled_back is not None and str(rolled_back["source_row_digest"]) != (
                _version_content_digest(row)
            ):
                raise DigitalSelfProjectionError(
                    "source content changed under a rolled-back receipt: "
                    f"{_VERSION_TABLE}:{row_id}"
                )
            entry = _classify_version(row, context)
        if entry.subject_id is not None and entry.outcome in {
            "mapped",
            "refreshed",
            "already_projected",
        }:
            projected[row_id] = entry.subject_id
        entries.append(entry)
    settled = True
    while settled:
        settled = False
        for index, entry in enumerate(entries):
            if entry.outcome != "mapped":
                continue
            reason = _version_reference_reason(entry, context, projected)
            if reason is None:
                continue
            projected.pop(entry.row_id, None)
            entries[index] = _downgrade(entry, reason)
            settled = True
    for row in sorted(context.source.audit_events, key=lambda item: str(item["event_id"])):
        event_id = str(row["event_id"])
        receipt = context.coverage.applied.get((_AUDIT_TABLE, event_id))
        if receipt is not None:
            entries.append(_covered_audit_entry(row, receipt))
            continue
        rolled_back = context.coverage.rolled_back.get((_AUDIT_TABLE, event_id))
        if rolled_back is not None and str(rolled_back["source_row_digest"]) != (
            _audit_content_digest(row)
        ):
            raise DigitalSelfProjectionError(
                "source content changed under a rolled-back receipt: "
                f"{_AUDIT_TABLE}:{event_id}"
            )
        entries.append(_classify_audit(row, context, projected))
    pending = tuple(entry for entry in entries if entry.outcome != "already_projected")
    return _RunPlan(entries=tuple(entries), pending=pending, statistics=_statistics(entries))


def _build_report(
    connection: sqlite3.Connection,
    context: _Context,
    *,
    session: _Session,
    planned_at: str,
) -> dict[str, Any]:
    """Build the deterministic manifest of one run without writing anything."""

    run = _run_plan(connection, context)
    current_versions, current_audit = _current_projection(connection)
    target_before = _projection_state_digest(current_versions, current_audit)
    planned_versions, planned_audit = _planned_projection(
        run.entries, current_versions, current_audit
    )
    target_after = _projection_state_digest(planned_versions, planned_audit)
    source_digest = context.source.digest
    rows = [_entry_dict(entry) for entry in run.entries]
    core: dict[str, Any] = {
        "version": 1,
        "kind": _SCOPE,
        "rules_version": _RULES_VERSION,
        "source": {
            "digest_before": source_digest,
            "digest_after": source_digest,
            "tables": [_VERSION_TABLE, _AUDIT_TABLE],
            "row_counts": {
                "versions": len(context.source.versions),
                "audit_events": len(context.source.audit_events),
            },
        },
        "identity": {
            "digest": context.identity.digest,
            "tables": list(context.identity.tables),
        },
        "target": {
            "digest_before": target_before,
            "digest_after": target_after,
            "tables": [_PROJECTED_VERSION_TABLE, _PROJECTED_AUDIT_TABLE],
        },
        "account_evidence": {
            "registered_account_count": len(context.registered_accounts),
        },
        "rows": rows,
        "statistics": run.statistics,
    }
    manifest_sha256 = _sha256(core)
    manifest = dict(core)
    manifest["manifest_sha256"] = manifest_sha256
    return {
        "scope": _SCOPE,
        "source_path": str(session.db_path),
        "identity_path": str(session.identity_path),
        "control_path": str(session.control_path),
        "dry_run": True,
        "applied": False,
        "idempotent": False,
        "migration_id": None,
        "manifest_sha256": manifest_sha256,
        "source_digest_before": source_digest,
        "source_digest_after": source_digest,
        "target_digest_before": target_before,
        "target_digest_after": target_after,
        "identity_digest": context.identity.digest,
        "planned_at": planned_at,
        "tables": [_VERSION_TABLE, _AUDIT_TABLE],
        "projection_tables": [_PROJECTED_VERSION_TABLE, _PROJECTED_AUDIT_TABLE],
        "rows": rows,
        "row_count": len(run.entries),
        "pending_row_count": len(run.pending),
        "statistics": run.statistics,
        "manifest": manifest,
    }


def _migration_id(manifest_sha256: str, sequence: int) -> str:
    if sequence <= 0:
        return f"digital-self-subject-v1-{manifest_sha256}"
    return f"digital-self-subject-v1-{manifest_sha256}-r{sequence}"


def _migration_sequence(connection: sqlite3.Connection, manifest_sha256: str) -> int:
    row = connection.execute(
        f"SELECT count(*) AS n FROM {_quote_identifier(_MIGRATION_TABLE)} "
        "WHERE manifest_sha256 = ?",
        (manifest_sha256,),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def _find_migration(
    connection: sqlite3.Connection,
    *,
    migration_id: str | None = None,
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    matches: dict[str, dict[str, Any]] = {}
    for column, value in (
        ("migration_id", migration_id),
        ("manifest_sha256", manifest_sha256),
    ):
        if value is None:
            continue
        rows = connection.execute(
            f"SELECT * FROM {_quote_identifier(_MIGRATION_TABLE)} "
            f"WHERE {_quote_identifier(column)} = ?",
            (value,),
        ).fetchall()
        if len(rows) > 1:
            raise DigitalSelfProjectionError(
                f"selector identifies more than one projection run: {value}; "
                "pass migration_id",
            )
        if rows:
            row = rows[0]
            matches[str(row["migration_id"])] = {
                str(column_name): row[column_name] for column_name in row.keys()
            }
    if not matches:
        raise DigitalSelfProjectionError(
            "requested digital self projection migration was not found"
        )
    if len(matches) != 1:
        raise DigitalSelfProjectionError("migration selectors identify different migrations")
    return next(iter(matches.values()))


def _migration_receipts(
    connection: sqlite3.Connection, migration_id: str
) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(
        f"SELECT * FROM {_quote_identifier(_RECEIPT_TABLE)} WHERE migration_id = ? "
        "ORDER BY table_name, source_row_id",
        (migration_id,),
    ).fetchall()
    return tuple({str(column): row[column] for column in row.keys()} for row in rows)


def _insert_projection_audit(
    connection: sqlite3.Connection,
    *,
    migration_id: str,
    table_name: str,
    source_row_id: str,
    action: str,
    payload: Mapping[str, Any],
    created_at: str,
) -> None:
    audit_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "memoria:digital-self:projection:"
            f"{migration_id}:{table_name}:{source_row_id}:{action}",
        )
    )
    connection.execute(
        f"INSERT INTO {_quote_identifier(_AUDIT_EVENT_TABLE)} "
        "(audit_id, migration_id, table_name, source_row_id, action, "
        "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            audit_id,
            migration_id,
            table_name,
            source_row_id,
            action,
            _canonical(payload),
            created_at,
        ),
    )


def _assert_target_free(connection: sqlite3.Connection, run: _RunPlan) -> None:
    for entry in run.pending:
        if entry.outcome != "mapped":
            continue
        if entry.table == _VERSION_TABLE:
            row = connection.execute(
                f"SELECT 1 FROM {_quote_identifier(_PROJECTED_VERSION_TABLE)} "
                "WHERE version_id = ?",
                (entry.row_id,),
            ).fetchone()
        else:
            row = connection.execute(
                f"SELECT 1 FROM {_quote_identifier(_PROJECTED_AUDIT_TABLE)} "
                "WHERE event_id = ?",
                (entry.row_id,),
            ).fetchone()
        if row is not None:
            raise DigitalSelfProjectionError(
                "projected row already exists without a receipt: "
                f"{entry.table}:{entry.row_id}",
            )


def _write_entry(
    connection: sqlite3.Connection,
    entry: _Entry,
    *,
    migration_id: str,
    timestamp: str,
) -> None:
    if entry.outcome in {"mapped", "refreshed"}:
        if entry.target_row is None:
            raise DigitalSelfProjectionError(
                f"entry has no projected row: {entry.table}:{entry.row_id}"
            )
        row_values = dict(entry.target_row)
        row_values["projection_migration_id"] = migration_id
        row_values["projected_at"] = timestamp
        columns: tuple[str, ...]
        if entry.table == _VERSION_TABLE:
            columns = _PROJECTED_VERSION_COLUMNS
            table = _PROJECTED_VERSION_TABLE
        else:
            columns = _PROJECTED_AUDIT_COLUMNS
            table = _PROJECTED_AUDIT_TABLE
        if entry.outcome == "mapped":
            connection.execute(
                f"INSERT INTO {_quote_identifier(table)} "
                f"({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                tuple(row_values[column] for column in columns),
            )
        else:
            cursor = connection.execute(
                f"UPDATE {_quote_identifier(table)} SET status = ? "
                "WHERE version_id = ?",
                (row_values["status"], entry.row_id),
            )
            if cursor.rowcount != 1:
                raise DigitalSelfProjectionError(
                    f"projected version is missing for a refresh: {entry.row_id}"
                )
    connection.execute(
        f"INSERT INTO {_quote_identifier(_RECEIPT_TABLE)} "
        "(migration_id, table_name, source_row_id, outcome, subject_id, "
        "before_status, after_status, source_row_digest, target_row_digest, "
        "reason, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            migration_id,
            entry.table,
            entry.row_id,
            entry.outcome,
            entry.subject_id,
            entry.before_status,
            entry.after_status,
            entry.source_digest,
            entry.target_digest,
            entry.reason,
            timestamp,
        ),
    )
    connection.execute(
        f"INSERT INTO {_quote_identifier(_BACKUP_TABLE)} "
        "(migration_id, table_name, source_row_id, source_row_json, "
        "source_row_digest, target_row_json, target_row_digest, "
        "evidence_ids_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            migration_id,
            entry.table,
            entry.row_id,
            _canonical(entry.source_row),
            entry.source_digest,
            _canonical(entry.target_row) if entry.target_row is not None else None,
            entry.target_digest,
            _canonical(list(entry.evidence_ids)),
            timestamp,
        ),
    )
    if entry.outcome in {"omitted", "quarantined"}:
        connection.execute(
            f"INSERT INTO {_quote_identifier(_QUARANTINE_TABLE)} "
            "(migration_id, table_name, source_row_id, account_id, subject_id, "
            "outcome, reason, details_json, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                migration_id,
                entry.table,
                entry.row_id,
                entry.account_id,
                entry.subject_id,
                entry.outcome,
                entry.reason,
                _canonical(list(entry.details)),
                timestamp,
            ),
        )
    _insert_projection_audit(
        connection,
        migration_id=migration_id,
        table_name=entry.table,
        source_row_id=entry.row_id,
        action=f"projection.{entry.outcome}",
        payload={
            "subject_id": entry.subject_id,
            "reason": entry.reason,
            "details": list(entry.details),
        },
        created_at=timestamp,
    )


def _source_version_row(
    connection: sqlite3.Connection, row_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT * FROM {_quote_identifier(_VERSION_TABLE)} WHERE version_id = ?",
        (row_id,),
    ).fetchone()
    if row is None:
        return None
    return {column: row[column] for column in _VERSION_COLUMNS}


def _source_audit_row(
    connection: sqlite3.Connection, row_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT * FROM {_quote_identifier(_AUDIT_TABLE)} WHERE event_id = ?",
        (row_id,),
    ).fetchone()
    if row is None:
        return None
    return {column: row[column] for column in _AUDIT_COLUMNS}


def _verify_rollback_fences(
    connection: sqlite3.Connection,
    migration_id: str,
    receipts: Iterable[Mapping[str, Any]],
) -> None:
    """Refuse a rollback whose receipts no longer describe both databases."""

    for receipt in receipts:
        table = str(receipt["table_name"])
        row_id = str(receipt["source_row_id"])
        outcome = str(receipt["outcome"])
        if table == _VERSION_TABLE:
            source = _source_version_row(connection, row_id)
            if source is None:
                raise DigitalSelfProjectionError(f"source version is missing: {row_id}")
            if _version_content_digest(source) != str(receipt["source_row_digest"]):
                raise DigitalSelfProjectionError(f"source version drifted: {row_id}")
        elif table == _AUDIT_TABLE:
            source = _source_audit_row(connection, row_id)
            if source is None:
                raise DigitalSelfProjectionError(
                    f"source lifecycle audit event is missing: {row_id}"
                )
            if _audit_content_digest(source) != str(receipt["source_row_digest"]):
                raise DigitalSelfProjectionError(
                    f"source lifecycle audit event drifted: {row_id}"
                )
        else:
            raise DigitalSelfProjectionError(
                f"receipt names an unsupported table: {table}"
            )
        if outcome not in {"mapped", "refreshed"}:
            continue
        expected = _normalise_text(receipt["target_row_digest"])
        if expected is None:
            raise DigitalSelfProjectionError(
                f"receipt has no target row digest: {table}:{row_id}"
            )
        projected = connection.execute(
            f"SELECT * FROM {_quote_identifier(_PROJECTED_VERSION_TABLE)} "
            "WHERE version_id = ?",
            (row_id,),
        ).fetchone() if table == _VERSION_TABLE else connection.execute(
            f"SELECT * FROM {_quote_identifier(_PROJECTED_AUDIT_TABLE)} "
            "WHERE event_id = ?",
            (row_id,),
        ).fetchone()
        if projected is None:
            raise DigitalSelfProjectionError(
                f"projected row is missing for rollback: {table}:{row_id}"
            )
        if table == _VERSION_TABLE:
            values = {
                column: projected[column]
                for column in _PROJECTED_VERSION_STATE_COLUMNS
            }
            if _version_content_digest(values) != expected:
                raise DigitalSelfProjectionError(f"projected version drifted: {row_id}")
            if str(values["status"]) != str(receipt["after_status"]):
                raise DigitalSelfProjectionError(
                    f"projected version status moved since the projection: {row_id}"
                )
            if outcome == "refreshed" and _normalise_text(receipt["before_status"]) is None:
                raise DigitalSelfProjectionError(
                    f"refresh receipt has no previous status: {row_id}"
                )
            owner = str(projected["projection_migration_id"])
        else:
            values = {column: projected[column] for column in _AUDIT_CONTENT_FIELDS}
            if _audit_content_digest(values) != expected:
                raise DigitalSelfProjectionError(
                    f"projected lifecycle audit event drifted: {row_id}"
                )
            owner = str(projected["projection_migration_id"])
        if outcome == "mapped" and owner != migration_id:
            raise DigitalSelfProjectionError(
                "a later projection run owns this row; roll that run back first: "
                f"{table}:{row_id}",
            )


def _rollback_rows(
    connection: sqlite3.Connection,
    migration_id: str,
    receipts: Iterable[Mapping[str, Any]],
    timestamp: str,
) -> tuple[list[str], list[str]]:
    """Remove the projected rows of one run and restore refreshed statuses."""

    deleted: list[str] = []
    restored: list[str] = []
    for receipt in receipts:
        table = str(receipt["table_name"])
        row_id = str(receipt["source_row_id"])
        outcome = str(receipt["outcome"])
        if outcome == "mapped":
            projected_table = (
                _PROJECTED_VERSION_TABLE if table == _VERSION_TABLE else _PROJECTED_AUDIT_TABLE
            )
            key = "version_id" if table == _VERSION_TABLE else "event_id"
            cursor = connection.execute(
                f"DELETE FROM {_quote_identifier(projected_table)} "
                f"WHERE {_quote_identifier(key)} = ?",
                (row_id,),
            )
            if cursor.rowcount != 1:
                raise DigitalSelfProjectionError(
                    f"projected row is missing during rollback: {table}:{row_id}"
                )
            deleted.append(f"{table}:{row_id}")
        elif outcome == "refreshed":
            before_status = _normalise_text(receipt["before_status"])
            cursor = connection.execute(
                f"UPDATE {_quote_identifier(_PROJECTED_VERSION_TABLE)} SET status = ? "
                "WHERE version_id = ?",
                (before_status, row_id),
            )
            if cursor.rowcount != 1:
                raise DigitalSelfProjectionError(
                    f"projected version is missing during rollback: {row_id}"
                )
            restored.append(f"{_VERSION_TABLE}:{row_id}")
        else:
            continue
        _insert_projection_audit(
            connection,
            migration_id=migration_id,
            table_name=table,
            source_row_id=row_id,
            action="projection.rolled_back",
            payload={"outcome": outcome, "reason": str(receipt["reason"])},
            created_at=timestamp,
        )
    return deleted, restored


def _rolled_back_report(
    connection: sqlite3.Connection,
    stored: Mapping[str, Any],
    receipts: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Re-verify a rolled-back run and report it again."""

    migration_id = str(stored["migration_id"])
    deleted: list[str] = []
    restored: list[str] = []
    for receipt in receipts:
        table = str(receipt["table_name"])
        row_id = str(receipt["source_row_id"])
        outcome = str(receipt["outcome"])
        if outcome == "mapped":
            projected_table = (
                _PROJECTED_VERSION_TABLE if table == _VERSION_TABLE else _PROJECTED_AUDIT_TABLE
            )
            key = "version_id" if table == _VERSION_TABLE else "event_id"
            row = connection.execute(
                f"SELECT projection_migration_id FROM {_quote_identifier(projected_table)} "
                f"WHERE {_quote_identifier(key)} = ?",
                (row_id,),
            ).fetchone()
            if row is not None and str(row["projection_migration_id"]) == migration_id:
                raise DigitalSelfProjectionError(
                    f"projected row survived its rollback: {table}:{row_id}"
                )
            deleted.append(f"{table}:{row_id}")
        elif outcome == "refreshed":
            row = connection.execute(
                f"SELECT * FROM {_quote_identifier(_PROJECTED_VERSION_TABLE)} "
                "WHERE version_id = ?",
                (row_id,),
            ).fetchone()
            if row is None:
                raise DigitalSelfProjectionError(
                    f"projected version is missing after the rollback: {row_id}"
                )
            values = {
                column: row[column] for column in _PROJECTED_VERSION_STATE_COLUMNS
            }
            if _version_content_digest(values) != str(receipt["target_row_digest"]):
                raise DigitalSelfProjectionError(
                    f"projected version drifted after the rollback: {row_id}"
                )
            if str(values["status"]) != str(receipt["before_status"]):
                raise DigitalSelfProjectionError(
                    f"projected version status moved after the rollback: {row_id}"
                )
            restored.append(f"{_VERSION_TABLE}:{row_id}")
    return {
        "scope": _SCOPE,
        "migration_id": migration_id,
        "manifest_sha256": str(stored["manifest_sha256"]),
        "status": "rolled_back",
        "idempotent": True,
        "deleted_rows": deleted,
        "restored_rows": restored,
        "target_digest_after": _projection_state_digest(*_current_projection(connection)),
    }


def _plan_session(session: _Session, planned_at: str) -> dict[str, Any]:
    connection = _connect_read_only(session.db_path)
    try:
        context = _load_context(connection, session)
        return _build_report(connection, context, session=session, planned_at=planned_at)
    finally:
        connection.close()


def plan(
    db_path: str | Path,
    *,
    identity_path: str | Path | None = None,
    control_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the projection manifest without writing anything.

    The manifest names every source row, the subject it would be projected to,
    and the digests of both databases, so an operator approves exactly this
    write set.
    """

    session = _sources(db_path, identity_path, control_path)
    return _plan_session(session, _now(now).isoformat())


def dry_run(
    db_path: str | Path,
    *,
    identity_path: str | Path | None = None,
    control_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Alias of :func:`plan` kept explicit for operator scripts."""

    report = plan(db_path, identity_path=identity_path, control_path=control_path, now=now)
    report["dry_run"] = True
    return report


def apply(
    db_path: str | Path,
    *,
    identity_path: str | Path | None = None,
    control_path: str | Path | None = None,
    expected_manifest_sha256: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project every provable row in one target transaction.

    Rows already covered by an applied run are skipped; a receipt-covered row
    whose content digest changed refuses the run instead of being rewritten.
    """

    session = _sources(db_path, identity_path, control_path)
    planned_at = _now(now).isoformat()
    planned = _plan_session(session, planned_at)
    manifest_sha256 = str(planned["manifest_sha256"])
    if (
        expected_manifest_sha256 is not None
        and expected_manifest_sha256 != manifest_sha256
    ):
        raise DigitalSelfProjectionError(
            "expected projection manifest SHA-256 does not match the current plan"
        )
    connection = _connect_write(session.db_path)
    try:
        before = _projection_state_digest(*_current_projection(connection))
        if before != str(planned["target_digest_before"]):
            raise DigitalSelfProjectionError(
                "projection target changed after planning and before apply"
            )
        connection.execute("BEGIN IMMEDIATE")
        try:
            context = _load_context(connection, session)
            if context.source.digest != str(planned["source_digest_before"]):
                raise DigitalSelfProjectionError(
                    "Digital Self source changed after planning and before apply"
                )
            if context.identity.digest != str(planned["identity_digest"]):
                raise DigitalSelfProjectionError(
                    "identity evidence changed after planning and before apply"
                )
            run = _run_plan(connection, context)
            if [_entry_dict(entry) for entry in run.entries] != planned["rows"]:
                raise DigitalSelfProjectionError(
                    "current Digital Self rows do not match the projection plan"
                )
            if not run.pending:
                connection.rollback()
                return {
                    **planned,
                    "dry_run": False,
                    "applied": False,
                    "idempotent": True,
                    "migration_id": None,
                }
            _ensure_projection_schema(connection)
            _assert_target_free(connection, run)
            migration_id = _migration_id(
                manifest_sha256, _migration_sequence(connection, manifest_sha256)
            )
            timestamp = _now(now).isoformat()
            # The journal row lands first so the per-row receipts can reference
            # it; a failure anywhere below rolls the whole transaction back.
            connection.execute(
                f"INSERT INTO {_quote_identifier(_MIGRATION_TABLE)} "
                "(migration_id, manifest_sha256, scope, source_digest_before, "
                "source_digest_after, target_digest_before, target_digest_after, "
                "identity_digest, status, manifest_json, statistics_json, created_at, "
                "applied_at, rolled_back_at, rollback_target_digest, source_path, "
                "identity_path, control_path, planned_at) VALUES (?, ?, ?, ?, ?, ?, "
                "?, ?, 'applied', ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
                (
                    migration_id,
                    manifest_sha256,
                    _SCOPE,
                    str(planned["source_digest_before"]),
                    str(planned["source_digest_after"]),
                    str(planned["target_digest_before"]),
                    str(planned["target_digest_after"]),
                    str(planned["identity_digest"]),
                    _canonical(planned["manifest"]),
                    _canonical(planned["statistics"]),
                    timestamp,
                    timestamp,
                    str(session.db_path),
                    str(session.identity_path),
                    str(session.control_path),
                    planned_at,
                ),
            )
            for entry in run.pending:
                _write_entry(
                    connection, entry, migration_id=migration_id, timestamp=timestamp
                )
            after = _projection_state_digest(*_current_projection(connection))
            if after != str(planned["target_digest_after"]):
                raise DigitalSelfProjectionError(
                    "projection digest after apply does not match the planned manifest"
                )
            _insert_projection_audit(
                connection,
                migration_id=migration_id,
                table_name=_MIGRATION_TABLE,
                source_row_id=migration_id,
                action="projection.applied",
                payload={
                    "manifest_sha256": manifest_sha256,
                    "pending_rows": len(run.pending),
                    "writes": run.statistics["writes"],
                },
                created_at=timestamp,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return {
            **planned,
            "dry_run": False,
            "applied": True,
            "idempotent": False,
            "migration_id": migration_id,
            "applied_at": timestamp,
        }
    finally:
        connection.close()


def rollback(
    db_path: str | Path,
    *,
    identity_path: str | Path | None = None,
    control_path: str | Path | None = None,
    migration_id: str | None = None,
    manifest_sha256: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Remove exactly the projected rows of one applied run.

    Only the rows this run recorded are touched: a `mapped` row is deleted and
    a `refreshed` row gets its previous status back, each verified against its
    receipt first.  The source tables are never written, and a second call for
    an already rolled-back run re-verifies that state and reports it again.
    """

    if migration_id is None and manifest_sha256 is None:
        raise DigitalSelfProjectionError(
            "rollback requires migration_id or manifest_sha256"
        )
    session = _sources(db_path, identity_path, control_path)
    connection = _connect_read_only(session.db_path)
    try:
        if not _table_exists(connection, "main", _MIGRATION_TABLE):
            raise DigitalSelfProjectionError(
                "no digital self projection migration found"
            )
        stored = _find_migration(
            connection, migration_id=migration_id, manifest_sha256=manifest_sha256
        )
        stored_id = str(stored["migration_id"])
        receipts = _migration_receipts(connection, stored_id)
        status = str(stored["status"])
        if status == "rolled_back":
            return _rolled_back_report(connection, stored, receipts)
        if status != "applied":
            raise DigitalSelfProjectionError(
                f"unsupported projection status: {status}"
            )
        _verify_rollback_fences(connection, stored_id, receipts)
    finally:
        connection.close()
    connection = _connect_write(session.db_path)
    try:
        _verify_rollback_fences(connection, stored_id, receipts)
        timestamp = _now(now).isoformat()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                f"UPDATE {_quote_identifier(_MIGRATION_TABLE)} "
                "SET status = 'rolling_back' WHERE migration_id = ?",
                (stored_id,),
            )
            deleted, restored = _rollback_rows(connection, stored_id, receipts, timestamp)
            target_after = _projection_state_digest(*_current_projection(connection))
            _insert_projection_audit(
                connection,
                migration_id=stored_id,
                table_name=_MIGRATION_TABLE,
                source_row_id=stored_id,
                action="projection.rolled_back",
                payload={
                    "deleted_rows": len(deleted),
                    "restored_rows": len(restored),
                },
                created_at=timestamp,
            )
            connection.execute(
                f"UPDATE {_quote_identifier(_MIGRATION_TABLE)} "
                "SET status = 'rolled_back', rolled_back_at = ?, "
                "rollback_target_digest = ? WHERE migration_id = ?",
                (timestamp, target_after, stored_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return {
            "scope": _SCOPE,
            "migration_id": stored_id,
            "manifest_sha256": str(stored["manifest_sha256"]),
            "status": "rolled_back",
            "idempotent": False,
            "deleted_rows": deleted,
            "restored_rows": restored,
            "target_digest_after": target_after,
            "rolled_back_at": timestamp,
        }
    finally:
        connection.close()
