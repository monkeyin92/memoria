"""SQLite development adapter for the memory scope store seam.

Local development fixture mirroring the PostgreSQL schema (section 11.7:
SQLite is NOT the production authority - development and test only).  Every
read call carries the caller's ``actor_subject_id`` exactly like the
PostgreSQL adapter so the fail-closed contract is exercised in tests too.
Records and status events are append-only (immutable triggers), multi-write
operations run inside ``BEGIN IMMEDIATE`` transactions, and withdrawal is a
status event that makes the record invisible to every read path.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from services.memory_scope.domain import (
    AlreadyVotedError,
    ConfirmationVote,
    CrossFamilyAccessError,
    MemoryAuditEvent,
    MemoryOutboxEvent,
    MemoryRecord,
    MemoryRecordStatusEvent,
    MemoryScope,
    MemoryStatus,
    ProposalStatus,
    RetentionPolicy,
    SharedMemoryProposal,
    VoteDecision,
    WriteFenceMissingError,
)

#: Explicit marker: this adapter must never back production traffic.
DEV_TEST_ONLY: bool = True

_SCOPE_VALUES = "('unknown', 'session_ephemeral', 'personal_private', 'guardian_summary', 'family_shared', 'legacy_archive')"
_STATUS_VALUES = "('candidate', 'confirmed', 'disputed', 'revoked')"
_PROPOSAL_STATUS_VALUES = "('pending', 'approvals_complete', 'promoted', 'frozen', 'withdrawn')"
_VOTE_VALUES = "('confirm', 'object')"

_VOTES_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS memory_shared_votes (
    proposal_id TEXT NOT NULL REFERENCES memory_shared_proposals(proposal_id),
    subject_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN {_VOTE_VALUES}),
    voted_at TEXT NOT NULL,
    evidence_id TEXT NOT NULL DEFAULT '',
    approval_receipt_id TEXT NOT NULL DEFAULT '',
    approval_snapshot_id TEXT NOT NULL DEFAULT '',
    approval_snapshot_revision INTEGER NOT NULL DEFAULT 0
        CHECK (approval_snapshot_revision >= 0),
    approval_snapshot_hash TEXT NOT NULL DEFAULT ''
        CHECK (length(approval_snapshot_hash) IN (0, 64)),
    CHECK (
        (decision = 'confirm' AND length(approval_receipt_id) BETWEEN 1 AND 128
            AND length(approval_snapshot_id) BETWEEN 1 AND 128
            AND approval_snapshot_revision >= 1
            AND length(approval_snapshot_hash) = 64)
        OR (decision = 'object' AND approval_receipt_id = ''
            AND approval_snapshot_id = '' AND approval_snapshot_revision = 0
            AND approval_snapshot_hash = '')
    ),
    -- Append-only superseding votes (main review): the same subject may
    -- first confirm and then object (BOTH rows are kept; the latest
    -- decision wins), so approvals_complete never locks objection out.
    -- A duplicate (subject, decision) pair is still rejected.
    PRIMARY KEY (proposal_id, subject_id, decision)
);
"""

