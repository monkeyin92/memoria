"""SQLite development adapter for the notification domain.

Local development fixture mirroring the PostgreSQL schema (section 11.7:
SQLite is NOT the production authority - development and test only).
Claims run inside ``BEGIN IMMEDIATE`` transactions so lease + fencing
tokens stay consistent even under concurrent workers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from services.notification.domain import (
    CancelReason,
    DeliveryAttempt,
    DeliveryReceipt,
    IntentStatus,
    NotificationConflictError,
    NotificationIntent,
    RecipientBinding,
    RelationshipStatusValue,
)
from services.notification.repository import (
    NotificationAuditEvent,
    NotificationOutboxEvent,
)

#: Explicit marker: this adapter must never back production traffic.
DEV_TEST_ONLY: bool = True

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_intents (
    intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    intent_kind TEXT NOT NULL CHECK (
        intent_kind IN ('crisis_safety', 'emergency', 'care_alert')
    ),
    subject_person_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL CHECK (length(source_event_id) BETWEEN 1 AND 128),
    policy_receipt_id TEXT NOT NULL
        CHECK (length(policy_receipt_id) BETWEEN 1 AND 128),
    session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128),
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    binding_id TEXT NOT NULL CHECK (length(binding_id) BETWEEN 1 AND 128),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    runtime_profile_id TEXT NOT NULL CHECK (length(runtime_profile_id) BETWEEN 1 AND 128),
    actor_person_id TEXT NOT NULL CHECK (length(actor_person_id) BETWEEN 1 AND 128),
    fence_context_hash TEXT NOT NULL
        CHECK (length(fence_context_hash) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL DEFAULT ''
        CHECK (length(device_id) BETWEEN 0 AND 128),
    subject_revision INTEGER NOT NULL DEFAULT 0 CHECK (subject_revision >= 0),
    valid_until TEXT NOT NULL,
    template_key TEXT NOT NULL CHECK (template_key IN (
        'crisis_safety_notice', 'emergency_notice', 'care_alert_notice'
    )),
    template_params_json TEXT NOT NULL CHECK (json_valid(template_params_json)),
    reason_code TEXT NOT NULL CHECK (
        reason_code IN ('safety_concern', 'emergency_alert', 'care_reminder')
    ),
    script_version TEXT NOT NULL CHECK (length(script_version) BETWEEN 1 AND 64),
    occurred_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'in_progress', 'delivered', 'dead_lettered', 'cancelled'
    )),
    cancelled_reason TEXT CHECK (cancelled_reason IN (
        'wrong_contact', 'relationship_revoked', 'relationship_expired',
        'relationship_disputed', 'operator_override', 'user_request',
        'authorization_revoked', 'authorization_expired'
    )),
    cancelled_at TEXT,
    delivered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_recipients (
    recipient_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES notification_intents(intent_id),
    person_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('guardian', 'emergency_contact', 'delegate')),
    relationship_id TEXT NOT NULL,
    relationship_status TEXT NOT NULL CHECK (relationship_status IN (
        'pending', 'active', 'suspended', 'revoked', 'expired', 'disputed'
    )),
    channels_json TEXT NOT NULL CHECK (json_valid(channels_json)),
    channel_index INTEGER NOT NULL DEFAULT 0 CHECK (channel_index >= 0),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'in_progress', 'delivered', 'failed', 'dead_lettered', 'cancelled'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_retries INTEGER NOT NULL CHECK (max_retries BETWEEN 1 AND 10),
    next_attempt_at TEXT,
    leased_until TEXT,
    fencing_token TEXT,
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 96
    ),
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    delivered_at TEXT,
    delivered_channel TEXT CHECK (delivered_channel IS NULL OR delivered_channel IN (
        'wechat_subscription', 'sms', 'phone_call'
    )),
    cancelled_reason TEXT CHECK (cancelled_reason IN (
        'wrong_contact', 'relationship_revoked', 'relationship_expired',
        'relationship_disputed', 'operator_override', 'user_request',
        'authorization_revoked', 'authorization_expired'
    )),
    relationship_snapshot_id TEXT NOT NULL DEFAULT ''
        CHECK (length(relationship_snapshot_id) BETWEEN 0 AND 128),
    relationship_revision INTEGER NOT NULL DEFAULT 1
        CHECK (relationship_revision >= 1),
    cancelled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_recipients_intent
ON notification_recipients(intent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notification_recipients_due
ON notification_recipients(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
    attempt_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES notification_intents(intent_id),
    recipient_id TEXT NOT NULL REFERENCES notification_recipients(recipient_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    channel TEXT NOT NULL CHECK (channel IN (
        'wechat_subscription', 'sms', 'phone_call'
    )),
    -- STABLE provider idempotency key shared by every retry of the same
    -- recipient+channel (NEVER contains attempt_number/fencing token).
    logical_delivery_key TEXT NOT NULL
        CHECK (length(logical_delivery_key) BETWEEN 1 AND 320),
    status TEXT NOT NULL
        CHECK (status IN ('leased', 'delivered', 'failed', 'uncertain')),
    fencing_token TEXT NOT NULL,
    leased_until TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT CHECK (
        error_code IS NULL OR length(error_code) BETWEEN 1 AND 96
    ),
    UNIQUE (intent_id, recipient_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS notification_receipts (
    receipt_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES notification_intents(intent_id),
    recipient_id TEXT NOT NULL REFERENCES notification_recipients(recipient_id),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES notification_delivery_attempts(attempt_id),
    channel TEXT NOT NULL CHECK (channel IN (
        'wechat_subscription', 'sms', 'phone_call'
    )),
    channel_receipt_id TEXT NOT NULL CHECK (
        length(channel_receipt_id) BETWEEN 1 AND 128
    ),
    delivered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_receipts_intent
ON notification_receipts(intent_id, delivered_at);

CREATE TABLE IF NOT EXISTS notification_audit_events (
    event_id TEXT PRIMARY KEY,
    action TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 64),
    actor_person_id TEXT,
    intent_id TEXT,
    recipient_id TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_audit_intent
ON notification_audit_events(intent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notification_outbox (
    outbox_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 64),
    subject_person_id TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'processing', 'delivered', 'failed', 'dead_lettered'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    locked_until TEXT,
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 96
    ),
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_outbox_pending
ON notification_outbox(status, created_at);
"""

