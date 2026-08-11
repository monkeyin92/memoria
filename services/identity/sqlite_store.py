"""SQLite development adapter for the identity domain.

Local development fixture mirroring the PostgreSQL schema (section 11.7:
SQLite is not the production authority).  Version assignment and status
transitions run inside ``BEGIN IMMEDIATE`` transactions so per-device
binding versions stay monotonic even under concurrent writers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar, cast

from services.identity.domain import (
    AgeBand,
    AgeEvidenceStatus,
    BindingReason,
    BindingRole,
    BindingStatus,
    BindingVersionConflictError,
    DeviceBinding,
    DeviceBindingRole,
    DeviceDeclaredMode,
    IdempotencyRecord,
    IdentityConflictError,
    IdentityNotFoundError,
    PersonStatus,
    PersonSubject,
    Relationship,
    RelationshipStatus,
    RelationType,
    SubjectCategory,
    TransferIntent,
    permission_set,
    transfer_from_dict,
)
from services.identity.repository import AuditEvent, OutboxEvent

_IdentityEvent = TypeVar("_IdentityEvent", AuditEvent, OutboxEvent)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_persons (
    person_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 128),
    subject_category TEXT NOT NULL DEFAULT 'unknown'
        CHECK (subject_category IN ('unknown', 'minor', 'adult')),
    age_band TEXT NOT NULL DEFAULT 'unknown'
        CHECK (age_band IN ('unknown', 'under_14', '14_17', 'adult')),
    age_evidence_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (age_evidence_status IN ('unverified', 'verified', 'disputed')),
    locale TEXT NOT NULL DEFAULT 'zh-CN' CHECK (length(locale) BETWEEN 2 AND 32),
    timezone TEXT NOT NULL CHECK (length(timezone) BETWEEN 1 AND 64),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (subject_category = 'adult' AND age_band = 'adult'
            AND age_evidence_status = 'verified')
        OR (subject_category = 'minor' AND age_band IN ('under_14', '14_17'))
        OR (subject_category = 'unknown'
            AND age_band IN ('unknown', 'adult')
            AND NOT (age_band = 'adult' AND age_evidence_status = 'verified'))
    )
);

CREATE TABLE IF NOT EXISTS identity_relationships (
    relationship_id TEXT PRIMARY KEY,
    source_person_id TEXT NOT NULL REFERENCES identity_persons(person_id),
    target_person_id TEXT NOT NULL REFERENCES identity_persons(person_id),
    relation_type TEXT NOT NULL CHECK (relation_type IN (
        'self', 'parent_of', 'child_of', 'guardian_of', 'ward_of',
        'spouse_of', 'sibling_of', 'caregiver_of', 'emergency_contact_for',
        'delegate_for', 'beneficiary_of', 'co_subject_of', 'family_member_of'
    )),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'active', 'suspended', 'revoked', 'expired', 'disputed'
    )),
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    established_evidence_id TEXT NOT NULL
        CHECK (length(established_evidence_id) BETWEEN 1 AND 128),
    confirmed_by_source_at TEXT,
    confirmed_by_target_at TEXT,
    dispute_resolution_acked_by_source_at TEXT,
    dispute_resolution_acked_by_target_at TEXT,
    requires_confirmation INTEGER NOT NULL CHECK (requires_confirmation IN (0, 1)),
    can_delegate INTEGER NOT NULL CHECK (can_delegate IN (0, 1)),
    delegated_from_relationship_id TEXT
        REFERENCES identity_relationships(relationship_id),
    delegation_depth INTEGER NOT NULL DEFAULT 0
        CHECK (delegation_depth BETWEEN 0 AND 3),
    permissions_json TEXT NOT NULL CHECK (json_valid(permissions_json)),
    dispute_reason TEXT,
    auto_suspended INTEGER NOT NULL DEFAULT 0 CHECK (auto_suspended IN (0, 1)),
    revoked_at TEXT,
    revocation_evidence_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (relation_type = 'self' AND source_person_id = target_person_id)
        OR (relation_type <> 'self' AND source_person_id <> target_person_id)
    ),
    CHECK (valid_until IS NULL OR valid_until > valid_from),
    CHECK (
        (status = 'revoked' AND revoked_at IS NOT NULL
            AND revocation_evidence_id IS NOT NULL)
        OR (status <> 'revoked' AND revoked_at IS NULL
            AND revocation_evidence_id IS NULL)
    ),
    CHECK (
        (delegated_from_relationship_id IS NULL AND delegation_depth = 0)
        OR (delegated_from_relationship_id IS NOT NULL
            AND delegation_depth BETWEEN 1 AND 3)
    )
);

CREATE INDEX IF NOT EXISTS idx_identity_relationships_source
ON identity_relationships(source_person_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_identity_relationships_target
ON identity_relationships(target_person_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS identity_device_bindings (
    binding_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL CHECK (length(device_id) BETWEEN 1 AND 128),
    declared_mode TEXT NOT NULL CHECK (declared_mode IN (
        'parent_for_child', 'self_use', 'child_for_parent', 'family_shared'
    )),
    family_space_id TEXT,
    account_owner_person_id TEXT NOT NULL
        REFERENCES identity_persons(person_id),
    binding_version INTEGER NOT NULL CHECK (binding_version > 0),
    status TEXT NOT NULL CHECK (status IN (
        'active', 'superseded', 'revoked', 'expired'
    )),
    reason TEXT NOT NULL CHECK (reason IN (
        'create', 'supersede', 'transfer', 'unbind', 'expire'
    )),
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    supersedes_binding_id TEXT
        REFERENCES identity_device_bindings(binding_id),
    service_profile_version TEXT NOT NULL
        CHECK (length(service_profile_version) BETWEEN 1 AND 64),
    policy_bundle_version TEXT NOT NULL
        CHECK (length(policy_bundle_version) BETWEEN 1 AND 64),
    consent_snapshot_id TEXT,
    persona_assignment_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (device_id, binding_version),
    CHECK (valid_until IS NULL OR valid_until > valid_from),
    CHECK (supersedes_binding_id IS NULL OR supersedes_binding_id <> binding_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_bindings_single_active
ON identity_device_bindings(device_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_identity_bindings_device
ON identity_device_bindings(device_id, binding_version DESC);

CREATE TABLE IF NOT EXISTS identity_device_binding_roles (
    binding_id TEXT NOT NULL
        REFERENCES identity_device_bindings(binding_id) ON DELETE RESTRICT,
    person_id TEXT NOT NULL REFERENCES identity_persons(person_id),
    role TEXT NOT NULL CHECK (role IN (
        'account_owner', 'device_admin', 'primary_subject', 'guardian',
        'delegate', 'emergency_contact', 'member'
    )),
    status TEXT NOT NULL CHECK (status IN (
        'active', 'superseded', 'revoked', 'expired'
    )),
    permissions_json TEXT NOT NULL CHECK (json_valid(permissions_json)),
    granted_at TEXT NOT NULL,
    ended_at TEXT,
    PRIMARY KEY (binding_id, person_id, role),
    CHECK (
        (status = 'active' AND ended_at IS NULL)
        OR (status <> 'active' AND ended_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_identity_binding_roles_person
ON identity_device_binding_roles(person_id, role, status);

CREATE TABLE IF NOT EXISTS identity_audit_events (
    event_id TEXT PRIMARY KEY,
    action TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 64),
    actor_person_id TEXT,
    subject_person_id TEXT,
    person_id TEXT,
    device_id TEXT,
    binding_id TEXT,
    relationship_id TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_identity_audit_device
ON identity_audit_events(device_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_identity_audit_person
ON identity_audit_events(person_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_identity_audit_binding
ON identity_audit_events(binding_id, created_at DESC);

CREATE TABLE IF NOT EXISTS identity_outbox (
    outbox_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 64),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'processing', 'delivered', 'failed', 'dead_lettered'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    locked_until TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_identity_outbox_pending
ON identity_outbox(status, created_at);

CREATE TABLE IF NOT EXISTS identity_transfer_intents (
    transfer_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL CHECK (length(device_id) BETWEEN 1 AND 128),
    from_account_owner_person_id TEXT NOT NULL
        REFERENCES identity_persons(person_id),
    to_account_owner_person_id TEXT NOT NULL
        REFERENCES identity_persons(person_id),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'accepted', 'cancelled', 'expired', 'conflicted'
    )),
    step_up_evidence_id TEXT NOT NULL
        CHECK (length(step_up_evidence_id) BETWEEN 1 AND 128),
    policy_receipt_id TEXT NOT NULL
        CHECK (length(policy_receipt_id) BETWEEN 1 AND 128),
    idempotency_key TEXT NOT NULL DEFAULT ''
        CHECK (length(idempotency_key) BETWEEN 0 AND 64),
    evidence_hash TEXT NOT NULL DEFAULT ''
        CHECK (length(evidence_hash) IN (0, 64)),
    supersedes_binding_id TEXT REFERENCES identity_device_bindings(binding_id),
    resulting_binding_id TEXT REFERENCES identity_device_bindings(binding_id),
    created_by_person_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    valid_until TEXT,
    accepted_at TEXT,
    cancelled_at TEXT,
    cancelled_by_person_id TEXT,
    cancel_reason TEXT,
    CHECK (valid_until IS NULL OR valid_until > created_at),
    CHECK (
        (status = 'accepted' AND accepted_at IS NOT NULL
            AND resulting_binding_id IS NOT NULL)
        OR (status <> 'accepted' AND accepted_at IS NULL
            AND resulting_binding_id IS NULL)
    ),
    CHECK (
        (status = 'cancelled' AND cancelled_at IS NOT NULL
            AND cancelled_by_person_id IS NOT NULL)
        OR (status <> 'cancelled' AND cancelled_at IS NULL
            AND cancelled_by_person_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_transfer_pending
ON identity_transfer_intents(device_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_identity_transfer_device
ON identity_transfer_intents(device_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS identity_idempotency_records (
    scope_key TEXT NOT NULL CHECK (length(scope_key) BETWEEN 1 AND 256),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 64),
    operation TEXT NOT NULL CHECK (operation IN (
        'transfer.create', 'transfer.accept', 'transfer.cancel'
    )),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    result_payload_json TEXT NOT NULL CHECK (json_valid(result_payload_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope_key, idempotency_key)
);


CREATE TRIGGER IF NOT EXISTS identity_person_core_immutable
BEFORE UPDATE OF person_id, created_at ON identity_persons
BEGIN
    SELECT RAISE(ABORT, 'identity person identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS identity_relationship_core_immutable
BEFORE UPDATE OF relationship_id, source_person_id, target_person_id,
                 relation_type, established_evidence_id, permissions_json,
                 created_at ON identity_relationships
BEGIN
    SELECT RAISE(ABORT, 'identity relationship identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS identity_binding_core_immutable
BEFORE UPDATE OF binding_id, device_id, declared_mode, family_space_id,
                 account_owner_person_id, binding_version,
                 supersedes_binding_id, service_profile_version,
                 policy_bundle_version, consent_snapshot_id,
                 persona_assignment_id, created_at, valid_from, reason
                 ON identity_device_bindings
BEGIN
    SELECT RAISE(ABORT, 'identity binding version is immutable');
END;

CREATE TRIGGER IF NOT EXISTS identity_role_core_immutable
BEFORE UPDATE OF person_id, role, permissions_json, granted_at
                 ON identity_device_binding_roles
BEGIN
    SELECT RAISE(ABORT, 'identity role grant is immutable');
END;

CREATE TRIGGER IF NOT EXISTS identity_person_delete_guard
BEFORE DELETE ON identity_persons
BEGIN
    SELECT RAISE(ABORT, 'identity records require the account deletion pipeline');
END;

CREATE TRIGGER IF NOT EXISTS identity_relationship_delete_guard
BEFORE DELETE ON identity_relationships
BEGIN
    SELECT RAISE(ABORT, 'identity records require the account deletion pipeline');
END;

CREATE TRIGGER IF NOT EXISTS identity_binding_delete_guard
BEFORE DELETE ON identity_device_bindings
BEGIN
    SELECT RAISE(ABORT, 'identity records require the account deletion pipeline');
END;

CREATE TRIGGER IF NOT EXISTS identity_role_delete_guard
BEFORE DELETE ON identity_device_binding_roles
BEGIN
    SELECT RAISE(ABORT, 'identity records require the account deletion pipeline');
END;
"""


