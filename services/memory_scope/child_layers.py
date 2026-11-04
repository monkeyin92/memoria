"""Parent-visible child memory layers on ``memory_records``.

``dialogue_summary`` (30-day physical TTL) and ``child_archive`` (idle 180-day
TTL, separate consent).  Writes go through the memory API role; deletes go
through the maintenance kind-erase ports so append-only triggers stay closed.
"""

from __future__ import annotations

import builtins
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import MemoryScope

from services.common.memory_decision_contract import MemoryDecision
from services.guardian.child_archive_review import (
    ChildArchiveFactView,
    ChildArchiveReviewCommand,
    ChildArchiveReviewError,
    ChildArchiveReviewReceipt,
    build_fact_view,
    validate_correct_patch,
)
from services.guardian.child_memory import (
    ARCHIVE_FIELDS,
    KIND_CHILD_ARCHIVE,
    KIND_DIALOGUE_SUMMARY,
    ArchiveFact,
    ArchiveField,
    ChildMemoryKind,
    DialogueSummary,
    archive_payload,
    parse_child_kind,
    summary_expires_at,
    summary_payload,
)
from services.guardian.preference_extract import DEFAULT_AUTO_CONFIDENCE
from services.memory_scope.subject_erasure import MAINTENANCE_ROLE, _require_subject

ChildLayerEraseReason = Literal[
    "unbind_purge", "retention_expiry", "child_layer_erase"
]


