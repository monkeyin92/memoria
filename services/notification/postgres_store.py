"""PostgreSQL / FORCE-RLS persistence for the notification domain.

Production authority (section 11.7 / PR-17).  The schema is shipped in
``postgres_schema.sql``: constraints mirror the domain rules, every table is
FORCE RLS, attempts/receipts are append-only and audit / outbox rows are
written by the same adapter.  Claims run inside a transaction guarded by a
``WHERE`` clause so concurrent workers cannot double-lease a recipient.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

import asyncpg

from services.notification.domain import (
    CancelReason,
    DeliveryAttempt,
    DeliveryReceipt,
    IntentStatus,
    NotificationConflictError,
    NotificationIntent,
    RecipientBinding,
)
from services.notification.repository import (
    NotificationAuditEvent,
    NotificationOutboxEvent,
)


class PostgresNotificationStoreContextError(RuntimeError):
    """Required transaction-level app context was missing or empty."""

_SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")
_RLS_TABLES = (
    "notification_intents",
    "notification_recipients",
    "notification_delivery_attempts",
    "notification_receipts",
    "notification_audit_events",
    "notification_outbox",
)

_ROLE_NAMES = {
    "api": "memoria_notification_api",
    "worker": "memoria_notification_worker",
}


def _jsonb_list(value: object) -> list[str]:
    """asyncpg may return JSONB as a decoded Python list OR as a str (when
    the connection codec did not decode it); both shapes must decode to a
    real list (fifth review)."""
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) for item in decoded
    ):
        raise ValueError("expected a JSON array of strings")
    return decoded


def _jsonb_dict(value: object) -> dict[str, object]:
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise ValueError("expected a JSON object")
    return decoded
_REQUIRED_TABLES = frozenset(
    {
        "notification_intents",
        "notification_recipients",
        "notification_delivery_attempts",
        "notification_receipts",
        "notification_audit_events",
        "notification_outbox",
    }
)


def _timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


async def _insert_audit_events(
    connection: asyncpg.Connection,
    events: tuple[NotificationAuditEvent, ...],
) -> None:
    """Idempotent audit append.  ``ON CONFLICT (event_id) DO NOTHING`` is
    NOT usable here: PostgreSQL raises 'new row violates row-level security'
    when the conflict-detection read is hidden by the worker-only SELECT
    policy, so the unique violation is swallowed instead (the stable event
    id makes replays no-ops and the RLS policy stays INSERT-only)."""
    for event in events:
        try:
            await connection.execute(
                """
                INSERT INTO notification_audit_events (
                    event_id, action, actor_person_id, intent_id, recipient_id,
                    payload_json, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                event.event_id,
                event.action,
                event.actor_person_id,
                event.intent_id,
                event.recipient_id,
                json.dumps(event.payload, sort_keys=True, ensure_ascii=False),
                _timestamp(event.created_at),
            )
        except asyncpg.UniqueViolationError:
            pass


async def _insert_outbox_events(
    connection: asyncpg.Connection,
    events: tuple[NotificationOutboxEvent, ...],
) -> None:
    """Idempotent outbox append with the same RLS-safe unique-violation
    swallowing as :func:`_insert_audit_events`; the row keeps the current
    subject context so the API INSERT policy (own-subject rows) holds."""
    for event in events:
        try:
            await connection.execute(
                """
                INSERT INTO notification_outbox (
                    outbox_id, event_id, topic, actor_person_id,
                    subject_person_id, payload_json,
                    status, attempts, locked_until, last_error_code, created_at,
                    delivered_at, updated_at
                ) VALUES ($1, $2, $3,
                          current_setting('app.authenticated_actor', true),
                          current_setting('app.notification.subject_person_id', true),
                          $4, 'pending', 0, NULL, NULL, $5, NULL, $5)
                """,
                event.outbox_id,
                event.event_id,
                event.topic,
                json.dumps(event.payload, sort_keys=True, ensure_ascii=False),
                _timestamp(event.created_at),
            )
        except asyncpg.UniqueViolationError:
            pass


def _from_db(value: object) -> datetime | None:
    if value is None:
        return None
    return cast(datetime, value).astimezone(UTC)


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


def _intent_from_row(row: asyncpg.Record) -> NotificationIntent:
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
        valid_until=cast(datetime, row["valid_until"]),
        template_key=row["template_key"],
        template_params=cast(dict[str, str], _jsonb_dict(row["template_params_json"])),
        reason_code=row["reason_code"],
        script_version=row["script_version"],
        occurred_at=_from_db(row["occurred_at"]) or datetime.now(UTC),
        status=row["status"],
        created_at=_from_db(row["created_at"]) or datetime.now(UTC),
        updated_at=_from_db(row["updated_at"]) or datetime.now(UTC),
        cancelled_reason=row["cancelled_reason"],
        cancelled_at=_from_db(row["cancelled_at"]),
        delivered_at=_from_db(row["delivered_at"]),
    )


def _recipient_from_row(row: asyncpg.Record) -> RecipientBinding:
    channels = tuple(_jsonb_list(row["channels_json"]))
    return RecipientBinding(
        recipient_id=row["recipient_id"],
        intent_id=row["intent_id"],
        person_id=row["person_id"],
        role=row["role"],
        relationship_id=row["relationship_id"],
        relationship_status=row["relationship_status"],
        relationship_snapshot_id=row["relationship_snapshot_id"],
        relationship_revision=row["relationship_revision"],
        channels=channels,  # type: ignore[arg-type]
        channel_index=row["channel_index"],
        status=row["status"],
        attempts=row["attempts"],
        max_retries=row["max_retries"],
        next_attempt_at=_from_db(row["next_attempt_at"]),
        leased_until=_from_db(row["leased_until"]),
        fencing_token=row["fencing_token"],
        last_error_code=row["last_error_code"],
        valid_from=_from_db(row["valid_from"]) or datetime.now(UTC),
        valid_until=_from_db(row["valid_until"]),
        created_at=_from_db(row["created_at"]) or datetime.now(UTC),
        updated_at=_from_db(row["updated_at"]) or datetime.now(UTC),
        delivered_at=_from_db(row["delivered_at"]),
        delivered_channel=row["delivered_channel"],
        cancelled_reason=row["cancelled_reason"],
        cancelled_at=_from_db(row["cancelled_at"]),
    )


def _attempt_from_row(row: asyncpg.Record) -> DeliveryAttempt:
    return DeliveryAttempt(
        attempt_id=row["attempt_id"],
        intent_id=row["intent_id"],
        recipient_id=row["recipient_id"],
        attempt_number=row["attempt_number"],
        channel=row["channel"],
        logical_delivery_key=row["logical_delivery_key"],
        status=row["status"],
        fencing_token=row["fencing_token"],
        leased_until=_from_db(row["leased_until"]) or datetime.now(UTC),
        started_at=_from_db(row["started_at"]) or datetime.now(UTC),
        finished_at=_from_db(row["finished_at"]),
        error_code=row["error_code"],
    )


