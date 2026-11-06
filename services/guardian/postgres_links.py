"""PostgreSQL guardian link lifecycle mixin.

``PostgresGuardianLinkMixin`` is composed into ``PostgresGuardianStore``. The
store owns the pool, the API context setter and the row mappers; this module
owns link creation, binding-code verification and the link read paths.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import asyncpg

from services.guardian.domain import (
    GuardianAccessDeniedError,
    GuardianConflictError,
    GuardianLink,
    GuardianLinkStatus,
    GuardianNotFoundError,
    Relation,
    VerifiedVia,
)
from services.guardian.postgres_helpers import _hash, _timestamp, _uuid


class PostgresGuardianLinkMixin:
    """Create, confirm and read guardian links."""

    if TYPE_CHECKING:
        # Shared members owned by PostgresGuardianStore.
        async def _ready_pool(self) -> asyncpg.Pool: ...

        @staticmethod
        async def _set_api_context(
            connection: asyncpg.Connection,
            *,
            actor_user_id: str,
            subject_user_id: str,
        ) -> None: ...

        @staticmethod
        def _link(row: asyncpg.Record) -> GuardianLink: ...

    async def create_link(
        self,
        *,
        link_id: str | None = None,
        guardian_user_id: str,
        minor_user_id: str,
        relation: Relation,
        verified_via: VerifiedVia,
        binding_code_hash: str,
        binding_expires_at: datetime,
        now: datetime,
    ) -> GuardianLink:
        link = GuardianLink(
            link_id=str(uuid.uuid4()) if link_id is None else str(_uuid(link_id, field="link_id")),
            guardian_user_id=guardian_user_id,
            minor_user_id=minor_user_id,
            relation=relation,
            status="pending",
            verified_via=verified_via,
            created_at=_timestamp(now, field="now"),
            binding_expires_at=_timestamp(binding_expires_at, field="binding_expires_at"),
        )
        digest = _hash(binding_code_hash)
        pool = await self._ready_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=guardian_user_id,
                    subject_user_id=minor_user_id,
                )
                row = await connection.fetchrow(
                    """
                    INSERT INTO guardian_links(
                        link_id, guardian_user_id, minor_user_id, relation, status,
                        verified_via, binding_code_hash, binding_expires_at, created_at
                    ) VALUES ($1,$2,$3,$4,'pending',$5,$6,$7,$8)
                    RETURNING *
                    """,
                    uuid.UUID(link.link_id),
                    link.guardian_user_id,
                    link.minor_user_id,
                    link.relation,
                    link.verified_via,
                    digest,
                    link.binding_expires_at,
                    link.created_at,
                )
        except asyncpg.UniqueViolationError as exc:
            raise GuardianConflictError("a live guardian link already exists") from exc
        if row is None:  # pragma: no cover
            raise RuntimeError("guardian link insert failed")
        return self._link(row)

    async def verify_binding_code(
        self,
        *,
        link_id: str,
        minor_user_id: str,
        binding_code_hash: str,
        now: datetime,
    ) -> GuardianLink:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=minor_user_id,
                    subject_user_id=minor_user_id,
                )
                row = await connection.fetchrow(
                    """
                    SELECT * FROM guardian_links
                    WHERE link_id = $1 AND minor_user_id = $2
                      AND status = 'pending' AND binding_code_hash = $3
                      AND binding_expires_at > $4
                    """,
                    _uuid(link_id, field="link_id"),
                    minor_user_id,
                    _hash(binding_code_hash),
                    _timestamp(now, field="now"),
                )
        if row is None:
            raise GuardianAccessDeniedError("binding code is invalid or expired")
        return self._link(row)

    async def confirm_link(
        self,
        *,
        link_id: str,
        minor_user_id: str,
        binding_code_hash: str,
        now: datetime,
    ) -> GuardianLink:
        confirmed_at = _timestamp(now, field="now")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_api_context(
                connection,
                actor_user_id=minor_user_id,
                subject_user_id=minor_user_id,
            )
            row = await connection.fetchrow(
                """
                UPDATE guardian_links
                SET status = 'active', activated_at = $4
                WHERE link_id = $1 AND minor_user_id = $2
                  AND status = 'pending' AND binding_code_hash = $3
                  AND binding_expires_at > $4
                RETURNING *
                """,
                _uuid(link_id, field="link_id"),
                minor_user_id,
                _hash(binding_code_hash),
                confirmed_at,
            )
            if row is None:
                row = await connection.fetchrow(
                    """
                    SELECT * FROM guardian_links
                    WHERE link_id = $1 AND minor_user_id = $2
                      AND status = 'active' AND binding_code_hash = $3
                    """,
                    _uuid(link_id, field="link_id"),
                    minor_user_id,
                    _hash(binding_code_hash),
                )
        if row is None:
            raise GuardianAccessDeniedError("binding code is invalid or expired")
        return self._link(row)

    async def get_link(self, *, link_id: str, actor_user_id: str) -> GuardianLink:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=actor_user_id,
                    subject_user_id=actor_user_id,
                )
                row = await connection.fetchrow(
                    """
                    SELECT * FROM guardian_links
                    WHERE link_id = $1
                      AND (guardian_user_id = $2 OR minor_user_id = $2)
                    """,
                    _uuid(link_id, field="link_id"),
                    actor_user_id,
                )
        if row is None:
            raise GuardianNotFoundError("guardian link not found")
        return self._link(row)

    async def active_link(
        self,
        *,
        guardian_user_id: str,
        minor_user_id: str,
    ) -> GuardianLink | None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=guardian_user_id,
                    subject_user_id=minor_user_id,
                )
                row = await connection.fetchrow(
                    """
                    SELECT * FROM guardian_links
                    WHERE guardian_user_id = $1 AND minor_user_id = $2 AND status = 'active'
                    ORDER BY activated_at DESC LIMIT 1
                    """,
                    guardian_user_id,
                    minor_user_id,
                )
        return self._link(row) if row is not None else None

    async def list_links(
        self,
        *,
        guardian_user_id: str,
        statuses: tuple[GuardianLinkStatus, ...] = ("pending", "active"),
    ) -> tuple[GuardianLink, ...]:
        allowed = {"pending", "active", "revoked"}
        if not statuses or any(status not in allowed for status in statuses):
            raise ValueError("guardian link statuses are invalid")
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=guardian_user_id,
                    subject_user_id=guardian_user_id,
                )
                rows = await connection.fetch(
                    """
                    SELECT * FROM guardian_links
                    WHERE guardian_user_id = $1 AND status = ANY($2::text[])
                    ORDER BY created_at DESC
                    """,
                    guardian_user_id,
                    list(statuses),
                )
        return tuple(self._link(row) for row in rows)

    async def list_links_for_actor(
        self,
        *,
        actor_user_id: str,
        statuses: tuple[GuardianLinkStatus, ...] = ("pending", "active"),
    ) -> tuple[GuardianLink, ...]:
        allowed = {"pending", "active", "revoked"}
        if not statuses or any(status not in allowed for status in statuses):
            raise ValueError("guardian link statuses are invalid")
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=actor_user_id,
                    subject_user_id=actor_user_id,
                )
                rows = await connection.fetch(
                    """
                    SELECT * FROM guardian_links
                    WHERE (guardian_user_id = $1 OR minor_user_id = $1)
                      AND status = ANY($2::text[])
                    ORDER BY created_at DESC
                    """,
                    actor_user_id,
                    list(statuses),
                )
        return tuple(self._link(row) for row in rows)

    async def active_guardian_links(
        self,
        *,
        minor_user_id: str,
    ) -> tuple[GuardianLink, ...]:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=minor_user_id,
                    subject_user_id=minor_user_id,
                )
                rows = await connection.fetch(
                    """
                    SELECT * FROM guardian_links
                    WHERE minor_user_id = $1 AND status = 'active'
                    ORDER BY activated_at DESC
                    """,
                    minor_user_id,
                )
        return tuple(self._link(row) for row in rows)
