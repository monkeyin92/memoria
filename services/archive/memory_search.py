"""PostgreSQL memory search mixin for the memory catalog.

``PostgresMemorySearchMixin`` is composed into ``PostgresMemoryCatalog``. The
catalog owns the pool, the RLS scope setter and the claim-vector backfill; this
module owns memory search and its row projection.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

import asyncpg

from services.archive import memory_lexical_sql as lexical_sql
from services.archive.memory_domain import (
    ConflictState,
    DomainCategory,
    MemoryCategory,
    MemoryEmbeddingUnavailableError,
    MemoryKind,
    MemorySearchItem,
    MemorySearchQuery,
    MemorySearchResult,
    MemorySensitivity,
    MemoryStatus,
    subject_lineage_predicates,
)
from services.archive.memory_lexical_sql import search_terms
from services.archive.memory_sql import _bind_dollar
from services.common.redaction import redact_pii

if TYPE_CHECKING:
    from services.archive.memory_domain import MemoryEmbedder


class PostgresMemorySearchMixin:
    """Search the memory catalog and project its rows."""

    if TYPE_CHECKING:
        # Shared state owned by PostgresMemoryCatalog.
        _embedder: MemoryEmbedder | None
        _fail_on_embedding_error: bool
        _vector_enabled: bool

        # Core catalog members consumed by this mixin.
        async def _ready_pool(self) -> asyncpg.Pool: ...

        @staticmethod
        async def _scope(connection: asyncpg.Connection, account_id: str) -> None: ...

        def _schedule_claim_vector_backfill(
            self, *, account_id: str, items: tuple[MemorySearchItem, ...]
        ) -> None: ...

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
            clauses.append("document.conflict_state != 'active'")
        else:
            clauses.append("document.status != 'retracted'")
        metadata_score = "(0.35 * document.stability + 0.65 * document.salience)"
        score_expression = f"{metadata_score}::double precision"
        semantic_cte = ""
        semantic_join = ""
        embedder = self._embedder if self._vector_enabled else None
        if query.text.strip():
            embedding: tuple[float, ...] | None = None
            if embedder is not None:
                try:
                    embedding = await embedder.embed(redact_pii(query.text.strip()))
                except MemoryEmbeddingUnavailableError:
                    if self._fail_on_embedding_error:
                        raise
            parameters.append(query.text.strip())
            text_index = len(parameters)

            def like(value: str) -> str:
                parameters.append("%" + re.sub(r"([\\%_])", r"\\\1", value) + "%")
                i = len(parameters)
                return (
                    f"document.title ILIKE ${i} ESCAPE '\\' OR document.body ILIKE ${i} ESCAPE '\\'"
                )

            whole = like(query.text.strip())
            sparse = [
                like(term)
                for term in search_terms(query.text, with_embedding=embedding is not None)
            ]
            text_match = lexical_sql.text_match(text_index, whole, sparse)
            if embedding is None or embedder is None:
                # No embeddings: rank by content terms hit (ts_rank ties Chinese).
                hits = lexical_sql.hit_count(sparse)
                clauses.append(text_match)
                score_expression = f"(({hits or '0'}) + 0.1 * {metadata_score})::double precision"
            else:
                text_score = lexical_sql.text_score(text_index, whole, sparse)
                parameters.append(embedder.model)
                model_index = len(parameters)
                parameters.append(embedder.dimensions)
                dimensions_index = len(parameters)
                parameters.append(json.dumps(embedding, separators=(",", ":")))
                vector_index = len(parameters)
                parameters.append(min(800, max(50, query.limit * 8)))
                semantic_limit_index = len(parameters)
                dimensions = embedder.dimensions
                semantic_cte = (
                    "WITH semantic_candidates AS ("
                    "SELECT item_id, GREATEST("
                    f"1.0 - (embedding::vector({dimensions}) <=> "
                    f"${vector_index}::vector({dimensions})), 0.0"
                    ") AS semantic_score "
                    "FROM memory_vector_documents "
                    "WHERE account_id = $1 "
                    f"AND embedding_model = ${model_index} "
                    f"AND embedding_dimensions = ${dimensions_index} "
                    f"ORDER BY embedding::vector({dimensions}) <=> "
                    f"${vector_index}::vector({dimensions}) "
                    f"LIMIT ${semantic_limit_index}"
                    ")"
                )
                semantic_join = (
                    "LEFT JOIN semantic_candidates semantic ON semantic.item_id = document.item_id "
                )
                clauses.append(f"({text_match} OR semantic.item_id IS NOT NULL)")
                score_expression = (
                    f"(0.35 * {text_score} + "
                    "0.45 * COALESCE(semantic.semantic_score, 0.0) + "
                    f"0.2 * {metadata_score})"
                    "::double precision"
                )
        if query.kinds:
            parameters.append(list(query.kinds))
            clauses.append(f"document.kind = ANY(${len(parameters)}::text[])")
        if query.memory_kinds:
            parameters.append(list(query.memory_kinds))
            clauses.append(f"document.memory_kind = ANY(${len(parameters)}::text[])")
        if query.domain_categories:
            parameters.append(list(query.domain_categories))
            clauses.append(f"document.domain_category = ANY(${len(parameters)}::text[])")
        if query.entity_ids:
            try:
                entity_ids = [uuid.UUID(value) for value in query.entity_ids]
            except ValueError as exc:
                raise ValueError("memory search entity_ids must be UUID values") from exc
            parameters.append(entity_ids)
            clauses.append(f"document.entity_ids && ${len(parameters)}::uuid[]")
        if query.valid_at is not None:
            parameters.append(query.valid_at)
            valid_index = len(parameters)
            clauses.append(
                f"(document.valid_from IS NULL OR document.valid_from <= ${valid_index}) "
                f"AND (document.valid_to IS NULL OR document.valid_to >= ${valid_index})"
            )
        if query.sensitivities:
            parameters.append(list(query.sensitivities))
            clauses.append(f"document.sensitivity = ANY(${len(parameters)}::text[])")
        if query.conflict_states:
            parameters.append(list(query.conflict_states))
            clauses.append(f"document.conflict_state = ANY(${len(parameters)}::text[])")
        if query.occurred_after is not None:
            parameters.append(query.occurred_after)
            clauses.append(f"document.occurred_at >= ${len(parameters)}")
        if query.occurred_before is not None:
            parameters.append(query.occurred_before)
            clauses.append(f"document.occurred_at <= ${len(parameters)}")
        if query.subject_id is not None:
            # Explicit scope: the document's own evidence plus every source it
            # was merged from must resolve to this subject.
            clauses.extend(
                subject_lineage_predicates(
                    bind=_bind_dollar(parameters),
                    subject_id=query.subject_id,
                    account_column="document.account_id",
                    single_source_column="document.source_event_id",
                    evidence_table="archive_evidence_events",
                    merged_source_link=(
                        "memory_search_document_sources",
                        "document_id",
                        "document.document_id",
                    ),
                )
            )
        parameters.append(query.limit)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, query.account_id)
            rows = await connection.fetch(
                f"""
                {semantic_cte}
                SELECT document.*, claim.value AS claim_value,
                       {score_expression} AS score,
                       ARRAY(
                           SELECT source.source_event_id
                           FROM memory_search_document_sources source
                           WHERE source.document_id = document.document_id
                           ORDER BY source.source_event_id
                       ) AS source_event_ids
                FROM memory_search_documents document
                LEFT JOIN memory_claims claim
                  ON document.kind = 'claim'
                 AND claim.claim_id = document.item_id
                 AND claim.account_id = document.account_id
                {semantic_join}
                WHERE {" AND ".join(clauses)}
                ORDER BY CASE document.status WHEN 'confirmed' THEN 0 ELSE 1 END,
                         score DESC, document.observed_at DESC, document.document_id
                LIMIT ${len(parameters)}
                """,
                *parameters,
            )
        items = tuple(self._search_item(row) for row in rows)
        self._schedule_claim_vector_backfill(account_id=query.account_id, items=items)
        return MemorySearchResult(items=items)


    @staticmethod
    def _search_item(row: asyncpg.Record) -> MemorySearchItem:
        claim_value = row["claim_value"]
        body = str(row["body"]).split("\n[retrieval-context]", 1)[0]
        return MemorySearchItem(
            item_id=str(row["item_id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            snippet=(
                str(claim_value)
                if str(row["kind"]) == "claim" and claim_value is not None
                else body
            ),
            category=cast(MemoryCategory, row["category"]),
            status=cast(MemoryStatus, row["status"]),
            source_event_id=str(row["source_event_id"]),
            occurred_at=cast(datetime, row["occurred_at"]),
            score=float(row["score"]),
            memory_kind=cast(MemoryKind, row["memory_kind"]),
            domain_category=cast(DomainCategory, row["domain_category"]),
            entity_ids=tuple(str(value) for value in row["entity_ids"]),
            source_event_ids=tuple(str(value) for value in row["source_event_ids"]),
            valid_from=cast(datetime | None, row["valid_from"]),
            valid_to=cast(datetime | None, row["valid_to"]),
            observed_at=cast(datetime, row["observed_at"]),
            stability=float(row["stability"]),
            salience=float(row["salience"]),
            sensitivity=cast(MemorySensitivity, row["sensitivity"]),
            conflict_state=cast(ConflictState, row["conflict_state"]),
        )