_SCHEMA = f"""
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

CREATE TABLE IF NOT EXISTS memory_shared_proposals (
    proposal_id TEXT PRIMARY KEY,
    family_space_id TEXT NOT NULL CHECK (length(family_space_id) BETWEEN 1 AND 128),
    proposer_subject_id TEXT NOT NULL,
    co_subject_ids TEXT NOT NULL DEFAULT '[]',
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 256),
    content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 16384),
    source_evidence_ids TEXT NOT NULL DEFAULT '[]',
    proposal_policy_receipt_id TEXT NOT NULL DEFAULT '' CHECK (length(proposal_policy_receipt_id) BETWEEN 1 AND 128),
    consent_snapshot_id TEXT NOT NULL DEFAULT '' CHECK (length(consent_snapshot_id) BETWEEN 1 AND 128),
    -- Canonical exact-action evidence (PolicyActionResourceFence contract):
    -- proposal revision, capture evidence digest and consent/membership
    -- snapshot id+revision+hash are the AUTHORITATIVE values the final
    -- promotion fence must match field-by-field.
    proposal_revision INTEGER NOT NULL DEFAULT 1 CHECK (proposal_revision >= 1),
    capture_evidence_hash TEXT NOT NULL DEFAULT '' CHECK (length(capture_evidence_hash) IN (0, 64)),
    consent_snapshot_revision INTEGER NOT NULL DEFAULT 0 CHECK (consent_snapshot_revision >= 0),
    consent_snapshot_hash TEXT NOT NULL DEFAULT '' CHECK (length(consent_snapshot_hash) IN (0, 64)),
    membership_snapshot_id TEXT NOT NULL DEFAULT '' CHECK (length(membership_snapshot_id) BETWEEN 0 AND 128),
    membership_snapshot_revision INTEGER NOT NULL DEFAULT 0 CHECK (membership_snapshot_revision >= 0),
    membership_snapshot_hash TEXT NOT NULL DEFAULT '' CHECK (length(membership_snapshot_hash) IN (0, 64)),
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    tool_epoch INTEGER NOT NULL DEFAULT 0 CHECK (tool_epoch >= 0),
    -- Immutable fence snapshot the proposal was issued under (main
    -- architecture review): the final confirmation re-verifies receipt /
    -- consent / membership against exactly this fence.
    session_id TEXT NOT NULL DEFAULT '' CHECK (length(session_id) BETWEEN 1 AND 128),
    epoch INTEGER NOT NULL DEFAULT 1 CHECK (epoch >= 1),
    binding_id TEXT NOT NULL DEFAULT '' CHECK (length(binding_id) BETWEEN 1 AND 128),
    binding_role TEXT NOT NULL DEFAULT '' CHECK (length(binding_role) BETWEEN 1 AND 64),
    runtime_profile_id TEXT NOT NULL DEFAULT '' CHECK (length(runtime_profile_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL DEFAULT '' CHECK (length(device_id) BETWEEN 0 AND 128),
    subject_revision INTEGER NOT NULL DEFAULT 0 CHECK (subject_revision >= 0),
    generation_id TEXT,
    turn_id INTEGER CHECK (turn_id >= 1),
    valid_until TEXT,
    fence_context_hash TEXT NOT NULL DEFAULT '' CHECK (length(fence_context_hash) BETWEEN 1 AND 128),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN {_PROPOSAL_STATUS_VALUES}),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_proposals_family
ON memory_shared_proposals(family_space_id, status, created_at);

{_VOTES_TABLE_DDL}

CREATE TABLE IF NOT EXISTS memory_outbox (
    outbox_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{{}}',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processed')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_outbox_pending
ON memory_outbox(status, created_at);

CREATE TABLE IF NOT EXISTS memory_audit_events (
    event_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    actor_subject_id TEXT NOT NULL,
    subject_id TEXT,
    record_id TEXT,
    proposal_id TEXT,
    payload TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_audit_record
ON memory_audit_events(record_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memory_audit_proposal
ON memory_audit_events(proposal_id, created_at);

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

CREATE TRIGGER IF NOT EXISTS trg_memory_votes_no_update
BEFORE UPDATE ON memory_shared_votes
BEGIN
    SELECT RAISE(ABORT, 'memory_shared_votes is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_votes_no_delete
BEFORE DELETE ON memory_shared_votes
BEGIN
    SELECT RAISE(ABORT, 'memory_shared_votes is append-only');
END;
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted timestamps must be timezone-aware")
    parsed = parsed.astimezone(UTC)
    return parsed


class SqliteMemoryStore:
    """SQLite adapter; thread-safe via a single connection plus a lock."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    async def initialize(self) -> None:
        with self._lock:
            connection = self._connect()
            connection.executescript(_SCHEMA)
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Idempotent additive migration for databases created before the
        proposal fence-snapshot columns existed (main architecture review).
        ``CREATE TABLE IF NOT EXISTS`` never adds columns, so production
        upgrades must ALTER explicitly; every statement is a no-op when the
        column already exists."""

        def _add_column(column: str, definition: str) -> None:
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(memory_shared_proposals)"
                )
            }
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE memory_shared_proposals"
                    f" ADD COLUMN {column} {definition}"
                )

        _add_column(
            "session_id",
            "TEXT NOT NULL DEFAULT '' CHECK (length(session_id) BETWEEN 1 AND 128)",
        )
        _add_column("epoch", "INTEGER NOT NULL DEFAULT 1 CHECK (epoch >= 1)")
        _add_column(
            "binding_id",
            "TEXT NOT NULL DEFAULT '' CHECK (length(binding_id) BETWEEN 1 AND 128)",
        )
        _add_column(
            "binding_role",
            "TEXT NOT NULL DEFAULT '' CHECK (length(binding_role) BETWEEN 1 AND 64)",
        )
        _add_column(
            "runtime_profile_id",
            "TEXT NOT NULL DEFAULT '' "
            "CHECK (length(runtime_profile_id) BETWEEN 1 AND 128)",
        )
        _add_column(
            "device_id",
            "TEXT NOT NULL DEFAULT '' CHECK (length(device_id) BETWEEN 0 AND 128)",
        )
        _add_column(
            "subject_revision",
            "INTEGER NOT NULL DEFAULT 0 CHECK (subject_revision >= 0)",
        )
        _add_column("generation_id", "TEXT")
        _add_column("turn_id", "INTEGER CHECK (turn_id >= 1)")
        _add_column("valid_until", "TEXT")
        _add_column(
            "fence_context_hash",
            "TEXT NOT NULL DEFAULT '' "
            "CHECK (length(fence_context_hash) BETWEEN 1 AND 128)",
        )
        # Canonical exact-action evidence columns (PolicyActionResourceFence
        # contract / main review): idempotent additive migration.
        _add_column(
            "proposal_revision",
            "INTEGER NOT NULL DEFAULT 1 CHECK (proposal_revision >= 1)",
        )
        _add_column(
            "capture_evidence_hash",
            "TEXT NOT NULL DEFAULT '' "
            "CHECK (length(capture_evidence_hash) IN (0, 64))",
        )
        _add_column(
            "consent_snapshot_revision",
            "INTEGER NOT NULL DEFAULT 0 CHECK (consent_snapshot_revision >= 0)",
        )
        _add_column(
            "consent_snapshot_hash",
            "TEXT NOT NULL DEFAULT '' "
            "CHECK (length(consent_snapshot_hash) IN (0, 64))",
        )
        _add_column(
            "membership_snapshot_id",
            "TEXT NOT NULL DEFAULT '' "
            "CHECK (length(membership_snapshot_id) BETWEEN 0 AND 128)",
        )
        _add_column(
            "membership_snapshot_revision",
            "INTEGER NOT NULL DEFAULT 0 CHECK (membership_snapshot_revision >= 0)",
        )
        _add_column(
            "membership_snapshot_hash",
            "TEXT NOT NULL DEFAULT '' "
            "CHECK (length(membership_snapshot_hash) IN (0, 64))",
        )
        _add_column("generation", "INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0)")
        _add_column(
            "tool_epoch", "INTEGER NOT NULL DEFAULT 0 CHECK (tool_epoch >= 0)"
        )
        vote_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(memory_shared_votes)")
        }
        if "approval_receipt_id" not in vote_columns:
            connection.execute(
                "ALTER TABLE memory_shared_votes"
                " ADD COLUMN approval_receipt_id TEXT NOT NULL DEFAULT ''"
            )
        if "approval_snapshot_id" not in vote_columns:
            connection.execute(
                "ALTER TABLE memory_shared_votes"
                " ADD COLUMN approval_snapshot_id TEXT NOT NULL DEFAULT ''"
            )
        if "approval_snapshot_revision" not in vote_columns:
            connection.execute(
                "ALTER TABLE memory_shared_votes"
                " ADD COLUMN approval_snapshot_revision INTEGER NOT NULL"
                " DEFAULT 0"
            )
        if "approval_snapshot_hash" not in vote_columns:
            connection.execute(
                "ALTER TABLE memory_shared_votes"
                " ADD COLUMN approval_snapshot_hash TEXT NOT NULL DEFAULT ''"
            )
        # Legacy votes PRIMARY KEY (proposal_id, subject_id) cannot hold an
        # append-only superseding vote (confirm -> object).  SQLite cannot
        # ALTER a primary key: rebuild the table with the current DDL
        # (proposal_id, subject_id, decision) and copy every row.  The
        # rebuild is idempotent - the new PK text is the marker.
        votes_ddl = connection.execute(
            "SELECT sql FROM sqlite_master"
            " WHERE type = 'table' AND name = 'memory_shared_votes'"
        ).fetchone()
        if votes_ddl is not None and (
            "subject_id, decision)" not in str(votes_ddl["sql"])
        ):
            connection.execute(
                "ALTER TABLE memory_shared_votes"
                " RENAME TO memory_shared_votes_legacy"
            )
            connection.execute(_VOTES_TABLE_DDL)
            connection.execute(
                """
                INSERT INTO memory_shared_votes (
                    proposal_id, subject_id, decision, voted_at, evidence_id,
                    approval_receipt_id, approval_snapshot_id,
                    approval_snapshot_revision, approval_snapshot_hash
                )
                SELECT proposal_id, subject_id, decision, voted_at, evidence_id,
                    approval_receipt_id, approval_snapshot_id,
                    approval_snapshot_revision, approval_snapshot_hash
                FROM memory_shared_votes_legacy
                """
            )
            connection.execute("DROP TABLE memory_shared_votes_legacy")
        if "promotion_receipt_id" in vote_columns:
            # The final promotion receipt belongs to the finalizer action
            # only; the old per-vote column is dropped (fail closed: any
            # legacy confirm row without approval evidence makes the
            # finalizer CAS fail and never promotes).
            connection.execute(
                "ALTER TABLE memory_shared_votes"
                " DROP COLUMN promotion_receipt_id"
            )
        proposal_columns_now = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(memory_shared_proposals)"
            )
        }
        if (
            "proposal_policy_receipt_id" not in proposal_columns_now
            and "policy_receipt_id" in proposal_columns_now
        ):
            connection.execute(
                "ALTER TABLE memory_shared_proposals"
                " RENAME COLUMN policy_receipt_id TO proposal_policy_receipt_id"
            )
        record_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(memory_records)")
        }
        if "promotion_receipt_id" not in record_columns:
            connection.execute(
                "ALTER TABLE memory_records"
                " ADD COLUMN promotion_receipt_id TEXT NOT NULL DEFAULT ''"
                " CHECK (length(promotion_receipt_id) BETWEEN 0 AND 128)"
            )
        # Controlled migration of the legacy ``confirmed`` proposal status:
        # a confirmed proposal WITH a promoted record maps to ``promoted``;
        # one WITHOUT any record is quarantined to ``frozen`` (never
        # guessed).  Each migration is audited.
        legacy_confirmed = connection.execute(
            "SELECT proposal_id FROM memory_shared_proposals"
            " WHERE status = 'confirmed'"
        ).fetchall()
        for row in legacy_confirmed:
            has_record = connection.execute(
                "SELECT 1 FROM memory_records"
                " WHERE shared_proposal_id = ?",
                (row["proposal_id"],),
            ).fetchone()
            new_status = "promoted" if has_record is not None else "frozen"
            connection.execute(
                "UPDATE memory_shared_proposals SET status = ?"
                " WHERE proposal_id = ?",
                (new_status, row["proposal_id"]),
            )
            connection.execute(
                "INSERT OR IGNORE INTO memory_audit_events ("
                " event_id, action, actor_subject_id, subject_id, record_id,"
                " proposal_id, payload, created_at"
                ") VALUES (?, 'proposal.status.migrated', NULL, NULL, NULL,"
                " ?, ?, ?)",
                (
                    f"audit:{row['proposal_id']}:migrated",
                    row["proposal_id"],
                    json.dumps(
                        {
                            "from": "confirmed",
                            "to": new_status,
                            "mapped_by_record": has_record is not None,
                        }
                    ),
                    datetime.now(UTC),
                ),
            )
        # Canonical-evidence quarantine (main review): a legacy
        # pending/approvals_complete proposal WITHOUT the required
        # proposal revision / consent+membership snapshot
        # id+revision+hash / capture evidence digest is NEVER promoted -
        # it is quarantined to ``frozen`` with an audit so it stays
        # readable but can never enter family promotion.
        legacy_incomplete = connection.execute(
            "SELECT proposal_id FROM memory_shared_proposals"
            " WHERE status IN ('pending', 'approvals_complete')"
            " AND (proposal_revision < 1 OR consent_snapshot_revision < 1"
            " OR membership_snapshot_revision < 1"
            " OR consent_snapshot_id = '' OR membership_snapshot_id = ''"
            " OR length(capture_evidence_hash) <> 64"
            " OR length(consent_snapshot_hash) <> 64"
            " OR length(membership_snapshot_hash) <> 64)"
        ).fetchall()
        for row in legacy_incomplete:
            connection.execute(
                "UPDATE memory_shared_proposals SET status = 'frozen',"
                " resolved_at = ? WHERE proposal_id = ?",
                (_iso(datetime.now(UTC)), row["proposal_id"]),
            )
            connection.execute(
                "INSERT OR IGNORE INTO memory_audit_events ("
                " event_id, action, actor_subject_id, subject_id, record_id,"
                " proposal_id, payload, created_at"
                ") VALUES (?, 'proposal.status.migrated', NULL, NULL, NULL,"
                " ?, ?, ?)",
                (
                    f"audit:{row['proposal_id']}:migrated:canonical-evidence",
                    row["proposal_id"],
                    json.dumps(
                        {
                            "from": "pending/approvals_complete",
                            "to": "frozen",
                            "reason": "missing canonical evidence",
                        }
                    ),
                    datetime.now(UTC),
                ),
            )
        if "promotion_fence_context_hash" not in record_columns:
            connection.execute(
                "ALTER TABLE memory_records"
                " ADD COLUMN promotion_fence_context_hash TEXT NOT NULL DEFAULT ''"
                " CHECK (length(promotion_fence_context_hash) BETWEEN 0 AND 128)"
            )
        if "approval_evidence_refs" not in record_columns:
            connection.execute(
                "ALTER TABLE memory_records"
                " ADD COLUMN approval_evidence_refs TEXT NOT NULL DEFAULT '[]'"
            )

    async def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            self._connection = connection
        return self._connection

    def _tx(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    # -- records ---------------------------------------------------------

    async def persist_record(
        self,
        record: MemoryRecord,
        *,
        actor_family_space_id: str | None,
        status_events: tuple[MemoryRecordStatusEvent, ...] = (),
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
        connection: object | None = None,
    ) -> None:
        _require_actor(record.created_by_actor_id)
        if record.family_space_id is not None:
            if actor_family_space_id != record.family_space_id:
                raise WriteFenceMissingError(
                    "record family scope must match the authoritative actor "
                    "family scope (fail closed)"
                )
        with self._lock:
            connection = self._connect()
            self._tx(connection)
            try:
                connection.execute(
                    "INSERT INTO memory_records (record_id, scope, subject_id,"
                    " resource_owner_id, family_space_id, co_subject_ids,"
                    " source_evidence_ids, policy_receipt_id, promotion_receipt_id,"
                    " promotion_fence_context_hash, approval_evidence_refs,"
                    " consent_snapshot_id,"
                    " memory_type, confidence, retention, retention_expires_at, payload,"
                    " created_by_actor_id, created_at, shared_proposal_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " ?, ?, ?)",
                    (
                        record.record_id,
                        record.scope.value,
                        record.subject_id,
                        record.resource_owner_id,
                        record.family_space_id,
                        json.dumps(list(record.co_subject_ids)),
                        json.dumps(list(record.source_evidence_ids)),
                        record.policy_receipt_id,
                        record.promotion_receipt_id or "",
                        record.promotion_fence_context_hash or "",
                        json.dumps([list(pair) for pair in record.approval_evidence_refs]),
                        record.consent_snapshot_id,
                        record.memory_type,
                        record.confidence,
                        record.retention,
                        _iso(record.retention_expires_at),
                        json.dumps(record.payload),
                        record.created_by_actor_id,
                        _iso(record.created_at),
                        record.shared_proposal_id,
                    ),
                )
                self._insert_status_events(connection, status_events)
                self._insert_outbox(connection, outbox)
                self._insert_audit(connection, audit)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    async def get_record(
        self,
        record_id: str,
        *,
        actor_subject_id: str,
        actor_family_space_id: str | None = None,
        grant_owner_id: str | None = None,
        grant_scope: str | None = None,
    ) -> MemoryRecord | None:
        _require_actor(actor_subject_id)
        with self._lock:
            connection = self._connect()
            row = connection.execute(
                "SELECT * FROM memory_records WHERE record_id = ?"
                " AND ("
                "  (family_space_id IS NULL OR family_space_id = ?)"
                "  AND (subject_id = ? OR resource_owner_id = ?"
                "       OR EXISTS (SELECT 1 FROM json_each(memory_records.co_subject_ids)"
                "                  WHERE json_each.value = ?))"
                "  OR (resource_owner_id = ? AND scope = ?)"
                ")",
                (
                    record_id,
                    actor_family_space_id,
                    actor_subject_id,
                    actor_subject_id,
                    actor_subject_id,
                    grant_owner_id,
                    grant_scope,
                ),
            ).fetchone()
            if row is None:
                return None
            events = self._status_events_for(connection, record_id)
            return _apply_status(_record_from_row(row), events)

    async def list_records_for_subject(
        self,
        subject_id: str,
        scopes: tuple[MemoryScope, ...] | None = None,
        *,
        actor_subject_id: str,
        actor_family_space_id: str | None = None,
        grant_owner_id: str | None = None,
        grant_scope: str | None = None,
        include_revoked: bool = False,
    ) -> tuple[MemoryRecord, ...]:
        _require_actor(actor_subject_id)
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                "SELECT * FROM memory_records WHERE"
                "  ("
                "    (subject_id = ? OR resource_owner_id = ?"
                "     OR EXISTS (SELECT 1 FROM json_each(memory_records.co_subject_ids)"
                "                WHERE json_each.value = ?))"
                "    AND (family_space_id IS NULL OR family_space_id = ?)"
                "  )"
                "  OR (resource_owner_id = ? AND scope = ?)"
                " ORDER BY created_at",
                (
                    subject_id,
                    subject_id,
                    subject_id,
                    actor_family_space_id,
                    grant_owner_id,
                    grant_scope,
                ),
            ).fetchall()
            return self._records_from_rows(connection, rows, scopes, include_revoked)

    async def list_records_in_family(
        self,
        family_space_id: str,
        subject_id: str,
        scopes: tuple[MemoryScope, ...] | None = None,
        *,
        actor_subject_id: str,
        actor_family_space_id: str | None,
        include_revoked: bool = False,
    ) -> tuple[MemoryRecord, ...]:
        _require_actor(actor_subject_id)
        if actor_family_space_id is not None and actor_family_space_id != family_space_id:
            raise CrossFamilyAccessError(
                f"actor family {actor_family_space_id} does not match "
                f"family {family_space_id}"
            )
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                "SELECT * FROM memory_records WHERE family_space_id = ?"
                " AND (subject_id = ? OR resource_owner_id = ?"
                "      OR EXISTS (SELECT 1 FROM json_each(memory_records.co_subject_ids)"
                "                 WHERE json_each.value = ?))"
                " ORDER BY created_at",
                (family_space_id, subject_id, subject_id, subject_id),
            ).fetchall()
            return self._records_from_rows(connection, rows, scopes, include_revoked)

    def _records_from_rows(
        self,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
        scopes: tuple[MemoryScope, ...] | None,
        include_revoked: bool,
    ) -> tuple[MemoryRecord, ...]:
        allowed = set(scopes) if scopes is not None else None
        result: list[MemoryRecord] = []
        for row in rows:
            record = _record_from_row(row)
            if allowed is not None and record.scope not in allowed:
                continue
            derived = _apply_status(
                record, self._status_events_for(connection, record.record_id)
            )
            if not include_revoked and not derived.is_visible():
                continue
            result.append(derived)
        return tuple(result)

    def _status_events_for(
        self, connection: sqlite3.Connection, record_id: str
    ) -> tuple[MemoryRecordStatusEvent, ...]:
        rows = connection.execute(
            "SELECT * FROM memory_status_events WHERE record_id = ? ORDER BY created_at",
            (record_id,),
        ).fetchall()
        return tuple(
            MemoryRecordStatusEvent(
                event_id=row["event_id"],
                record_id=row["record_id"],
                status=cast(MemoryStatus, row["status"]),
                reason_code=row["reason_code"],
                created_at=_parse_iso(row["created_at"]) or _now(),
            )
            for row in rows
        )

    def _insert_status_events(
        self, connection: sqlite3.Connection, events: tuple[MemoryRecordStatusEvent, ...]
    ) -> None:
        for event in events:
            connection.execute(
                "INSERT INTO memory_status_events"
                " (event_id, record_id, status, reason_code, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.record_id,
                    event.status,
                    event.reason_code,
                    _iso(event.created_at),
                ),
            )

    async def get_status_events(
        self, record_id: str, *, actor_subject_id: str
    ) -> tuple[MemoryRecordStatusEvent, ...]:
        _require_actor(actor_subject_id)
        with self._lock:
            return self._status_events_for(self._connect(), record_id)

    # -- proposals ---------------------------------------------------------

    async def persist_proposal(
        self,
        proposal: SharedMemoryProposal,
        *,
        actor_family_space_id: str | None,
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
    ) -> None:
        _require_actor(proposal.proposer_subject_id)
        if proposal.family_space_id != actor_family_space_id:
            raise WriteFenceMissingError(
                "proposal family scope must match the authoritative actor "
                "family scope (fail closed)"
            )
        with self._lock:
            connection = self._connect()
            self._tx(connection)
            try:
                connection.execute(
                    "INSERT INTO memory_shared_proposals (proposal_id, family_space_id,"
                    " proposer_subject_id, co_subject_ids, binding_version, title, content,"
                    " session_id, epoch, binding_id, binding_role, runtime_profile_id,"
                    " device_id, subject_revision, generation_id, turn_id, valid_until,"
                    " fence_context_hash,"
                    " source_evidence_ids, proposal_policy_receipt_id,"
                    " consent_snapshot_id,"
                    " proposal_revision, capture_evidence_hash,"
                    " consent_snapshot_revision, consent_snapshot_hash,"
                    " membership_snapshot_id, membership_snapshot_revision,"
                    " membership_snapshot_hash, generation, tool_epoch,"
                    " status, created_at, resolved_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        proposal.proposal_id,
                        proposal.family_space_id,
                        proposal.proposer_subject_id,
                        json.dumps(list(proposal.co_subject_ids)),
                        proposal.binding_version,
                        proposal.title,
                        proposal.content,
                        proposal.session_id,
                        proposal.epoch,
                        proposal.binding_id,
                        proposal.binding_role,
                        proposal.runtime_profile_id,
                        proposal.device_id,
                        proposal.subject_revision,
                        proposal.generation_id,
                        proposal.turn_id,
                        _iso(proposal.valid_until),
                        proposal.fence_context_hash,
                        json.dumps(list(proposal.source_evidence_ids)),
                        proposal.proposal_policy_receipt_id,
                        proposal.consent_snapshot_id,
                        proposal.proposal_revision,
                        proposal.capture_evidence_hash,
                        proposal.consent_snapshot_revision,
                        proposal.consent_snapshot_hash,
                        proposal.membership_snapshot_id,
                        proposal.membership_snapshot_revision,
                        proposal.membership_snapshot_hash,
                        proposal.generation,
                        proposal.tool_epoch,
                        proposal.status,
                        _iso(proposal.created_at),
                        _iso(proposal.resolved_at),
                    ),
                )
                self._insert_outbox(connection, outbox)
                self._insert_audit(connection, audit)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    async def get_proposal(
        self,
        proposal_id: str,
        *,
        actor_subject_id: str,
        actor_family_space_id: str,
    ) -> SharedMemoryProposal | None:
        _require_actor(actor_subject_id)
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM memory_shared_proposals"
                " WHERE proposal_id = ? AND family_space_id = ?",
                (proposal_id, actor_family_space_id),
            ).fetchone()
            return _proposal_from_row(row) if row is not None else None

    async def update_proposal_status(
        self,
        proposal_id: str,
        status: ProposalStatus,
        *,
        actor_subject_id: str,
        resolved_at: datetime | None = None,
    ) -> None:
        _require_actor(actor_subject_id)
        with self._lock:
            connection = self._connect()
            self._tx(connection)
            try:
                connection.execute(
                    "UPDATE memory_shared_proposals SET status = ?, resolved_at = ?"
                    " WHERE proposal_id = ?",
                    (status, _iso(resolved_at), proposal_id),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    async def persist_vote(
        self,
        vote: ConfirmationVote,
        *,
        proposal_status: ProposalStatus | None = None,
        resolved_at: datetime | None = None,
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
    ) -> None:
        with self._lock:
            connection = self._connect()
            self._tx(connection)
            try:
                connection.execute(
                    "INSERT INTO memory_shared_votes"
                    " (proposal_id, subject_id, decision, voted_at, evidence_id,"
                    " approval_receipt_id, approval_snapshot_id,"
                    " approval_snapshot_revision, approval_snapshot_hash)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        vote.proposal_id,
                        vote.subject_id,
                        vote.decision,
                        _iso(vote.voted_at),
                        vote.evidence_id,
                        vote.approval_receipt_id,
                        vote.approval_snapshot_id,
                        vote.approval_snapshot_revision,
                        vote.approval_snapshot_hash,
                    ),
                )
                if proposal_status is not None:
                    connection.execute(
                        "UPDATE memory_shared_proposals SET status = ?, resolved_at = ?"
                        " WHERE proposal_id = ?",
                        (proposal_status, _iso(resolved_at), vote.proposal_id),
                    )
                self._insert_outbox(connection, outbox)
                self._insert_audit(connection, audit)
                connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                if "memory_shared_votes" in str(exc):
                    raise AlreadyVotedError(f"vote already recorded: {exc}") from exc
                raise
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    async def vote_and_transition(
        self,
        vote: ConfirmationVote,
        *,
        actor_family_space_id: str | None,
        audit: tuple[MemoryAuditEvent, ...],
        now: datetime,
    ) -> str:
        """P0-F/P0-6: the authoritative proposal row is read inside the
        transaction - the caller's view can never influence promotion."""
        with self._lock:
            connection = self._connect()
            self._tx(connection)
            try:
                row = connection.execute(
                    "SELECT * FROM memory_shared_proposals WHERE proposal_id = ?",
                    (vote.proposal_id,),
                ).fetchone()
                if row is None:
                    connection.execute("ROLLBACK")
                    return "terminal"
                if vote.decision == "confirm" and row["status"] != "pending":
                    connection.execute("ROLLBACK")
                    return "terminal"
                if vote.decision == "object" and row["status"] not in (
                    "pending",
                    "approvals_complete",
                ):
                    connection.execute("ROLLBACK")
                    return "terminal"
                proposal = _proposal_from_row(row)
                if vote.subject_id not in proposal.all_confirmable_subjects:
                    connection.execute("ROLLBACK")
                    from services.memory_scope.domain import NotAuthorizedError

                    raise NotAuthorizedError(
                        f"{vote.subject_id} is not a co-subject of this memory"
                    )
                connection.execute(
                    "INSERT INTO memory_shared_votes"
                    " (proposal_id, subject_id, decision, voted_at, evidence_id,"
                    " approval_receipt_id, approval_snapshot_id,"
                    " approval_snapshot_revision, approval_snapshot_hash)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        vote.proposal_id,
                        vote.subject_id,
                        vote.decision,
                        _iso(vote.voted_at),
                        vote.evidence_id,
                        vote.approval_receipt_id,
                        vote.approval_snapshot_id,
                        vote.approval_snapshot_revision,
                        vote.approval_snapshot_hash,
                    ),
                )
                vote_rows = connection.execute(
                    "SELECT subject_id, decision FROM memory_shared_votes"
                    " WHERE proposal_id = ?",
                    (vote.proposal_id,),
                ).fetchall()
                confirmable = set(proposal.all_confirmable_subjects)
                # Append-only superseding votes: the LATEST decision per
                # subject wins (a subject may confirm then object; both
                # rows are kept).
                latest_by_subject: dict[str, str] = {}
                for row in vote_rows:
                    latest_by_subject[row["subject_id"]] = row["decision"]
                objectors = {
                    subject
                    for subject, decision in latest_by_subject.items()
                    if decision == "object"
                }
                confirmers = {
                    subject
                    for subject, decision in latest_by_subject.items()
                    if decision == "confirm"
                }
                result_status: str
                if objectors:
                    result_status = "frozen"
                    connection.execute(
                        "UPDATE memory_shared_proposals SET status = 'frozen',"
                        " resolved_at = ? WHERE proposal_id = ?",
                        (_iso(now), vote.proposal_id),
                    )
                    self._insert_terminal_outbox(
                        connection,
                        event_id=f"{vote.proposal_id}:frozen",
                        topic="memory.shared.frozen",
                        payload={
                            "proposal_id": vote.proposal_id,
                            "objected_by": sorted(objectors),
                        },
                        now=now,
                    )
                elif confirmers == confirmable:
                    # Vote-acceptance transaction (three authority actions (proposal / per-vote approval / final promotion)): vote is
                    # appended but NOT promoted; the proposal reports
                    # ``all_confirmed`` and the separate promotion
                    # finalizer performs the CAS promotion with a DISTINCT
                    # fresh family_shared_memory_promotion receipt.
                    result_status = "approvals_complete"
                    connection.execute(
                        "UPDATE memory_shared_proposals"
                        " SET status = 'approvals_complete',"
                        " resolved_at = ? WHERE proposal_id = ?",
                        (_iso(now), vote.proposal_id),
                    )
                else:
                    result_status = "pending"
                for event in audit:
                    connection.execute(
                        "INSERT INTO memory_audit_events (event_id, action,"
                        " actor_subject_id, subject_id, record_id, proposal_id,"
                        " payload, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            event.event_id,
                            event.action,
                            event.actor_subject_id,
                            event.subject_id,
                            event.record_id,
                            event.proposal_id,
                            json.dumps(event.payload),
                            _iso(event.created_at),
                        ),
                    )
                connection.execute("COMMIT")
                return result_status
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                if "memory_shared_votes" in str(exc):
                    raise AlreadyVotedError(f"vote already recorded: {exc}") from exc
                raise
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _insert_terminal_outbox(
        connection: sqlite3.Connection,
        *,
        event_id: str,
        topic: str,
        payload: dict[str, object],
        now: datetime,
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO memory_outbox (outbox_id, event_id, topic,"
            " payload, status, created_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?)",
            (event_id, event_id, topic, json.dumps(payload), _iso(now)),
        )

    async def freeze_proposal_atomically(
        self,
        proposal_id: str,
        *,
        actor_subject_id: str,
        family_space_id: str,
        reason: str,
        audit: tuple[MemoryAuditEvent, ...],
        outbox: tuple[MemoryOutboxEvent, ...],
        now: datetime,
    ) -> bool:
        """Fail-closed freeze (main architecture review): pending /
        approvals_complete -> frozen in ONE transaction with stable
        audit/outbox events; a terminal proposal is an idempotent no-op.
        ``actor_subject_id`` / ``family_space_id`` keep the signature
        identical with the RLS adapters (SQLite has no RLS and ignores
        them - the service layer already authorized the caller)."""
        with self._lock:
            connection = self._connect()
            self._tx(connection)
            try:
                row = connection.execute(
                    "SELECT status FROM memory_shared_proposals"
                    " WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
                if row is None or row["status"] not in (
                    "pending",
                    "approvals_complete",
                ):
                    connection.execute("ROLLBACK")
                    return False
                connection.execute(
                    "UPDATE memory_shared_proposals SET status = 'frozen',"
                    " resolved_at = ? WHERE proposal_id = ?",
                    (_iso(now), proposal_id),
                )
                self._insert_outbox(connection, outbox)
                self._insert_audit(connection, audit)
                connection.execute("COMMIT")
                return True
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    async def finalize_promotion_atomically(
        self,
        proposal_id: str,
        *,
        promotion_receipt_id: str,
        promotion_fence_context_hash: str,
        required_subject_ids: tuple[str, ...],
        approval_revisions: tuple[tuple[str, str, int, str], ...],
        actor_subject_id: str,
        family_space_id: str,
        audit: tuple[MemoryAuditEvent, ...],
        now: datetime,
    ) -> bool:
        """Promotion finalizer CAS (three authority actions (proposal / per-vote approval / final promotion)): the proposal row and
        the append-only vote set are re-read inside ONE transaction; the
        promotion only commits when still pending with the full confirm set
        and no objection.  Concurrent finalizers serialize on the single
        connection lock, producing at most one record."""
        with self._lock:
            connection = self._connect()
            self._tx(connection)
            try:
                row = connection.execute(
                    "SELECT * FROM memory_shared_proposals"
                    " WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
                if row is None or row["status"] != "approvals_complete":
                    connection.execute("ROLLBACK")
                    return False
                proposal = _proposal_from_row(row)
                vote_rows = connection.execute(
                    "SELECT subject_id, decision, approval_receipt_id,"
                    " approval_snapshot_id, approval_snapshot_revision,"
                    " approval_snapshot_hash"
                    " FROM memory_shared_votes WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchall()
                confirmable = set(proposal.all_confirmable_subjects)
                # Append-only superseding votes: the LATEST decision per
                # subject wins.
                latest_by_subject: dict[str, sqlite3.Row] = {}
                for vote_row in vote_rows:
                    latest_by_subject[vote_row["subject_id"]] = vote_row
                objectors = {
                    subject
                    for subject, vote_row in latest_by_subject.items()
                    if vote_row["decision"] == "object"
                }
                confirmers = {
                    subject
                    for subject, vote_row in latest_by_subject.items()
                    if vote_row["decision"] == "confirm"
                }
                if objectors or confirmers != confirmable:
                    connection.execute("ROLLBACK")
                    return False
                # CAS: the persisted per-vote approval evidence must match
                # the EXACT expected revisions - any drift / missing /
                # empty approval fails closed and produces NO record.
                persisted_approvals = tuple(
                    sorted(
                        (
                            vote_row["subject_id"],
                            vote_row["approval_snapshot_id"],
                            vote_row["approval_snapshot_revision"],
                            vote_row["approval_snapshot_hash"],
                        )
                        for vote_row in latest_by_subject.values()
                        if vote_row["decision"] == "confirm"
                    )
                )
                expected_approvals = tuple(sorted(approval_revisions))
                if persisted_approvals != expected_approvals or any(
                    not snapshot_id
                    for _, snapshot_id, _, _ in persisted_approvals
                ):
                    connection.execute("ROLLBACK")
                    return False
                connection.execute(
                    "UPDATE memory_shared_proposals SET status = 'promoted',"
                    " resolved_at = ? WHERE proposal_id = ?",
                    (_iso(now), proposal_id),
                )
                record_id = str(uuid.uuid4())
                cursor = connection.execute(
                    "INSERT INTO memory_records (record_id, scope, subject_id,"
                    " resource_owner_id, family_space_id, co_subject_ids,"
                    " source_evidence_ids, policy_receipt_id, promotion_receipt_id,"
                    " promotion_fence_context_hash, approval_evidence_refs,"
                    " consent_snapshot_id, memory_type, confidence, retention,"
                    " retention_expires_at, payload, created_by_actor_id,"
                    " created_at, shared_proposal_id)"
                    " SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " ?, ?, ?"
                    " WHERE NOT EXISTS ("
                    "   SELECT 1 FROM memory_records WHERE shared_proposal_id = ?"
                    ")",
                    (
                        record_id,
                        MemoryScope.MEMORY_SCOPE_FAMILY_SHARED.value,
                        proposal.proposer_subject_id,
                        proposal.family_space_id,
                        proposal.family_space_id,
                        json.dumps(list(proposal.co_subject_ids)),
                        json.dumps(list(proposal.source_evidence_ids)),
                        proposal.proposal_policy_receipt_id,
                        promotion_receipt_id,
                        promotion_fence_context_hash,
                        json.dumps(
                            [
                                list(pair)
                                for pair in (
                                    (
                                        vote_row["subject_id"],
                                        vote_row["approval_receipt_id"],
                                        vote_row["approval_snapshot_id"],
                                        vote_row["approval_snapshot_revision"],
                                        vote_row["approval_snapshot_hash"],
                                    )
                                    for vote_row in latest_by_subject.values()
                                    if vote_row["decision"] == "confirm"
                                )
                            ]
                        ),
                        proposal.consent_snapshot_id,
                        "semantic",
                        1.0,
                        "indefinite",
                        None,
                        json.dumps(
                            {"title": proposal.title, "content": proposal.content}
                        ),
                        actor_subject_id,
                        _iso(now),
                        proposal_id,
                        proposal_id,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.execute("ROLLBACK")
                    return False
                connection.execute(
                    "INSERT INTO memory_status_events"
                    " (event_id, record_id, status, reason_code, created_at)"
                    " VALUES (?, ?, 'confirmed',"
                    " 'all_co_subjects_confirmed', ?)",
                    (f"{record_id}:confirmed", record_id, _iso(now)),
                )
                self._insert_terminal_outbox(
                    connection,
                    event_id=f"{proposal_id}:confirmed",
                    topic="memory.shared.confirmed",
                    payload={
                        "proposal_id": proposal_id,
                        "record_id": record_id,
                        "family_space_id": proposal.family_space_id,
                        "promotion_receipt_id": promotion_receipt_id,
                    },
                    now=now,
                )
                self._insert_audit(connection, audit)
                connection.execute("COMMIT")
                return True
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    async def list_votes(
        self, proposal_id: str, *, actor_subject_id: str
    ) -> tuple[ConfirmationVote, ...]:
        _require_actor(actor_subject_id)
        with self._lock:
            rows = self._connect().execute(
                "SELECT * FROM memory_shared_votes WHERE proposal_id = ? ORDER BY voted_at",
                (proposal_id,),
            ).fetchall()
            return tuple(
                ConfirmationVote(
                    proposal_id=row["proposal_id"],
                    subject_id=row["subject_id"],
                    decision=cast(VoteDecision, row["decision"]),
                    voted_at=_parse_iso(row["voted_at"]) or _now(),
                    evidence_id=row["evidence_id"],
                    approval_receipt_id=row["approval_receipt_id"],
                    approval_snapshot_id=row["approval_snapshot_id"],
                    approval_snapshot_revision=row["approval_snapshot_revision"],
                    approval_snapshot_hash=row["approval_snapshot_hash"],
                )
                for row in rows
            )

    async def list_proposals_for_subject(
        self,
        subject_id: str,
        statuses: tuple[ProposalStatus, ...] | None = None,
        *,
        actor_subject_id: str,
        actor_family_space_id: str,
    ) -> tuple[SharedMemoryProposal, ...]:
        _require_actor(actor_subject_id)
        with self._lock:
            allowed = set(statuses) if statuses is not None else None
            rows = self._connect().execute(
                "SELECT * FROM memory_shared_proposals"
                " WHERE family_space_id = ? ORDER BY created_at",
                (actor_family_space_id,),
            ).fetchall()
            result: list[SharedMemoryProposal] = []
            for row in rows:
                proposal = _proposal_from_row(row)
                if subject_id not in proposal.all_confirmable_subjects:
                    continue
                if allowed is not None and proposal.status not in allowed:
                    continue
                result.append(proposal)
            return tuple(result)

    async def persist_withdrawal(
        self,
        proposal_id: str,
        *,
        actor_subject_id: str,
        record_id: str | None = None,
        status_events: tuple[MemoryRecordStatusEvent, ...] = (),
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
    ) -> None:
        _require_actor(actor_subject_id)
        with self._lock:
            connection = self._connect()
            self._tx(connection)
            try:
                connection.execute(
                    "UPDATE memory_shared_proposals SET status = 'withdrawn',"
                    " resolved_at = ? WHERE proposal_id = ?",
                    (_iso(_now()), proposal_id),
                )
                self._insert_status_events(connection, status_events)
                self._insert_outbox(connection, outbox)
                self._insert_audit(connection, audit)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    # -- outbox / audit ---------------------------------------------------

    def _insert_outbox(
        self, connection: sqlite3.Connection, events: tuple[MemoryOutboxEvent, ...]
    ) -> None:
        for event in events:
            connection.execute(
                "INSERT INTO memory_outbox (outbox_id, event_id, topic, payload, status, created_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?)",
                (
                    event.outbox_id,
                    event.event_id,
                    event.topic,
                    json.dumps(event.payload),
                    _iso(event.created_at),
                ),
            )

    def _insert_audit(
        self, connection: sqlite3.Connection, events: tuple[MemoryAuditEvent, ...]
    ) -> None:
        for event in events:
            connection.execute(
                "INSERT INTO memory_audit_events (event_id, action, actor_subject_id,"
                " subject_id, record_id, proposal_id, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.action,
                    event.actor_subject_id,
                    event.subject_id,
                    event.record_id,
                    event.proposal_id,
                    json.dumps(event.payload),
                    _iso(event.created_at),
                ),
            )

    async def append_outbox(self, event: MemoryOutboxEvent) -> None:
        with self._lock:
            connection = self._connect()
            self._tx(connection)
            try:
                self._insert_outbox(connection, (event,))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    async def append_audit(self, event: MemoryAuditEvent) -> None:
        with self._lock:
            connection = self._connect()
            self._tx(connection)
            try:
                self._insert_audit(connection, (event,))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    async def list_pending_outbox(self, limit: int = 100) -> tuple[MemoryOutboxEvent, ...]:
        with self._lock:
            rows = self._connect().execute(
                "SELECT * FROM memory_outbox WHERE status = 'pending'"
                " ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(
                MemoryOutboxEvent(
                    outbox_id=row["outbox_id"],
                    event_id=row["event_id"],
                    topic=row["topic"],
                    payload=json.loads(row["payload"]),
                    created_at=_parse_iso(row["created_at"]) or _now(),
                )
                for row in rows
            )

    async def mark_outbox_processed(self, outbox_id: str) -> None:
        with self._lock:
            connection = self._connect()
            self._tx(connection)
            try:
                connection.execute(
                    "UPDATE memory_outbox SET status = 'processed' WHERE outbox_id = ?",
                    (outbox_id,),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise


def _record_from_row(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        record_id=row["record_id"],
        scope=MemoryScope.from_value(row["scope"]) or MemoryScope.MEMORY_SCOPE_UNKNOWN,
        subject_id=row["subject_id"],
        resource_owner_id=row["resource_owner_id"],
        family_space_id=row["family_space_id"],
        co_subject_ids=tuple(json.loads(row["co_subject_ids"])),
        source_evidence_ids=tuple(json.loads(row["source_evidence_ids"])),
        policy_receipt_id=row["policy_receipt_id"],
        promotion_receipt_id=row["promotion_receipt_id"],
        promotion_fence_context_hash=row["promotion_fence_context_hash"],
        approval_evidence_refs=tuple(
            tuple(pair) for pair in json.loads(row["approval_evidence_refs"])
        ),
        consent_snapshot_id=row["consent_snapshot_id"],
        memory_type=row["memory_type"],
        confidence=row["confidence"],
        retention=cast(RetentionPolicy, row["retention"]),
        retention_expires_at=_parse_iso(row["retention_expires_at"]),
        payload=json.loads(row["payload"]),
        created_by_actor_id=row["created_by_actor_id"],
        created_at=_parse_iso(row["created_at"]) or _now(),
        shared_proposal_id=row["shared_proposal_id"],
    )


def _proposal_from_row(row: sqlite3.Row) -> SharedMemoryProposal:
    return SharedMemoryProposal(
        proposal_id=row["proposal_id"],
        family_space_id=row["family_space_id"],
        proposer_subject_id=row["proposer_subject_id"],
        co_subject_ids=tuple(json.loads(row["co_subject_ids"])),
        binding_version=row["binding_version"],
        session_id=row["session_id"],
        epoch=row["epoch"],
        binding_id=row["binding_id"],
        binding_role=row["binding_role"],
        runtime_profile_id=row["runtime_profile_id"],
        device_id=row["device_id"],
        subject_revision=row["subject_revision"],
        generation_id=row["generation_id"],
        turn_id=row["turn_id"],
        valid_until=_parse_iso(row["valid_until"]),
        fence_context_hash=row["fence_context_hash"],
        title=row["title"],
        content=row["content"],
        source_evidence_ids=tuple(json.loads(row["source_evidence_ids"])),
        proposal_policy_receipt_id=row["proposal_policy_receipt_id"],
        consent_snapshot_id=row["consent_snapshot_id"],
        proposal_revision=row["proposal_revision"],
        capture_evidence_hash=row["capture_evidence_hash"],
        consent_snapshot_revision=row["consent_snapshot_revision"],
        consent_snapshot_hash=row["consent_snapshot_hash"],
        membership_snapshot_id=row["membership_snapshot_id"],
        membership_snapshot_revision=row["membership_snapshot_revision"],
        membership_snapshot_hash=row["membership_snapshot_hash"],
        generation=row["generation"],
        tool_epoch=row["tool_epoch"],
        status=cast(ProposalStatus, row["status"]),
        created_at=_parse_iso(row["created_at"]) or _now(),
        resolved_at=_parse_iso(row["resolved_at"]),
    )


def _apply_status(
    record: MemoryRecord, events: tuple[MemoryRecordStatusEvent, ...]
) -> MemoryRecord:
    if not events:
        return record
    latest = events[-1]
    withdrawn_at = latest.created_at if latest.status == "revoked" else None
    return replace(
        record,
        status=latest.status,
        withdrawn_at=withdrawn_at,
        updated_at=latest.created_at,
    )


def _require_actor(actor_subject_id: str) -> None:
    if not actor_subject_id or not actor_subject_id.strip():
        raise WriteFenceMissingError(
            "store reads require the caller's actor_subject_id (fail closed)"
        )