def archive_birth_stamp(birth_decision: MemoryDecision | None) -> tuple[str, float, str]:
    """Status, confidence, and reason for one child-archive insert.

    Parent writes omit the decision and stay confirmed at 1.0. An auto-extract
    birth is a candidate below 1.0.
    """

    if birth_decision is None:
        return "confirmed", 1.0, "child_archive_fact"
    if birth_decision.fact.provenance != "auto_extract":
        raise ValueError("birth_decision provenance must be auto_extract")
    confidence = birth_decision.fact.confidence
    if confidence is None:
        confidence = DEFAULT_AUTO_CONFIDENCE
    return "candidate", confidence, "child_archive_auto_extract"


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
        birth_decision: MemoryDecision | None = None,
    ) -> ChildLayerRecord:
        stamped = created_at or datetime.now(UTC)
        status, confidence, reason_code = archive_birth_stamp(birth_decision)
        record_id = f"charch:{uuid.uuid4()}"
        if birth_decision is not None and birth_decision.record_id:
            stripped = birth_decision.record_id.strip()
            if stripped:
                record_id = stripped
        receipt = f"{policy_receipt_id}:{record_id}"
        if birth_decision is None:
            payload = archive_payload(
                fact,
                created_at=stamped,
                last_mentioned_at=stamped,
                provenance="parent_entry",
                fact_version=1,
            )
        else:
            payload = archive_payload(
                fact,
                created_at=stamped,
                last_mentioned_at=stamped,
                provenance=birth_decision.fact.provenance,
                rule_version=birth_decision.fact.rule_version,
                source_session_id=birth_decision.fact.source_session_id,
                polarity=birth_decision.fact.polarity,
                fact_version=birth_decision.fact_version or 1,
            )
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
                    " $5, '', '', '[]'::jsonb, $6, 'child_archive', $7,"
                    " 'indefinite', NULL, $8::jsonb, $9, $10, NULL)",
                    record_id,
                    MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE.value,
                    fact.subject_id,
                    resource_owner_id,
                    receipt,
                    consent_snapshot_id,
                    confidence,
                    json.dumps(payload),
                    actor_id,
                    stamped,
                )
                await connection.execute(
                    "INSERT INTO memory_status_events ("
                    " event_id, record_id, status, reason_code, created_at)"
                    " VALUES ($1, $2, $3, $4, $5)",
                    f"status:{record_id}",
                    record_id,
                    status,
                    reason_code,
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
        birth_decision: MemoryDecision | None = None,
    ) -> ChildLayerRecord:
        """Write a whitelist fact, or refresh ``last_mentioned_at`` in place.

        Re-mentions keep ``created_at`` and the same ``record_id``. Only
        ``payload.last_mentioned_at`` moves forward (no delete/insert).
        """

        stamped = created_at or datetime.now(UTC)
        existing = await self.find_archive_fact(
            subject_id=fact.subject_id,
            field=fact.field,
            text=fact.text,
            viewer_subject_id=resource_owner_id,
        )
        if existing is not None:
            async with (await self._api()).acquire() as connection:
                async with connection.transaction():
                    await connection.fetchval(
                        "SELECT memory_child_archive_touch_mentioned($1, $2, $3)",
                        fact.subject_id,
                        existing.record_id,
                        stamped,
                    )
            listed = await self.get(
                subject_id=fact.subject_id,
                record_id=existing.record_id,
                viewer_subject_id=resource_owner_id,
            )
            assert listed is not None
            return listed
        return await self.write_archive_fact(
            fact,
            resource_owner_id=resource_owner_id,
            actor_id=actor_id,
            consent_snapshot_id=consent_snapshot_id,
            created_at=stamped,
            birth_decision=birth_decision,
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

    async def list_archive_views(
        self,
        *,
        subject_id: str,
        viewer_subject_id: str,
        limit: int = 50,
        include_withdrawn: bool = False,
    ) -> builtins.list[ChildArchiveFactView]:
        """Parent-facing archive cards with status, provenance, and version."""

        _require_subject(subject_id)
        _require_subject(viewer_subject_id)
        async with (await self._api()).acquire() as connection:
            async with connection.transaction():
                await _scope(connection, actor_subject_id=viewer_subject_id)
                rows = await connection.fetch(
                    "SELECT r.record_id, r.subject_id, r.payload, r.created_at,"
                    " r.retention_expires_at, r.confidence,"
                    " s.status AS latest_status, s.created_at AS handled_at,"
                    " s.reason_code AS reason_code"
                    " FROM memory_records r"
                    " LEFT JOIN LATERAL ("
                    "   SELECT status, created_at, reason_code"
                    "   FROM memory_status_events"
                    "   WHERE record_id = r.record_id"
                    "   ORDER BY created_at DESC"
                    "   LIMIT 1"
                    " ) s ON true"
                    " WHERE r.subject_id = $1"
                    "   AND r.payload ->> 'kind' = $2"
                    "   AND ($3::boolean OR COALESCE(s.status, 'candidate') <> 'revoked')"
                    " ORDER BY r.created_at DESC"
                    " LIMIT $4",
                    subject_id,
                    KIND_CHILD_ARCHIVE,
                    include_withdrawn,
                    limit,
                )
        views: builtins.list[ChildArchiveFactView] = []
        for row in rows:
            parsed = _row_to_record(row)
            if parsed is None or parsed.field is None:
                continue
            status = row["latest_status"] or "candidate"
            views.append(
                build_fact_view(
                    record_id=parsed.record_id,
                    field=parsed.field,
                    text=parsed.text,
                    created_at=parsed.created_at,
                    retention_expires_at=parsed.retention_expires_at,
                    payload=parsed.payload,
                    status=str(status),
                    reason_code=(
                        str(row["reason_code"]) if row["reason_code"] is not None else None
                    ),
                    handled_at=row["handled_at"],
                    confidence=(
                        float(row["confidence"]) if row["confidence"] is not None else None
                    ),
                )
            )
        return views

    async def get_archive_view(
        self,
        *,
        subject_id: str,
        record_id: str,
        viewer_subject_id: str,
    ) -> ChildArchiveFactView | None:
        _require_subject(subject_id)
        _require_subject(viewer_subject_id)
        async with (await self._api()).acquire() as connection:
            async with connection.transaction():
                await _scope(connection, actor_subject_id=viewer_subject_id)
                row = await connection.fetchrow(
                    "SELECT r.record_id, r.subject_id, r.payload, r.created_at,"
                    " r.retention_expires_at, r.confidence,"
                    " s.status AS latest_status, s.created_at AS handled_at,"
                    " s.reason_code AS reason_code"
                    " FROM memory_records r"
                    " LEFT JOIN LATERAL ("
                    "   SELECT status, created_at, reason_code"
                    "   FROM memory_status_events"
                    "   WHERE record_id = r.record_id"
                    "   ORDER BY created_at DESC"
                    "   LIMIT 1"
                    " ) s ON true"
                    " WHERE r.subject_id = $1 AND r.record_id = $2"
                    "   AND r.payload ->> 'kind' = $3",
                    subject_id,
                    record_id,
                    KIND_CHILD_ARCHIVE,
                )
        if row is None:
            return None
        parsed = _row_to_record(row)
        if parsed is None or parsed.field is None:
            return None
        status = row["latest_status"] or "candidate"
        return build_fact_view(
            record_id=parsed.record_id,
            field=parsed.field,
            text=parsed.text,
            created_at=parsed.created_at,
            retention_expires_at=parsed.retention_expires_at,
            payload=parsed.payload,
            status=str(status),
            reason_code=str(row["reason_code"]) if row["reason_code"] is not None else None,
            handled_at=row["handled_at"],
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        )

    async def review_archive_fact(
        self,
        *,
        subject_id: str,
        resource_owner_id: str,
        consent_snapshot_id: str,
        command: ChildArchiveReviewCommand,
    ) -> ChildArchiveReviewReceipt:
        """Confirm, correct (supersede), or withdraw one archive fact."""

        _require_subject(subject_id)
        _require_subject(resource_owner_id)
        actor = command.actor_id.strip()
        if not actor:
            raise ChildArchiveReviewError("actor_required")
        current = await self.get_archive_view(
            subject_id=subject_id,
            record_id=command.record_id,
            viewer_subject_id=resource_owner_id,
        )
        if current is None:
            raise ChildArchiveReviewError("child_archive_not_found")
        if current.status == "revoked" or current.display_lifecycle == "withdrawn":
            raise ChildArchiveReviewError("child_archive_already_withdrawn")
        stamped = datetime.now(UTC)
        if command.action == "confirm":
            return await self._confirm_archive(
                subject_id=subject_id,
                resource_owner_id=resource_owner_id,
                current=current,
                actor_id=actor,
                stamped=stamped,
            )
        if command.action == "withdraw":
            return await self._withdraw_archive(
                subject_id=subject_id,
                resource_owner_id=resource_owner_id,
                current=current,
                actor_id=actor,
                stamped=stamped,
            )
        if command.action == "correct":
            return await self._correct_archive(
                subject_id=subject_id,
                resource_owner_id=resource_owner_id,
                consent_snapshot_id=consent_snapshot_id,
                current=current,
                command=command,
                actor_id=actor,
                stamped=stamped,
            )
        raise ChildArchiveReviewError("unsupported_review_action")

    async def _confirm_archive(
        self,
        *,
        subject_id: str,
        resource_owner_id: str,
        current: ChildArchiveFactView,
        actor_id: str,
        stamped: datetime,
    ) -> ChildArchiveReviewReceipt:
        if current.status == "confirmed" and current.display_lifecycle == "parent_confirmed":
            audit_id = await self._append_audit(
                action="child_archive.parent_confirm",
                actor_id=actor_id,
                subject_id=subject_id,
                record_id=current.record_id,
                stamped=stamped,
                payload={
                    "result": "idempotent",
                    "old_status": current.status,
                    "new_status": "confirmed",
                    "old_version": current.fact_version,
                    "new_version": current.fact_version,
                    "auth": "binding_owner",
                },
            )
            return ChildArchiveReviewReceipt(
                action="confirm",
                record_id=current.record_id,
                status="confirmed",
                display_lifecycle="parent_confirmed",
                display_label="家长已确认",
                reviewed_at=stamped,
                audit_event_id=audit_id,
                old_version=current.fact_version,
                new_version=current.fact_version,
            )
        event_id = f"status:confirm:{current.record_id}:{uuid.uuid4()}"
        async with (await self._api()).acquire() as connection:
            async with connection.transaction():
                await _scope(
                    connection,
                    subject_id=subject_id,
                    actor_subject_id=actor_id,
                )
                await connection.execute(
                    "INSERT INTO memory_status_events ("
                    " event_id, record_id, status, reason_code, created_at)"
                    " VALUES ($1, $2, 'confirmed', 'parent_confirm', $3)",
                    event_id,
                    current.record_id,
                    stamped,
                )
        audit_id = await self._append_audit(
            action="child_archive.parent_confirm",
            actor_id=actor_id,
            subject_id=subject_id,
            record_id=current.record_id,
            stamped=stamped,
            payload={
                "result": "ok",
                "old_status": current.status,
                "new_status": "confirmed",
                "old_version": current.fact_version,
                "new_version": current.fact_version,
                "auth": "binding_owner",
                "resource_owner_id": resource_owner_id,
            },
        )
        return ChildArchiveReviewReceipt(
            action="confirm",
            record_id=current.record_id,
            status="confirmed",
            display_lifecycle="parent_confirmed",
            display_label="家长已确认",
            reviewed_at=stamped,
            audit_event_id=audit_id,
            old_version=current.fact_version,
            new_version=current.fact_version,
        )

    async def _withdraw_archive(
        self,
        *,
        subject_id: str,
        resource_owner_id: str,
        current: ChildArchiveFactView,
        actor_id: str,
        stamped: datetime,
    ) -> ChildArchiveReviewReceipt:
        event_id = f"status:withdraw:{current.record_id}:{uuid.uuid4()}"
        async with (await self._api()).acquire() as connection:
            async with connection.transaction():
                await _scope(
                    connection,
                    subject_id=subject_id,
                    actor_subject_id=actor_id,
                )
                await connection.execute(
                    "INSERT INTO memory_status_events ("
                    " event_id, record_id, status, reason_code, created_at)"
                    " VALUES ($1, $2, 'revoked', 'parent_withdraw', $3)",
                    event_id,
                    current.record_id,
                    stamped,
                )
        audit_id = await self._append_audit(
            action="child_archive.parent_withdraw",
            actor_id=actor_id,
            subject_id=subject_id,
            record_id=current.record_id,
            stamped=stamped,
            payload={
                "result": "ok",
                "old_status": current.status,
                "new_status": "revoked",
                "old_version": current.fact_version,
                "new_version": current.fact_version,
                "auth": "binding_owner",
                "resource_owner_id": resource_owner_id,
                "a05_handoff": "new_session_and_cache_proof",
            },
        )
        return ChildArchiveReviewReceipt(
            action="withdraw",
            record_id=current.record_id,
            status="revoked",
            display_lifecycle="withdrawn",
            display_label="已撤回",
            reviewed_at=stamped,
            audit_event_id=audit_id,
            old_version=current.fact_version,
            new_version=current.fact_version,
        )

    async def _correct_archive(
        self,
        *,
        subject_id: str,
        resource_owner_id: str,
        consent_snapshot_id: str,
        current: ChildArchiveFactView,
        command: ChildArchiveReviewCommand,
        actor_id: str,
        stamped: datetime,
    ) -> ChildArchiveReviewReceipt:
        next_text, next_field, next_polarity = validate_correct_patch(
            text=command.text,
            field=command.field,
            polarity=command.polarity,
        )
        candidate_field = next_field if next_field is not None else current.field
        if candidate_field not in ARCHIVE_FIELDS:
            raise ChildArchiveReviewError("invalid_archive_field")
        field = cast(ArchiveField, candidate_field)
        text = next_text or current.text
        polarity = next_polarity or current.polarity or "unknown"
        if field == current.field and text == current.text and polarity == (
            current.polarity or "unknown"
        ):
            raise ChildArchiveReviewError("correct_noop")
        new_version = current.fact_version + 1
        try:
            fact = ArchiveFact(subject_id=subject_id, field=field, text=text)
        except ValueError as exc:
            raise ChildArchiveReviewError("invalid_child_archive_fact", str(exc)) from exc
        new_record_id = f"charch:{uuid.uuid4()}"
        reason = (
            "parent_correct_history"
            if command.history_change
            else "parent_correct"
        )
        payload = archive_payload(
            fact,
            created_at=stamped,
            last_mentioned_at=stamped,
            provenance="parent_confirm",
            rule_version=current.rule_version,
            source_session_id=current.source_session_id,
            polarity=polarity,
            fact_version=new_version,
            supersedes_record_id=current.record_id,
        )
        receipt = f"child-archive-correct:{new_record_id}"
        revoke_event = f"status:supersede:{current.record_id}:{uuid.uuid4()}"
        confirm_event = f"status:correct:{new_record_id}"
        async with (await self._api()).acquire() as connection:
            async with connection.transaction():
                await _scope(
                    connection,
                    subject_id=subject_id,
                    actor_subject_id=actor_id,
                )
                await connection.execute(
                    "INSERT INTO memory_status_events ("
                    " event_id, record_id, status, reason_code, created_at)"
                    " VALUES ($1, $2, 'revoked', $3, $4)",
                    revoke_event,
                    current.record_id,
                    f"superseded_by:{new_record_id}",
                    stamped,
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
                    new_record_id,
                    MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE.value,
                    subject_id,
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
                    " VALUES ($1, $2, 'confirmed', $3, $4)",
                    confirm_event,
                    new_record_id,
                    reason,
                    stamped,
                )
        audit_id = await self._append_audit(
            action="child_archive.parent_correct",
            actor_id=actor_id,
            subject_id=subject_id,
            record_id=new_record_id,
            stamped=stamped,
            payload={
                "result": "ok",
                "old_status": current.status,
                "new_status": "confirmed",
                "old_record_id": current.record_id,
                "new_record_id": new_record_id,
                "old_version": current.fact_version,
                "new_version": new_version,
                "old_text": current.text,
                "new_text": text,
                "old_polarity": current.polarity,
                "new_polarity": polarity,
                "history_change": command.history_change,
                "auth": "binding_owner",
                "resource_owner_id": resource_owner_id,
            },
        )
        return ChildArchiveReviewReceipt(
            action="correct",
            record_id=new_record_id,
            status="confirmed",
            display_lifecycle="parent_confirmed",
            display_label="家长已确认",
            reviewed_at=stamped,
            audit_event_id=audit_id,
            old_record_id=current.record_id,
            new_record_id=new_record_id,
            old_version=current.fact_version,
            new_version=new_version,
        )

    async def _append_audit(
        self,
        *,
        action: str,
        actor_id: str,
        subject_id: str,
        record_id: str,
        stamped: datetime,
        payload: Mapping[str, Any],
    ) -> str:
        event_id = f"audit:{action}:{record_id}:{uuid.uuid4()}"
        async with (await self._api()).acquire() as connection:
            async with connection.transaction():
                await _scope(
                    connection,
                    subject_id=subject_id,
                    actor_subject_id=actor_id,
                )
                await connection.execute(
                    "INSERT INTO memory_audit_events ("
                    " event_id, action, actor_subject_id, subject_id, record_id,"
                    " proposal_id, payload, created_at)"
                    " VALUES ($1, $2, $3, $4, $5, NULL, $6::jsonb, $7)",
                    event_id,
                    action,
                    actor_id,
                    subject_id,
                    record_id,
                    json.dumps(dict(payload)),
                    stamped,
                )
        return event_id

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
        reason: ChildLayerEraseReason = "child_layer_erase",
    ) -> Mapping[str, Any]:
        _require_subject(subject_id)
        async with (await self._maintenance()).acquire() as connection:
            async with connection.transaction():
                # Transaction-local audit label; the SQL function falls back
                # to only_expired when a caller does not set it.
                await connection.execute(
                    "SELECT set_config('app.memory.child_layer_erase_reason', $1, true)",
                    reason,
                )
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

    async def record_retention_basis(self, *, subject_id: str) -> Mapping[str, Any]:
        """Append the content-free basis for keeping the child layers on unbind."""

        _require_subject(subject_id)
        async with (await self._maintenance()).acquire() as connection:
            raw = await connection.fetchval(
                "SELECT memory_child_layer_retain($1)", subject_id
            )
        return _jsonb(raw)

    async def purge_both_layers(self, *, subject_id: str) -> dict[str, Mapping[str, Any]]:
        """Guardian chose delete: remove the archive and every summary, including fresh ones."""

        summary = await self.erase_kind(
            subject_id=subject_id,
            kind=KIND_DIALOGUE_SUMMARY,
            only_expired=False,
            reason="unbind_purge",
        )
        archive = await self.erase_kind(
            subject_id=subject_id,
            kind=KIND_CHILD_ARCHIVE,
            only_expired=False,
            reason="unbind_purge",
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
            subject_id=subject_id,
            kind=KIND_DIALOGUE_SUMMARY,
            only_expired=True,
            reason="retention_expiry",
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

    async def purge_archive(self, *, subject_id: str) -> Mapping[str, Any]:
        erased = await self.erase_kind(
            subject_id=subject_id,
            kind=KIND_CHILD_ARCHIVE,
            only_expired=False,
            reason="retention_expiry",
        )
        remaining = await self.remaining_kind(
            subject_id=subject_id, kind=KIND_CHILD_ARCHIVE, only_expired=False
        )
        if int(remaining.get("memory_records", 0)):
            raise RuntimeError(
                f"child_archive remaining after purge subject={subject_id}"
                f" remaining={remaining}"
            )
        return {"erased": erased, "remaining": remaining}

    async def kind_subjects(
        self, kind: ChildMemoryKind, *, only_expired: bool = False
    ) -> tuple[str, ...]:
        """Subjects holding rows of ``kind``; only those with expired rows if asked."""

        function = "memory_kind_due_subjects" if only_expired else "memory_kind_subjects"
        async with (await self._maintenance()).acquire() as connection:
            subjects = await connection.fetchval(f"SELECT {function}($1)", kind)
        return tuple(str(item) for item in subjects or ())


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
    "archive_birth_stamp",
]
