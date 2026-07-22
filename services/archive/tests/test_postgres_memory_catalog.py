from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import (
    CompileReport,
    MemoryClaimReview,
    MemoryEmbeddingUnavailableError,
    MemorySearchQuery,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog, QwenMemoryEmbedder


class SemanticEmbedderStub:
    model = "semantic-test-v1"

    async def embed(self, text: str) -> tuple[float, ...]:
        if "杭州" in text or "城市经历" in text:
            return (1.0, 0.0)
        return (0.0, 1.0)


class UnavailableEmbedderStub:
    model = "unavailable-test-v1"

    async def embed(self, text: str) -> tuple[float, ...]:
        del text
        raise MemoryEmbeddingUnavailableError("embedding service unavailable")


class UpgradedEmbedderStub:
    model = "semantic-test-v2"

    async def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (1.0, 0.0, 0.0)


@pytest.mark.asyncio
async def test_qwen_memory_embedder_uses_the_bounded_embeddings_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://dashscope.test/compatible-mode/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.read() == (
            b'{"model":"text-embedding-v4","input":"\xe5\x9f\x8e\xe5\xb8\x82\xe7\xbb\x8f\xe5\x8e\x86",'
            b'"encoding_format":"float"}'
        )
        return httpx.Response(200, json={"data": [{"embedding": [0.25, 0.75]}]})

    embedder = QwenMemoryEmbedder(
        endpoint="https://dashscope.test/compatible-mode/v1/embeddings",
        api_key="test-key",
        model="text-embedding-v4",
        transport=httpx.MockTransport(handler),
    )

    assert await embedder.embed("城市经历") == (0.25, 0.75)


