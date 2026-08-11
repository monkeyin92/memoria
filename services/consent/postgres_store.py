"""PostgreSQL consent store (production authority, asyncpg).

All tables use FORCE ROW LEVEL SECURITY (see ``postgres_schema.sql``).  RLS
separates service roles; per-request actor/subject authority stays in
ConsentAuthority and the deployment-provisioned authorization mapping.  The
store never writes caller-controlled GUCs.  JSONB payloads are strictly
decoded: unknown keys raise ``ValueError`` and every evidence/snapshot hash is
verified on read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

import asyncpg

from services.consent.events import (
    AuditEntry,
    ConsentOutboxEvent,
    normalize_payload,
)
from services.consent.evidence import ConsentEvidence, ConsentOffer, ConsentSnapshot
from services.consent.store import (
    AsyncConsentUnitOfWork,
    ConsentConflictError,
    ConsentConsistencyError,
    IdempotencyRecord,
)


class PostgresConsentStore:
    """asyncpg-backed consent store implementing :class:`AsyncConsentStorePort`."""

    def __init__(
        self,
        dsn: str,
        *,
        worker_id: str = "consent-outbox-worker",
        session_role: Literal[
            "memoria_consent",
            "memoria_policy_projector",
            "memoria_consent_outbox",
            "memoria_consent_audit",
            "memoria_consent_maintenance",
        ]
        | None = None,
    ) -> None:
        normalized_worker_id = worker_id.strip()
        if not normalized_worker_id or len(normalized_worker_id) > 128:
            raise ValueError("worker_id must be a bounded non-empty string")
        self._dsn = dsn
        self._worker_id = normalized_worker_id
        self._session_role = session_role
        self._pool: asyncpg.Pool | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def initialize(self) -> None:
        async def initialize_connection(conn: asyncpg.Connection) -> None:
            if self._session_role is not None:
                await conn.execute(f"SET SESSION AUTHORIZATION {self._session_role}")

        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=10,
            init=initialize_connection,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def transaction(self) -> AsyncConsentUnitOfWork:
        pool = self._pool
        if pool is None:
            raise RuntimeError("PostgresConsentStore not initialized")
        conn = await pool.acquire()
        try:
            transaction = conn.transaction()
            await transaction.start()
            return _PostgresUow(conn, transaction, pool)
        except BaseException:
            await pool.release(conn)
            raise

    async def outbox_pending(self, limit: int = 100) -> tuple[ConsentOutboxEvent, ...]:
        """Atomically claim pending events through the worker-only DB function."""
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be within 1..1000")
        pool = self._pool
        if pool is None:
            raise RuntimeError("PostgresConsentStore not initialized")
        conn = await pool.acquire()
        try:
            rows = await conn.fetch(
                "SELECT * FROM consent_claim_outbox($1, $2)",
                self._worker_id,
                limit,
            )
            return tuple(_row_to_event(row) for row in rows)
        finally:
            await pool.release(conn)

    async def mark_outbox_processed(self, event_id: str, status: str = "delivered") -> None:
        if status not in {"delivered", "dead_lettered"}:
            raise ValueError("status must be delivered or dead_lettered")
        pool = self._pool
        if pool is None:
            raise RuntimeError("PostgresConsentStore not initialized")
        conn = await pool.acquire()
        try:
            await conn.fetchrow(
                "SELECT * FROM consent_complete_outbox($1, $2)",
                event_id,
                status,
            )
        finally:
            await pool.release(conn)


class _PostgresUow:
    def __init__(
        self,
        conn: asyncpg.Connection,
        transaction: asyncpg.transaction.Transaction,
        pool: asyncpg.Pool,
    ) -> None:
        self._conn = conn
        self._transaction = transaction
        self._pool = pool
        self._closed = False
        self._operation_actor_id: str | None = None
        self._operation_binding_id: str | None = None
        self._locked_offer_heads: dict[str, tuple[int, str | None]] = {}
        self._locked_consent_heads: dict[
            tuple[str, str, str, int, str, str], tuple[int, str | None, str]
        ] = {}
        self._locked_snapshot_heads: dict[
            tuple[str, str, int], tuple[int, str | None, str]
        ] = {}
        self._pending_consent_heads: dict[
            tuple[str, str, str, int, str, str], list[ConsentEvidence]
        ] = {}

    async def latest_offer(self, offer_id: str) -> ConsentOffer | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM consent_offer WHERE offer_id = $1 "
            "ORDER BY version DESC LIMIT 1",
            offer_id,
        )
        return _row_to_offer(row)

    async def offer_by_id(self, offer_id: str, version: int) -> ConsentOffer | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM consent_offer WHERE offer_id = $1 AND version = $2",
            offer_id,
            version,
        )
        return _row_to_offer(row)

    async def lock_offer_head(
        self, offer_id: str, actor_id: str, subject_id: str
    ) -> ConsentOffer | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM consent_ensure_offer_head($1, $2, $3)",
            offer_id,
            actor_id,
            subject_id,
        )
        if row is None:
            raise ConsentConsistencyError("offer authority head could not be locked")
        version = int(row["current_version"])
        current_hash = str(row["current_hash"]) if row["current_hash"] is not None else None
        self._locked_offer_heads[offer_id] = (version, current_hash)
        return await self.offer_by_id(offer_id, version) if version > 0 else None

    async def latest_consent(self, consent_id: str) -> ConsentEvidence | None:
        row = await self._conn.fetchrow(
            "SELECT evidence_json FROM consent_evidence "
            "WHERE consent_id = $1 ORDER BY version DESC LIMIT 1",
            consent_id,
        )
        return _row_to_evidence(row)

    async def get_consent(self, consent_id: str, version: int) -> ConsentEvidence | None:
        row = await self._conn.fetchrow(
            "SELECT evidence_json FROM consent_evidence WHERE consent_id = $1 AND version = $2",
            consent_id,
            version,
        )
        return _row_to_evidence(row)

    async def lock_consent_head(
        self,
        request_actor_id: str,
        evidence_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: str,
        purpose: str,
    ) -> ConsentEvidence | None:
        key = (
            evidence_actor_id,
            subject_id,
            binding_id,
            binding_version,
            capability,
            purpose,
        )
        row = await self._conn.fetchrow(
            "SELECT * FROM consent_lock_authority_head($1,$2,$3,$4,$5,$6,$7)",
            request_actor_id,
            evidence_actor_id,
            subject_id,
            binding_id,
            binding_version,
            capability,
            purpose,
        )
        if row is None:
            row = await self._conn.fetchrow(
                "SELECT * FROM consent_ensure_authority_head($1,$2,$3,$4,$5,$6,$7)",
                request_actor_id,
                evidence_actor_id,
                subject_id,
                binding_id,
                binding_version,
                capability,
                purpose,
            )
        if row is None:
            raise ConsentConsistencyError("consent authority head could not be locked")
        revision = int(row["current_revision"])
        current_hash = str(row["current_hash"]) if row["current_hash"] is not None else None
        self._locked_consent_heads[key] = (revision, current_hash, request_actor_id)
        self._operation_actor_id = request_actor_id
        self._operation_binding_id = binding_id
        if revision == 0:
            return None
        if "evidence_json" not in row:
            row = await self._conn.fetchrow(
                "SELECT * FROM consent_lock_authority_head($1,$2,$3,$4,$5,$6,$7)",
                request_actor_id,
                evidence_actor_id,
                subject_id,
                binding_id,
                binding_version,
                capability,
                purpose,
            )
            if row is None:
                raise ConsentConsistencyError("current consent head lacks immutable evidence")
        return _row_to_evidence(row)

    async def active_chains(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> tuple[ConsentEvidence, ...]:
        rows = await self._conn.fetch(
            "SELECT e.evidence_json FROM consent_evidence e "
            "WHERE e.status = 'active' AND e.subject_id = $1 "
            "AND e.binding_id = $2 AND e.binding_version = $3 "
            "AND e.version = (SELECT MAX(e2.version) FROM consent_evidence e2 "
            "WHERE e2.consent_id = e.consent_id)",
            subject_id,
            binding_id,
            binding_version,
        )
        evidences: list[ConsentEvidence] = []
        for row in rows:
            evidence = _row_to_evidence(row)
            assert evidence is not None
            evidences.append(evidence)
        return tuple(evidences)

    async def all_active_chains(self) -> tuple[ConsentEvidence, ...]:
        rows = await self._conn.fetch(
            "SELECT e.evidence_json FROM consent_evidence e "
            "WHERE e.status = 'active' "
            "AND e.version = (SELECT MAX(e2.version) FROM consent_evidence e2 "
            "WHERE e2.consent_id = e.consent_id)"
        )
        evidences: list[ConsentEvidence] = []
        for row in rows:
            evidence = _row_to_evidence(row)
            assert evidence is not None
            evidences.append(evidence)
        return tuple(evidences)

    async def latest_snapshot(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> ConsentSnapshot | None:
        row = await self._conn.fetchrow(
            "SELECT snapshot_json FROM consent_snapshot "
            "WHERE subject_id = $1 AND binding_id = $2 AND binding_version = $3 "
            "ORDER BY version DESC LIMIT 1",
            subject_id,
            binding_id,
            binding_version,
        )
        return _row_to_snapshot(row)

    async def lock_snapshot_head(
        self,
        request_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> ConsentSnapshot | None:
        key = (subject_id, binding_id, binding_version)
        row = await self._conn.fetchrow(
            "SELECT * FROM consent_lock_snapshot_head($1, $2, $3, $4)",
            request_actor_id,
            subject_id,
            binding_id,
            binding_version,
        )
        if row is None:
            row = await self._conn.fetchrow(
                "SELECT * FROM consent_ensure_snapshot_head($1, $2, $3, $4)",
                request_actor_id,
                subject_id,
                binding_id,
                binding_version,
            )
        if row is None:
            raise ConsentConsistencyError("consent snapshot head could not be locked")
        revision = int(row["current_revision"])
        current_hash = str(row["current_hash"]) if row["current_hash"] is not None else None
        self._locked_snapshot_heads[key] = (revision, current_hash, request_actor_id)
        self._operation_actor_id = request_actor_id
        self._operation_binding_id = binding_id
        if revision == 0:
            return None
        if "snapshot_json" not in row:
            row = await self._conn.fetchrow(
                "SELECT * FROM consent_lock_snapshot_head($1, $2, $3, $4)",
                request_actor_id,
                subject_id,
                binding_id,
                binding_version,
            )
            if row is None:
                raise ConsentConsistencyError("current snapshot head lacks immutable snapshot")
        return _row_to_snapshot(row)

    async def snapshot_by_id(self, snapshot_id: str) -> ConsentSnapshot | None:
        row = await self._conn.fetchrow(
            "SELECT snapshot_json FROM consent_snapshot WHERE snapshot_id = $1",
            snapshot_id,
        )
        return _row_to_snapshot(row)

    async def outbox_by_id(self, event_id: str) -> ConsentOutboxEvent | None:
        row = await self._conn.fetchrow("SELECT * FROM consent_read_outbox($1)", event_id)
        return _row_to_event(row)

    async def audit_by_id(self, audit_id: str) -> AuditEntry | None:
        row = await self._conn.fetchrow("SELECT * FROM consent_read_audit($1)", audit_id)
        return _row_to_audit(row)

    async def get_idempotency(self, idempotency_key: str) -> IdempotencyRecord | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM consent_idempotency WHERE idempotency_key = $1",
            idempotency_key,
        )
        return _row_to_idempotency(row)

    async def append_consent(self, evidence: ConsentEvidence) -> None:
        key = (
            evidence.actor_id,
            evidence.subject_id,
            evidence.binding_id,
            evidence.binding_version,
            evidence.capability,
            evidence.purpose,
        )
        if key not in self._locked_consent_heads:
            await self.lock_consent_head(
                evidence.actor_id,
                evidence.actor_id,
                evidence.subject_id,
                evidence.binding_id,
                evidence.binding_version,
                evidence.capability,
                evidence.purpose,
            )
        try:
            await self._conn.execute(
                "INSERT INTO consent_evidence ("
                " consent_id, version, actor_id, subject_id, binding_id,"
                " binding_version, status, evidence_json"
                ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                evidence.consent_id,
                evidence.version,
                evidence.actor_id,
                evidence.subject_id,
                evidence.binding_id,
                evidence.binding_version,
                evidence.status,
                _jsonb(evidence.to_canonical_dict()),
            )
            if self._operation_actor_id is None:
                self._operation_actor_id = evidence.actor_id
            self._operation_binding_id = evidence.binding_id
            self._pending_consent_heads.setdefault(key, []).append(evidence)
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise ConsentConflictError(
                f"consent version ({evidence.consent_id}, {evidence.version}) already exists"
            ) from exc

    async def append_offer(self, offer: ConsentOffer) -> None:
        if offer.offer_id not in self._locked_offer_heads:
            await self.lock_offer_head(offer.offer_id, offer.actor_id, offer.subject_id)
        try:
            await self._conn.execute(
                "INSERT INTO consent_offer ("
                " offer_id, version, status, capability, actor_id, subject_id,"
                " resource_owner_id, purpose, params_json, policy_version,"
                " valid_from, valid_until, issued_at, issuer, canonical_hash,"
                " supersedes_offer_id"
                ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,"
                " $12, $13, $14, $15, $16)",
                offer.offer_id,
                offer.version,
                offer.status,
                offer.capability,
                offer.actor_id,
                offer.subject_id,
                offer.resource_owner_id,
                offer.purpose,
                _jsonb(offer.params.to_canonical_dict()),
                offer.policy_version,
                offer.valid_from,
                offer.valid_until,
                offer.created_at,
                offer.issuer,
                offer.canonical_hash,
                offer.supersedes_offer_id,
            )
            authorization = await self._conn.fetchrow(
                "SELECT binding_id FROM consent_authorization "
                "WHERE db_role = 'memoria_consent' AND actor_id = $1 "
                "AND subject_id = $2 ORDER BY binding_id LIMIT 1",
                offer.actor_id,
                offer.subject_id,
            )
            if authorization is None:
                raise ConsentConsistencyError(
                    "offer persistence requires a deployment authorization mapping"
                )
            self._operation_actor_id = offer.actor_id
            self._operation_binding_id = str(authorization["binding_id"])
            expected_version, expected_hash = self._locked_offer_heads[offer.offer_id]
            advanced = await self._conn.fetchrow(
                "SELECT * FROM consent_advance_offer_head($1,$2,$3,$4,$5,$6)",
                offer.offer_id,
                expected_version,
                expected_hash,
                offer.version,
                offer.canonical_hash,
                offer.status,
            )
            if advanced is None:
                raise ConsentConflictError("offer authority head did not advance")
            self._locked_offer_heads[offer.offer_id] = (offer.version, offer.canonical_hash)
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise ConsentConflictError(
                f"offer version ({offer.offer_id}, {offer.version}) already exists"
            ) from exc

    async def append_snapshot(self, snapshot: ConsentSnapshot) -> None:
        actor_id = self._operation_actor_id or _snapshot_actor_id(snapshot)
        snapshot_key = (
            snapshot.subject_id,
            snapshot.binding_id,
            snapshot.binding_version,
        )
        if snapshot_key not in self._locked_snapshot_heads:
            await self.lock_snapshot_head(
                actor_id,
                snapshot.subject_id,
                snapshot.binding_id,
                snapshot.binding_version,
            )
        try:
            await self._conn.execute(
                "INSERT INTO consent_snapshot ("
                " snapshot_id, version, actor_id, subject_id, binding_id,"
                " binding_version, snapshot_json"
                ") VALUES ($1, $2, $3, $4, $5, $6, $7)",
                snapshot.snapshot_id,
                snapshot.version,
                actor_id,
                snapshot.subject_id,
                snapshot.binding_id,
                snapshot.binding_version,
                _jsonb(snapshot.to_canonical_dict()),
            )
            self._operation_actor_id = actor_id
            self._operation_binding_id = snapshot.binding_id
            expected_revision, expected_hash, request_actor_id = self._locked_snapshot_heads[
                snapshot_key
            ]
            advanced_snapshot = await self._conn.fetchrow(
                "SELECT * FROM consent_advance_snapshot_head("
                "$1,$2,$3,$4,$5,$6,$7,$8,$9)",
                request_actor_id,
                snapshot.subject_id,
                snapshot.binding_id,
                snapshot.binding_version,
                expected_revision,
                expected_hash,
                snapshot.snapshot_id,
                snapshot.version,
                snapshot.canonical_hash,
            )
            if advanced_snapshot is None:
                raise ConsentConflictError("snapshot authority head did not advance")
            self._locked_snapshot_heads[snapshot_key] = (
                snapshot.version,
                snapshot.canonical_hash,
                request_actor_id,
            )
            for key, evidences in tuple(self._pending_consent_heads.items()):
                for evidence in evidences:
                    head_revision, head_hash, head_request_actor_id = (
                        self._locked_consent_heads[key]
                    )
                    advanced_head = await self._conn.fetchrow(
                        "SELECT * FROM consent_advance_authority_head("
                        "$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                        head_request_actor_id,
                        evidence.actor_id,
                        evidence.subject_id,
                        evidence.binding_id,
                        evidence.binding_version,
                        evidence.capability,
                        evidence.purpose,
                        head_revision,
                        head_hash,
                        evidence.consent_id,
                        evidence.version,
                        evidence.canonical_hash,
                    )
                    if advanced_head is None:
                        raise ConsentConflictError("consent authority head did not advance")
                    self._locked_consent_heads[key] = (
                        evidence.version,
                        evidence.canonical_hash,
                        head_request_actor_id,
                    )
                del self._pending_consent_heads[key]
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise ConsentConflictError(
                f"snapshot {snapshot.snapshot_id!r} or its version already exists"
            ) from exc

    async def append_outbox(self, event: ConsentOutboxEvent) -> None:
        payload = dict(event.payload)
        actor_id = self._operation_actor_id or payload.get("actor_id", "")
        binding_id = self._operation_binding_id or payload.get("binding_id", "")
        try:
            await self._conn.execute(
                "INSERT INTO consent_outbox ("
                " event_id, aggregate_type, aggregate_id, version, subject_id,"
                " binding_id, actor_id, payload_json, created_at, idempotency_key"
                ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                event.event_id,
                event.aggregate_type,
                event.aggregate_id,
                event.version,
                payload.get("subject_id", ""),
                binding_id,
                actor_id,
                _jsonb(list(event.payload)),
                event.created_at,
                event.idempotency_key,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise ConsentConflictError(f"outbox event {event.event_id!r} already exists") from exc

    async def append_audit(self, entry: AuditEntry) -> None:
        binding_id = await self._operation_binding(entry.snapshot_id)
        try:
            await self._conn.execute(
                "INSERT INTO consent_audit ("
                " audit_id, event_id, action, actor_id, subject_id, binding_id,"
                " consent_id, snapshot_id, payload_json, created_at"
                ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                entry.audit_id,
                entry.event_id,
                entry.action,
                entry.actor_id,
                entry.subject_id,
                binding_id,
                entry.consent_id,
                entry.snapshot_id,
                _jsonb(list(entry.payload)),
                entry.created_at,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise ConsentConflictError(f"audit entry {entry.audit_id!r} already exists") from exc

    async def save_idempotency(self, record: IdempotencyRecord) -> None:
        binding_id = await self._operation_binding(record.snapshot_id)
        try:
            await self._conn.execute(
                "INSERT INTO consent_idempotency ("
                " idempotency_key, content_hash, consent_id, version, snapshot_id,"
                " event_id, audit_id, actor_id, subject_id, binding_id, created_at"
                ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                record.idempotency_key,
                record.content_hash,
                record.consent_id,
                record.version,
                record.snapshot_id,
                record.event_id,
                record.audit_id,
                record.actor_id or "",
                record.subject_id or "",
                binding_id,
                record.created_at,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise ConsentConflictError(
                f"idempotency key {record.idempotency_key!r} already exists"
            ) from exc

    async def _operation_binding(self, snapshot_id: str | None) -> str:
        if self._operation_binding_id is not None:
            return self._operation_binding_id
        if snapshot_id is not None:
            row = await self._conn.fetchrow(
                "SELECT binding_id FROM consent_snapshot WHERE snapshot_id = $1",
                snapshot_id,
            )
            if row is not None:
                binding_id = str(row["binding_id"])
                self._operation_binding_id = binding_id
                return binding_id
        raise RuntimeError("binding_id is required for consent persistence")

    async def commit(self) -> None:
        if self._closed:
            return
        await self._transaction.commit()
        self._closed = True
        await self._pool.release(self._conn)

    async def rollback(self) -> None:
        if self._closed:
            return
        await self._transaction.rollback()
        self._closed = True
        await self._pool.release(self._conn)

    async def __aenter__(self) -> AsyncConsentUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._closed:
            return
        if exc_type is None:
            await self.commit()
        else:
            await self.rollback()


def _jsonb(value: object) -> Any:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row_to_evidence(row: asyncpg.Record | None) -> ConsentEvidence | None:
    if row is None:
        return None
    return ConsentEvidence.from_canonical_dict(_jsonb_object(row["evidence_json"], "evidence_json"))


def _row_to_offer(row: asyncpg.Record | None) -> ConsentOffer | None:
    if row is None:
        return None
    return ConsentOffer.from_canonical_dict(
        {
            "offer_id": row["offer_id"],
            "version": row["version"],
            "status": row["status"],
            "capability": row["capability"],
            "actor_id": row["actor_id"],
            "subject_id": row["subject_id"],
            "resource_owner_id": row["resource_owner_id"],
            "purpose": row["purpose"],
            "params": _jsonb_object(row["params_json"], "params_json"),
            "policy_version": row["policy_version"],
            "valid_from": _aware(row["valid_from"]).isoformat(),
            "valid_until": _aware(row["valid_until"]).isoformat(),
            "created_at": _aware(row["issued_at"]).isoformat(),
            "issuer": row["issuer"],
            "canonical_hash": row["canonical_hash"],
            "supersedes_offer_id": row["supersedes_offer_id"],
        }
    )


def _row_to_snapshot(row: asyncpg.Record | None) -> ConsentSnapshot | None:
    if row is None:
        return None
    return ConsentSnapshot.from_canonical_dict(_jsonb_object(row["snapshot_json"], "snapshot_json"))


def _row_to_event(row: asyncpg.Record) -> ConsentOutboxEvent:
    payload = _jsonb_array(row["payload_json"], "outbox payload")
    return ConsentOutboxEvent(
        event_id=row["event_id"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        version=row["version"],
        payload=_jsonb_pairs(payload, "outbox payload"),
        created_at=_aware(row["created_at"]),
        idempotency_key=row["idempotency_key"],
    )


def _row_to_audit(row: asyncpg.Record) -> AuditEntry:
    payload = _jsonb_array(row["payload_json"], "audit payload")
    return AuditEntry(
        audit_id=row["audit_id"],
        event_id=row["event_id"],
        action=row["action"],
        actor_id=row["actor_id"],
        subject_id=row["subject_id"],
        consent_id=row["consent_id"],
        snapshot_id=row["snapshot_id"],
        payload=_jsonb_pairs(payload, "audit payload"),
        created_at=_aware(row["created_at"]),
    )


def _row_to_idempotency(row: asyncpg.Record | None) -> IdempotencyRecord | None:
    if row is None:
        return None
    return IdempotencyRecord(
        idempotency_key=row["idempotency_key"],
        content_hash=row["content_hash"],
        consent_id=row["consent_id"],
        version=row["version"],
        snapshot_id=row["snapshot_id"],
        event_id=row["event_id"],
        audit_id=row["audit_id"],
        actor_id=row["actor_id"],
        subject_id=row["subject_id"],
        created_at=_aware(row["created_at"]),
    )


def _snapshot_actor_id(snapshot: ConsentSnapshot) -> str:
    if snapshot.grants:
        return snapshot.grants[-1].actor_id
    if snapshot.relationships:
        return snapshot.relationships[0].source_person_id
    return snapshot.subject_id


def _decoded_jsonb(value: object, field_name: str) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} is not valid JSON") from exc
    return value


def _jsonb_object(value: object, field_name: str) -> dict[str, object]:
    decoded = _decoded_jsonb(value, field_name)
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise ValueError(f"{field_name} must be a JSON object")
    return decoded


def _jsonb_array(value: object, field_name: str) -> list[object]:
    decoded = _decoded_jsonb(value, field_name)
    if not isinstance(decoded, list):
        raise ValueError(f"{field_name} must be a JSON array")
    return decoded


def _jsonb_pairs(value: list[object], field_name: str) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise ValueError(f"{field_name} must contain string pairs")
        pairs.append((item[0], item[1]))
    return normalize_payload(tuple(pairs))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["PostgresConsentStore"]