def _ts(value: datetime, *, field: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _from_iso(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


def _with_binding_version(event: _IdentityEvent, version: int) -> _IdentityEvent:  # noqa: UP047
    """Rewrite the advisory ``binding_version`` to the store-assigned value."""
    payload = dict(event.payload)
    if "binding_version" in payload:
        payload["binding_version"] = version
    return replace(event, payload=payload)


def _with_transition_state(  # noqa: UP047

    event: _IdentityEvent, status: str, valid_until: datetime | None
) -> _IdentityEvent:
    """Rewrite the advisory status/valid_until to the transition values."""
    payload = dict(event.payload)
    payload["status"] = status
    payload["valid_until"] = valid_until.isoformat() if valid_until else None
    return replace(event, payload=payload)


def _migrate_relationship_columns(connection: sqlite3.Connection) -> None:
    """Add relationship columns introduced after the first schema release."""
    present = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(identity_relationships)")
    }
    additions = {
        "dispute_resolution_acked_by_source_at": "TEXT",
        "dispute_resolution_acked_by_target_at": "TEXT",
        "auto_suspended": (
            "INTEGER NOT NULL DEFAULT 0 CHECK (auto_suspended IN (0, 1))"
        ),
    }
    for name, ddl in additions.items():
        if name not in present:
            connection.execute(
                f"ALTER TABLE identity_relationships ADD COLUMN {name} {ddl}"
            )
    transfer_present = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(identity_transfer_intents)")
    }
    transfer_additions = {
        "idempotency_key": (
            "TEXT NOT NULL DEFAULT '' CHECK (length(idempotency_key) BETWEEN 0 AND 64)"
        ),
        "evidence_hash": (
            "TEXT NOT NULL DEFAULT '' CHECK (length(evidence_hash) IN (0, 64))"
        ),
    }
    for name, ddl in transfer_additions.items():
        if name not in transfer_present:
            connection.execute(
                f"ALTER TABLE identity_transfer_intents ADD COLUMN {name} {ddl}"
            )