class PostgresNotificationStore:
    """PostgreSQL implementation of the ``NotificationStore`` protocol."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._expected_role: Literal["api", "worker"] = "api"
        self._pool: asyncpg.Pool | None = None

    def with_role(self, role: Literal["api", "worker"]) -> PostgresNotificationStore:
        """Declare which database role this adapter connects as (P0-2):
        command authority comes from the real role, never from GUCs."""
        self._expected_role = role
        return self

    def _require_worker(self) -> None:
        if self._expected_role != "worker":
            raise RuntimeError(
                "operation requires the memoria_notification_worker database "
                "role; the API role must not execute worker commands (P0-2)"
            )

    def _require_api(self) -> None:
        if self._expected_role != "api":
            raise RuntimeError(
                "operation is subject-API only; the worker adapter must not "
                "impersonate the subject API path (P0-2)"
            )

    @staticmethod
    async def _authenticated_scope(
        connection: asyncpg.Connection,
        *,
        actor_person_id: str | None,
        subject_person_id: str | None,
    ) -> None:
        """Bind the independent authenticated principal for one transaction.

        These values are deliberately separate from the business payload.
        PostgreSQL RLS compares them with the immutable actor/subject facts;
        missing or empty context is rejected before any API write.
        """
        for name, value in (
            ("actor", actor_person_id),
            ("subject", subject_person_id),
        ):
            if value is None or not value.strip():
                raise PostgresNotificationStoreContextError(
                    f"app.authenticated_{name} is required (fail closed)"
                )
            await connection.execute(
                "SELECT set_config('app.authenticated_' || $1, $2, true)",
                name,
                value,
            )

    @staticmethod
    async def _scope(
        connection: asyncpg.Connection,
        **values: str | None,
    ) -> None:
        """Set transaction-local ``app.notification.*`` context values
        (fail closed on empty values).  ``set_config(..., true)`` keeps the
        context local to the current transaction; every operation runs
        inside one."""
        for key, value in values.items():
            if value is None:
                continue
            if not value.strip():
                raise PostgresNotificationStoreContextError(
                    f"app.notification.{key} must not be empty (fail closed)"
                )
            await connection.execute(
                "SELECT set_config('app.notification.' || $1, $2, true)",
                key,
                value,
            )

    async def initialize(
        self,
        *,
        bootstrap_dsn: str | None = None,
        app_role_password: str | None = None,
    ) -> None:
        if self._pool is not None:
            return
        if bootstrap_dsn is not None:
            await self._bootstrap(
                bootstrap_dsn,
                app_role_password=app_role_password,
            )
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
        assert self._pool is not None
        async with self._pool.acquire() as connection:
            await connection.execute("SET application_name = 'memoria-notification'")
            await self._verify_runtime_security(connection)
            present = {
                str(row["tablename"])
                for row in await connection.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            }
        missing = _REQUIRED_TABLES - present
        if missing:
            raise RuntimeError(
                f"notification schema tables missing: {sorted(missing)}"
            )

    async def _bootstrap(
        self,
        bootstrap_dsn: str,
        *,
        app_role_password: str | None,
    ) -> None:
        """Schema/bootstrap runs as the table owner (deployment step); the
        production app never connects as the owner (P0-2)."""
        app_role = urlsplit(self._dsn).username or ""
        if not app_role:
            raise PostgresNotificationStoreContextError(
                "app DSN must carry a memoria_notification role as its user"
            )
        bootstrap = await asyncpg.connect(bootstrap_dsn)
        try:
            # Bootstrap/migration runs as the schema owner: row_security is
            # disabled so quarantine audits can be written even though the
            # audit policies only target the app roles.
            await bootstrap.execute("SET row_security = off")
            for role_name in (_ROLE_NAMES["api"], _ROLE_NAMES["worker"]):
                role_exists = await bootstrap.fetchval(
                    "SELECT 1 FROM pg_roles WHERE rolname = $1", role_name
                )
                if role_exists is None:
                    create_stmt = await bootstrap.fetchval(
                        "SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER "
                        "NOBYPASSRLS', $1::text)",
                        role_name,
                    )
                    await bootstrap.execute(create_stmt)
                if app_role_password is not None:
                    alter_stmt = await bootstrap.fetchval(
                        "SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER "
                        "NOBYPASSRLS PASSWORD %L', $1::text, $2::text)",
                        role_name,
                        app_role_password,
                    )
                    await bootstrap.execute(alter_stmt)
            if app_role not in (_ROLE_NAMES["api"], _ROLE_NAMES["worker"]):
                raise PostgresNotificationStoreContextError(
                    f"app DSN user {app_role!r} is not a notification role"
                )
            schema = _SCHEMA_PATH.read_text(encoding="utf-8")
            await bootstrap.execute(schema)
            # Additive migrations for databases created before the
            # fence-snapshot columns existed.  CREATE TABLE IF NOT EXISTS
            # never adds columns to an existing table, so production
            # upgrades must ALTER explicitly; every statement is idempotent
            # and therefore safe to run on every bootstrap.
            await bootstrap.execute(
                """
                ALTER TABLE notification_intents
                    ADD COLUMN IF NOT EXISTS device_id TEXT NOT NULL DEFAULT ''
                        CHECK (char_length(device_id) BETWEEN 0 AND 128)
                """
            )
            await bootstrap.execute(
                """
                ALTER TABLE notification_intents
                    ADD COLUMN IF NOT EXISTS subject_revision INTEGER NOT NULL
                        DEFAULT 0 CHECK (subject_revision >= 0)
                """
            )
            await bootstrap.execute(
                """
                ALTER TABLE notification_intents
                    ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ
                """
            )
            # Legacy NULL fence windows fail closed: the intent is CANCELLED
            # (quarantined) with an audit trail - the NULL window is never
            # treated as unlimited and the intent can never be dispatched.
            # The core-immutable trigger is DISABLED for this controlled
            # owner migration ONLY (re-enabled in the same transaction; any
            # failure rolls the whole bootstrap back including the
            # disable, so the runtime guard can never be lost or bypassed
            # by ordinary traffic).
            await bootstrap.execute(
                "ALTER TABLE notification_intents"
                " DISABLE TRIGGER notification_intent_core_immutable"
            )
            legacy_null = await bootstrap.fetch(
                "SELECT intent_id, created_at FROM notification_intents"
                " WHERE valid_until IS NULL"
            )
            try:
                for row in legacy_null:
                    await bootstrap.execute(
                        "UPDATE notification_intents"
                        " SET valid_until = created_at, status = 'cancelled',"
                        "     cancelled_reason = 'authorization_expired',"
                        "     cancelled_at = created_at, updated_at = created_at"
                        " WHERE intent_id = $1",
                        row["intent_id"],
                    )
                    await bootstrap.execute(
                        "INSERT INTO notification_audit_events ("
                        " event_id, action, actor_person_id, intent_id,"
                        " recipient_id, payload_json, created_at"
                        ") VALUES ($1, 'intent.quarantine', NULL, $2, NULL,"
                        " $3, $4)"
                        " ON CONFLICT (event_id) DO NOTHING",
                        f"audit:{row['intent_id']}:quarantine",
                        row["intent_id"],
                        json.dumps({"reason": "legacy_null_fence_window"}),
                        row["created_at"],
                    )
            finally:
                # Same-transaction re-enable: if the UPDATE failed the whole
                # bootstrap transaction rolls back (including this enable
                # and the disable), so the immutable guard always exists.
                await bootstrap.execute(
                    "ALTER TABLE notification_intents"
                    " ENABLE TRIGGER notification_intent_core_immutable"
                )
            # The fence window invariant is now enforced at the CATALOG
            # level: any future NULL write is rejected (fail closed), not
            # just migrated away.
            await bootstrap.execute(
                """
                ALTER TABLE notification_intents
                    ALTER COLUMN valid_until SET NOT NULL
                """
            )
            # Safe cleanup of the erroneous relationship columns that a
            # previous DDL placed on notification_intents: they belong on
            # notification_recipients (added below).  IF EXISTS keeps this
            # a no-op for databases that never had them.
            await bootstrap.execute(
                """
                ALTER TABLE notification_intents
                    DROP COLUMN IF EXISTS relationship_snapshot_id
                """
            )
            await bootstrap.execute(
                """
                ALTER TABLE notification_intents
                    DROP COLUMN IF EXISTS relationship_revision
                """
            )
            await bootstrap.execute(
                """
                ALTER TABLE notification_recipients
                    ADD COLUMN IF NOT EXISTS relationship_snapshot_id TEXT
                        NOT NULL DEFAULT ''
                        CHECK (char_length(relationship_snapshot_id)
                               BETWEEN 0 AND 128)
                """
            )
            await bootstrap.execute(
                """
                ALTER TABLE notification_recipients
                    ADD COLUMN IF NOT EXISTS relationship_revision INTEGER
                        NOT NULL DEFAULT 1 CHECK (relationship_revision >= 1)
                """
            )
            # Stable logical delivery key (P0): every retry of the same
            # recipient+channel reuses it; the attempt_number is NEVER part
            # of the provider idempotency key.  Legacy rows are backfilled
            # from intent/recipient/channel.
            await bootstrap.execute(
                """
                ALTER TABLE notification_delivery_attempts
                    ADD COLUMN IF NOT EXISTS logical_delivery_key TEXT
                        NOT NULL DEFAULT ''
                """
            )
            await bootstrap.execute(
                """
                UPDATE notification_delivery_attempts
                SET logical_delivery_key = intent_id || ':' || recipient_id
                    || ':' || channel
                WHERE logical_delivery_key = ''
                """
            )
            # Legacy status CHECK predates ``uncertain``: rebuild the
            # constraint (idempotent, fail closed - the key stays NOT NULL).
            await bootstrap.execute(
                """
                ALTER TABLE notification_delivery_attempts
                    DROP CONSTRAINT IF EXISTS
                    notification_delivery_attempts_status_check
                """
            )
            await bootstrap.execute(
                """
                ALTER TABLE notification_delivery_attempts
                    ADD CONSTRAINT notification_delivery_attempts_status_check
                    CHECK (status IN
                        ('leased', 'delivered', 'failed', 'uncertain'))
                """
            )
            await bootstrap.execute(
                """
                ALTER TABLE notification_delivery_attempts
                    ALTER COLUMN logical_delivery_key SET NOT NULL
                """
            )
        finally:
            await bootstrap.close()

    async def _verify_runtime_security(
        self, connection: asyncpg.Connection
    ) -> None:
        """Runtime verification, fail closed (P0-2): non-superuser,
        non-BYPASSRLS app role, FORCE RLS on every table, context-gated
        policies and no open pass-all policy."""
        row = await connection.fetchrow(
            "SELECT current_user AS usr,"
            " (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS su,"
            " (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)"
            " AS bypass,"
            " pg_has_role(current_user, $1, 'member') AS expected_role,"
            " pg_has_role(current_user, $2, 'member') AS is_worker",
            _ROLE_NAMES[self._expected_role],
            _ROLE_NAMES["worker"],
        )
        if row is None or bool(row["su"]) or bool(row["bypass"]):
            raise PostgresNotificationStoreContextError(
                "app connection must be a non-superuser, non-BYPASSRLS role; "
                "refusing to initialize"
            )
        if not bool(row["expected_role"]):
            raise PostgresNotificationStoreContextError(
                f"connection role {row['usr']} is not a member of "
                f"{_ROLE_NAMES[self._expected_role]}; refusing to initialize"
            )
        if self._expected_role == "api" and bool(row["is_worker"]):
            raise PostgresNotificationStoreContextError(
                "API connection must not hold worker role membership"
            )
        tables = await connection.fetch(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity"
            " FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])",
            list(_RLS_TABLES),
        )
        present = {str(t["relname"]) for t in tables}
        if present != set(_RLS_TABLES):
            raise PostgresNotificationStoreContextError(
                f"RLS tables missing: {sorted(set(_RLS_TABLES) - present)}"
            )
        if not all(
            bool(t["relrowsecurity"]) and bool(t["relforcerowsecurity"])
            for t in tables
        ):
            raise PostgresNotificationStoreContextError(
                "every notification table must have FORCE row-level security"
            )
        policies = await connection.fetch(
            "SELECT tablename, qual, with_check FROM pg_policies"
            " WHERE schemaname = 'public' AND tablename = ANY($1::text[])",
            list(_RLS_TABLES),
        )
        by_table: dict[str, list[asyncpg.Record]] = {}
        for policy in policies:
            by_table.setdefault(str(policy["tablename"]), []).append(policy)
            qual = policy["qual"]
            check = policy["with_check"]
            if qual is not None and str(qual).strip() == "true":
                raise PostgresNotificationStoreContextError(
                    f"open SELECT policy on {policy['tablename']}; refusing to initialize"
                )
            if check is not None and str(check).strip() == "true":
                raise PostgresNotificationStoreContextError(
                    f"open WITH CHECK policy on {policy['tablename']}; "
                    "refusing to initialize"
                )
            text = f"{qual or ''} {check or ''}"
            # P0-2/P0-1: a policy must reference the row-context GUC, the
            # real role membership (pg_has_role) or another RLS table
            # (indirect scope, e.g. outbox subject join into intents).  A
            # bare pass-all policy is rejected above.
            if (
                "app.notification." not in text
                and "app.authenticated_actor" not in text
                and "app.authenticated_subject" not in text
                and "pg_has_role" not in text
                and not any(table in text for table in _RLS_TABLES)
            ):
                raise PostgresNotificationStoreContextError(
                    f"policy on {policy['tablename']} does not reference the "
                    "app context or a real role"
                )
        for table in _RLS_TABLES:
            if not by_table.get(table):
                raise PostgresNotificationStoreContextError(
                    f"no RLS policy found on {table}; refusing to initialize"
                )
        async with connection.transaction():
            await self._authenticated_scope(
                connection,
                actor_person_id="__notification_readiness_actor__",
                subject_person_id="__notification_readiness_subject__",
            )
            await connection.execute(
                "SELECT set_config("
                "'app.notification.service_role', 'notification_worker', true)"
            )
            probe = await connection.fetchval(
                "SELECT current_setting('app.notification.service_role', true)"
            )
            if probe != "notification_worker":
                raise PostgresNotificationStoreContextError(
                    "transaction-level app context is not usable; refusing to initialize"
                )
            auth = await connection.fetchrow(
                "SELECT current_setting('app.authenticated_actor', true) AS actor, "
                "current_setting('app.authenticated_subject', true) AS subject"
            )
            if auth["actor"] != "__notification_readiness_actor__" or auth[
                "subject"
            ] != "__notification_readiness_subject__":
                raise PostgresNotificationStoreContextError(
                    "authenticated actor/subject transaction context is not usable; "
                    "refusing to initialize"
                )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _ready(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresNotificationStore.initialize() must run first")
        return self._pool

    async def save_intent(self, intent: NotificationIntent) -> NotificationIntent:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    subject_person_id=intent.subject_person_id,
                )
                try:
                    await connection.execute(
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
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                                  $12, $13, $14, $15, $16, $17, $18, $19, $20, $21,
                                  $22, $23, $24, $25, $26, $27)
                        ON CONFLICT (intent_id) DO UPDATE SET
                            status = EXCLUDED.status,
                            cancelled_reason = EXCLUDED.cancelled_reason,
                            cancelled_at = EXCLUDED.cancelled_at,
                            delivered_at = EXCLUDED.delivered_at,
                            updated_at = EXCLUDED.updated_at
                        """,
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
                        intent.valid_until,
                        intent.template_key,
                        json.dumps(
                            intent.template_params, sort_keys=True, ensure_ascii=False
                        ),
                        intent.reason_code,
                        intent.script_version,
                        _timestamp(intent.occurred_at),
                        intent.status,
                        intent.cancelled_reason,
                        intent.cancelled_at,
                        intent.delivered_at,
                        _timestamp(intent.created_at),
                        _timestamp(intent.updated_at),
                    )
                except asyncpg.UniqueViolationError as exc:
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
        """First-write-wins ONE-transaction commit of the intent, its
        recipients, the audit and the outbox rows (section 11.6): returns
        the intent when this call won, ``None`` when a concurrent writer
        already owns the idempotency key (the whole transaction rolls
        back), and never leaves a half-created intent."""
        self._require_api()
        if authenticated_actor_person_id != intent.actor_person_id:
            raise PostgresNotificationStoreContextError(
                "authenticated actor must match intent actor"
            )
        if authenticated_subject_person_id != intent.subject_person_id:
            raise PostgresNotificationStoreContextError(
                "authenticated subject must match intent subject"
            )
        pool = self._ready()
        async with pool.acquire() as connection:
            try:
                async with connection.transaction():
                    await self._authenticated_scope(
                        connection,
                        actor_person_id=authenticated_actor_person_id,
                        subject_person_id=authenticated_subject_person_id,
                    )
                    await self._scope(
                        connection,
                        subject_person_id=intent.subject_person_id,
                    )
                    await connection.execute(
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
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                              $12, $13, $14, $15, $16, $17, $18, $19, $20, $21,
                              $22, $23, $24, $25, $26, $27)
                    """,
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
                    intent.valid_until,
                    intent.template_key,
                    json.dumps(
                        intent.template_params, sort_keys=True, ensure_ascii=False
                    ),
                    intent.reason_code,
                    intent.script_version,
                    _timestamp(intent.occurred_at),
                    intent.status,
                    intent.cancelled_reason,
                    intent.cancelled_at,
                    intent.delivered_at,
                    _timestamp(intent.created_at),
                    _timestamp(intent.updated_at),
                )
                    for recipient in recipients:
                        await connection.execute(
                        """
                        INSERT INTO notification_recipients (
                            recipient_id, intent_id, person_id, role, relationship_id,
                            relationship_status, relationship_snapshot_id,
                            relationship_revision, channels_json, channel_index, status,
                            attempts, max_retries, next_attempt_at, leased_until,
                            fencing_token, last_error_code, valid_from, valid_until,
                            delivered_at, delivered_channel, cancelled_reason,
                            cancelled_at, created_at, updated_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                                  $12, $13, $14, $15, $16, $17, $18, $19, $20,
                                  $21, $22, $23, $24, $25)
                        """,
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
                        recipient.next_attempt_at,
                        recipient.leased_until,
                        recipient.fencing_token,
                        recipient.last_error_code,
                        _timestamp(recipient.valid_from),
                        recipient.valid_until,
                        recipient.delivered_at,
                        recipient.delivered_channel,
                        recipient.cancelled_reason,
                        recipient.cancelled_at,
                        _timestamp(recipient.created_at),
                        _timestamp(recipient.updated_at),
                    )
                    await _insert_audit_events(connection, audit)
                    await _insert_outbox_events(connection, outbox)
            except asyncpg.UniqueViolationError as exc:
                if "notification_intents_idempotency_key_key" in str(exc) or (
                    "idempotency" in str(exc)
                ):
                    # Concurrent writer already owns the key: first write
                    # wins, our transaction rolled back.
                    return None
                raise
            return intent

    async def get_intent(
        self, intent_id: str, *, subject_person_id: str
    ) -> NotificationIntent | None:
        self._require_api()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._authenticated_scope(
                    connection,
                    actor_person_id=subject_person_id,
                    subject_person_id=subject_person_id,
                )
                await self._scope(
                    connection,
                    subject_person_id=subject_person_id,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM notification_intents WHERE intent_id = $1",
                    intent_id,
                )
                return _intent_from_row(row) if row is not None else None

    async def get_intent_for_worker(self, intent_id: str) -> NotificationIntent | None:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                row = await connection.fetchrow(
                    "SELECT * FROM notification_intents WHERE intent_id = $1",
                    intent_id,
                )
                return _intent_from_row(row) if row is not None else None

    async def get_intent_by_idempotency_key(
        self, idempotency_key: str, *, subject_person_id: str
    ) -> NotificationIntent | None:
        self._require_api()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._authenticated_scope(
                    connection,
                    actor_person_id=subject_person_id,
                    subject_person_id=subject_person_id,
                )
                await self._scope(
                    connection,
                    subject_person_id=subject_person_id,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM notification_intents WHERE idempotency_key = $1",
                    idempotency_key,
                )
                return _intent_from_row(row) if row is not None else None

    async def save_recipient(self, recipient: RecipientBinding) -> RecipientBinding:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    person_id=recipient.person_id,
                    recipient_id=recipient.recipient_id,
                )
                await connection.execute(
                    """
                    UPDATE notification_recipients SET
                        relationship_status = $2,
                        channel_index = $3,
                        status = $4,
                        attempts = $5,
                        next_attempt_at = $6,
                        leased_until = $7,
                        fencing_token = $8,
                        last_error_code = $9,
                        delivered_at = $10,
                        delivered_channel = $11,
                        cancelled_reason = $12,
                        cancelled_at = $13,
                        updated_at = $14
                    WHERE recipient_id = $1
                      AND intent_id = $15
                    """,
                    recipient.recipient_id,
                    recipient.relationship_status,
                    recipient.channel_index,
                    recipient.status,
                    recipient.attempts,
                    recipient.next_attempt_at,
                    recipient.leased_until,
                    recipient.fencing_token,
                    recipient.last_error_code,
                    recipient.delivered_at,
                    recipient.delivered_channel,
                    recipient.cancelled_reason,
                    recipient.cancelled_at,
                    _timestamp(recipient.updated_at),
                    recipient.intent_id,
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
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    subject_person_id=intent.subject_person_id,
                )
                for recipient in recipients:
                    await connection.execute(
                        """
                        UPDATE notification_recipients
                        SET status = $2,
                            leased_until = NULL,
                            fencing_token = NULL,
                            next_attempt_at = NULL,
                            cancelled_reason = $3,
                            cancelled_at = $4,
                            updated_at = $5
                        WHERE recipient_id = $1
                          AND status NOT IN ('delivered', 'dead_lettered', 'cancelled')
                        """,
                        recipient.recipient_id,
                        recipient.status,
                        recipient.cancelled_reason,
                        recipient.cancelled_at,
                        _timestamp(recipient.updated_at),
                    )
                # P0-4: the intent status is DERIVED from the recipients in
                # this transaction (terminal recipients stay untouched), so
                # a delivered recipient keeps the intent delivered.
                derived_intent = await self._derive_intent(
                    connection, intent.intent_id, now
                )
                await _insert_audit_events(connection, audit)
                await _insert_outbox_events(connection, outbox)
                return derived_intent

    async def get_recipient(
        self, recipient_id: str, *, person_id: str
    ) -> RecipientBinding | None:
        self._require_api()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._authenticated_scope(
                    connection,
                    actor_person_id=person_id,
                    subject_person_id=person_id,
                )
                await self._scope(
                    connection,
                    person_id=person_id,
                    recipient_id=recipient_id,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM notification_recipients WHERE recipient_id = $1",
                    recipient_id,
                )
                return _recipient_from_row(row) if row is not None else None

    async def get_recipient_for_worker(
        self, recipient_id: str
    ) -> RecipientBinding | None:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    recipient_id=recipient_id,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM notification_recipients WHERE recipient_id = $1",
                    recipient_id,
                )
                return _recipient_from_row(row) if row is not None else None

    async def list_recipients(
        self, intent_id: str, *, person_id: str
    ) -> tuple[RecipientBinding, ...]:
        self._require_api()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._authenticated_scope(
                    connection,
                    actor_person_id=person_id,
                    subject_person_id=person_id,
                )
                await self._scope(
                    connection,
                    person_id=person_id,
                )
                rows = await connection.fetch(
                    "SELECT * FROM notification_recipients WHERE intent_id = $1 "
                    "AND person_id = $2 ORDER BY created_at",
                    intent_id,
                    person_id,
                )
                return tuple(_recipient_from_row(row) for row in rows)

    async def list_recipients_for_worker(
        self, intent_id: str
    ) -> tuple[RecipientBinding, ...]:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                rows = await connection.fetch(
                    "SELECT * FROM notification_recipients WHERE intent_id = $1 "
                    "ORDER BY created_at",
                    intent_id,
                )
                return tuple(_recipient_from_row(row) for row in rows)

    async def list_recipients_by_intent_subject(
        self, intent_id: str, *, subject_person_id: str
    ) -> tuple[RecipientBinding, ...]:
        self._require_api()
        """API-scoped read joined on the intent's subject (never the worker
        role): the caller only sees recipients of intents they own."""
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._authenticated_scope(
                    connection,
                    actor_person_id=subject_person_id,
                    subject_person_id=subject_person_id,
                )
                await self._scope(
                    connection,
                    subject_person_id=subject_person_id,
                )
                rows = await connection.fetch(
                    "SELECT r.* FROM notification_recipients r"
                    " JOIN notification_intents i ON i.intent_id = r.intent_id"
                    " WHERE r.intent_id = $1 AND i.subject_person_id = $2"
                    " ORDER BY r.created_at",
                    intent_id,
                    subject_person_id,
                )
                return tuple(_recipient_from_row(row) for row in rows)

    async def list_recipients_by_relationship(
        self, relationship_id: str
    ) -> tuple[RecipientBinding, ...]:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                rows = await connection.fetch(
                    "SELECT * FROM notification_recipients "
                    "WHERE relationship_id = $1 ORDER BY created_at",
                    relationship_id,
                )
                return tuple(_recipient_from_row(row) for row in rows)

    async def list_due_recipients(
        self, now: datetime, limit: int
    ) -> tuple[RecipientBinding, ...]:
        self._require_worker()
        pool = self._ready()
        timestamp = _timestamp(now)
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                rows = await connection.fetch(
                    """
                    SELECT * FROM notification_recipients
                    WHERE status IN ('pending', 'failed', 'in_progress')
                      AND status <> 'cancelled'
                      AND relationship_status = 'active'
                      AND (valid_until IS NULL OR valid_until > $1)
                      AND valid_from <= $1
                      AND (next_attempt_at IS NULL OR next_attempt_at <= $1)
                      AND (
                          status IN ('pending', 'failed')
                          OR (status = 'in_progress' AND leased_until <= $1)
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM notification_delivery_attempts a
                          WHERE a.recipient_id =
                                notification_recipients.recipient_id
                            AND a.status = 'uncertain'
                      )
                    ORDER BY created_at
                    LIMIT $2
                    """,
                    timestamp,
                    limit,
                )
                return tuple(_recipient_from_row(row) for row in rows)

    async def claim_recipient(
        self,
        recipient_id: str,
        attempt: DeliveryAttempt,
        now: datetime,
    ) -> tuple[RecipientBinding, DeliveryAttempt] | None:
        self._require_worker()
        pool = self._ready()
        timestamp = _timestamp(now)
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    recipient_id=recipient_id,
                )
                updated = await connection.fetchrow(
                    """
                    UPDATE notification_recipients
                    SET status = 'in_progress',
                        attempts = $2,
                        fencing_token = $3,
                        leased_until = $4,
                        next_attempt_at = NULL,
                        updated_at = $5
                    WHERE recipient_id = $1
                      AND status IN ('pending', 'failed')
                      AND status <> 'cancelled'
                      AND relationship_status = 'active'
                      AND (valid_until IS NULL OR valid_until > $5)
                      AND valid_from <= $5
                      AND (next_attempt_at IS NULL OR next_attempt_at <= $5)
                      AND attempts = $6
                    RETURNING *
                    """,
                    recipient_id,
                    attempt.attempt_number,
                    attempt.fencing_token,
                    _timestamp(attempt.leased_until),
                    timestamp,
                    attempt.attempt_number - 1,
                )
                if updated is None:
                    updated = await connection.fetchrow(
                        """
                        UPDATE notification_recipients
                        SET status = 'in_progress',
                            attempts = $2,
                            fencing_token = $3,
                            leased_until = $4,
                            next_attempt_at = NULL,
                            updated_at = $5
                        WHERE recipient_id = $1
                          AND status = 'in_progress'
                          AND leased_until <= $5
                          AND relationship_status = 'active'
                          AND attempts = $6
                        RETURNING *
                        """,
                        recipient_id,
                        attempt.attempt_number,
                        attempt.fencing_token,
                        _timestamp(attempt.leased_until),
                        timestamp,
                        attempt.attempt_number - 1,
                    )
                if updated is None:
                    return None
                try:
                    await connection.execute(
                        """
                        INSERT INTO notification_delivery_attempts (
                            attempt_id, intent_id, recipient_id, attempt_number,
                            channel, logical_delivery_key, status, fencing_token,
                            leased_until, started_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, 'leased', $7, $8, $9)
                        """,
                        attempt.attempt_id,
                        attempt.intent_id,
                        attempt.recipient_id,
                        attempt.attempt_number,
                        attempt.channel,
                        attempt.logical_delivery_key,
                        attempt.fencing_token,
                        _timestamp(attempt.leased_until),
                        _timestamp(attempt.started_at),
                    )
                except asyncpg.UniqueViolationError:
                    return None
            return (_recipient_from_row(updated), attempt)

    async def save_attempt(self, attempt: DeliveryAttempt) -> DeliveryAttempt:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    recipient_id=attempt.recipient_id,
                )
                await connection.execute(
                    """
                    INSERT INTO notification_delivery_attempts (
                        attempt_id, intent_id, recipient_id, attempt_number,
                        channel, logical_delivery_key, status, fencing_token,
                        leased_until, started_at, finished_at, error_code
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT (attempt_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        finished_at = EXCLUDED.finished_at,
                        error_code = EXCLUDED.error_code
                    """,
                    attempt.attempt_id,
                    attempt.intent_id,
                    attempt.recipient_id,
                    attempt.attempt_number,
                    attempt.channel,
                    attempt.logical_delivery_key,
                    attempt.status,
                    attempt.fencing_token,
                    _timestamp(attempt.leased_until),
                    _timestamp(attempt.started_at),
                    attempt.finished_at,
                    attempt.error_code,
                )
        return attempt

    async def get_attempt(self, attempt_id: str) -> DeliveryAttempt | None:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                row = await connection.fetchrow(
                    "SELECT * FROM notification_delivery_attempts WHERE attempt_id = $1",
                    attempt_id,
                )
                return _attempt_from_row(row) if row is not None else None

    async def list_attempts(
        self, recipient_id: str, *, person_id: str
    ) -> tuple[DeliveryAttempt, ...]:
        self._require_api()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._authenticated_scope(
                    connection,
                    actor_person_id=person_id,
                    subject_person_id=person_id,
                )
                await self._scope(
                    connection,
                    person_id=person_id,
                    recipient_id=recipient_id,
                )
                rows = await connection.fetch(
                    "SELECT a.* FROM notification_delivery_attempts a"
                    " JOIN notification_recipients r ON r.recipient_id = a.recipient_id"
                    " WHERE a.recipient_id = $1 AND r.person_id = $2"
                    " ORDER BY a.attempt_number",
                    recipient_id,
                    person_id,
                )
                return tuple(_attempt_from_row(row) for row in rows)

    async def list_uncertain_attempts(
        self, limit: int
    ) -> tuple[DeliveryAttempt, ...]:
        """Worker-only scan of UNKNOWN-outcome attempts (timeout without an
        authoritative reconcile answer yet).  Their recipients are excluded
        from claiming until a reconcile resolves the outcome, so no retry /
        fallback happens while the result is unknown."""
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                rows = await connection.fetch(
                    "SELECT * FROM notification_delivery_attempts"
                    " WHERE status = 'uncertain'"
                    " ORDER BY started_at LIMIT $1",
                    limit,
                )
                return tuple(_attempt_from_row(row) for row in rows)

    async def save_receipt(self, receipt: DeliveryReceipt) -> DeliveryReceipt:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    recipient_id=receipt.recipient_id,
                )
                await connection.execute(
                    """
                    INSERT INTO notification_receipts (
                        receipt_id, intent_id, recipient_id, attempt_id, channel,
                        channel_receipt_id, delivered_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (attempt_id) DO NOTHING
                    """,
                    receipt.receipt_id,
                    receipt.intent_id,
                    receipt.recipient_id,
                    receipt.attempt_id,
                    receipt.channel,
                    receipt.channel_receipt_id,
                    _timestamp(receipt.delivered_at),
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
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    recipient_id=recipient.recipient_id,
                )
                leased = await connection.fetchrow(
                    "SELECT attempt_id, intent_id, recipient_id, attempt_number"
                    " FROM notification_delivery_attempts"
                    # A provider reconcile / late result may complete an
                    # ``uncertain`` attempt (delivered / not-delivered) -
                    # always under the SAME fencing token CAS.
                    " WHERE attempt_id = $1 AND status IN ('leased', 'uncertain')"
                    "   AND fencing_token = $2 FOR UPDATE",
                    attempt_id,
                    fencing_token,
                )
                if leased is None:
                    return None
                # P0-A: the attempt identity must match the caller's claim so
                # a stale worker cannot finish an attempt that now belongs to
                # a different intent/recipient/attempt number.
                if (
                    leased["intent_id"] != recipient.intent_id
                    or leased["recipient_id"] != recipient.recipient_id
                    or leased["attempt_number"] != finished_attempt.attempt_number
                ):
                    return None
                updated_recipient = await connection.fetchrow(
                    """
                    UPDATE notification_recipients
                    SET relationship_status = $4,
                        channel_index = $5,
                        status = $6,
                        attempts = $7,
                        next_attempt_at = $8,
                        leased_until = NULL,
                        fencing_token = NULL,
                        last_error_code = $9,
                        delivered_at = $10,
                        delivered_channel = $11,
                        cancelled_reason = $12,
                        cancelled_at = $13,
                        updated_at = $14
                    WHERE recipient_id = $1
                      AND fencing_token = $2
                      AND status = 'in_progress'
                      AND attempts = $3
                    RETURNING *
                    """,
                    recipient.recipient_id,
                    fencing_token,
                    finished_attempt.attempt_number,
                    recipient.relationship_status,
                    recipient.channel_index,
                    recipient.status,
                    recipient.attempts,
                    recipient.next_attempt_at,
                    recipient.last_error_code,
                    recipient.delivered_at,
                    recipient.delivered_channel,
                    recipient.cancelled_reason,
                    recipient.cancelled_at,
                    _timestamp(recipient.updated_at),
                )
                if updated_recipient is None:
                    return None
                await connection.execute(
                    """
                    UPDATE notification_delivery_attempts
                    SET status = $2, finished_at = $3, error_code = $4
                    WHERE attempt_id = $1
                    """,
                    finished_attempt.attempt_id,
                    finished_attempt.status,
                    finished_attempt.finished_at,
                    finished_attempt.error_code,
                )
                if receipt is not None:
                    await connection.execute(
                        """
                        INSERT INTO notification_receipts (
                            receipt_id, intent_id, recipient_id, attempt_id, channel,
                            channel_receipt_id, delivered_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (attempt_id) DO NOTHING
                        """,
                        receipt.receipt_id,
                        receipt.intent_id,
                        receipt.recipient_id,
                        receipt.attempt_id,
                        receipt.channel,
                        receipt.channel_receipt_id,
                        _timestamp(receipt.delivered_at),
                    )
                await _insert_audit_events(connection, audit)
                await _insert_outbox_events(connection, outbox)
                # Derive + persist the intent status from THIS transaction's
                # recipients (section 11.6).
                intent = await self._derive_intent(
                    connection, recipient.intent_id, now
                )
                return (
                    _recipient_from_row(updated_recipient),
                    intent,
                )

    async def _derive_intent(
        self,
        connection: asyncpg.Connection,
        intent_id: str,
        now: datetime,
    ) -> NotificationIntent | None:
        """Derive and persist the intent status from the recipients visible
        in the CURRENT transaction; terminal transitions also write the
        stable terminal outbox event in the same transaction."""
        from services.notification.domain import derive_intent_status

        intent_row = await connection.fetchrow(
            "SELECT * FROM notification_intents WHERE intent_id = $1 FOR UPDATE",
            intent_id,
        )
        if intent_row is None:
            return None
        intent = _intent_from_row(intent_row)
        all_recipients = await connection.fetch(
            "SELECT * FROM notification_recipients"
            " WHERE intent_id = $1 ORDER BY created_at",
            intent_id,
        )
        derived = derive_intent_status(
            tuple(_recipient_from_row(row) for row in all_recipients)
        )
        if derived == intent.status:
            return intent
        await connection.execute(
            "UPDATE notification_intents"
            " SET status = $2, delivered_at = $3, updated_at = $4"
            " WHERE intent_id = $1",
            intent.intent_id,
            derived,
            _timestamp(now) if derived == "delivered" else None,
            _timestamp(now),
        )
        intent = _intent_status_update(intent, derived, now)
        if derived in ("delivered", "dead_lettered", "cancelled"):
            await _insert_outbox_events(
                connection,
                (
                    NotificationOutboxEvent(
                        outbox_id=str(uuid.uuid4()),
                        event_id=f"{intent.idempotency_key}:intent.{derived}",
                        topic=f"notification.intent.{derived}",
                        payload=intent.to_dict(),
                        created_at=now,
                    ),
                ),
            )
        return intent

    async def cancel_recipient_atomically(
        self,
        *,
        recipient_id: str,
        recipient: RecipientBinding,
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> tuple[RecipientBinding, NotificationIntent | None] | None:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    recipient_id=recipient_id,
                )
                locked = await connection.fetchrow(
                    "SELECT status FROM notification_recipients"
                    " WHERE recipient_id = $1 FOR UPDATE",
                    recipient_id,
                )
                if locked is None or locked["status"] in (
                    "delivered",
                    "dead_lettered",
                    "cancelled",
                ):
                    return None
                updated = await connection.fetchrow(
                    """
                    UPDATE notification_recipients
                    SET status = 'cancelled',
                        leased_until = NULL,
                        fencing_token = NULL,
                        next_attempt_at = NULL,
                        cancelled_reason = $2,
                        cancelled_at = $3,
                        updated_at = $4
                    WHERE recipient_id = $1
                      AND status NOT IN ('delivered', 'dead_lettered', 'cancelled')
                    RETURNING *
                    """,
                    recipient_id,
                    recipient.cancelled_reason,
                    recipient.cancelled_at,
                    _timestamp(recipient.updated_at),
                )
                if updated is None:
                    return None
                await _insert_audit_events(connection, audit)
                await _insert_outbox_events(connection, outbox)
                intent = await self._derive_intent(connection, recipient.intent_id, now)
                return (_recipient_from_row(updated), intent)

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
        in the same transaction (no leased residue): the attempt is finished
        only while still ``leased`` to this fencing token (a newer lease is
        never overwritten); the recipient is cancelled only while not
        terminal; repeated calls are idempotent no-ops."""
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    recipient_id=recipient_id,
                )
                locked = await connection.fetchrow(
                    "SELECT status FROM notification_recipients"
                    " WHERE recipient_id = $1 FOR UPDATE",
                    recipient_id,
                )
                if locked is None or locked["status"] in (
                    "delivered",
                    "dead_lettered",
                    "cancelled",
                ):
                    return None
                updated = await connection.fetchrow(
                    """
                    UPDATE notification_recipients
                    SET status = 'cancelled',
                        leased_until = NULL,
                        fencing_token = NULL,
                        next_attempt_at = NULL,
                        cancelled_reason = $2,
                        cancelled_at = $3,
                        updated_at = $4
                    WHERE recipient_id = $1
                      AND status NOT IN ('delivered', 'dead_lettered', 'cancelled')
                    RETURNING *
                    """,
                    recipient_id,
                    recipient.cancelled_reason,
                    recipient.cancelled_at,
                    _timestamp(recipient.updated_at),
                )
                if updated is None:
                    return None
                await connection.execute(
                    """
                    UPDATE notification_delivery_attempts
                    SET status = $2, finished_at = $3, error_code = $4
                    WHERE attempt_id = $1
                      AND status = 'leased'
                      AND fencing_token = $5
                    """,
                    attempt_id,
                    finished_attempt.status,
                    _timestamp(finished_attempt.finished_at or now),
                    finished_attempt.error_code,
                    fencing_token,
                )
                await _insert_audit_events(connection, audit)
                await _insert_outbox_events(connection, outbox)
                intent = await self._derive_intent(connection, recipient.intent_id, now)
                return (_recipient_from_row(updated), intent)

    async def replay_recipient_atomically(
        self,
        *,
        recipient_id: str,
        recipient: RecipientBinding,
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> tuple[RecipientBinding, NotificationIntent | None] | None:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    recipient_id=recipient_id,
                )
                locked = await connection.fetchrow(
                    "SELECT * FROM notification_recipients"
                    " WHERE recipient_id = $1 FOR UPDATE",
                    recipient_id,
                )
                if locked is None or locked["status"] not in (
                    "failed",
                    "dead_lettered",
                ):
                    return None
                if locked["relationship_status"] != "active":
                    return None
                # P0-5/P0-4: the replay transition is derived from the DB
                # row - the caller can never supply its own timestamps.
                updated = await connection.fetchrow(
                    """
                    UPDATE notification_recipients
                    SET status = 'failed',
                        next_attempt_at = $2,
                        last_error_code = NULL,
                        updated_at = $2
                    WHERE recipient_id = $1
                      AND status IN ('failed', 'dead_lettered')
                      AND relationship_status = 'active'
                    RETURNING *
                    """,
                    recipient_id,
                    _timestamp(now),
                )
                if updated is None:
                    return None
                await _insert_audit_events(connection, audit)
                await _insert_outbox_events(connection, outbox)
                intent = await self._derive_intent(connection, recipient.intent_id, now)
                return (_recipient_from_row(updated), intent)

    async def cancel_relationship_atomically(
        self,
        *,
        relationship_id: str,
        relationship_status: str,
        reason: CancelReason,
        actor_person_id: str | None,
        now: datetime,
    ) -> tuple[tuple[RecipientBinding, ...], tuple[NotificationIntent | None, ...]]:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                cancelled: list[RecipientBinding] = []
                touched_intents: dict[str, NotificationIntent | None] = {}
                # P0-5: select the authoritative set from the store, lock
                # the rows, and update with a relationship_id condition.
                rows = await connection.fetch(
                    "SELECT * FROM notification_recipients"
                    " WHERE relationship_id = $1 FOR UPDATE",
                    relationship_id,
                )
                for recipient in rows:
                    updated = await connection.fetchrow(
                        """
                        UPDATE notification_recipients
                        SET relationship_status = $2,
                            status = 'cancelled',
                            leased_until = NULL,
                            fencing_token = NULL,
                            next_attempt_at = NULL,
                            cancelled_reason = $3,
                            cancelled_at = $4,
                            updated_at = $5
                        WHERE recipient_id = $1
                          AND relationship_id = $6
                          AND status NOT IN ('delivered', 'dead_lettered', 'cancelled')
                        RETURNING *
                        """,
                        recipient["recipient_id"],
                        relationship_status,
                        reason,
                        now,
                        _timestamp(now),
                        relationship_id,
                    )
                    if updated is not None:
                        cancelled.append(_recipient_from_row(updated))
                        touched_intents[recipient["intent_id"]] = None
                # P0-5: audit/outbox only for the recipients that ACTUALLY
                # transitioned; stable event ids make replays no-ops.
                event_id = f"relationship:{relationship_id}.inactive"
                for recipient in cancelled:
                    await connection.execute(
                        """
                        INSERT INTO notification_audit_events (
                            event_id, action, actor_person_id, intent_id, recipient_id,
                            payload_json, created_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        f"audit:{event_id}:{recipient.recipient_id}",
                        "recipient.cancel",
                        actor_person_id,
                        recipient.intent_id,
                        recipient.recipient_id,
                        json.dumps(
                            {
                                "relationship_id": relationship_id,
                                "status": relationship_status,
                                "reason": reason,
                            },
                            sort_keys=True,
                            ensure_ascii=False,
                        ),
                        _timestamp(now),
                    )
                if cancelled:
                    await connection.execute(
                        """
                        INSERT INTO notification_outbox (
                            outbox_id, event_id, topic, subject_person_id,
                            payload_json, status, attempts, locked_until,
                            last_error_code, created_at, delivered_at, updated_at
                        ) SELECT $1, $2, $3, i.subject_person_id, $4, 'pending',
                                  0, NULL, NULL, $5, NULL, $5
                           FROM notification_intents i
                           WHERE i.intent_id = $6
                        ON CONFLICT (event_id) DO UPDATE
                            SET payload_json = EXCLUDED.payload_json
                            WHERE notification_outbox.payload_json
                                  IS DISTINCT FROM EXCLUDED.payload_json
                        """,
                        event_id,
                        event_id,
                        "notification.relationship.inactive",
                        json.dumps(
                            {
                                "relationship_id": relationship_id,
                                "status": relationship_status,
                                "reason": reason,
                                "recipient_ids": [
                                    item.recipient_id for item in cancelled
                                ],
                            },
                            sort_keys=True,
                            ensure_ascii=False,
                        ),
                        _timestamp(now),
                        cancelled[0].intent_id,
                    )
                for intent_id in list(touched_intents):
                    touched_intents[intent_id] = await self._derive_intent(
                        connection, intent_id, now
                    )
                return (
                    tuple(cancelled),
                    tuple(touched_intents.values()),
                )

    async def get_receipt_by_attempt(self, attempt_id: str) -> DeliveryReceipt | None:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                row = await connection.fetchrow(
                    "SELECT * FROM notification_receipts WHERE attempt_id = $1",
                    attempt_id,
                )
                if row is None:
                    return None
                return DeliveryReceipt(
                    receipt_id=row["receipt_id"],
                    intent_id=row["intent_id"],
                    recipient_id=row["recipient_id"],
                    attempt_id=row["attempt_id"],
                    channel=row["channel"],
                    channel_receipt_id=row["channel_receipt_id"],
                    delivered_at=_from_db(row["delivered_at"]) or datetime.now(UTC),
                )

    async def list_receipts(
        self, intent_id: str, *, person_id: str
    ) -> tuple[DeliveryReceipt, ...]:
        self._require_api()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._authenticated_scope(
                    connection,
                    actor_person_id=person_id,
                    subject_person_id=person_id,
                )
                await self._scope(
                    connection,
                    subject_person_id=person_id,
                )
                rows = await connection.fetch(
                    "SELECT rc.* FROM notification_receipts rc"
                    " JOIN notification_intents i ON i.intent_id = rc.intent_id"
                    " WHERE rc.intent_id = $1 AND i.subject_person_id = $2"
                    " ORDER BY rc.delivered_at",
                    intent_id,
                    person_id,
                )
                return tuple(
                    DeliveryReceipt(
                        receipt_id=row["receipt_id"],
                        intent_id=row["intent_id"],
                        recipient_id=row["recipient_id"],
                        attempt_id=row["attempt_id"],
                        channel=row["channel"],
                        channel_receipt_id=row["channel_receipt_id"],
                        delivered_at=_from_db(row["delivered_at"]) or datetime.now(UTC),
                    )
                    for row in rows
                )

    async def scan_intents(
        self, statuses: tuple[IntentStatus, ...] | None = None
    ) -> tuple[NotificationIntent, ...]:
        self._require_worker()
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                if statuses is None:
                    rows = await connection.fetch(
                        "SELECT * FROM notification_intents ORDER BY created_at"
                    )
                else:
                    rows = await connection.fetch(
                        "SELECT * FROM notification_intents "
                        "WHERE status = ANY($1::text[]) ORDER BY created_at",
                        list(statuses),
                    )
                return tuple(_intent_from_row(row) for row in rows)

    async def append_audit(self, event: NotificationAuditEvent) -> None:
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                await connection.execute(
                    """
                    INSERT INTO notification_audit_events (
                        event_id, action, actor_person_id, intent_id, recipient_id,
                        payload_json, created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    event.event_id,
                    event.action,
                    event.actor_person_id,
                    event.intent_id,
                    event.recipient_id,
                    json.dumps(event.payload, sort_keys=True, ensure_ascii=False),
                    _timestamp(event.created_at),
                )

    async def enqueue_outbox(self, event: NotificationOutboxEvent) -> None:
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                await _insert_outbox_events(connection, (event,))


__all__ = ["PostgresNotificationStore"]
