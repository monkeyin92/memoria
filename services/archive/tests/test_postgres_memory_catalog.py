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
    ExtractedClaim,
    ExtractedTimeline,
    MemoryClaimReview,
    MemoryEmbeddingUnavailableError,
    MemoryExtraction,
    MemorySearchQuery,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog, QwenMemoryEmbedder


class _DomainDriftCanonicalExtractor:
    """One real episode the lexical categoriser files under two domains."""

    version = "pg-canonical-domain-drift-test-v1"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        text = str(event.payload["text"])
        domain = "work_experience" if "失败" in text else "daily_life"
        return MemoryExtraction(
            claims=(
                ExtractedClaim(
                    domain_category=domain,
                    subject_key="self",
                    predicate=domain,
                    value=text,
                    confidence=0.8,
                ),
            ),
            timeline=(
                ExtractedTimeline(
                    title=text,
                    domain_category=domain,
                    event_start=event.occurred_at,
                    event_end=None,
                    canonical_key="campus-delivery-startup",
                ),
            ),
            extractor_version=self.version,
        )


class SemanticEmbedderStub:
    model = "semantic-test-v1"
    dimensions = 2

    async def embed(self, text: str) -> tuple[float, ...]:
        if "杭州" in text or "城市经历" in text:
            return (1.0, 0.0)
        return (0.0, 1.0)


class UnavailableEmbedderStub:
    model = "unavailable-test-v1"
    dimensions = 2

    async def embed(self, text: str) -> tuple[float, ...]:
        del text
        raise MemoryEmbeddingUnavailableError("embedding service unavailable")


