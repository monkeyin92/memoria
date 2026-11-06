"""PostgreSQL/RLS implementation of the rebuildable long-term memory catalog."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

import asyncpg

from services.archive.claim_subjects import BoundChildSubjectIds
from services.archive.domain import EvidenceEvent
from services.archive.episode_consolidator import EpisodeConsolidator
from services.archive.extraction_gate import IgnoredReason, Withhold, extraction_gate
from services.archive.memory_domain import (
    AccountWriteGuard,
    AccountWriteRejectedError,
    CompileReport,
    MemoryEmbedder,
    MemoryEmbeddingUnavailableError,
    MemoryExtraction,
    MemoryExtractor,
    MemorySearchItem,
)
from services.archive.memory_review import PostgresMemoryReviewMixin
from services.archive.memory_search import PostgresMemorySearchMixin
from services.archive.memory_timeline import PostgresMemoryTimelineMixin
from services.archive.memory_vector_backfill import (
    BackfillReport,
    ClaimVectorBackfill,
    MissingVectorCount,
)
from services.archive.memory_vector_cleanup import ensure_vector_index
from services.archive.memory_write import PostgresMemoryWriteMixin
from services.archive.memory_write_policy import (
    MemoryWritePolicy,
    filter_extraction_for_subject,
    is_policy_confirmation_event,
)
from services.common.evidence_policy import contribution_for


class OutboxLeaseLostError(RuntimeError):
    """A compiler attempted to commit after its monotonic outbox fence changed."""


@asynccontextmanager
async def _allow_account_write(_: str) -> AsyncIterator[None]:
    yield


class PostgresMemoryCatalog(
    PostgresMemoryReviewMixin,
    PostgresMemorySearchMixin,
    PostgresMemoryTimelineMixin,
    PostgresMemoryWriteMixin,
):
    def __init__(
        self,
        dsn: str,
        *,
        extractor: MemoryExtractor,
        compiler_dsn: str | None = None,
        compiler_role: str | None = None,
        account_guard: AccountWriteGuard | None = None,
        embedder: MemoryEmbedder | None = None,
        require_vector: bool = False,
        fail_on_embedding_error: bool = False,
        episode_consolidator: EpisodeConsolidator | None = None,
        subject_category_resolver: Callable[[str], str | None] | None = None,
        evidence_subject_category_resolver: (
            Callable[[EvidenceEvent], str | None | Awaitable[str | None]] | None
        ) = None,
        bound_child_subject_ids: BoundChildSubjectIds | None = None,
        capture_evidence_projector: Callable[[EvidenceEvent], Awaitable[bool]] | None = None,
        outbox_lease_s: float = 300.0,
        outbox_retry_base_s: float = 2.0,
        outbox_retry_max_s: float = 300.0,
        pool: asyncpg.Pool | None = None,
    ) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("memory catalog DSN must use PostgreSQL")
        if compiler_dsn is not None and not compiler_dsn.startswith(
            ("postgresql://", "postgres://")
        ):
            raise ValueError("memory compiler DSN must use PostgreSQL")
        if compiler_role is not None and not compiler_role.strip():
            raise ValueError("memory compiler role must not be blank")
        if not 5 <= outbox_lease_s <= 3600:
            raise ValueError("memory compiler lease must be between 5 and 3600 seconds")
        if not 0.1 <= outbox_retry_base_s <= 300:
            raise ValueError("memory compiler retry base must be between 0.1 and 300 seconds")
        if not outbox_retry_base_s <= outbox_retry_max_s <= 3600:
            raise ValueError(
                "memory compiler retry max must be between retry base and 3600 seconds"
            )
        self._dsn = dsn
        self._compiler_dsn = compiler_dsn
        self._compiler_role = compiler_role
        self._extractor = extractor
        self._account_guard = account_guard or _allow_account_write
        self._embedder = embedder
        self._require_vector = require_vector
        self._fail_on_embedding_error = fail_on_embedding_error
        self._episode_consolidator = episode_consolidator or EpisodeConsolidator()
        self._memory_write_policy = MemoryWritePolicy()
        self._subject_category_resolver = subject_category_resolver or (lambda _: None)
        # Compilation must ask about the evidence subject.  Callers that only
        # know the account owner keep the account resolver; it is not a guess
        # that every subject is that owner.
        self._evidence_subject_category_resolver = evidence_subject_category_resolver or (
            lambda event: self._subject_category_resolver(event.account_id)
        )
        self._bound_child_subject_ids = bound_child_subject_ids
        self._capture_evidence_projector = capture_evidence_projector
        self._outbox_lease_s = outbox_lease_s
        self._outbox_retry_base_s = outbox_retry_base_s
        self._outbox_retry_max_s = outbox_retry_max_s
        self._worker_id = f"memory-compiler:{uuid.uuid4()}"
        self._vector_enabled = False
        self._vector_backfill: ClaimVectorBackfill | None = None
        self._shared_pool = pool
        self._pool: asyncpg.Pool | None = None
        self._compiler_pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        pool = self._shared_pool or await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=10, command_timeout=15
        )
        if pool is None:  # pragma: no cover
            raise RuntimeError("failed to create PostgreSQL memory pool")
        archive_dir = Path(__file__).parent
        archive_schema = (archive_dir / "postgres_archive_schema.sql").read_text(encoding="utf-8")
        memory_schema = (archive_dir / "postgres_memory_schema.sql").read_text(encoding="utf-8")
        async with pool.acquire() as connection:
            await connection.execute(archive_schema)
            await connection.execute(memory_schema)
            self._vector_enabled = bool(
                await connection.fetchval(
                    "SELECT to_regclass('memory_vector_documents') IS NOT NULL"
                )
            )
            if self._vector_enabled and self._embedder is not None:
                await ensure_vector_index(connection, self._embedder)
        if self._require_vector and (not self._vector_enabled or self._embedder is None):
            await pool.close()
            raise RuntimeError("production memory catalog requires pgvector and an embedder")
        self._pool = pool
        if self._vector_enabled and self._embedder is not None:
            # Only a deployment with pgvector and an embedder can repair.
            self._vector_backfill = ClaimVectorBackfill(pool, self._embedder)
        if self._compiler_dsn is not None:
            compiler_pool = await asyncpg.create_pool(
                self._compiler_dsn, min_size=1, max_size=2, command_timeout=15
            )
            if compiler_pool is None:  # pragma: no cover
                raise RuntimeError("failed to create PostgreSQL compiler pool")
            try:
                async with compiler_pool.acquire() as connection:
                    compiler_user = str(await connection.fetchval("SELECT current_user"))
                    if self._compiler_role is not None and compiler_user != self._compiler_role:
                        raise RuntimeError("memory compiler DSN role does not match configuration")
                    role = await connection.fetchrow(
                        """
                        SELECT rolsuper, rolbypassrls,
                               pg_has_role(current_user, 'memoria_archive_compiler', 'member')
                                   AS compiler_member
                        FROM pg_roles WHERE rolname = current_user
                        """
                    )
                    if role is None or bool(role["rolsuper"]) or bool(role["rolbypassrls"]):
                        raise RuntimeError("memory compiler role must not bypass RLS")
                    if not bool(role["compiler_member"]):
                        raise RuntimeError(
                            "memory compiler role must belong to memoria_archive_compiler"
                        )
                async with pool.acquire() as connection:
                    archive_user = str(await connection.fetchval("SELECT current_user"))
                if archive_user == compiler_user:
                    raise RuntimeError("memory compiler must use an independent database role")
            except Exception:
                await compiler_pool.close()
                self._pool = None
                await pool.close()
                raise
            self._compiler_pool = compiler_pool

    async def _ready_pool(self) -> asyncpg.Pool:
        await self.initialize()
        if self._pool is None:  # pragma: no cover
            raise RuntimeError("PostgreSQL memory catalog is not initialized")
        return self._pool

    @property
    def vector_enabled(self) -> bool:
        """Whether this deployment has pgvector and an embedder.

        The periodic repair and background scheduling both key off this flag.
        """
        return self._vector_enabled and self._embedder is not None

    async def wait_for_vector_backfills(self) -> None:
        if self._vector_backfill is not None:
            await self._vector_backfill.wait()

    async def count_missing_claim_vectors(self, *, account_id: str) -> MissingVectorCount:
        """Read-only count of claims missing the current model's vector; zero when off."""

        if self._vector_backfill is None:
            return MissingVectorCount()
        return await self._vector_backfill.count(account_id=account_id)

    async def backfill_missing_claim_vectors(
        self, *, account_ids: Sequence[str], limit: int
    ) -> BackfillReport:
        """Repair one round; the module rechecks every row under lock before writing."""

        if self._vector_backfill is None:
            return BackfillReport()
        return await self._vector_backfill.backfill_accounts(account_ids=account_ids, limit=limit)

    def _schedule_claim_vector_backfill(
        self, *, account_id: str, items: tuple[MemorySearchItem, ...]
    ) -> None:
        """Queue repairs so recall never waits on embedding.

        A task failure only logs a count; the search result is unchanged."""

        if self._vector_backfill is None:
            return
        # Duplicate searches share the same in-flight key, so one claim is repaired once.
        self._vector_backfill.schedule(
            account_id=account_id,
            claim_ids=(uuid.UUID(item.item_id) for item in items if item.kind == "claim"),
        )

    @staticmethod
    async def _scope(connection: asyncpg.Connection, account_id: str) -> None:
        await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)

    async def compile_pending(self, *, limit: int = 100) -> CompileReport:
        if not 1 <= limit <= 1000:
            raise ValueError("compile limit must be between 1 and 1000")
        pool = await self._ready_pool()
        claim_pool = self._compiler_pool or pool
        async with claim_pool.acquire() as connection, connection.transaction():
            claimed = await connection.fetch(
                """
                WITH next_items AS (
                    SELECT outbox_id
                    FROM archive_processing_outbox
                    WHERE task_type = 'compile_evidence'
                      AND attempts < max_attempts
                      AND (
                          (
                              status IN ('pending', 'failed')
                              AND available_at <= now()
                          )
                          OR (
                              status = 'processing'
                              AND (locked_until IS NULL OR locked_until <= now())
                          )
                      )
                    ORDER BY created_at, outbox_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                UPDATE archive_processing_outbox outbox
                SET status = 'processing', attempts = outbox.attempts + 1,
                    locked_until = now() + ($2 * interval '1 second'),
                    fencing_token = outbox.fencing_token + 1,
                    worker_id = $3,
                    completed_at = NULL,
                    dead_lettered_at = NULL,
                    last_error_code = NULL
                FROM next_items
                WHERE outbox.outbox_id = next_items.outbox_id
                RETURNING outbox.outbox_id, outbox.event_id, outbox.account_id,
                          outbox.attempts, outbox.max_attempts, outbox.fencing_token
                """,
                limit,
                self._outbox_lease_s,
                self._worker_id,
            )

        outcomes = [await self._compile_claimed(row, pool) for row in claimed]
        return CompileReport(
            compiled_events=outcomes.count("compiled"),
            ignored_events=outcomes.count("ignored"),
            failed_events=outcomes.count("failed"),
        )

    async def _compile_claimed(
        self,
        claimed_row: asyncpg.Record,
        pool: asyncpg.Pool,
    ) -> str:
        account_id = str(claimed_row["account_id"])
        outbox_id = cast(uuid.UUID, claimed_row["outbox_id"])
        fencing_token = int(claimed_row["fencing_token"])
        try:
            async with self._account_guard(account_id):
                return await self._compile_claimed_guarded(claimed_row, pool)
        except AccountWriteRejectedError as exc:
            await self._fail_outbox(
                account_id,
                outbox_id,
                fencing_token=fencing_token,
                attempts=int(claimed_row["attempts"]),
                max_attempts=int(claimed_row["max_attempts"]),
                error_code=type(exc).__name__,
            )
            return "failed"

    async def _compile_claimed_guarded(
        self,
        claimed_row: asyncpg.Record,
        pool: asyncpg.Pool,
    ) -> str:
        account_id = str(claimed_row["account_id"])
        event_id = str(claimed_row["event_id"])
        outbox_id = cast(uuid.UUID, claimed_row["outbox_id"])
        fencing_token = int(claimed_row["fencing_token"])
        attempts = int(claimed_row["attempts"])
        max_attempts = int(claimed_row["max_attempts"])
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            receipt = await connection.fetchval(
                "SELECT outcome FROM memory_compile_receipts WHERE event_id = $1",
                event_id,
            )
            row = await connection.fetchrow(
                "SELECT * FROM archive_evidence_events WHERE event_id = $1",
                event_id,
            )
        if row is None:
            await self._fail_outbox(
                account_id,
                outbox_id,
                fencing_token=fencing_token,
                attempts=attempts,
                max_attempts=max_attempts,
                error_code="EvidenceNotFound",
            )
            return "failed"
        event = self._event_from_row(row)
        try:
            await self._project_capture_evidence(event)
        except Exception as exc:
            await self._fail_outbox(
                account_id,
                outbox_id,
                fencing_token=fencing_token,
                attempts=attempts,
                max_attempts=max_attempts,
                error_code=type(exc).__name__,
            )
            return "failed"
        if receipt is not None:
            async with pool.acquire() as connection, connection.transaction():
                await self._scope(connection, account_id)
                await self._complete_outbox(
                    connection,
                    outbox_id,
                    fencing_token=fencing_token,
                )
            return "skipped"
        if event.event_type == "memory.claim_reviewed" and (
            event.source == "user.memory_review" or is_policy_confirmation_event(event)
        ):
            try:
                async with pool.acquire() as connection, connection.transaction():
                    await self._scope(connection, account_id)
                    await self._replay_review_event(connection, event)
                    await self._record_receipt(connection, event, outcome="compiled")
                    await self._complete_outbox(
                        connection,
                        outbox_id,
                        fencing_token=fencing_token,
                    )
                return "compiled"
            except Exception as exc:
                await self._fail_outbox(
                    account_id,
                    outbox_id,
                    fencing_token=fencing_token,
                    attempts=attempts,
                    max_attempts=max_attempts,
                    error_code=type(exc).__name__,
                )
                return "failed"
        if not contribution_for(event).accepted:
            return await self._ignore(pool, event, outbox_id, fencing_token=fencing_token)
        try:
            gate = await extraction_gate(self._evidence_subject_category_resolver, event)
            if isinstance(gate, Withhold):
                return await self._ignore(
                    pool, event, outbox_id, fencing_token=fencing_token, reason=gate.ignored_reason
                )
            extraction = await self._extractor.extract(event)
            if not isinstance(extraction, MemoryExtraction):
                raise TypeError("memory extractor returned an invalid result")
            extraction = filter_extraction_for_subject(
                event,
                extraction,
                subject_category=gate.subject_category,
            )
            async with pool.acquire() as connection, connection.transaction():
                await self._scope(connection, account_id)
                decision = self._memory_write_policy.decide(
                    event,
                    extraction,
                    existing_values=await self._existing_single_value_claims(
                        connection,
                        event=event,
                        extraction=extraction,
                    ),
                    subject_category=gate.subject_category,
                )
                await self._write_extraction(connection, event, extraction)
                await self._apply_memory_write_decision(
                    connection,
                    event=event,
                    extraction=extraction,
                    decision=decision,
                )
                await self._record_receipt(
                    connection,
                    event,
                    outcome="compiled",
                    extractor_version=extraction.extractor_version,
                )
                await self._complete_outbox(
                    connection,
                    outbox_id,
                    fencing_token=fencing_token,
                )
            return "compiled"
        except Exception as exc:
            await self._fail_outbox(
                account_id,
                outbox_id,
                fencing_token=fencing_token,
                attempts=attempts,
                max_attempts=max_attempts,
                error_code=type(exc).__name__,
            )
            if self._fail_on_embedding_error and isinstance(exc, MemoryEmbeddingUnavailableError):
                raise
            return "failed"

    async def _ignore(
        self,
        pool: asyncpg.Pool,
        event: EvidenceEvent,
        outbox_id: uuid.UUID,
        *,
        fencing_token: int,
        reason: IgnoredReason | None = None,
    ) -> str:
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, event.account_id)
            await self._record_receipt(connection, event, outcome="ignored", ignored_reason=reason)
            await self._complete_outbox(connection, outbox_id, fencing_token=fencing_token)
        return "ignored"

    async def _project_capture_evidence(self, event: EvidenceEvent) -> None:
        candidate = event.payload.get("memory_capture_candidate_v1")
        if candidate is None:
            return
        if not isinstance(candidate, dict):
            raise ValueError("memory capture candidate must be an object")
        projector = self._capture_evidence_projector
        if projector is None:
            raise RuntimeError("memory capture evidence projector is unavailable")
        # False is an explicit stale/forged candidate decision.  The Archive
        # event is still valid evidence and its normal projections continue;
        # only the durable Memory capture authority is withheld.
        await projector(event)

    def _retry_delay_s(self, outbox_id: uuid.UUID, attempts: int) -> float:
        exponent = min(max(attempts - 1, 0), 20)
        base = min(
            self._outbox_retry_max_s,
            self._outbox_retry_base_s * (2**exponent),
        )
        digest = sha256(f"{outbox_id}:{attempts}".encode()).digest()
        jitter = 0.75 + (int.from_bytes(digest[:2], "big") / 65535) * 0.5
        # Keep the adapter boundary concrete: asyncpg/third-party stubs can
        # otherwise widen ``min`` to Any even though all configured values
        # are validated floats at construction time.
        return float(min(self._outbox_retry_max_s, base * jitter))

    async def _fail_outbox(
        self,
        account_id: str,
        outbox_id: uuid.UUID,
        *,
        fencing_token: int,
        attempts: int,
        max_attempts: int,
        error_code: str,
    ) -> None:
        pool = await self._ready_pool()
        dead_letter = attempts >= max_attempts
        retry_delay_s = self._retry_delay_s(outbox_id, attempts)
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await connection.execute(
                """
                UPDATE archive_processing_outbox
                SET status = CASE WHEN $4 THEN 'dead' ELSE 'failed' END,
                    available_at = CASE
                        WHEN $4 THEN available_at
                        ELSE now() + ($5 * interval '1 second')
                    END,
                    locked_until = NULL,
                    worker_id = NULL,
                    completed_at = NULL,
                    dead_lettered_at = CASE WHEN $4 THEN now() ELSE NULL END,
                    last_error_code = $1
                WHERE outbox_id = $2
                  AND status = 'processing'
                  AND fencing_token = $3
                """,
                error_code,
                outbox_id,
                fencing_token,
                dead_letter,
                retry_delay_s,
            )

    async def _record_receipt(
        self,
        connection: asyncpg.Connection,
        event: EvidenceEvent,
        *,
        outcome: Literal["compiled", "ignored"],
        extractor_version: str = "",
        ignored_reason: IgnoredReason | None = None,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO memory_compile_receipts (
                event_id, account_id, extractor_version, outcome, ignored_reason
            ) VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT DO NOTHING
            """,
            event.event_id,
            event.account_id,
            extractor_version or self._extractor.version,
            outcome,
            ignored_reason,
        )

    @staticmethod
    async def _complete_outbox(
        connection: asyncpg.Connection,
        outbox_id: uuid.UUID,
        *,
        fencing_token: int,
    ) -> None:
        updated = await connection.fetchval(
            """
            UPDATE archive_processing_outbox
            SET status = 'completed',
                locked_until = NULL,
                worker_id = NULL,
                completed_at = now(),
                dead_lettered_at = NULL,
                last_error_code = NULL
            WHERE outbox_id = $1
              AND status = 'processing'
              AND fencing_token = $2
            RETURNING 1
            """,
            outbox_id,
            fencing_token,
        )
        if updated != 1:
            raise OutboxLeaseLostError("memory compiler outbox lease was lost")

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
            subject_id=cast(str | None, row["subject_id"]),
            speaker_identity_id=row["speaker_identity_id"],
            consent_grant_id=row["consent_grant_id"],
            schema_version=int(row["schema_version"]),
            supersedes_event_id=row["supersedes_event_id"],
        )

    async def close(self) -> None:
        if self._vector_backfill is not None:
            await self._vector_backfill.close()
            self._vector_backfill = None
        if self._compiler_pool is not None:
            await self._compiler_pool.close()
            self._compiler_pool = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None