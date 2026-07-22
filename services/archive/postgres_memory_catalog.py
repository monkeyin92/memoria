"""PostgreSQL/RLS implementation of the rebuildable long-term memory catalog."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import asyncpg
import httpx

from services.archive.domain import EvidenceEvent, EvidenceNotFoundError
from services.archive.memory_domain import (
    AccountWriteGuard,
    AccountWriteRejectedError,
    CompileReport,
    MemoryCategory,
    MemoryClaimReview,
    MemoryEmbedder,
    MemoryEmbeddingUnavailableError,
    MemoryExtraction,
    MemoryExtractor,
    MemorySearchItem,
    MemorySearchQuery,
    MemorySearchResult,
    MemoryStatus,
    PersonItem,
    ReviewedClaim,
    ReviewQueueItem,
    TimelineItem,
)
from services.common.evidence_policy import contribution_for

_SINGLE_VALUE_PREDICATES = frozenset({"age", "birth_date", "birth_place"})
_REVIEW_STATUS: dict[str, MemoryStatus] = {
    "confirm": "confirmed",
    "dispute": "disputed",
    "retract": "retracted",
    "correct": "confirmed",
}
_PROJECTION_REVIEW_STATUS: dict[str, MemoryStatus] = {
    **_REVIEW_STATUS,
    "correct": "retracted",
}
_SOURCE_STATUS_TABLES = (
    "person_entities",
    "person_aliases",
    "relationships",
    "life_episodes",
    "timeline_entries",
    "knowledge_items",
)


@asynccontextmanager
async def _allow_account_write(_: str) -> AsyncIterator[None]:
    yield


def _stable_uuid(kind: str, *values: object) -> uuid.UUID:
    key = ":".join(str(value) for value in values)
    return uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:{kind}:{key}")


class QwenMemoryEmbedder:
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str,
        timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("memory embedding endpoint must use HTTP(S)")
        if not api_key or not model.strip() or timeout_s <= 0:
            raise ValueError("memory embedding API key, model and timeout are required")
        self._endpoint = endpoint
        self._api_key = api_key
        self.model = model.strip()
        self._timeout_s = timeout_s
        self._transport = transport

    async def embed(self, text: str) -> tuple[float, ...]:
        if not text.strip():
            raise ValueError("memory embedding text must not be blank")
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self.model, "input": text, "encoding_format": "float"},
                )
                response.raise_for_status()
                values = response.json()["data"][0]["embedding"]
            embedding = tuple(float(value) for value in values)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise MemoryEmbeddingUnavailableError(
                "memory embedding service returned an invalid response"
            ) from exc
        if not embedding or any(not math.isfinite(value) for value in embedding):
            raise MemoryEmbeddingUnavailableError(
                "memory embedding service returned an invalid vector"
            )
        return embedding


class PostgresMemoryCatalog:
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
    ) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("memory catalog DSN must use PostgreSQL")
        if compiler_dsn is not None and not compiler_dsn.startswith(
            ("postgresql://", "postgres://")
        ):
            raise ValueError("memory compiler DSN must use PostgreSQL")
        if compiler_role is not None and not compiler_role.strip():
            raise ValueError("memory compiler role must not be blank")
        self._dsn = dsn
        self._compiler_dsn = compiler_dsn
        self._compiler_role = compiler_role
        self._extractor = extractor
        self._account_guard = account_guard or _allow_account_write
        self._embedder = embedder
        self._require_vector = require_vector
        self._vector_enabled = False
        self._pool: asyncpg.Pool | None = None
        self._compiler_pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10, command_timeout=15)
        if pool is None:  # pragma: no cover
            raise RuntimeError("failed to create PostgreSQL memory pool")
        archive_schema = Path(__file__).with_name("postgres_schema.sql").read_text(encoding="utf-8")
        memory_schema = (
            Path(__file__).with_name("postgres_memory_schema.sql").read_text(encoding="utf-8")
        )
        async with pool.acquire() as connection:
            await connection.execute(archive_schema)
            await connection.execute(memory_schema)
            self._vector_enabled = bool(
                await connection.fetchval(
                    "SELECT to_regclass('memory_vector_documents') IS NOT NULL"
                )
            )
        if self._require_vector and (not self._vector_enabled or self._embedder is None):
            await pool.close()
            raise RuntimeError("production memory catalog requires pgvector and an embedder")
        self._pool = pool
        if self._compiler_dsn is not None:
            compiler_pool = await asyncpg.create_pool(
                self._compiler_dsn,
                min_size=1,
                max_size=2,
                command_timeout=15,
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
                      AND status IN ('pending', 'processing', 'failed')
                      AND available_at <= now()
                    ORDER BY created_at, outbox_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                UPDATE archive_processing_outbox outbox
                SET status = 'processing', attempts = outbox.attempts + 1,
                    completed_at = NULL, last_error_code = NULL
                FROM next_items
                WHERE outbox.outbox_id = next_items.outbox_id
                RETURNING outbox.outbox_id, outbox.event_id, outbox.account_id
                """,
                limit,
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
        try:
            async with self._account_guard(account_id):
                return await self._compile_claimed_guarded(claimed_row, pool)
        except AccountWriteRejectedError as exc:
            await self._fail_outbox(account_id, outbox_id, type(exc).__name__)
            return "failed"

    async def _compile_claimed_guarded(
        self,
        claimed_row: asyncpg.Record,
        pool: asyncpg.Pool,
    ) -> str:
        account_id = str(claimed_row["account_id"])
        event_id = str(claimed_row["event_id"])
        outbox_id = cast(uuid.UUID, claimed_row["outbox_id"])
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            receipt = await connection.fetchval(
                "SELECT outcome FROM memory_compile_receipts WHERE event_id = $1",
                event_id,
            )
            if receipt is not None:
                await self._complete_outbox(connection, outbox_id)
                return "skipped"
            row = await connection.fetchrow(
                "SELECT * FROM archive_evidence_events WHERE event_id = $1",
                event_id,
            )
        if row is None:
            await self._fail_outbox(account_id, outbox_id, "EvidenceNotFound")
            return "failed"
        event = self._event_from_row(row)
        if event.event_type == "memory.claim_reviewed" and event.source == "user.memory_review":
            try:
                async with pool.acquire() as connection, connection.transaction():
                    await self._scope(connection, account_id)
                    await self._replay_review_event(connection, event)
                    await self._record_receipt(connection, event, outcome="compiled")
                    await self._complete_outbox(connection, outbox_id)
                return "compiled"
            except Exception as exc:
                await self._fail_outbox(account_id, outbox_id, type(exc).__name__)
                return "failed"
        contribution = contribution_for(event)
        if not contribution.accepted:
            async with pool.acquire() as connection, connection.transaction():
                await self._scope(connection, account_id)
                await self._record_receipt(connection, event, outcome="ignored")
                await self._complete_outbox(connection, outbox_id)
            return "ignored"
        try:
            extraction = await self._extractor.extract(event)
            if not isinstance(extraction, MemoryExtraction):
                raise TypeError("memory extractor returned an invalid result")
            async with pool.acquire() as connection, connection.transaction():
                await self._scope(connection, account_id)
                await self._write_extraction(connection, event, extraction)
                await self._record_receipt(
                    connection,
                    event,
                    outcome="compiled",
                    extractor_version=extraction.extractor_version,
                )
                await self._complete_outbox(connection, outbox_id)
            return "compiled"
        except Exception as exc:
            await self._fail_outbox(account_id, outbox_id, type(exc).__name__)
            return "failed"

    async def _fail_outbox(
        self,
        account_id: str,
        outbox_id: uuid.UUID,
        error_code: str,
    ) -> None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await connection.execute(
                """
                UPDATE archive_processing_outbox
                SET status = 'failed', completed_at = NULL, last_error_code = $1
                WHERE outbox_id = $2
                """,
                error_code,
                outbox_id,
            )

    async def _write_extraction(
        self,
        connection: asyncpg.Connection,
        event: EvidenceEvent,
        extraction: MemoryExtraction,
    ) -> None:
        person_ids: dict[str, uuid.UUID] = {}
        for person in extraction.people:
            person_id = _stable_uuid("person", event.account_id, person.canonical_key)
            person_ids[person.canonical_key] = person_id
            await connection.execute(
                """
                INSERT INTO person_entities (
                    person_id, account_id, canonical_key, display_name,
                    relationship_to_owner, source_event_id, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (account_id, canonical_key) DO NOTHING
                """,
                person_id,
                event.account_id,
                person.canonical_key,
                person.display_name,
                person.relationship_to_owner,
                event.event_id,
                event.occurred_at,
            )
            for alias in person.aliases:
                await connection.execute(
                    """
                    INSERT INTO person_aliases (
                        person_id, account_id, alias, source_event_id
                    ) VALUES ($1, $2, $3, $4)
                    ON CONFLICT DO NOTHING
                    """,
                    person_id,
                    event.account_id,
                    alias,
                    event.event_id,
                )

        for index, relationship in enumerate(extraction.relationships):
            person_id = person_ids.get(relationship.person_key) or _stable_uuid(
                "person", event.account_id, relationship.person_key
            )
            await connection.execute(
                """
                INSERT INTO relationships (
                    relationship_id, account_id, person_id, relationship_type,
                    source_event_id, valid_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT DO NOTHING
                """,
                _stable_uuid("relationship", event.event_id, index),
                event.account_id,
                person_id,
                relationship.relationship_type,
                event.event_id,
                event.occurred_at,
            )

        for index, claim in enumerate(extraction.claims):
            claim_id = _stable_uuid("claim", event.event_id, index)
            await connection.execute(
                """
                INSERT INTO memory_claims (
                    claim_id, account_id, category, subject_key, predicate,
                    value, confidence, sensitive_domain, extractor_version,
                    source_event_id, valid_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT DO NOTHING
                """,
                claim_id,
                event.account_id,
                claim.category,
                claim.subject_key,
                claim.predicate,
                claim.value,
                    min(1.0, claim.confidence * contribution_for(event).factor),
                claim.sensitive_domain,
                extraction.extractor_version or self._extractor.version,
                event.event_id,
                event.occurred_at,
            )
            await self._insert_search_document(
                connection,
                account_id=event.account_id,
                item_id=claim_id,
                kind="claim",
                title=claim.value[:80],
                body=claim.value,
                category=claim.category,
                source_event_id=event.event_id,
                occurred_at=event.occurred_at,
            )

        for index, timeline in enumerate(extraction.timeline):
            episode_id = _stable_uuid(
                "episode",
                event.account_id,
                event.session_id or event.event_id,
                timeline.category,
                timeline.event_start.date(),
                index,
            )
            timeline_id = _stable_uuid("timeline", event.event_id, index)
            await connection.execute(
                """
                INSERT INTO life_episodes (
                    episode_id, account_id, title, category, event_start,
                    event_end, source_event_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT DO NOTHING
                """,
                episode_id,
                event.account_id,
                timeline.title,
                timeline.category,
                timeline.event_start,
                timeline.event_end,
                event.event_id,
            )
            await connection.execute(
                """
                INSERT INTO episode_evidence (episode_id, account_id, source_event_id)
                VALUES ($1, $2, $3)
                ON CONFLICT DO NOTHING
                """,
                episode_id,
                event.account_id,
                event.event_id,
            )
            await connection.execute(
                """
                INSERT INTO timeline_entries (
                    timeline_id, account_id, episode_id, title, category,
                    event_start, event_end, time_precision, source_event_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT DO NOTHING
                """,
                timeline_id,
                event.account_id,
                episode_id,
                timeline.title,
                timeline.category,
                timeline.event_start,
                timeline.event_end,
                timeline.time_precision,
                event.event_id,
            )
            await self._insert_search_document(
                connection,
                account_id=event.account_id,
                item_id=timeline_id,
                kind="timeline",
                title=timeline.title,
                body=timeline.title,
                category=timeline.category,
                source_event_id=event.event_id,
                occurred_at=timeline.event_start,
            )

        for index, knowledge in enumerate(extraction.knowledge):
            knowledge_id = _stable_uuid("knowledge", event.event_id, index)
            await connection.execute(
                """
                INSERT INTO knowledge_items (
                    knowledge_id, account_id, category, question, answer,
                    applicability, counterexample, source_event_id, occurred_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT DO NOTHING
                """,
                knowledge_id,
                event.account_id,
                knowledge.category,
                knowledge.question,
                knowledge.answer,
                knowledge.applicability,
                knowledge.counterexample,
                event.event_id,
                event.occurred_at,
            )
            body = " ".join(
                part
                for part in (
                    knowledge.answer,
                    knowledge.applicability,
                    knowledge.counterexample,
                )
                if part
            )
            await self._insert_search_document(
                connection,
                account_id=event.account_id,
                item_id=knowledge_id,
                kind="knowledge",
                title=knowledge.question,
                body=body,
                category=knowledge.category,
                source_event_id=event.event_id,
                occurred_at=event.occurred_at,
            )

    async def _insert_search_document(
        self,
        connection: asyncpg.Connection,
        *,
        account_id: str,
        item_id: uuid.UUID,
        kind: str,
        title: str,
        body: str,
        category: MemoryCategory,
        source_event_id: str,
        occurred_at: datetime,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO memory_search_documents (
                document_id, account_id, item_id, kind, title, body,
                category, status, source_event_id, occurred_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'candidate', $8, $9)
            ON CONFLICT DO NOTHING
            """,
            _stable_uuid("search", kind, item_id),
            account_id,
            item_id,
            kind,
            title,
            body,
            category,
            source_event_id,
            occurred_at,
        )
        if self._vector_enabled and self._embedder is not None:
            try:
                embedding = await self._embedder.embed(
                    " ".join(part for part in (title, body) if part)
                )
            except MemoryEmbeddingUnavailableError:
                await connection.execute(
                    """
                    DELETE FROM memory_vector_documents
                    WHERE item_id = $1 AND account_id = $2
                    """,
                    item_id,
                    account_id,
                )
                return
            vector = json.dumps(embedding, separators=(",", ":"))
            await connection.execute(
                """
                INSERT INTO memory_vector_documents (
                    item_id, account_id, embedding_model, embedding, source_event_id
                ) VALUES ($1, $2, $3, $4::vector, $5)
                ON CONFLICT (item_id) DO UPDATE
                SET embedding_model = EXCLUDED.embedding_model,
                    embedding = EXCLUDED.embedding,
                    source_event_id = EXCLUDED.source_event_id,
                    created_at = now()
                """,
                item_id,
                account_id,
                self._embedder.model,
                vector,
                source_event_id,
            )

    async def _record_receipt(
        self,
        connection: asyncpg.Connection,
        event: EvidenceEvent,
        *,
        outcome: str,
        extractor_version: str = "",
    ) -> None:
        await connection.execute(
            """
            INSERT INTO memory_compile_receipts (
                event_id, account_id, extractor_version, outcome
            ) VALUES ($1, $2, $3, $4)
            ON CONFLICT DO NOTHING
            """,
            event.event_id,
            event.account_id,
            extractor_version or self._extractor.version,
            outcome,
        )

    @staticmethod
    async def _complete_outbox(
        connection: asyncpg.Connection,
        outbox_id: uuid.UUID,
    ) -> None:
        await connection.execute(
            """
            UPDATE archive_processing_outbox
            SET status = 'completed', completed_at = now(), last_error_code = NULL
            WHERE outbox_id = $1
            """,
            outbox_id,
        )

    async def search(self, query: MemorySearchQuery) -> MemorySearchResult:
        return await self._search(query, confirmed_only=False)

    async def context(self, query: MemorySearchQuery) -> MemorySearchResult:
        return await self._search(query, confirmed_only=True)

    async def _search(
        self,
        query: MemorySearchQuery,
        *,
        confirmed_only: bool,
    ) -> MemorySearchResult:
        if query.speaker_class != "owner":
            return MemorySearchResult()
        clauses = ["document.account_id = $1"]
        parameters: list[Any] = [query.account_id]
        if confirmed_only or not query.include_candidates:
            clauses.append("document.status = 'confirmed'")
        else:
            clauses.append("document.status != 'retracted'")
        score_expression = "1.0::double precision"
        semantic_join = ""
        if query.text.strip():
            parameters.append(query.text.strip())
            text_index = len(parameters)
            escaped = (
                query.text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            parameters.append(f"%{escaped}%")
            like_index = len(parameters)
            text_match = (
                "(document.search_vector @@ websearch_to_tsquery('simple', "
                f"${text_index}) OR document.title ILIKE ${like_index} ESCAPE '\\' "
                f"OR document.body ILIKE ${like_index} ESCAPE '\\')"
            )
            text_score = (
                "GREATEST(ts_rank(document.search_vector, "
                f"websearch_to_tsquery('simple', ${text_index})), "
                f"CASE WHEN {text_match} THEN 0.5 ELSE 0.0 END)"
            )
            if self._vector_enabled and self._embedder is not None:
                try:
                    embedding = await self._embedder.embed(query.text.strip())
                except MemoryEmbeddingUnavailableError:
                    clauses.append(text_match)
                    score_expression = f"{text_score}::double precision"
                else:
                    parameters.append(self._embedder.model)
                    model_index = len(parameters)
                    parameters.append(json.dumps(embedding, separators=(",", ":")))
                    vector_index = len(parameters)
                    semantic_join = (
                        "LEFT JOIN memory_vector_documents semantic "
                        "ON semantic.item_id = document.item_id "
                        "AND semantic.account_id = document.account_id "
                        f"AND semantic.embedding_model = ${model_index}"
                    )
                    score_expression = (
                        f"(0.45 * {text_score} + 0.55 * GREATEST("
                        f"1.0 - (semantic.embedding <=> ${vector_index}::vector), 0.0))"
                        "::double precision"
                    )
            else:
                clauses.append(text_match)
                score_expression = f"{text_score}::double precision"
        if query.kinds:
            parameters.append(list(query.kinds))
            clauses.append(f"document.kind = ANY(${len(parameters)}::text[])")
        if query.categories:
            parameters.append(list(query.categories))
            clauses.append(f"document.category = ANY(${len(parameters)}::text[])")
        if query.occurred_after is not None:
            parameters.append(query.occurred_after)
            clauses.append(f"document.occurred_at >= ${len(parameters)}")
        if query.occurred_before is not None:
            parameters.append(query.occurred_before)
            clauses.append(f"document.occurred_at <= ${len(parameters)}")
        parameters.append(query.limit)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, query.account_id)
            rows = await connection.fetch(
                f"""
                SELECT document.*, {score_expression} AS score
                FROM memory_search_documents document
                {semantic_join}
                WHERE {" AND ".join(clauses)}
                ORDER BY CASE document.status WHEN 'confirmed' THEN 0 ELSE 1 END,
                         score DESC, document.occurred_at DESC, document.document_id
                LIMIT ${len(parameters)}
                """,
                *parameters,
            )
        return MemorySearchResult(items=tuple(self._search_item(row) for row in rows))

    async def timeline(self, *, account_id: str, limit: int = 50) -> tuple[TimelineItem, ...]:
        if not account_id.strip() or not 1 <= limit <= 100:
            raise ValueError("timeline requires account_id and limit 1..100")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT * FROM timeline_entries
                WHERE account_id = $1 AND status != 'retracted'
                ORDER BY event_start DESC, timeline_id
                LIMIT $2
                """,
                account_id,
                limit,
            )
        return tuple(
            TimelineItem(
                timeline_id=str(row["timeline_id"]),
                title=str(row["title"]),
                category=cast(MemoryCategory, row["category"]),
                status=cast(MemoryStatus, row["status"]),
                event_start=cast(datetime, row["event_start"]),
                event_end=cast(datetime | None, row["event_end"]),
                time_precision=str(row["time_precision"]),
                source_event_id=str(row["source_event_id"]),
                episode_id=str(row["episode_id"]),
            )
            for row in rows
        )

    async def people(self, *, account_id: str, limit: int = 100) -> tuple[PersonItem, ...]:
        if not account_id.strip() or not 1 <= limit <= 100:
            raise ValueError("people requires account_id and limit 1..100")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT entity.*, coalesce(
                    array_agg(DISTINCT alias.alias ORDER BY alias.alias)
                        FILTER (WHERE alias.alias IS NOT NULL),
                    ARRAY[]::text[]
                ) AS aliases
                FROM person_entities entity
                LEFT JOIN person_aliases alias
                  ON alias.person_id = entity.person_id
                 AND alias.status != 'retracted'
                WHERE entity.account_id = $1 AND entity.status != 'retracted'
                GROUP BY entity.person_id
                ORDER BY entity.created_at, entity.person_id
                LIMIT $2
                """,
                account_id,
                limit,
            )
        return tuple(
            PersonItem(
                person_id=str(row["person_id"]),
                display_name=str(row["display_name"]),
                relationship_to_owner=str(row["relationship_to_owner"]),
                aliases=tuple(str(alias) for alias in row["aliases"]),
                status=cast(MemoryStatus, row["status"]),
                source_event_id=str(row["source_event_id"]),
            )
            for row in rows
        )

    async def review_queue(self, *, account_id: str) -> tuple[ReviewQueueItem, ...]:
        if not account_id.strip():
            raise ValueError("review queue requires account_id")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT claim.*,
                       EXISTS (
                           SELECT 1 FROM memory_claims other
                           WHERE other.account_id = claim.account_id
                             AND other.subject_key = claim.subject_key
                             AND other.predicate = claim.predicate
                             AND other.value != claim.value
                             AND other.status != 'retracted'
                       ) AS has_conflict
                FROM memory_claims claim
                WHERE claim.account_id = $1
                  AND claim.status IN ('candidate', 'disputed')
                ORDER BY claim.valid_at, claim.claim_id
                """,
                account_id,
            )
        return tuple(
            ReviewQueueItem(
                item_id=str(row["claim_id"]),
                kind="claim",
                category=cast(MemoryCategory, row["category"]),
                value=str(row["value"]),
                status=cast(MemoryStatus, row["status"]),
                reason=(
                    "conflicting_values"
                    if str(row["predicate"]) in _SINGLE_VALUE_PREDICATES
                    and bool(row["has_conflict"])
                    else "pending_confirmation"
                ),
                source_event_id=str(row["source_event_id"]),
            )
            for row in rows
        )

    async def review(self, command: MemoryClaimReview) -> ReviewedClaim:
        try:
            claim_id = uuid.UUID(command.claim_id)
        except ValueError as exc:
            raise EvidenceNotFoundError(command.claim_id) from exc
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, command.account_id)
            row = await connection.fetchrow(
                """
                SELECT * FROM memory_claims
                WHERE claim_id = $1 AND account_id = $2
                FOR UPDATE
                """,
                claim_id,
                command.account_id,
            )
            if row is None:
                raise EvidenceNotFoundError(command.claim_id)
            previous_value = str(row["value"])
            value = (command.corrected_value or previous_value).strip()
            status = _REVIEW_STATUS[command.action]
            review_event = EvidenceEvent(
                event_id=str(uuid.uuid4()),
                account_id=command.account_id,
                event_type="memory.claim_reviewed",
                occurred_at=datetime.now(UTC),
                speaker_class="system",
                source="user.memory_review",
                payload={
                    "target_id": command.claim_id,
                    "action": command.action,
                    "previous_value": previous_value,
                    **({"corrected_value": value} if command.action == "correct" else {}),
                },
            )
            await self._insert_evidence(connection, review_event)
            await self._write_review_projection(
                connection,
                account_id=command.account_id,
                claim_id=claim_id,
                status=status,
                projection_status=_PROJECTION_REVIEW_STATUS[command.action],
                title=value[:80],
                value=value,
                review_event_id=review_event.event_id,
            )
        return ReviewedClaim(
            claim_id=command.claim_id,
            status=status,
            value=value,
            review_event_id=review_event.event_id,
        )

    async def _replay_review_event(
        self,
        connection: asyncpg.Connection,
        event: EvidenceEvent,
    ) -> None:
        target_id = str(event.payload.get("target_id", ""))
        action = str(event.payload.get("action", ""))
        try:
            claim_id = uuid.UUID(target_id)
            status = _REVIEW_STATUS[action]
        except (KeyError, ValueError) as exc:
            raise ValueError("invalid memory review evidence") from exc
        row = await connection.fetchrow(
            """
            SELECT value FROM memory_claims
            WHERE claim_id = $1 AND account_id = $2
            FOR UPDATE
            """,
            claim_id,
            event.account_id,
        )
        if row is None:
            raise EvidenceNotFoundError(target_id)
        value = str(row["value"])
        if action == "correct":
            value = str(event.payload.get("corrected_value", "")).strip()
            if not value:
                raise ValueError("corrected memory value must not be blank")
        await self._write_review_projection(
            connection,
            account_id=event.account_id,
            claim_id=claim_id,
            status=status,
            projection_status=_PROJECTION_REVIEW_STATUS[action],
            title=value[:80],
            value=value,
            review_event_id=event.event_id,
        )

    @staticmethod
    async def _write_review_projection(
        connection: asyncpg.Connection,
        *,
        account_id: str,
        claim_id: uuid.UUID,
        status: MemoryStatus,
        projection_status: MemoryStatus,
        title: str,
        value: str,
        review_event_id: str,
    ) -> None:
        source_event_id = await connection.fetchval(
            """
            SELECT source_event_id FROM memory_claims
            WHERE claim_id = $1 AND account_id = $2
            """,
            claim_id,
            account_id,
        )
        if source_event_id is None:
            raise EvidenceNotFoundError(str(claim_id))
        await connection.execute(
            """
            UPDATE memory_claims
            SET status = $1, value = $2, review_event_id = $3
            WHERE claim_id = $4 AND account_id = $5
            """,
            status,
            value,
            review_event_id,
            claim_id,
            account_id,
        )
        for table in _SOURCE_STATUS_TABLES:
            await connection.execute(
                f"UPDATE {table} SET status = $1 WHERE account_id = $2 AND source_event_id = $3",
                projection_status,
                account_id,
                source_event_id,
            )
        await connection.execute(
            """
            UPDATE memory_search_documents
            SET status = $1
            WHERE kind != 'claim' AND account_id = $2 AND source_event_id = $3
            """,
            projection_status,
            account_id,
            source_event_id,
        )
        await connection.execute(
            """
            UPDATE memory_search_documents
            SET status = $1, title = $2, body = $3
            WHERE kind = 'claim' AND item_id = $4 AND account_id = $5
            """,
            status,
            title,
            value,
            claim_id,
            account_id,
        )

    @staticmethod
    async def _insert_evidence(
        connection: asyncpg.Connection,
        event: EvidenceEvent,
    ) -> None:
        await connection.execute(
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
        await connection.execute(
            """
            INSERT INTO archive_processing_outbox (
                outbox_id, account_id, event_id, task_type
            ) VALUES ($1, $2, $3, 'compile_evidence')
            """,
            _stable_uuid("outbox", event.event_id),
            event.account_id,
            event.event_id,
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

    @staticmethod
    def _search_item(row: asyncpg.Record) -> MemorySearchItem:
        return MemorySearchItem(
            item_id=str(row["item_id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            snippet=str(row["body"]),
            category=cast(MemoryCategory, row["category"]),
            status=cast(MemoryStatus, row["status"]),
            source_event_id=str(row["source_event_id"]),
            occurred_at=cast(datetime, row["occurred_at"]),
            score=float(row["score"]),
        )

    async def close(self) -> None:
        if self._compiler_pool is not None:
            await self._compiler_pool.close()
            self._compiler_pool = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
