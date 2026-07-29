from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import MemorySearchResult
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_benchmark import (
    PostgresMemoryBenchmarkReport,
    benchmark_postgres_memory_search,
    benchmark_report_json,
    explain_pgvector_ann,
)
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog


class BenchmarkEmbedder:
    model = "benchmark-semantic-v1"
    dimensions = 2

    async def embed(self, text: str) -> tuple[float, ...]:
        return (1.0, 0.0) if "城市" in text or "杭州" in text else (0.0, 1.0)


class EmptySearchCatalog:
    async def search(self, query: object) -> MemorySearchResult:
        del query
        return MemorySearchResult()


def _database_dsn(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    auth = quote(parsed.username or "postgres")
    if parsed.password is not None:
        auth = f"{auth}:{quote(parsed.password)}"
    return urlunsplit(
        (
            parsed.scheme,
            f"{auth}@{host}",
            f"/{database}",
            parsed.query,
            "",
        )
    )


def test_pgvector_benchmark_report_is_stable_json() -> None:
    report = PostgresMemoryBenchmarkReport(
        account_id="account",
        query_count=2,
        iterations=3,
        latency_p50_ms=1.5,
        latency_p95_ms=2.5,
        search_document_rows=10,
        vector_rows=8,
        table_bytes=1000,
        index_bytes=500,
        shared_buffer_resident_bytes=None,
        index_sizes=(("idx_a", 500),),
    )

    payload = benchmark_report_json(report)

    assert '"latency_p95_ms": 2.5' in payload
    assert '"shared_buffer_resident_bytes": null' in payload
    assert '"name": "idx_a"' in payload


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for pgvector upgrade and ANN contracts",
)
async def test_legacy_vector_schema_upgrades_in_place_and_hnsw_is_used() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_vector_upgrade_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    catalog: PostgresMemoryCatalog | None = None
    archive: PostgresLifeArchive | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        dsn = _database_dsn(admin_dsn, database)
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(
                Path("services/archive/postgres_schema.sql").read_text(
                    encoding="utf-8"
                )
            )
            await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await connection.execute(
                """
                CREATE TABLE memory_vector_documents (
                    item_id UUID PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    embedding vector NOT NULL,
                    source_event_id TEXT NOT NULL
                        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            legacy_item_id = uuid.uuid4()
            await connection.execute(
                """
                INSERT INTO archive_evidence_events (
                    event_id, account_id, event_type, schema_version, occurred_at,
                    speaker_class, source, payload, content_sha256
                ) VALUES (
                    'legacy-vector-event', 'vector-upgrade-account',
                    'speech.utterance_finalized', 1, now(), 'owner', 'upgrade-test',
                    '{"text":"我在杭州读过书。"}'::jsonb,
                    repeat('0', 64)
                )
                """
            )
            await connection.execute(
                """
                INSERT INTO memory_vector_documents (
                    item_id, account_id, embedding_model, embedding,
                    source_event_id
                ) VALUES (
                    $1, 'vector-upgrade-account', 'benchmark-semantic-v1',
                    '[1,0]'::vector, 'legacy-vector-event'
                )
                """,
                legacy_item_id,
            )
        finally:
            await connection.close()

        archive = PostgresLifeArchive(dsn)
        catalog = PostgresMemoryCatalog(
            dsn,
            extractor=RuleBasedMemoryExtractor(),
            embedder=BenchmarkEmbedder(),
            require_vector=True,
        )
        await archive.initialize()
        await catalog.initialize()
        connection = await asyncpg.connect(dsn)
        try:
            dimensions = await connection.fetchval(
                """
                SELECT embedding_dimensions
                FROM memory_vector_documents
                WHERE item_id = $1 AND embedding_model = 'benchmark-semantic-v1'
                """,
                legacy_item_id,
            )
            primary_key = await connection.fetchval(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'memory_vector_documents'::regclass
                  AND contype = 'p'
                """
            )
            constraints = {
                str(row["conname"])
                for row in await connection.fetch(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'memory_vector_documents'::regclass
                    """
                )
            }
            indexes = {
                str(row["indexname"]): str(row["indexdef"])
                for row in await connection.fetch(
                    """
                    SELECT indexname, indexdef FROM pg_indexes
                    WHERE tablename = 'memory_vector_documents'
                    """
                )
            }
            await connection.execute(
                """
                INSERT INTO memory_vector_documents (
                    item_id, account_id, embedding_model, embedding_dimensions,
                    embedding, source_event_id
                ) VALUES (
                    $1, 'vector-upgrade-account', 'benchmark-semantic-v2', 3,
                    '[1,0,0]'::vector, 'legacy-vector-event'
                )
                """,
                legacy_item_id,
            )
            isolated_rows = int(
                await connection.fetchval(
                    """
                    SELECT count(*) FROM memory_vector_documents
                    WHERE item_id = $1
                    """,
                    legacy_item_id,
                )
            )
        finally:
            await connection.close()

        plan, uses_hnsw = await explain_pgvector_ann(
            dsn=dsn,
            account_id="vector-upgrade-account",
            embedding_model="benchmark-semantic-v1",
            embedding_dimensions=2,
            vector=(1.0, 0.0),
            limit=10,
        )

        assert dimensions == 2
        assert primary_key == (
            "PRIMARY KEY (item_id, embedding_model, embedding_dimensions)"
        )
        assert "memory_vector_dimensions_match" in constraints
        assert isolated_rows == 2
        assert any(
            "USING hnsw" in definition
            and "benchmark-semantic-v1" in definition
            and "embedding_dimensions = 2" in definition
            for definition in indexes.values()
        )
        assert uses_hnsw is True, plan

        event_id = f"benchmark-event-{uuid.uuid4()}"
        await archive.record(
            EvidenceEvent(
                event_id=event_id,
                account_id="vector-upgrade-account",
                event_type="speech.utterance_finalized",
                occurred_at=datetime.now(UTC),
                speaker_class="owner",
                source="benchmark-test",
                payload={
                    "text": "我在杭州读过书。",
                    "interaction_mode": "companion",
                    "prompt_kind": "spontaneous",
                    "owner_projection_eligible": True,
                },
            )
        )
        await catalog.compile_pending(limit=100)
        report = await benchmark_postgres_memory_search(
            catalog=catalog,
            dsn=dsn,
            account_id="vector-upgrade-account",
            queries=("城市经历", "杭州"),
            iterations=2,
            embedding_model="benchmark-semantic-v1",
            embedding_dimensions=2,
        )

        assert report.query_count == 2
        assert report.latency_p50_ms >= 0
        assert report.latency_p95_ms >= report.latency_p50_ms
        assert report.search_document_rows > 0
        assert report.vector_rows > 0
        assert report.table_bytes > 0
        assert report.index_bytes > 0
    finally:
        if catalog is not None:
            await catalog.close()
        if archive is not None:
            await archive.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.close()
