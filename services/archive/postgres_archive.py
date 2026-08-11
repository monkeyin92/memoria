"""PostgreSQL production adapter for the LifeArchive public contract."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import asyncpg

from services.archive.domain import (
    ContextBundle,
    ContextQuery,
    EvidenceEvent,
    EvidenceNotFoundError,
    IdempotencyConflictError,
    MemoryReview,
    OutboxReplayResult,
    RawVoiceConsent,
    RawVoiceConsentRequiredError,
    RawVoiceRetentionPolicy,
    RawVoiceRevocation,
    RecordResult,
    ReviewedMemory,
)
from services.archive.object_store import ObjectRef


class PostgresLifeArchive:
    def __init__(self, dsn: str, *, outbox_max_attempts: int = 8) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("archive DSN must use PostgreSQL")
        if not 1 <= outbox_max_attempts <= 100:
            raise ValueError("archive outbox max attempts must be between 1 and 100")
        self._dsn = dsn
        self._outbox_max_attempts = outbox_max_attempts
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10, command_timeout=10)
        if pool is None:  # pragma: no cover - asyncpg returns a pool outside its context manager
            raise RuntimeError("failed to create PostgreSQL pool")
        schema = Path(__file__).with_name("postgres_schema.sql").read_text(encoding="utf-8")
        async with pool.acquire() as connection:
            await connection.execute(schema)
        self._pool = pool

    async def _ready_pool(self) -> asyncpg.Pool:
        await self.initialize()
        if self._pool is None:  # pragma: no cover
            raise RuntimeError("PostgreSQL archive is not initialized")
        return self._pool

    @staticmethod
    async def _scope(connection: asyncpg.Connection, account_id: str) -> None:
        await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)

    async def record(self, event: EvidenceEvent) -> RecordResult:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            return await self._record_with_connection(connection, event)

    async def event(
        self,
        *,
        account_id: str,
        event_id: str,
    ) -> EvidenceEvent | None:
        if not account_id.strip() or not event_id.strip():
            raise ValueError("event lookup requires account_id and event_id")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            row = await connection.fetchrow(
                """
                SELECT * FROM archive_evidence_events
                WHERE account_id = $1 AND event_id = $2
                """,
                account_id,
                event_id,
            )
        return self._event_from_row(row) if row is not None else None

    async def turn_event(
        self,
        *,
        account_id: str,
        session_id: str,
        turn_id: int,
        generation_id: int,
        event_type: str,
    ) -> EvidenceEvent | None:
        if not account_id.strip() or not session_id.strip() or not event_type.strip():
            raise ValueError("turn event lookup requires account, session and event type")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT * FROM archive_evidence_events
                WHERE account_id = $1 AND session_id = $2
                  AND turn_id = $3 AND generation_id = $4 AND event_type = $5
                ORDER BY recorded_at DESC, event_id DESC
                LIMIT 2
                """,
                account_id,
                session_id,
                turn_id,
                generation_id,
                event_type,
            )
        if len(rows) > 1:
            raise IdempotencyConflictError("turn has multiple canonical evidence events")
        return self._event_from_row(rows[0]) if rows else None

    async def evidence_window(
        self,
        *,
        account_id: str,
        occurred_after: datetime,
        occurred_before: datetime,
        event_types: tuple[str, ...] = (),
        limit: int = 10_000,
    ) -> tuple[EvidenceEvent, ...]:
        if not account_id.strip() or not 1 <= limit <= 10_000:
            raise ValueError("evidence window requires account_id and limit 1..10000")
        if occurred_after.tzinfo is None or occurred_before.tzinfo is None:
            raise ValueError("evidence window timestamps must include timezone")
        start = occurred_after.astimezone(UTC)
        end = occurred_before.astimezone(UTC)
        if start > end:
            raise ValueError("evidence window start must not follow end")
        if any(not value.strip() for value in event_types):
            raise ValueError("evidence event types must not be blank")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT * FROM archive_evidence_events
                WHERE account_id = $1 AND occurred_at >= $2 AND occurred_at <= $3
                  AND (cardinality($4::text[]) = 0 OR event_type = ANY($4::text[]))
                ORDER BY occurred_at, event_id
                LIMIT $5
                """,
                account_id,
                start,
                end,
                list(event_types),
                limit,
            )
        return tuple(self._event_from_row(row) for row in rows)

    async def grant_raw_voice_consent(
        self,
        *,
        account_id: str,
        policy_version: str,
        retention_policy: RawVoiceRetentionPolicy,
        granted_at: datetime,
    ) -> RawVoiceConsent:
        if not account_id.strip() or not policy_version.strip():
            raise ValueError("raw voice consent requires account_id and policy_version")
        if granted_at.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        granted_at = granted_at.astimezone(UTC)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"raw_voice_archive:{account_id}",
            )
            row = await connection.fetchrow(
                """
                SELECT *, (expires_at IS NULL OR expires_at > now()) AS is_active
                FROM archive_consent_grants
                WHERE account_id = $1 AND purpose = 'raw_voice_archive'
                  AND revoked_at IS NULL
                ORDER BY granted_at DESC, consent_grant_id DESC
                LIMIT 1 FOR UPDATE
                """,
                account_id,
            )
            active = self._consent_from_row(row) if row is not None else None
            if (
                active is not None
                and bool(row["is_active"])
                and active.policy_version == policy_version
                and active.retention_policy == retention_policy
            ):
                return active
            if active is not None:
                await connection.execute(
                    "UPDATE archive_consent_grants SET revoked_at = $1 "
                    "WHERE consent_grant_id = $2",
                    granted_at,
                    active.consent_grant_id,
                )
            consent = RawVoiceConsent(
                consent_grant_id=str(uuid.uuid4()),
                account_id=account_id,
                policy_version=policy_version,
                retention_policy=retention_policy,
                granted_at=granted_at,
            )
            await connection.execute(
                """
                INSERT INTO archive_consent_grants (
                    consent_grant_id, account_id, purpose, policy_version,
                    retention_policy, granted_at
                ) VALUES ($1, $2, 'raw_voice_archive', $3, $4, $5)
                """,
                consent.consent_grant_id,
                account_id,
                policy_version,
                retention_policy,
                granted_at,
            )
            return consent

    async def active_raw_voice_consent(self, *, account_id: str) -> RawVoiceConsent | None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            row = await connection.fetchrow(
                """
                SELECT * FROM archive_consent_grants
                WHERE account_id = $1 AND purpose = 'raw_voice_archive'
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY granted_at DESC, consent_grant_id DESC
                LIMIT 1
                """,
                account_id,
            )
        return self._consent_from_row(row) if row is not None else None

    async def revoke_raw_voice_consent(
        self,
        *,
        account_id: str,
        revoked_at: datetime,
    ) -> RawVoiceRevocation:
        if revoked_at.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        revoked_at = revoked_at.astimezone(UTC)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"raw_voice_archive:{account_id}",
            )
            row = await connection.fetchrow(
                """
                SELECT * FROM archive_consent_grants
                WHERE account_id = $1 AND purpose = 'raw_voice_archive'
                  AND revoked_at IS NULL
                ORDER BY granted_at DESC, consent_grant_id DESC
                LIMIT 1 FOR UPDATE
                """,
                account_id,
            )
            if row is None:
                row = await connection.fetchrow(
                    """
                    SELECT * FROM archive_consent_grants
                    WHERE account_id = $1 AND purpose = 'raw_voice_archive'
                    ORDER BY granted_at DESC, consent_grant_id DESC
                    LIMIT 1
                    """,
                    account_id,
                )
            if row is None:
                raise RawVoiceConsentRequiredError("active raw voice consent is required")
            consent = self._consent_from_row(row)
            effective_revoked_at = consent.revoked_at or revoked_at
            if consent.revoked_at is None:
                await connection.execute(
                    "UPDATE archive_consent_grants SET revoked_at = $1 "
                    "WHERE consent_grant_id = $2",
                    effective_revoked_at,
                    consent.consent_grant_id,
                )
            rows = await connection.fetch(
                """
                SELECT blob.* FROM archive_evidence_blobs blob
                JOIN archive_evidence_events event
                  ON event.event_id = blob.evidence_event_id
                JOIN archive_consent_grants consent
                  ON consent.consent_grant_id = event.consent_grant_id
                WHERE blob.account_id = $1 AND consent.purpose = 'raw_voice_archive'
                ORDER BY blob.created_at, blob.blob_id
                """,
                account_id,
            )
        return RawVoiceRevocation(
            consent=RawVoiceConsent(
                consent_grant_id=consent.consent_grant_id,
                account_id=consent.account_id,
                policy_version=consent.policy_version,
                retention_policy=consent.retention_policy,
                granted_at=consent.granted_at,
                revoked_at=effective_revoked_at,
            ),
            references=self._raw_voice_references(account_id, rows),
        )

    async def purge_raw_voice_blobs(
        self,
        *,
        account_id: str,
        object_keys: tuple[str, ...],
    ) -> None:
        keys = tuple(dict.fromkeys(object_keys))
        if not keys:
            return
        if not account_id.strip() or any(not key.strip() for key in keys):
            raise ValueError("raw voice purge requires account_id and object keys")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await connection.execute(
                """
                DELETE FROM archive_evidence_blobs blob
                USING archive_evidence_events event, archive_consent_grants consent
                WHERE blob.evidence_event_id = event.event_id
                  AND event.consent_grant_id = consent.consent_grant_id
                  AND blob.account_id = $1
                  AND event.account_id = $1
                  AND consent.purpose = 'raw_voice_archive'
                  AND blob.object_key = ANY($2::text[])
                """,
                account_id,
                list(keys),
            )

    async def record_with_blob(
        self,
        event: EvidenceEvent,
        reference: ObjectRef,
        *,
        retention_policy: RawVoiceRetentionPolicy,
    ) -> RecordResult:
        if event.speaker_class != "owner" or reference.account_id != event.account_id:
            raise RawVoiceConsentRequiredError("raw voice archive is restricted to the owner")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, event.account_id)
            consent = await connection.fetchval(
                """
                SELECT 1 FROM archive_consent_grants
                WHERE consent_grant_id = $1 AND account_id = $2
                  AND purpose = 'raw_voice_archive' AND retention_policy = $3
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > now())
                FOR SHARE
                """,
                event.consent_grant_id,
                event.account_id,
                retention_policy,
            )
            if consent is None:
                raise RawVoiceConsentRequiredError("active raw voice consent is required")
            result = await self._record_with_connection(connection, event)
            existing = await connection.fetchrow(
                "SELECT * FROM archive_evidence_blobs WHERE evidence_event_id = $1",
                event.event_id,
            )
            if existing is not None:
                if (
                    str(existing["content_sha256"]) != reference.content_sha256
                    or int(existing["byte_count"]) != reference.byte_count
                    or str(existing["retention_policy"]) != retention_policy
                ):
                    raise IdempotencyConflictError("raw voice retry has different blob content")
                return replace(
                    result,
                    blob_duplicate=True,
                    retained_object_key=str(existing["object_key"]),
                )
            await connection.execute(
                """
                INSERT INTO archive_evidence_blobs (
                    blob_id, account_id, evidence_event_id, object_key, media_type,
                    byte_count, content_sha256, encryption_key_version,
                    retention_policy
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:blob:{event.event_id}"),
                event.account_id,
                event.event_id,
                reference.object_key,
                reference.media_type,
                reference.byte_count,
                reference.content_sha256,
                reference.encryption_key_version,
                retention_policy,
            )
            return replace(
                result,
                blob_duplicate=False,
                retained_object_key=reference.object_key,
            )

    async def raw_voice_blobs(self, *, account_id: str) -> tuple[ObjectRef, ...]:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT blob.* FROM archive_evidence_blobs blob
                JOIN archive_evidence_events event
                  ON event.event_id = blob.evidence_event_id
                JOIN archive_consent_grants consent
                  ON consent.consent_grant_id = event.consent_grant_id
                WHERE blob.account_id = $1 AND consent.purpose = 'raw_voice_archive'
                ORDER BY blob.created_at, blob.blob_id
                """,
                account_id,
            )
        return self._raw_voice_references(account_id, rows)

    @staticmethod
    def _raw_voice_references(
        account_id: str,
        rows: list[asyncpg.Record],
    ) -> tuple[ObjectRef, ...]:
        return tuple(
            ObjectRef(
                account_id=account_id,
                object_key=str(row["object_key"]),
                media_type=str(row["media_type"]),
                byte_count=int(row["byte_count"]),
                content_sha256=str(row["content_sha256"]),
                encryption_key_version=str(row["encryption_key_version"]),
                backend="archive",
            )
            for row in rows
        )

    @staticmethod
    def _consent_from_row(row: asyncpg.Record) -> RawVoiceConsent:
        return RawVoiceConsent(
            consent_grant_id=str(row["consent_grant_id"]),
            account_id=str(row["account_id"]),
            policy_version=str(row["policy_version"]),
            retention_policy=cast(RawVoiceRetentionPolicy, row["retention_policy"]),
            granted_at=cast(datetime, row["granted_at"]),
            revoked_at=cast(datetime | None, row["revoked_at"]),
        )

    async def import_events_atomic(
        self,
        events: list[EvidenceEvent],
    ) -> list[RecordResult]:
        """Migration-only batch: all new evidence commits or none of it does."""
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            return [await self._record_with_connection(connection, event) for event in events]

    async def _record_with_connection(
        self,
        connection: asyncpg.Connection,
        event: EvidenceEvent,
    ) -> RecordResult:
        outbox_id = uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:outbox:{event.event_id}")
        await self._scope(connection, event.account_id)
        inserted = await connection.fetchrow(
                """
                INSERT INTO archive_evidence_events (
                    event_id, account_id, session_id, turn_id, generation_id,
                    event_type, schema_version, occurred_at, speaker_identity_id,
                    speaker_class, source, consent_grant_id, payload,
                    content_sha256, supersedes_event_id
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                    $13::jsonb, $14, $15
                )
                ON CONFLICT (event_id) DO NOTHING
                RETURNING recorded_at
                """,
                event.event_id,
                event.account_id,
                event.session_id,
                event.turn_id,
                event.generation_id,
                event.event_type,
                event.schema_version,
                event.occurred_at,
                event.speaker_identity_id,
                event.speaker_class,
                event.source,
                event.consent_grant_id,
                json.dumps(dict(event.payload), ensure_ascii=False, separators=(",", ":")),
                event.content_sha256,
                event.supersedes_event_id,
        )
        if inserted is not None:
            await connection.execute(
                """
                INSERT INTO archive_processing_outbox (
                    outbox_id, account_id, event_id, task_type, max_attempts
                ) VALUES ($1, $2, $3, 'compile_evidence', $4)
                """,
                outbox_id,
                event.account_id,
                event.event_id,
                self._outbox_max_attempts,
            )
            return RecordResult(
                event_id=event.event_id,
                outbox_id=str(outbox_id),
                recorded_at=cast(datetime, inserted["recorded_at"]),
                duplicate=False,
            )

        existing = await connection.fetchrow(
            """
            SELECT e.*, o.outbox_id
            FROM archive_evidence_events e
            JOIN archive_processing_outbox o ON o.event_id = e.event_id
            WHERE e.event_id = $1
            """,
            event.event_id,
        )
        if existing is None or (
            str(existing["content_sha256"]) != event.content_sha256
            and self._event_from_row(existing).idempotency_sha256 != event.idempotency_sha256
        ):
            raise IdempotencyConflictError(
                f"event_id {event.event_id!r} already has different content"
            )
        return RecordResult(
            event_id=event.event_id,
            outbox_id=str(existing["outbox_id"]),
            recorded_at=cast(datetime, existing["recorded_at"]),
            duplicate=True,
        )

    async def replay_dead_letter(
        self,
        *,
        account_id: str,
        outbox_id: str,
        actor_id: str,
        reason: str,
    ) -> OutboxReplayResult:
        """Requeue one dead-lettered compile task with an account-scoped audit record."""

        if not account_id.strip() or not actor_id.strip() or not reason.strip():
            raise ValueError("outbox replay requires account_id, actor_id and reason")
        try:
            parsed_outbox_id = uuid.UUID(outbox_id)
        except ValueError as exc:
            raise ValueError("outbox_id must be a UUID") from exc
        if len(actor_id) > 128 or len(reason) > 512:
            raise ValueError("outbox replay actor or reason is too long")

        replay_id = uuid.uuid4()
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            row = await connection.fetchrow(
                """
                SELECT account_id, status, attempts, replay_count, last_error_code
                FROM archive_processing_outbox
                WHERE outbox_id = $1
                FOR UPDATE
                """,
                parsed_outbox_id,
            )
            if row is None:
                raise EvidenceNotFoundError("archive outbox item was not found")
            if str(row["status"]) != "dead":
                raise IdempotencyConflictError(
                    "archive outbox item is not in the dead-letter state"
                )
            previous_attempts = int(row["attempts"])
            replayed_at = cast(
                datetime,
                await connection.fetchval(
                    """
                    INSERT INTO archive_outbox_replay_audit (
                        replay_id, outbox_id, account_id, actor_id, reason,
                        previous_attempts, previous_error_code
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING replayed_at
                    """,
                    replay_id,
                    parsed_outbox_id,
                    account_id,
                    actor_id,
                    reason,
                    previous_attempts,
                    row["last_error_code"],
                ),
            )
            updated = await connection.fetchrow(
                """
                UPDATE archive_processing_outbox
                SET status = 'pending',
                    attempts = 0,
                    available_at = now(),
                    locked_until = NULL,
                    worker_id = NULL,
                    completed_at = NULL,
                    dead_lettered_at = NULL,
                    replay_count = replay_count + 1,
                    last_error_code = NULL
                WHERE outbox_id = $1 AND status = 'dead'
                RETURNING replay_count
                """,
                parsed_outbox_id,
            )
            if updated is None:  # pragma: no cover - row is locked in this transaction
                raise IdempotencyConflictError("archive outbox replay lost its state fence")
        return OutboxReplayResult(
            outbox_id=str(parsed_outbox_id),
            account_id=account_id,
            replay_id=str(replay_id),
            replay_count=int(updated["replay_count"]),
            previous_attempts=previous_attempts,
            replayed_at=replayed_at,
        )

    async def context(self, query: ContextQuery) -> ContextBundle:
        pool = await self._ready_pool()
        clauses = ["account_id = $1"]
        parameters: list[Any] = [query.account_id]
        if query.speaker_class == "owner":
            clauses.append("speaker_class IN ('owner', 'assistant', 'system')")
        else:
            if query.session_id is None:
                return ContextBundle()
            parameters.append(query.session_id)
            clauses.extend(
                [
                    f"session_id = ${len(parameters)}",
                    f"speaker_class IN (${len(parameters) + 1}, 'assistant', 'system')",
                ]
            )
            parameters.append(query.speaker_class)
        if query.text.strip():
            parameters.append(f"%{query.text.strip()}%")
            clauses.append(f"payload::text ILIKE ${len(parameters)}")
        parameters.append(query.limit)
        sql = f"""
            SELECT * FROM archive_evidence_events
            WHERE {' AND '.join(clauses)}
            ORDER BY occurred_at DESC, event_id DESC
            LIMIT ${len(parameters)}
        """
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, query.account_id)
            rows = await connection.fetch(sql, *parameters)
        return ContextBundle(evidence=tuple(self._event_from_row(row) for row in rows))

    async def review(self, command: MemoryReview) -> ReviewedMemory:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, command.account_id)
            row = await connection.fetchrow(
                """
                SELECT * FROM archive_evidence_events
                WHERE event_id = $1 AND account_id = $2
                """,
                command.target_id,
                command.account_id,
            )
            if row is None:
                raise EvidenceNotFoundError(command.target_id)
            target = self._event_from_row(row)
            current_text = (
                command.corrected_text.strip()
                if command.corrected_text is not None
                else str(target.payload.get("text") or "") or None
            )
            event_type = (
                "speech.transcript_revised"
                if command.action == "correct"
                else "memory.claim_reviewed"
            )
            await self._record_with_connection(
                connection,
                EvidenceEvent(
                    event_id=command.review_event_id,
                    account_id=command.account_id,
                    event_type=event_type,
                    occurred_at=command.occurred_at,
                    speaker_class="system",
                    source="user.archive_review",
                    payload={
                        "target_id": command.target_id,
                        "action": command.action,
                        **(
                            {"corrected_text": current_text}
                            if command.action == "correct"
                            else {}
                        ),
                    },
                    session_id=target.session_id,
                    turn_id=target.turn_id,
                    generation_id=target.generation_id,
                    supersedes_event_id=command.target_id,
                ),
            )
        status_by_action = {
            "confirm": "confirmed",
            "dispute": "disputed",
            "retract": "retracted",
            "correct": "corrected",
        }
        return ReviewedMemory(
            target_id=command.target_id,
            review_event_id=command.review_event_id,
            status=cast(Any, status_by_action[command.action]),
            current_text=current_text,
        )

    @staticmethod
    def _event_from_row(row: asyncpg.Record) -> EvidenceEvent:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return EvidenceEvent(
            event_id=str(row["event_id"]),
            account_id=str(row["account_id"]),
            event_type=str(row["event_type"]),
            occurred_at=cast(datetime, row["occurred_at"]),
            speaker_class=cast(Any, row["speaker_class"]),
            source=str(row["source"]),
            payload=cast(dict[str, Any], payload),
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            generation_id=row["generation_id"],
            speaker_identity_id=row["speaker_identity_id"],
            consent_grant_id=row["consent_grant_id"],
            schema_version=int(row["schema_version"]),
            supersedes_event_id=row["supersedes_event_id"],
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
