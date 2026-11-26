"""PostgreSQL / FORCE-RLS persistence for the identity domain.

Production authority (section 11.7 / PR-17).  The schema is shipped in
``postgres_schema.sql``: constraints mirror the domain rules, every table is
FORCE RLS, versioned binding records are immutable after insert, and audit /
outbox rows are written by the same adapter.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import TypeVar, cast

import asyncpg
from services.identity.domain import (
    BindingVersionConflictError,
    CustomPersonaLimitError,
    CustomPersonaRecord,
    DeviceBinding,
    IdempotencyRecord,
    IdentityAccessDeniedError,
    IdentityConflictError,
    IdentityNotFoundError,
    PersonaAssignmentRecord,
    PersonSubject,
    TransferIntent,
    person_from_dict,
)
from services.identity.postgres_bindings import (
    PostgresIdentityBindingMixin,
    _insert_binding,
    _lock_binding_subjects,
)
from services.identity.postgres_helpers import _timestamp
from services.identity.postgres_relationships import (
    PostgresIdentityRelationshipMixin,
)
from services.identity.postgres_rows import (
    _custom_persona,
    _from_db,
    _jsonb_object,
    _person,
    _persona_assignment,
    _transfer_intent,
)
from services.identity.postgres_schema_bootstrap import (
    PostgresIdentitySchemaMixin,
)
from services.identity.repository import AuditEvent, OutboxEvent

_IdentityEvent = TypeVar("_IdentityEvent", AuditEvent, OutboxEvent)


_REGISTRATION_CONFLICT_SQLSTATE = "II001"

class PostgresIdentityStore(
    PostgresIdentitySchemaMixin,
    PostgresIdentityRelationshipMixin,
    PostgresIdentityBindingMixin,
):
    """PostgreSQL implementation of the ``IdentityStore`` protocol."""

    def __init__(self, dsn: str, registration_dsn: str | None = None) -> None:
        self._dsn = dsn
        self._registration_dsn = registration_dsn
        self._pool: asyncpg.Pool | None = None
        self._registration_pool: asyncpg.Pool | None = None
        self._initialize_lock = asyncio.Lock()


    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        if self._registration_pool is not None:
            await self._registration_pool.close()
            self._registration_pool = None

    async def _ready(self) -> asyncpg.Pool:
        if self._pool is None:
            await self.initialize()
        return cast(asyncpg.Pool, self._pool)

    async def _registration_ready(self) -> asyncpg.Pool:
        if self._registration_pool is None and self._pool is None:
            await self.initialize()
        if self._registration_pool is None:
            raise RuntimeError(
                "PostgresIdentityStore registration authority is not "
                "configured (registration_dsn)"
            )
        return self._registration_pool

    @staticmethod
    async def _apply_context(
        connection: asyncpg.Connection,
        *,
        actor_person_id: str | None,
        scope: str,
    ) -> None:
        """Set the transaction-local RLS context (FORCE RLS, section 11.7)."""
        await connection.execute(
            "SELECT set_config('app.identity_actor', $1, true)",
            actor_person_id or "",
        )
        await connection.execute(
            "SELECT set_config('app.identity_scope', $1, true)", scope
        )

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
        # The port is an atomic compare-or-insert: it persists person + audit
        # + outbox atomically with fixed action/topic and derived event ids,
        # and an exact canonical replay returns the persisted DB row while any
        # content drift raises a conflict (the registration role has no
        # standalone unrestricted read port).
        if audit_event is None:
            raise RuntimeError(
                "identity registration requires an audit event "
                "(person + audit + outbox are written atomically)"
            )
        pool = await self._registration_ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                try:
                    raw = await connection.fetchval(
                        """
                        SELECT identity_register_person(
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            $11, $12, $13
                        )
                        """,
                        person.person_id,
                        person.display_name,
                        person.subject_category,
                        person.age_band,
                        person.age_evidence_status,
                        person.locale,
                        person.timezone,
                        person.status,
                        _timestamp(person.created_at, field="created_at"),
                        _timestamp(person.updated_at, field="updated_at"),
                        audit_event.actor_person_id,
                        json.dumps(audit_event.payload, ensure_ascii=False),
                        evidence_id,
                    )
                except asyncpg.PostgresError as exc:
                    if exc.sqlstate == _REGISTRATION_CONFLICT_SQLSTATE:
                        raise IdentityConflictError(
                            f"person {person.person_id} already registered "
                            "with different authoritative fields"
                        ) from exc
                    raise
        if raw is None:
            raise RuntimeError(
                f"person {person.person_id} registration did not return a row"
            )
        decoded = _jsonb_object(raw, field="registration_snapshot")
        return person_from_dict(decoded)

    async def save_person(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        outbox_event: OutboxEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None:
        await self.register_person(
            person,
            audit_event=audit_event,
            outbox_event=outbox_event,
            actor_person_id=actor_person_id,
            scope=scope,
        )

    async def declare_age_evidence(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None:
        # Age DECLARATION port: self or an active guardian (validity window
        # re-verified inside the database); declarations can never claim
        # adult/verified and never touch profile/status fields.
        if actor_person_id is None:
            raise IdentityAccessDeniedError(
                "age declarations require an authenticated actor"
            )
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                await connection.execute(
                    """
                    SELECT identity_declare_age_evidence(
                        $1, $2, $3, $4
                    )
                    """,
                    person.person_id,
                    person.age_band,
                    _timestamp(person.updated_at, field="updated_at"),
                    actor_person_id,
                )

    async def verify_age_evidence(
        self,
        person: PersonSubject,
        *,
        evidence_id: str,
        verifier_person_id: str,
        actor_person_id: str | None = None,
        scope: str = "registration",
    ) -> None:
        # Authoritative age verification: executed as the dedicated
        # registration role through the action-level port which requires a
        # non-empty evidence id and a distinct verified-adult verifier; the
        # evidence-carrying audit/outbox trail is written in the same
        # transaction by the port.
        pool = await self._registration_ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    SELECT identity_verify_age_evidence(
                        $1, $2, $3, $4, $5, $6
                    )
                    """,
                    person.person_id,
                    person.age_band,
                    person.age_evidence_status,
                    evidence_id,
                    verifier_person_id,
                    _timestamp(person.updated_at, field="updated_at"),
                )

    async def reconcile_account_registration(
        self, person: PersonSubject, *, expected_updated_at: datetime,
        evidence_id: str, source_revision: int,
        audit_event: AuditEvent, outbox_event: OutboxEvent,
    ) -> None:
        pool = await self._registration_ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                try:
                    await connection.execute(
                        "SELECT identity_reconcile_account_registration($1, $2, $3, $4, $5)",
                        person.person_id, evidence_id, source_revision,
                        _timestamp(expected_updated_at, field="expected_updated_at"),
                        _timestamp(person.updated_at, field="updated_at"),
                    )
                except asyncpg.PostgresError as exc:
                    if exc.sqlstate == _REGISTRATION_CONFLICT_SQLSTATE:
                        raise IdentityConflictError(
                            "registration changed or has protected evidence"
                        ) from exc
                    raise

    async def update_person_profile(
        self,
        person: PersonSubject,
        *,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> None:
        # Minimal self-profile port: display_name/locale/timezone only, and
        # only by the person themselves.  Age/status fields are not writable.
        if actor_person_id is None or actor_person_id != person.person_id:
            raise IdentityAccessDeniedError(
                "profile updates are self-service only"
            )
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                await connection.execute(
                    """
                    SELECT identity_self_update_profile(
                        $1, $2, $3, $4, $5, $6
                    )
                    """,
                    person.person_id,
                    person.display_name,
                    person.locale,
                    person.timezone,
                    _timestamp(person.updated_at, field="updated_at"),
                    actor_person_id,
                )

    async def redact_bound_subject(
        self,
        *,
        person_id: str,
        display_name: str,
        updated_at: datetime,
        audit_event: AuditEvent,
        actor_person_id: str,
    ) -> None:
        # The function re-checks the attestation and the absence of a live
        # binding under the owner's RLS context, and writes its own audit.
        del audit_event
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope="api"
                )
                await connection.execute(
                    "SELECT identity_redact_bound_subject($1, $2, $3, $4)",
                    person_id,
                    actor_person_id,
                    display_name,
                    _timestamp(updated_at, field="updated_at"),
                )

    async def _delete_identity_rows(
        self,
        *,
        function: str,
        args: tuple[object, ...],
        actor_person_id: str,
    ) -> dict[str, int]:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope="api"
                )
                # The BEFORE DELETE guard stays closed by default. The port
                # opens it inside its own body after verifying the actor, so
                # this caller never touches the flag.
                raw = await connection.fetchval(
                    f"SELECT {function}({', '.join(f'${index}' for index in range(1, len(args) + 1))})",
                    *args,
                )
        counts = _jsonb_object(raw, field="identity deletion counts")
        return {
            str(key): int(value)
            for key, value in counts.items()
            if key not in {"binding_refs"} and isinstance(value, (int, str))
        }

    async def delete_account_identity(
        self,
        *,
        account_person_id: str,
        display_name: str,
        actor_person_id: str,
    ) -> dict[str, int]:
        return await self._delete_identity_rows(
            function="identity_delete_account_identity",
            args=(account_person_id, display_name),
            actor_person_id=actor_person_id,
        )

    async def remaining_account_identity(
        self, *, account_person_id: str
    ) -> dict[str, int]:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=account_person_id, scope="api"
                )
                exists = await connection.fetchval(
                    "SELECT identity_person_exists($1)", account_person_id
                )
        return {"persons": 1} if exists else {}

    async def get_person(
        self,
        person_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> PersonSubject | None:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                row = await connection.fetchrow(
                    "SELECT * FROM identity_persons WHERE person_id = $1", person_id
                )
            return _person(row) if row is not None else None

    async def person_exists(self, person_id: str) -> bool:
        pool = await self._ready()
        async with pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    "SELECT identity_person_exists($1)", person_id
                )
            )

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
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                if idempotency_record is not None:
                    await _insert_idempotency_record(
                        connection, idempotency_record
                    )
                if expected_updated_at is not None:
                    existing = await connection.fetchrow(
                        "SELECT updated_at FROM identity_transfer_intents "
                        "WHERE transfer_id = $1 FOR UPDATE",
                        intent.transfer_id,
                    )
                    if existing is None:
                        raise IdentityNotFoundError(
                            f"transfer {intent.transfer_id} does not exist"
                        )
                    if cast(datetime, existing["updated_at"]) != expected_updated_at:
                        raise IdentityConflictError(
                            f"transfer {intent.transfer_id} changed concurrently"
                        )
                    await _update_transfer_intent(connection, intent)
                else:
                    try:
                        await _insert_transfer_intent(connection, intent)
                    except asyncpg.UniqueViolationError as exc:
                        raise IdentityConflictError(
                            f"transfer {intent.transfer_id} already exists"
                        ) from exc
    async def get_transfer_intent(
        self,
        transfer_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> TransferIntent | None:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                row = await connection.fetchrow(
                    "SELECT * FROM identity_transfer_intents WHERE transfer_id = $1",
                    transfer_id,
                )
            return _transfer_intent(row) if row is not None else None

    async def list_transfer_intents(
        self,
        device_id: str,
        statuses: tuple[str, ...] | None = None,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[TransferIntent, ...]:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                if statuses is None:
                    rows = await connection.fetch(
                        """
                        SELECT * FROM identity_transfer_intents
                        WHERE device_id = $1 ORDER BY created_at
                        """,
                        device_id,
                    )
                else:
                    rows = await connection.fetch(
                        """
                        SELECT * FROM identity_transfer_intents
                        WHERE device_id = $1 AND status = ANY($2::text[])
                        ORDER BY created_at
                        """,
                        device_id,
                        list(statuses),
                    )
            return tuple(_transfer_intent(row) for row in rows)

    async def scan_transfer_intents(
        self,
        statuses: tuple[str, ...] | None = None,
        *,
        scope: str = "api",
    ) -> tuple[TransferIntent, ...]:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=None, scope=scope
                )
                if statuses is None:
                    rows = await connection.fetch(
                        "SELECT * FROM identity_transfer_intents ORDER BY created_at"
                    )
                else:
                    rows = await connection.fetch(
                        """
                        SELECT * FROM identity_transfer_intents
                        WHERE status = ANY($1::text[])
                        ORDER BY created_at
                        """,
                        list(statuses),
                    )
            return tuple(_transfer_intent(row) for row in rows)

    async def get_idempotency_record(
        self,
        scope_key: str,
        idempotency_key: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> IdempotencyRecord | None:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                row = await connection.fetchrow(
                    """
                    SELECT scope_key, idempotency_key, operation, content_hash,
                           result_payload_json, created_at
                    FROM identity_idempotency_records
                    WHERE scope_key = $1 AND idempotency_key = $2
                    """,
                    scope_key,
                    idempotency_key,
                )
            if row is None:
                return None
            return IdempotencyRecord(
                scope_key=str(row["scope_key"]),
                idempotency_key=str(row["idempotency_key"]),
                operation=str(row["operation"]),  # type: ignore[arg-type]
                content_hash=str(row["content_hash"]),
                result_payload=_jsonb_object(
                    row["result_payload_json"], field="result_payload_json"
                ),
                created_at=_from_db(row["created_at"]) or datetime.now(UTC),
            )

    async def save_idempotency_record(
        self,
        record: IdempotencyRecord,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> bool:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                return await _insert_idempotency_record(connection, record)

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
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                if idempotency_record is not None:
                    await _insert_idempotency_record(
                        connection, idempotency_record
                    )
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    binding.device_id,
                )
                await _lock_binding_subjects(connection, binding, previous_binding_id)
                stored = await connection.fetchrow(
                    "SELECT * FROM identity_transfer_intents WHERE transfer_id = $1",
                    intent.transfer_id,
                )
                if stored is None:
                    raise IdentityNotFoundError(
                        f"transfer {intent.transfer_id} does not exist"
                    )
                if str(stored["status"]) != "pending":
                    raise RuntimeError(
                        f"transfer {intent.transfer_id} is not pending "
                        f"(status={stored['status']})"
                    )
                previous = await connection.fetchrow(
                    "SELECT * FROM identity_device_bindings WHERE binding_id = $1",
                    previous_binding_id,
                )
                if previous is None:
                    raise IdentityNotFoundError(
                        f"binding {previous_binding_id} to supersede does not exist"
                    )
                if str(previous["device_id"]) != binding.device_id:
                    raise BindingVersionConflictError(
                        "superseding binding must target the same device"
                    )
                if str(previous["status"]) != "active":
                    raise BindingVersionConflictError(
                        "only an active binding can be superseded"
                    )
                version = int(previous["binding_version"]) + 1
                version_exists = await connection.fetchval(
                    """
                    SELECT 1 FROM identity_device_bindings
                    WHERE device_id = $1 AND binding_version = $2
                    """,
                    binding.device_id,
                    version,
                )
                if version_exists:
                    raise BindingVersionConflictError(
                        f"binding version {version} already exists for "
                        f"device {binding.device_id}"
                    )
                valid_until = binding.valid_from
                if previous["valid_until"] is not None:
                    previous_until = cast(datetime, previous["valid_until"])
                    if previous_until < valid_until:
                        valid_until = previous_until
                await connection.execute(
                    """
                    UPDATE identity_device_bindings
                    SET status = 'superseded', valid_until = $1
                    WHERE binding_id = $2
                    """,
                    valid_until,
                    previous_binding_id,
                )
                await connection.execute(
                    """
                    UPDATE identity_device_binding_roles
                    SET status = 'superseded', ended_at = $1
                    WHERE binding_id = $2 AND status = 'active'
                    """,
                    binding.valid_from,
                    previous_binding_id,
                )
                try:
                    await _insert_binding(connection, binding, version)
                except asyncpg.UniqueViolationError as exc:
                    raise BindingVersionConflictError(
                        f"binding version {version} already exists for "
                        f"device {binding.device_id}"
                    ) from exc
                await _update_transfer_intent(connection, intent)
                if idempotency_record is not None and "binding_version" in (
                    idempotency_record.result_payload
                ):
                    result_payload = dict(idempotency_record.result_payload)
                    result_payload["binding_version"] = version
                    await connection.execute(
                        """
                        SELECT identity_patch_idempotency_result(
                            $1, $2, $3::jsonb
                        )
                        """,
                        idempotency_record.scope_key,
                        idempotency_record.idempotency_key,
                        json.dumps(
                            result_payload, ensure_ascii=False, sort_keys=True
                        ),
                    )
                return replace(binding, binding_version=version)

    async def append_audit(self, event: AuditEvent) -> None:
        raise RuntimeError(
            "PostgresIdentityStore.append_audit is not available: audit rows "
            "are synthesized by the business mutation triggers"
        )

    async def enqueue_outbox(self, event: OutboxEvent) -> None:
        raise RuntimeError(
            "PostgresIdentityStore.enqueue_outbox is not available: outbox "
            "events are synthesized by the business mutation triggers"
        )

    async def upsert_persona_assignment(
        self,
        record: PersonaAssignmentRecord,
        *,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> PersonaAssignmentRecord:
        """Insert-or-repoint one subject override.

        ``audit_event`` is accepted for protocol parity but never written
        directly: the API role holds no INSERT on ``identity_audit_events``,
        so the row is synthesized by ``identity_persona_assignments_audit_trigger``.
        """
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                existing = await connection.fetchrow(
                    """
                    SELECT * FROM identity_persona_assignments
                    WHERE binding_id = $1 AND subject_id = $2
                    FOR UPDATE
                    """,
                    record.binding_id,
                    record.subject_id,
                )
                if (
                    existing is not None
                    and str(existing["assignment_id"]) == record.assignment_id
                ):
                    # Idempotent replay: the persisted row wins verbatim.
                    return _persona_assignment(existing)
                persisted = (
                    replace(
                        record,
                        created_at=cast(datetime, existing["created_at"]),
                    )
                    if existing is not None
                    else record
                )
                row = await connection.fetchrow(
                    """
                    INSERT INTO identity_persona_assignments (
                        binding_id, subject_id, assignment_id, persona_id,
                        persona_version, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (binding_id, subject_id) DO UPDATE SET
                        assignment_id = excluded.assignment_id,
                        persona_id = excluded.persona_id,
                        persona_version = excluded.persona_version,
                        updated_at = excluded.updated_at
                    RETURNING *
                    """,
                    persisted.binding_id,
                    persisted.subject_id,
                    persisted.assignment_id,
                    persisted.persona_id,
                    persisted.persona_version,
                    _timestamp(persisted.created_at, field="created_at"),
                    _timestamp(persisted.updated_at, field="updated_at"),
                )
                assert row is not None
                return _persona_assignment(row)

    async def get_persona_assignment(
        self,
        *,
        binding_id: str,
        subject_id: str,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> PersonaAssignmentRecord | None:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                row = await connection.fetchrow(
                    """
                    SELECT * FROM identity_persona_assignments
                    WHERE binding_id = $1 AND subject_id = $2
                    """,
                    binding_id,
                    subject_id,
                )
                return _persona_assignment(row) if row is not None else None

    async def list_persona_assignments(
        self,
        *,
        binding_id: str,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[PersonaAssignmentRecord, ...]:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                rows = await connection.fetch(
                    """
                    SELECT * FROM identity_persona_assignments
                    WHERE binding_id = $1
                    ORDER BY subject_id
                    """,
                    binding_id,
                )
                return tuple(_persona_assignment(row) for row in rows)

    async def delete_persona_assignment(
        self,
        *,
        binding_id: str,
        subject_id: str,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> bool:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                deleted = await connection.fetchval(
                    """
                    DELETE FROM identity_persona_assignments
                    WHERE binding_id = $1 AND subject_id = $2
                    RETURNING 1
                    """,
                    binding_id,
                    subject_id,
                )
                return deleted is not None

    async def save_custom_persona(
        self,
        record: CustomPersonaRecord,
        *,
        max_per_owner: int | None = None,
        idempotency_record: IdempotencyRecord | None = None,
        audit_event: AuditEvent | None = None,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> CustomPersonaRecord:
        """Insert one immutable v1 row under an owner-level advisory lock.

        The advisory lock serialises the per-owner count check with the
        insert, and the audit row is synthesized by
        ``identity_custom_personas_audit_trigger`` (the API role holds no
        INSERT on ``identity_audit_events``).
        """
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    record.owner_person_id,
                )
                if idempotency_record is not None:
                    await _insert_idempotency_record(connection, idempotency_record)
                if max_per_owner is not None:
                    owned = int(
                        await connection.fetchval(
                            """
                            SELECT count(*) FROM identity_custom_personas
                            WHERE owner_person_id = $1
                            """,
                            record.owner_person_id,
                        )
                        or 0
                    )
                    if owned >= max_per_owner:
                        raise CustomPersonaLimitError(max_per_owner)
                try:
                    row = await connection.fetchrow(
                        """
                        INSERT INTO identity_custom_personas (
                            persona_id, owner_person_id, display_name,
                            style_description, warmth, directness,
                            response_length, question_frequency,
                            interview_depth, welcome_text,
                            conversation_instruction, voice_instruction,
                            default_voice_emotion, default_voice_rate,
                            fallback_designed_voice, persona_version, source,
                            created_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                            $13, $14, $15, $16, $17, $18
                        )
                        RETURNING *
                        """,
                        record.persona_id,
                        record.owner_person_id,
                        record.display_name,
                        record.style_description,
                        record.warmth,
                        record.directness,
                        record.response_length,
                        record.question_frequency,
                        record.interview_depth,
                        record.welcome_text,
                        record.conversation_instruction,
                        record.voice_instruction,
                        record.default_voice_emotion,
                        record.default_voice_rate,
                        record.fallback_designed_voice,
                        record.persona_version,
                        record.source,
                        _timestamp(record.created_at, field="created_at"),
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise IdentityConflictError(
                        f"custom persona {record.persona_id} already exists"
                    ) from exc
                assert row is not None
                return _custom_persona(row)

    async def get_custom_persona(
        self,
        persona_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> CustomPersonaRecord | None:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                row = await connection.fetchrow(
                    "SELECT * FROM identity_custom_personas WHERE persona_id = $1",
                    persona_id,
                )
                return _custom_persona(row) if row is not None else None

    async def list_custom_personas(
        self,
        owner_person_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[CustomPersonaRecord, ...]:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                rows = await connection.fetch(
                    """
                    SELECT * FROM identity_custom_personas
                    WHERE owner_person_id = $1
                    ORDER BY created_at, persona_id
                    """,
                    owner_person_id,
                )
                return tuple(_custom_persona(row) for row in rows)

    async def count_custom_personas(
        self,
        owner_person_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> int:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                count = await connection.fetchval(
                    """
                    SELECT count(*) FROM identity_custom_personas
                    WHERE owner_person_id = $1
                    """,
                    owner_person_id,
                )
                return int(count or 0)

    async def count_persona_assignment_references(
        self,
        persona_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> int:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                count = await connection.fetchval(
                    """
                    SELECT count(*) FROM identity_persona_assignments
                    WHERE persona_id = $1
                    """,
                    persona_id,
                )
                return int(count or 0)

    async def delete_custom_persona(
        self,
        persona_id: str,
        *,
        owner_person_id: str,
        audit_events: tuple[AuditEvent, ...] = (),
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> int:
        """Drop references then the persona in ONE transaction.

        The order is fixed (count -> delete referencing assignments -> delete
        persona) so no dangling assignment can survive the deletion.  A
        persona absent or owned by another account returns ``0`` untouched.
        """
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                owner = await connection.fetchval(
                    """
                    SELECT owner_person_id FROM identity_custom_personas
                    WHERE persona_id = $1
                    """,
                    persona_id,
                )
                if owner is None or str(owner) != owner_person_id:
                    return 0
                drifted = int(
                    await connection.fetchval(
                        """
                        SELECT count(*) FROM identity_persona_assignments
                        WHERE persona_id = $1
                        """,
                        persona_id,
                    )
                    or 0
                )
                await connection.execute(
                    "DELETE FROM identity_persona_assignments WHERE persona_id = $1",
                    persona_id,
                )
                await connection.execute(
                    "DELETE FROM identity_custom_personas WHERE persona_id = $1",
                    persona_id,
                )
                return drifted


async def _insert_transfer_intent(
    connection: asyncpg.Connection, intent: TransferIntent
) -> None:
    await connection.execute(
        """
        INSERT INTO identity_transfer_intents (
            transfer_id, device_id, from_account_owner_person_id,
            to_account_owner_person_id, status, step_up_evidence_id,
            policy_receipt_id, idempotency_key, evidence_hash,
            supersedes_binding_id, resulting_binding_id, created_by_person_id,
            created_at, updated_at, valid_until, accepted_at, cancelled_at,
            cancelled_by_person_id, cancel_reason
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                  $14, $15, $16, $17, $18, $19)
        """,
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
        _timestamp(intent.created_at, field="created_at"),
        _timestamp(intent.updated_at, field="updated_at"),
        _timestamp(intent.valid_until, field="valid_until") if intent.valid_until else None,
        _timestamp(intent.accepted_at, field="accepted_at") if intent.accepted_at else None,
        _timestamp(intent.cancelled_at, field="cancelled_at")
        if intent.cancelled_at
        else None,
        intent.cancelled_by_person_id,
        intent.cancel_reason,
    )


async def _update_transfer_intent(
    connection: asyncpg.Connection, intent: TransferIntent
) -> None:
    await connection.execute(
        """
        UPDATE identity_transfer_intents
        SET status = $2,
            updated_at = $3,
            supersedes_binding_id = $4,
            resulting_binding_id = $5,
            valid_until = $6,
            accepted_at = $7,
            cancelled_at = $8,
            cancelled_by_person_id = $9,
            cancel_reason = $10
        WHERE transfer_id = $1
        """,
        intent.transfer_id,
        intent.status,
        _timestamp(intent.updated_at, field="updated_at"),
        intent.supersedes_binding_id,
        intent.resulting_binding_id,
        _timestamp(intent.valid_until, field="valid_until")
        if intent.valid_until
        else None,
        _timestamp(intent.accepted_at, field="accepted_at")
        if intent.accepted_at
        else None,
        _timestamp(intent.cancelled_at, field="cancelled_at")
        if intent.cancelled_at
        else None,
        intent.cancelled_by_person_id,
        intent.cancel_reason,
    )


async def _insert_idempotency_record(
    connection: asyncpg.Connection, record: IdempotencyRecord
) -> bool:
    inserted = await connection.fetchval(
        """
        SELECT identity_write_idempotency(
            $1, $2, $3, $4, $5::jsonb, $6
        )
        """,
        record.scope_key,
        record.idempotency_key,
        record.operation,
        record.content_hash,
        json.dumps(record.result_payload, ensure_ascii=False, sort_keys=True),
        _timestamp(record.created_at, field="created_at"),
    )
    if inserted:
        # First write wins; a skipped insert means the key is already taken.
        return True
    raise IdentityConflictError(
        f"idempotency key {record.idempotency_key!r} already used "
        f"for scope {record.scope_key!r}"
    )
