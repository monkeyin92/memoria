"""PostgreSQL device-binding persistence mixin for the identity store.

``PostgresIdentityBindingMixin`` owns binding persistence, lookup and version
transitions.  The store keeps the pool, transaction context and the person /
relationship / persona families; shared state is declared here for type
checking only.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, cast

import asyncpg
from services.identity.domain import (
    BindingStatus,
    BindingVersionConflictError,
    DeviceBinding,
    IdentityAccessDeniedError,
    IdentityNotFoundError,
)
from services.identity.postgres_helpers import _timestamp
from services.identity.postgres_rows import _binding
from services.identity.repository import AuditEvent, OutboxEvent
from services.identity.subject_release import BindingRevocation, SubjectRelease


class PostgresIdentityBindingMixin:
    """Read and transition device binding rows."""

    if TYPE_CHECKING:
        async def _ready(self) -> asyncpg.Pool: ...

        @staticmethod
        async def _apply_context(
            connection: asyncpg.Connection,
            *,
            actor_person_id: str | None,
            scope: str,
        ) -> None: ...


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
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    binding.device_id,
                )
                await _lock_binding_subjects(connection, binding, previous_binding_id)
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
                        "SELECT identity_next_binding_version($1) AS version",
                        binding.device_id,
                    )
                    version = int(version_row["version"])
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
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
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
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
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

    async def list_active_bindings_for_person(
        self,
        person_id: str,
        now: datetime,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[DeviceBinding, ...]:
        pool = await self._ready()
        timestamp = _timestamp(now, field="now")
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
                rows = await connection.fetch(
                    """
                    SELECT * FROM identity_device_bindings AS binding
                    WHERE binding.status = 'active'
                      AND binding.valid_from <= $2
                      AND (binding.valid_until IS NULL OR binding.valid_until > $2)
                      AND (
                        binding.account_owner_person_id = $1
                        OR EXISTS (
                            SELECT 1 FROM identity_device_binding_roles AS role
                            WHERE role.binding_id = binding.binding_id
                              AND role.person_id = $1
                              AND role.status = 'active'
                              AND role.ended_at IS NULL
                        )
                      )
                    ORDER BY binding.device_id, binding.binding_version DESC
                    """,
                    person_id,
                    timestamp,
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

    async def list_binding_versions(
        self,
        device_id: str,
        *,
        actor_person_id: str | None = None,
        scope: str = "api",
    ) -> tuple[DeviceBinding, ...]:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
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
        pool = await self._ready()
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

    async def revoke_binding(self, binding_id: str, *, at: datetime, actor_person_id: str,
                             audit_event: AuditEvent | None = None, outbox_event: OutboxEvent | None = None) -> BindingRevocation:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope="api")
                try:
                    raw = await connection.fetchval("SELECT identity_revoke_binding($1,$2,$3)", binding_id, actor_person_id, at)
                except asyncpg.PostgresError as exc:
                    if exc.sqlstate == "IB403":
                        raise IdentityAccessDeniedError("binding manage denied") from exc
                    if exc.sqlstate == "IB409":
                        raise BindingVersionConflictError("binding is no longer active") from exc
                    raise
                row = await connection.fetchrow("SELECT * FROM identity_device_bindings WHERE binding_id=$1", binding_id)
                roles = await connection.fetch("SELECT * FROM identity_device_binding_roles WHERE binding_id=$1", binding_id)
                return BindingRevocation(_binding(row, roles), tuple(SubjectRelease(**s) for s in json.loads(raw)))

    async def prepare_account_subject_erasure(self, account_id: str) -> tuple[str, ...]:
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=account_id, scope="api")
                return tuple(await connection.fetchval("SELECT identity_prepare_account_subject_erasure($1)", account_id))

    async def pending_subject_releases(self, *, limit: int = 100) -> tuple[tuple[str, str], ...]:
        pool = await self._ready()
        async with pool.acquire() as connection:
            rows = await connection.fetch("SELECT * FROM identity_pending_subject_releases($1)", limit)
            return tuple((r["account_id"], r["subject_id"]) for r in rows)

    async def complete_subject_release(self, account_id: str, subject_id: str) -> None:
        pool = await self._ready()
        async with pool.acquire() as connection:
            await connection.execute("SELECT identity_complete_subject_release($1,$2)", account_id, subject_id)

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
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
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


async def _lock_binding_subjects(connection: asyncpg.Connection, binding: DeviceBinding, previous_id: str | None) -> None:
    subjects = {r.person_id for r in binding.roles}
    if previous_id is not None:
        subjects.update(await connection.fetchval(
            "SELECT array_agg(person_id) FROM identity_device_binding_roles WHERE binding_id=$1", previous_id,
        ) or ())
    try:
        await connection.execute(
            "SELECT identity_lock_binding_subjects($1,$2::text[])", binding.device_id, sorted(subjects),
        )
    except asyncpg.PostgresError as exc:
        if exc.sqlstate == "IB409":
            raise BindingVersionConflictError("subject is released or unavailable") from exc
        raise


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
