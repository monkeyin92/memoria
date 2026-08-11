"""PostgreSQL production adapter for the memory scope store seam.

Mirrors the FORCE-RLS ``postgres_schema.sql`` (PR-17).  RLS policies are
NOT ``USING (true)``: every policy evaluates the transaction-level app
context (``app.memory.actor_subject_id`` / ``subject_id`` /
``family_space_id`` / ``service_role``) which this adapter sets through
``SELECT set_config(..., true)`` at the start of each transaction.  A
missing context value fails closed in Python (``PostgresMemoryStoreContextError``)
and the database also fails closed (FORCE RLS without context exposes
zero rows and rejects writes).  ``initialize`` probes that the app context
mechanism works and refuses to start when it does not.

Every multi-write operation commits in a single transaction together with
its outbox and audit rows.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import asyncpg

from services.memory_scope.domain import (
    AlreadyVotedError,
    ConfirmationVote,
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

_SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")
_RLS_TABLES = (
    "memory_records",
    "memory_status_events",
    "memory_shared_proposals",
    "memory_shared_votes",
    "memory_outbox",
    "memory_audit_events",
)

_ROLE_NAMES = {
    "api": "memoria_memory_api",
    "worker": "memoria_memory_worker",
    "action_executor": "memoria_action_executor",
}


def _jsonb_list(value: object) -> list[Any]:
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


class PostgresMemoryStoreContextError(WriteFenceMissingError):
    """Required transaction-level app context was missing or empty."""


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: datetime | None) -> datetime | None:
    return value


class PostgresMemoryStore:
    """asyncpg adapter; ``schema_path`` defaults to the bundled schema."""

    #: PostgreSQL CANNOT join the Policy/Consent authority into the insert
    #: transaction from the app process: the service refuses any commit
    #: authority that is not the Policy sub-agent's same-connection seam
    #: (``same_transaction_seam``), so production family promotion fails
    #: closed instead of faking cross-database atomicity.
    requires_same_transaction_authority: bool = True

    def __init__(self, dsn: str, *, schema_path: str | Path | None = None) -> None:
        self._dsn = dsn
        self._schema_path = Path(schema_path) if schema_path is not None else _SCHEMA_PATH
        self._expected_role: Literal["api", "worker", "action_executor"] = "api"
        self._pool: asyncpg.Pool | None = None

    def with_role(
        self, role: Literal["api", "worker", "action_executor"]
    ) -> PostgresMemoryStore:
        """Declare which database role this adapter connects as (P0-2):
        command authority comes from the real role, never from GUCs."""
        self._expected_role = role
        return self

    def _require_worker(self) -> None:
        if self._expected_role != "worker":
            raise RuntimeError(
                "operation requires the memoria_memory_worker database role; "
                "the API role must not execute worker commands (P0-2)"
            )

    def _require_api(self) -> None:
        if self._expected_role != "api":
            raise RuntimeError(
                "operation is subject-API only; the worker adapter must not "
                "impersonate the subject API path (P0-2)"
            )

    async def initialize(
        self,
        *,
        bootstrap_dsn: str | None = None,
        app_role_password: str | None = None,
    ) -> None:
        if bootstrap_dsn is not None:
            await self._bootstrap(
                bootstrap_dsn,
                app_role_password=app_role_password,
            )
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        async with self._pool.acquire() as connection:
            await self._verify_runtime_security(connection)

    async def _bootstrap(
        self,
        bootstrap_dsn: str,
        *,
        app_role_password: str | None,
    ) -> None:
        """Run the deployment/migration bootstrap.

        The schema temporarily assumes the dedicated
        ``memoria_memory_owner`` NOLOGIN role before creating or replacing
        sensitive objects.  Neither the bootstrap login nor any production
        application login remains an object owner.
        """
        app_role = urlsplit(self._dsn).username or ""
        if not app_role:
            raise PostgresMemoryStoreContextError(
                "app DSN must carry the memoria_memory_api/worker role as its user"
            )
        bootstrap = await asyncpg.connect(bootstrap_dsn)
        try:
            # P0-2: ALL real roles exist with login, non-superuser,
            # non-BYPASSRLS; the schema grants/policies target them.
            for role_name in _ROLE_NAMES.values():
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
            for role_name in _ROLE_NAMES.values():
                if app_role_password is not None:
                    alter_stmt = await bootstrap.fetchval(
                        "SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER "
                        "NOBYPASSRLS PASSWORD %L', $1::text, $2::text)",
                        role_name,
                        app_role_password,
                    )
                    await bootstrap.execute(alter_stmt)
                else:
                    alter_stmt = await bootstrap.fetchval(
                        "SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER "
                        "NOBYPASSRLS', $1::text)",
                        role_name,
                    )
                    await bootstrap.execute(alter_stmt)
            if app_role not in (_ROLE_NAMES["api"], _ROLE_NAMES["worker"]):
                raise PostgresMemoryStoreContextError(
                    f"app DSN user {app_role!r} is not a memoria_memory role"
                )
            schema = self._schema_path.read_text(encoding="utf-8")
            await bootstrap.execute(schema)
            # Additive migrations for databases created before the proposal
            # fence-snapshot columns existed (main architecture review);
            # every statement is idempotent and safe on every bootstrap.
            proposal_columns = (
                (
                    "session_id",
                    "TEXT NOT NULL DEFAULT '' "
                    "CHECK (char_length(session_id) BETWEEN 1 AND 128)",
                ),
                ("epoch", "INTEGER NOT NULL DEFAULT 1 CHECK (epoch >= 1)"),
                (
                    "binding_id",
                    "TEXT NOT NULL DEFAULT '' "
                    "CHECK (char_length(binding_id) BETWEEN 1 AND 128)",
                ),
                (
                    "binding_role",
                    "TEXT NOT NULL DEFAULT '' "
                    "CHECK (char_length(binding_role) BETWEEN 1 AND 64)",
                ),
                (
                    "runtime_profile_id",
                    "TEXT NOT NULL DEFAULT '' "
                    "CHECK (char_length(runtime_profile_id) BETWEEN 1 AND 128)",
                ),
                (
                    "device_id",
                    "TEXT NOT NULL DEFAULT '' "
                    "CHECK (char_length(device_id) BETWEEN 0 AND 128)",
                ),
                (
                    "subject_revision",
                    "INTEGER NOT NULL DEFAULT 0 CHECK (subject_revision >= 0)",
                ),
                ("generation_id", "TEXT"),
                ("turn_id", "INTEGER CHECK (turn_id >= 1)"),
                ("valid_until", "TIMESTAMPTZ"),
                (
                    "fence_context_hash",
                    "TEXT NOT NULL DEFAULT '' "
                    "CHECK (char_length(fence_context_hash) BETWEEN 1 AND 128)",
                ),
                (
                    "proposal_revision",
                    "INTEGER NOT NULL DEFAULT 1 CHECK (proposal_revision >= 1)",
                ),
                (
                    "capture_evidence_hash",
                    "TEXT NOT NULL DEFAULT '' "
                    "CHECK (char_length(capture_evidence_hash) IN (0, 64))",
                ),
                (
                    "consent_snapshot_revision",
                    "INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (consent_snapshot_revision >= 0)",
                ),
                (
                    "consent_snapshot_hash",
                    "TEXT NOT NULL DEFAULT '' "
                    "CHECK (char_length(consent_snapshot_hash) IN (0, 64))",
                ),
                (
                    "membership_snapshot_id",
                    "TEXT NOT NULL DEFAULT '' "
                    "CHECK (char_length(membership_snapshot_id) BETWEEN 0 AND 128)",
                ),
                (
                    "membership_snapshot_revision",
                    "INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (membership_snapshot_revision >= 0)",
                ),
                (
                    "membership_snapshot_hash",
                    "TEXT NOT NULL DEFAULT '' "
                    "CHECK (char_length(membership_snapshot_hash) IN (0, 64))",
                ),
                (
                    "generation",
                    "INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0)",
                ),
                (
                    "tool_epoch",
                    "INTEGER NOT NULL DEFAULT 0 CHECK (tool_epoch >= 0)",
                ),
            )
            for column, definition in proposal_columns:
                stmt = await bootstrap.fetchval(
                    "SELECT format('ALTER TABLE memory_shared_proposals "
                    "ADD COLUMN IF NOT EXISTS %I %s', $1::text, $2::text)",
                    column,
                    definition,
                )
                await bootstrap.execute(stmt)
            renamed = await bootstrap.fetchval(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'memory_shared_proposals'
                  AND column_name = 'policy_receipt_id'
                """
            )
            if renamed is not None:
                await bootstrap.execute(
                    "ALTER TABLE memory_shared_proposals"
                    " RENAME COLUMN policy_receipt_id"
                    " TO proposal_policy_receipt_id"
                )
            await bootstrap.execute(
                """
                ALTER TABLE memory_shared_votes
                    ADD COLUMN IF NOT EXISTS approval_receipt_id TEXT
                        NOT NULL DEFAULT ''
                """
            )
            for column, definition in (
                ("approval_snapshot_id", "TEXT NOT NULL DEFAULT ''"),
                (
                    "approval_snapshot_revision",
                    "INTEGER NOT NULL DEFAULT 0",
                ),
                ("approval_snapshot_hash", "TEXT NOT NULL DEFAULT ''"),
            ):
                stmt = await bootstrap.fetchval(
                    "SELECT format('ALTER TABLE memory_shared_votes "
                    "ADD COLUMN IF NOT EXISTS %I %s', $1::text, $2::text)",
                    column,
                    definition,
                )
                await bootstrap.execute(stmt)
            await bootstrap.execute(
                """
                ALTER TABLE memory_records
                    ADD COLUMN IF NOT EXISTS promotion_fence_context_hash TEXT
                        NOT NULL DEFAULT ''
                """
            )
            await bootstrap.execute(
                """
                ALTER TABLE memory_records
                    ADD COLUMN IF NOT EXISTS approval_evidence_refs JSONB
                        NOT NULL DEFAULT '[]'::jsonb
                """
            )
            # Controlled migration of the legacy ``confirmed`` proposal
            # status: with a promoted record -> promoted; without any
            # record -> quarantined to frozen (never guessed), audited.
            legacy_confirmed = await bootstrap.fetch(
                "SELECT proposal_id FROM memory_shared_proposals"
                " WHERE status = 'confirmed'"
            )
            for row in legacy_confirmed:
                has_record = await bootstrap.fetchval(
                    "SELECT 1 FROM memory_records"
                    " WHERE shared_proposal_id = $1",
                    row["proposal_id"],
                )
                new_status = "promoted" if has_record is not None else "frozen"
                await bootstrap.execute(
                    "UPDATE memory_shared_proposals SET status = $2"
                    " WHERE proposal_id = $1",
                    row["proposal_id"],
                    new_status,
                )
                await bootstrap.execute(
                    "INSERT INTO memory_audit_events ("
                    " event_id, action, actor_subject_id, subject_id, record_id,"
                    " proposal_id, payload, created_at"
                    ") VALUES ($1, 'proposal.status.migrated', NULL, NULL, NULL,"
                    " $2, $3, $4)"
                    " ON CONFLICT (event_id) DO NOTHING",
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
                )
            # Canonical-evidence quarantine (main review): a legacy
            # pending/approvals_complete proposal WITHOUT the required
            # proposal revision / consent+membership snapshot
            # id+revision+hash / capture evidence digest is NEVER
            # promoted - quarantined to ``frozen`` with an audit.
            legacy_incomplete = await bootstrap.fetch(
                "SELECT proposal_id FROM memory_shared_proposals"
                " WHERE status IN ('pending', 'approvals_complete')"
                " AND (proposal_revision < 1 OR consent_snapshot_revision < 1"
                " OR membership_snapshot_revision < 1"
                " OR consent_snapshot_id = '' OR membership_snapshot_id = ''"
                " OR char_length(capture_evidence_hash) <> 64"
                " OR char_length(consent_snapshot_hash) <> 64"
                " OR char_length(membership_snapshot_hash) <> 64)"
            )
            for row in legacy_incomplete:
                await bootstrap.execute(
                    "UPDATE memory_shared_proposals SET status = 'frozen',"
                    " resolved_at = $2 WHERE proposal_id = $1",
                    row["proposal_id"],
                    datetime.now(UTC),
                )
                await bootstrap.execute(
                    "INSERT INTO memory_audit_events ("
                    " event_id, action, actor_subject_id, subject_id,"
                    " record_id, proposal_id, payload, created_at"
                    ") VALUES ($1, 'proposal.status.migrated', NULL, NULL,"
                    " NULL, $2, $3, $4)"
                    " ON CONFLICT (event_id) DO NOTHING",
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
                )
            # The final promotion receipt belongs to the finalizer action
            # only; the old per-vote column is dropped (fail closed: any
            # legacy confirm row without approval evidence makes the
            # finalizer CAS fail and never promotes).
            await bootstrap.execute(
                """
                ALTER TABLE memory_shared_votes
                    DROP COLUMN IF EXISTS promotion_receipt_id
                """
            )
            # Append-only superseding votes (main review): the legacy
            # PRIMARY KEY (proposal_id, subject_id) cannot hold a
            # confirm->object pair.  Rebuild the key to
            # (proposal_id, subject_id, decision) - idempotent (IF EXISTS)
            # and fail-closed: the old key never silently remains.
            await bootstrap.execute(
                """
                ALTER TABLE memory_shared_votes
                    DROP CONSTRAINT IF EXISTS memory_shared_votes_pkey
                """
            )
            await bootstrap.execute(
                """
                ALTER TABLE memory_shared_votes
                    ADD CONSTRAINT memory_shared_votes_pkey
                    PRIMARY KEY (proposal_id, subject_id, decision)
                """
            )
        finally:
            await bootstrap.close()

    async def _verify_runtime_security(
        self, connection: asyncpg.Connection
    ) -> None:
        """Runtime verification, fail closed (P0-2): the app connection must
        be a non-superuser, non-BYPASSRLS role; every table must have FORCE
        RLS with context-gated policies and no open pass-all policy."""
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
            raise PostgresMemoryStoreContextError(
                "app connection must be a non-superuser, non-BYPASSRLS role; "
                "refusing to initialize"
            )
        if not bool(row["expected_role"]):
            raise PostgresMemoryStoreContextError(
                f"connection role {row['usr']} is not a member of "
                f"{_ROLE_NAMES[self._expected_role]}; refusing to initialize"
            )
        if self._expected_role == "api" and bool(row["is_worker"]):
            raise PostgresMemoryStoreContextError(
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
            raise PostgresMemoryStoreContextError(
                f"RLS tables missing: {sorted(set(_RLS_TABLES) - present)}"
            )
        if not all(bool(t["relrowsecurity"]) and bool(t["relforcerowsecurity"]) for t in tables):
            raise PostgresMemoryStoreContextError(
                "every memory table must have FORCE row-level security"
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
                raise PostgresMemoryStoreContextError(
                    f"open SELECT policy on {policy['tablename']}; refusing to initialize"
                )
            if check is not None and str(check).strip() == "true":
                raise PostgresMemoryStoreContextError(
                    f"open WITH CHECK policy on {policy['tablename']}; "
                    "refusing to initialize"
                )
            text = f"{qual or ''} {check or ''}"
            # P0-2/P0-1: a policy must reference the row-context GUC, the
            # real worker role membership (pg_has_role) or another RLS table
            # (indirect scope: the referenced table's own policies gate the
            # subquery, e.g. memory_status_events -> memory_records).  A
            # bare pass-all policy is rejected above.
            if (
                "app.memory." not in text
                and "pg_has_role" not in text
                and not any(table in text for table in _RLS_TABLES)
            ):
                raise PostgresMemoryStoreContextError(
                    f"policy on {policy['tablename']} does not reference the "
                    "app context or a real role"
                )
        for table in _RLS_TABLES:
            if not by_table.get(table):
                raise PostgresMemoryStoreContextError(
                    f"no RLS policy found on {table}; refusing to initialize"
                )
        # The context mechanism must be usable inside a transaction.
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.memory.service_role', 'memory_worker', true)"
            )
            probe = await connection.fetchval(
                "SELECT current_setting('app.memory.service_role', true)"
            )
            if probe != "memory_worker":
                raise PostgresMemoryStoreContextError(
                    "transaction-level app context is not usable; refusing to initialize"
                )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("store not initialized")
        return self._pool

    @staticmethod
    async def _scope(
        connection: asyncpg.Connection,
        **values: str | int | None,
    ) -> None:
        """Set transaction-local ``app.memory.*`` context values (fail closed
        on empty values).  ``set_config(..., true)`` keeps the context local
        to the current transaction; every operation runs inside one."""
        for key, value in values.items():
            if value is None:
                continue
            text = str(value)
            if not text.strip():
                raise PostgresMemoryStoreContextError(
                    f"app.memory.{key} must not be empty (fail closed)"
                )
            await connection.execute(
                "SELECT set_config('app.memory.' || $1, $2, true)", key, text
            )

    # -- records ---------------------------------------------------------

    async def persist_record(
        self,
        record: MemoryRecord,
        *,
        actor_family_space_id: str | None,
        status_events: tuple[MemoryRecordStatusEvent, ...] = (),
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
        connection: asyncpg.Connection | None = None,
    ) -> None:
        self._require_api()
        pool = self._require_pool()
        if connection is not None:
            # Caller-owned transaction (Policy SensitiveWriteService seam):
            # the caller already opened the transaction and holds the
            # authority locks - write inside it and never commit here.
            await self._persist_record_on(connection, record, actor_family_space_id, status_events, outbox, audit)
            return
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._persist_record_on(connection, record, actor_family_space_id, status_events, outbox, audit)

    async def _persist_record_on(
        self,
        connection: asyncpg.Connection,
        record: MemoryRecord,
        actor_family_space_id: str | None,
        status_events: tuple[MemoryRecordStatusEvent, ...],
        outbox: tuple[MemoryOutboxEvent, ...],
        audit: tuple[MemoryAuditEvent, ...],
    ) -> None:
                await self._scope(
                    connection,
                    actor_subject_id=record.created_by_actor_id,
                    subject_id=record.subject_id,
                    # P1: the family context is the AUTHORITATIVE actor
                    # family scope passed by the service - never derived from
                    # the row the caller wants to write.
                    family_space_id=actor_family_space_id,
                    record_id=record.record_id,
                )
                await connection.execute(
                    "INSERT INTO memory_records (record_id, scope, subject_id,"
                    " resource_owner_id, family_space_id, co_subject_ids,"
                    " source_evidence_ids, policy_receipt_id, promotion_receipt_id,"
                    " promotion_fence_context_hash, approval_evidence_refs,"
                    " consent_snapshot_id,"
                    " memory_type, confidence, retention, retention_expires_at, payload,"
                    " created_by_actor_id, created_at, shared_proposal_id)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,"
                    " $13, $14, $15, $16, $17, $18, $19, $20)",
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
                    record.retention_expires_at,
                    json.dumps(record.payload),
                    record.created_by_actor_id,
                    record.created_at,
                    record.shared_proposal_id,
                )
                await self._insert_status_events(connection, status_events)
                await self._insert_outbox(connection, outbox)
                await self._insert_audit(connection, audit)

    async def sensitive_commit(
        self,
        record: MemoryRecord,
        *,
        actor_family_space_id: str | None,
        connection: asyncpg.Connection,
    ) -> str:
        """Caller-owned transaction sensitive write (cross-domain
        supplement): invokes ONLY the narrow SECURITY DEFINER
        ``memory_sensitive_commit`` function inside the caller's open
        transaction.  The function validates the session-local
        app.memory.* actor/subject/family context against its arguments
        (fail closed) and inserts exactly ONE row - the API role never
        gets table INSERT privileges and never touches Policy/Consent
        tables directly."""
        await self._scope(
            connection,
            actor_subject_id=record.created_by_actor_id,
            subject_id=record.subject_id,
            family_space_id=actor_family_space_id,
            record_id=record.record_id,
        )
        committed = await connection.fetchval(
            "SELECT memory_sensitive_commit("
            " $1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, $10)",
            record.record_id,
            record.scope.value,
            record.subject_id,
            record.resource_owner_id,
            record.family_space_id,
            json.dumps(list(record.co_subject_ids)),
            json.dumps(list(record.source_evidence_ids)),
            record.policy_receipt_id,
            record.consent_snapshot_id,
            record.created_by_actor_id,
        )
        return cast(str, committed)

    async def get_record(
        self,
        record_id: str,
        *,
        actor_subject_id: str,
        actor_family_space_id: str | None = None,
        grant_owner_id: str | None = None,
        grant_scope: str | None = None,
    ) -> MemoryRecord | None:
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=actor_subject_id,
                    family_space_id=actor_family_space_id,
                    grant_owner_id=grant_owner_id,
                    grant_scope=grant_scope,
                    record_id=record_id,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM memory_records WHERE record_id = $1", record_id
                )
                if row is None:
                    return None
                events = await self._status_events_for(connection, record_id)
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
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=actor_subject_id,
                    subject_id=subject_id,
                    family_space_id=actor_family_space_id,
                    grant_owner_id=grant_owner_id,
                    grant_scope=grant_scope,
                )
                rows = await connection.fetch(
                    "SELECT * FROM memory_records WHERE subject_id = $1"
                    " OR resource_owner_id = $1"
                    " OR co_subject_ids @> to_jsonb($1::text)"
                    " ORDER BY created_at",
                    subject_id,
                )
                return await self._records_from_rows(
                    connection, rows, scopes, include_revoked
                )

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
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=actor_subject_id,
                    subject_id=subject_id,
                    family_space_id=actor_family_space_id,
                )
                rows = await connection.fetch(
                    "SELECT * FROM memory_records WHERE family_space_id = $1"
                    " AND (subject_id = $2 OR resource_owner_id = $2"
                    "      OR co_subject_ids @> to_jsonb($2::text))"
                    " ORDER BY created_at",
                    family_space_id,
                    subject_id,
                )
                return await self._records_from_rows(
                    connection, rows, scopes, include_revoked
                )

    async def _records_from_rows(
        self,
        connection: asyncpg.Connection,
        rows: list[asyncpg.Record],
        scopes: tuple[MemoryScope, ...] | None,
        include_revoked: bool,
    ) -> tuple[MemoryRecord, ...]:
        allowed = set(scopes) if scopes is not None else None
        result: list[MemoryRecord] = []
        for row in rows:
            record = _record_from_row(row)
            if allowed is not None and record.scope not in allowed:
                continue
            events = await self._status_events_for(connection, record.record_id)
            derived = _apply_status(record, events)
            if not include_revoked and not derived.is_visible():
                continue
            result.append(derived)
        return tuple(result)

    async def _status_events_for(
        self, connection: asyncpg.Connection, record_id: str
    ) -> tuple[MemoryRecordStatusEvent, ...]:
        rows = await connection.fetch(
            "SELECT * FROM memory_status_events WHERE record_id = $1 ORDER BY created_at",
            record_id,
        )
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

    async def _insert_status_events(
        self, connection: asyncpg.Connection, events: tuple[MemoryRecordStatusEvent, ...]
    ) -> None:
        for event in events:
            await connection.execute(
                "INSERT INTO memory_status_events"
                " (event_id, record_id, status, reason_code, created_at)"
                " VALUES ($1, $2, $3, $4, $5)",
                event.event_id,
                event.record_id,
                event.status,
                event.reason_code,
                event.created_at,
            )

    async def get_status_events(
        self, record_id: str, *, actor_subject_id: str
    ) -> tuple[MemoryRecordStatusEvent, ...]:
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=actor_subject_id,
                    record_id=record_id,
                )
                return await self._status_events_for(connection, record_id)

    # -- proposals ---------------------------------------------------------

    async def persist_proposal(
        self,
        proposal: SharedMemoryProposal,
        *,
        actor_family_space_id: str | None,
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
    ) -> None:
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=proposal.proposer_subject_id,
                    subject_id=proposal.proposer_subject_id,
                    family_space_id=actor_family_space_id,
                    proposal_id=proposal.proposal_id,
                )
                await connection.execute(
                    "INSERT INTO memory_shared_proposals (proposal_id, family_space_id,"
                    " proposer_subject_id, co_subject_ids, binding_version,"
                    " session_id, epoch, binding_id, binding_role, runtime_profile_id,"
                    " device_id, subject_revision, generation_id, turn_id,"
                    " valid_until, fence_context_hash,"
                    " title, content, source_evidence_ids, proposal_policy_receipt_id,"
                    " consent_snapshot_id,"
                    " proposal_revision, capture_evidence_hash,"
                    " consent_snapshot_revision, consent_snapshot_hash,"
                    " membership_snapshot_id, membership_snapshot_revision,"
                    " membership_snapshot_hash, generation, tool_epoch,"
                    " status, created_at, resolved_at)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,"
                    " $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22,"
                    " $23, $24, $25, $26, $27, $28, $29, $30, $31, $32, $33)",
                    proposal.proposal_id,
                    proposal.family_space_id,
                    proposal.proposer_subject_id,
                    json.dumps(list(proposal.co_subject_ids)),
                    proposal.binding_version,
                    proposal.session_id,
                    proposal.epoch,
                    proposal.binding_id,
                    proposal.binding_role,
                    proposal.runtime_profile_id,
                    proposal.device_id,
                    proposal.subject_revision,
                    proposal.generation_id,
                    proposal.turn_id,
                    proposal.valid_until,
                    proposal.fence_context_hash,
                    proposal.title,
                    proposal.content,
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
                    proposal.created_at,
                    proposal.resolved_at,
                )
                await self._insert_outbox(connection, outbox)
                await self._insert_audit(connection, audit)

    async def get_proposal(
        self,
        proposal_id: str,
        *,
        actor_subject_id: str,
        actor_family_space_id: str,
    ) -> SharedMemoryProposal | None:
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=actor_subject_id,
                    family_space_id=actor_family_space_id,
                    proposal_id=proposal_id,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM memory_shared_proposals WHERE proposal_id = $1",
                    proposal_id,
                )
                return _proposal_from_row(row) if row is not None else None

    async def update_proposal_status(
        self,
        proposal_id: str,
        status: ProposalStatus,
        *,
        actor_subject_id: str,
        resolved_at: datetime | None = None,
    ) -> None:
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=actor_subject_id,
                    proposal_id=proposal_id,
                )
                await connection.execute(
                    "UPDATE memory_shared_proposals SET status = $2, resolved_at = $3"
                    " WHERE proposal_id = $1",
                    proposal_id,
                    status,
                    resolved_at,
                )

    async def persist_vote(
        self,
        vote: ConfirmationVote,
        *,
        proposal_status: ProposalStatus | None = None,
        resolved_at: datetime | None = None,
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
    ) -> None:
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=vote.subject_id,
                    subject_id=vote.subject_id,
                    proposal_id=vote.proposal_id,
                )
                try:
                    await connection.execute(
                        "INSERT INTO memory_shared_votes"
                        " (proposal_id, subject_id, decision, voted_at, evidence_id,"
                        " approval_receipt_id, approval_snapshot_id,"
                        " approval_snapshot_revision, approval_snapshot_hash)"
                        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                        vote.proposal_id,
                        vote.subject_id,
                        vote.decision,
                        vote.voted_at,
                        vote.evidence_id,
                        vote.approval_receipt_id,
                        vote.approval_snapshot_id,
                        vote.approval_snapshot_revision,
                        vote.approval_snapshot_hash,
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise AlreadyVotedError(
                        f"vote already recorded for {vote.subject_id}"
                    ) from exc
                if proposal_status is not None:
                    await connection.execute(
                        "UPDATE memory_shared_proposals SET status = $2, resolved_at = $3"
                        " WHERE proposal_id = $1",
                        vote.proposal_id,
                        proposal_status,
                        resolved_at,
                    )
                await self._insert_outbox(connection, outbox)
                await self._insert_audit(connection, audit)

    async def vote_and_transition(
        self,
        vote: ConfirmationVote,
        *,
        actor_family_space_id: str | None,
        audit: tuple[MemoryAuditEvent, ...],
        now: datetime,
    ) -> str:
        """P0-F: authoritative in-transaction transition decision."""
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=vote.subject_id,
                    subject_id=vote.subject_id,
                    family_space_id=actor_family_space_id,
                    proposal_id=vote.proposal_id,
                )
                locked = await connection.fetchrow(
                    "SELECT * FROM memory_shared_proposals"
                    " WHERE proposal_id = $1 FOR UPDATE",
                    vote.proposal_id,
                )
                if locked is None:
                    return "terminal"
                if vote.decision == "confirm" and locked["status"] != "pending":
                    return "terminal"
                if vote.decision == "object" and locked["status"] not in (
                    "pending",
                    "approvals_complete",
                ):
                    return "terminal"
                proposal = _proposal_from_row(locked)
                # P0-6/P0-5: only an authoritative confirmable subject may
                # vote - the caller can never smuggle a vote for someone
                # else or for a tampered proposal view.
                if vote.subject_id not in proposal.all_confirmable_subjects:
                    from services.memory_scope.domain import NotAuthorizedError

                    raise NotAuthorizedError(
                        f"{vote.subject_id} is not a co-subject of this memory"
                    )
                try:
                    await connection.execute(
                        "INSERT INTO memory_shared_votes"
                        " (proposal_id, subject_id, decision, voted_at, evidence_id,"
                        " approval_receipt_id, approval_snapshot_id,"
                        " approval_snapshot_revision, approval_snapshot_hash)"
                        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                        vote.proposal_id,
                        vote.subject_id,
                        vote.decision,
                        vote.voted_at,
                        vote.evidence_id,
                        vote.approval_receipt_id,
                        vote.approval_snapshot_id,
                        vote.approval_snapshot_revision,
                        vote.approval_snapshot_hash,
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise AlreadyVotedError(
                        f"vote already recorded for {vote.subject_id}"
                    ) from exc
                vote_rows = await connection.fetch(
                    "SELECT subject_id, decision FROM memory_shared_votes"
                    " WHERE proposal_id = $1",
                    vote.proposal_id,
                )
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
                    await connection.execute(
                        "UPDATE memory_shared_proposals SET status = 'frozen',"
                        " resolved_at = $2 WHERE proposal_id = $1",
                        vote.proposal_id,
                        now,
                    )
                    await self._insert_terminal_outbox(
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
                    # Vote-acceptance transaction (three authority actions (proposal / per-vote approval / final promotion)): the
                    # vote is APPENDED but NOT promoted here.  The full
                    # approval set now exists, so the proposal reports
                    # ``all_confirmed``; a separate promotion finalizer
                    # requests a DISTINCT fresh
                    # family_shared_memory_promotion receipt bound to the
                    # exact vote/approval revisions and performs the CAS
                    # promotion (at most one record).
                    result_status = "approvals_complete"
                    await connection.execute(
                        "UPDATE memory_shared_proposals"
                        " SET status = 'approvals_complete',"
                        " resolved_at = $2 WHERE proposal_id = $1",
                        vote.proposal_id,
                        now,
                    )
                else:
                    result_status = "pending"
                for event in audit:
                    await connection.execute(
                        "INSERT INTO memory_audit_events (event_id, action,"
                        " actor_subject_id, subject_id, record_id, proposal_id,"
                        " payload, created_at)"
                        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                        event.event_id,
                        event.action,
                        event.actor_subject_id,
                        event.subject_id,
                        event.record_id,
                        event.proposal_id,
                        json.dumps(event.payload),
                        event.created_at,
                    )
                return result_status

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
        """Fail-closed freeze (main architecture review): pending ->
        frozen in ONE transaction with stable audit/outbox events; a
        terminal proposal is an idempotent no-op."""
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=actor_subject_id,
                    family_space_id=family_space_id,
                    proposal_id=proposal_id,
                )
                locked = await connection.fetchrow(
                    "SELECT status FROM memory_shared_proposals"
                    " WHERE proposal_id = $1 FOR UPDATE",
                    proposal_id,
                )
                if locked is None or locked["status"] not in (
                    "pending",
                    "approvals_complete",
                ):
                    return False
                await connection.execute(
                    "UPDATE memory_shared_proposals SET status = 'frozen',"
                    " resolved_at = $2 WHERE proposal_id = $1",
                    proposal_id,
                    now,
                )
                for event in audit:
                    try:
                        await connection.execute(
                            "INSERT INTO memory_audit_events (event_id, action,"
                            " actor_subject_id, subject_id, record_id, proposal_id,"
                            " payload, created_at)"
                            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                            event.event_id,
                            event.action,
                            event.actor_subject_id,
                            event.subject_id,
                            event.record_id,
                            event.proposal_id,
                            json.dumps(event.payload),
                            event.created_at,
                        )
                    except asyncpg.UniqueViolationError:
                        pass
                for out_event in outbox:
                    try:
                        await connection.execute(
                            "INSERT INTO memory_outbox (outbox_id, event_id, topic,"
                            " payload, status, created_at)"
                            " VALUES ($1, $2, $3, $4, 'pending', $5)",
                            out_event.outbox_id,
                            out_event.event_id,
                            out_event.topic,
                            json.dumps(out_event.payload),
                            out_event.created_at,
                        )
                    except asyncpg.UniqueViolationError:
                        pass
                return True

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
        """Promotion finalizer CAS: re-lock proposal + vote set; promote
        only when still pending with the full confirm set and no objection.
        Persists the fresh promotion receipt id + fence hash with the
        record; concurrent finalizers serialize on the proposal row lock so
        at most one record is produced."""
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=actor_subject_id,
                    subject_id=actor_subject_id,
                    family_space_id=family_space_id,
                    proposal_id=proposal_id,
                )
                locked = await connection.fetchrow(
                    "SELECT * FROM memory_shared_proposals"
                    " WHERE proposal_id = $1 FOR UPDATE",
                    proposal_id,
                )
                # Canonical lifecycle: durable approvals_complete ->
                # promoted.  A pending proposal is never promoted directly
                # (the vote-acceptance transaction owns the
                # pending -> approvals_complete jump).
                if locked is None or locked["status"] != "approvals_complete":
                    return False
                proposal = _proposal_from_row(locked)
                vote_rows = await connection.fetch(
                    "SELECT subject_id, decision, approval_receipt_id,"
                    " approval_snapshot_id, approval_snapshot_revision,"
                    " approval_snapshot_hash"
                    " FROM memory_shared_votes WHERE proposal_id = $1",
                    proposal_id,
                )
                confirmable = set(proposal.all_confirmable_subjects)
                # Append-only superseding votes: the LATEST decision per
                # subject wins.
                latest_by_subject: dict[str, asyncpg.Record] = {}
                for row in vote_rows:
                    latest_by_subject[row["subject_id"]] = row
                objectors = {
                    subject
                    for subject, row in latest_by_subject.items()
                    if row["decision"] == "object"
                }
                confirmers = {
                    subject
                    for subject, row in latest_by_subject.items()
                    if row["decision"] == "confirm"
                }
                if objectors or confirmers != confirmable:
                    return False
                # CAS: the persisted per-vote approval evidence must match
                # the EXACT expected revisions - any drift / missing /
                # empty approval fails closed and produces NO record.
                persisted_approvals = tuple(
                    sorted(
                        (
                            row["subject_id"],
                            row["approval_snapshot_id"],
                            row["approval_snapshot_revision"],
                            row["approval_snapshot_hash"],
                        )
                        for row in latest_by_subject.values()
                        if row["decision"] == "confirm"
                    )
                )
                expected_approvals = tuple(sorted(approval_revisions))
                if persisted_approvals != expected_approvals or any(
                    not snapshot_id
                    for _, snapshot_id, _, _ in persisted_approvals
                ):
                    return False
                await self._scope(
                    connection, subject_id=proposal.proposer_subject_id
                )
                await connection.execute(
                    "UPDATE memory_shared_proposals SET status = 'promoted',"
                    " resolved_at = $2 WHERE proposal_id = $1",
                    proposal_id,
                    now,
                )
                record_id = str(uuid.uuid4())
                inserted = await connection.fetchval(
                    """
                    INSERT INTO memory_records (
                        record_id, scope, subject_id, resource_owner_id,
                        family_space_id, co_subject_ids, source_evidence_ids,
                        policy_receipt_id, promotion_receipt_id,
                        promotion_fence_context_hash, approval_evidence_refs,
                        consent_snapshot_id,
                        memory_type, confidence, retention, retention_expires_at,
                        payload, created_by_actor_id, created_at, shared_proposal_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                              $13, $14, $15, $16, $17, $18, $19, $20)
                    ON CONFLICT (shared_proposal_id)
                    WHERE shared_proposal_id IS NOT NULL DO NOTHING
                    RETURNING record_id
                    """,
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
                                    row["subject_id"],
                                    row["approval_receipt_id"],
                                    row["approval_snapshot_id"],
                                    row["approval_snapshot_revision"],
                                    row["approval_snapshot_hash"],
                                )
                                for row in latest_by_subject.values()
                                if row["decision"] == "confirm"
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
                    now,
                    proposal_id,
                )
                if inserted is None:
                    return False
                await connection.execute(
                    "INSERT INTO memory_status_events"
                    " (event_id, record_id, status, reason_code, created_at)"
                    " VALUES ($1, $2, 'confirmed',"
                    " 'all_co_subjects_confirmed', $3)",
                    f"{record_id}:confirmed",
                    record_id,
                    now,
                )
                await self._insert_terminal_outbox(
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
                for event in audit:
                    # ON CONFLICT is NOT usable here: PostgreSQL raises
                    # permission denied when the conflict-detection read is
                    # hidden by the worker-only SELECT policy, so the unique
                    # violation is swallowed instead (stable event ids make
                    # replays no-ops; the API keeps INSERT-only access).
                    try:
                        await connection.execute(
                            "INSERT INTO memory_audit_events (event_id, action,"
                            " actor_subject_id, subject_id, record_id, proposal_id,"
                            " payload, created_at)"
                            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                            event.event_id,
                            event.action,
                            event.actor_subject_id,
                            event.subject_id,
                            event.record_id,
                            event.proposal_id,
                            json.dumps(event.payload),
                            event.created_at,
                        )
                    except asyncpg.UniqueViolationError:
                        pass
                return True

    @staticmethod
    async def _insert_terminal_outbox(
        connection: asyncpg.Connection,
        *,
        event_id: str,
        topic: str,
        payload: dict[str, object],
        now: datetime,
    ) -> None:
        # ON CONFLICT is NOT usable from the API role: the conflict read is
        # hidden by the worker-only outbox SELECT policy (permission
        # denied), so the unique violation is swallowed instead (stable
        # event ids make replays no-ops).
        try:
            await connection.execute(
                "INSERT INTO memory_outbox (outbox_id, event_id, topic, payload,"
                " status, created_at)"
                " VALUES ($1, $1, $2, $3, 'pending', $4)",
                event_id,
                topic,
                json.dumps(payload),
                now,
            )
        except asyncpg.UniqueViolationError:
            pass

    async def list_votes(
        self, proposal_id: str, *, actor_subject_id: str
    ) -> tuple[ConfirmationVote, ...]:
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=actor_subject_id,
                    proposal_id=proposal_id,
                )
                rows = await connection.fetch(
                    "SELECT * FROM memory_shared_votes WHERE proposal_id = $1"
                    " ORDER BY voted_at",
                    proposal_id,
                )
                return tuple(
                    ConfirmationVote(
                        proposal_id=row["proposal_id"],
                        subject_id=row["subject_id"],
                        decision=cast(VoteDecision, row["decision"]),
                        voted_at=_parse_iso(row["voted_at"]) or _now(),
                        evidence_id=row["evidence_id"],
                        approval_receipt_id=row["approval_receipt_id"],
                        approval_snapshot_id=row["approval_snapshot_id"],
                        approval_snapshot_revision=row[
                            "approval_snapshot_revision"
                        ],
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
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=actor_subject_id,
                    subject_id=subject_id,
                    family_space_id=actor_family_space_id,
                )
                rows = await connection.fetch(
                    "SELECT * FROM memory_shared_proposals ORDER BY created_at"
                )
                allowed = set(statuses) if statuses is not None else None
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
        self._require_api()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(
                    connection,
                    actor_subject_id=actor_subject_id,
                    proposal_id=proposal_id,
                    record_id=record_id,
                )
                await connection.execute(
                    "UPDATE memory_shared_proposals SET status = 'withdrawn',"
                    " resolved_at = $2 WHERE proposal_id = $1",
                    proposal_id,
                    _now(),
                )
                await self._insert_status_events(connection, status_events)
                await self._insert_outbox(connection, outbox)
                await self._insert_audit(connection, audit)

    # -- outbox / audit ---------------------------------------------------

    async def _insert_outbox(
        self, connection: asyncpg.Connection, events: tuple[MemoryOutboxEvent, ...]
    ) -> None:
        for event in events:
            await connection.execute(
                "INSERT INTO memory_outbox (outbox_id, event_id, topic, payload, status, created_at)"
                " VALUES ($1, $2, $3, $4, 'pending', $5)",
                event.outbox_id,
                event.event_id,
                event.topic,
                json.dumps(event.payload),
                event.created_at,
            )

    async def _insert_audit(
        self, connection: asyncpg.Connection, events: tuple[MemoryAuditEvent, ...]
    ) -> None:
        for event in events:
            await connection.execute(
                "INSERT INTO memory_audit_events (event_id, action, actor_subject_id,"
                " subject_id, record_id, proposal_id, payload, created_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                event.event_id,
                event.action,
                event.actor_subject_id,
                event.subject_id,
                event.record_id,
                event.proposal_id,
                json.dumps(event.payload),
                event.created_at,
            )

    async def append_outbox(self, event: MemoryOutboxEvent) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                await self._insert_outbox(connection, (event,))

    async def append_audit(self, event: MemoryAuditEvent) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                await self._insert_audit(connection, (event,))

    async def list_pending_outbox(self, limit: int = 100) -> tuple[MemoryOutboxEvent, ...]:
        self._require_worker()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                rows = await connection.fetch(
                    "SELECT * FROM memory_outbox WHERE status = 'pending'"
                    " ORDER BY created_at LIMIT $1",
                    limit,
                )
                return tuple(
                    MemoryOutboxEvent(
                        outbox_id=row["outbox_id"],
                        event_id=row["event_id"],
                        topic=row["topic"],
                        payload=_jsonb_dict(row["payload"]),
                        created_at=_parse_iso(row["created_at"]) or _now(),
                    )
                    for row in rows
                )

    async def mark_outbox_processed(self, outbox_id: str) -> None:
        self._require_worker()
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._scope(connection)
                await connection.execute(
                    "UPDATE memory_outbox SET status = 'processed' WHERE outbox_id = $1",
                    outbox_id,
                )


def _record_from_row(row: asyncpg.Record) -> MemoryRecord:
    return MemoryRecord(
        record_id=row["record_id"],
        scope=MemoryScope.from_value(row["scope"]) or MemoryScope.MEMORY_SCOPE_UNKNOWN,
        subject_id=row["subject_id"],
        resource_owner_id=row["resource_owner_id"],
        family_space_id=row["family_space_id"],
        co_subject_ids=tuple(_jsonb_list(row["co_subject_ids"])),
        source_evidence_ids=tuple(_jsonb_list(row["source_evidence_ids"])),
        policy_receipt_id=row["policy_receipt_id"],
        promotion_receipt_id=row["promotion_receipt_id"],
        promotion_fence_context_hash=row["promotion_fence_context_hash"],
        approval_evidence_refs=tuple(
            tuple(pair) for pair in _jsonb_list(row["approval_evidence_refs"])
        ),
        consent_snapshot_id=row["consent_snapshot_id"],
        memory_type=row["memory_type"],
        confidence=float(row["confidence"]),
        retention=cast(RetentionPolicy, row["retention"]),
        retention_expires_at=row["retention_expires_at"],
        payload=_jsonb_dict(row["payload"]),
        created_by_actor_id=row["created_by_actor_id"],
        created_at=row["created_at"],
        shared_proposal_id=row["shared_proposal_id"],
    )


def _proposal_from_row(row: asyncpg.Record) -> SharedMemoryProposal:
    return SharedMemoryProposal(
        proposal_id=row["proposal_id"],
        family_space_id=row["family_space_id"],
        proposer_subject_id=row["proposer_subject_id"],
        co_subject_ids=tuple(_jsonb_list(row["co_subject_ids"])),
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
        valid_until=row["valid_until"],
        fence_context_hash=row["fence_context_hash"],
        title=row["title"],
        content=row["content"],
        source_evidence_ids=tuple(_jsonb_list(row["source_evidence_ids"])),
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
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
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