class UpgradedEmbedderStub:
    model = "semantic-test-v2"
    dimensions = 3

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
            b'"encoding_format":"float","dimensions":2}'
        )
        return httpx.Response(200, json={"data": [{"embedding": [0.25, 0.75]}]})

    embedder = QwenMemoryEmbedder(
        endpoint="https://dashscope.test/compatible-mode/v1/embeddings",
        api_key="test-key",
        model="text-embedding-v4",
        dimensions=2,
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
    projected_event_ids: list[str] = []

    async def project_capture(event: EvidenceEvent) -> bool:
        projected_event_ids.append(event.event_id)
        return event.account_id == "compiler-account-a"

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
            capture_evidence_projector=project_capture,
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
                        "memory_capture_candidate_v1": {"version": 1},
                    },
                )
            )

        assert await ordinary.compile_pending(limit=10) == CompileReport()
        report = await compiler.compile_pending(limit=10)

        assert report.compiled_events == 2
        assert set(projected_event_ids) == {
            "event-compiler-account-a",
            "event-compiler-account-b",
        }
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
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL episode identity contract",
)
async def test_postgres_one_canonical_key_merges_across_domains() -> None:
    """P1-06: the shared canonical key is the episode identity in PostgreSQL too.

    The candidate query filters by ``domain_category``, so the cross-domain
    identity rule needs both the SQL fix (canonical rows are always candidates)
    and the scoring rule to hold on the production store.
    """
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    suffix = uuid.uuid4().hex[:10]
    account_id = f"pg-episode-{suffix}"
    archive = PostgresLifeArchive(dsn)
    catalog = PostgresMemoryCatalog(dsn, extractor=_DomainDriftCanonicalExtractor())
    await archive.initialize()
    await catalog.initialize()
    connection = await asyncpg.connect(dsn)
    try:
        for event_id, session_id, occurred_at, text in (
            (
                f"pg-episode-start-{suffix}",
                "pg-episode-session-a",
                datetime(2026, 7, 12, 2, 0, tzinfo=UTC),
                "大学时我和老王做过校园外卖创业。",
            ),
            (
                f"pg-episode-outcome-{suffix}",
                "pg-episode-session-b",
                datetime(2026, 7, 20, 2, 0, tzinfo=UTC),
                "那次校园外卖项目后来失败了，让我很重视现金流。",
            ),
        ):
            await archive.record(
                EvidenceEvent(
                    event_id=event_id,
                    account_id=account_id,
                    session_id=session_id,
                    event_type="speech.utterance_finalized",
                    occurred_at=occurred_at,
                    speaker_class="owner",
                    source="pg-episode-contract-test",
                    payload={
                        "text": text,
                        "interaction_mode": "companion",
                        "prompt_kind": "spontaneous",
                        "owner_projection_eligible": True,
                    },
                )
            )

        await catalog.compile_pending(limit=1000)
        queue = await catalog.review_queue(account_id=account_id)
        items = {item.source_event_id: item for item in queue}
        for event_id in (
            f"pg-episode-start-{suffix}",
            f"pg-episode-outcome-{suffix}",
        ):
            await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    claim_id=items[event_id].item_id,
                    action="confirm",
                )
            )

        memories = await catalog.context(
            MemorySearchQuery(
                account_id=account_id,
                speaker_class="owner",
                kinds=("episode",),
            )
        )

        assert len(memories.items) == 1
        assert set(memories.items[0].source_event_ids) == {
            f"pg-episode-start-{suffix}",
            f"pg-episode-outcome-{suffix}",
        }
        assert "校园外卖创业" in memories.items[0].snippet
    finally:
        await connection.execute(
            "DELETE FROM episode_evidence WHERE account_id = $1", account_id
        )
        await connection.execute(
            "DELETE FROM timeline_entries WHERE account_id = $1", account_id
        )
        await connection.execute("DELETE FROM life_episodes WHERE account_id = $1", account_id)
        await connection.execute(
            "DELETE FROM archive_evidence_events WHERE account_id = $1", account_id
        )
        await connection.close()
        await catalog.close()
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
        ("episode", "postgres-memory-0", "confirmed"),
    }
    assert all(item.domain_category == "family_principle" for item in context.items)
    assert {item.memory_kind for item in context.items} == {
        "semantic",
        "episodic",
        "procedural",
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


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to a pgvector PostgreSQL for person recall",
)
async def test_postgres_person_alias_is_recallable_and_status_gated() -> None:
    """The production catalog must expose the same person projection as SQLite.

    Aliases live in ``person_aliases``, which no read path searches, so without
    this projection a confirmed nickname cannot recall the person at all. The
    document must also stay gated: a candidate person never appears in the
    confirmed-only context.
    """

    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    account_id = f"person-alias-{uuid.uuid4()}"
    event_id = f"person-alias-event-{uuid.uuid4()}"
    archive = PostgresLifeArchive(dsn)
    catalog = PostgresMemoryCatalog(dsn, extractor=RuleBasedMemoryExtractor())
    await archive.initialize()
    await catalog.initialize()
    try:
        await archive.record(
            EvidenceEvent(
                event_id=event_id,
                account_id=account_id,
                event_type="speech.utterance_finalized",
                occurred_at=datetime.now(UTC),
                speaker_class="owner",
                source="person-alias-contract-test",
                payload={
                    "text": "我妈妈叫李梅，家里人也叫她阿梅。",
                    "interaction_mode": "companion",
                    "prompt_kind": "spontaneous",
                    "owner_projection_eligible": True,
                },
            )
        )
        report = await catalog.compile_pending(limit=1000)
        assert report.failed_events == 0

        people = await catalog.people(account_id=account_id)
        assert [(person.display_name, person.status) for person in people] == [
            ("李梅", "candidate")
        ]
        assert "阿梅" in people[0].aliases

        context = await catalog.context(
            MemorySearchQuery(
                account_id=account_id,
                speaker_class="owner",
                text="阿梅是谁？",
            )
        )
        assert context.items == ()

        search = await catalog.search(
            MemorySearchQuery(
                account_id=account_id,
                speaker_class="owner",
                text="阿梅是谁？",
                include_candidates=True,
            )
        )
        recallable = [item for item in search.items if item.kind == "person"]
        assert len(recallable) == 1
        assert recallable[0].title == "李梅"
        assert "阿梅" in recallable[0].snippet
        assert "妈妈" in recallable[0].snippet
    finally:
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(
                "DELETE FROM archive_evidence_events WHERE account_id = $1",
                account_id,
            )
        finally:
            await connection.close()
        await catalog.close()
        await archive.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL minor subject contract",
)
async def test_postgres_minor_subject_filter_keeps_only_allowlisted_demo_memories() -> None:
    """The demo minor cases must survive the production PostgreSQL compiler.

    The resolver is the only category authority.  A payload that claims adult,
    or a retention marker that is not the compiler's input, must not reopen the
    minor allowlist or drop an allowlisted projection.
    """

    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_minor_demo_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    parsed = urlsplit(admin_dsn)
    dsn = _postgres_dsn(
        admin_dsn,
        user=parsed.username or "postgres",
        password=parsed.password or "",
        database=database,
    )
    categories = {
        "minor-student": "minor",
        "minor-child": "minor",
        "payload-adult": "minor",
        "payload-ephemeral": "minor",
    }

    async def resolve_category(event: EvidenceEvent) -> str:
        subject_id = event.subject_id or ""
        if subject_id not in categories:
            raise AssertionError(f"unexpected subject {subject_id}")
        return categories[subject_id]

    archive: PostgresLifeArchive | None = None
    catalog: PostgresMemoryCatalog | None = None
    try:
        archive = PostgresLifeArchive(dsn)
        catalog = PostgresMemoryCatalog(
            dsn,
            extractor=RuleBasedMemoryExtractor(),
            evidence_subject_category_resolver=resolve_category,
        )
        await archive.initialize()
        await catalog.initialize()
        utterances = (
            (
                "math-progress",
                "minor-student",
                "我今天练习了数学应用题，分数应用题还是薄弱点。",
                False,
                {},
            ),
            (
                "learning-preference",
                "minor-student",
                "学习时我喜欢先跟读，再自己说一遍。",
                False,
                {},
            ),
            (
                "reading-preference",
                "minor-student",
                "请帮我记住我喜欢阅读。",
                True,
                {},
            ),
            (
                "teacher-criticism",
                "minor-child",
                "我今天被老师批评了，好难过。",
                False,
                {},
            ),
            (
                "family-conflict",
                "minor-child",
                "我和爸爸最近总吵架。",
                False,
                {},
            ),
            (
                "payload-cannot-widen",
                "payload-adult",
                "我和爸爸最近总吵架。",
                False,
                {"subject_category": "adult"},
            ),
            (
                "retention-cannot-drop",
                "payload-ephemeral",
                "我今天练习了数学应用题，分数应用题还是薄弱点。",
                False,
                {
                    "subject_category": "unknown",
                    "memory_retention": "ephemeral_only",
                },
            ),
        )
        for index, (event_id, subject_id, text, explicit_memory, extra) in enumerate(
            utterances
        ):
            payload: dict[str, object] = {
                "text": text,
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
                "tool_epoch": 0,
                **extra,
            }
            if explicit_memory:
                payload["memory_write_intent"] = {
                    "kind": "explicit_remember",
                    "policy_version": "explicit-memory-v2",
                }
            await archive.record(
                EvidenceEvent(
                    event_id=event_id,
                    account_id="minor-demo-account",
                    subject_id=subject_id,
                    session_id="minor-demo-session",
                    turn_id=index + 1,
                    generation_id=1,
                    event_type="speech.utterance_finalized",
                    occurred_at=datetime(2026, 9, 4, 10, index, tzinfo=UTC),
                    speaker_class="owner",
                    source="minor-demo-contract",
                    payload=payload,
                )
            )

        report = await catalog.compile_pending(limit=1000)
        study = await catalog.search(
            MemorySearchQuery(
                account_id="minor-demo-account",
                speaker_class="owner",
                text="分数应用题 先跟读",
                include_candidates=True,
            )
        )
        reading = await catalog.context(
            MemorySearchQuery(
                account_id="minor-demo-account",
                speaker_class="owner",
                text="阅读",
            )
        )
        sensitive = await catalog.search(
            MemorySearchQuery(
                account_id="minor-demo-account",
                speaker_class="owner",
                text="难过 爸爸 吵架 批评",
                include_candidates=True,
            )
        )
        retained = await catalog.search(
            MemorySearchQuery(
                account_id="minor-demo-account",
                speaker_class="owner",
                text="分数应用题",
                include_candidates=True,
            )
        )

        assert report.failed_events == 0
        assert report.compiled_events == len(utterances)
        assert {
            (item.source_event_id, item.domain_category, item.status)
            for item in study.items
            if item.source_event_id in {"math-progress", "learning-preference"}
        } == {
            ("math-progress", "study_progress", "candidate"),
            ("learning-preference", "learning_preference", "candidate"),
        }
        assert {
            (item.source_event_id, item.domain_category, item.status)
            for item in reading.items
        } == {("reading-preference", "daily_life", "confirmed")}
        assert all("阅读" in f"{item.title} {item.snippet}" for item in reading.items)
        assert sensitive.items == ()
        assert any(item.source_event_id == "retention-cannot-drop" for item in retained.items)
        assert all(item.source_event_id != "payload-cannot-widen" for item in sensitive.items)
    finally:
        if catalog is not None:
            await catalog.close()
        if archive is not None:
            await archive.close()
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
                database,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        finally:
            await admin.close()
