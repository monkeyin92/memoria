"""PostgreSQL record persistence and read mixin.

``PostgresMemoryRecordMixin`` is composed into ``PostgresMemoryStore``. The
store owns the pool, the role gates, the transaction context setter and the
outbox/audit inserts; this module owns record writes, the caller-owned
sensitive commit and the status-event read paths.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import asyncpg

from services.memory_scope.domain import (
    MemoryAuditEvent,
    MemoryOutboxEvent,
    MemoryRecord,
    MemoryRecordStatusEvent,
    MemoryScope,
    MemoryStatus,
    RetentionPolicy,
)
from services.memory_scope.postgres_helpers import (
    _jsonb_dict,
    _jsonb_list,
    _now,
    _parse_iso,
)


class PostgresMemoryRecordMixin:
    """Persist and read memory records and their status events."""

    if TYPE_CHECKING:
        # Shared members owned by PostgresMemoryStore.
        def _require_api(self) -> None: ...

        def _require_pool(self) -> asyncpg.Pool: ...

        @staticmethod
        async def _scope(
            connection: asyncpg.Connection,
            **values: str | int | None,
        ) -> None: ...

        async def _insert_outbox(
            self, connection: asyncpg.Connection, events: tuple[MemoryOutboxEvent, ...]
        ) -> None: ...

        async def _insert_audit(
            self, connection: asyncpg.Connection, events: tuple[MemoryAuditEvent, ...]
        ) -> None: ...

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
