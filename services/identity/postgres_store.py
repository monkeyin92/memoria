"""PostgreSQL / FORCE-RLS persistence for the identity domain.

Production authority (section 11.7 / PR-17).  The schema is shipped in
``postgres_schema.sql``: constraints mirror the domain rules, every table is
FORCE RLS, versioned binding records are immutable after insert, and audit /
outbox rows are written by the same adapter.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar, cast

import asyncpg
from asyncpg.exceptions import InsufficientPrivilegeError
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
    IdentityAccessDeniedError,
    IdentityConflictError,
    IdentityNotFoundError,
    Permission,
    PersonStatus,
    PersonSubject,
    Relationship,
    RelationshipStatus,
    RelationType,
    SubjectCategory,
    TransferIntent,
    permission_set,
    person_from_dict,
    transfer_from_dict,
)
from services.identity.repository import AuditEvent, OutboxEvent

_IdentityEvent = TypeVar("_IdentityEvent", AuditEvent, OutboxEvent)

_SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")
_REQUIRED_TABLES = frozenset(
    {
        "identity_persons",
        "identity_relationships",
        "identity_device_bindings",
        "identity_device_binding_roles",
        "identity_audit_events",
        "identity_outbox",
        "identity_transfer_intents",
        "identity_idempotency_records",
    }
)

_REQUIRED_ROLES = frozenset(
    {
        "memoria_identity",
        "memoria_identity_registration",
        "memoria_identity_outbox",
        "memoria_identity_migration",
    }
)

_REGISTRATION_ROLE = "memoria_identity_registration"

_REGISTRATION_CONFLICT_SQLSTATE = "II001"

_API_NO_DIRECT_WRITE_TABLES = (
    "identity_persons",
    "identity_audit_events",
    "identity_outbox",
    "identity_idempotency_records",
)


def _timestamp(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _from_db(value: object) -> datetime | None:
    if value is None:
        return None
    return cast(datetime, value)


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


def _jsonb_list(value: object, *, field: str) -> list[object]:
    """Strict asyncpg jsonb decoding: asyncpg may return ``str`` or ``list``.
    Verifies the top-level type and every element type (fail closed)."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} is not valid JSONB") from exc
    else:
        decoded = value
    if not isinstance(decoded, list):
        raise ValueError(f"{field} must be a JSONB array")
    if not all(isinstance(item, str) for item in decoded):
        raise ValueError(f"{field} array items must be strings")
    return decoded