#: Standalone DDL for the attempts table (the rebuild migration rewrites a
#: legacy table whose status CHECK predates ``uncertain``; the logical
#: delivery key column is backfilled from intent/recipient/channel).
_ATTEMPTS_TABLE_DDL = """
CREATE TABLE notification_delivery_attempts (
    attempt_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES notification_intents(intent_id),
    recipient_id TEXT NOT NULL REFERENCES notification_recipients(recipient_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    channel TEXT NOT NULL CHECK (channel IN (
        'wechat_subscription', 'sms', 'phone_call'
    )),
    logical_delivery_key TEXT NOT NULL
        CHECK (length(logical_delivery_key) BETWEEN 1 AND 320),
    status TEXT NOT NULL
        CHECK (status IN ('leased', 'delivered', 'failed', 'uncertain')),
    fencing_token TEXT NOT NULL,
    leased_until TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT CHECK (
        error_code IS NULL OR length(error_code) BETWEEN 1 AND 96
    ),
    UNIQUE (intent_id, recipient_id, attempt_number)
);
"""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _from_iso(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(cast(str, value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"persisted timestamp is not timezone-aware: {value!r}"
        )
    return parsed.astimezone(UTC)


def _required_iso(value: object) -> datetime:
    """NOT NULL timestamp column: aware, UTC-normalized or fail."""
    parsed = _from_iso(value)
    if parsed is None:
        raise ValueError("missing required timestamp column")
    return parsed


def _intent_from_row(row: sqlite3.Row) -> NotificationIntent:
    return NotificationIntent(
        intent_id=row["intent_id"],
        idempotency_key=row["idempotency_key"],
        intent_kind=row["intent_kind"],
        subject_person_id=row["subject_person_id"],
        source_event_id=row["source_event_id"],
        policy_receipt_id=row["policy_receipt_id"],
        session_id=row["session_id"],
        epoch=row["epoch"],
        binding_id=row["binding_id"],
        binding_version=row["binding_version"],
        runtime_profile_id=row["runtime_profile_id"],
        actor_person_id=row["actor_person_id"],
        fence_context_hash=row["fence_context_hash"],
        device_id=row["device_id"],
        subject_revision=row["subject_revision"],
        valid_until=_required_iso(row["valid_until"]),
        template_key=row["template_key"],
        template_params=json.loads(row["template_params_json"]),
        reason_code=row["reason_code"],
        script_version=row["script_version"],
        occurred_at=_required_iso(row["occurred_at"]),
        status=row["status"],
        created_at=_required_iso(row["created_at"]),
        updated_at=_required_iso(row["updated_at"]),
        cancelled_reason=row["cancelled_reason"],
        cancelled_at=_from_iso(row["cancelled_at"]),
        delivered_at=_from_iso(row["delivered_at"]),
    )


def _intent_status_update(
    intent: NotificationIntent, status: str, now: datetime
) -> NotificationIntent:
    from services.notification.domain import IntentStatus

    return NotificationIntent(
        intent_id=intent.intent_id,
        idempotency_key=intent.idempotency_key,
        intent_kind=intent.intent_kind,
        subject_person_id=intent.subject_person_id,
        source_event_id=intent.source_event_id,
        policy_receipt_id=intent.policy_receipt_id,
        session_id=intent.session_id,
        epoch=intent.epoch,
        binding_id=intent.binding_id,
        binding_version=intent.binding_version,
        runtime_profile_id=intent.runtime_profile_id,
        actor_person_id=intent.actor_person_id,
        fence_context_hash=intent.fence_context_hash,
        device_id=intent.device_id,
        subject_revision=intent.subject_revision,
        valid_until=intent.valid_until,
        template_key=intent.template_key,
        template_params=dict(intent.template_params),
        reason_code=intent.reason_code,
        script_version=intent.script_version,
        occurred_at=intent.occurred_at,
        status=cast(IntentStatus, status),
        created_at=intent.created_at,
        updated_at=now,
        cancelled_reason=intent.cancelled_reason,
        cancelled_at=intent.cancelled_at,
        delivered_at=now if status == "delivered" else intent.delivered_at,
    )


def _recipient_from_row(row: sqlite3.Row) -> RecipientBinding:
    return RecipientBinding(
        recipient_id=row["recipient_id"],
        intent_id=row["intent_id"],
        person_id=row["person_id"],
        role=row["role"],
        relationship_id=row["relationship_id"],
        relationship_status=row["relationship_status"],
        relationship_snapshot_id=row["relationship_snapshot_id"],
        relationship_revision=row["relationship_revision"],
        channels=tuple(json.loads(row["channels_json"])),
        channel_index=row["channel_index"],
        status=row["status"],
        attempts=row["attempts"],
        max_retries=row["max_retries"],
        next_attempt_at=_from_iso(row["next_attempt_at"]),
        leased_until=_from_iso(row["leased_until"]),
        fencing_token=row["fencing_token"],
        last_error_code=row["last_error_code"],
        valid_from=_required_iso(row["valid_from"]),
        valid_until=_from_iso(row["valid_until"]),
        created_at=_required_iso(row["created_at"]),
        updated_at=_required_iso(row["updated_at"]),
        delivered_at=_from_iso(row["delivered_at"]),
        delivered_channel=row["delivered_channel"],
        cancelled_reason=row["cancelled_reason"],
        cancelled_at=_from_iso(row["cancelled_at"]),
    )


def _attempt_from_row(row: sqlite3.Row) -> DeliveryAttempt:
    return DeliveryAttempt(
        attempt_id=row["attempt_id"],
        intent_id=row["intent_id"],
        recipient_id=row["recipient_id"],
        attempt_number=row["attempt_number"],
        channel=row["channel"],
        logical_delivery_key=row["logical_delivery_key"],
        status=row["status"],
        fencing_token=row["fencing_token"],
        leased_until=_required_iso(row["leased_until"]),
        started_at=_required_iso(row["started_at"]),
        finished_at=_from_iso(row["finished_at"]),
        error_code=row["error_code"],
    )


class SqliteNotificationStore:
    """SQLite implementation of the ``NotificationStore`` protocol."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._fail_after_step: str | None = None

    async def initialize(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            connection = sqlite3.connect(self._path, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(_SCHEMA)
            self._migrate(connection)
            self._connection = connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Idempotent additive migrations for databases created before the
        fence-snapshot columns existed.  ``CREATE TABLE IF NOT EXISTS`` does
        not add columns to an existing table, so every new NOT NULL column
        with a default must be backfilled here; running this twice is a
        no-op (PR-12 fence columns: intents.device_id/subject_revision and
        recipients.relationship_snapshot_id/relationship_revision)."""
        def _add_column(table: str, column: str, definition: str) -> None:
            columns = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )

        _add_column(
            "notification_intents",
            "device_id",
            "TEXT NOT NULL DEFAULT '' CHECK (length(device_id) BETWEEN 0 AND 128)",
        )
        _add_column(
            "notification_intents",
            "subject_revision",
            "INTEGER NOT NULL DEFAULT 0 CHECK (subject_revision >= 0)",
        )
        _add_column("notification_intents", "valid_until", "TEXT")
        attempt_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(notification_delivery_attempts)"
            )
        }
        if "logical_delivery_key" not in attempt_columns:
            connection.execute(
                "ALTER TABLE notification_delivery_attempts"
                " ADD COLUMN logical_delivery_key TEXT NOT NULL DEFAULT ''"
            )
            connection.execute(
                "UPDATE notification_delivery_attempts"
                " SET logical_delivery_key = intent_id || ':' || recipient_id"
                " || ':' || channel"
                " WHERE logical_delivery_key = ''"
            )
        # Legacy attempts-table status CHECK predates ``uncertain`` (SQLite
        # cannot ALTER a CHECK): rebuild the table with the current DDL and
        # backfill the stable logical delivery key.  Idempotent: once the
        # rebuilt DDL is in place the upgrade is a no-op.
        attempts_ddl = connection.execute(
            "SELECT sql FROM sqlite_master"
            " WHERE type = 'table' AND name = 'notification_delivery_attempts'"
        ).fetchone()
        if attempts_ddl is not None and "uncertain" not in str(attempts_ddl["sql"]):
            connection.execute(
                "ALTER TABLE notification_delivery_attempts"
                " RENAME TO notification_delivery_attempts_legacy"
            )
            connection.execute(_ATTEMPTS_TABLE_DDL)
            connection.execute(
                """
                INSERT INTO notification_delivery_attempts (
                    attempt_id, intent_id, recipient_id, attempt_number,
                    channel, logical_delivery_key, status, fencing_token,
                    leased_until, started_at, finished_at, error_code
                )
                SELECT attempt_id, intent_id, recipient_id, attempt_number,
                    channel,
                    CASE
                        WHEN logical_delivery_key = ''
                        THEN intent_id || ':' || recipient_id || ':' || channel
                        ELSE logical_delivery_key
                    END,
                    status, fencing_token, leased_until, started_at,
                    finished_at, error_code
                FROM notification_delivery_attempts_legacy
                """
            )
            connection.execute(
                "DROP TABLE notification_delivery_attempts_legacy"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_notification_attempts_recipient"
                " ON notification_delivery_attempts(recipient_id, attempt_number)"
            )
        # Legacy NULL fence windows fail closed: the intent is CANCELLED
        # (quarantined) with an audit trail - the NULL window is never
        # treated as unlimited and the intent can never be dispatched.
        null_rows = connection.execute(
            "SELECT intent_id, created_at FROM notification_intents"
            " WHERE valid_until IS NULL"
        ).fetchall()
        for row in null_rows:
            created_at = _required_iso(row["created_at"])
            connection.execute(
                "UPDATE notification_intents"
                " SET valid_until = created_at, status = 'cancelled',"
                "     cancelled_reason = 'authorization_expired',"
                "     cancelled_at = created_at, updated_at = created_at"
                " WHERE intent_id = ?",
                (row["intent_id"],),
            )
            connection.execute(
                "INSERT OR IGNORE INTO notification_audit_events ("
                " event_id, action, actor_person_id, intent_id, recipient_id,"
                " payload_json, created_at"
                ") VALUES (?, 'intent.quarantine', NULL, ?, NULL, ?, ?)",
                (
                    f"audit:{row['intent_id']}:quarantine",
                    row["intent_id"],
                    json.dumps({"reason": "legacy_null_fence_window"}),
                    _iso(created_at),
                ),
            )
        # The fence window invariant is now enforced at the database level
        # for legacy databases too (SQLite cannot ALTER a column to NOT
        # NULL, so equivalent BEFORE INSERT/UPDATE triggers reject any
        # future NULL write - fail closed, not just migrated away).
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS
            trg_intents_valid_until_not_null_insert
            BEFORE INSERT ON notification_intents
            WHEN NEW.valid_until IS NULL
            BEGIN
                SELECT RAISE(ABORT,
                    'notification_intents.valid_until must not be NULL');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS
            trg_intents_valid_until_not_null_update
            BEFORE UPDATE OF valid_until ON notification_intents
            WHEN NEW.valid_until IS NULL
            BEGIN
                SELECT RAISE(ABORT,
                    'notification_intents.valid_until must not be NULL');
            END
            """
        )
        _add_column(
            "notification_recipients",
            "relationship_snapshot_id",
            "TEXT NOT NULL DEFAULT '' "
            "CHECK (length(relationship_snapshot_id) BETWEEN 0 AND 128)",
        )
        _add_column(
            "notification_recipients",
            "relationship_revision",
            "INTEGER NOT NULL DEFAULT 1 CHECK (relationship_revision >= 1)",
        )

    async def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _ready(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SqliteNotificationStore.initialize() must run first")
        return self._connection

    async def save_intent(self, intent: NotificationIntent) -> NotificationIntent:
        connection = self._ready()
        with self._lock:
            try:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO notification_intents (
                            intent_id, idempotency_key, intent_kind, subject_person_id,
                            source_event_id, policy_receipt_id, session_id, epoch,
                            binding_id, binding_version, runtime_profile_id,
                            actor_person_id, fence_context_hash, device_id,
                            subject_revision, valid_until, template_key,
                            template_params_json, reason_code, script_version,
                            occurred_at, status, cancelled_reason, cancelled_at,
                            delivered_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (intent_id) DO UPDATE SET
                            status = excluded.status,
                            cancelled_reason = excluded.cancelled_reason,
                            cancelled_at = excluded.cancelled_at,
                            delivered_at = excluded.delivered_at,
                            updated_at = excluded.updated_at
                        """,
                        (
                            intent.intent_id,
                            intent.idempotency_key,
                            intent.intent_kind,
                            intent.subject_person_id,
                            intent.source_event_id,
                            intent.policy_receipt_id,
                            intent.session_id,
                            intent.epoch,
                            intent.binding_id,
                            intent.binding_version,
                            intent.runtime_profile_id,
                            intent.actor_person_id,
                            intent.fence_context_hash,
                            intent.device_id,
                            intent.subject_revision,
                            _iso(intent.valid_until),
                            intent.template_key,
                            json.dumps(intent.template_params, sort_keys=True),
                            intent.reason_code,
                            intent.script_version,
                            _iso(intent.occurred_at),
                            intent.status,
                            intent.cancelled_reason,
                            _iso(intent.cancelled_at) if intent.cancelled_at else None,
                            _iso(intent.delivered_at) if intent.delivered_at else None,
                            _iso(intent.created_at),
                            _iso(intent.updated_at),
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise NotificationConflictError(
                    f"intent idempotency key conflict: {intent.idempotency_key}"
                ) from exc
            return intent

    async def save_intent_atomically(
        self,
        intent: NotificationIntent,
        *,
        recipients: tuple[RecipientBinding, ...],
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        authenticated_actor_person_id: str | None = None,
        authenticated_subject_person_id: str | None = None,
    ) -> NotificationIntent | None:
        """First-write-wins: one SQLite transaction commits intent +
        recipients + audit + outbox; returns the intent when this call won,
        ``None`` when the idempotency key already exists, and rolls back all
        of them on any mid-way failure (section 11.6)."""
        if (
            authenticated_actor_person_id is not None
            and authenticated_actor_person_id != intent.actor_person_id
        ):
            raise ValueError("authenticated actor does not match intent actor")
        if (
            authenticated_subject_person_id is not None
            and authenticated_subject_person_id != intent.subject_person_id
        ):
            raise ValueError("authenticated subject does not match intent subject")
        connection = self._ready()
        with self._lock:
            try:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO notification_intents (
                            intent_id, idempotency_key, intent_kind, subject_person_id,
                            source_event_id, policy_receipt_id, session_id, epoch,
                            binding_id, binding_version, runtime_profile_id,
                            actor_person_id, fence_context_hash, device_id,
                            subject_revision, valid_until, template_key,
                            template_params_json, reason_code, script_version,
                            occurred_at, status, cancelled_reason, cancelled_at,
                            delivered_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            intent.intent_id,
                            intent.idempotency_key,
                            intent.intent_kind,
                            intent.subject_person_id,
                            intent.source_event_id,
                            intent.policy_receipt_id,
                            intent.session_id,
                            intent.epoch,
                            intent.binding_id,
                            intent.binding_version,
                            intent.runtime_profile_id,
                            intent.actor_person_id,
                            intent.fence_context_hash,
                            intent.device_id,
                            intent.subject_revision,
                            _iso(intent.valid_until),
                            intent.template_key,
                            json.dumps(intent.template_params, sort_keys=True),
                            intent.reason_code,
                            intent.script_version,
                            _iso(intent.occurred_at),
                            intent.status,
                            intent.cancelled_reason,
                            _iso(intent.cancelled_at) if intent.cancelled_at else None,
                            _iso(intent.delivered_at) if intent.delivered_at else None,
                            _iso(intent.created_at),
                            _iso(intent.updated_at),
                        ),
                    )
                    for recipient in recipients:
                        if self._fail_after_step == "recipients":
                            raise RuntimeError("injected failure after recipients")
                        connection.execute(
                            """
                            INSERT INTO notification_recipients (
                                recipient_id, intent_id, person_id, role, relationship_id,
                                relationship_status, relationship_snapshot_id,
                                relationship_revision, channels_json, channel_index, status,
                                attempts, max_retries, next_attempt_at, leased_until,
                                fencing_token, last_error_code, valid_from, valid_until,
                                delivered_at, delivered_channel, cancelled_reason,
                                cancelled_at, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                recipient.recipient_id,
                                recipient.intent_id,
                                recipient.person_id,
                                recipient.role,
                                recipient.relationship_id,
                                recipient.relationship_status,
                                recipient.relationship_snapshot_id,
                                recipient.relationship_revision,
                                json.dumps(list(recipient.channels)),
                                recipient.channel_index,
                                recipient.status,
                                recipient.attempts,
                                recipient.max_retries,
                                _iso(recipient.next_attempt_at),
                                _iso(recipient.leased_until),
                                recipient.fencing_token,
                                recipient.last_error_code,
                                _iso(recipient.valid_from),
                                _iso(recipient.valid_until),
                                _iso(recipient.delivered_at),
                                recipient.delivered_channel,
                                recipient.cancelled_reason,
                                _iso(recipient.cancelled_at),
                                _iso(recipient.created_at),
                                _iso(recipient.updated_at),
                            ),
                        )
                    for event in audit:
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO notification_audit_events (
                                event_id, action, actor_person_id, intent_id, recipient_id,
                                payload_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                event.event_id,
                                event.action,
                                event.actor_person_id,
                                event.intent_id,
                                event.recipient_id,
                                json.dumps(event.payload, sort_keys=True),
                                _iso(event.created_at),
                            ),
                        )
                    for out_event in outbox:
                        connection.execute(
                            """
                            INSERT INTO notification_outbox (
                                outbox_id, event_id, topic, payload_json, status, attempts,
                                locked_until, last_error_code, created_at, delivered_at,
                                updated_at
                            ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL, ?)
                            """,
                            (
                                out_event.outbox_id,
                                out_event.event_id,
                                out_event.topic,
                                json.dumps(out_event.payload, sort_keys=True),
                                _iso(out_event.created_at),
                                _iso(out_event.created_at),
                            ),
                        )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc) or "constraint" in str(exc):
                    # Concurrent (or duplicate) idempotency key: first write
                    # wins; the whole transaction rolled back.
                    return None
                raise
            return intent
    async def get_intent(
        self, intent_id: str, *, subject_person_id: str
    ) -> NotificationIntent | None:
        connection = self._ready()
        with self._lock:
            row = connection.execute(
                "SELECT * FROM notification_intents"
                " WHERE intent_id = ? AND subject_person_id = ?",
                (intent_id, subject_person_id),
            ).fetchone()
            return _intent_from_row(row) if row is not None else None

    async def get_intent_for_worker(self, intent_id: str) -> NotificationIntent | None:
        connection = self._ready()
        with self._lock:
            row = connection.execute(
                "SELECT * FROM notification_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            return _intent_from_row(row) if row is not None else None

    async def get_intent_by_idempotency_key(
        self, idempotency_key: str, *, subject_person_id: str
    ) -> NotificationIntent | None:
        connection = self._ready()
        with self._lock:
            row = connection.execute(
                "SELECT * FROM notification_intents"
                " WHERE idempotency_key = ? AND subject_person_id = ?",
                (idempotency_key, subject_person_id),
            ).fetchone()
            return _intent_from_row(row) if row is not None else None

    async def save_recipient(self, recipient: RecipientBinding) -> RecipientBinding:
        connection = self._ready()
        with self._lock:
            with connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO notification_recipients (
                        recipient_id, intent_id, person_id, role, relationship_id,
                        relationship_status, channels_json, channel_index, status,
                        attempts, max_retries, next_attempt_at, leased_until,
                        fencing_token, last_error_code, valid_from, valid_until,
                        delivered_at, delivered_channel, cancelled_reason,
                        cancelled_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        recipient.recipient_id,
                        recipient.intent_id,
                        recipient.person_id,
                        recipient.role,
                        recipient.relationship_id,
                        recipient.relationship_status,
                        json.dumps(list(recipient.channels)),
                        recipient.channel_index,
                        recipient.status,
                        recipient.attempts,
                        recipient.max_retries,
                        _iso(recipient.next_attempt_at) if recipient.next_attempt_at else None,
                        _iso(recipient.leased_until) if recipient.leased_until else None,
                        recipient.fencing_token,
                        recipient.last_error_code,
                        _iso(recipient.valid_from),
                        _iso(recipient.valid_until) if recipient.valid_until else None,
                        _iso(recipient.delivered_at) if recipient.delivered_at else None,
                        recipient.delivered_channel,
                        recipient.cancelled_reason,
                        _iso(recipient.cancelled_at) if recipient.cancelled_at else None,
                        _iso(recipient.created_at),
                        _iso(recipient.updated_at),
                    ),
                )
            return recipient

    async def cancel_intent_atomically(
        self,
        *,
        intent: NotificationIntent,
        recipients: tuple[RecipientBinding, ...],
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> NotificationIntent | None:
        connection = self._ready()
        with self._lock:
            with connection:
                for recipient in recipients:
                    connection.execute(
                        """
                        UPDATE notification_recipients
                        SET status = ?, leased_until = NULL, fencing_token = NULL,
                            next_attempt_at = NULL, cancelled_reason = ?,
                            cancelled_at = ?, updated_at = ?
                        WHERE recipient_id = ?
                          AND status NOT IN ('delivered', 'dead_lettered', 'cancelled')
                        """,
                        (
                            recipient.status,
                            recipient.cancelled_reason,
                            _iso(recipient.cancelled_at),
                            _iso(recipient.updated_at),
                            recipient.recipient_id,
                        ),
                    )

                derived_intent = self._derive_intent_sync(
                    connection, intent.intent_id, now
                )
                for event in audit:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO notification_audit_events (
                            event_id, action, actor_person_id, intent_id, recipient_id,
                            payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_id,
                            event.action,
                            event.actor_person_id,
                            event.intent_id,
                            event.recipient_id,
                            json.dumps(event.payload, sort_keys=True),
                            _iso(event.created_at),
                        ),
                    )
                for out_event in outbox:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO notification_outbox (
                            outbox_id, event_id, topic, payload_json, status, attempts,
                            locked_until, last_error_code, created_at, delivered_at,
                            updated_at
                        ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL, ?)
                        """,
                        (
                            out_event.outbox_id,
                            out_event.event_id,
                            out_event.topic,
                            json.dumps(out_event.payload, sort_keys=True),
                            _iso(out_event.created_at),
                            _iso(out_event.created_at),
                        ),
                    )
                return derived_intent

    async def cancel_recipient_atomically(
        self,
        *,
        recipient_id: str,
        recipient: RecipientBinding,
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> tuple[RecipientBinding, NotificationIntent | None] | None:
        connection = self._ready()
        with self._lock:
            with connection:
                locked = connection.execute(
                    "SELECT status FROM notification_recipients"
                    " WHERE recipient_id = ?",
                    (recipient_id,),
                ).fetchone()
                if locked is None or locked["status"] in (
                    "delivered",
                    "dead_lettered",
                    "cancelled",
                ):
                    return None
                cursor = connection.execute(
                    """
                    UPDATE notification_recipients
                    SET status = 'cancelled', leased_until = NULL,
                        fencing_token = NULL, next_attempt_at = NULL,
                        cancelled_reason = ?, cancelled_at = ?, updated_at = ?
                    WHERE recipient_id = ?
                      AND status NOT IN ('delivered', 'dead_lettered', 'cancelled')
                    """,
                    (
                        recipient.cancelled_reason,
                        _iso(recipient.cancelled_at),
                        _iso(recipient.updated_at),
                        recipient_id,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                self._insert_audit_events(connection, audit)
                self._insert_outbox_events(connection, outbox)
                intent = self._derive_intent_sync(connection, recipient.intent_id, now)
                return (recipient, intent)

    async def cancel_recipient_and_attempt_atomically(
        self,
        *,
        recipient_id: str,
        attempt_id: str,
        fencing_token: str,
        recipient: RecipientBinding,
        finished_attempt: DeliveryAttempt,
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> tuple[RecipientBinding, NotificationIntent | None] | None:
        """Worker fail-closed cancel that ALSO terminates the leased attempt
        in the same transaction (no leased residue): the attempt is only
        finished while still ``leased`` with this fencing token; the
        recipient only cancelled while not terminal; repeated calls are
        idempotent no-ops."""
        connection = self._ready()
        with self._lock:
            with connection:
                locked = connection.execute(
                    "SELECT status FROM notification_recipients"
                    " WHERE recipient_id = ?",
                    (recipient_id,),
                ).fetchone()
                if locked is None or locked["status"] in (
                    "delivered",
                    "dead_lettered",
                    "cancelled",
                ):
                    return None
                cursor = connection.execute(
                    """
                    UPDATE notification_recipients
                    SET status = 'cancelled', leased_until = NULL,
                        fencing_token = NULL, next_attempt_at = NULL,
                        cancelled_reason = ?, cancelled_at = ?, updated_at = ?
                    WHERE recipient_id = ?
                      AND status NOT IN ('delivered', 'dead_lettered', 'cancelled')
                    """,
                    (
                        recipient.cancelled_reason,
                        _iso(recipient.cancelled_at),
                        _iso(recipient.updated_at),
                        recipient_id,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                # Terminate the leased attempt only while it is still ours.
                connection.execute(
                    """
                    UPDATE notification_delivery_attempts
                    SET status = ?, finished_at = ?, error_code = ?
                    WHERE attempt_id = ?
                      AND status = 'leased'
                      AND fencing_token = ?
                    """,
                    (
                        finished_attempt.status,
                        _iso(finished_attempt.finished_at),
                        finished_attempt.error_code,
                        attempt_id,
                        fencing_token,
                    ),
                )
                self._insert_audit_events(connection, audit)
                self._insert_outbox_events(connection, outbox)
                intent = self._derive_intent_sync(connection, recipient.intent_id, now)
                return (recipient, intent)

    async def replay_recipient_atomically(
        self,
        *,
        recipient_id: str,
        recipient: RecipientBinding,
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> tuple[RecipientBinding, NotificationIntent | None] | None:
        connection = self._ready()
        with self._lock:
            with connection:
                locked = connection.execute(
                    "SELECT * FROM notification_recipients"
                    " WHERE recipient_id = ?",
                    (recipient_id,),
                ).fetchone()
                if locked is None or locked["status"] not in (
                    "failed",
                    "dead_lettered",
                ):
                    return None
                if locked["relationship_status"] != "active":
                    return None
                cursor = connection.execute(
                    """
                    UPDATE notification_recipients
                    SET status = 'failed', next_attempt_at = ?,
                        last_error_code = NULL, updated_at = ?
                    WHERE recipient_id = ?
                      AND status IN ('failed', 'dead_lettered')
                      AND relationship_status = 'active'
                    """,
                    (
                        _iso(now),
                        _iso(now),
                        recipient_id,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                self._insert_audit_events(connection, audit)
                self._insert_outbox_events(connection, outbox)
                intent = self._derive_intent_sync(connection, recipient.intent_id, now)
                return (recipient, intent)

    async def cancel_relationship_atomically(
        self,
        *,
        relationship_id: str,
        relationship_status: RelationshipStatusValue,
        reason: CancelReason,
        actor_person_id: str | None,
        now: datetime,
    ) -> tuple[tuple[RecipientBinding, ...], tuple[NotificationIntent | None, ...]]:
        connection = self._ready()
        with self._lock:
            with connection:
                from dataclasses import replace

                from services.notification.domain import cancelled_recipient

                rows = connection.execute(
                    "SELECT * FROM notification_recipients"
                    " WHERE relationship_id = ?",
                    (relationship_id,),
                ).fetchall()
                cancelled: list[RecipientBinding] = []
                touched: dict[str, NotificationIntent | None] = {}
                for row in rows:
                    cursor = connection.execute(
                        """
                        UPDATE notification_recipients
                        SET relationship_status = ?, status = 'cancelled',
                            leased_until = NULL, fencing_token = NULL,
                            next_attempt_at = NULL, cancelled_reason = ?,
                            cancelled_at = ?, updated_at = ?
                        WHERE recipient_id = ?
                          AND relationship_id = ?
                          AND status NOT IN ('delivered', 'dead_lettered', 'cancelled')
                        """,
                        (
                            relationship_status,
                            reason,
                            _iso(now),
                            _iso(now),
                            row["recipient_id"],
                            relationship_id,
                        ),
                    )
                    if cursor.rowcount == 1:
                        recipient = _recipient_from_row(row)
                        cancelled.append(
                            replace(
                                cancelled_recipient(recipient, reason=reason, now=now),
                                relationship_status=relationship_status,
                            )
                        )
                        touched[recipient.intent_id] = None
                event_id = f"relationship:{relationship_id}.inactive"
                for recipient in cancelled:
                    self._insert_audit_events(
                        connection,
                        (
                            NotificationAuditEvent(
                                event_id=f"audit:{event_id}:{recipient.recipient_id}",
                                action="recipient.cancel",
                                actor_person_id=actor_person_id,
                                intent_id=recipient.intent_id,
                                recipient_id=recipient.recipient_id,
                                payload={
                                    "relationship_id": relationship_id,
                                    "status": relationship_status,
                                    "reason": reason,
                                },
                                created_at=now,
                            ),
                        ),
                    )
                if cancelled:
                    self._insert_outbox_events(
                        connection,
                        (
                            NotificationOutboxEvent(
                                outbox_id=event_id,
                                event_id=event_id,
                                topic="notification.relationship.inactive",
                                payload={
                                    "relationship_id": relationship_id,
                                    "status": relationship_status,
                                    "reason": reason,
                                    "recipient_ids": [
                                        item.recipient_id for item in cancelled
                                    ],
                                },
                                created_at=now,
                            ),
                        ),
                    )
                for intent_id in list(touched):
                    touched[intent_id] = self._derive_intent_sync(
                        connection, intent_id, now
                    )
                return (tuple(cancelled), tuple(touched.values()))

    def _derive_intent_sync(
        self, connection: sqlite3.Connection, intent_id: str, now: datetime
    ) -> NotificationIntent | None:
        from services.notification.domain import derive_intent_status

        intent_row = connection.execute(
            "SELECT * FROM notification_intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if intent_row is None:
            return None
        intent = _intent_from_row(intent_row)
        rows = connection.execute(
            "SELECT * FROM notification_recipients"
            " WHERE intent_id = ? ORDER BY created_at",
            (intent_id,),
        ).fetchall()
        derived = derive_intent_status(tuple(_recipient_from_row(row) for row in rows))
        if derived != intent.status:
            connection.execute(
                "UPDATE notification_intents"
                " SET status = ?, delivered_at = ?, updated_at = ?"
                " WHERE intent_id = ?",
                (
                    derived,
                    _iso(now) if derived == "delivered" else None,
                    _iso(now),
                    intent_id,
                ),
            )
            intent = _intent_status_update(intent, derived, now)
            if derived in ("delivered", "dead_lettered", "cancelled"):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_outbox (
                        outbox_id, event_id, topic, payload_json, status,
                        attempts, locked_until, last_error_code, created_at,
                        delivered_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL, ?)
                    """,
                    (
                        f"{intent.idempotency_key}:intent.{derived}",
                        f"{intent.idempotency_key}:intent.{derived}",
                        f"notification.intent.{derived}",
                        json.dumps(intent.to_dict(), sort_keys=True),
                        _iso(now),
                        _iso(now),
                    ),
                )
        return intent

    def _insert_audit_events(
        self, connection: sqlite3.Connection, events: tuple[NotificationAuditEvent, ...]
    ) -> None:
        for event in events:
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_audit_events (
                    event_id, action, actor_person_id, intent_id, recipient_id,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.action,
                    event.actor_person_id,
                    event.intent_id,
                    event.recipient_id,
                    json.dumps(event.payload, sort_keys=True),
                    _iso(event.created_at),
                ),
            )

    def _insert_outbox_events(
        self, connection: sqlite3.Connection, events: tuple[NotificationOutboxEvent, ...]
    ) -> None:
        for event in events:
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_outbox (
                    outbox_id, event_id, topic, payload_json, status,
                    attempts, locked_until, last_error_code, created_at,
                    delivered_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL, ?)
                """,
                (
                    event.outbox_id,
                    event.event_id,
                    event.topic,
                    json.dumps(event.payload, sort_keys=True),
                    _iso(event.created_at),
                    _iso(event.created_at),
                ),
            )
    async def get_recipient(
        self, recipient_id: str, *, person_id: str
    ) -> RecipientBinding | None:
        connection = self._ready()
        with self._lock:
            row = connection.execute(
                "SELECT * FROM notification_recipients"
                " WHERE recipient_id = ? AND person_id = ?",
                (recipient_id, person_id),
            ).fetchone()
            return _recipient_from_row(row) if row is not None else None

    async def get_recipient_for_worker(
        self, recipient_id: str
    ) -> RecipientBinding | None:
        connection = self._ready()
        with self._lock:
            row = connection.execute(
                "SELECT * FROM notification_recipients WHERE recipient_id = ?",
                (recipient_id,),
            ).fetchone()
            return _recipient_from_row(row) if row is not None else None

    async def list_recipients(
        self, intent_id: str, *, person_id: str
    ) -> tuple[RecipientBinding, ...]:
        connection = self._ready()
        with self._lock:
            rows = connection.execute(
                "SELECT * FROM notification_recipients"
                " WHERE intent_id = ? AND person_id = ? ORDER BY created_at",
                (intent_id, person_id),
            ).fetchall()
            return tuple(_recipient_from_row(row) for row in rows)

    async def list_recipients_for_worker(
        self, intent_id: str
    ) -> tuple[RecipientBinding, ...]:
        connection = self._ready()
        with self._lock:
            rows = connection.execute(
                "SELECT * FROM notification_recipients WHERE intent_id = ? "
                "ORDER BY created_at",
                (intent_id,),
            ).fetchall()
            return tuple(_recipient_from_row(row) for row in rows)

    async def list_recipients_by_intent_subject(
        self, intent_id: str, *, subject_person_id: str
    ) -> tuple[RecipientBinding, ...]:
        connection = self._ready()
        with self._lock:
            owner = connection.execute(
                "SELECT subject_person_id FROM notification_intents"
                " WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if owner is None or owner["subject_person_id"] != subject_person_id:
                return ()
            rows = connection.execute(
                "SELECT * FROM notification_recipients WHERE intent_id = ? "
                "ORDER BY created_at",
                (intent_id,),
            ).fetchall()
            return tuple(_recipient_from_row(row) for row in rows)

    async def list_recipients_by_relationship(
        self, relationship_id: str
    ) -> tuple[RecipientBinding, ...]:
        connection = self._ready()
        with self._lock:
            rows = connection.execute(
                "SELECT * FROM notification_recipients "
                "WHERE relationship_id = ? ORDER BY created_at",
                (relationship_id,),
            ).fetchall()
            return tuple(_recipient_from_row(row) for row in rows)

    async def list_due_recipients(
        self, now: datetime, limit: int
    ) -> tuple[RecipientBinding, ...]:
        connection = self._ready()
        now_iso = _iso(now)
        with self._lock:
            rows = connection.execute(
                """
                SELECT * FROM notification_recipients
                WHERE status IN ('pending', 'failed', 'in_progress')
                  AND status <> 'cancelled'
                  AND relationship_status = 'active'
                  AND (valid_until IS NULL OR valid_until > ?)
                  AND valid_from <= ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM notification_delivery_attempts a
                      WHERE a.recipient_id = notification_recipients.recipient_id
                        AND a.status = 'uncertain'
                  )
                  AND (
                      status IN ('pending', 'failed')
                      OR (status = 'in_progress' AND leased_until <= ?)
                  )
                ORDER BY created_at
                LIMIT ?
                """,
                (now_iso, now_iso, now_iso, now_iso, limit),
            ).fetchall()
            return tuple(_recipient_from_row(row) for row in rows)

    async def list_uncertain_attempts(
        self, limit: int
    ) -> tuple[DeliveryAttempt, ...]:
        connection = self._ready()
        with self._lock:
            rows = connection.execute(
                "SELECT * FROM notification_delivery_attempts"
                " WHERE status = 'uncertain'"
                " ORDER BY started_at LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(_attempt_from_row(row) for row in rows)

    async def claim_recipient(
        self,
        recipient_id: str,
        attempt: DeliveryAttempt,
        now: datetime,
    ) -> tuple[RecipientBinding, DeliveryAttempt] | None:
        connection = self._ready()
        now_iso = _iso(now)
        with self._lock:
            try:
                with connection:
                    cursor = connection.execute(
                        """
                        UPDATE notification_recipients
                        SET status = 'in_progress',
                            attempts = ?,
                            fencing_token = ?,
                            leased_until = ?,
                            next_attempt_at = NULL,
                            updated_at = ?
                        WHERE recipient_id = ?
                          AND status IN ('pending', 'failed')
                          AND status <> 'cancelled'
                          AND relationship_status = 'active'
                          AND (valid_until IS NULL OR valid_until > ?)
                          AND valid_from <= ?
                          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                          AND attempts = ?
                        """,
                        (
                            attempt.attempt_number,
                            attempt.fencing_token,
                            _iso(attempt.leased_until),
                            now_iso,
                            recipient_id,
                            now_iso,
                            now_iso,
                            now_iso,
                            attempt.attempt_number - 1,
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor2 = connection.execute(
                            """
                            UPDATE notification_recipients
                            SET status = 'in_progress',
                                attempts = ?,
                                fencing_token = ?,
                                leased_until = ?,
                                next_attempt_at = NULL,
                                updated_at = ?
                            WHERE recipient_id = ?
                              AND status = 'in_progress'
                              AND leased_until <= ?
                              AND relationship_status = 'active'
                              AND attempts = ?
                            """,
                            (
                                attempt.attempt_number,
                                attempt.fencing_token,
                                _iso(attempt.leased_until),
                                now_iso,
                                recipient_id,
                                now_iso,
                                attempt.attempt_number - 1,
                            ),
                        )
                        if cursor2.rowcount == 0:
                            return None
                    connection.execute(
                        """
                        INSERT INTO notification_delivery_attempts (
                            attempt_id, intent_id, recipient_id, attempt_number,
                            channel, logical_delivery_key, status, fencing_token,
                            leased_until, started_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'leased', ?, ?, ?)
                        """,
                        (
                            attempt.attempt_id,
                            attempt.intent_id,
                            attempt.recipient_id,
                            attempt.attempt_number,
                            attempt.channel,
                            attempt.logical_delivery_key,
                            attempt.fencing_token,
                            _iso(attempt.leased_until),
                            _iso(attempt.started_at),
                        ),
                    )
            except sqlite3.IntegrityError:
                return None
            row = connection.execute(
                "SELECT * FROM notification_recipients WHERE recipient_id = ?",
                (recipient_id,),
            ).fetchone()
            if row is None:
                return None
            return (_recipient_from_row(row), attempt)

    async def save_attempt(self, attempt: DeliveryAttempt) -> DeliveryAttempt:
        connection = self._ready()
        with self._lock:
            with connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO notification_delivery_attempts (
                        attempt_id, intent_id, recipient_id, attempt_number,
                        channel, status, fencing_token, leased_until, started_at,
                        finished_at, error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.attempt_id,
                        attempt.intent_id,
                        attempt.recipient_id,
                        attempt.attempt_number,
                        attempt.channel,
                        attempt.status,
                        attempt.fencing_token,
                        _iso(attempt.leased_until),
                        _iso(attempt.started_at),
                        _iso(attempt.finished_at) if attempt.finished_at else None,
                        attempt.error_code,
                    ),
                )
            return attempt

    async def get_attempt(self, attempt_id: str) -> DeliveryAttempt | None:
        connection = self._ready()
        with self._lock:
            row = connection.execute(
                "SELECT * FROM notification_delivery_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            return _attempt_from_row(row) if row is not None else None

    async def list_attempts(
        self, recipient_id: str, *, person_id: str
    ) -> tuple[DeliveryAttempt, ...]:
        connection = self._ready()
        with self._lock:
            owner = connection.execute(
                "SELECT person_id FROM notification_recipients WHERE recipient_id = ?",
                (recipient_id,),
            ).fetchone()
            if owner is None or owner["person_id"] != person_id:
                return ()
            rows = connection.execute(
                "SELECT * FROM notification_delivery_attempts "
                "WHERE recipient_id = ? ORDER BY attempt_number",
                (recipient_id,),
            ).fetchall()
            return tuple(_attempt_from_row(row) for row in rows)

    async def save_receipt(self, receipt: DeliveryReceipt) -> DeliveryReceipt:
        connection = self._ready()
        with self._lock:
            existing = connection.execute(
                "SELECT * FROM notification_receipts WHERE attempt_id = ?",
                (receipt.attempt_id,),
            ).fetchone()
            if existing is not None:
                return DeliveryReceipt(
                    receipt_id=existing["receipt_id"],
                    intent_id=existing["intent_id"],
                    recipient_id=existing["recipient_id"],
                    attempt_id=existing["attempt_id"],
                    channel=existing["channel"],
                    channel_receipt_id=existing["channel_receipt_id"],
                    delivered_at=_required_iso(existing["delivered_at"]),
                )
            with connection:
                connection.execute(
                    """
                    INSERT INTO notification_receipts (
                        receipt_id, intent_id, recipient_id, attempt_id, channel,
                        channel_receipt_id, delivered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.receipt_id,
                        receipt.intent_id,
                        receipt.recipient_id,
                        receipt.attempt_id,
                        receipt.channel,
                        receipt.channel_receipt_id,
                        _iso(receipt.delivered_at),
                    ),
                )
            return receipt

    async def complete_attempt_atomically(
        self,
        *,
        attempt_id: str,
        fencing_token: str,
        recipient: RecipientBinding,
        finished_attempt: DeliveryAttempt,
        receipt: DeliveryReceipt | None,
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> tuple[RecipientBinding, NotificationIntent | None] | None:
        connection = self._ready()
        with self._lock:
            with connection:
                leased = connection.execute(
                    "SELECT attempt_id, intent_id, recipient_id, attempt_number"
                    " FROM notification_delivery_attempts"
                    " WHERE attempt_id = ? AND status IN ('leased', 'uncertain')"
                    "   AND fencing_token = ?",
                    (attempt_id, fencing_token),
                ).fetchone()
                if leased is None:
                    return None
                if (
                    leased["intent_id"] != recipient.intent_id
                    or leased["recipient_id"] != recipient.recipient_id
                    or leased["attempt_number"] != finished_attempt.attempt_number
                ):
                    return None
                cursor = connection.execute(
                    """
                    UPDATE notification_recipients
                    SET relationship_status = ?, channel_index = ?, status = ?,
                        attempts = ?, next_attempt_at = ?, leased_until = NULL,
                        fencing_token = NULL, last_error_code = ?,
                        delivered_at = ?, delivered_channel = ?,
                        cancelled_reason = ?, cancelled_at = ?, updated_at = ?
                    WHERE recipient_id = ?
                      AND fencing_token = ?
                      AND status = 'in_progress'
                      AND attempts = ?
                    """,
                    (
                        recipient.relationship_status,
                        recipient.channel_index,
                        recipient.status,
                        recipient.attempts,
                        _iso(recipient.next_attempt_at),
                        recipient.last_error_code,
                        _iso(recipient.delivered_at),
                        recipient.delivered_channel,
                        recipient.cancelled_reason,
                        _iso(recipient.cancelled_at),
                        _iso(recipient.updated_at),
                        recipient.recipient_id,
                        fencing_token,
                        finished_attempt.attempt_number,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                connection.execute(
                    "UPDATE notification_delivery_attempts"
                    " SET status = ?, finished_at = ?, error_code = ?"
                    " WHERE attempt_id = ?",
                    (
                        finished_attempt.status,
                        _iso(finished_attempt.finished_at),
                        finished_attempt.error_code,
                        finished_attempt.attempt_id,
                    ),
                )
                if receipt is not None:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO notification_receipts (
                            receipt_id, intent_id, recipient_id, attempt_id, channel,
                            channel_receipt_id, delivered_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            receipt.receipt_id,
                            receipt.intent_id,
                            receipt.recipient_id,
                            receipt.attempt_id,
                            receipt.channel,
                            receipt.channel_receipt_id,
                            _iso(receipt.delivered_at),
                        ),
                    )
                for event in audit:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO notification_audit_events (
                            event_id, action, actor_person_id, intent_id, recipient_id,
                            payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_id,
                            event.action,
                            event.actor_person_id,
                            event.intent_id,
                            event.recipient_id,
                            json.dumps(event.payload, sort_keys=True),
                            _iso(event.created_at),
                        ),
                    )
                for out_event in outbox:
                    connection.execute(
                        """
                        INSERT INTO notification_outbox (
                            outbox_id, event_id, topic, payload_json, status, attempts,
                            locked_until, last_error_code, created_at, delivered_at,
                            updated_at
                        ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL, ?)
                        """,
                        (
                            out_event.outbox_id,
                            out_event.event_id,
                            out_event.topic,
                            json.dumps(out_event.payload, sort_keys=True),
                            _iso(out_event.created_at),
                            _iso(out_event.created_at),
                        ),
                    )
                # Intent status derived inside the same transaction.
                from services.notification.domain import derive_intent_status

                intent_row = connection.execute(
                    "SELECT * FROM notification_intents WHERE intent_id = ?",
                    (recipient.intent_id,),
                ).fetchone()
                intent = _intent_from_row(intent_row) if intent_row is not None else None
                if intent is not None:
                    rows = connection.execute(
                        "SELECT * FROM notification_recipients"
                        " WHERE intent_id = ? ORDER BY created_at",
                        (recipient.intent_id,),
                    ).fetchall()
                    derived = derive_intent_status(
                        tuple(_recipient_from_row(row) for row in rows)
                    )
                    if derived != intent.status:
                        connection.execute(
                            "UPDATE notification_intents"
                            " SET status = ?, delivered_at = ?, updated_at = ?"
                            " WHERE intent_id = ?",
                            (
                                derived,
                                _iso(now) if derived == "delivered" else None,
                                _iso(now),
                                intent.intent_id,
                            ),
                        )
                        intent = _intent_status_update(intent, derived, now)
                        if derived in ("delivered", "dead_lettered", "cancelled"):
                            connection.execute(
                                """
                                INSERT OR IGNORE INTO notification_outbox (
                                    outbox_id, event_id, topic, payload_json, status,
                                    attempts, locked_until, last_error_code, created_at,
                                    delivered_at, updated_at
                                ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL,
                                          ?, NULL, ?)
                                """,
                                (
                                    uuid.uuid4().hex,
                                    f"{intent.idempotency_key}:intent.{derived}",
                                    f"notification.intent.{derived}",
                                    json.dumps(intent.to_dict(), sort_keys=True),
                                    _iso(now),
                                    _iso(now),
                                ),
                            )
                return (recipient, intent)

    async def get_receipt_by_attempt(self, attempt_id: str) -> DeliveryReceipt | None:
        connection = self._ready()
        with self._lock:
            row = connection.execute(
                "SELECT * FROM notification_receipts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                return None
            return DeliveryReceipt(
                receipt_id=row["receipt_id"],
                intent_id=row["intent_id"],
                recipient_id=row["recipient_id"],
                attempt_id=row["attempt_id"],
                channel=row["channel"],
                channel_receipt_id=row["channel_receipt_id"],
                delivered_at=_required_iso(row["delivered_at"]),
            )

    async def list_receipts(
        self, intent_id: str, *, person_id: str
    ) -> tuple[DeliveryReceipt, ...]:
        connection = self._ready()
        with self._lock:
            owner = connection.execute(
                "SELECT subject_person_id FROM notification_intents"
                " WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if owner is None or owner["subject_person_id"] != person_id:
                return ()
            rows = connection.execute(
                "SELECT * FROM notification_receipts WHERE intent_id = ? "
                "ORDER BY delivered_at",
                (intent_id,),
            ).fetchall()
            return tuple(
                DeliveryReceipt(
                    receipt_id=row["receipt_id"],
                    intent_id=row["intent_id"],
                    recipient_id=row["recipient_id"],
                    attempt_id=row["attempt_id"],
                    channel=row["channel"],
                    channel_receipt_id=row["channel_receipt_id"],
                    delivered_at=_required_iso(row["delivered_at"]),
                )
                for row in rows
            )

    async def scan_intents(
        self, statuses: tuple[IntentStatus, ...] | None = None
    ) -> tuple[NotificationIntent, ...]:
        connection = self._ready()
        with self._lock:
            if statuses is None:
                rows = connection.execute(
                    "SELECT * FROM notification_intents ORDER BY created_at"
                ).fetchall()
            else:
                placeholders = ",".join("?" for _ in statuses)
                rows = connection.execute(
                    f"SELECT * FROM notification_intents "
                    f"WHERE status IN ({placeholders}) ORDER BY created_at",
                    tuple(statuses),
                ).fetchall()
            return tuple(_intent_from_row(row) for row in rows)

    async def append_audit(self, event: NotificationAuditEvent) -> None:
        connection = self._ready()
        with self._lock:
            with connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_audit_events (
                        event_id, action, actor_person_id, intent_id, recipient_id,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.action,
                        event.actor_person_id,
                        event.intent_id,
                        event.recipient_id,
                        json.dumps(event.payload, sort_keys=True, ensure_ascii=False),
                        _iso(event.created_at),
                    ),
                )

    async def enqueue_outbox(self, event: NotificationOutboxEvent) -> None:
        connection = self._ready()
        with self._lock:
            with connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_outbox (
                        outbox_id, event_id, topic, payload_json, status, attempts,
                        locked_until, last_error_code, created_at, delivered_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL, ?)
                    """,
                    (
                        event.outbox_id,
                        event.event_id,
                        event.topic,
                        json.dumps(event.payload, sort_keys=True, ensure_ascii=False),
                        _iso(event.created_at),
                        _iso(event.created_at),
                    ),
                )

    # -- test helpers ------------------------------------------------------

    def audit_events(self) -> tuple[NotificationAuditEvent, ...]:
        connection = self._ready()
        with self._lock:
            rows = connection.execute(
                "SELECT * FROM notification_audit_events ORDER BY created_at"
            ).fetchall()
            return tuple(
                NotificationAuditEvent(
                    event_id=row["event_id"],
                    action=row["action"],
                    actor_person_id=row["actor_person_id"],
                    intent_id=row["intent_id"],
                    recipient_id=row["recipient_id"],
                    payload=json.loads(row["payload_json"]),
                    created_at=_required_iso(row["created_at"]),
                )
                for row in rows
            )

    def outbox_events(self) -> tuple[NotificationOutboxEvent, ...]:
        connection = self._ready()
        with self._lock:
            rows = connection.execute(
                "SELECT * FROM notification_outbox ORDER BY created_at"
            ).fetchall()
            return tuple(
                NotificationOutboxEvent(
                    outbox_id=row["outbox_id"],
                    event_id=row["event_id"],
                    topic=row["topic"],
                    payload=json.loads(row["payload_json"]),
                    created_at=_required_iso(row["created_at"]),
                )
                for row in rows
            )


__all__ = ["SqliteNotificationStore"]
