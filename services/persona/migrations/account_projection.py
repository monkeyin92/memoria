"""Fail-closed projection of account-keyed Persona data onto durable subjects.

The application still reads the account-keyed Persona tables.  This module is
an operator-invoked, SQLite-only migration seam: it proves the account,
Identity lineage, and evidence subject before copying a row into a
subject-keyed table.  Source tables are never updated or deleted.

The implementation intentionally follows the receipt/manifest/fence shape of
the Digital Self migration, while keeping Persona's six source tables
independent.  A status-only source change can refresh the projected status;
all other source changes are treated as drift until the earlier run is rolled
back.
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

PersonaOutcome = Literal[
    "mapped", "refreshed", "omitted", "quarantined", "already_projected"
]

_SCOPE = "persona_subject_projection"
_RULES_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_TRAIT = "persona_traits"
_EVIDENCE = "persona_evidence"
_OBSERVATION = "persona_observation_receipts"
_STYLE = "speech_style_stats"
_CONSENT = "persona_learning_consents"
_VERSION = "persona_versions"
_EVENT = "evidence_events"
_SOURCE_TABLES = (_TRAIT, _EVIDENCE, _OBSERVATION, _STYLE, _CONSENT, _VERSION)

_PROJECTED = {
    _TRAIT: "persona_subject_traits",
    _EVIDENCE: "persona_subject_evidence",
    _OBSERVATION: "persona_subject_observation_receipts",
    _STYLE: "persona_subject_style_stats",
    _CONSENT: "persona_subject_learning_consents",
    _VERSION: "persona_subject_versions",
}

_MIGRATIONS = "persona_projection_migrations"
_RECEIPTS = "persona_projection_receipts"
_QUARANTINE = "persona_projection_quarantine"
_BACKUPS = "persona_projection_backups"
_AUDIT = "persona_projection_audit_events"

_IDENTITY_SCHEMA = "persona_identity"
_CONTROL_SCHEMA = "persona_control"

_TRAIT_COLUMNS = (
    "trait_id",
    "account_id",
    "category",
    "normalized_key",
    "description",
    "context",
    "counterexample",
    "confidence",
    "status",
    "observation_count",
    "created_at",
    "updated_at",
    "review_event_id",
)
_EVIDENCE_COLUMNS = (
    "trait_id",
    "account_id",
    "source_event_id",
    "scene",
    "weight",
    "occurred_at",
)
_OBSERVATION_COLUMNS = ("source_event_id", "account_id", "observed_at")
_STYLE_COLUMNS = (
    "account_id",
    "scene",
    "utterance_count",
    "char_count",
    "speech_duration_ms",
    "pause_ratio_sum",
    "pause_sample_count",
    "tic_counts_json",
    "updated_at",
)
_CONSENT_COLUMNS = (
    "account_id",
    "policy_version",
    "granted_at",
    "revoked_at",
    "grant_event_id",
    "revoke_event_id",
)
_VERSION_COLUMNS = (
    "version_id",
    "account_id",
    "version_number",
    "status",
    "reason",
    "snapshot_json",
    "parent_version_id",
    "created_at",
)
_EVENT_COLUMNS = (
    "event_id",
    "account_id",
    "event_type",
    "schema_version",
    "occurred_at",
    "recorded_at",
    "subject_id",
    "speaker_identity_id",
    "speaker_class",
    "source",
    "consent_grant_id",
    "payload_json",
    "content_sha256",
    "supersedes_event_id",
)

_COLUMNS = {
    _TRAIT: _TRAIT_COLUMNS,
    _EVIDENCE: _EVIDENCE_COLUMNS,
    _OBSERVATION: _OBSERVATION_COLUMNS,
    _STYLE: _STYLE_COLUMNS,
    _CONSENT: _CONSENT_COLUMNS,
    _VERSION: _VERSION_COLUMNS,
}
_MUTABLE_COLUMNS = {_TRAIT: frozenset({"status"}), _VERSION: frozenset({"status"})}

_TARGET_COLUMNS = {
    table: (*columns, "source_account_id", "subject_id", "projection_migration_id", "projected_at")
    for table, columns in _COLUMNS.items()
}
_MIGRATION_STATUSES = "('applied', 'rolling_back', 'rolled_back')"
_RECEIPT_OUTCOMES = "('mapped', 'refreshed', 'omitted', 'quarantined')"
_SOURCE_CONTENT_FIELDS = {
    table: tuple(column for column in columns if column not in _MUTABLE_COLUMNS.get(table, frozenset()))
    for table, columns in _COLUMNS.items()
}
_STATUS_VALUES = "('candidate', 'confirmed', 'disabled')"
_VERSION_STATUS_VALUES = "('active', 'superseded')"

_SUPPORT_SCHEMA = (
    f"""
    CREATE TABLE IF NOT EXISTS {_MIGRATIONS} (
        migration_id TEXT PRIMARY KEY,
        manifest_sha256 TEXT NOT NULL,
        scope TEXT NOT NULL,
        source_digest_before TEXT NOT NULL,
        source_digest_after TEXT NOT NULL,
        target_digest_before TEXT NOT NULL,
        target_digest_after TEXT NOT NULL,
        identity_digest TEXT NOT NULL,
        control_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN {_MIGRATION_STATUSES}),
        manifest_json TEXT NOT NULL,
        statistics_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        rolled_back_at TEXT,
        rollback_target_digest TEXT,
        source_path TEXT NOT NULL,
        identity_path TEXT NOT NULL,
        control_path TEXT NOT NULL,
        planned_at TEXT NOT NULL
    )
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_persona_projection_migrations_status
    ON {_MIGRATIONS}(status, applied_at DESC)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_RECEIPTS} (
        migration_id TEXT NOT NULL REFERENCES {_MIGRATIONS}(migration_id),
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
    CREATE INDEX IF NOT EXISTS idx_persona_projection_receipts_row
    ON {_RECEIPTS}(table_name, source_row_id)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_QUARANTINE} (
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
    CREATE TABLE IF NOT EXISTS {_BACKUPS} (
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
    CREATE TABLE IF NOT EXISTS {_AUDIT} (
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

_TARGET_PRIMARY_KEYS = {
    _TRAIT: "PRIMARY KEY (trait_id)",
    _EVIDENCE: "PRIMARY KEY (trait_id, source_event_id)",
    _OBSERVATION: "PRIMARY KEY (source_event_id)",
    _STYLE: "PRIMARY KEY (subject_id, scene)",
    _CONSENT: "PRIMARY KEY (subject_id)",
    _VERSION: "PRIMARY KEY (version_id)",
}
_TARGET_PRIMARY_KEY_COLUMNS = {
    _TRAIT: ("trait_id",),
    _EVIDENCE: ("trait_id", "source_event_id"),
    _OBSERVATION: ("source_event_id",),
    _STYLE: ("subject_id", "scene"),
    _CONSENT: ("subject_id",),
    _VERSION: ("version_id",),
}


def _sqlite_type(column: str) -> str:
    if column in {"observation_count", "utterance_count", "char_count", "speech_duration_ms", "pause_sample_count", "version_number"}:
        return "INTEGER"
    if column in {"confidence", "weight", "pause_ratio_sum"}:
        return "REAL"
    return "TEXT"


def _projection_schema_statements() -> tuple[str, ...]:
    statements: list[str] = []
    for source_table, target_table in _PROJECTED.items():
        columns = list(_COLUMNS[source_table])
        definitions = [f"{_quote(column)} {_sqlite_type(column)}" for column in columns]
        definitions.extend(
            [
                "source_account_id TEXT NOT NULL",
                "subject_id TEXT NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 128)",
                "projection_migration_id TEXT NOT NULL",
                "projected_at TEXT NOT NULL",
                _TARGET_PRIMARY_KEYS[source_table],
            ]
        )
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {_quote(target_table)} (" + ", ".join(definitions) + ")"
        )
        if source_table == _TRAIT:
            statements.append(
                f"CREATE INDEX IF NOT EXISTS idx_persona_subject_traits_subject "
                f"ON {_quote(target_table)}(subject_id, status)"
            )
        elif source_table == _VERSION:
            statements.append(
                f"CREATE UNIQUE INDEX IF NOT EXISTS idx_persona_subject_versions_subject_number "
                f"ON {_quote(target_table)}(subject_id, version_number)"
            )
            statements.append(
                f"CREATE INDEX IF NOT EXISTS idx_persona_subject_versions_status "
                f"ON {_quote(target_table)}(subject_id, status, version_number DESC)"
            )
        else:
            statements.append(
                f"CREATE INDEX IF NOT EXISTS idx_{target_table}_subject "
                f"ON {_quote(target_table)}(subject_id)"
            )

        mutable = _MUTABLE_COLUMNS.get(source_table, frozenset())
        immutable_columns = [column for column in columns if column not in mutable]
        immutable_columns.extend(
            ["source_account_id", "subject_id", "projection_migration_id", "projected_at"]
        )
        if immutable_columns:
            update_columns = ", ".join(_quote(column) for column in immutable_columns)
            statements.append(
                f"""
                CREATE TRIGGER IF NOT EXISTS {_quote(target_table + '_immutable')}
                BEFORE UPDATE OF {update_columns} ON {_quote(target_table)}
                BEGIN
                    SELECT RAISE(ABORT, 'projected Persona content is immutable');
                END
                """
            )
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS {_quote(target_table + '_delete_guard')}
            BEFORE DELETE ON {_quote(target_table)}
            WHEN NOT EXISTS (
                SELECT 1 FROM {_quote(_MIGRATIONS)}
                WHERE migration_id = OLD.projection_migration_id
                  AND status = 'rolling_back'
            )
            BEGIN
                SELECT RAISE(ABORT, 'projected Persona rows are removed only by rollback');
            END
            """
        )
    return tuple(statements)

_STATUS_VALUES = "('candidate', 'confirmed', 'disabled')"
_VERSION_STATUS_VALUES = "('active', 'superseded')"
_REGISTERED_TABLES = (("accounts", "user_id"), ("profiles", "user_id"))
_IDENTITY_TABLES = (
    ("identity_persons", ("person_id", "status")),
    ("identity_device_bindings", ("binding_id", "account_owner_person_id", "status")),
    ("identity_device_binding_roles", ("binding_id", "person_id", "role", "status")),
    (
        "identity_relationships",
        ("source_person_id", "target_person_id", "relation_type", "status"),
    ),
)


class PersonaProjectionError(RuntimeError):
    """Raised when Persona data cannot be projected safely or without drift."""


@dataclass(frozen=True, slots=True)
class _Session:
    db_path: Path
    identity_path: Path
    control_path: Path


@dataclass(frozen=True, slots=True)
class _IdentityState:
    persons: Mapping[str, Mapping[str, Any]]
    bindings: tuple[Mapping[str, Any], ...]
    roles: tuple[Mapping[str, Any], ...]
    relationships: tuple[Mapping[str, Any], ...]
    digest: str
    tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SourceState:
    rows: Mapping[str, tuple[dict[str, Any], ...]]
    events: Mapping[str, Mapping[str, Any]]
    digest: str


@dataclass(frozen=True, slots=True)
class _Coverage:
    applied: Mapping[tuple[str, str], Mapping[str, Any]]
    rolled_back: Mapping[tuple[str, str], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class _Context:
    source: _SourceState
    identity: _IdentityState
    registered_accounts: frozenset[str]
    coverage: _Coverage
    control_digest: str
    traits_by_id: Mapping[str, Mapping[str, Any]]
    trait_subjects: Mapping[str, str]
    trait_errors: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Resolution:
    subject_id: str | None
    subject_class: str
    reason: str
    details: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.subject_id is not None


@dataclass(frozen=True, slots=True)
class _Entry:
    table: str
    row_id: str
    account_id: str | None
    subject_id: str | None
    subject_class: str
    outcome: PersonaOutcome
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
    entries: tuple[_Entry, ...]
    pending: tuple[_Entry, ...]
    statistics: dict[str, Any]


def _quote(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise PersonaProjectionError(f"unsafe SQLite identifier: {value!r}")
    return f'"{value}"'


def _qualified(schema: str, table: str) -> str:
    return f"{_quote(schema)}.{_quote(table)}"


def _normalise(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


def _canonical(value: object) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("migration timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _same_path(first: Path, second: Path) -> bool:
    return first.expanduser().resolve() == second.expanduser().resolve()


def _row_digest(table: str, row: Mapping[str, Any], *, include_mutable: bool) -> str:
    columns = _COLUMNS[table]
    mutable = _MUTABLE_COLUMNS.get(table, frozenset())
    values = [
        {"name": column, "value": _json_safe(row.get(column))}
        for column in columns
        if include_mutable or column not in mutable
    ]
    return _sha256({"table": table, "values": values})


def _target_content_digest(table: str, row: Mapping[str, Any]) -> str:
    """Digest immutable projected content, excluding refreshable statuses."""

    projected = _PROJECTED[table]
    mutable = _MUTABLE_COLUMNS.get(table, frozenset())
    columns = tuple(
        column
        for column in (*_COLUMNS[table], "source_account_id", "subject_id")
        if column not in mutable
    )
    return _sha256(
        {
            "table": projected,
            "values": [
                {"name": column, "value": _json_safe(row[column])}
                for column in sorted(columns)
            ],
        }
    )


def _read_only_uri(path: Path) -> str:
    return f"{path.expanduser().resolve().as_uri()}?mode=ro"


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise PersonaProjectionError(f"Persona SQLite database does not exist: {path}")
    connection = sqlite3.connect(_read_only_uri(path), uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _connect_write(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path.as_uri(), uri=True, isolation_level=None, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _attach_read_only(connection: sqlite3.Connection, path: Path, schema: str, current: Path) -> str:
    if _same_path(path, current):
        return "main"
    connection.execute(f"ATTACH DATABASE ? AS {_quote(schema)}", (_read_only_uri(path),))
    return schema


def _table_exists(connection: sqlite3.Connection, schema: str, table: str) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {_quote(schema)}.sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(connection: sqlite3.Connection, schema: str, table: str) -> tuple[str, ...]:
    rows = connection.execute(f"PRAGMA {_quote(schema)}.table_info({_quote(table)})").fetchall()
    return tuple(str(row["name"]) for row in rows)


def _require_columns(connection: sqlite3.Connection, schema: str, table: str, required: Iterable[str]) -> None:
    columns = _columns(connection, schema, table)
    if not columns:
        raise PersonaProjectionError(f"missing SQLite table {schema}.{table}")
    missing = sorted(set(required).difference(columns))
    if missing:
        raise PersonaProjectionError(f"{schema}.{table} is missing required columns: {', '.join(missing)}")


def _read_rows(connection: sqlite3.Connection, schema: str, table: str, columns: Sequence[str]) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(f"SELECT * FROM {_qualified(schema, table)}").fetchall()
    return tuple({column: row[column] for column in columns} for row in rows)


def _read_all_rows(
    connection: sqlite3.Connection, schema: str, table: str
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    columns = _columns(connection, schema, table)
    if not columns:
        raise PersonaProjectionError(f"missing SQLite table {schema}.{table}")
    return columns, _read_rows(connection, schema, table, columns)


def _table_digest(
    table: str, columns: Sequence[str], rows: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    payloads = [
        {column: _json_safe(row.get(column)) for column in columns}
        for row in rows
    ]
    return {
        "table": table,
        "columns": list(columns),
        "rows": sorted(payloads, key=_canonical),
    }


def _sources(db_path: str | Path, identity_path: str | Path | None, control_path: str | Path | None) -> _Session:
    db = Path(db_path).expanduser().resolve()
    identity = Path(identity_path).expanduser().resolve() if identity_path is not None else db
    control = Path(control_path).expanduser().resolve() if control_path is not None else db
    return _Session(db_path=db, identity_path=identity, control_path=control)


def _source_digest(rows: Mapping[str, Sequence[Mapping[str, Any]]], events: Mapping[str, Mapping[str, Any]]) -> str:
    payload: dict[str, Any] = {}
    for table in _SOURCE_TABLES:
        payload[table] = [
            {"id": _source_row_id(table, row), "digest": _row_digest(table, row, include_mutable=True)}
            for row in sorted(rows[table], key=lambda item: _source_row_id(table, item))
        ]
    payload[_EVENT] = [
        {"id": event_id, "digest": _sha256({column: _json_safe(row.get(column)) for column in _EVENT_COLUMNS})}
        for event_id, row in sorted(events.items())
    ]
    return _sha256(payload)


def _load_source(connection: sqlite3.Connection) -> _SourceState:
    rows: dict[str, tuple[dict[str, Any], ...]] = {}
    for table in _SOURCE_TABLES:
        _require_columns(connection, "main", table, _COLUMNS[table])
        rows[table] = _read_rows(connection, "main", table, _COLUMNS[table])
    _require_columns(connection, "main", _EVENT, _EVENT_COLUMNS)
    event_rows = _read_rows(connection, "main", _EVENT, _EVENT_COLUMNS)
    events = {str(row["event_id"]): row for row in event_rows}
    return _SourceState(rows=rows, events=events, digest=_source_digest(rows, events))


def _load_identity(connection: sqlite3.Connection, schema: str) -> _IdentityState:
    for table, required in _IDENTITY_TABLES:
        _require_columns(connection, schema, table, required)
    person_columns, person_rows = _read_all_rows(
        connection, schema, "identity_persons"
    )
    binding_columns, bindings = _read_all_rows(
        connection, schema, "identity_device_bindings"
    )
    role_columns, roles = _read_all_rows(
        connection, schema, "identity_device_binding_roles"
    )
    relationship_columns, relationships = _read_all_rows(
        connection, schema, "identity_relationships"
    )
    persons = {str(row["person_id"]): row for row in person_rows}
    digest = _sha256(
        [
            _table_digest("identity_persons", person_columns, person_rows),
            _table_digest(
                "identity_device_bindings", binding_columns, bindings
            ),
            _table_digest(
                "identity_device_binding_roles", role_columns, roles
            ),
            _table_digest(
                "identity_relationships",
                relationship_columns,
                relationships,
            ),
        ]
    )
    return _IdentityState(
        persons=persons,
        bindings=bindings,
        roles=roles,
        relationships=relationships,
        digest=digest,
        tables=tuple(table for table, _ in _IDENTITY_TABLES),
    )


def _registered_accounts(connection: sqlite3.Connection, schema: str) -> frozenset[str]:
    accounts: set[str] = set()
    found = False
    for table, column in _REGISTERED_TABLES:
        if not _table_exists(connection, schema, table) or column not in _columns(connection, schema, table):
            continue
        found = True
        rows = connection.execute(
            f"SELECT DISTINCT {_quote(column)} AS account_id FROM {_qualified(schema, table)} WHERE {_quote(column)} IS NOT NULL"
        ).fetchall()
        accounts.update(str(row["account_id"]) for row in rows)
    if not found:
        raise PersonaProjectionError("control accounts or profiles table is required")
    return frozenset(accounts)


def _control_digest(connection: sqlite3.Connection, schema: str) -> str:
    """Digest every column and row of the Control registration tables."""

    tables: list[dict[str, Any]] = []
    for table, _ in _REGISTERED_TABLES:
        if not _table_exists(connection, schema, table):
            continue
        columns, rows = _read_all_rows(connection, schema, table)
        tables.append(_table_digest(table, columns, rows))
    if not tables:
        raise PersonaProjectionError("control accounts or profiles table is required")
    return _sha256(tables)


def _load_context(connection: sqlite3.Connection, session: _Session) -> _Context:
    identity_schema = _attach_read_only(connection, session.identity_path, _IDENTITY_SCHEMA, session.db_path)
    control_schema = _attach_read_only(connection, session.control_path, _CONTROL_SCHEMA, session.db_path)
    source = _load_source(connection)
    base = _Context(
        source=source,
        identity=_load_identity(connection, identity_schema),
        registered_accounts=_registered_accounts(connection, control_schema),
        coverage=_load_coverage(connection),
        control_digest=_control_digest(connection, control_schema),
        traits_by_id={str(row["trait_id"]): row for row in source.rows[_TRAIT]},
        trait_subjects={},
        trait_errors={},
    )
    subjects, errors = _derive_trait_metadata(base)
    return _Context(
        source=base.source,
        identity=base.identity,
        registered_accounts=base.registered_accounts,
        coverage=base.coverage,
        control_digest=base.control_digest,
        traits_by_id=base.traits_by_id,
        trait_subjects=subjects,
        trait_errors=errors,
    )


def _source_row_id(table: str, row: Mapping[str, Any]) -> str:
    if table in {_TRAIT, _VERSION, _CONSENT, _OBSERVATION}:
        key = {
            _TRAIT: "trait_id",
            _VERSION: "version_id",
            _CONSENT: "account_id",
            _OBSERVATION: "source_event_id",
        }[table]
        return str(row.get(key) or "")
    if table == _EVIDENCE:
        return f"{row.get('trait_id', '')}::{row.get('source_event_id', '')}"
    if table == _STYLE:
        return f"{row.get('account_id', '')}::{row.get('scene', '')}"
    raise PersonaProjectionError(f"unsupported Persona source table: {table}")


def _row_json(row: Mapping[str, Any], columns: Sequence[str]) -> dict[str, Any]:
    return {column: _json_safe(row.get(column)) for column in columns}


def _owner_evidence(account_id: str, identity: _IdentityState) -> tuple[str | None, tuple[str, ...]]:
    active_bindings = {
        str(row["binding_id"]): _normalise(row["account_owner_person_id"])
        for row in identity.bindings
        if _normalise(row["status"]) == "active"
    }
    owned = sorted(binding_id for binding_id, owner in active_bindings.items() if owner == account_id)
    if owned:
        return "owner_binding", tuple(owned)
    role_bindings = sorted(
        {
            str(row["binding_id"])
            for row in identity.roles
            if _normalise(row["person_id"]) == account_id
            and _normalise(row["role"]) == "account_owner"
            and _normalise(row["status"]) == "active"
            and str(row["binding_id"]) in active_bindings
        }
    )
    if role_bindings:
        return "owner_binding_role", tuple(role_bindings)
    self_rows = sorted(
        {
            str(row["source_person_id"])
            for row in identity.relationships
            if _normalise(row["source_person_id"]) == account_id
            and _normalise(row["target_person_id"]) == account_id
            and _normalise(row["relation_type"]) == "self"
            and _normalise(row["status"]) == "active"
        }
    )
    if self_rows:
        return "owner_relationship", tuple(self_rows)
    return None, ()


def _member_owners(subject_id: str, identity: _IdentityState) -> tuple[dict[str, tuple[str, ...]], bool]:
    """Return active, explicit account->lineage evidence for a member."""

    owners: dict[str, list[str]] = {}
    ambiguous_signal = False
    parent_relations = {
        "parent_of",
        "guardian_of",
        "caregiver_of",
        "delegate_for",
        "family_member_of",
        "co_subject_of",
        "beneficiary_of",
    }
    inverse_relations = {"child_of", "ward_of", "delegate_for", "beneficiary_of"}
    for row in identity.relationships:
        if _normalise(row["status"]) != "active":
            continue
        source = _normalise(row["source_person_id"])
        target = _normalise(row["target_person_id"])
        relation = _normalise(row["relation_type"])
        if source is None or target is None or source == target:
            continue
        if target == subject_id and relation in parent_relations:
            owners.setdefault(source, []).append(f"relationship:{relation}")
        elif source == subject_id and relation in inverse_relations:
            owners.setdefault(target, []).append(f"relationship:{relation}")
    active_bindings = {
        str(row["binding_id"]): _normalise(row["account_owner_person_id"])
        for row in identity.bindings
        if _normalise(row["status"]) == "active"
    }
    for row in identity.roles:
        if _normalise(row["status"]) != "active" or _normalise(row["person_id"]) != subject_id:
            continue
        role = _normalise(row["role"])
        if role not in {"member", "primary_subject", "guardian", "delegate"}:
            continue
        owner = active_bindings.get(str(row["binding_id"]))
        if owner is not None:
            owners.setdefault(owner, []).append(f"binding_role:{role}:{row['binding_id']}")
    # A subject with multiple distinct lineage records is not itself ambiguous;
    # multiple distinct owner accounts are.
    if len(owners) > 1:
        ambiguous_signal = True
    return {owner: tuple(sorted(values)) for owner, values in owners.items()}, ambiguous_signal


def _resolve_subject(
    account_id: str | None,
    subjects: Iterable[object],
    context: _Context,
) -> _Resolution:
    if account_id is None:
        return _Resolution(None, "no-owner-evidence", "account_id_missing")
    if account_id not in context.registered_accounts:
        return _Resolution(None, "no-owner-evidence", "owner_account_unregistered", (account_id,))
    owner_person = context.identity.persons.get(account_id)
    if owner_person is None:
        return _Resolution(None, "no-owner-evidence", "owner_person_missing", (account_id,))
    if _normalise(owner_person.get("status")) != "active":
        return _Resolution(None, "inactive", "owner_person_inactive", (account_id,))
    owner_reason, owner_details = _owner_evidence(account_id, context.identity)
    values = [_normalise(value) for value in subjects]
    non_null = {value for value in values if value is not None}
    if not values:
        return _Resolution(None, "no-subject-evidence", "no_subject_evidence")
    if not non_null:
        return _Resolution(None, "no-subject-evidence", "no_subject_evidence")
    if len(non_null) != 1 or any(value is None for value in values):
        return _Resolution(None, "mixed-subject-evidence", "mixed_subject_evidence", tuple(sorted(non_null)))
    subject_id = next(iter(non_null))
    subject_person = context.identity.persons.get(subject_id)
    if subject_person is None or _normalise(subject_person.get("status")) != "active":
        return _Resolution(None, "inactive", "inactive_subject", (subject_id,))
    if subject_id == account_id:
        if owner_reason is None:
            return _Resolution(None, "no-owner-evidence", "no_owner_evidence", (account_id,))
        return _Resolution(subject_id, "owner/self", owner_reason, owner_details)
    if owner_reason is None:
        return _Resolution(None, "no-owner-evidence", "no_owner_evidence", (account_id,))
    owners, ambiguous = _member_owners(subject_id, context.identity)
    if ambiguous:
        return _Resolution(None, "ambiguous", "ambiguous_subject", tuple(sorted(owners)))
    if not owners:
        return _Resolution(None, "no-subject-evidence", "no_subject_evidence", (subject_id,))
    if set(owners) != {account_id}:
        return _Resolution(None, "foreign", "foreign_subject", tuple(sorted(owners)))
    details = owner_details + tuple(f"{subject_id}:{item}" for item in owners[account_id])
    return _Resolution(subject_id, "child/member", "child_member_lineage", details)


def _event_subjects(events: Mapping[str, Mapping[str, Any]], event_ids: Iterable[str]) -> tuple[object, ...]:
    return tuple(events[event_id].get("subject_id") for event_id in event_ids if event_id in events)


def _current_projection(connection: sqlite3.Connection) -> dict[str, tuple[dict[str, Any], ...]]:
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for source_table, target_table in _PROJECTED.items():
        if not _table_exists(connection, "main", target_table):
            result[source_table] = ()
            continue
        columns = _columns(connection, "main", target_table)
        missing = sorted(set(_TARGET_COLUMNS[source_table]).difference(columns))
        if missing:
            count = int(
                connection.execute(
                    f"SELECT count(*) FROM {_quote(target_table)}"
                ).fetchone()[0]
            )
            if count:
                raise PersonaProjectionError(
                    f"{target_table} is missing required columns: {', '.join(missing)}"
                )
            result[source_table] = ()
            continue
        rows = connection.execute(
            f"SELECT * FROM {_quote(target_table)} ORDER BY rowid"
        ).fetchall()
        result[source_table] = tuple(
            {column: row[column] for column in _TARGET_COLUMNS[source_table]}
            for row in rows
        )
    return result


def _projection_digest(projection: Mapping[str, Iterable[Mapping[str, Any]]]) -> str:
    return _sha256(
        {
            table: [
                {
                    "row_id": _target_row_id(table, row),
                    "content_digest": _target_content_digest(table, row),
                    "mutable": {
                        column: _json_safe(row.get(column))
                        for column in sorted(_MUTABLE_COLUMNS.get(table, frozenset()))
                    },
                }
                for row in sorted(rows, key=lambda item: _target_row_id(table, item))
            ]
            for table, rows in sorted(projection.items())
        }
    )


def _target_row_id(table: str, row: Mapping[str, Any]) -> str:
    if table == _TRAIT:
        return str(row.get("trait_id"))
    if table == _EVIDENCE:
        return f"{row.get('trait_id')}::{row.get('source_event_id')}"
    if table == _OBSERVATION:
        return str(row.get("source_event_id"))
    if table == _STYLE:
        return f"{row.get('subject_id')}::{row.get('scene')}"
    if table == _CONSENT:
        return str(row.get("subject_id"))
    if table == _VERSION:
        return str(row.get("version_id"))
    raise PersonaProjectionError(f"unsupported target row table: {table}")


def _load_coverage(connection: sqlite3.Connection) -> _Coverage:
    if not _table_exists(connection, "main", _RECEIPTS):
        return _Coverage(applied={}, rolled_back={})
    if not _table_exists(connection, "main", _MIGRATIONS):
        raise PersonaProjectionError("Persona receipts exist without a migration journal")
    rows = connection.execute(
        f"SELECT r.*, m.status AS migration_status, m.applied_at FROM {_quote(_RECEIPTS)} r "
        f"LEFT JOIN {_quote(_MIGRATIONS)} m ON m.migration_id = r.migration_id "
        "ORDER BY COALESCE(m.applied_at, '' ) DESC, r.recorded_at DESC, r.migration_id DESC"
    ).fetchall()
    applied: dict[tuple[str, str], Mapping[str, Any]] = {}
    rolled: dict[tuple[str, str], Mapping[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        status = _normalise(row["migration_status"])
        if status is None:
            raise PersonaProjectionError(f"Persona receipt has no migration journal: {row['migration_id']}")
        if status not in {"applied", "rolled_back"}:
            raise PersonaProjectionError(f"Persona migration is mid-rollback: {row['migration_id']}")
        key = (str(row["table_name"]), str(row["source_row_id"]))
        if key in seen:
            continue
        seen.add(key)
        receipt = {str(column): row[column] for column in row.keys()}
        bucket = applied if status == "applied" else rolled
        bucket[key] = receipt
    return _Coverage(applied=applied, rolled_back=rolled)


def _source_content_digest(table: str, row: Mapping[str, Any]) -> str:
    """Digest source content while intentionally excluding status-only fields."""

    return _sha256(
        {
            "table": table,
            "values": [
                {"name": column, "value": _json_safe(row.get(column))}
                for column in _SOURCE_CONTENT_FIELDS[table]
            ],
        }
    )


def _source_full_digest(table: str, row: Mapping[str, Any]) -> str:
    return _row_digest(table, row, include_mutable=True)


def _target_table(source_table: str) -> str:
    try:
        return _PROJECTED[source_table]
    except KeyError as exc:
        raise PersonaProjectionError(f"unsupported Persona source table: {source_table}") from exc


def _target_key_column(source_table: str) -> tuple[str, ...]:
    if source_table == _TRAIT:
        return ("trait_id",)
    if source_table == _EVIDENCE:
        return ("trait_id", "source_event_id")
    if source_table == _OBSERVATION:
        return ("source_event_id",)
    if source_table == _STYLE:
        return ("subject_id", "scene")
    if source_table == _CONSENT:
        return ("subject_id",)
    if source_table == _VERSION:
        return ("version_id",)
    raise PersonaProjectionError(f"unsupported Persona source table: {source_table}")


def _target_key(source_table: str, row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(column) or "") for column in _target_key_column(source_table))


def _source_row(source_table: str, row: Mapping[str, Any]) -> dict[str, Any]:
    return {column: _json_safe(row.get(column)) for column in _COLUMNS[source_table]}


def _projected_payload(
    source_table: str,
    row: Mapping[str, Any],
    subject_id: str,
) -> dict[str, Any]:
    account_id = _normalise(row.get("account_id"))
    if account_id is None:
        raise PersonaProjectionError(
            f"cannot build projected {source_table} row without account_id"
        )
    payload = {column: row.get(column) for column in _COLUMNS[source_table]}
    payload["source_account_id"] = account_id
    payload["subject_id"] = subject_id
    return payload


def _blocked_entry(
    source_table: str,
    row: Mapping[str, Any],
    *,
    outcome: PersonaOutcome,
    reason: str,
    details: Iterable[str] = (),
    subject_id: str | None = None,
    evidence_ids: Iterable[str] = (),
    subject_class: str | None = None,
) -> _Entry:
    row_id = _source_row_id(source_table, row)
    account_id = _normalise(row.get("account_id"))
    status = _normalise(row.get("status")) if source_table in {_TRAIT, _VERSION} else None
    return _Entry(
        table=source_table,
        row_id=row_id,
        account_id=account_id,
        subject_id=subject_id,
        subject_class=(
            subject_class
            if subject_class is not None
            else ("omitted" if outcome == "omitted" else "quarantined")
        ),
        outcome=outcome,
        reason=reason,
        details=tuple(str(item) for item in details),
        source_digest=_source_content_digest(source_table, row),
        target_digest=None,
        source_row=_source_row(source_table, row),
        target_row=None,
        evidence_ids=tuple(str(item) for item in evidence_ids),
        after_status=status,
    )


def _mapped_entry(
    source_table: str,
    row: Mapping[str, Any],
    resolution: _Resolution,
    *,
    evidence_ids: Iterable[str] = (),
    outcome: PersonaOutcome = "mapped",
    before_status: str | None = None,
) -> _Entry:
    if not resolution.valid or resolution.subject_id is None:
        raise PersonaProjectionError("cannot create a mapped Persona entry without subject")
    payload = _projected_payload(source_table, row, resolution.subject_id)
    status = _normalise(row.get("status")) if source_table in {_TRAIT, _VERSION} else None
    return _Entry(
        table=source_table,
        row_id=_source_row_id(source_table, row),
        account_id=_normalise(row.get("account_id")),
        subject_id=resolution.subject_id,
        subject_class=resolution.subject_class,
        outcome=outcome,
        reason=resolution.reason,
        details=resolution.details,
        source_digest=_source_content_digest(source_table, row),
        target_digest=_target_content_digest(source_table, payload),
        source_row=_source_row(source_table, row),
        target_row=payload,
        evidence_ids=tuple(str(item) for item in evidence_ids),
        before_status=before_status,
        after_status=status,
    )


def _resolution_for_event_subjects(
    account_id: str | None,
    subjects: Iterable[object],
    context: _Context,
) -> _Resolution:
    return _resolve_subject(account_id, subjects, context)


def _trait_resolution(
    row: Mapping[str, Any],
    context: _Context,
) -> tuple[_Resolution, tuple[str, ...]]:
    """Validate a trait and its complete Persona evidence lineage."""

    trait_id = _normalise(row.get("trait_id"))
    account_id = _normalise(row.get("account_id"))
    if trait_id is None:
        return _Resolution(None, "ambiguous", "trait_id_missing"), ()
    if account_id is None:
        return _Resolution(None, "no-owner-evidence", "account_id_missing"), ()
    evidence_rows = tuple(
        item for item in context.source.rows[_EVIDENCE]
        if _normalise(item.get("trait_id")) == trait_id
    )
    if not evidence_rows:
        return _Resolution(None, "no-subject-evidence", "trait_evidence_missing"), ()
    subjects: list[object] = []
    event_ids: list[str] = []
    for evidence in evidence_rows:
        evidence_account = _normalise(evidence.get("account_id"))
        if evidence_account != account_id:
            return (
                _Resolution(None, "foreign", "evidence_account_mismatch", (str(evidence_account),)),
                tuple(event_ids),
            )
        event_id = _normalise(evidence.get("source_event_id"))
        if event_id is None:
            return _Resolution(None, "no-subject-evidence", "source_event_missing"), tuple(event_ids)
        event = context.source.events.get(event_id)
        if event is None:
            return _Resolution(None, "no-subject-evidence", "source_event_missing", (event_id,)), tuple(event_ids)
        if _normalise(event.get("account_id")) != account_id:
            return _Resolution(None, "foreign", "event_account_mismatch", (event_id,)), tuple(event_ids)
        event_ids.append(event_id)
        subjects.append(event.get("subject_id"))
    resolution = _resolution_for_event_subjects(account_id, subjects, context)
    return resolution, tuple(sorted(set(event_ids)))


def _derive_trait_metadata(
    context: _Context,
) -> tuple[dict[str, str], dict[str, str]]:
    subjects: dict[str, str] = {}
    errors: dict[str, str] = {}
    for row in sorted(context.source.rows[_TRAIT], key=lambda item: _source_row_id(_TRAIT, item)):
        trait_id = _normalise(row.get("trait_id"))
        if trait_id is None:
            continue
        resolution, _ = _trait_resolution(row, context)
        if resolution.valid and resolution.subject_id is not None:
            subjects[trait_id] = resolution.subject_id
        else:
            errors[trait_id] = resolution.reason
    return subjects, errors


def _trait_failure_resolution(
    trait_id: str,
    trait: Mapping[str, Any],
    context: _Context,
) -> _Resolution:
    """Return the trait's root refusal instead of hiding it behind a dependency error."""

    resolution, _ = _trait_resolution(trait, context)
    if resolution.valid:
        raise PersonaProjectionError(
            f"trait subject metadata is inconsistent for {trait_id}"
        )
    return _Resolution(
        None,
        resolution.subject_class,
        resolution.reason,
        (trait_id, *resolution.details),
    )


def _ensure_projection_schema(connection: sqlite3.Connection) -> None:
    """Create migration, target, guard, and audit tables in the caller transaction."""

    for statement in _SUPPORT_SCHEMA:
        connection.execute(statement)
    present = set(_columns(connection, "main", _MIGRATIONS))
    additions = {
        "control_digest": "TEXT NOT NULL DEFAULT ''",
        "source_path": "TEXT NOT NULL DEFAULT ''",
        "identity_path": "TEXT NOT NULL DEFAULT ''",
        "control_path": "TEXT NOT NULL DEFAULT ''",
        "planned_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in present:
            connection.execute(
                f"ALTER TABLE {_quote(_MIGRATIONS)} ADD COLUMN {_quote(name)} {definition}"
            )
    for source_table, target_table in _PROJECTED.items():
        if _table_exists(connection, "main", target_table):
            info = connection.execute(
                f"PRAGMA table_info({_quote(target_table)})"
            ).fetchall()
            primary_key = tuple(
                str(row["name"])
                for row in sorted(
                    (row for row in info if int(row["pk"]) > 0),
                    key=lambda row: int(row["pk"]),
                )
            )
            expected = _TARGET_PRIMARY_KEY_COLUMNS[source_table]
            if primary_key != expected:
                count = int(
                    connection.execute(
                        f"SELECT count(*) FROM {_quote(target_table)}"
                    ).fetchone()[0]
                )
                if count:
                    raise PersonaProjectionError(
                        f"legacy Persona target schema has unsafe primary key: {target_table}"
                    )
                connection.execute(f"DROP TABLE {_quote(target_table)}")
        connection.execute(
            f"DROP TRIGGER IF EXISTS {_quote(target_table + '_immutable')}"
        )
        connection.execute(
            f"DROP TRIGGER IF EXISTS {_quote(target_table + '_delete_guard')}"
        )
    for statement in _projection_schema_statements():
        connection.execute(statement)


def _entry_from_resolution(
    source_table: str,
    row: Mapping[str, Any],
    resolution: _Resolution,
    *,
    evidence_ids: Iterable[str] = (),
) -> _Entry:
    if resolution.valid:
        return _mapped_entry(
            source_table, row, resolution, evidence_ids=evidence_ids
        )
    outcome: PersonaOutcome = (
        "omitted"
        if resolution.subject_class == "no-subject-evidence"
        else "quarantined"
    )
    return _blocked_entry(
        source_table,
        row,
        outcome=outcome,
        reason=resolution.reason,
        details=resolution.details,
        evidence_ids=evidence_ids,
        subject_class=resolution.subject_class,
    )


def _classify_trait(row: Mapping[str, Any], context: _Context) -> _Entry:
    status = _normalise(row.get("status"))
    if status not in {"candidate", "confirmed", "disabled"}:
        return _blocked_entry(
            _TRAIT,
            row,
            outcome="quarantined",
            reason="trait_status_unsupported",
            details=(str(status),),
            subject_class="ambiguous",
        )
    resolution, evidence_ids = _trait_resolution(row, context)
    return _entry_from_resolution(
        _TRAIT, row, resolution, evidence_ids=evidence_ids
    )


def _classify_evidence(row: Mapping[str, Any], context: _Context) -> _Entry:
    trait_id = _normalise(row.get("trait_id"))
    account_id = _normalise(row.get("account_id"))
    event_id = _normalise(row.get("source_event_id"))
    if trait_id is None:
        return _blocked_entry(
            _EVIDENCE, row, outcome="quarantined", reason="trait_id_missing"
        )
    trait = context.traits_by_id.get(trait_id)
    if trait is None:
        return _blocked_entry(
            _EVIDENCE,
            row,
            outcome="quarantined",
            reason="trait_missing",
            details=(trait_id,),
            subject_class="no-subject-evidence",
        )
    if account_id is None or _normalise(trait.get("account_id")) != account_id:
        return _blocked_entry(
            _EVIDENCE,
            row,
            outcome="quarantined",
            reason="trait_account_mismatch",
            details=(trait_id,),
            subject_class="foreign",
        )
    if event_id is None or event_id not in context.source.events:
        return _blocked_entry(
            _EVIDENCE,
            row,
            outcome="omitted",
            reason="source_event_missing",
            details=((event_id,) if event_id is not None else ()),
            subject_class="no-subject-evidence",
        )
    event = context.source.events[event_id]
    if _normalise(event.get("account_id")) != account_id:
        return _blocked_entry(
            _EVIDENCE,
            row,
            outcome="quarantined",
            reason="event_account_mismatch",
            details=(event_id,),
            evidence_ids=(event_id,),
            subject_class="foreign",
        )
    trait_subject = context.trait_subjects.get(trait_id)
    if trait_subject is None:
        return _entry_from_resolution(
            _EVIDENCE,
            row,
            _trait_failure_resolution(trait_id, trait, context),
            evidence_ids=(event_id,),
        )
    resolution = _resolve_subject(
        account_id, (event.get("subject_id"),), context
    )
    if resolution.valid and resolution.subject_id != trait_subject:
        resolution = _Resolution(
            None,
            "mixed-subject-evidence",
            "trait_subject_mismatch",
            (trait_subject, str(resolution.subject_id)),
        )
    return _entry_from_resolution(
        _EVIDENCE, row, resolution, evidence_ids=(event_id,)
    )


def _classify_observation(row: Mapping[str, Any], context: _Context) -> _Entry:
    account_id = _normalise(row.get("account_id"))
    event_id = _normalise(row.get("source_event_id"))
    if event_id is None or event_id not in context.source.events:
        return _blocked_entry(
            _OBSERVATION,
            row,
            outcome="omitted",
            reason="source_event_missing",
            details=((event_id,) if event_id is not None else ()),
            subject_class="no-subject-evidence",
        )
    event = context.source.events[event_id]
    if account_id is None or _normalise(event.get("account_id")) != account_id:
        return _blocked_entry(
            _OBSERVATION,
            row,
            outcome="quarantined",
            reason="event_account_mismatch",
            details=(event_id,),
            evidence_ids=(event_id,),
            subject_class="foreign",
        )
    resolution = _resolve_subject(
        account_id, (event.get("subject_id"),), context
    )
    return _entry_from_resolution(
        _OBSERVATION, row, resolution, evidence_ids=(event_id,)
    )


def _classify_style(row: Mapping[str, Any], context: _Context) -> _Entry:
    account_id = _normalise(row.get("account_id"))
    scene = _normalise(row.get("scene"))
    evidence_rows = tuple(
        evidence
        for evidence in context.source.rows[_EVIDENCE]
        if _normalise(evidence.get("account_id")) == account_id
        and _normalise(evidence.get("scene")) == scene
    )
    event_ids: list[str] = []
    subjects: list[object] = []
    for evidence in evidence_rows:
        event_id = _normalise(evidence.get("source_event_id"))
        if event_id is None or event_id not in context.source.events:
            return _blocked_entry(
                _STYLE,
                row,
                outcome="omitted",
                reason="style_subject_event_missing",
                details=((event_id,) if event_id is not None else ()),
                evidence_ids=event_ids,
                subject_class="no-subject-evidence",
            )
        event = context.source.events[event_id]
        if _normalise(event.get("account_id")) != account_id:
            return _blocked_entry(
                _STYLE,
                row,
                outcome="quarantined",
                reason="style_event_account_mismatch",
                details=(event_id,),
                evidence_ids=(*event_ids, event_id),
                subject_class="foreign",
            )
        event_ids.append(event_id)
        subjects.append(event.get("subject_id"))
    resolution = _resolve_subject(account_id, subjects, context)
    return _entry_from_resolution(
        _STYLE, row, resolution, evidence_ids=tuple(sorted(set(event_ids)))
    )


def _classify_consent(row: Mapping[str, Any], context: _Context) -> _Entry:
    account_id = _normalise(row.get("account_id"))
    grant_event_id = _normalise(row.get("grant_event_id"))
    revoke_event_id = _normalise(row.get("revoke_event_id"))
    revoked_at = _normalise(row.get("revoked_at"))
    if (revoke_event_id is None) != (revoked_at is None):
        return _blocked_entry(
            _CONSENT,
            row,
            outcome="quarantined",
            reason="consent_revoke_state_mismatch",
            subject_class="ambiguous",
        )
    event_specs: list[tuple[str | None, str, str]] = [
        (grant_event_id, "consent.granted", "grant")
    ]
    if revoke_event_id is not None:
        event_specs.append((revoke_event_id, "consent.revoked", "revoke"))
    event_ids: list[str] = []
    subjects: list[object] = []
    for event_id, expected_type, label in event_specs:
        if event_id is None or event_id not in context.source.events:
            return _blocked_entry(
                _CONSENT,
                row,
                outcome="omitted",
                reason=f"consent_{label}_event_missing",
                details=((event_id,) if event_id is not None else ()),
                evidence_ids=event_ids,
                subject_class="no-subject-evidence",
            )
        event = context.source.events[event_id]
        if _normalise(event.get("account_id")) != account_id:
            return _blocked_entry(
                _CONSENT,
                row,
                outcome="quarantined",
                reason=f"consent_{label}_event_account_mismatch",
                details=(event_id,),
                evidence_ids=(*event_ids, event_id),
                subject_class="foreign",
            )
        if _normalise(event.get("event_type")) != expected_type:
            return _blocked_entry(
                _CONSENT,
                row,
                outcome="quarantined",
                reason=f"consent_{label}_event_type_mismatch",
                details=(event_id, str(event.get("event_type"))),
                evidence_ids=(*event_ids, event_id),
                subject_class="ambiguous",
            )
        event_ids.append(event_id)
        subjects.append(event.get("subject_id"))
    resolution = _resolve_subject(account_id, subjects, context)
    return _entry_from_resolution(
        _CONSENT, row, resolution, evidence_ids=tuple(event_ids)
    )


def _snapshot_items(raw: object) -> list[Mapping[str, Any]] | None:
    try:
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        decoded = json.loads(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, list) or not all(
        isinstance(item, Mapping) for item in decoded
    ):
        return None
    return list(decoded)


def _classify_version(row: Mapping[str, Any], context: _Context) -> _Entry:
    account_id = _normalise(row.get("account_id"))
    status = _normalise(row.get("status"))
    if status not in {"active", "superseded"}:
        return _blocked_entry(
            _VERSION,
            row,
            outcome="quarantined",
            reason="version_status_unsupported",
            details=(str(status),),
            subject_class="ambiguous",
        )
    snapshot = _snapshot_items(row.get("snapshot_json"))
    if snapshot is None:
        return _blocked_entry(
            _VERSION,
            row,
            outcome="quarantined",
            reason="snapshot_json_invalid",
            subject_class="ambiguous",
        )
    if not snapshot:
        return _blocked_entry(
            _VERSION,
            row,
            outcome="omitted",
            reason="snapshot_has_no_traits",
            subject_class="no-subject-evidence",
        )
    trait_ids: list[str] = []
    subjects: list[str] = []
    for item in snapshot:
        trait_id = _normalise(item.get("trait_id"))
        if trait_id is None:
            return _blocked_entry(
                _VERSION,
                row,
                outcome="quarantined",
                reason="snapshot_trait_id_missing",
                evidence_ids=trait_ids,
                subject_class="ambiguous",
            )
        trait = context.traits_by_id.get(trait_id)
        if trait is None:
            return _blocked_entry(
                _VERSION,
                row,
                outcome="quarantined",
                reason="snapshot_trait_missing",
                details=(trait_id,),
                evidence_ids=(*trait_ids, trait_id),
                subject_class="no-subject-evidence",
            )
        if _normalise(trait.get("account_id")) != account_id:
            return _blocked_entry(
                _VERSION,
                row,
                outcome="quarantined",
                reason="snapshot_trait_account_mismatch",
                details=(trait_id,),
                evidence_ids=(*trait_ids, trait_id),
                subject_class="foreign",
            )
        subject = context.trait_subjects.get(trait_id)
        if subject is None:
            return _entry_from_resolution(
                _VERSION,
                row,
                _trait_failure_resolution(trait_id, trait, context),
                evidence_ids=(*trait_ids, trait_id),
            )
        trait_ids.append(trait_id)
        subjects.append(subject)
    resolution = _resolve_subject(account_id, subjects, context)
    return _entry_from_resolution(
        _VERSION, row, resolution, evidence_ids=tuple(trait_ids)
    )


_CLASSIFIERS = {
    _TRAIT: _classify_trait,
    _EVIDENCE: _classify_evidence,
    _OBSERVATION: _classify_observation,
    _STYLE: _classify_style,
    _CONSENT: _classify_consent,
    _VERSION: _classify_version,
}


def _source_index(source: _SourceState) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {
        (table, _source_row_id(table, row)): row
        for table in _SOURCE_TABLES
        for row in source.rows[table]
    }


def _target_index(
    projection: Mapping[str, Iterable[Mapping[str, Any]]],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for table, rows in projection.items():
        for row in rows:
            key = (table, _source_row_id(table, row))
            if key in result:
                raise PersonaProjectionError(
                    "more than one projected row claims the same Persona source row: "
                    f"{table}:{key[1]}"
                )
            result[key] = row
    return result


def _covered_entry(
    table: str,
    row: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> _Entry:
    row_id = _source_row_id(table, row)
    source_digest = _source_content_digest(table, row)
    if str(receipt["source_row_digest"]) != source_digest:
        raise PersonaProjectionError(
            f"source content drifted under an applied receipt: {table}:{row_id}"
        )
    outcome = str(receipt["outcome"])
    migration_id = str(receipt["migration_id"])
    subject_id = _normalise(receipt["subject_id"])
    account_id = _normalise(row.get("account_id"))
    status = _normalise(row.get("status")) if table in _MUTABLE_COLUMNS else None
    subject_class = (
        "owner/self"
        if subject_id is not None and subject_id == account_id
        else "child/member" if subject_id is not None else "covered"
    )
    if outcome not in {"mapped", "refreshed"}:
        return _Entry(
            table=table,
            row_id=row_id,
            account_id=account_id,
            subject_id=None,
            subject_class="covered",
            outcome="already_projected",
            reason=f"covered_by_{outcome}",
            details=(f"migration:{migration_id}", str(receipt["reason"])),
            source_digest=source_digest,
            target_digest=None,
            source_row=_source_row(table, row),
            target_row=None,
            evidence_ids=(),
            after_status=status,
        )
    if subject_id is None:
        raise PersonaProjectionError(
            f"receipt {migration_id} projects {table}:{row_id} without a subject"
        )
    if table in _MUTABLE_COLUMNS:
        projected_status = _normalise(receipt["after_status"])
        if projected_status is None:
            raise PersonaProjectionError(
                f"receipt {migration_id} projects {table}:{row_id} without a status"
            )
        if status != projected_status:
            resolution = _Resolution(
                subject_id,
                subject_class,
                "status_refresh",
                (f"migration:{migration_id}",),
            )
            return _mapped_entry(
                table,
                row,
                resolution,
                outcome="refreshed",
                before_status=projected_status,
            )
    return _Entry(
        table=table,
        row_id=row_id,
        account_id=account_id,
        subject_id=subject_id,
        subject_class=subject_class,
        outcome="already_projected",
        reason="subject_and_status_current",
        details=(f"migration:{migration_id}",),
        source_digest=source_digest,
        target_digest=None,
        source_row=_source_row(table, row),
        target_row=None,
        evidence_ids=(),
        after_status=status,
    )


def _verify_applied_receipts(
    connection: sqlite3.Connection,
    context: _Context,
) -> None:
    """Fence every source row and target row already covered by a receipt."""

    projection = _current_projection(connection)
    targets = _target_index(projection)
    sources = _source_index(context.source)
    for key, projected_row in targets.items():
        receipt = context.coverage.applied.get(key)
        if receipt is None or str(receipt["outcome"]) not in {"mapped", "refreshed"}:
            raise PersonaProjectionError(
                "projected row exists without a current applied receipt: "
                f"{key[0]}:{key[1]}"
            )
        if _normalise(receipt["subject_id"]) != _normalise(
            projected_row.get("subject_id")
        ):
            raise PersonaProjectionError(
                f"projected subject drifted: {key[0]}:{key[1]}"
            )
        if str(receipt["outcome"]) == "mapped" and str(
            projected_row.get("projection_migration_id")
        ) != str(receipt["migration_id"]):
            raise PersonaProjectionError(
                "projected row owner does not match its mapping receipt: "
                f"{key[0]}:{key[1]}"
            )
    for key, receipt in sorted(context.coverage.applied.items()):
        table, row_id = key
        source = sources.get(key)
        if source is None:
            raise PersonaProjectionError(
                f"source row is missing for an applied receipt: {table}:{row_id}"
            )
        if _source_content_digest(table, source) != str(receipt["source_row_digest"]):
            raise PersonaProjectionError(
                f"source content drifted under an applied receipt: {table}:{row_id}"
            )
        outcome = str(receipt["outcome"])
        target = targets.get(key)
        if outcome not in {"mapped", "refreshed"}:
            if target is not None:
                raise PersonaProjectionError(
                    "a blocked receipt unexpectedly has a projected row: "
                    f"{table}:{row_id}"
                )
            continue
        if target is None:
            raise PersonaProjectionError(
                f"projected row is missing for an applied receipt: {table}:{row_id}"
            )
        expected = _normalise(receipt["target_row_digest"])
        if expected is None:
            raise PersonaProjectionError(
                f"receipt has no target row digest: {table}:{row_id}"
            )
        if _target_content_digest(table, target) != expected:
            raise PersonaProjectionError(f"projected row drifted: {table}:{row_id}")
        if table in _MUTABLE_COLUMNS:
            expected_status = _normalise(receipt["after_status"])
            if expected_status is None or _normalise(target.get("status")) != expected_status:
                raise PersonaProjectionError(
                    f"projected status drifted: {table}:{row_id}"
                )


def _downgrade(
    entry: _Entry,
    reason: str,
    *,
    details: Iterable[str] = (),
    subject_class: str = "ambiguous",
) -> _Entry:
    return _Entry(
        table=entry.table,
        row_id=entry.row_id,
        account_id=entry.account_id,
        subject_id=None,
        subject_class=subject_class,
        outcome="quarantined",
        reason=reason,
        details=tuple(str(item) for item in details) or entry.details,
        source_digest=entry.source_digest,
        target_digest=None,
        source_row=entry.source_row,
        target_row=None,
        evidence_ids=entry.evidence_ids,
        before_status=entry.before_status,
        after_status=entry.after_status,
    )


def _entry_dict(entry: _Entry) -> dict[str, Any]:
    return {
        "table": entry.table,
        "source_row_id": entry.row_id,
        "account_id": entry.account_id,
        "subject_id": entry.subject_id,
        "subject_class": entry.subject_class,
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


def _statistics(entries: Sequence[_Entry]) -> dict[str, Any]:
    outcomes = ("mapped", "refreshed", "omitted", "quarantined", "already_projected")
    by_table = {
        table: {
            outcome: sum(
                1
                for entry in entries
                if entry.table == table and entry.outcome == outcome
            )
            for outcome in outcomes
        }
        for table in _SOURCE_TABLES
    }
    return {
        "row_count": len(entries),
        "by_table": by_table,
        "subjects": sorted(
            {
                str(entry.subject_id)
                for entry in entries
                if entry.subject_id is not None
                and entry.outcome in {"mapped", "refreshed", "already_projected"}
            }
        ),
        "writes": sum(
            1 for entry in entries if entry.outcome in {"mapped", "refreshed"}
        ),
    }


def _replace_entry(entries: list[_Entry], index: int, replacement: _Entry) -> None:
    entries[index] = replacement


def _positive(entry: _Entry) -> bool:
    return entry.subject_id is not None and entry.outcome in {
        "mapped",
        "refreshed",
        "already_projected",
    }


def _apply_target_conflicts(
    entries: list[_Entry],
    projection: Mapping[str, Iterable[Mapping[str, Any]]],
) -> None:
    current_keys: dict[tuple[str, tuple[str, ...]], str] = {}
    current_version_numbers: dict[tuple[str, str], str] = {}
    for table, rows in projection.items():
        for row in rows:
            current_keys[(table, _target_key(table, row))] = _source_row_id(table, row)
            if table == _VERSION:
                current_version_numbers[
                    (str(row.get("subject_id") or ""), str(row.get("version_number") or ""))
                ] = str(row.get("version_id") or "")

    planned_keys: dict[tuple[str, tuple[str, ...]], list[int]] = {}
    planned_version_numbers: dict[tuple[str, str], list[int]] = {}
    for index, entry in enumerate(entries):
        if entry.outcome != "mapped" or entry.target_row is None:
            continue
        target_key = _target_key(entry.table, entry.target_row)
        planned_keys.setdefault((entry.table, target_key), []).append(index)
        current_owner = current_keys.get((entry.table, target_key))
        if current_owner is not None and current_owner != entry.row_id:
            _replace_entry(
                entries,
                index,
                _downgrade(
                    entry,
                    "target_key_conflict",
                    details=(current_owner,),
                ),
            )
            continue
        if entry.table == _VERSION:
            number_key = (
                str(entry.subject_id or ""),
                str(entry.target_row.get("version_number") or ""),
            )
            planned_version_numbers.setdefault(number_key, []).append(index)
            current_version = current_version_numbers.get(number_key)
            if current_version is not None and current_version != entry.row_id:
                _replace_entry(
                    entries,
                    index,
                    _downgrade(
                        entry,
                        "version_number_conflict",
                        details=(current_version,),
                    ),
                )

    for indices in planned_keys.values():
        active = [index for index in indices if entries[index].outcome == "mapped"]
        if len(active) <= 1:
            continue
        row_ids = tuple(sorted(entries[index].row_id for index in active))
        for index in active:
            _replace_entry(
                entries,
                index,
                _downgrade(entries[index], "target_key_conflict", details=row_ids),
            )
    for indices in planned_version_numbers.values():
        active = [index for index in indices if entries[index].outcome == "mapped"]
        if len(active) <= 1:
            continue
        row_ids = tuple(sorted(entries[index].row_id for index in active))
        for index in active:
            _replace_entry(
                entries,
                index,
                _downgrade(
                    entries[index], "version_number_conflict", details=row_ids
                ),
            )


def _version_cycle_nodes(entries: Sequence[_Entry]) -> set[str]:
    parents = {
        entry.row_id: _normalise(entry.source_row.get("parent_version_id"))
        for entry in entries
        if entry.table == _VERSION and _positive(entry)
    }
    cycles: set[str] = set()
    completed: set[str] = set()
    for start in parents:
        if start in completed:
            continue
        path: list[str] = []
        positions: dict[str, int] = {}
        current: str | None = start
        while current is not None and current in parents and current not in completed:
            if current in positions:
                cycles.update(path[positions[current] :])
                break
            positions[current] = len(path)
            path.append(current)
            current = parents[current]
        completed.update(path)
    return cycles


def _apply_reference_fences(entries: list[_Entry], context: _Context) -> None:
    def indexes() -> tuple[dict[str, int], dict[str, int]]:
        traits = {
            entry.row_id: index
            for index, entry in enumerate(entries)
            if entry.table == _TRAIT
        }
        versions = {
            entry.row_id: index
            for index, entry in enumerate(entries)
            if entry.table == _VERSION
        }
        return traits, versions

    trait_indexes, version_indexes = indexes()
    for index, entry in enumerate(tuple(entries)):
        if entry.outcome != "mapped":
            continue
        if entry.table == _EVIDENCE:
            trait_id = _normalise(entry.source_row.get("trait_id"))
            trait_index = trait_indexes.get(trait_id or "")
            trait = entries[trait_index] if trait_index is not None else None
            if trait is None or not _positive(trait) or trait.subject_id != entry.subject_id:
                _replace_entry(
                    entries,
                    index,
                    _downgrade(
                        entry,
                        "trait_not_projected",
                        details=((trait_id,) if trait_id is not None else ()),
                    ),
                )
        elif entry.table == _VERSION:
            refused = tuple(
                trait_id
                for trait_id in entry.evidence_ids
                if trait_id not in trait_indexes
                or not _positive(entries[trait_indexes[trait_id]])
                or entries[trait_indexes[trait_id]].subject_id != entry.subject_id
            )
            if refused:
                _replace_entry(
                    entries,
                    index,
                    _downgrade(
                        entry,
                        "snapshot_trait_not_projected",
                        details=refused,
                    ),
                )

    cycle_nodes = _version_cycle_nodes(entries)
    for version_id in cycle_nodes:
        index = version_indexes[version_id]
        if entries[index].outcome == "mapped":
            _replace_entry(
                entries,
                index,
                _downgrade(entries[index], "parent_version_cycle"),
            )

    changed = True
    while changed:
        changed = False
        for _version_id, index in version_indexes.items():
            entry = entries[index]
            if entry.outcome != "mapped":
                continue
            parent_id = _normalise(entry.source_row.get("parent_version_id"))
            if parent_id is None:
                continue
            parent_row = next(
                (
                    row
                    for row in context.source.rows[_VERSION]
                    if _source_row_id(_VERSION, row) == parent_id
                ),
                None,
            )
            if parent_row is None:
                replacement = _downgrade(
                    entry, "parent_version_missing", details=(parent_id,)
                )
            elif _normalise(parent_row.get("account_id")) != entry.account_id:
                replacement = _downgrade(
                    entry, "parent_version_account_mismatch", details=(parent_id,)
                )
            else:
                parent_index = version_indexes.get(parent_id)
                parent = entries[parent_index] if parent_index is not None else None
                if parent is None or not _positive(parent):
                    replacement = _downgrade(
                        entry, "parent_version_not_projected", details=(parent_id,)
                    )
                elif parent.subject_id != entry.subject_id:
                    replacement = _downgrade(
                        entry, "parent_version_subject_mismatch", details=(parent_id,)
                    )
                else:
                    continue
            _replace_entry(entries, index, replacement)
            changed = True


def _run_plan(connection: sqlite3.Connection, context: _Context) -> _RunPlan:
    """Classify all six source tables and then enforce cross-row fences."""

    _verify_applied_receipts(connection, context)
    entries: list[_Entry] = []
    for table in _SOURCE_TABLES:
        for row in sorted(
            context.source.rows[table],
            key=lambda item: _source_row_id(table, item),
        ):
            row_id = _source_row_id(table, row)
            receipt = context.coverage.applied.get((table, row_id))
            if receipt is not None:
                entries.append(_covered_entry(table, row, receipt))
                continue
            rolled_back = context.coverage.rolled_back.get((table, row_id))
            if rolled_back is not None and str(
                rolled_back["source_row_digest"]
            ) != _source_content_digest(table, row):
                raise PersonaProjectionError(
                    "source content changed under a rolled-back receipt: "
                    f"{table}:{row_id}"
                )
            entries.append(_CLASSIFIERS[table](row, context))
    projection = _current_projection(connection)
    _apply_target_conflicts(entries, projection)
    _apply_reference_fences(entries, context)
    pending = tuple(entry for entry in entries if entry.outcome != "already_projected")
    return _RunPlan(
        entries=tuple(entries),
        pending=pending,
        statistics=_statistics(entries),
    )


def _planned_projection(
    entries: Iterable[_Entry],
    current: Mapping[str, Iterable[Mapping[str, Any]]],
) -> dict[str, tuple[dict[str, Any], ...]]:
    planned: dict[str, dict[str, dict[str, Any]]] = {
        table: {
            _target_row_id(table, row): dict(row)
            for row in rows
        }
        for table, rows in current.items()
    }
    for entry in entries:
        if entry.outcome not in {"mapped", "refreshed"} or entry.target_row is None:
            continue
        table_rows = planned[entry.table]
        if entry.outcome == "mapped":
            table_rows[_target_row_id(entry.table, entry.target_row)] = dict(
                entry.target_row
            )
            continue
        matches = [
            row
            for row in table_rows.values()
            if _source_row_id(entry.table, row) == entry.row_id
        ]
        if len(matches) != 1:
            raise PersonaProjectionError(
                f"projected row is missing for a refresh: {entry.table}:{entry.row_id}"
            )
        matches[0]["status"] = entry.after_status
    return {
        table: tuple(rows.values())
        for table, rows in planned.items()
    }


def _build_report(
    connection: sqlite3.Connection,
    context: _Context,
    *,
    session: _Session,
    planned_at: str,
) -> dict[str, Any]:
    run = _run_plan(connection, context)
    current = _current_projection(connection)
    target_before = _projection_digest(current)
    target_after = _projection_digest(_planned_projection(run.entries, current))
    rows = [_entry_dict(entry) for entry in run.entries]
    source_counts = {
        table: len(context.source.rows[table]) for table in _SOURCE_TABLES
    }
    source_counts[_EVENT] = len(context.source.events)
    core: dict[str, Any] = {
        "version": 1,
        "kind": _SCOPE,
        "rules_version": _RULES_VERSION,
        "source": {
            "digest_before": context.source.digest,
            "digest_after": context.source.digest,
            "tables": [*_SOURCE_TABLES, _EVENT],
            "row_counts": source_counts,
        },
        "identity": {
            "digest": context.identity.digest,
            "tables": list(context.identity.tables),
        },
        "control": {
            "digest": context.control_digest,
            "registered_account_count": len(context.registered_accounts),
        },
        "target": {
            "digest_before": target_before,
            "digest_after": target_after,
            "tables": list(_PROJECTED.values()),
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
        "source_digest_before": context.source.digest,
        "source_digest_after": context.source.digest,
        "target_digest_before": target_before,
        "target_digest_after": target_after,
        "identity_digest": context.identity.digest,
        "control_digest": context.control_digest,
        "planned_at": planned_at,
        "tables": [*_SOURCE_TABLES, _EVENT],
        "projection_tables": list(_PROJECTED.values()),
        "rows": rows,
        "row_count": len(run.entries),
        "pending_row_count": len(run.pending),
        "statistics": run.statistics,
        "manifest": manifest,
    }


def _migration_id(manifest_sha256: str, sequence: int) -> str:
    suffix = "" if sequence <= 0 else f"-r{sequence}"
    return f"persona-subject-v1-{manifest_sha256}{suffix}"


def _migration_sequence(connection: sqlite3.Connection, manifest_sha256: str) -> int:
    row = connection.execute(
        f"SELECT count(*) AS n FROM {_quote(_MIGRATIONS)} WHERE manifest_sha256 = ?",
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
            f"SELECT * FROM {_quote(_MIGRATIONS)} WHERE {_quote(column)} = ?",
            (value,),
        ).fetchall()
        if len(rows) > 1:
            raise PersonaProjectionError(
                f"selector identifies more than one Persona projection run: {value}; "
                "pass migration_id"
            )
        if rows:
            row = rows[0]
            matches[str(row["migration_id"])] = {
                str(name): row[name] for name in row.keys()
            }
    if not matches:
        raise PersonaProjectionError(
            "requested Persona projection migration was not found"
        )
    if len(matches) != 1:
        raise PersonaProjectionError("migration selectors identify different migrations")
    return next(iter(matches.values()))


def _migration_receipts(
    connection: sqlite3.Connection,
    migration_id: str,
) -> tuple[dict[str, Any], ...]:
    rows = connection.execute(
        f"SELECT * FROM {_quote(_RECEIPTS)} WHERE migration_id = ? "
        "ORDER BY table_name, source_row_id",
        (migration_id,),
    ).fetchall()
    return tuple(
        {str(column): row[column] for column in row.keys()} for row in rows
    )


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
            "memoria:persona:projection:"
            f"{migration_id}:{table_name}:{source_row_id}:{action}",
        )
    )
    connection.execute(
        f"INSERT INTO {_quote(_AUDIT)} "
        "(audit_id, migration_id, table_name, source_row_id, action, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
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
        if entry.outcome != "mapped" or entry.target_row is None:
            continue
        columns = _target_key_column(entry.table)
        where = " AND ".join(f"{_quote(column)} = ?" for column in columns)
        values = tuple(entry.target_row.get(column) for column in columns)
        row = connection.execute(
            f"SELECT 1 FROM {_quote(_target_table(entry.table))} WHERE {where}",
            values,
        ).fetchone()
        if row is not None:
            raise PersonaProjectionError(
                "projected row already exists without a matching receipt: "
                f"{entry.table}:{entry.row_id}"
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
            raise PersonaProjectionError(
                f"entry has no projected row: {entry.table}:{entry.row_id}"
            )
        target_table = _target_table(entry.table)
        if entry.outcome == "mapped":
            row_values = dict(entry.target_row)
            row_values["projection_migration_id"] = migration_id
            row_values["projected_at"] = timestamp
            columns = _TARGET_COLUMNS[entry.table]
            connection.execute(
                f"INSERT INTO {_quote(target_table)} "
                f"({', '.join(_quote(column) for column in columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(row_values[column] for column in columns),
            )
        else:
            if entry.table not in _MUTABLE_COLUMNS:
                raise PersonaProjectionError(
                    f"unsupported Persona refresh: {entry.table}:{entry.row_id}"
                )
            source_key = {
                _TRAIT: "trait_id",
                _VERSION: "version_id",
            }[entry.table]
            cursor = connection.execute(
                f"UPDATE {_quote(target_table)} SET status = ? "
                f"WHERE {_quote(source_key)} = ?",
                (entry.after_status, entry.row_id),
            )
            if cursor.rowcount != 1:
                raise PersonaProjectionError(
                    f"projected row is missing for a refresh: {entry.table}:{entry.row_id}"
                )
    connection.execute(
        f"INSERT INTO {_quote(_RECEIPTS)} "
        "(migration_id, table_name, source_row_id, outcome, subject_id, before_status, "
        "after_status, source_row_digest, target_row_digest, reason, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        f"INSERT INTO {_quote(_BACKUPS)} "
        "(migration_id, table_name, source_row_id, source_row_json, source_row_digest, "
        "target_row_json, target_row_digest, evidence_ids_json, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            f"INSERT INTO {_quote(_QUARANTINE)} "
            "(migration_id, table_name, source_row_id, account_id, subject_id, outcome, "
            "reason, details_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            "subject_class": entry.subject_class,
            "reason": entry.reason,
            "details": list(entry.details),
        },
        created_at=timestamp,
    )


def _target_row_for_source(
    connection: sqlite3.Connection,
    table: str,
    row_id: str,
) -> dict[str, Any] | None:
    target_table = _target_table(table)
    if not _table_exists(connection, "main", target_table):
        return None
    rows = tuple(
        {str(column): row[column] for column in row.keys()}
        for row in connection.execute(
            f"SELECT * FROM {_quote(target_table)}"
        ).fetchall()
    )
    matches = [
        row
        for row in rows
        if _source_row_id(table, row) == row_id
    ]
    if len(matches) > 1:
        raise PersonaProjectionError(
            f"more than one projected row matches {table}:{row_id}"
        )
    return matches[0] if matches else None


def _verify_rollback_fences(
    connection: sqlite3.Connection,
    stored: Mapping[str, Any],
    receipts: Iterable[Mapping[str, Any]],
) -> None:
    migration_id = str(stored["migration_id"])
    source = _load_source(connection)
    if source.digest != str(stored["source_digest_after"]):
        raise PersonaProjectionError(
            "Persona source changed after the selected projection run"
        )
    sources = _source_index(source)
    for receipt in receipts:
        table = str(receipt["table_name"])
        row_id = str(receipt["source_row_id"])
        source_row = sources.get((table, row_id))
        if source_row is None:
            raise PersonaProjectionError(f"source row is missing: {table}:{row_id}")
        if _source_content_digest(table, source_row) != str(
            receipt["source_row_digest"]
        ):
            raise PersonaProjectionError(f"source row drifted: {table}:{row_id}")
        outcome = str(receipt["outcome"])
        if outcome not in {"mapped", "refreshed"}:
            continue
        target = _target_row_for_source(connection, table, row_id)
        if target is None:
            raise PersonaProjectionError(
                f"projected row is missing for rollback: {table}:{row_id}"
            )
        expected = _normalise(receipt["target_row_digest"])
        if expected is None or _target_content_digest(table, target) != expected:
            raise PersonaProjectionError(
                f"projected row drifted before rollback: {table}:{row_id}"
            )
        if table in _MUTABLE_COLUMNS:
            if _normalise(target.get("status")) != _normalise(receipt["after_status"]):
                raise PersonaProjectionError(
                    f"projected status moved since projection: {table}:{row_id}"
                )
        if outcome == "mapped" and str(
            target.get("projection_migration_id")
        ) != migration_id:
            raise PersonaProjectionError(
                "a later projection run owns this row; roll that run back first: "
                f"{table}:{row_id}"
            )
        if outcome == "refreshed" and _normalise(receipt["before_status"]) is None:
            raise PersonaProjectionError(
                f"refresh receipt has no previous status: {table}:{row_id}"
            )


def _rollback_rows(
    connection: sqlite3.Connection,
    migration_id: str,
    receipts: Iterable[Mapping[str, Any]],
    timestamp: str,
) -> tuple[list[str], list[str]]:
    deleted: list[str] = []
    restored: list[str] = []
    for receipt in receipts:
        table = str(receipt["table_name"])
        row_id = str(receipt["source_row_id"])
        outcome = str(receipt["outcome"])
        if outcome == "mapped":
            target = _target_row_for_source(connection, table, row_id)
            if target is None:
                raise PersonaProjectionError(
                    f"projected row is missing during rollback: {table}:{row_id}"
                )
            columns = _target_key_column(table)
            where = " AND ".join(f"{_quote(column)} = ?" for column in columns)
            values = tuple(target.get(column) for column in columns)
            cursor = connection.execute(
                f"DELETE FROM {_quote(_target_table(table))} WHERE {where} "
                "AND projection_migration_id = ?",
                (*values, migration_id),
            )
            if cursor.rowcount != 1:
                raise PersonaProjectionError(
                    f"projected row ownership changed during rollback: {table}:{row_id}"
                )
            deleted.append(f"{table}:{row_id}")
        elif outcome == "refreshed":
            key_column = {_TRAIT: "trait_id", _VERSION: "version_id"}.get(table)
            if key_column is None:
                raise PersonaProjectionError(
                    f"unsupported refresh receipt: {table}:{row_id}"
                )
            cursor = connection.execute(
                f"UPDATE {_quote(_target_table(table))} SET status = ? "
                f"WHERE {_quote(key_column)} = ?",
                (_normalise(receipt["before_status"]), row_id),
            )
            if cursor.rowcount != 1:
                raise PersonaProjectionError(
                    f"projected row is missing during rollback: {table}:{row_id}"
                )
            restored.append(f"{table}:{row_id}")
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
    if _load_source(connection).digest != str(stored["source_digest_after"]):
        raise PersonaProjectionError(
            "Persona source changed after the selected projection run"
        )
    migration_id = str(stored["migration_id"])
    deleted: list[str] = []
    restored: list[str] = []
    for receipt in receipts:
        table = str(receipt["table_name"])
        row_id = str(receipt["source_row_id"])
        outcome = str(receipt["outcome"])
        target = _target_row_for_source(connection, table, row_id)
        if outcome == "mapped":
            if target is not None and str(
                target.get("projection_migration_id")
            ) == migration_id:
                raise PersonaProjectionError(
                    f"projected row survived its rollback: {table}:{row_id}"
                )
            deleted.append(f"{table}:{row_id}")
        elif outcome == "refreshed":
            if target is None:
                raise PersonaProjectionError(
                    f"projected row is missing after rollback: {table}:{row_id}"
                )
            if _target_content_digest(table, target) != str(
                receipt["target_row_digest"]
            ):
                raise PersonaProjectionError(
                    f"projected row drifted after rollback: {table}:{row_id}"
                )
            if _normalise(target.get("status")) != _normalise(
                receipt["before_status"]
            ):
                raise PersonaProjectionError(
                    f"projected status moved after rollback: {table}:{row_id}"
                )
            restored.append(f"{table}:{row_id}")
    return {
        "scope": _SCOPE,
        "migration_id": migration_id,
        "manifest_sha256": str(stored["manifest_sha256"]),
        "status": "rolled_back",
        "idempotent": True,
        "deleted_rows": deleted,
        "restored_rows": restored,
        "target_digest_after": _projection_digest(_current_projection(connection)),
    }


def _plan_session(session: _Session, planned_at: str) -> dict[str, Any]:
    connection = _connect_read_only(session.db_path)
    try:
        context = _load_context(connection, session)
        return _build_report(
            connection,
            context,
            session=session,
            planned_at=planned_at,
        )
    finally:
        connection.close()


def plan(
    db_path: str | Path,
    *,
    identity_path: str | Path | None = None,
    control_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a deterministic Persona projection manifest without writing."""

    session = _sources(db_path, identity_path, control_path)
    return _plan_session(session, _now(now).isoformat())


def dry_run(
    db_path: str | Path,
    *,
    identity_path: str | Path | None = None,
    control_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Explicit operator-facing alias of :func:`plan`."""

    report = plan(
        db_path,
        identity_path=identity_path,
        control_path=control_path,
        now=now,
    )
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
    """Project all currently provable Persona rows in one SQLite transaction."""

    session = _sources(db_path, identity_path, control_path)
    planned_at = _now(now).isoformat()
    planned = _plan_session(session, planned_at)
    manifest_sha256 = str(planned["manifest_sha256"])
    if (
        expected_manifest_sha256 is not None
        and expected_manifest_sha256 != manifest_sha256
    ):
        raise PersonaProjectionError(
            "expected projection manifest SHA-256 does not match the current plan"
        )
    connection = _connect_write(session.db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            context = _load_context(connection, session)
            if context.source.digest != str(planned["source_digest_before"]):
                raise PersonaProjectionError(
                    "Persona source changed after planning and before apply"
                )
            if context.identity.digest != str(planned["identity_digest"]):
                raise PersonaProjectionError(
                    "identity evidence changed after planning and before apply"
                )
            if context.control_digest != str(planned["control_digest"]):
                raise PersonaProjectionError(
                    "Control registration evidence changed after planning and before apply"
                )
            current = _current_projection(connection)
            if _projection_digest(current) != str(planned["target_digest_before"]):
                raise PersonaProjectionError(
                    "projection target changed after planning and before apply"
                )
            run = _run_plan(connection, context)
            if [_entry_dict(entry) for entry in run.entries] != planned["rows"]:
                raise PersonaProjectionError(
                    "current Persona rows do not match the approved projection plan"
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
                manifest_sha256,
                _migration_sequence(connection, manifest_sha256),
            )
            timestamp = _now(now).isoformat()
            connection.execute(
                f"INSERT INTO {_quote(_MIGRATIONS)} "
                "(migration_id, manifest_sha256, scope, source_digest_before, "
                "source_digest_after, target_digest_before, target_digest_after, "
                "identity_digest, control_digest, status, manifest_json, statistics_json, "
                "created_at, applied_at, rolled_back_at, rollback_target_digest, "
                "source_path, identity_path, control_path, planned_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?, ?, ?, NULL, NULL, "
                "?, ?, ?, ?)",
                (
                    migration_id,
                    manifest_sha256,
                    _SCOPE,
                    str(planned["source_digest_before"]),
                    str(planned["source_digest_after"]),
                    str(planned["target_digest_before"]),
                    str(planned["target_digest_after"]),
                    str(planned["identity_digest"]),
                    str(planned["control_digest"]),
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
                    connection,
                    entry,
                    migration_id=migration_id,
                    timestamp=timestamp,
                )
            source_after = _load_source(connection)
            if source_after.digest != str(planned["source_digest_after"]):
                raise PersonaProjectionError(
                    "Persona source changed while applying the projection"
                )
            target_after = _projection_digest(_current_projection(connection))
            if target_after != str(planned["target_digest_after"]):
                raise PersonaProjectionError(
                    "projection digest after apply does not match the approved manifest"
                )
            _insert_projection_audit(
                connection,
                migration_id=migration_id,
                table_name=_MIGRATIONS,
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
    """Remove only one migration's rows and restore its status refreshes."""

    if migration_id is None and manifest_sha256 is None:
        raise PersonaProjectionError(
            "rollback requires migration_id or manifest_sha256"
        )
    session = _sources(db_path, identity_path, control_path)
    read_connection = _connect_read_only(session.db_path)
    try:
        if not _table_exists(read_connection, "main", _MIGRATIONS):
            raise PersonaProjectionError("no Persona projection migration found")
        stored = _find_migration(
            read_connection,
            migration_id=migration_id,
            manifest_sha256=manifest_sha256,
        )
        stored_id = str(stored["migration_id"])
        receipts = _migration_receipts(read_connection, stored_id)
        status = str(stored["status"])
        if status == "rolled_back":
            return _rolled_back_report(read_connection, stored, receipts)
        if status != "applied":
            raise PersonaProjectionError(
                f"unsupported Persona projection status: {status}"
            )
        _verify_rollback_fences(read_connection, stored, receipts)
    finally:
        read_connection.close()

    connection = _connect_write(session.db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = _find_migration(connection, migration_id=stored_id)
            if str(current["status"]) != "applied":
                raise PersonaProjectionError(
                    "Persona projection status changed before rollback"
                )
            current_receipts = _migration_receipts(connection, stored_id)
            _verify_rollback_fences(connection, current, current_receipts)
            timestamp = _now(now).isoformat()
            cursor = connection.execute(
                f"UPDATE {_quote(_MIGRATIONS)} SET status = 'rolling_back' "
                "WHERE migration_id = ? AND status = 'applied'",
                (stored_id,),
            )
            if cursor.rowcount != 1:
                raise PersonaProjectionError(
                    "Persona projection status changed before rollback"
                )
            deleted, restored = _rollback_rows(
                connection,
                stored_id,
                current_receipts,
                timestamp,
            )
            target_after = _projection_digest(_current_projection(connection))
            _insert_projection_audit(
                connection,
                migration_id=stored_id,
                table_name=_MIGRATIONS,
                source_row_id=stored_id,
                action="projection.rolled_back",
                payload={
                    "deleted_rows": len(deleted),
                    "restored_rows": len(restored),
                },
                created_at=timestamp,
            )
            connection.execute(
                f"UPDATE {_quote(_MIGRATIONS)} SET status = 'rolled_back', "
                "rolled_back_at = ?, rollback_target_digest = ? WHERE migration_id = ?",
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
