"""PostgreSQL Session Runtime authority and action-transaction adapter.

Writes run as ``memoria_action_executor`` and only invoke SECURITY DEFINER
ports; the role has no direct table privileges.  Reads use the independent
``memoria_session_api`` RLS role.  Policy/Identity adapters receive the same
already-open asyncpg connection owned by the caller service.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyReceiptV2,
    RuntimeProfileSignedV2,
    SessionEvent,
)

_SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")
SESSION_RUNTIME_SCHEMA_SQL: str = _SCHEMA_PATH.read_text(encoding="utf-8")

_REQUIRED_TABLES = frozenset(
    {
        "session_runtime_contexts",
        "session_runtime_profiles",
        "session_runtime_profile_receipts",
        "session_runtime_events",
        "session_runtime_outbox",
        "session_runtime_idempotency",
        "session_runtime_tool_effect_intents",
        "session_runtime_tool_effect_outbox",
    }
)
_REQUIRED_RLS_TABLES = frozenset(_REQUIRED_TABLES)
_REQUIRED_ROLES = frozenset(
    {
        "memoria_action_executor",
        "memoria_session_api",
        "memoria_session_projector",
        "memoria_session_worker",
        "memoria_session_maintenance",
    }
)
_REQUIRED_FUNCTIONS = frozenset(
    {
        "action_device_lock_trust",
        "action_identity_lock_binding",
        "action_identity_can_switch_subject",
        "action_policy_insert_receipt",
        "action_policy_lock_receipt",
        "session_runtime_action_current_profile",
        "session_runtime_action_receipt_authority",
        "session_runtime_advance_action_fence",
        "session_runtime_assert_action_context",
        "session_runtime_actor",
        "session_runtime_commit_initial",
        "session_runtime_commit_rotation",
        "session_runtime_fail_session",
        "session_runtime_close_session",
        "session_runtime_prepare_initial",
        "session_runtime_prepare_rotation",
        "session_runtime_action_effect_context",
        "session_runtime_commit_tool_effect",
        "session_runtime_reconcile_tool_effect",
        "session_runtime_tool_effect_outbox_claim",
        "session_runtime_tool_effect_outbox_complete",
    }
)
_REQUIRED_POLICIES = frozenset(
    {
        ("session_runtime_contexts", "session_runtime_owner_contexts"),
        ("session_runtime_profiles", "session_runtime_owner_profiles"),
        (
            "session_runtime_profile_receipts",
            "session_runtime_owner_receipts",
        ),
        ("session_runtime_events", "session_runtime_owner_events"),
        ("session_runtime_outbox", "session_runtime_owner_outbox"),
        ("session_runtime_idempotency", "session_runtime_owner_idempotency"),
        (
            "session_runtime_tool_effect_intents",
            "session_runtime_owner_tool_effect_intents",
        ),
        (
            "session_runtime_tool_effect_outbox",
            "session_runtime_owner_tool_effect_outbox",
        ),
        ("session_runtime_contexts", "session_runtime_api_contexts"),
        ("session_runtime_profiles", "session_runtime_api_profiles"),
        (
            "session_runtime_profile_receipts",
            "session_runtime_api_receipts",
        ),
        ("session_runtime_events", "session_runtime_api_events"),
        ("session_runtime_contexts", "session_runtime_projector_contexts"),
        ("session_runtime_profiles", "session_runtime_projector_profiles"),
        (
            "session_runtime_profile_receipts",
            "session_runtime_projector_receipts",
        ),
        ("session_runtime_events", "session_runtime_projector_events"),
        ("session_runtime_contexts", "session_runtime_maintenance_contexts"),
        ("session_runtime_profiles", "session_runtime_maintenance_profiles"),
        (
            "session_runtime_profile_receipts",
            "session_runtime_maintenance_receipts",
        ),
        ("session_runtime_events", "session_runtime_maintenance_events"),
        ("session_runtime_outbox", "session_runtime_maintenance_outbox"),
        ("session_runtime_idempotency", "session_runtime_maintenance_idempotency"),
        (
            "session_runtime_tool_effect_intents",
            "session_runtime_maintenance_tool_effect_intents",
        ),
        (
            "session_runtime_tool_effect_outbox",
            "session_runtime_maintenance_tool_effect_outbox",
        ),
        (
            "session_runtime_tool_effect_intents",
            "session_runtime_worker_tool_effect_intents",
        ),
        (
            "session_runtime_tool_effect_outbox",
            "session_runtime_worker_tool_effect_outbox",
        ),
    }
)
_ACTION_EXECUTOR_TABLES = frozenset(
    {
        "session_runtime_contexts",
        "session_runtime_profiles",
        "session_runtime_profile_receipts",
        "session_runtime_events",
        "session_runtime_outbox",
        "session_runtime_idempotency",
        "session_runtime_tool_effect_intents",
        "session_runtime_tool_effect_outbox",
        "policy_receipts_v2",
        "identity_persons",
        "identity_device_bindings",
        "identity_device_binding_roles",
        "identity_persona_assignments",
        "device_fleet_devices",
        "device_fleet_certificates",
        "device_fleet_attestations",
    }
)
_ACTION_EXECUTOR_FUNCTION_SIGNATURES = frozenset(
    {
        "action_device_lock_trust(text, text, text, integer, timestamptz)",
        "action_identity_can_switch_subject(text, text, integer, text)",
        "action_identity_lock_binding(text, text, integer, timestamptz)",
        "action_policy_insert_receipt(jsonb)",
        "action_policy_lock_receipt(text)",
        "session_runtime_action_current_profile(text)",
        "session_runtime_action_receipt_authority(text, jsonb)",
        "session_runtime_advance_action_fence(jsonb)",
        "session_runtime_assert_action_context(text, text, text, text, text, integer, integer)",
        "session_runtime_commit_initial(jsonb, integer, jsonb, text)",
        "session_runtime_commit_rotation(jsonb, integer, jsonb)",
        "session_runtime_fail_session(text, text, integer, jsonb)",
        "session_runtime_close_session(text, text, integer, jsonb)",
        "session_runtime_prepare_initial(jsonb, text, text)",
        "session_runtime_prepare_rotation(text, integer, integer, jsonb, integer, jsonb)",
        "session_runtime_action_effect_context(text, text, jsonb, jsonb)",
        "session_runtime_commit_tool_effect(jsonb)",
        "session_runtime_reconcile_tool_effect(text)",
    }
)
_CONDITIONAL_ACTION_EXECUTOR_FUNCTION_SIGNATURES = frozenset(
    {
        # Consent installs this discovery port only when its schema is present;
        # Session grants EXECUTE at install time and readiness requires the
        # grant whenever the function exists, but tolerates its absence.
        "consent_discover_action_fence(text, text, text, text, text, integer, text, text, timestamptz)",
    }
)
_WORKER_FUNCTION_SIGNATURES = frozenset(
    {
        "session_runtime_tool_effect_outbox_claim(text, integer, integer)",
        "session_runtime_tool_effect_outbox_complete(text, text, text, text)",
    }
)


class SessionRuntimeStoreError(RuntimeError):
    """Base persistence failure."""


class SessionRuntimeConflict(SessionRuntimeStoreError):
    """An idempotency or CAS fence conflicted."""


class SessionRuntimeAuthorityUnavailable(SessionRuntimeStoreError):
    """A required database authority function or role is unavailable."""


@dataclass(frozen=True, slots=True)
class SessionRuntimeContext:
    session_id: str
    actor_id: str
    device_id: str
    binding_id: str
    binding_version: int
    active_subject_id: str | None
    subject_revision: int
    session_epoch: int
    profile_revision: int
    current_runtime_profile_id: str
    generation_id: int
    turn_id: int
    tool_epoch: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_profile(
        cls,
        profile: RuntimeProfileSignedV2,
        *,
        profile_revision: int,
        generation_id: int = 0,
        turn_id: int = 0,
        tool_epoch: int = 0,
    ) -> SessionRuntimeContext:
        return cls(
            session_id=profile.session_id,
            actor_id=profile.actor_id,
            device_id=profile.device_id,
            binding_id=profile.binding_id,
            binding_version=profile.binding_version,
            active_subject_id=profile.active_subject_id,
            subject_revision=profile.subject_revision,
            session_epoch=profile.session_epoch,
            profile_revision=profile_revision,
            current_runtime_profile_id=profile.runtime_profile_id,
            generation_id=generation_id,
            turn_id=turn_id,
            tool_epoch=tool_epoch,
            created_at=profile.issued_at,
            updated_at=profile.issued_at,
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "actor_id": self.actor_id,
            "device_id": self.device_id,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "active_subject_id": self.active_subject_id,
            "subject_revision": self.subject_revision,
            "session_epoch": self.session_epoch,
            "profile_revision": self.profile_revision,
            "current_runtime_profile_id": self.current_runtime_profile_id,
            "generation_id": self.generation_id,
            "turn_id": self.turn_id,
            "tool_epoch": self.tool_epoch,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def _json_text(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_object(value: object, *, field: str) -> dict[str, object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise SessionRuntimeStoreError(f"{field} is not a JSON object")
    return {str(key): item for key, item in decoded.items()}


def _profile_from_json(value: object) -> RuntimeProfileSignedV2:
    return RuntimeProfileSignedV2.model_validate(
        _json_object(value, field="runtime profile")
    )


def _context_from_json(value: object) -> SessionRuntimeContext:
    raw = _json_object(value, field="session context")
    created_at = raw.get("created_at")
    updated_at = raw.get("updated_at")
    if isinstance(created_at, str):
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    else:
        raise SessionRuntimeAuthorityUnavailable("session context created_at is invalid")
    if isinstance(updated_at, str):
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    else:
        raise SessionRuntimeAuthorityUnavailable("session context updated_at is invalid")

    def context_int(name: str) -> int:
        raw_value = raw.get(name)
        if type(raw_value) is not int:
            raise SessionRuntimeAuthorityUnavailable(f"session context {name} is invalid")
        return raw_value

    return SessionRuntimeContext(
        session_id=str(raw["session_id"]),
        actor_id=str(raw["actor_id"]),
        device_id=str(raw["device_id"]),
        binding_id=str(raw["binding_id"]),
        binding_version=context_int("binding_version"),
        active_subject_id=(
            str(raw["active_subject_id"])
            if raw.get("active_subject_id") is not None
            else None
        ),
        subject_revision=context_int("subject_revision"),
        session_epoch=context_int("session_epoch"),
        profile_revision=context_int("profile_revision"),
        current_runtime_profile_id=str(raw["current_runtime_profile_id"]),
        generation_id=context_int("generation_id"),
        turn_id=context_int("turn_id"),
        tool_epoch=context_int("tool_epoch"),
        created_at=created,
        updated_at=updated,
    )


def _require_transaction(connection: asyncpg.Connection) -> None:
    if not connection.is_in_transaction():
        raise RuntimeError("Session Runtime requires a caller-owned transaction")


class _ActionExecutorConnection:
    """Capability-reducing facade over one physical asyncpg connection.

    Policy's public Session batch currently reads its locked receipt through a
    table-shaped repository query after invoking ``policy_lock_receipt_v2``.
    The unified action role deliberately has no table SELECT.  This facade maps
    only that explicit lock/read sequence to Session's validated
    ``action_policy_lock_receipt`` SECURITY DEFINER port; every other operation
    stays on the same underlying connection and transaction.
    """

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    def is_in_transaction(self) -> bool:
        return bool(self._connection.is_in_transaction())

    async def execute(self, query: str, *args: object, **kwargs: object) -> str:
        return str(await self._connection.execute(query, *args, **kwargs))

    async def fetchval(
        self,
        query: str,
        *args: object,
        **kwargs: object,
    ) -> object:
        if "policy_lock_receipt_v2" in query:
            raw = await self._connection.fetchval(
                "SELECT action_policy_lock_receipt($1)",
                *args,
            )
            return raw is not None
        return await self._connection.fetchval(query, *args, **kwargs)

    async def fetchrow(
        self,
        query: str,
        *args: object,
        **kwargs: object,
    ) -> object:
        if "FROM policy_receipts_v2 WHERE receipt_id = $1" in query:
            raw = await self._connection.fetchval(
                "SELECT action_policy_lock_receipt($1)",
                *args,
            )
            if raw is None:
                return None
            receipt = _json_object(raw, field="policy receipt")
            for field in ("created_at", "expires_at"):
                value = receipt.get(field)
                if isinstance(value, str):
                    receipt[field] = datetime.fromisoformat(
                        value.replace("Z", "+00:00")
                    )
            return receipt
        return await self._connection.fetchrow(query, *args, **kwargs)

    async def fetch(
        self,
        query: str,
        *args: object,
        **kwargs: object,
    ) -> list[asyncpg.Record]:
        return list(await self._connection.fetch(query, *args, **kwargs))


class PostgresSessionRuntimeStore:
    """Deep persistence boundary for current context and immutable history."""

    def __init__(
        self,
        *,
        dsn: str,
        action_dsn: str,
        bootstrap_dsn: str | None = None,
        connect_timeout_seconds: float = 5.0,
        command_timeout_seconds: float = 10.0,
    ) -> None:
        self._dsn = dsn
        self._action_dsn = action_dsn
        self._bootstrap_dsn = bootstrap_dsn
        self._connect_timeout_seconds = connect_timeout_seconds
        self._command_timeout_seconds = command_timeout_seconds

    async def initialize(self) -> None:
        dsn = self._bootstrap_dsn or self._dsn
        connection = await self._connect(dsn, application_name="memoria-session-bootstrap")
        try:
            if self._bootstrap_dsn is not None:
                await connection.execute(SESSION_RUNTIME_SCHEMA_SQL)
            await self._verify_schema(connection)
            await self._verify_runtime_roles()
        except asyncpg.PostgresError as exc:
            raise SessionRuntimeAuthorityUnavailable(str(exc)) from exc
        finally:
            await connection.close()

    async def close(self) -> None:
        """Connections are transaction-scoped; retained for lifecycle symmetry."""

    async def _connect(
        self,
        dsn: str,
        *,
        application_name: str,
    ) -> asyncpg.Connection:
        return await asyncpg.connect(
            dsn,
            timeout=self._connect_timeout_seconds,
            command_timeout=self._command_timeout_seconds,
            server_settings={"application_name": application_name},
        )

    async def _verify_schema(self, connection: asyncpg.Connection) -> None:
        tables = {
            str(row["tablename"])
            for row in await connection.fetch(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename = ANY($1::text[])
                """,
                list(_REQUIRED_TABLES),
            )
        }
        rls_tables = {
            str(row["relname"]): (
                bool(row["relrowsecurity"]),
                bool(row["relforcerowsecurity"]),
            )
            for row in await connection.fetch(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relkind = 'r'
                  AND relname = ANY($1::text[])
                """,
                list(_REQUIRED_RLS_TABLES),
            )
        }
        required_rls_tables = {
            table_name: (True, True) for table_name in _REQUIRED_RLS_TABLES
        }
        policies = {
            (str(row["tablename"]), str(row["policyname"]))
            for row in await connection.fetch(
                """
                SELECT tablename, policyname
                FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename = ANY($1::text[])
                """,
                list(_REQUIRED_TABLES),
            )
        }
        roles = {
            str(row["rolname"])
            for row in await connection.fetch(
                """
                SELECT rolname FROM pg_roles
                WHERE rolname = ANY($1::text[])
                  AND NOT rolsuper AND NOT rolbypassrls
                """,
                list(_REQUIRED_ROLES),
            )
        }
        functions = {
            str(row["proname"])
            for row in await connection.fetch(
                """
                SELECT DISTINCT proname FROM pg_proc
                WHERE proname = ANY($1::text[])
                """,
                list(_REQUIRED_FUNCTIONS),
            )
        }
        if (
            tables != _REQUIRED_TABLES
            or rls_tables != required_rls_tables
            or policies != _REQUIRED_POLICIES
            or roles != _REQUIRED_ROLES
            or functions != _REQUIRED_FUNCTIONS
        ):
            raise SessionRuntimeAuthorityUnavailable(
                "Session Runtime schema, RLS, policies, or least-privilege roles are incomplete"
            )

    async def _verify_runtime_roles(self) -> bool:
        """Verify runtime role boundaries; report conditional Consent ports."""
        read = await self._connect(
            self._dsn,
            application_name="memoria-session-readiness",
        )
        try:
            if await read.fetchval("SELECT current_user") != "memoria_session_api":
                raise SessionRuntimeAuthorityUnavailable(
                    "Session Runtime read DSN has the wrong role"
                )
        finally:
            await read.close()
        action = await self._connect(
            self._action_dsn,
            application_name="memoria-action-readiness",
        )
        conditional_present = False
        try:
            row = await action.fetchrow(
                """
                SELECT current_user AS role,
                       has_table_privilege(
                           current_user, 'session_runtime_contexts', 'SELECT'
                       ) AS session_select,
                       has_table_privilege(
                           current_user, 'policy_receipts_v2', 'SELECT'
                       ) AS policy_select,
                       has_table_privilege(
                           current_user, 'identity_device_bindings', 'SELECT'
                       ) AS identity_select
                """
            )
            if row is None or row["role"] != "memoria_action_executor" or any(
                bool(row[field])
                for field in ("session_select", "policy_select", "identity_select")
            ):
                raise SessionRuntimeAuthorityUnavailable(
                    "action executor role has unsafe direct table privileges"
                )
            for table_name in _ACTION_EXECUTOR_TABLES:
                relation = await action.fetchval("SELECT to_regclass($1)", table_name)
                if relation is None:
                    continue
                privilege = await action.fetchval(
                    """
                    SELECT has_table_privilege(current_user, $1::regclass, 'SELECT')
                        OR has_table_privilege(current_user, $1::regclass, 'INSERT')
                        OR has_table_privilege(current_user, $1::regclass, 'UPDATE')
                        OR has_table_privilege(current_user, $1::regclass, 'DELETE')
                        OR has_table_privilege(current_user, $1::regclass, 'TRUNCATE')
                        OR has_table_privilege(current_user, $1::regclass, 'REFERENCES')
                        OR has_table_privilege(current_user, $1::regclass, 'TRIGGER')
                    """,
                    relation,
                )
                if privilege:
                    raise SessionRuntimeAuthorityUnavailable(
                        f"action executor role has direct table privileges on {table_name}"
                    )
            for signature in _ACTION_EXECUTOR_FUNCTION_SIGNATURES:
                row = await action.fetchrow(
                    """
                    SELECT proname,
                           has_function_privilege(
                               current_user, p.oid, 'EXECUTE'
                           ) AS can_execute
                    FROM pg_proc p
                    WHERE p.oid = to_regprocedure($1)
                    """,
                    signature,
                )
                if row is None or not bool(row["can_execute"]):
                    raise SessionRuntimeAuthorityUnavailable(
                        f"action executor role is missing EXECUTE on {signature}"
                    )
            for signature in _CONDITIONAL_ACTION_EXECUTOR_FUNCTION_SIGNATURES:
                exists = await action.fetchval(
                    "SELECT to_regprocedure($1) IS NOT NULL",
                    signature,
                )
                if not bool(exists):
                    continue
                conditional_present = True
                row = await action.fetchrow(
                    """
                    SELECT proname,
                           has_function_privilege(
                               current_user, p.oid, 'EXECUTE'
                           ) AS can_execute
                    FROM pg_proc p
                    WHERE p.oid = to_regprocedure($1)
                    """,
                    signature,
                )
                if row is None or not bool(row["can_execute"]):
                    raise SessionRuntimeAuthorityUnavailable(
                        f"action executor role is missing EXECUTE on {signature}"
                    )
            for signature in _WORKER_FUNCTION_SIGNATURES:
                row = await action.fetchrow(
                    """
                    SELECT proname,
                           has_function_privilege(
                               'memoria_session_worker', p.oid, 'EXECUTE'
                           ) AS can_execute
                    FROM pg_proc p
                    WHERE p.oid = to_regprocedure($1)
                    """,
                    signature,
                )
                if row is None or not bool(row["can_execute"]):
                    raise SessionRuntimeAuthorityUnavailable(
                        f"Session worker is missing EXECUTE on {signature}"
                    )
            for table_name in (
                "session_runtime_tool_effect_intents",
                "session_runtime_tool_effect_outbox",
            ):
                privilege = await action.fetchval(
                    """
                    SELECT has_table_privilege(
                        'memoria_session_worker', $1::regclass, 'SELECT'
                    )
                    OR has_table_privilege(
                        'memoria_session_worker', $1::regclass, 'UPDATE'
                    )
                    """,
                    table_name,
                )
                if privilege:
                    raise SessionRuntimeAuthorityUnavailable(
                        f"Session worker has direct table privileges on {table_name}"
                    )
        finally:
            await action.close()
        return conditional_present

    async def readiness(self) -> dict[str, str]:
        connection = await self._connect(
            self._dsn,
            application_name="memoria-session-readiness",
        )
        try:
            await self._verify_schema(connection)
            consent_discover_present = await self._verify_runtime_roles()
        except asyncpg.PostgresError as exc:
            raise SessionRuntimeAuthorityUnavailable(str(exc)) from exc
        finally:
            await connection.close()
        return {
            "session_runtime_schema": "ready",
            "session_runtime_rls": "ready",
            "session_runtime_policies": "ready",
            "session_runtime_read_role": "ready",
            "action_executor_role": "ready",
            "action_executor_exec": "ready",
            "action_executor_consent_discover_exec": (
                "ready" if consent_discover_present else "unavailable"
            ),
        }

    @asynccontextmanager
    async def action_transaction(
        self,
        *,
        actor_id: str,
        device_id: str,
        subject_id: str | None = None,
    ) -> AsyncIterator[asyncpg.Connection]:
        connection = await self._connect(
            self._action_dsn,
            application_name="memoria-action-executor",
        )
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('app.authenticated_actor', $1, true)",
                    actor_id,
                )
                await connection.execute(
                    "SELECT set_config('app.authenticated_device', $1, true)",
                    device_id,
                )
                if subject_id is not None:
                    await connection.execute(
                        "SELECT set_config('app.authenticated_subject', $1, true)",
                        subject_id,
                    )
                yield cast(
                    asyncpg.Connection,
                    _ActionExecutorConnection(connection),
                )
        finally:
            await connection.close()

    @staticmethod
    async def set_action_subject(
        connection: asyncpg.Connection,
        subject_id: str | None,
    ) -> None:
        """Bind the authoritative active subject on the current action txn."""
        _require_transaction(connection)
        await connection.execute(
            "SELECT set_config('app.authenticated_subject', $1, true)",
            subject_id or "",
        )

    @asynccontextmanager
    async def read_transaction(
        self,
        *,
        actor_id: str,
    ) -> AsyncIterator[asyncpg.Connection]:
        connection = await self._connect(
            self._dsn,
            application_name="memoria-session-api",
        )
        try:
            async with connection.transaction(readonly=True):
                await connection.execute(
                    "SELECT set_config('app.session_actor', $1, true)",
                    actor_id,
                )
                yield connection
        finally:
            await connection.close()

    async def persist_initial(
        self,
        connection: asyncpg.Connection,
        *,
        context: SessionRuntimeContext,
        profile: RuntimeProfileSignedV2,
        event: SessionEvent,
        request_hash: str,
        idempotency_key: str,
    ) -> RuntimeProfileSignedV2:
        _require_transaction(connection)
        self._validate_initial(context=context, profile=profile, event=event)
        replay = await self.prepare_initial(
            connection,
            context=context,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            return replay
        return await self.commit_initial(
            connection,
            context=context,
            profile=profile,
            event=event,
            idempotency_key=idempotency_key,
        )

    async def prepare_initial(
        self,
        connection: asyncpg.Connection,
        *,
        context: SessionRuntimeContext,
        request_hash: str,
        idempotency_key: str,
    ) -> RuntimeProfileSignedV2 | None:
        _require_transaction(connection)
        try:
            prepared_raw = await connection.fetchval(
                "SELECT session_runtime_prepare_initial($1::jsonb, $2, $3)",
                _json_text(context.to_json_dict()),
                request_hash,
                idempotency_key,
            )
            prepared = _json_object(prepared_raw, field="prepare result")
            if prepared.get("status") == "replay":
                loaded = await self.current_profile(
                    connection,
                    session_id=context.session_id,
                )
                if loaded is None:
                    raise SessionRuntimeConflict(
                        "idempotent profile replay is unavailable"
                    )
                return loaded
            return None
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)
        raise AssertionError("unreachable")

    async def commit_initial(
        self,
        connection: asyncpg.Connection,
        *,
        context: SessionRuntimeContext,
        profile: RuntimeProfileSignedV2,
        event: SessionEvent,
        idempotency_key: str,
    ) -> RuntimeProfileSignedV2:
        _require_transaction(connection)
        self._validate_initial(context=context, profile=profile, event=event)
        try:
            committed = await connection.fetchval(
                "SELECT session_runtime_commit_initial($1::jsonb, $2, $3::jsonb, $4)",
                _json_text(profile.model_dump(mode="json")),
                context.profile_revision,
                _json_text(event.model_dump(mode="json")),
                idempotency_key,
            )
            return _profile_from_json(committed)
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)
        raise AssertionError("unreachable")

    async def current_profile(
        self,
        connection: asyncpg.Connection,
        *,
        session_id: str,
    ) -> RuntimeProfileSignedV2 | None:
        _require_transaction(connection)
        current_user = await connection.fetchval("SELECT current_user")
        if current_user == "memoria_action_executor":
            raw = await connection.fetchval(
                "SELECT session_runtime_action_current_profile($1)",
                session_id,
            )
        else:
            raw = await connection.fetchval(
                """
                SELECT p.payload_json
                FROM session_runtime_contexts c
                JOIN session_runtime_profiles p
                  ON p.runtime_profile_id = c.current_runtime_profile_id
                WHERE c.session_id = $1 AND c.state = 'active'
                """,
                session_id,
            )
        return None if raw is None else _profile_from_json(raw)

    async def profile_by_id(
        self,
        connection: asyncpg.Connection,
        *,
        runtime_profile_id: str,
    ) -> RuntimeProfileSignedV2 | None:
        _require_transaction(connection)
        raw = await connection.fetchval(
            """
            SELECT payload_json
            FROM session_runtime_profiles
            WHERE runtime_profile_id = $1
            """,
            runtime_profile_id,
        )
        return None if raw is None else _profile_from_json(raw)

    async def current_context(
        self,
        connection: asyncpg.Connection,
        *,
        session_id: str,
    ) -> SessionRuntimeContext | None:
        _require_transaction(connection)
        row = await connection.fetchrow(
            """
            SELECT session_id, actor_id, device_id, binding_id, binding_version,
                   active_subject_id, subject_revision, session_epoch,
                   profile_revision, current_runtime_profile_id,
                   generation_id, turn_id, tool_epoch, created_at, updated_at
            FROM session_runtime_contexts
            WHERE session_id = $1 AND state = 'active'
            """,
            session_id,
        )
        if row is None:
            return None
        return SessionRuntimeContext(
            session_id=str(row["session_id"]),
            actor_id=str(row["actor_id"]),
            device_id=str(row["device_id"]),
            binding_id=str(row["binding_id"]),
            binding_version=int(row["binding_version"]),
            active_subject_id=(
                str(row["active_subject_id"])
                if row["active_subject_id"] is not None
                else None
            ),
            subject_revision=int(row["subject_revision"]),
            session_epoch=int(row["session_epoch"]),
            profile_revision=int(row["profile_revision"]),
            current_runtime_profile_id=str(row["current_runtime_profile_id"]),
            generation_id=int(row["generation_id"]),
            turn_id=int(row["turn_id"]),
            tool_epoch=int(row["tool_epoch"]),
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
        )

    async def context_any_state(
        self,
        connection: asyncpg.Connection,
        *,
        session_id: str,
    ) -> tuple[SessionRuntimeContext, str] | None:
        """Read the context row regardless of lifecycle state.

        Returns ``(context, state)`` so callers can distinguish an idempotent
        already-closed session from an unknown or failed one without exposing
        the lifecycle state on the shared context dataclass.
        """
        _require_transaction(connection)
        row = await connection.fetchrow(
            """
            SELECT session_id, actor_id, device_id, binding_id, binding_version,
                   active_subject_id, subject_revision, session_epoch,
                   profile_revision, current_runtime_profile_id,
                   generation_id, turn_id, tool_epoch, created_at, updated_at,
                   state
            FROM session_runtime_contexts
            WHERE session_id = $1
            """,
            session_id,
        )
        if row is None:
            return None
        return (
            SessionRuntimeContext(
                session_id=str(row["session_id"]),
                actor_id=str(row["actor_id"]),
                device_id=str(row["device_id"]),
                binding_id=str(row["binding_id"]),
                binding_version=int(row["binding_version"]),
                active_subject_id=(
                    str(row["active_subject_id"])
                    if row["active_subject_id"] is not None
                    else None
                ),
                subject_revision=int(row["subject_revision"]),
                session_epoch=int(row["session_epoch"]),
                profile_revision=int(row["profile_revision"]),
                current_runtime_profile_id=str(row["current_runtime_profile_id"]),
                generation_id=int(row["generation_id"]),
                turn_id=int(row["turn_id"]),
                tool_epoch=int(row["tool_epoch"]),
                created_at=cast(datetime, row["created_at"]),
                updated_at=cast(datetime, row["updated_at"]),
            ),
            str(row["state"]),
        )

    async def lock_tool_effect_context(
        self,
        connection: asyncpg.Connection,
        *,
        session_id: str,
        runtime_profile_id: str,
        fence: Mapping[str, object],
        profile: Mapping[str, object],
    ) -> tuple[RuntimeProfileSignedV2, SessionRuntimeContext]:
        """Lock the current Session context/profile on the caller transaction."""
        _require_transaction(connection)
        try:
            raw = await connection.fetchval(
                """
                SELECT session_runtime_action_effect_context($1, $2, $3::jsonb, $4::jsonb)
                """,
                session_id,
                runtime_profile_id,
                _json_text(fence),
                _json_text(profile),
            )
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)
        authority = _json_object(raw, field="tool effect authority")
        current_profile = _profile_from_json(authority.get("profile"))
        current_context = _context_from_json(authority.get("context"))
        return current_profile, current_context

    async def commit_tool_effect(
        self,
        connection: asyncpg.Connection,
        *,
        intent: Mapping[str, object],
    ) -> dict[str, object]:
        """Insert one durable intent and its outbox row atomically."""
        _require_transaction(connection)
        try:
            raw = await connection.fetchval(
                "SELECT session_runtime_commit_tool_effect($1::jsonb)",
                _json_text(intent),
            )
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)
        return _json_object(raw, field="tool effect commit receipt")

    async def reconcile_tool_effect(
        self,
        connection: asyncpg.Connection,
        *,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Read the durable outcome through the narrow action port."""
        _require_transaction(connection)
        try:
            raw = await connection.fetchval(
                "SELECT session_runtime_reconcile_tool_effect($1)",
                idempotency_key,
            )
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)
        return _json_object(raw, field="tool effect reconcile result")

    async def rotate_profile(
        self,
        connection: asyncpg.Connection,
        *,
        expected_runtime_profile_id: str,
        expected_profile_revision: int,
        expected_session_epoch: int,
        profile: RuntimeProfileSignedV2,
        event: SessionEvent,
    ) -> RuntimeProfileSignedV2:
        _require_transaction(connection)
        if (
            profile.session_epoch != expected_session_epoch + 1
            or event.session_id != profile.session_id
            or event.session_epoch != profile.session_epoch
            or event.runtime_profile_id != profile.runtime_profile_id
            or event.actor_id != profile.actor_id
            or event.binding_id != profile.binding_id
            or event.binding_version != profile.binding_version
            or event.active_subject_id != profile.active_subject_id
            or event.subject_revision != profile.subject_revision
        ):
            raise ValueError("rotated profile event or epoch fence is invalid")
        try:
            await connection.fetchval(
                """
                SELECT session_runtime_prepare_rotation(
                    $1, $2, $3, $4::jsonb, $5, $6::jsonb
                )
                """,
                expected_runtime_profile_id,
                expected_profile_revision,
                expected_session_epoch,
                _json_text(profile.model_dump(mode="json")),
                expected_profile_revision + 1,
                _json_text(event.model_dump(mode="json")),
            )
            committed = await connection.fetchval(
                "SELECT session_runtime_commit_rotation($1::jsonb, $2, $3::jsonb)",
                _json_text(profile.model_dump(mode="json")),
                expected_profile_revision + 1,
                _json_text(event.model_dump(mode="json")),
            )
            return _profile_from_json(committed)
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)
        raise AssertionError("unreachable")

    async def prepare_rotation(
        self,
        connection: asyncpg.Connection,
        *,
        expected: SessionRuntimeContext,
        profile: RuntimeProfileSignedV2,
        event: SessionEvent,
    ) -> None:
        _require_transaction(connection)
        try:
            await connection.fetchval(
                """
                SELECT session_runtime_prepare_rotation(
                    $1, $2, $3, $4::jsonb, $5, $6::jsonb
                )
                """,
                expected.current_runtime_profile_id,
                expected.profile_revision,
                expected.session_epoch,
                _json_text(profile.model_dump(mode="json")),
                expected.profile_revision + 1,
                _json_text(event.model_dump(mode="json")),
            )
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)

    async def commit_rotation(
        self,
        connection: asyncpg.Connection,
        *,
        profile_revision: int,
        profile: RuntimeProfileSignedV2,
        event: SessionEvent,
    ) -> RuntimeProfileSignedV2:
        _require_transaction(connection)
        try:
            committed = await connection.fetchval(
                "SELECT session_runtime_commit_rotation($1::jsonb, $2, $3::jsonb)",
                _json_text(profile.model_dump(mode="json")),
                profile_revision,
                _json_text(event.model_dump(mode="json")),
            )
            return _profile_from_json(committed)
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)
        raise AssertionError("unreachable")

    async def fail_session(
        self,
        connection: asyncpg.Connection,
        *,
        expected: SessionRuntimeContext,
        event: SessionEvent,
    ) -> None:
        _require_transaction(connection)
        try:
            failed = await connection.fetchval(
                "SELECT session_runtime_fail_session($1, $2, $3, $4::jsonb)",
                expected.session_id,
                expected.current_runtime_profile_id,
                expected.session_epoch,
                _json_text(event.model_dump(mode="json")),
            )
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)
        if failed is not True:
            raise SessionRuntimeAuthorityUnavailable(
                "Session failure transition was not applied"
            )

    async def close_session(
        self,
        connection: asyncpg.Connection,
        *,
        expected: SessionRuntimeContext,
        event: SessionEvent,
    ) -> str:
        """Apply or replay the terminal session_closed authority transition.

        Returns closed when applied and already_closed on an idempotent
        replay of the pre-close or observed terminal epoch; a failed session
        stays failed and surfaces the CAS conflict instead of relabeling closed.
        """
        _require_transaction(connection)
        try:
            raw = await connection.fetchval(
                "SELECT session_runtime_close_session($1, $2, $3, $4::jsonb)",
                expected.session_id,
                expected.current_runtime_profile_id,
                expected.session_epoch,
                _json_text(event.model_dump(mode="json")),
            )
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)
        status = _json_object(raw, field="close result").get("status")
        if status not in {"closed", "already_closed"}:
            raise SessionRuntimeAuthorityUnavailable(
                "Session close transition was not applied"
            )
        return str(status)

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipts: tuple[PolicyReceiptV2, ...],
    ) -> None:
        """Policy Session batch adapter: lock one provisional/current profile head."""
        _require_transaction(connection)
        if not receipts:
            raise SessionRuntimeAuthorityUnavailable("profile receipt batch is empty")
        first = receipts[0]
        if any(
            (
                item.session_id,
                item.runtime_profile_id,
                item.actor_id,
                item.device_id,
                item.binding_id,
                item.binding_version,
                item.session_epoch,
            )
            != (
                first.session_id,
                first.runtime_profile_id,
                first.actor_id,
                first.device_id,
                first.binding_id,
                first.binding_version,
                first.session_epoch,
            )
            for item in receipts
        ):
            raise SessionRuntimeAuthorityUnavailable(
                "profile receipt batch crosses a Session authority fence"
            )
        try:
            valid = await connection.fetchval(
                """
                SELECT session_runtime_assert_action_context(
                    $1, $2, $3, $4, $5, $6, $7
                )
                """,
                first.session_id,
                first.runtime_profile_id,
                first.actor_id,
                first.device_id,
                first.binding_id,
                first.binding_version,
                first.session_epoch,
            )
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)
        if valid is not True:
            raise SessionRuntimeAuthorityUnavailable(
                "current profile authority did not validate"
            )

    async def advance_action_fence(
        self,
        connection: asyncpg.Connection,
        *,
        context: SessionRuntimeContext,
        fence_kind: str,
        next_generation_id: int,
        next_turn_id: int,
        next_tool_epoch: int,
        occurred_at: datetime,
    ) -> SessionRuntimeContext:
        """Authoritatively advance one legal monotonic Session action fence.

        ``fence_kind`` is ``turn``, ``interrupt`` or ``tool``.  The database
        locks the current context row and rejects any transition that skips,
        rewinds or mixes epochs, or any claimed context that no longer matches
        the authoritative row.  The returned context carries the committed
        fence.
        """
        _require_transaction(connection)
        if fence_kind == "turn":
            legal = (
                next_turn_id == context.turn_id + 1
                and next_generation_id == context.generation_id + 1
                and next_tool_epoch == context.tool_epoch
            )
        elif fence_kind == "interrupt":
            legal = (
                next_turn_id == context.turn_id
                and next_generation_id == context.generation_id + 1
                and next_tool_epoch == context.tool_epoch
            )
        elif fence_kind == "tool":
            legal = (
                next_turn_id == context.turn_id
                and next_generation_id == context.generation_id + 1
                and next_tool_epoch == context.tool_epoch + 1
            )
        else:
            raise ValueError("action fence kind is invalid")
        if not legal:
            raise ValueError("action fence transition is not monotonic")
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("action fence timestamps must be aware")
        try:
            raw = await connection.fetchval(
                "SELECT session_runtime_advance_action_fence($1::jsonb)",
                _json_text(
                    {
                        "session_id": context.session_id,
                        "runtime_profile_id": context.current_runtime_profile_id,
                        "actor_id": context.actor_id,
                        "device_id": context.device_id,
                        "binding_id": context.binding_id,
                        "binding_version": context.binding_version,
                        "session_epoch": context.session_epoch,
                        "active_subject_id": context.active_subject_id,
                        "subject_revision": context.subject_revision,
                        "fence_kind": fence_kind,
                        "generation_id": next_generation_id,
                        "turn_id": next_turn_id,
                        "tool_epoch": next_tool_epoch,
                        "occurred_at": occurred_at.isoformat(),
                    }
                ),
            )
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)
        advanced = _json_object(raw, field="advanced action fence")
        if advanced.get("status") != "advanced":
            raise SessionRuntimeAuthorityUnavailable(
                "action fence advance was not applied"
            )
        return SessionRuntimeContext(
            session_id=context.session_id,
            actor_id=context.actor_id,
            device_id=context.device_id,
            binding_id=context.binding_id,
            binding_version=context.binding_version,
            active_subject_id=context.active_subject_id,
            subject_revision=context.subject_revision,
            session_epoch=context.session_epoch,
            profile_revision=context.profile_revision,
            current_runtime_profile_id=context.current_runtime_profile_id,
            generation_id=next_generation_id,
            turn_id=next_turn_id,
            tool_epoch=next_tool_epoch,
            created_at=context.created_at,
            updated_at=occurred_at,
        )

    async def lock_receipt_authority(
        self,
        connection: asyncpg.Connection,
        *,
        receipt: PolicyReceiptV2,
        generation_id: int,
        turn_id: int,
        tool_epoch: int,
    ) -> PolicyReceiptV2 | None:
        """Lock one policy receipt by its exact Session action fence.

        The database locks the immutable receipt row and the current context,
        then requires every Session fence field (session/profile/actor/device/
        binding/session epoch/subject revision plus generation/turn/tool
        epochs) to match exactly.  Replaying the same receipt at the same
        fence is idempotent and returns the same locked receipt; a receipt
        that no longer matches the current fence fails closed.
        """
        _require_transaction(connection)
        try:
            raw = await connection.fetchval(
                "SELECT session_runtime_action_receipt_authority($1, $2::jsonb)",
                receipt.receipt_id,
                _json_text(
                    {
                        "session_id": receipt.session_id,
                        "runtime_profile_id": receipt.runtime_profile_id,
                        "actor_id": receipt.actor_id,
                        "device_id": receipt.device_id,
                        "binding_id": receipt.binding_id,
                        "binding_version": receipt.binding_version,
                        "session_epoch": receipt.session_epoch,
                        "subject_revision": receipt.subject_revision,
                        "generation_id": generation_id,
                        "turn_id": turn_id,
                        "tool_epoch": tool_epoch,
                    }
                ),
            )
        except asyncpg.PostgresError as exc:
            self._raise_mapped(exc)
        if raw is None:
            return None
        locked = _json_object(raw, field="locked receipt")
        return PolicyReceiptV2.model_validate(locked)

    @staticmethod
    def _raise_mapped(exc: asyncpg.PostgresError) -> None:
        if exc.sqlstate in {"SR409", "SR412", "23505"}:
            raise SessionRuntimeConflict(str(exc)) from exc
        if exc.sqlstate in {"SR400", "SR403", "42501", "42883"}:
            raise SessionRuntimeAuthorityUnavailable(str(exc)) from exc
        raise exc

    @staticmethod
    def _validate_initial(
        *,
        context: SessionRuntimeContext,
        profile: RuntimeProfileSignedV2,
        event: SessionEvent,
    ) -> None:
        expected_context = SessionRuntimeContext.from_profile(
            profile,
            profile_revision=context.profile_revision,
            generation_id=context.generation_id,
            turn_id=context.turn_id,
            tool_epoch=context.tool_epoch,
        )
        if context != expected_context:
            raise ValueError("session context does not match signed profile")
        if (
            event.session_id != profile.session_id
            or event.session_epoch != profile.session_epoch
            or event.runtime_profile_id != profile.runtime_profile_id
            or event.actor_id != profile.actor_id
            or event.binding_id != profile.binding_id
            or event.binding_version != profile.binding_version
            or event.active_subject_id != profile.active_subject_id
            or event.subject_revision != profile.subject_revision
        ):
            raise ValueError("session event does not match signed profile fence")


def record_mapping(row: object) -> Mapping[str, Any]:
    """Narrow cast helper retained for strict asyncpg adapters."""
    return cast(Mapping[str, Any], row)
