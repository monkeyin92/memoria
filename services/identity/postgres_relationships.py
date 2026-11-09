"""PostgreSQL relationship persistence mixin for the identity store.

``PostgresIdentityRelationshipMixin`` is composed into
``PostgresIdentityStore``; the pool and transaction context stay owned by the
store.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, cast

import asyncpg
from services.identity.domain import (
    IdentityConflictError,
    IdentityNotFoundError,
    Relationship,
    RelationshipStatus,
)
from services.identity.postgres_helpers import _timestamp
from services.identity.postgres_rows import _relationship
from services.identity.repository import AuditEvent, OutboxEvent


class PostgresIdentityRelationshipMixin:
    """Read and transition relationship rows."""

    if TYPE_CHECKING:
        async def _ready(self) -> asyncpg.Pool: ...

        @staticmethod
        async def _apply_context(
            connection: asyncpg.Connection,
            *,
            actor_person_id: str | None,
            scope: str,
        ) -> None: ...

    async def has_active_relationship(
        self,
        *,
        source_person_id: str,
        target_person_id: str,
        relation_type: str,
        at: datetime,
    ) -> bool:
        pool = await self._ready()
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

    async def has_source_confirmed_relationship(
        self,
        *,
        source_person_id: str,
        target_person_id: str,
        relation_type: str,
        at: datetime,
    ) -> bool:
        """True for a one-sided declaration: source confirmed, target never did.

        The row stays ``pending``, so it is a declaration of the source
        endpoint, never verified guardianship.
        """

        pool = await self._ready()
        async with pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    """
                    SELECT identity_relationship_source_confirmed($1, $2, $3, $4)
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
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
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
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
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
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
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
        pool = await self._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._apply_context(connection, actor_person_id=actor_person_id, scope=scope)
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
