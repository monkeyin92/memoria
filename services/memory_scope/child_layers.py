"""Parent-visible child memory layers on ``memory_records``.

``dialogue_summary`` (30-day physical TTL) and ``child_archive`` (idle 180-day
TTL, separate consent).  Writes go through the memory API role; deletes go
through the maintenance kind-erase ports so append-only triggers stay closed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import MemoryScope

from services.guardian.child_memory import (
    KIND_CHILD_ARCHIVE,
    KIND_DIALOGUE_SUMMARY,
    ArchiveFact,
    ChildMemoryKind,
    DialogueSummary,
    archive_payload,
    parse_child_kind,
    summary_expires_at,
    summary_payload,
)
from services.memory_scope.subject_erasure import MAINTENANCE_ROLE, _require_subject


async def _scope(connection: asyncpg.Connection, **values: str | None) -> None:
    for key, value in values.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            raise ValueError(f"app.memory.{key} must not be empty")
        await connection.execute(
            "SELECT set_config('app.memory.' || $1, $2, true)", key, text
        )


@dataclass(frozen=True, slots=True)
class ChildLayerRecord:
    record_id: str
    subject_id: str
    kind: ChildMemoryKind
    text: str
    field: str | None
    session_id: str | None
    created_at: datetime
    retention_expires_at: datetime | None
    payload: dict[str, object]


def _parse_payload(raw: object) -> dict[str, object]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return dict(loaded)
    return {}


def _row_to_record(row: asyncpg.Record) -> ChildLayerRecord | None:
    payload = _parse_payload(row["payload"])
    kind = parse_child_kind(payload)
    if kind is None:
        return None
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    field = payload.get("field")
    session_id = payload.get("session_id")
    return ChildLayerRecord(
        record_id=str(row["record_id"]),
        subject_id=str(row["subject_id"]),
        kind=kind,
        text=text.strip(),
        field=str(field) if isinstance(field, str) else None,
        session_id=str(session_id) if isinstance(session_id, str) else None,
        created_at=row["created_at"],
        retention_expires_at=row["retention_expires_at"],
        payload=payload,
    )


class ChildLayerStore:
    """Read/write child layers; physical deletes use maintenance DSN."""

    def __init__(
        self,
        *,
        api_dsn: str,
        maintenance_dsn: str,
    ) -> None:
        self._api_dsn = api_dsn
        self._maintenance_dsn = maintenance_dsn
        self._api_pool: asyncpg.Pool | None = None
        self._maintenance_pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._api_pool is None:
            self._api_pool = await asyncpg.create_pool(
                self._api_dsn, min_size=1, max_size=4, command_timeout=30
            )
        if self._maintenance_pool is None:
            pool = await asyncpg.create_pool(
                self._maintenance_dsn, min_size=1, max_size=2, command_timeout=30
            )
            try:
                async with pool.acquire() as connection:
                    row = await connection.fetchrow(
                        "SELECT current_user AS usr, rolsuper, rolbypassrls"
                        " FROM pg_roles WHERE rolname = current_user"
                    )
                if (
                    row is None
                    or row["usr"] != MAINTENANCE_ROLE
                    or row["rolsuper"]
                    or row["rolbypassrls"]
                ):
                    raise RuntimeError(
                        f"child layer erasure requires the non-superuser, "
                        f"NOBYPASSRLS {MAINTENANCE_ROLE} login"
                    )
            except BaseException:
                await pool.close()
                raise
            self._maintenance_pool = pool

    async def close(self) -> None:
        if self._api_pool is not None:
            await self._api_pool.close()
            self._api_pool = None
        if self._maintenance_pool is not None:
            await self._maintenance_pool.close()
            self._maintenance_pool = None

    async def _api(self) -> asyncpg.Pool:
        if self._api_pool is None:
            await self.initialize()
        assert self._api_pool is not None
        return self._api_pool

    async def _maintenance(self) -> asyncpg.Pool:
        if self._maintenance_pool is None:
            await self.initialize()
        assert self._maintenance_pool is not None
        return self._maintenance_pool

    async def write_summary(
        self,
        summary: DialogueSummary,
        *,
        resource_owner_id: str,
        actor_id: str,
        consent_snapshot_id: str,
        policy_receipt_id: str = "child-dialogue-summary",
        created_at: datetime | None = None,
    ) -> ChildLayerRecord:
        stamped = created_at or datetime.now(UTC)
        expires = summary_expires_at(stamped)
        record_id = f"dlgsum:{uuid.uuid4()}"
        receipt = f"{policy_receipt_id}:{record_id}"
        payload = summary_payload(summary, created_at=stamped)
        async with (await self._api()).acquire() as connection:
            async with connection.transaction():
                await _scope(
                    connection,
                    subject_id=summary.subject_id,
                    actor_subject_id=actor_id,
                )
                await connection.execute(
                    "INSERT INTO memory_records ("
                    " record_id, scope, subject_id, resource_owner_id,"
                    " family_space_id, co_subject_ids, source_evidence_ids,"
                    " policy_receipt_id, promotion_receipt_id,"
                    " promotion_fence_context_hash, approval_evidence_refs,"
                    " consent_snapshot_id, memory_type, confidence, retention,"
                    " retention_expires_at, payload, created_by_actor_id,"
                    " created_at, shared_proposal_id)"
                    " VALUES ("
                    " $1, $2, $3, $4, NULL, '[]'::jsonb, $5::jsonb,"
                    " $6, '', '', '[]'::jsonb, $7, 'dialogue_summary', 1.0,"
                    " 'ttl', $8, $9::jsonb, $10, $11, NULL)",
                    record_id,
                    MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY.value,
                    summary.subject_id,
                    resource_owner_id,
                    json.dumps([summary.source_event_id]),
                    receipt,
                    consent_snapshot_id,
                    expires,
                    json.dumps(payload),
                    actor_id,
                    stamped,
                )
                await connection.execute(
                    "INSERT INTO memory_status_events ("
                    " event_id, record_id, status, reason_code, created_at)"
                    " VALUES ($1, $2, 'confirmed', 'child_dialogue_summary', $3)",
                    f"status:{record_id}",
                    record_id,
                    stamped,
                )
        listed = await self.get(
            subject_id=summary.subject_id,
            record_id=record_id,
            viewer_subject_id=resource_owner_id,
        )
        assert listed is not None
        return listed

    async def find_archive_fact(
        self,
        *,
        subject_id: str,
        field: str,
        text: str,
        viewer_subject_id: str,
    ) -> ChildLayerRecord | None:
        """Return the existing whitelist fact with the same field and text, if any."""

        items = await self.list(
            subject_id=subject_id,
            kind=KIND_CHILD_ARCHIVE,
            viewer_subject_id=viewer_subject_id,
            limit=50,
        )
        needle = text.strip()
        for item in items:
            if item.field == field and item.text == needle:
                return item
        return None

    async def write_archive_fact(
        self,
        fact: ArchiveFact,
        *,
        resource_owner_id: str,
        actor_id: str,
        consent_snapshot_id: str,
        policy_receipt_id: str = "child-archive-fact",
        created_at: datetime | None = None,
    ) -> ChildLayerRecord:
        stamped = created_at or datetime.now(UTC)
        record_id = f"charch:{uuid.uuid4()}"
        receipt = f"{policy_receipt_id}:{record_id}"
        payload = archive_payload(fact, created_at=stamped, last_mentioned_at=stamped)
        async with (await self._api()).acquire() as connection:
            async with connection.transaction():
                await _scope(
                    connection,
                    subject_id=fact.subject_id,
                    actor_subject_id=actor_id,
                )
                await connection.execute(
                    "INSERT INTO memory_records ("
                    " record_id, scope, subject_id, resource_owner_id,"
                    " family_space_id, co_subject_ids, source_evidence_ids,"
                    " policy_receipt_id, promotion_receipt_id,"
                    " promotion_fence_context_hash, approval_evidence_refs,"
                    " consent_snapshot_id, memory_type, confidence, retention,"
                    " retention_expires_at, payload, created_by_actor_id,"
                    " created_at, shared_proposal_id)"
                    " VALUES ("
                    " $1, $2, $3, $4, NULL, '[]'::jsonb, '[]'::jsonb,"
                    " $5, '', '', '[]'::jsonb, $6, 'child_archive', 1.0,"
                    " 'indefinite', NULL, $7::jsonb, $8, $9, NULL)",
                    record_id,
                    MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE.value,
                    fact.subject_id,
                    resource_owner_id,
                    receipt,
                    consent_snapshot_id,
                    json.dumps(payload),
                    actor_id,
                    stamped,
                )
                await connection.execute(
                    "INSERT INTO memory_status_events ("
                    " event_id, record_id, status, reason_code, created_at)"
                    " VALUES ($1, $2, 'confirmed', 'child_archive_fact', $3)",
                    f"status:{record_id}",
                    record_id,
                    stamped,
                )
        listed = await self.get(
            subject_id=fact.subject_id,
            record_id=record_id,
            viewer_subject_id=resource_owner_id,
        )
        assert listed is not None
        return listed

    async def upsert_archive_fact(
        self,
        fact: ArchiveFact,
        *,
        resource_owner_id: str,
        actor_id: str,
        consent_snapshot_id: str,
        created_at: datetime | None = None,
    ) -> ChildLayerRecord:
        """Write a whitelist fact, or replace a duplicate so last-mentioned moves forward.

        ``memory_records`` is append-only, so a re-mention erases the prior row and
        inserts a fresh one whose ``created_at`` / ``last_mentioned_at`` is now.
        """

        stamped = created_at or datetime.now(UTC)
        existing = await self.find_archive_fact(
            subject_id=fact.subject_id,
            field=fact.field,
            text=fact.text,
            viewer_subject_id=resource_owner_id,
        )
        if existing is not None:
            await self.delete_record(subject_id=fact.subject_id, record_id=existing.record_id)
        return await self.write_archive_fact(
            fact,
            resource_owner_id=resource_owner_id,
            actor_id=actor_id,
            consent_snapshot_id=consent_snapshot_id,
            created_at=stamped,
        )

    async def list(
        self,
        *,
        subject_id: str,
        kind: ChildMemoryKind,
        viewer_subject_id: str,
        limit: int = 50,
    ) -> list[ChildLayerRecord]:
        _require_subject(subject_id)
        _require_subject(viewer_subject_id)
        async with (await self._api()).acquire() as connection:
            async with connection.transaction():
                await _scope(connection, actor_subject_id=viewer_subject_id)
                rows = await connection.fetch(
                    "SELECT record_id, subject_id, payload, created_at,"
                    " retention_expires_at FROM memory_records"
                    " WHERE subject_id = $1 AND payload ->> 'kind' = $2"
                    " ORDER BY created_at DESC LIMIT $3",
                    subject_id,
                    kind,
                    limit,
                )
        records: list[ChildLayerRecord] = []
        for row in rows:
            parsed = _row_to_record(row)
            if parsed is not None:
                records.append(parsed)
        return records

    async def get(
        self,
        *,
        subject_id: str,
        record_id: str,
        viewer_subject_id: str,
    ) -> ChildLayerRecord | None:
        _require_subject(subject_id)
        _require_subject(viewer_subject_id)
        async with (await self._api()).acquire() as connection:
            async with connection.transaction():
                await _scope(connection, actor_subject_id=viewer_subject_id)
                row = await connection.fetchrow(
                    "SELECT record_id, subject_id, payload, created_at,"
                    " retention_expires_at FROM memory_records"
                    " WHERE subject_id = $1 AND record_id = $2",
                    subject_id,
                    record_id,
                )
        if row is None:
            return None
        return _row_to_record(row)

    async def delete_record(
        self, *, subject_id: str, record_id: str
    ) -> Mapping[str, Any]:
        _require_subject(subject_id)
        async with (await self._maintenance()).acquire() as connection:
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT memory_record_erase($1, $2)", subject_id, record_id
                )
        return _jsonb(raw)

    async def erase_kind(
        self,
        *,
        subject_id: str,
        kind: ChildMemoryKind,
        only_expired: bool = False,
    ) -> Mapping[str, Any]:
        _require_subject(subject_id)
        async with (await self._maintenance()).acquire() as connection:
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT memory_kind_erase($1, $2, $3)",
                    subject_id,
                    kind,
                    only_expired,
                )
        return _jsonb(raw)

    async def remaining_kind(
        self,
        *,
        subject_id: str,
        kind: ChildMemoryKind,
        only_expired: bool = False,
    ) -> Mapping[str, Any]:
        _require_subject(subject_id)
        async with (await self._maintenance()).acquire() as connection:
            raw = await connection.fetchval(
                "SELECT memory_kind_remaining($1, $2, $3)",
                subject_id,
                kind,
                only_expired,
            )
        return _jsonb(raw)

    async def purge_both_layers(self, *, subject_id: str) -> dict[str, Mapping[str, Any]]:
        """Unbind / PIPL art. 47: delete archive and all summaries, including fresh ones."""

        summary = await self.erase_kind(
            subject_id=subject_id, kind=KIND_DIALOGUE_SUMMARY, only_expired=False
        )
        archive = await self.erase_kind(
            subject_id=subject_id, kind=KIND_CHILD_ARCHIVE, only_expired=False
        )
        summary_left = await self.remaining_kind(
            subject_id=subject_id, kind=KIND_DIALOGUE_SUMMARY, only_expired=False
        )
        archive_left = await self.remaining_kind(
            subject_id=subject_id, kind=KIND_CHILD_ARCHIVE, only_expired=False
        )
        if int(summary_left.get("memory_records", 0)) or int(
            archive_left.get("memory_records", 0)
        ):
            raise RuntimeError(
                f"child layer purge incomplete subject={subject_id}"
                f" summary_left={summary_left} archive_left={archive_left}"
            )
        return {"dialogue_summary": summary, "child_archive": archive}

    async def expire_due_summaries(self, *, subject_id: str) -> Mapping[str, Any]:
        erased = await self.erase_kind(
            subject_id=subject_id, kind=KIND_DIALOGUE_SUMMARY, only_expired=True
        )
        remaining = await self.remaining_kind(
            subject_id=subject_id, kind=KIND_DIALOGUE_SUMMARY, only_expired=True
        )
        if int(remaining.get("memory_records", 0)):
            raise RuntimeError(
                f"expired dialogue_summary remaining subject={subject_id}"
                f" remaining={remaining}"
            )
        return {"erased": erased, "remaining": remaining}


def _jsonb(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return dict(loaded)
    raise TypeError("expected jsonb object from memory kind erase")


__all__ = [
    "ChildLayerRecord",
    "ChildLayerStore",
]