def _postgres_dsn(dsn: str, *, user: str, password: str, database: str) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit(
        (
            parsed.scheme,
            f"{quote(user)}:{quote(password)}@{host}",
            f"/{database}",
            parsed.query,
            "",
        )
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL compiler RLS contract",
)
async def test_compiler_role_claims_outbox_without_bypassing_account_rls() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    suffix = uuid.uuid4().hex[:10]
    database = f"memoria_compiler_{suffix}"
    app_role = f"memoria_app_{suffix}"
    compiler_role = f"memoria_compiler_{suffix}"
    app_password = f"app-{suffix}-password"
    compiler_password = f"compiler-{suffix}-password"
    admin = await asyncpg.connect(admin_dsn)
    archive: PostgresLifeArchive | None = None
    ordinary: PostgresMemoryCatalog | None = None
    compiler: PostgresMemoryCatalog | None = None
    try:
        await admin.execute(
            """
            DO $roles$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_archive_compiler')
                THEN
                    CREATE ROLE memoria_archive_compiler NOLOGIN NOSUPERUSER NOBYPASSRLS;
                END IF;
            END
            $roles$
            """
        )
        await admin.execute(
            f'CREATE ROLE "{app_role}" LOGIN PASSWORD \'{app_password}\' '
            "NOSUPERUSER NOBYPASSRLS"
        )
        await admin.execute(
            f'CREATE ROLE "{compiler_role}" LOGIN PASSWORD \'{compiler_password}\' '
            "NOSUPERUSER NOBYPASSRLS IN ROLE memoria_archive_compiler"
        )
        await admin.execute(f'CREATE DATABASE "{database}" OWNER "{app_role}"')
        app_dsn = _postgres_dsn(
            admin_dsn,
            user=app_role,
            password=app_password,
            database=database,
        )
        compiler_dsn = _postgres_dsn(
            admin_dsn,
            user=compiler_role,
            password=compiler_password,
            database=database,
        )
        archive = PostgresLifeArchive(app_dsn)
        ordinary = PostgresMemoryCatalog(app_dsn, extractor=RuleBasedMemoryExtractor())
        compiler = PostgresMemoryCatalog(
            app_dsn,
            compiler_dsn=compiler_dsn,
            compiler_role=compiler_role,
            extractor=RuleBasedMemoryExtractor(),
        )
        await archive.initialize()
        await ordinary.initialize()
        await compiler.initialize()
        for account_id in ("compiler-account-a", "compiler-account-b"):
            await archive.record(
                EvidenceEvent(
                    event_id=f"event-{account_id}",
                    account_id=account_id,
                    event_type="speech.utterance_finalized",
                    occurred_at=datetime.now(UTC),
                    speaker_class="owner",
                    source="compiler-rls-test",
                    payload={
                        "text": "我在杭州读过书。",
                        "interaction_mode": "companion",
                        "prompt_kind": "spontaneous",
                        "owner_projection_eligible": True,
                    },
                )
            )

        assert await ordinary.compile_pending(limit=10) == CompileReport()
        report = await compiler.compile_pending(limit=10)

        assert report.compiled_events == 2
        ordinary_connection = await asyncpg.connect(app_dsn)
        compiler_connection = await asyncpg.connect(compiler_dsn)
        try:
            assert (
                await ordinary_connection.fetchval("SELECT count(*) FROM archive_processing_outbox")
                == 0
            )
            assert (
                await compiler_connection.fetchval(
                    "SELECT count(*) FROM archive_processing_outbox WHERE status = 'completed'"
                )
                == 2
            )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await compiler_connection.fetchval("SELECT count(*) FROM archive_evidence_events")
        finally:
            await ordinary_connection.close()
            await compiler_connection.close()
    finally:
        if compiler is not None:
            await compiler.close()
        if ordinary is not None:
            await ordinary.close()
        if archive is not None:
            await archive.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{compiler_role}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{app_role}"')
        await admin.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to a pgvector PostgreSQL for semantic memory",
)
async def test_pgvector_projection_and_hybrid_search_are_rebuildable() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = f"vector-memory-{uuid.uuid4()}"
    event_ids = (f"vector-city-{uuid.uuid4()}", f"vector-family-{uuid.uuid4()}")
    archive = PostgresLifeArchive(dsn)
    catalog = PostgresMemoryCatalog(
        dsn,
        extractor=RuleBasedMemoryExtractor(),
        embedder=SemanticEmbedderStub(),
        require_vector=True,
    )
    await archive.initialize()
    await catalog.initialize()
    connection = await asyncpg.connect(dsn)
    try:
        for event_id, text in zip(
            event_ids,
            ("我在杭州读过书。", "我们家的家训是答应别人的事一定做到。"),
            strict=True,
        ):
            await archive.record(
                EvidenceEvent(
                    event_id=event_id,
                    account_id=account_id,
                    event_type="speech.utterance_finalized",
                    occurred_at=datetime.now(UTC),
                    speaker_class="owner",
                    source="vector-contract-test",
                    payload={
                        "text": text,
                        "interaction_mode": "companion",
                        "prompt_kind": "spontaneous",
                        "owner_projection_eligible": True,
                    },
                )
            )

        report = await catalog.compile_pending(limit=1000)
        result = await catalog.search(
            MemorySearchQuery(
                account_id=account_id,
                speaker_class="owner",
                text="城市经历",
            )
        )
        vectors = await connection.fetch(
            """
            SELECT embedding_model, source_event_id
            FROM memory_vector_documents
            WHERE account_id = $1
            """,
            account_id,
        )

        assert report.compiled_events >= 2
        assert {str(row["embedding_model"]) for row in vectors} == {"semantic-test-v1"}
        assert result.items
        assert result.items[0].source_event_id == event_ids[0]
    finally:
        await connection.execute(
            "DELETE FROM archive_evidence_events WHERE account_id = $1",
            account_id,
        )
        await connection.close()
        await catalog.close()
        await archive.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to a pgvector PostgreSQL for semantic fallback",
)
async def test_embedding_outage_keeps_fulltext_projection_and_search_available() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = f"embedding-fallback-{uuid.uuid4()}"
    event_id = f"embedding-fallback-event-{uuid.uuid4()}"
    archive = PostgresLifeArchive(dsn)
    catalog = PostgresMemoryCatalog(
        dsn,
        extractor=RuleBasedMemoryExtractor(),
        embedder=UnavailableEmbedderStub(),
        require_vector=True,
    )
    await archive.initialize()
    await catalog.initialize()
    connection = await asyncpg.connect(dsn)
    try:
        await archive.record(
            EvidenceEvent(
                event_id=event_id,
                account_id=account_id,
                event_type="speech.utterance_finalized",
                occurred_at=datetime.now(UTC),
                speaker_class="owner",
                source="embedding-fallback-test",
                payload={
                    "text": "我们家的家训是答应别人的事一定做到。",
                    "interaction_mode": "companion",
                    "prompt_kind": "spontaneous",
                    "owner_projection_eligible": True,
                },
            )
        )

        report = await catalog.compile_pending(limit=1000)
        result = await catalog.search(
            MemorySearchQuery(account_id=account_id, speaker_class="owner", text="家训")
        )

        assert report.compiled_events >= 1
        assert result.items
        assert result.items[0].source_event_id == event_id
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM memory_vector_documents WHERE account_id = $1",
                account_id,
            )
            == 0
        )
    finally:
        await connection.execute(
            "DELETE FROM archive_evidence_events WHERE account_id = $1",
            account_id,
        )
        await connection.close()
        await catalog.close()
        await archive.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to a pgvector PostgreSQL for model upgrade",
)
async def test_hybrid_search_ignores_stale_vectors_from_an_older_model() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = f"embedding-upgrade-{uuid.uuid4()}"
    event_id = f"embedding-upgrade-event-{uuid.uuid4()}"
    archive = PostgresLifeArchive(dsn)
    old_catalog = PostgresMemoryCatalog(
        dsn,
        extractor=RuleBasedMemoryExtractor(),
        embedder=SemanticEmbedderStub(),
        require_vector=True,
    )
    upgraded_catalog = PostgresMemoryCatalog(
        dsn,
        extractor=RuleBasedMemoryExtractor(),
        embedder=UpgradedEmbedderStub(),
        require_vector=True,
    )
    await archive.initialize()
    await old_catalog.initialize()
    await upgraded_catalog.initialize()
    connection = await asyncpg.connect(dsn)
    try:
        await archive.record(
            EvidenceEvent(
                event_id=event_id,
                account_id=account_id,
                event_type="speech.utterance_finalized",
                occurred_at=datetime.now(UTC),
                speaker_class="owner",
                source="embedding-upgrade-test",
                payload={
                    "text": "我们家的家训是答应别人的事一定做到。",
                    "interaction_mode": "companion",
                    "prompt_kind": "spontaneous",
                    "owner_projection_eligible": True,
                },
            )
        )
        await old_catalog.compile_pending(limit=1000)

        result = await upgraded_catalog.search(
            MemorySearchQuery(account_id=account_id, speaker_class="owner", text="家训")
        )

        assert result.items
        assert result.items[0].source_event_id == event_id
    finally:
        await connection.execute(
            "DELETE FROM archive_evidence_events WHERE account_id = $1",
            account_id,
        )
        await connection.close()
        await upgraded_catalog.close()
        await old_catalog.close()
        await archive.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL memory contract test",
)
async def test_postgres_memory_catalog_matches_sqlite_contract_and_forces_rls() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = "postgres-memory-account"
    archive = PostgresLifeArchive(dsn)
    catalog = PostgresMemoryCatalog(dsn, extractor=RuleBasedMemoryExtractor())
    await archive.initialize()
    await catalog.initialize()
    connection = await asyncpg.connect(dsn)
    await connection.execute(
        "DELETE FROM archive_evidence_events WHERE account_id = $1",
        account_id,
    )
    await connection.close()

    for index, (speaker_class, text) in enumerate(
        (
            ("owner", "我们家的家训是答应别人的事一定做到。"),
            ("owner", "我妈妈叫李梅，今年60岁。"),
            ("owner", "我母亲李梅今年61岁了。"),
            ("guest", "访客内容不能进入主人的知识库。"),
        )
    ):
        await archive.record(
            EvidenceEvent(
                event_id=f"postgres-memory-{index}",
                account_id=account_id,
                session_id="postgres-memory-session",
                turn_id=index + 1,
                event_type="speech.utterance_finalized",
                occurred_at=datetime(2026, 7, 19, 11, index, tzinfo=UTC),
                speaker_class=speaker_class,  # type: ignore[arg-type]
                source="contract-test",
                payload={
                    "text": text,
                    "interaction_mode": "companion",
                    "prompt_kind": "spontaneous",
                    "owner_projection_eligible": speaker_class == "owner",
                },
            )
        )

    report = await catalog.compile_pending(limit=1000)
    search = await catalog.search(
        MemorySearchQuery(
            account_id=account_id,
            speaker_class="owner",
            text="家训",
        )
    )
    guest_search = await catalog.search(
        MemorySearchQuery(
            account_id=account_id,
            speaker_class="guest",
            text="家训",
        )
    )
    people = await catalog.people(account_id=account_id)
    queue = await catalog.review_queue(account_id=account_id)
    family_claim = next(
        item for item in queue if item.source_event_id == "postgres-memory-0"
    )
    reviewed = await catalog.review(
        MemoryClaimReview(
            account_id=account_id,
            claim_id=family_claim.item_id,
            action="confirm",
        )
    )
    context = await catalog.context(
        MemorySearchQuery(
            account_id=account_id,
            speaker_class="owner",
            text="家训",
        )
    )
    mother_claim = next(
        item
        for item in queue
        if item.source_event_id == "postgres-memory-1" and item.value != "60"
    )
    await catalog.review(
        MemoryClaimReview(
            account_id=account_id,
            claim_id=mother_claim.item_id,
            action="confirm",
        )
    )
    reviewed_people = await catalog.people(account_id=account_id)
    queue_after_review = await catalog.review_queue(account_id=account_id)

    assert report.compiled_events >= 3
    assert report.ignored_events >= 1
    assert search.items[0].source_event_id == "postgres-memory-0"
    assert guest_search.items == ()
    assert [(item.display_name, item.relationship_to_owner) for item in people] == [
        ("李梅", "mother")
    ]
    conflicts = [item for item in queue if item.reason == "conflicting_values"]
    assert {item.value for item in conflicts} == {"60", "61"}
    assert reviewed.status == "confirmed"
    assert {
        (item.kind, item.source_event_id, item.status) for item in context.items
    } == {
        ("claim", "postgres-memory-0", "confirmed"),
        ("knowledge", "postgres-memory-0", "confirmed"),
        ("timeline", "postgres-memory-0", "confirmed"),
    }
    assert [(item.display_name, item.status) for item in reviewed_people] == [
        ("李梅", "confirmed")
    ]
    assert {item.value for item in queue_after_review if item.reason == "conflicting_values"} == {
        "60",
        "61",
    }

    connection = await asyncpg.connect(dsn)
    rls = await connection.fetch(
        """
        SELECT relname, relrowsecurity, relforcerowsecurity
        FROM pg_class
        WHERE relname = ANY($1::text[])
        ORDER BY relname
        """,
        [
            "memory_claims",
            "person_entities",
            "timeline_entries",
            "knowledge_items",
            "memory_search_documents",
        ],
    )
    assert len(rls) == 5
    assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in rls)
    await connection.execute(
        "DELETE FROM archive_evidence_events WHERE account_id = $1",
        account_id,
    )
    await connection.close()
    await catalog.close()
    await archive.close()