class SqliteIdentityStore:
    """SQLite implementation of the ``IdentityStore`` protocol."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser().resolve()
        self._initialized = False
        self._initialize_lock = threading.Lock()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
                _migrate_relationship_columns(connection)
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def close(self) -> None:
        return None

    def _ready(self) -> None:
        self.initialize()

    async def save_person(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO identity_persons (
                    person_id, display_name, subject_category, age_band,
                    age_evidence_status, locale, timezone, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(person_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    subject_category = excluded.subject_category,
                    age_band = excluded.age_band,
                    age_evidence_status = excluded.age_evidence_status,
                    locale = excluded.locale,
                    timezone = excluded.timezone,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    person.person_id,
                    person.display_name,
                    person.subject_category,
                    person.age_band,
                    person.age_evidence_status,
                    person.locale,
                    person.timezone,
                    person.status,
                    _ts(person.created_at, field="created_at"),
                    _ts(person.updated_at, field="updated_at"),
                ),
            )
            if audit_event is not None:
                _insert_audit(connection, audit_event)
            if outbox_event is not None:
                _insert_outbox(connection, outbox_event)

    async def register_person(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        evidence_id: str | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> PersonSubject:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM identity_persons WHERE person_id = ?",
                (person.person_id,),
            ).fetchone()
            if row is not None:
                return _person(row)
            connection.execute(
                """
                INSERT INTO identity_persons (
                    person_id, display_name, subject_category, age_band,
                    age_evidence_status, locale, timezone, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    person.person_id,
                    person.display_name,
                    person.subject_category,
                    person.age_band,
                    person.age_evidence_status,
                    person.locale,
                    person.timezone,
                    person.status,
                    _ts(person.created_at, field="created_at"),
                    _ts(person.updated_at, field="updated_at"),
                ),
            )
            if audit_event is not None:
                _insert_audit(connection, audit_event)
            if outbox_event is not None:
                _insert_outbox(connection, outbox_event)
            return person

    async def declare_age_evidence(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE identity_persons
                SET subject_category = ?, age_band = ?,
                    age_evidence_status = ?, updated_at = ?
                WHERE person_id = ?
                """,
                (
                    person.subject_category,
                    person.age_band,
                    person.age_evidence_status,
                    _ts(person.updated_at, field="updated_at"),
                    person.person_id,
                ),
            )
            if audit_event is not None:
                _insert_audit(connection, audit_event)

    async def verify_age_evidence(
        self,
        person: PersonSubject,
        *,
        evidence_id: str,
        verifier_person_id: str,
        actor_person_id: str | None = None,
        scope: str = "registration",
    ) -> None:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE identity_persons
                SET subject_category = ?, age_band = ?,
                    age_evidence_status = ?, updated_at = ?
                WHERE person_id = ?
                """,
                (
                    person.subject_category,
                    person.age_band,
                    person.age_evidence_status,
                    _ts(person.updated_at, field="updated_at"),
                    person.person_id,
                ),
            )

    async def update_person_profile(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE identity_persons
                SET display_name = ?, locale = ?, timezone = ?,
                    updated_at = ?
                WHERE person_id = ?
                """,
                (
                    person.display_name,
                    person.locale,
                    person.timezone,
                    _ts(person.updated_at, field="updated_at"),
                    person.person_id,
                ),
            )
            if audit_event is not None:
                _insert_audit(connection, audit_event)

    async def get_person(
        self,
        person_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> PersonSubject | None:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM identity_persons WHERE person_id = ?", (person_id,)
            ).fetchone()
            return _person(row) if row is not None else None

    async def person_exists(self, person_id: str) -> bool:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM identity_persons WHERE person_id = ?", (person_id,)
            ).fetchone()
            return row is not None

    async def has_active_relationship(
        self,
        *,
        source_person_id: str,
        target_person_id: str,
        relation_type: str,
        at: datetime,
    ) -> bool:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM identity_relationships
                WHERE source_person_id = ?
                  AND target_person_id = ?
                  AND relation_type = ?
                  AND status = 'active'
                  AND valid_from <= ?
                  AND (valid_until IS NULL OR valid_until > ?)
                LIMIT 1
                """,
                (
                    source_person_id,
                    target_person_id,
                    relation_type,
                    _ts(at, field="at"),
                    _ts(at, field="at"),
                ),
            ).fetchone()
            return row is not None

    async def save_relationship(
        self,
        relationship: Relationship,
        *,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        extra_relationships: tuple[Relationship, ...] = (),
        extra_audit_events: tuple[AuditEvent, ...] = (),
        expected_updated_at: datetime | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _upsert_relationship(
                connection, relationship, expected_updated_at=expected_updated_at
            )
            if audit_event is not None:
                _insert_audit(connection, audit_event)
            if outbox_event is not None:
                _insert_outbox(connection, outbox_event)
            for extra in extra_relationships:
                _insert_relationship(connection, extra)
            for extra_audit in extra_audit_events:
                _insert_audit(connection, extra_audit)

    async def get_relationship(
        self,
        relationship_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> Relationship | None:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM identity_relationships WHERE relationship_id = ?",
                (relationship_id,),
            ).fetchone()
            return _relationship(row) if row is not None else None

    async def list_relationships(
        self,
        person_id: str,
        statuses: tuple[RelationshipStatus, ...] | None = None,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[Relationship, ...]:
        self._ready()
        with self._connect() as connection:
            if statuses is None:
                rows = connection.execute(
                    """
                    SELECT * FROM identity_relationships
                    WHERE source_person_id = ? OR target_person_id = ?
                    ORDER BY created_at
                    """,
                    (person_id, person_id),
                ).fetchall()
            else:
                placeholders = ", ".join("?" for _ in statuses)
                rows = connection.execute(
                    f"""
                    SELECT * FROM identity_relationships
                    WHERE (source_person_id = ? OR target_person_id = ?)
                      AND status IN ({placeholders})
                    ORDER BY created_at
                    """,
                    (person_id, person_id, *statuses),
                ).fetchall()
            return tuple(_relationship(row) for row in rows)

    async def scan_relationships(
        self,
        statuses: tuple[RelationshipStatus, ...] | None = None,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[Relationship, ...]:
        self._ready()
        with self._connect() as connection:
            if statuses is None:
                rows = connection.execute(
                    "SELECT * FROM identity_relationships ORDER BY created_at"
                ).fetchall()
            else:
                placeholders = ", ".join("?" for _ in statuses)
                rows = connection.execute(
                    f"""
                    SELECT * FROM identity_relationships
                    WHERE status IN ({placeholders})
                    ORDER BY created_at
                    """,
                    tuple(statuses),
                ).fetchall()
            return tuple(_relationship(row) for row in rows)

    async def persist_binding(
        self,
        binding: DeviceBinding,
        *,
        previous_binding_id: str | None = None,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if previous_binding_id is not None:
                previous_row = connection.execute(
                    "SELECT * FROM identity_device_bindings WHERE binding_id = ?",
                    (previous_binding_id,),
                ).fetchone()
                if previous_row is None:
                    raise IdentityNotFoundError(
                        f"binding {previous_binding_id} to supersede does not exist"
                    )
                if str(previous_row["device_id"]) != binding.device_id:
                    raise BindingVersionConflictError(
                        "superseding binding must target the same device"
                    )
                if str(previous_row["status"]) != "active":
                    raise BindingVersionConflictError(
                        "only an active binding can be superseded"
                    )
                version = int(previous_row["binding_version"]) + 1
                valid_until = _ts(binding.valid_from, field="valid_from")
                if previous_row["valid_until"] is not None:
                    previous_until = str(previous_row["valid_until"])
                    if previous_until < valid_until:
                        valid_until = previous_until
                connection.execute(
                    """
                    UPDATE identity_device_bindings
                    SET status = 'superseded', valid_until = ?
                    WHERE binding_id = ?
                    """,
                    (valid_until, previous_binding_id),
                )
                connection.execute(
                    """
                    UPDATE identity_device_binding_roles
                    SET status = 'superseded', ended_at = ?
                    WHERE binding_id = ? AND status = 'active'
                    """,
                    (_ts(binding.valid_from, field="valid_from"), previous_binding_id),
                )
            else:
                version_row = connection.execute(
                    "SELECT COALESCE(MAX(binding_version), 0) AS version "
                    "FROM identity_device_bindings WHERE device_id = ?",
                    (binding.device_id,),
                ).fetchone()
                version = int(version_row["version"]) + 1
            try:
                _insert_binding(connection, binding, version)
            except sqlite3.IntegrityError as exc:
                raise BindingVersionConflictError(
                    f"binding version {version} already exists for device {binding.device_id}"
                ) from exc
            persisted = replace(binding, binding_version=version)
            if audit_event is not None:
                _insert_audit(connection, _with_binding_version(audit_event, version))
            if outbox_event is not None:
                _insert_outbox(connection, _with_binding_version(outbox_event, version))
            return persisted

    async def get_binding(
        self,
        binding_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding | None:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM identity_device_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if row is None:
                return None
            roles = connection.execute(
                "SELECT * FROM identity_device_binding_roles WHERE binding_id = ?",
                (binding_id,),
            ).fetchall()
            return _binding(row, roles)

    async def get_active_binding(
        self,
        device_id: str,
        now: datetime,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding | None:
        self._ready()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM identity_device_bindings
                WHERE device_id = ? AND status = 'active'
                ORDER BY binding_version DESC
                """,
                (device_id,),
            ).fetchall()
            for row in rows:
                valid_from = _from_iso(row["valid_from"])
                valid_until = _from_iso(row["valid_until"])
                if valid_from is not None and valid_from > now:
                    continue
                if valid_until is not None and valid_until <= now:
                    continue
                roles = connection.execute(
                    "SELECT * FROM identity_device_binding_roles WHERE binding_id = ?",
                    (str(row["binding_id"]),),
                ).fetchall()
                return _binding(row, roles)
            return None

    async def list_binding_versions(
        self,
        device_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[DeviceBinding, ...]:
        self._ready()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM identity_device_bindings
                WHERE device_id = ? ORDER BY binding_version
                """,
                (device_id,),
            ).fetchall()
            return tuple(
                _binding(
                    row,
                    connection.execute(
                        "SELECT * FROM identity_device_binding_roles WHERE binding_id = ?",
                        (str(row["binding_id"]),),
                    ).fetchall(),
                )
                for row in rows
            )

    async def scan_bindings(
        self,
        statuses: tuple[BindingStatus, ...] | None = None,
        *,
        scope: str = "api",
    ) -> tuple[DeviceBinding, ...]:
        self._ready()
        with self._connect() as connection:
            if statuses is None:
                rows = connection.execute(
                    "SELECT * FROM identity_device_bindings ORDER BY created_at"
                ).fetchall()
            else:
                placeholders = ", ".join("?" for _ in statuses)
                rows = connection.execute(
                    f"""
                    SELECT * FROM identity_device_bindings
                    WHERE status IN ({placeholders})
                    ORDER BY created_at
                    """,
                    tuple(statuses),
                ).fetchall()
            return tuple(
                _binding(
                    row,
                    connection.execute(
                        "SELECT * FROM identity_device_binding_roles WHERE binding_id = ?",
                        (str(row["binding_id"]),),
                    ).fetchall(),
                )
                for row in rows
            )

    async def transition_binding(
        self,
        binding_id: str,
        *,
        status: BindingStatus,
        valid_until: datetime | None,
        at: datetime,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM identity_device_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise IdentityNotFoundError(f"binding {binding_id} does not exist")
            until = _ts(valid_until, field="valid_until") if valid_until else None
            connection.execute(
                """
                UPDATE identity_device_bindings
                SET status = ?, valid_until = ?
                WHERE binding_id = ?
                """,
                (status, until, binding_id),
            )
            connection.execute(
                """
                UPDATE identity_device_binding_roles
                SET status = ?, ended_at = ?
                WHERE binding_id = ? AND status = 'active'
                """,
                (status, _ts(at, field="at"), binding_id),
            )
            updated = connection.execute(
                "SELECT * FROM identity_device_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            roles = connection.execute(
                "SELECT * FROM identity_device_binding_roles WHERE binding_id = ?",
                (binding_id,),
            ).fetchall()
            result = _binding(updated, roles)
            if audit_event is not None:
                _insert_audit(
                    connection, _with_transition_state(audit_event, status, valid_until)
                )
            if outbox_event is not None:
                _insert_outbox(
                    connection, _with_transition_state(outbox_event, status, valid_until)
                )
            return result

    async def save_transfer_intent(
        self,
        intent: TransferIntent,
        *,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        expected_updated_at: datetime | None = None,
        idempotency_record: IdempotencyRecord | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_record is not None:
                _insert_idempotency_record(connection, idempotency_record)
            _upsert_transfer_intent(
                connection, intent, expected_updated_at=expected_updated_at
            )
            if audit_event is not None:
                _insert_audit(connection, audit_event)
            if outbox_event is not None:
                _insert_outbox(connection, outbox_event)

    async def get_transfer_intent(
        self,
        transfer_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> TransferIntent | None:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM identity_transfer_intents WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
            return _transfer_intent(row) if row is not None else None

    async def list_transfer_intents(
        self,
        device_id: str,
        statuses: tuple[str, ...] | None = None,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[TransferIntent, ...]:
        self._ready()
        with self._connect() as connection:
            if statuses is None:
                rows = connection.execute(
                    "SELECT * FROM identity_transfer_intents "
                    "WHERE device_id = ? ORDER BY created_at",
                    (device_id,),
                ).fetchall()
            else:
                placeholders = ", ".join("?" for _ in statuses)
                rows = connection.execute(
                    f"SELECT * FROM identity_transfer_intents "
                    f"WHERE device_id = ? AND status IN ({placeholders}) "
                    f"ORDER BY created_at",
                    (device_id, *statuses),
                ).fetchall()
            return tuple(_transfer_intent(row) for row in rows)

    async def get_idempotency_record(
        self,
        scope_key: str,
        idempotency_key: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> IdempotencyRecord | None:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT scope_key, idempotency_key, operation, content_hash,
                       result_payload_json, created_at
                FROM identity_idempotency_records
                WHERE scope_key = ? AND idempotency_key = ?
                """,
                (scope_key, idempotency_key),
            ).fetchone()
            if row is None:
                return None
            return IdempotencyRecord(
                scope_key=str(row["scope_key"]),
                idempotency_key=str(row["idempotency_key"]),
                operation=str(row["operation"]),  # type: ignore[arg-type]
                content_hash=str(row["content_hash"]),
                result_payload=json.loads(str(row["result_payload_json"])),
                created_at=cast(datetime, _from_iso(row["created_at"])),
            )

    async def save_idempotency_record(
        self,
        record: IdempotencyRecord,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> bool:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return _insert_idempotency_record(connection, record)

    async def scan_transfer_intents(
        self,
        statuses: tuple[str, ...] | None = None,
        *,
        scope: str = "api",
    ) -> tuple[TransferIntent, ...]:
        self._ready()
        with self._connect() as connection:
            if statuses is None:
                rows = connection.execute(
                    "SELECT * FROM identity_transfer_intents ORDER BY created_at"
                ).fetchall()
            else:
                placeholders = ", ".join("?" for _ in statuses)
                rows = connection.execute(
                    f"SELECT * FROM identity_transfer_intents "
                    f"WHERE status IN ({placeholders}) ORDER BY created_at",
                    tuple(statuses),
                ).fetchall()
            return tuple(_transfer_intent(row) for row in rows)

    async def complete_transfer(
        self,
        *,
        intent: TransferIntent,
        binding: DeviceBinding,
        previous_binding_id: str,
        audit_events: tuple[AuditEvent, ...],
        outbox_events: tuple[OutboxEvent, ...],
        idempotency_record: IdempotencyRecord | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_record is not None:
                _insert_idempotency_record(connection, idempotency_record)
            stored_row = connection.execute(
                "SELECT * FROM identity_transfer_intents WHERE transfer_id = ?",
                (intent.transfer_id,),
            ).fetchone()
            if stored_row is None:
                raise IdentityNotFoundError(
                    f"transfer {intent.transfer_id} does not exist"
                )
            if str(stored_row["status"]) != "pending":
                raise RuntimeError(
                    f"transfer {intent.transfer_id} is not pending "
                    f"(status={stored_row['status']})"
                )
            previous_row = connection.execute(
                "SELECT * FROM identity_device_bindings WHERE binding_id = ?",
                (previous_binding_id,),
            ).fetchone()
            if previous_row is None:
                raise IdentityNotFoundError(
                    f"binding {previous_binding_id} to supersede does not exist"
                )
            if str(previous_row["device_id"]) != binding.device_id:
                raise BindingVersionConflictError(
                    "superseding binding must target the same device"
                )
            if str(previous_row["status"]) != "active":
                raise BindingVersionConflictError(
                    "only an active binding can be superseded"
                )
            version = int(previous_row["binding_version"]) + 1
            existing = connection.execute(
                "SELECT 1 FROM identity_device_bindings "
                "WHERE device_id = ? AND binding_version = ?",
                (binding.device_id, version),
            ).fetchone()
            if existing is not None:
                raise BindingVersionConflictError(
                    f"binding version {version} already exists for device {binding.device_id}"
                )
            valid_until = _ts(binding.valid_from, field="valid_from")
            if previous_row["valid_until"] is not None:
                previous_until = str(previous_row["valid_until"])
                if previous_until < valid_until:
                    valid_until = previous_until
            connection.execute(
                "UPDATE identity_device_bindings "
                "SET status = 'superseded', valid_until = ? WHERE binding_id = ?",
                (valid_until, previous_binding_id),
            )
            connection.execute(
                "UPDATE identity_device_binding_roles "
                "SET status = 'superseded', ended_at = ? "
                "WHERE binding_id = ? AND status = 'active'",
                (_ts(binding.valid_from, field="valid_from"), previous_binding_id),
            )
            persisted = replace(binding, binding_version=version)
            _insert_binding(connection, persisted, version)
            _insert_transfer_intent(connection, intent)
            for audit_event in audit_events:
                _insert_audit(connection, _with_binding_version(audit_event, version))
            for outbox_event in outbox_events:
                _insert_outbox(connection, _with_binding_version(outbox_event, version))
            if idempotency_record is not None and "binding_version" in (
                idempotency_record.result_payload
            ):
                result_payload = dict(idempotency_record.result_payload)
                result_payload["binding_version"] = version
                connection.execute(
                    """
                    UPDATE identity_idempotency_records
                    SET result_payload_json = ?
                    WHERE scope_key = ? AND idempotency_key = ?
                    """,
                    (
                        json.dumps(
                            result_payload, ensure_ascii=False, sort_keys=True
                        ),
                        idempotency_record.scope_key,
                        idempotency_record.idempotency_key,
                    ),
                )
            return persisted

    async def append_audit(self, event: AuditEvent) -> None:
        self._ready()
        with self._connect() as connection:
            _insert_audit(connection, event)

    async def enqueue_outbox(self, event: OutboxEvent) -> None:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _insert_outbox(connection, event)
            except sqlite3.IntegrityError as exc:
                raise RuntimeError(
                    f"outbox event {event.event_id} already enqueued"
                ) from exc


def _person(row: sqlite3.Row) -> PersonSubject:
    return PersonSubject(
        person_id=str(row["person_id"]),
        display_name=str(row["display_name"]),
        subject_category=cast(SubjectCategory, str(row["subject_category"])),
        age_band=cast(AgeBand, str(row["age_band"])),
        age_evidence_status=cast(AgeEvidenceStatus, str(row["age_evidence_status"])),
        locale=str(row["locale"]),
        timezone=str(row["timezone"]),
        status=cast(PersonStatus, str(row["status"])),
        created_at=cast(datetime, _from_iso(row["created_at"])),
        updated_at=cast(datetime, _from_iso(row["updated_at"])),
    )


def _relationship(row: sqlite3.Row) -> Relationship:
    permissions = json.loads(str(row["permissions_json"]))
    return Relationship(
        relationship_id=str(row["relationship_id"]),
        source_person_id=str(row["source_person_id"]),
        target_person_id=str(row["target_person_id"]),
        relation_type=cast(RelationType, str(row["relation_type"])),
        status=cast(RelationshipStatus, str(row["status"])),
        valid_from=cast(datetime, _from_iso(row["valid_from"])),
        valid_until=_from_iso(row["valid_until"]),
        established_evidence_id=str(row["established_evidence_id"]),
        confirmed_by_source_at=_from_iso(row["confirmed_by_source_at"]),
        confirmed_by_target_at=_from_iso(row["confirmed_by_target_at"]),
        dispute_resolution_acked_by_source_at=_from_iso(
            row["dispute_resolution_acked_by_source_at"]
        ),
        dispute_resolution_acked_by_target_at=_from_iso(
            row["dispute_resolution_acked_by_target_at"]
        ),
        requires_confirmation=bool(int(row["requires_confirmation"])),
        can_delegate=bool(int(row["can_delegate"])),
        delegated_from_relationship_id=(
            str(row["delegated_from_relationship_id"])
            if row["delegated_from_relationship_id"] is not None
            else None
        ),
        delegation_depth=int(row["delegation_depth"]),
        permissions=permission_set(permissions),
        dispute_reason=(
            str(row["dispute_reason"]) if row["dispute_reason"] is not None else None
        ),
        auto_suspended=bool(int(row["auto_suspended"]))
        if row["auto_suspended"] is not None
        else False,
        revoked_at=_from_iso(row["revoked_at"]),
        revocation_evidence_id=(
            str(row["revocation_evidence_id"])
            if row["revocation_evidence_id"] is not None
            else None
        ),
        created_at=cast("datetime", _from_iso(row["created_at"])),
        updated_at=cast("datetime", _from_iso(row["updated_at"])),
    )


def _transfer_intent(row: sqlite3.Row) -> TransferIntent:
    return transfer_from_dict(
        {
            "transfer_id": str(row["transfer_id"]),
            "device_id": str(row["device_id"]),
            "from_account_owner_person_id": str(row["from_account_owner_person_id"]),
            "to_account_owner_person_id": str(row["to_account_owner_person_id"]),
            "status": str(row["status"]),
            "step_up_evidence_id": str(row["step_up_evidence_id"]),
            "policy_receipt_id": str(row["policy_receipt_id"]),
            "idempotency_key": str(row["idempotency_key"] or ""),
            "evidence_hash": str(row["evidence_hash"] or ""),
            "supersedes_binding_id": row["supersedes_binding_id"],
            "resulting_binding_id": row["resulting_binding_id"],
            "created_by_person_id": row["created_by_person_id"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "valid_until": row["valid_until"],
            "accepted_at": row["accepted_at"],
            "cancelled_at": row["cancelled_at"],
            "cancelled_by_person_id": row["cancelled_by_person_id"],
            "cancel_reason": row["cancel_reason"],
        }
    )


def _insert_binding(
    connection: sqlite3.Connection, binding: DeviceBinding, version: int
) -> None:
    connection.execute(
        """
        INSERT INTO identity_device_bindings (
            binding_id, device_id, declared_mode, family_space_id,
            account_owner_person_id, binding_version, status, reason,
            valid_from, valid_until, supersedes_binding_id,
            service_profile_version, policy_bundle_version,
            consent_snapshot_id, persona_assignment_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            binding.binding_id,
            binding.device_id,
            binding.declared_mode,
            binding.family_space_id,
            binding.account_owner_person_id,
            version,
            binding.status,
            binding.reason,
            _ts(binding.valid_from, field="valid_from"),
            _ts(binding.valid_until, field="valid_until") if binding.valid_until else None,
            binding.supersedes_binding_id,
            binding.service_profile_version,
            binding.policy_bundle_version,
            binding.consent_snapshot_id,
            binding.persona_assignment_id,
            _ts(binding.created_at, field="created_at"),
        ),
    )
    for grant in binding.roles:
        connection.execute(
            """
            INSERT INTO identity_device_binding_roles (
                binding_id, person_id, role, status,
                permissions_json, granted_at, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding.binding_id,
                grant.person_id,
                grant.role,
                grant.status,
                json.dumps(sorted(grant.permissions), ensure_ascii=False),
                _ts(grant.granted_at, field="granted_at"),
                _ts(grant.ended_at, field="ended_at") if grant.ended_at else None,
            ),
        )


def _insert_transfer_intent(
    connection: sqlite3.Connection, intent: TransferIntent
) -> None:
    connection.execute(
        """
        INSERT INTO identity_transfer_intents (
            transfer_id, device_id, from_account_owner_person_id,
            to_account_owner_person_id, status, step_up_evidence_id,
            policy_receipt_id, idempotency_key, evidence_hash,
            supersedes_binding_id, resulting_binding_id, created_by_person_id,
            created_at, updated_at, valid_until, accepted_at, cancelled_at,
            cancelled_by_person_id, cancel_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(transfer_id) DO UPDATE SET
            status = excluded.status,
            updated_at = excluded.updated_at,
            supersedes_binding_id = excluded.supersedes_binding_id,
            resulting_binding_id = excluded.resulting_binding_id,
            valid_until = excluded.valid_until,
            accepted_at = excluded.accepted_at,
            cancelled_at = excluded.cancelled_at,
            cancelled_by_person_id = excluded.cancelled_by_person_id,
            cancel_reason = excluded.cancel_reason
        """,
        (
            intent.transfer_id,
            intent.device_id,
            intent.from_account_owner_person_id,
            intent.to_account_owner_person_id,
            intent.status,
            intent.step_up_evidence_id,
            intent.policy_receipt_id,
            intent.idempotency_key,
            intent.evidence_hash,
            intent.supersedes_binding_id,
            intent.resulting_binding_id,
            intent.created_by_person_id,
            _ts(intent.created_at, field="created_at"),
            _ts(intent.updated_at, field="updated_at"),
            _ts(intent.valid_until, field="valid_until") if intent.valid_until else None,
            _ts(intent.accepted_at, field="accepted_at") if intent.accepted_at else None,
            _ts(intent.cancelled_at, field="cancelled_at") if intent.cancelled_at else None,
            intent.cancelled_by_person_id,
            intent.cancel_reason,
        ),
    )


def _upsert_transfer_intent(
    connection: sqlite3.Connection,
    intent: TransferIntent,
    *,
    expected_updated_at: datetime | None,
) -> None:
    """CAS-style intent transition: rejects stale concurrent writers."""
    if expected_updated_at is None:
        _insert_transfer_intent(connection, intent)
        return
    existing = connection.execute(
        "SELECT updated_at FROM identity_transfer_intents WHERE transfer_id = ?",
        (intent.transfer_id,),
    ).fetchone()
    if existing is None:
        _insert_transfer_intent(connection, intent)
        return
    if str(existing["updated_at"]) != _ts(
        expected_updated_at, field="expected_updated_at"
    ):
        raise IdentityConflictError(
            f"transfer {intent.transfer_id} changed concurrently"
        )
    _insert_transfer_intent(connection, intent)


def _insert_relationship(
    connection: sqlite3.Connection, relationship: Relationship
) -> None:
    connection.execute(
        """
        INSERT INTO identity_relationships (
            relationship_id, source_person_id, target_person_id,
            relation_type, status, valid_from, valid_until,
            established_evidence_id, confirmed_by_source_at,
            confirmed_by_target_at, dispute_resolution_acked_by_source_at,
            dispute_resolution_acked_by_target_at, requires_confirmation,
            can_delegate, delegated_from_relationship_id,
            delegation_depth, permissions_json, dispute_reason,
            auto_suspended, revoked_at, revocation_evidence_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(relationship_id) DO UPDATE SET
            status = excluded.status,
            valid_from = excluded.valid_from,
            valid_until = excluded.valid_until,
            confirmed_by_source_at = excluded.confirmed_by_source_at,
            confirmed_by_target_at = excluded.confirmed_by_target_at,
            dispute_resolution_acked_by_source_at =
                excluded.dispute_resolution_acked_by_source_at,
            dispute_resolution_acked_by_target_at =
                excluded.dispute_resolution_acked_by_target_at,
            dispute_reason = excluded.dispute_reason,
            auto_suspended = excluded.auto_suspended,
            revoked_at = excluded.revoked_at,
            revocation_evidence_id = excluded.revocation_evidence_id,
            updated_at = excluded.updated_at
        """,
        (
            relationship.relationship_id,
            relationship.source_person_id,
            relationship.target_person_id,
            relationship.relation_type,
            relationship.status,
            _ts(relationship.valid_from, field="valid_from"),
            _ts(relationship.valid_until, field="valid_until")
            if relationship.valid_until
            else None,
            relationship.established_evidence_id,
            _ts(relationship.confirmed_by_source_at, field="confirmed_by_source_at")
            if relationship.confirmed_by_source_at
            else None,
            _ts(relationship.confirmed_by_target_at, field="confirmed_by_target_at")
            if relationship.confirmed_by_target_at
            else None,
            _ts(
                relationship.dispute_resolution_acked_by_source_at,
                field="dispute_resolution_acked_by_source_at",
            )
            if relationship.dispute_resolution_acked_by_source_at
            else None,
            _ts(
                relationship.dispute_resolution_acked_by_target_at,
                field="dispute_resolution_acked_by_target_at",
            )
            if relationship.dispute_resolution_acked_by_target_at
            else None,
            1 if relationship.requires_confirmation else 0,
            1 if relationship.can_delegate else 0,
            relationship.delegated_from_relationship_id,
            relationship.delegation_depth,
            json.dumps(sorted(relationship.permissions), ensure_ascii=False),
            relationship.dispute_reason,
            1 if relationship.auto_suspended else 0,
            _ts(relationship.revoked_at, field="revoked_at")
            if relationship.revoked_at
            else None,
            relationship.revocation_evidence_id,
            _ts(relationship.created_at, field="created_at"),
            _ts(relationship.updated_at, field="updated_at"),
        ),
    )


def _upsert_relationship(
    connection: sqlite3.Connection,
    relationship: Relationship,
    *,
    expected_updated_at: datetime | None,
) -> None:
    """CAS-style relationship transition: rejects stale concurrent writers
    (two-party confirm / dispute acknowledgement / revoke races)."""
    if expected_updated_at is None:
        _insert_relationship(connection, relationship)
        return
    existing = connection.execute(
        "SELECT updated_at FROM identity_relationships WHERE relationship_id = ?",
        (relationship.relationship_id,),
    ).fetchone()
    if existing is None:
        _insert_relationship(connection, relationship)
        return
    if str(existing["updated_at"]) != _ts(
        expected_updated_at, field="expected_updated_at"
    ):
        raise IdentityConflictError(
            f"relationship {relationship.relationship_id} changed concurrently"
        )
    _insert_relationship(connection, relationship)


def _insert_audit(connection: sqlite3.Connection, event: AuditEvent) -> None:
    connection.execute(
        """
        INSERT INTO identity_audit_events (
            event_id, action, actor_person_id, subject_person_id,
            person_id, device_id, binding_id, relationship_id,
            payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.action,
            event.actor_person_id,
            event.subject_person_id,
            event.person_id,
            event.device_id,
            event.binding_id,
            event.relationship_id,
            json.dumps(event.payload, ensure_ascii=False),
            _ts(event.created_at, field="created_at"),
        ),
    )


def _insert_outbox(connection: sqlite3.Connection, event: OutboxEvent) -> None:
    connection.execute(
        """
        INSERT INTO identity_outbox (
            outbox_id, event_id, topic, payload_json, status,
            attempts, locked_until, last_error_code, created_at,
            delivered_at, updated_at
        ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL, ?)
        """,
        (
            event.outbox_id,
            event.event_id,
            event.topic,
            json.dumps(event.payload, ensure_ascii=False),
            _ts(event.created_at, field="created_at"),
            _ts(event.created_at, field="created_at"),
        ),
    )


def _insert_idempotency_record(
    connection: sqlite3.Connection, record: IdempotencyRecord
) -> bool:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO identity_idempotency_records (
            scope_key, idempotency_key, operation, content_hash,
            result_payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            record.scope_key,
            record.idempotency_key,
            record.operation,
            record.content_hash,
            json.dumps(record.result_payload, ensure_ascii=False, sort_keys=True),
            _ts(record.created_at, field="created_at"),
        ),
    )
    if cursor.rowcount == 0:
        raise IdentityConflictError(
            f"idempotency key {record.idempotency_key!r} already used "
            f"for scope {record.scope_key!r}"
        )
    return True


def _binding(binding_row: sqlite3.Row, role_rows: list[sqlite3.Row]) -> DeviceBinding:
    primary_ids = tuple(
        sorted(
            str(row["person_id"])
            for row in role_rows
            if str(row["role"]) == "primary_subject"
        )
    )
    return DeviceBinding(
        binding_id=str(binding_row["binding_id"]),
        device_id=str(binding_row["device_id"]),
        declared_mode=cast(DeviceDeclaredMode, str(binding_row["declared_mode"])),
        account_owner_person_id=str(binding_row["account_owner_person_id"]),
        primary_subject_ids=primary_ids,
        binding_version=int(binding_row["binding_version"]),
        status=cast(BindingStatus, str(binding_row["status"])),
        reason=cast(BindingReason, str(binding_row["reason"])),
        family_space_id=(
            str(binding_row["family_space_id"])
            if binding_row["family_space_id"] is not None
            else None
        ),
        supersedes_binding_id=(
            str(binding_row["supersedes_binding_id"])
            if binding_row["supersedes_binding_id"] is not None
            else None
        ),
        valid_from=cast(datetime, _from_iso(binding_row["valid_from"])),
        valid_until=_from_iso(binding_row["valid_until"]),
        roles=tuple(_role(row) for row in role_rows),
        service_profile_version=str(binding_row["service_profile_version"]),
        policy_bundle_version=str(binding_row["policy_bundle_version"]),
        consent_snapshot_id=(
            str(binding_row["consent_snapshot_id"])
            if binding_row["consent_snapshot_id"] is not None
            else None
        ),
        persona_assignment_id=(
            str(binding_row["persona_assignment_id"])
            if binding_row["persona_assignment_id"] is not None
            else None
        ),
        created_at=cast(datetime, _from_iso(binding_row["created_at"])),
    )


def _role(row: sqlite3.Row) -> DeviceBindingRole:
    permissions = json.loads(str(row["permissions_json"]))
    return DeviceBindingRole(
        binding_id=str(row["binding_id"]),
        person_id=str(row["person_id"]),
        role=cast(BindingRole, str(row["role"])),
        status=cast(Literal["active", "superseded", "revoked", "expired"], str(row["status"])),
        permissions=permission_set(permissions),
        granted_at=cast(datetime, _from_iso(row["granted_at"])),
        ended_at=_from_iso(row["ended_at"]),
    )