def _jsonb_object(value: object, *, field: str) -> dict[str, object]:
    """Strict asyncpg jsonb object decoding (str or dict, fail closed)."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} is not valid JSONB") from exc
    else:
        decoded = value
    if not isinstance(decoded, dict):
        raise ValueError(f"{field} must be a JSONB object")
    return decoded


def _permissions(value: object, *, field: str) -> frozenset[Permission]:
    return permission_set(_jsonb_list(value, field=field))


class PostgresIdentityStore:
    """PostgreSQL implementation of the ``IdentityStore`` protocol."""

    def __init__(self, dsn: str, registration_dsn: str | None = None) -> None:
        self._dsn = dsn
        self._registration_dsn = registration_dsn
        self._pool: asyncpg.Pool | None = None
        self._registration_pool: asyncpg.Pool | None = None

    async def initialize(self, *, expected_role: str | None = None) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
        assert self._pool is not None
        schema = _SCHEMA_PATH.read_text(encoding="utf-8")
        async with self._pool.acquire() as connection:
            try:
                await connection.execute(schema)
            except InsufficientPrivilegeError:
                # A non-owner role (e.g. the API role) cannot run DDL; the
                # schema was bootstrapped by an admin.  Verification below
                # still guards roles / FORCE RLS / open policies.
                pass
            await connection.execute("SET application_name = 'memoria-identity'")
            present = {
                str(row["tablename"])
                for row in await connection.fetch(
                    """
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public'
                      AND tablename LIKE 'identity\\_%'
                    """
                )
            }
            missing = _REQUIRED_TABLES - present
            if missing:
                raise RuntimeError(f"identity schema tables missing: {sorted(missing)}")
            rls_state = await connection.fetch(
                """
                SELECT c.relname AS tablename,
                       c.relrowsecurity,
                       c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname LIKE 'identity\\_%'
                  AND c.relkind = 'r'
                """
            )
            unenabled = [
                str(row["tablename"])
                for row in rls_state
                if not bool(row["relrowsecurity"])
            ]
            if unenabled:
                raise RuntimeError(
                    f"identity tables must enable row-level security: "
                    f"{sorted(unenabled)}"
                )
            unforced = [
                str(row["tablename"])
                for row in rls_state
                if not bool(row["relforcerowsecurity"])
            ]
            if unforced:
                raise RuntimeError(
                    f"identity tables must force row-level security: "
                    f"{sorted(unforced)}"
                )
            open_policies = [
                f"{row['tablename']}.{row['policyname']}"
                for row in await connection.fetch(
                    """
                    SELECT tablename, policyname, qual, with_check
                    FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename LIKE 'identity\\_%'
                    """
                )
                if str(row["qual"] or "").strip().lower() in {"true", "(true)"}
                or str(row["with_check"] or "").strip().lower() in {"true", "(true)"}
            ]
            if open_policies:
                raise RuntimeError(
                    "identity RLS must not contain open USING(true)/WITH CHECK(true) "
                    f"policies: {sorted(open_policies)}"
                )
            present_roles = {
                str(row["rolname"])
                for row in await connection.fetch(
                    """
                    SELECT rolname FROM pg_roles
                    WHERE rolname = ANY($1::text[])
                    """
                    , list(_REQUIRED_ROLES),
                )
            }
            missing_roles = _REQUIRED_ROLES - present_roles
            if missing_roles:
                raise RuntimeError(
                    "identity production roles missing "
                    f"(LOGIN NOSUPERUSER NOBYPASSRLS): {sorted(missing_roles)}"
                )
            # The API role must never hold direct write privileges on the
            # authoritative tables: registration, audit, outbox, idempotency
            # records AND person updates go through SECURITY DEFINER ports
            # only (profile / age declaration / verification).  A GUC
            # (app.identity_scope / app.identity_actor) is not a credential
            # and must never unlock raw DML.
            for table in _API_NO_DIRECT_WRITE_TABLES:
                for privilege in ("INSERT", "UPDATE"):
                    granted = await connection.fetchval(
                        """
                        SELECT has_table_privilege(
                            current_user, $1::regclass, $2
                        )
                        """,
                        table,
                        privilege,
                    )
                    if granted:
                        raise RuntimeError(
                            f"identity API role must not hold {privilege} on "
                            f"{table} (registration/audit/outbox/idempotency "
                            "writes go through SECURITY DEFINER ports only)"
                        )
            # Generic audit/outbox/update ports must not exist for the API
            # role: business mutation events are synthesized by the triggers
            # with fixed actions, and person updates are RLS/action-level.
            for signature in (
                "identity_write_audit(text,text,text,text,text,text,text,"
                "text,jsonb,timestamptz)",
                "identity_enqueue_outbox(text,text,text,jsonb,timestamptz)",
                "identity_update_person(text,text,text,text,text,text,text,"
                "text,timestamptz,text)",
                "identity_update_age_evidence(text,text,text,text,"
                "timestamptz,text)",
            ):
                exists_or_granted = await connection.fetchval(
                    """
                    SELECT to_regprocedure($1) IS NOT NULL
                           OR has_function_privilege(
                               current_user, to_regprocedure($1), 'EXECUTE'
                           )
                    """,
                    signature,
                )
                if exists_or_granted:
                    raise RuntimeError(
                        f"generic port {signature.split('(')[0]} must not "
                        "exist for the identity API role"
                    )
            for port, signature in (
                (
                    "identity_self_update_profile",
                    "identity_self_update_profile(text,text,text,text,"
                    "timestamptz,text)",
                ),
                (
                    "identity_declare_age_evidence",
                    "identity_declare_age_evidence(text,text,timestamptz,text)",
                ),
            ):
                can_exec = await connection.fetchval(
                    """
                    SELECT has_function_privilege(
                        current_user, to_regprocedure($1), 'EXECUTE'
                    )
                    """,
                    signature,
                )
                if not can_exec:
                    raise RuntimeError(
                        f"identity API role must be able to EXECUTE {port}"
                    )
            verify_denied = await connection.fetchval(
                """
                SELECT has_function_privilege(
                    current_user,
                    to_regprocedure(
                        'identity_verify_age_evidence(text,text,text,text,'
                        'text,timestamptz)'
                    ),
                    'EXECUTE'
                )
                """
            )
            if verify_denied:
                raise RuntimeError(
                    "identity API role must NOT EXECUTE "
                    "identity_verify_age_evidence"
                )
            audit_triggers = {
                str(row["tgname"])
                for row in await connection.fetch(
                    """
                    SELECT tgname FROM pg_trigger
                    WHERE tgname IN (
                        'identity_relationships_audit_trigger',
                        'identity_bindings_audit_trigger',
                        'identity_transfers_audit_trigger',
                        'identity_persons_update_audit_trigger'
                    )
                    """
                )
            }
            missing_triggers = {
                "identity_relationships_audit_trigger",
                "identity_bindings_audit_trigger",
                "identity_transfers_audit_trigger",
                "identity_persons_update_audit_trigger",
            } - audit_triggers
            if missing_triggers:
                raise RuntimeError(
                    "identity mutation audit triggers missing: "
                    f"{sorted(missing_triggers)}"
                )
            role_row = await connection.fetchrow(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
            if role_row is None:
                raise RuntimeError("cannot verify the connecting PostgreSQL role")
            if bool(role_row["rolsuper"]):
                raise RuntimeError(
                    "PostgresIdentityStore must connect as a non-superuser role"
                )
            if bool(role_row["rolbypassrls"]):
                raise RuntimeError(
                    "PostgresIdentityStore role must not bypass RLS"
                )
            current_role = str(await connection.fetchval("SELECT current_user"))
            if expected_role is not None and current_role != expected_role:
                raise RuntimeError(
                    f"PostgresIdentityStore expected role {expected_role!r}, "
                    f"connected as {current_role!r}"
                )
            role_attrs = await connection.fetchval(
                """
                SELECT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
                FROM pg_roles WHERE rolname = current_user
                """
            )
            if not role_attrs:
                raise RuntimeError(
                    "identity store role must be LOGIN NOSUPERUSER NOBYPASSRLS"
                )
        if self._registration_dsn is None:
            raise RuntimeError(
                "PostgresIdentityStore requires an explicit registration "
                "authority (registration_dsn / "
                "MEMORIA_IDENTITY_REGISTRATION_DATABASE_URL): person "
                "registration must run as the dedicated "
                f"{_REGISTRATION_ROLE!r} role; refusing to run without it"
            )
        self._registration_pool = await asyncpg.create_pool(
            self._registration_dsn, min_size=1, max_size=2
        )
        assert self._registration_pool is not None
        async with self._registration_pool.acquire() as connection:
            await connection.execute("SET application_name = 'memoria-identity-reg'")
            current_role = str(await connection.fetchval("SELECT current_user"))
            if current_role != _REGISTRATION_ROLE:
                raise RuntimeError(
                    "identity registration pool must connect as "
                    f"{_REGISTRATION_ROLE!r}, connected as {current_role!r}"
                )
            attrs = await connection.fetchval(
                """
                SELECT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls
                FROM pg_roles WHERE rolname = current_user
                """
            )
            if not attrs:
                raise RuntimeError(
                    "identity registration role must be LOGIN NOSUPERUSER "
                    "NOBYPASSRLS"
                )
            table_privs = await connection.fetch(
                """
                SELECT c.relname,
                       has_table_privilege(current_user, c.oid, 'SELECT')
                           AS can_select,
                       has_table_privilege(current_user, c.oid, 'INSERT')
                           AS can_insert,
                       has_table_privilege(current_user, c.oid, 'UPDATE')
                           AS can_update
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname LIKE 'identity\\_%'
                  AND c.relkind = 'r'
                """
            )
            for row in table_privs:
                if any(bool(row[key]) for key in ("can_select", "can_insert", "can_update")):
                    raise RuntimeError(
                        "identity registration role must hold NO table "
                        f"privileges on identity_* (got {row['relname']})"
                    )
            can_register = await connection.fetchval(
                """
                SELECT has_function_privilege(
                    current_user,
                    to_regprocedure(
                        'identity_register_person(text,text,text,text,text,'
                        'text,text,text,timestamptz,timestamptz,text,jsonb,'
                        'text)'
                    ),
                    'EXECUTE'
                )
                """
            )
            if not can_register:
                raise RuntimeError(
                    "identity registration role must be able to EXECUTE "
                    "identity_register_person"
                )
            can_verify = await connection.fetchval(
                """
                SELECT has_function_privilege(
                    current_user,
                    to_regprocedure(
                        'identity_verify_age_evidence(text,text,text,text,'
                        'text,timestamptz)'
                    ),
                    'EXECUTE'
                )
                """
            )
            if not can_verify:
                raise RuntimeError(
                    "identity registration role must be able to EXECUTE "
                    "identity_verify_age_evidence"
                )
            denied_ports = {
                "identity_write_idempotency": (
                    "identity_write_idempotency(text,text,text,text,jsonb,"
                    "timestamptz)"
                ),
                "identity_patch_idempotency_result": (
                    "identity_patch_idempotency_result(text,text,jsonb)"
                ),
                "identity_self_update_profile": (
                    "identity_self_update_profile(text,text,text,text,"
                    "timestamptz,text)"
                ),
                "identity_declare_age_evidence": (
                    "identity_declare_age_evidence(text,text,timestamptz,text)"
                ),
            }
            for port, signature in denied_ports.items():
                can_exec = await connection.fetchval(
                    """
                    SELECT has_function_privilege(
                        current_user, to_regprocedure($1), 'EXECUTE'
                    )
                    """,
                    signature,
                )
                if can_exec:
                    raise RuntimeError(
                        "identity registration role must NOT be able to "
                        f"EXECUTE {port}"
                    )
            port_owner = await connection.fetchval(
                """
                SELECT pg_get_userbyid(p.proowner) = 'memoria_identity_owner'
                       AND p.prosecdef
                       AND 'row_security=on' = ANY(p.proconfig)
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'
                  AND p.proname = 'identity_register_person'
                """
            )
            if not port_owner:
                raise RuntimeError(
                    "identity_register_person must be SECURITY DEFINER owned "
                    "by memoria_identity_owner with row_security=on"
                )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        if self._registration_pool is not None:
            await self._registration_pool.close()
            self._registration_pool = None

    def _ready(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresIdentityStore.initialize() must run first")
        return self._pool

    def _registration_ready(self) -> asyncpg.Pool:
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
        # Person creation is registration: it runs as the dedicated
        # registration role through the SECURITY DEFINER port so a forged
        # app.identity_scope GUC can never grant the API role the ability to
        # create persons (the API role has no INSERT grant on identity_persons).
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
        pool = self._registration_ready()
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
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
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
        pool = self._registration_ready()
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
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
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

    async def get_person(
        self,
        person_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> PersonSubject | None:
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
                row = await connection.fetchrow(
                    "SELECT * FROM identity_persons WHERE person_id = $1", person_id
                )
            return _person(row) if row is not None else None

    async def person_exists(self, person_id: str) -> bool:
        pool = self._ready()
        async with pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    "SELECT identity_person_exists($1)", person_id
                )
            )

    async def has_active_relationship(
        self,
        *,
        source_person_id: str,
        target_person_id: str,
        relation_type: str,
        at: datetime,
    ) -> bool:
        pool = self._ready()
        async with pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    """
                    SELECT identity_relationship_active($1, $2, $3, $4)
                    """,
                    source_person_id,
                    target_person_id,
                    relation_type,
                    _timestamp(at, field="at"),
                )
            )

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
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
                if expected_updated_at is not None:
                    existing = await connection.fetchrow(
                        "SELECT updated_at FROM identity_relationships "
                        "WHERE relationship_id = $1 FOR UPDATE",
                        relationship.relationship_id,
                    )
                    if existing is None:
                        raise IdentityNotFoundError(
                            f"relationship {relationship.relationship_id} "
                            "does not exist"
                        )
                    if cast(datetime, existing["updated_at"]) != expected_updated_at:
                        raise IdentityConflictError(
                            f"relationship {relationship.relationship_id} "
                            "changed concurrently"
                        )
                    await _update_relationship(connection, relationship)
                else:
                    try:
                        await _insert_relationship(connection, relationship)
                    except asyncpg.UniqueViolationError as exc:
                        raise IdentityConflictError(
                            f"relationship {relationship.relationship_id} "
                            "already exists"
                        ) from exc
                for extra in extra_relationships:
                    await _update_relationship(connection, extra)

    async def get_relationship(
        self,
        relationship_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> Relationship | None:
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
                row = await connection.fetchrow(
                    "SELECT * FROM identity_relationships WHERE relationship_id = $1",
                    relationship_id,
                )
            return _relationship(row) if row is not None else None

    async def list_relationships(
        self,
        person_id: str,
        statuses: tuple[RelationshipStatus, ...] | None = None,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[Relationship, ...]:
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
                if statuses is None:
                    rows = await connection.fetch(
                        """
                        SELECT * FROM identity_relationships
                        WHERE source_person_id = $1 OR target_person_id = $1
                        ORDER BY created_at
                        """,
                        person_id,
                    )
                else:
                    rows = await connection.fetch(
                        """
                        SELECT * FROM identity_relationships
                        WHERE (source_person_id = $1 OR target_person_id = $1)
                          AND status = ANY($2::text[])
                        ORDER BY created_at
                        """,
                        person_id,
                        list(statuses),
                    )
            return tuple(_relationship(row) for row in rows)

    async def scan_relationships(
        self,
        statuses: tuple[RelationshipStatus, ...] | None = None,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[Relationship, ...]:
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
                if statuses is None:
                    rows = await connection.fetch(
                        "SELECT * FROM identity_relationships ORDER BY created_at"
                    )
                else:
                    rows = await connection.fetch(
                        """
                        SELECT * FROM identity_relationships
                        WHERE status = ANY($1::text[])
                        ORDER BY created_at
                        """,
                        list(statuses),
                    )
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
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    binding.device_id,
                )
                if previous_binding_id is not None:
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
                else:
                    version_row = await connection.fetchrow(
                        """
                        SELECT COALESCE(MAX(binding_version), 0) AS version
                        FROM identity_device_bindings
                        WHERE device_id = $1
                        """,
                        binding.device_id,
                    )
                    version = int(version_row["version"]) + 1
                try:
                    await connection.execute(
                        """
                        INSERT INTO identity_device_bindings (
                            binding_id, device_id, declared_mode,
                            family_space_id, account_owner_person_id,
                            binding_version, status, reason, valid_from,
                            valid_until, supersedes_binding_id,
                            service_profile_version, policy_bundle_version,
                            consent_snapshot_id, persona_assignment_id,
                            created_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                            $12, $13, $14, $15, $16
                        )
                        """,
                        binding.binding_id,
                        binding.device_id,
                        binding.declared_mode,
                        binding.family_space_id,
                        binding.account_owner_person_id,
                        version,
                        binding.status,
                        binding.reason,
                        _timestamp(binding.valid_from, field="valid_from"),
                        (
                            _timestamp(binding.valid_until, field="valid_until")
                            if binding.valid_until
                            else None
                        ),
                        binding.supersedes_binding_id,
                        binding.service_profile_version,
                        binding.policy_bundle_version,
                        binding.consent_snapshot_id,
                        binding.persona_assignment_id,
                        _timestamp(binding.created_at, field="created_at"),
                    )
                    for grant in binding.roles:
                        await connection.execute(
                            """
                            INSERT INTO identity_device_binding_roles (
                                binding_id, person_id, role, status,
                                permissions_json, granted_at, ended_at
                            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                            """,
                            binding.binding_id,
                            grant.person_id,
                            grant.role,
                            grant.status,
                            json.dumps(sorted(grant.permissions), ensure_ascii=False),
                            _timestamp(grant.granted_at, field="granted_at"),
                            (
                                _timestamp(grant.ended_at, field="ended_at")
                                if grant.ended_at
                                else None
                            ),
                        )
                except asyncpg.UniqueViolationError as exc:
                    raise BindingVersionConflictError(
                        f"binding version {version} already exists for "
                        f"device {binding.device_id}"
                    ) from exc
            return replace(binding, binding_version=version)

    async def get_binding(
        self,
        binding_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding | None:
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
                row = await connection.fetchrow(
                    "SELECT * FROM identity_device_bindings WHERE binding_id = $1",
                    binding_id,
                )
                if row is None:
                    return None
                roles = await connection.fetch(
                    "SELECT * FROM identity_device_binding_roles WHERE binding_id = $1",
                    binding_id,
                )
            return _binding(row, roles)

    async def get_active_binding(
        self,
        device_id: str,
        now: datetime,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> DeviceBinding | None:
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
                rows = await connection.fetch(
                    """
                    SELECT * FROM identity_device_bindings
                    WHERE device_id = $1 AND status = 'active'
                    ORDER BY binding_version DESC
                    """,
                    device_id,
                )
                for row in rows:
                    valid_from = cast(datetime, row["valid_from"])
                    valid_until = row["valid_until"]
                    if valid_from > now:
                        continue
                    if valid_until is not None and cast(datetime, valid_until) <= now:
                        continue
                    roles = await connection.fetch(
                        """
                        SELECT * FROM identity_device_binding_roles
                        WHERE binding_id = $1
                        """,
                        str(row["binding_id"]),
                    )
                    return _binding(row, roles)
            return None

    async def list_binding_versions(
        self,
        device_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[DeviceBinding, ...]:
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
                rows = await connection.fetch(
                    """
                    SELECT * FROM identity_device_bindings
                    WHERE device_id = $1 ORDER BY binding_version
                    """,
                    device_id,
                )
                bindings: list[DeviceBinding] = []
                for row in rows:
                    roles = await connection.fetch(
                        """
                        SELECT * FROM identity_device_binding_roles
                        WHERE binding_id = $1
                        """,
                        str(row["binding_id"]),
                    )
                    bindings.append(_binding(row, roles))
            return tuple(bindings)

    async def scan_bindings(
        self,
        statuses: tuple[BindingStatus, ...] | None = None,
        *,
        scope: str = "api",
    ) -> tuple[DeviceBinding, ...]:
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=None, scope=scope
                )
                if statuses is None:
                    rows = await connection.fetch(
                        "SELECT * FROM identity_device_bindings ORDER BY created_at"
                    )
                else:
                    rows = await connection.fetch(
                        """
                        SELECT * FROM identity_device_bindings
                        WHERE status = ANY($1::text[])
                        ORDER BY created_at
                        """,
                        list(statuses),
                    )
                bindings: list[DeviceBinding] = []
                for row in rows:
                    roles = await connection.fetch(
                        """
                        SELECT * FROM identity_device_binding_roles
                        WHERE binding_id = $1
                        """,
                        str(row["binding_id"]),
                    )
                    bindings.append(_binding(row, roles))
            return tuple(bindings)

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
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
                row = await connection.fetchrow(
                    "SELECT * FROM identity_device_bindings WHERE binding_id = $1",
                    binding_id,
                )
                if row is None:
                    raise IdentityNotFoundError(f"binding {binding_id} does not exist")
                await connection.execute(
                    """
                    UPDATE identity_device_bindings
                    SET status = $1, valid_until = $2
                    WHERE binding_id = $3
                    """,
                    status,
                    _timestamp(valid_until, field="valid_until") if valid_until else None,
                    binding_id,
                )
                await connection.execute(
                    """
                    UPDATE identity_device_binding_roles
                    SET status = $1, ended_at = $2
                    WHERE binding_id = $3 AND status = 'active'
                    """,
                    status,
                    _timestamp(at, field="at"),
                    binding_id,
                )
                updated = await connection.fetchrow(
                    "SELECT * FROM identity_device_bindings WHERE binding_id = $1",
                    binding_id,
                )
                roles = await connection.fetch(
                    """
                    SELECT * FROM identity_device_binding_roles
                    WHERE binding_id = $1
                    """,
                    binding_id,
                )
                result = _binding(updated, roles)
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
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
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
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
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
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
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
        pool = self._ready()
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
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
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
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
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
        pool = self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(
                    connection, actor_person_id=actor_person_id, scope=scope
                )
                if idempotency_record is not None:
                    await _insert_idempotency_record(
                        connection, idempotency_record
                    )
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    binding.device_id,
                )
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
        # The generic audit port is gone: audit rows are synthesized by the
        # mutation triggers with fixed actions.  There is no standalone
        # append path for the API role (fail closed).
        raise RuntimeError(
            "PostgresIdentityStore.append_audit is not available: audit rows "
            "are synthesized by the business mutation triggers"
        )

    async def enqueue_outbox(self, event: OutboxEvent) -> None:
        # The generic outbox port is gone: outbox events are synthesized by
        # the mutation triggers.  There is no standalone enqueue path for the
        # API role (fail closed).
        raise RuntimeError(
            "PostgresIdentityStore.enqueue_outbox is not available: outbox "
            "events are synthesized by the business mutation triggers"
        )


def _person(row: asyncpg.Record) -> PersonSubject:
    return PersonSubject(
        person_id=str(row["person_id"]),
        display_name=str(row["display_name"]),
        subject_category=cast(SubjectCategory, str(row["subject_category"])),
        age_band=cast(AgeBand, str(row["age_band"])),
        age_evidence_status=cast(AgeEvidenceStatus, str(row["age_evidence_status"])),
        locale=str(row["locale"]),
        timezone=str(row["timezone"]),
        status=cast(PersonStatus, str(row["status"])),
        created_at=_from_db(row["created_at"]) or datetime.now(UTC),
        updated_at=_from_db(row["updated_at"]) or datetime.now(UTC),
    )


def _relationship(row: asyncpg.Record) -> Relationship:
    return Relationship(
        relationship_id=str(row["relationship_id"]),
        source_person_id=str(row["source_person_id"]),
        target_person_id=str(row["target_person_id"]),
        relation_type=cast(RelationType, str(row["relation_type"])),
        status=cast(RelationshipStatus, str(row["status"])),
        valid_from=_from_db(row["valid_from"]) or datetime.now(UTC),
        valid_until=_from_db(row["valid_until"]),
        established_evidence_id=str(row["established_evidence_id"]),
        confirmed_by_source_at=_from_db(row["confirmed_by_source_at"]),
        confirmed_by_target_at=_from_db(row["confirmed_by_target_at"]),
        dispute_resolution_acked_by_source_at=_from_db(
            row["dispute_resolution_acked_by_source_at"]
        ),
        dispute_resolution_acked_by_target_at=_from_db(
            row["dispute_resolution_acked_by_target_at"]
        ),
        requires_confirmation=bool(row["requires_confirmation"]),
        can_delegate=bool(row["can_delegate"]),
        delegated_from_relationship_id=(
            str(row["delegated_from_relationship_id"])
            if row["delegated_from_relationship_id"] is not None
            else None
        ),
        delegation_depth=int(row["delegation_depth"]),
        permissions=_permissions(
            row["permissions_json"], field="permissions_json"
        ),
        dispute_reason=(
            str(row["dispute_reason"]) if row["dispute_reason"] is not None else None
        ),
        auto_suspended=bool(row["auto_suspended"]),
        revoked_at=_from_db(row["revoked_at"]),
        revocation_evidence_id=(
            str(row["revocation_evidence_id"])
            if row["revocation_evidence_id"] is not None
            else None
        ),
        created_at=_from_db(row["created_at"]) or datetime.now(UTC),
        updated_at=_from_db(row["updated_at"]) or datetime.now(UTC),
    )


def _transfer_intent(row: asyncpg.Record) -> TransferIntent:
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
            "created_at": (
                row["created_at"].isoformat() if row["created_at"] is not None else None
            ),
            "updated_at": (
                row["updated_at"].isoformat() if row["updated_at"] is not None else None
            ),
            "valid_until": row["valid_until"],
            "accepted_at": row["accepted_at"],
            "cancelled_at": row["cancelled_at"],
            "cancelled_by_person_id": row["cancelled_by_person_id"],
            "cancel_reason": row["cancel_reason"],
        }
    )


async def _insert_binding(
    connection: asyncpg.Connection, binding: DeviceBinding, version: int
) -> None:
    await connection.execute(
        """
        INSERT INTO identity_device_bindings (
            binding_id, device_id, declared_mode,
            family_space_id, account_owner_person_id,
            binding_version, status, reason, valid_from,
            valid_until, supersedes_binding_id,
            service_profile_version, policy_bundle_version,
            consent_snapshot_id, persona_assignment_id,
            created_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
            $12, $13, $14, $15, $16
        )
        """,
        binding.binding_id,
        binding.device_id,
        binding.declared_mode,
        binding.family_space_id,
        binding.account_owner_person_id,
        version,
        binding.status,
        binding.reason,
        _timestamp(binding.valid_from, field="valid_from"),
        (
            _timestamp(binding.valid_until, field="valid_until")
            if binding.valid_until
            else None
        ),
        binding.supersedes_binding_id,
        binding.service_profile_version,
        binding.policy_bundle_version,
        binding.consent_snapshot_id,
        binding.persona_assignment_id,
        _timestamp(binding.created_at, field="created_at"),
    )
    for grant in binding.roles:
        await connection.execute(
            """
            INSERT INTO identity_device_binding_roles (
                binding_id, person_id, role, status,
                permissions_json, granted_at, ended_at
            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            """,
            binding.binding_id,
            grant.person_id,
            grant.role,
            grant.status,
            json.dumps(sorted(grant.permissions), ensure_ascii=False),
            _timestamp(grant.granted_at, field="granted_at"),
            (
                _timestamp(grant.ended_at, field="ended_at")
                if grant.ended_at
                else None
            ),
        )


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


async def _insert_relationship(
    connection: asyncpg.Connection, relationship: Relationship
) -> None:
    await connection.execute(
        """
        INSERT INTO identity_relationships (
            relationship_id, source_person_id, target_person_id,
            relation_type, status, valid_from, valid_until,
            established_evidence_id, confirmed_by_source_at,
            confirmed_by_target_at,
            dispute_resolution_acked_by_source_at,
            dispute_resolution_acked_by_target_at,
            requires_confirmation, can_delegate,
            delegated_from_relationship_id, delegation_depth,
            permissions_json, dispute_reason, auto_suspended,
            revoked_at, revocation_evidence_id, created_at, updated_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
            $12, $13, $14, $15, $16, $17, $18::jsonb, $19, $20,
            $21, $22, $23
        )
        """,
        relationship.relationship_id,
        relationship.source_person_id,
        relationship.target_person_id,
        relationship.relation_type,
        relationship.status,
        _timestamp(relationship.valid_from, field="valid_from"),
        (
            _timestamp(relationship.valid_until, field="valid_until")
            if relationship.valid_until
            else None
        ),
        relationship.established_evidence_id,
        (
            _timestamp(
                relationship.confirmed_by_source_at,
                field="confirmed_by_source_at",
            )
            if relationship.confirmed_by_source_at
            else None
        ),
        (
            _timestamp(
                relationship.confirmed_by_target_at,
                field="confirmed_by_target_at",
            )
            if relationship.confirmed_by_target_at
            else None
        ),
        (
            _timestamp(
                relationship.dispute_resolution_acked_by_source_at,
                field="dispute_resolution_acked_by_source_at",
            )
            if relationship.dispute_resolution_acked_by_source_at
            else None
        ),
        (
            _timestamp(
                relationship.dispute_resolution_acked_by_target_at,
                field="dispute_resolution_acked_by_target_at",
            )
            if relationship.dispute_resolution_acked_by_target_at
            else None
        ),
        relationship.requires_confirmation,
        relationship.can_delegate,
        relationship.delegated_from_relationship_id,
        relationship.delegation_depth,
        json.dumps(sorted(relationship.permissions), ensure_ascii=False),
        relationship.dispute_reason,
        relationship.auto_suspended,
        (
            _timestamp(relationship.revoked_at, field="revoked_at")
            if relationship.revoked_at
            else None
        ),
        relationship.revocation_evidence_id,
        _timestamp(relationship.created_at, field="created_at"),
        _timestamp(relationship.updated_at, field="updated_at"),
    )


async def _update_relationship(
    connection: asyncpg.Connection, relationship: Relationship
) -> None:
    await connection.execute(
        """
        UPDATE identity_relationships
        SET status = $2,
            valid_from = $3,
            valid_until = $4,
            confirmed_by_source_at = $5,
            confirmed_by_target_at = $6,
            dispute_resolution_acked_by_source_at = $7,
            dispute_resolution_acked_by_target_at = $8,
            dispute_reason = $9,
            auto_suspended = $10,
            revoked_at = $11,
            revocation_evidence_id = $12,
            updated_at = $13
        WHERE relationship_id = $1
        """,
        relationship.relationship_id,
        relationship.status,
        _timestamp(relationship.valid_from, field="valid_from"),
        (
            _timestamp(relationship.valid_until, field="valid_until")
            if relationship.valid_until
            else None
        ),
        (
            _timestamp(
                relationship.confirmed_by_source_at,
                field="confirmed_by_source_at",
            )
            if relationship.confirmed_by_source_at
            else None
        ),
        (
            _timestamp(
                relationship.confirmed_by_target_at,
                field="confirmed_by_target_at",
            )
            if relationship.confirmed_by_target_at
            else None
        ),
        (
            _timestamp(
                relationship.dispute_resolution_acked_by_source_at,
                field="dispute_resolution_acked_by_source_at",
            )
            if relationship.dispute_resolution_acked_by_source_at
            else None
        ),
        (
            _timestamp(
                relationship.dispute_resolution_acked_by_target_at,
                field="dispute_resolution_acked_by_target_at",
            )
            if relationship.dispute_resolution_acked_by_target_at
            else None
        ),
        relationship.dispute_reason,
        relationship.auto_suspended,
        (
            _timestamp(relationship.revoked_at, field="revoked_at")
            if relationship.revoked_at
            else None
        ),
        relationship.revocation_evidence_id,
        _timestamp(relationship.updated_at, field="updated_at"),
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


def _binding(binding_row: asyncpg.Record, role_rows: list[asyncpg.Record]) -> DeviceBinding:
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
        valid_from=_from_db(binding_row["valid_from"]) or datetime.now(UTC),
        valid_until=_from_db(binding_row["valid_until"]),
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
        created_at=_from_db(binding_row["created_at"]) or datetime.now(UTC),
    )


def _role(row: asyncpg.Record) -> DeviceBindingRole:
    return DeviceBindingRole(
        binding_id=str(row["binding_id"]),
        person_id=str(row["person_id"]),
        role=cast(BindingRole, str(row["role"])),
        status=cast(Literal["active", "superseded", "revoked", "expired"], str(row["status"])),
        permissions=_permissions(
            row["permissions_json"], field="permissions_json"
        ),
        granted_at=_from_db(row["granted_at"]) or datetime.now(UTC),
        ended_at=_from_db(row["ended_at"]),
    )
