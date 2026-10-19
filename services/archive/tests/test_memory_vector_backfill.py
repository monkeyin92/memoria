"""A missing claim vector is repaired from the current claim value only."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import asyncpg
import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import (
    ExtractedClaim,
    MemoryClaimReview,
    MemoryEmbeddingUnavailableError,
    MemoryExtraction,
    MemorySearchQuery,
)
from services.archive.memory_vector_backfill import (
    backfill_missing_claim_vectors,
    count_missing_claim_vectors,
)
from services.archive.memory_vector_cleanup import (
    claim_vector_body,
    claim_vector_title,
    document_embedding_input,
)
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog


class _Embedder:
    model = "vector-backfill-test-v1"
    dimensions = 2

    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.unavailable = False
        self.hook: Callable[[], Awaitable[None]] | None = None

    async def embed(self, text: str) -> tuple[float, ...]:
        self.inputs.append(text)
        if self.unavailable:
            raise MemoryEmbeddingUnavailableError("embedding service unavailable")
        if "红色" in text or "晚霞" in text:
            embedding = (1.0, 0.0)
        elif "绿色" in text or "乐高" in text:
            embedding = (0.0, 1.0)
        else:
            embedding = (0.5, 0.5)
        if self.hook is not None:
            hook, self.hook = self.hook, None
            await hook()
        return embedding


class _CurrentValueExtractor:
    """A claim whose value is not the captured sentence, like a real extraction."""

    version = "vector-backfill-test-v1"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        text = str(event.payload.get("text") or "").strip()
        if not text:
            return MemoryExtraction(extractor_version=self.version)
        if "孩子原话" in text:
            claim = ExtractedClaim(
                domain_category="daily_life",
                subject_key="self",
                predicate="toy",
                value="乐高",
                confidence=0.9,
            )
        else:
            claim = ExtractedClaim(
                domain_category="daily_life",
                subject_key="self",
                predicate="favourite_colour",
                value=text,
                confidence=0.9,
            )
        return MemoryExtraction(claims=(claim,), extractor_version=self.version)


def _dsn() -> str:
    dsn = os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("set MEMORIA_TEST_POSTGRES_DSN for the pgvector backfill contract")
    return dsn


@asynccontextmanager
async def _catalog(
    dsn: str,
    embedder: _Embedder,
    *,
    fail_on_embedding_error: bool = False,
) -> AsyncIterator[tuple[PostgresLifeArchive, PostgresMemoryCatalog]]:
    archive = PostgresLifeArchive(dsn)
    catalog = PostgresMemoryCatalog(
        dsn,
        extractor=_CurrentValueExtractor(),
        embedder=embedder,
        require_vector=True,
        fail_on_embedding_error=fail_on_embedding_error,
        evidence_subject_category_resolver=lambda _event: "adult",
    )
    await archive.initialize()
    await catalog.initialize()
    try:
        yield archive, catalog
    finally:
        await catalog.close()
        await archive.close()


async def _record(
    archive: PostgresLifeArchive,
    *,
    event_id: str,
    account_id: str,
    subject_id: str | None,
    text: str,
) -> None:
    await archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id=account_id,
            subject_id=subject_id,
            session_id="vector-backfill-session",
            turn_id=1,
            generation_id=1,
            event_type="speech.utterance_finalized",
            occurred_at=datetime.now(UTC),
            speaker_class="owner",
            source="vector-backfill-test",
            payload={
                "text": text,
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
                "tool_epoch": 0,
            },
        )
    )


async def _claim_id(catalog: PostgresMemoryCatalog, account_id: str, value: str) -> uuid.UUID:
    queue = await catalog.review_queue(account_id=account_id)
    return {item.value: uuid.UUID(item.item_id) for item in queue}[value]


async def _vector_count(connection: asyncpg.Connection, account_id: str, item_id: uuid.UUID) -> int:
    return int(
        await connection.fetchval(
            "SELECT count(*) FROM memory_vector_documents WHERE account_id = $1 AND item_id = $2",
            account_id,
            item_id,
        )
    )


async def _vector_text(
    connection: asyncpg.Connection, account_id: str, item_id: uuid.UUID
) -> str | None:
    value = await connection.fetchval(
        "SELECT embedding::text FROM memory_vector_documents"
        " WHERE account_id = $1 AND item_id = $2",
        account_id,
        item_id,
    )
    return None if value is None else str(value)


async def _delete_vector(
    connection: asyncpg.Connection, account_id: str, item_id: uuid.UUID
) -> None:
    await connection.execute(
        "DELETE FROM memory_vector_documents WHERE account_id = $1 AND item_id = $2",
        account_id,
        item_id,
    )


async def _cleanup(connection: asyncpg.Connection, account_id: str) -> None:
    for statement in (
        "DELETE FROM memory_vector_documents WHERE account_id = $1",
        "DELETE FROM memory_search_documents WHERE account_id = $1",
        "DELETE FROM memory_claims WHERE account_id = $1",
        "DELETE FROM archive_evidence_events WHERE account_id = $1",
    ):
        await connection.execute(statement, account_id)


@pytest.mark.asyncio
async def test_backfill_embeds_only_the_claim_current_value() -> None:
    dsn = _dsn()
    account_id = f"vector-backfill-value-{uuid.uuid4()}"
    child_id = f"vector-backfill-child-{uuid.uuid4()}"
    old_event = f"vector-backfill-old-{uuid.uuid4()}"
    uncorrected_event = f"vector-backfill-uncorrected-{uuid.uuid4()}"
    embedder = _Embedder()
    connection = await asyncpg.connect(dsn)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with _catalog(dsn, embedder) as (archive, catalog):
            await _record(
                archive,
                event_id=old_event,
                account_id=account_id,
                subject_id=child_id,
                text="我最喜欢红色。",
            )
            await _record(
                archive,
                event_id=uncorrected_event,
                account_id=account_id,
                subject_id=child_id,
                text="孩子原话：我最喜欢红色，房间很乱。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            corrected_id = await _claim_id(catalog, account_id, "我最喜欢红色。")
            uncorrected_id = await _claim_id(catalog, account_id, "乐高")

            embedder.unavailable = True
            await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    subject_id=child_id,
                    claim_id=str(corrected_id),
                    action="correct",
                    corrected_value="我最喜欢绿色。",
                )
            )
            assert await _vector_count(connection, account_id, corrected_id) == 0
            await _delete_vector(connection, account_id, uncorrected_id)
            assert await _vector_count(connection, account_id, uncorrected_id) == 0

            missing = await count_missing_claim_vectors(pool, embedder, account_id=account_id)
            assert (missing.total, missing.corrected) == (2, 1)

            embedder.unavailable = False
            embedder.inputs.clear()
            report = await backfill_missing_claim_vectors(
                pool, embedder, account_ids=[account_id], limit=10
            )

            assert (report.attempted, report.embedded, report.failed, report.skipped) == (
                2,
                2,
                0,
                0,
            )
            assert report.remaining == 0
            assert len(embedder.inputs) == 2
            corrected_value = "我最喜欢绿色。"
            corrected_input = document_embedding_input(
                claim_vector_title(corrected_value),
                claim_vector_body("self", "favourite_colour", corrected_value),
            )
            uncorrected_input = document_embedding_input(
                claim_vector_title("乐高"),
                claim_vector_body("self", "toy", "乐高"),
            )
            assert sorted(embedder.inputs) == sorted([corrected_input, uncorrected_input])
            assert all("红色" not in text for text in embedder.inputs)
            assert all("孩子原话" not in text for text in embedder.inputs)
            assert all("房间很乱" not in text for text in embedder.inputs)
            assert all("context:" not in text for text in embedder.inputs)
            assert await _vector_text(connection, account_id, corrected_id) == "[0,1]"
            assert await _vector_text(connection, account_id, uncorrected_id) == "[0,1]"
    finally:
        await pool.close()
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_backfill_skips_retracted_claims() -> None:
    dsn = _dsn()
    account_id = f"vector-backfill-retracted-{uuid.uuid4()}"
    event_id = f"vector-backfill-retracted-turn-{uuid.uuid4()}"
    embedder = _Embedder()
    connection = await asyncpg.connect(dsn)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with _catalog(dsn, embedder) as (archive, catalog):
            await _record(
                archive,
                event_id=event_id,
                account_id=account_id,
                subject_id=None,
                text="我最喜欢红色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claim_id = await _claim_id(catalog, account_id, "我最喜欢红色。")
            await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    claim_id=str(claim_id),
                    action="retract",
                )
            )
            assert await _vector_count(connection, account_id, claim_id) == 0
            embedder.inputs.clear()

            report = await backfill_missing_claim_vectors(
                pool, embedder, account_ids=[account_id], limit=10
            )
            missing = await count_missing_claim_vectors(pool, embedder, account_id=account_id)
            await catalog.context(
                MemorySearchQuery(
                    account_id=account_id,
                    speaker_class="owner",
                    text="",
                    kinds=("claim",),
                )
            )
            await catalog.wait_for_vector_backfills()

            assert (report.attempted, report.embedded, report.failed, report.skipped) == (
                0,
                0,
                0,
                0,
            )
            assert missing.total == 0 and missing.corrected == 0
            assert embedder.inputs == []
            assert await _vector_count(connection, account_id, claim_id) == 0
    finally:
        await pool.close()
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_backfill_skips_claims_of_deleted_subjects() -> None:
    dsn = _dsn()
    account_id = f"vector-backfill-deleted-{uuid.uuid4()}"
    child_id = f"vector-backfill-deleted-child-{uuid.uuid4()}"
    event_id = f"vector-backfill-deleted-turn-{uuid.uuid4()}"
    embedder = _Embedder()
    connection = await asyncpg.connect(dsn)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with _catalog(dsn, embedder) as (archive, catalog):
            await _record(
                archive,
                event_id=event_id,
                account_id=account_id,
                subject_id=child_id,
                text="我最喜欢红色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claim_id = await _claim_id(catalog, account_id, "我最喜欢红色。")
            await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    subject_id=child_id,
                    claim_id=str(claim_id),
                    action="confirm",
                )
            )
            await _delete_vector(connection, account_id, claim_id)

            # Subject deletion deletes the evidence and cascades to claims and
            # search rows in one transaction.  A partial restore can leave the
            # claim behind without its evidence; the repair must not revive it.
            await connection.execute("SET session_replication_role = replica")
            try:
                await connection.execute(
                    "DELETE FROM archive_evidence_events WHERE account_id = $1 AND event_id = $2",
                    account_id,
                    event_id,
                )
            finally:
                await connection.execute("RESET session_replication_role")
            embedder.inputs.clear()

            report = await backfill_missing_claim_vectors(
                pool, embedder, account_ids=[account_id], limit=10
            )
            missing = await count_missing_claim_vectors(pool, embedder, account_id=account_id)
            await catalog.context(
                MemorySearchQuery(
                    account_id=account_id,
                    speaker_class="owner",
                    text="",
                    kinds=("claim",),
                )
            )
            await catalog.wait_for_vector_backfills()

            assert (report.attempted, report.embedded, report.failed, report.skipped) == (
                0,
                0,
                0,
                0,
            )
            assert missing.total == 0
            assert embedder.inputs == []
            assert await _vector_count(connection, account_id, claim_id) == 0
    finally:
        await pool.close()
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_context_backfills_a_claim_missing_its_vector() -> None:
    dsn = _dsn()
    account_id = f"vector-backfill-context-{uuid.uuid4()}"
    event_id = f"vector-backfill-context-turn-{uuid.uuid4()}"
    embedder = _Embedder()
    connection = await asyncpg.connect(dsn)
    try:
        async with _catalog(dsn, embedder) as (archive, catalog):
            await _record(
                archive,
                event_id=event_id,
                account_id=account_id,
                subject_id=None,
                text="我最喜欢红色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claim_id = await _claim_id(catalog, account_id, "我最喜欢红色。")
            embedder.unavailable = True
            await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    claim_id=str(claim_id),
                    action="correct",
                    corrected_value="我最喜欢绿色。",
                )
            )
            assert await _vector_count(connection, account_id, claim_id) == 0

            embedder.unavailable = False
            first = await catalog.context(
                MemorySearchQuery(
                    account_id=account_id,
                    speaker_class="owner",
                    text="绿色",
                    kinds=("claim",),
                )
            )
            await catalog.wait_for_vector_backfills()
            assert [item.item_id for item in first.items] == [str(claim_id)]
            assert await _vector_text(connection, account_id, claim_id) == "[0,1]"

            recalled = await catalog.context(
                MemorySearchQuery(
                    account_id=account_id,
                    speaker_class="owner",
                    text="天边的火烧云",
                    kinds=("claim",),
                )
            )
            assert [item.item_id for item in recalled.items] == [str(claim_id)]
            assert recalled.items[0].snippet == "我最喜欢绿色。"
    finally:
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_scheduled_backfill_retries_until_embedding_recovers() -> None:
    dsn = _dsn()
    account_id = f"vector-backfill-retry-{uuid.uuid4()}"
    event_id = f"vector-backfill-retry-turn-{uuid.uuid4()}"
    embedder = _Embedder()
    connection = await asyncpg.connect(dsn)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with _catalog(dsn, embedder) as (archive, catalog):
            await _record(
                archive,
                event_id=event_id,
                account_id=account_id,
                subject_id=None,
                text="我最喜欢红色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claim_id = await _claim_id(catalog, account_id, "我最喜欢红色。")
            await _delete_vector(connection, account_id, claim_id)

            embedder.unavailable = True
            first = await backfill_missing_claim_vectors(
                pool, embedder, account_ids=[account_id], limit=10
            )
            first_missing = await count_missing_claim_vectors(pool, embedder, account_id=account_id)
            embedder.unavailable = False
            second = await backfill_missing_claim_vectors(
                pool, embedder, account_ids=[account_id], limit=10
            )
            second_missing = await count_missing_claim_vectors(
                pool, embedder, account_id=account_id
            )

            assert (first.failed, first.embedded, first.remaining) == (1, 0, 1)
            assert first_missing.total == 1
            assert (second.failed, second.embedded, second.remaining) == (0, 1, 0)
            assert second_missing.total == 0
            assert await _vector_count(connection, account_id, claim_id) == 1
    finally:
        await pool.close()
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_backfill_does_not_resurrect_a_claim_retracted_mid_flight() -> None:
    dsn = _dsn()
    account_id = f"vector-backfill-race-{uuid.uuid4()}"
    event_id = f"vector-backfill-race-turn-{uuid.uuid4()}"
    embedder = _Embedder()
    connection = await asyncpg.connect(dsn)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with _catalog(dsn, embedder) as (archive, catalog):
            await _record(
                archive,
                event_id=event_id,
                account_id=account_id,
                subject_id=None,
                text="我最喜欢红色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claim_id = await _claim_id(catalog, account_id, "我最喜欢红色。")
            await _delete_vector(connection, account_id, claim_id)

            async def retract() -> None:
                await catalog.review(
                    MemoryClaimReview(
                        account_id=account_id,
                        claim_id=str(claim_id),
                        action="retract",
                    )
                )

            embedder.hook = retract
            report = await backfill_missing_claim_vectors(
                pool, embedder, account_ids=[account_id], limit=10
            )

            assert (report.attempted, report.embedded, report.failed, report.skipped) == (
                1,
                0,
                0,
                1,
            )
            assert await _vector_count(connection, account_id, claim_id) == 0
            status = await connection.fetchval(
                "SELECT status FROM memory_claims WHERE account_id = $1 AND claim_id = $2",
                account_id,
                claim_id,
            )
            assert status == "retracted"
    finally:
        await pool.close()
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_backfill_does_not_write_after_a_second_correction() -> None:
    dsn = _dsn()
    account_id = f"vector-backfill-recorrect-{uuid.uuid4()}"
    event_id = f"vector-backfill-recorrect-turn-{uuid.uuid4()}"
    embedder = _Embedder()
    connection = await asyncpg.connect(dsn)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with _catalog(dsn, embedder) as (archive, catalog):
            await _record(
                archive,
                event_id=event_id,
                account_id=account_id,
                subject_id=None,
                text="我最喜欢红色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claim_id = await _claim_id(catalog, account_id, "我最喜欢红色。")
            await _delete_vector(connection, account_id, claim_id)

            async def correct_again() -> None:
                await catalog.review(
                    MemoryClaimReview(
                        account_id=account_id,
                        claim_id=str(claim_id),
                        action="correct",
                        corrected_value="晚霞是红色。",
                    )
                )

            embedder.hook = correct_again
            report = await backfill_missing_claim_vectors(
                pool, embedder, account_ids=[account_id], limit=10
            )
            stored = await connection.fetchrow(
                "SELECT claim.value, stored.embedding::text AS embedding"
                " FROM memory_claims claim"
                " JOIN memory_vector_documents stored"
                "   ON stored.item_id = claim.claim_id"
                "  AND stored.account_id = claim.account_id"
                " WHERE claim.account_id = $1 AND claim.claim_id = $2",
                account_id,
                claim_id,
            )

            assert (report.attempted, report.embedded, report.failed, report.skipped) == (
                1,
                0,
                0,
                1,
            )
            assert stored is not None
            assert stored["value"] == "晚霞是红色。"
            assert stored["embedding"] == "[1,0]"
    finally:
        await pool.close()
        await _cleanup(connection, account_id)
        await connection.close()
