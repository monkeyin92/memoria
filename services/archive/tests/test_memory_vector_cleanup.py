"""Reviewing a claim keeps its pgvector embedding in step with the search row."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import (
    MemoryClaimReview,
    MemoryEmbeddingUnavailableError,
    MemorySearchQuery,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog


class _Embedder:
    model = "vector-cleanup-test-v1"
    dimensions = 2

    async def embed(self, text: str) -> tuple[float, ...]:
        if "蓝色" in text or "天空" in text:
            return (1.0, 0.0)
        return (0.0, 1.0)


class _CountingEmbedder(_Embedder):
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def embed(self, text: str) -> tuple[float, ...]:
        self.inputs.append(text)
        return await super().embed(text)


class _FlakyEmbedder(_Embedder):
    def __init__(self) -> None:
        self.unavailable = False

    async def embed(self, text: str) -> tuple[float, ...]:
        if self.unavailable:
            raise MemoryEmbeddingUnavailableError("embedding service unavailable")
        return await super().embed(text)


def _dsn() -> str:
    dsn = os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("set MEMORIA_TEST_POSTGRES_DSN for the pgvector cleanup contract")
    return dsn


@asynccontextmanager
async def _catalog(
    dsn: str,
    embedder: _Embedder | None = None,
    *,
    fail_on_embedding_error: bool = False,
) -> AsyncIterator[tuple[PostgresLifeArchive, PostgresMemoryCatalog]]:
    archive = PostgresLifeArchive(dsn)
    catalog = PostgresMemoryCatalog(
        dsn,
        extractor=RuleBasedMemoryExtractor(),
        embedder=embedder or _Embedder(),
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
    memory_write_intent: dict[str, Any] | None = None,
) -> None:
    await archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id=account_id,
            subject_id=subject_id,
            session_id="vector-cleanup-session",
            turn_id=1,
            generation_id=1,
            event_type="speech.utterance_finalized",
            occurred_at=datetime.now(UTC),
            speaker_class="owner",
            source="vector-cleanup-test",
            payload={
                "text": text,
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
                "tool_epoch": 0,
                **({"memory_write_intent": memory_write_intent} if memory_write_intent else {}),
            },
        )
    )


async def _claim_ids(catalog: PostgresMemoryCatalog, account_id: str) -> dict[str, uuid.UUID]:
    queue = await catalog.review_queue(account_id=account_id)
    return {item.value: uuid.UUID(item.item_id) for item in queue}


async def _vector_count(connection: asyncpg.Connection, account_id: str, item_id: uuid.UUID) -> int:
    return int(
        await connection.fetchval(
            "SELECT count(*) FROM memory_vector_documents WHERE account_id = $1 AND item_id = $2",
            account_id,
            item_id,
        )
    )


async def _cleanup(connection: asyncpg.Connection, account_id: str) -> None:
    await connection.execute(
        "DELETE FROM memory_vector_documents WHERE account_id = $1", account_id
    )
    await connection.execute(
        "DELETE FROM archive_evidence_events WHERE account_id = $1", account_id
    )


@pytest.mark.asyncio
async def test_retract_deletes_only_that_claims_vector_and_keeps_evidence() -> None:
    dsn = _dsn()
    account_id = f"vector-retract-{uuid.uuid4()}"
    first = f"vector-retract-first-{uuid.uuid4()}"
    second = f"vector-retract-second-{uuid.uuid4()}"
    connection = await asyncpg.connect(dsn)
    try:
        async with _catalog(dsn) as (archive, catalog):
            await _record(
                archive,
                event_id=first,
                account_id=account_id,
                subject_id=None,
                text="我喜欢蓝色。",
            )
            await _record(
                archive,
                event_id=second,
                account_id=account_id,
                subject_id=None,
                text="我喜欢红色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claims = await _claim_ids(catalog, account_id)
            retracted_id = claims["我喜欢蓝色。"]
            kept_id = claims["我喜欢红色。"]
            assert await _vector_count(connection, account_id, retracted_id) == 1
            assert await _vector_count(connection, account_id, kept_id) == 1
            evidence_before = int(
                await connection.fetchval(
                    "SELECT count(*) FROM archive_evidence_events WHERE account_id = $1",
                    account_id,
                )
            )

            reviewed = await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    claim_id=str(retracted_id),
                    action="retract",
                )
            )

            assert await _vector_count(connection, account_id, retracted_id) == 0
            assert await _vector_count(connection, account_id, kept_id) == 1
            document = await connection.fetchrow(
                "SELECT status FROM memory_search_documents"
                " WHERE account_id = $1 AND item_id = $2 AND kind = 'claim'",
                account_id,
                retracted_id,
            )
            assert document is not None and document["status"] == "retracted"
            assert (
                int(
                    await connection.fetchval(
                        "SELECT count(*) FROM archive_evidence_events WHERE account_id = $1",
                        account_id,
                    )
                )
                == evidence_before + 1
            )
            for event_id in (first, second):
                assert await connection.fetchval(
                    "SELECT 1 FROM archive_evidence_events WHERE event_id = $1", event_id
                )
            review_event = await connection.fetchrow(
                "SELECT payload FROM archive_evidence_events WHERE event_id = $1",
                reviewed.review_event_id,
            )
            payload = json.loads(review_event["payload"])
            assert payload["action"] == "retract"
            assert "previous_value" not in payload
    finally:
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_guardian_subject_retract_deletes_the_childs_claim_vector() -> None:
    dsn = _dsn()
    account_id = f"vector-guardian-{uuid.uuid4()}"
    child_id = f"vector-guardian-child-{uuid.uuid4()}"
    child_event = f"vector-guardian-child-turn-{uuid.uuid4()}"
    owner_event = f"vector-guardian-owner-turn-{uuid.uuid4()}"
    connection = await asyncpg.connect(dsn)
    try:
        async with _catalog(dsn) as (archive, catalog):
            await _record(
                archive,
                event_id=child_event,
                account_id=account_id,
                subject_id=child_id,
                text="我喜欢蓝色。",
            )
            await _record(
                archive,
                event_id=owner_event,
                account_id=account_id,
                subject_id=None,
                text="我喜欢红色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claims = await _claim_ids(catalog, account_id)
            child_claim = claims["我喜欢蓝色。"]
            owner_claim = claims["我喜欢红色。"]

            await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    subject_id=child_id,
                    claim_id=str(child_claim),
                    action="retract",
                )
            )

            assert await _vector_count(connection, account_id, child_claim) == 0
            assert await _vector_count(connection, account_id, owner_claim) == 1
            assert await connection.fetchval(
                "SELECT 1 FROM archive_evidence_events WHERE event_id = $1", child_event
            )
    finally:
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_retracted_claim_can_be_confirmed_again() -> None:
    dsn = _dsn()
    account_id = f"vector-confirm-{uuid.uuid4()}"
    child_id = f"vector-confirm-child-{uuid.uuid4()}"
    event_id = f"vector-confirm-turn-{uuid.uuid4()}"
    connection = await asyncpg.connect(dsn)
    try:
        async with _catalog(dsn) as (archive, catalog):
            await _record(
                archive,
                event_id=event_id,
                account_id=account_id,
                subject_id=child_id,
                text="我喜欢蓝色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claim_id = (await _claim_ids(catalog, account_id))["我喜欢蓝色。"]

            await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    subject_id=child_id,
                    claim_id=str(claim_id),
                    action="retract",
                )
            )
            confirmed = await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    subject_id=child_id,
                    claim_id=str(claim_id),
                    action="confirm",
                )
            )
            document = await connection.fetchrow(
                "SELECT status FROM memory_search_documents"
                " WHERE account_id = $1 AND item_id = $2 AND kind = 'claim'",
                account_id,
                claim_id,
            )
            found = await catalog.search(
                MemorySearchQuery(
                    account_id=account_id,
                    subject_id=child_id,
                    speaker_class="owner",
                    text="蓝色",
                    kinds=("claim",),
                    include_candidates=False,
                )
            )

            replayed = await catalog.compile_pending()

            assert confirmed.status == "confirmed"
            assert document is not None and document["status"] == "confirmed"
            assert [item.item_id for item in found.items] == [str(claim_id)]
            assert replayed.failed_events == 0
            # Confirmation restores the vector immediately, and replaying the
            # earlier retraction must not delete it again.
            assert await _vector_count(connection, account_id, claim_id) == 1
    finally:
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_policy_confirmation_payload_has_no_previous_value() -> None:
    dsn = _dsn()
    account_id = f"vector-policy-{uuid.uuid4()}"
    event_id = f"vector-policy-turn-{uuid.uuid4()}"
    connection = await asyncpg.connect(dsn)
    try:
        async with _catalog(dsn) as (archive, catalog):
            await _record(
                archive,
                event_id=event_id,
                account_id=account_id,
                subject_id=None,
                text="请记住我喜欢雨天散步。",
                memory_write_intent={
                    "kind": "explicit_remember",
                    "policy_version": "explicit-memory-v2",
                },
            )
            assert (await catalog.compile_pending()).failed_events == 0
            review = await connection.fetchrow(
                "SELECT payload FROM archive_evidence_events"
                " WHERE account_id = $1 AND event_type = 'memory.claim_reviewed'"
                "   AND source = 'system.memory_write_policy'",
                account_id,
            )

            assert review is not None
            payload = json.loads(review["payload"])
            assert payload["action"] == "confirm"
            assert "previous_value" not in payload
            assert "我喜欢雨天散步。" not in json.dumps(payload, ensure_ascii=False)
    finally:
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_confirm_restores_only_that_claims_vector_for_the_next_recall() -> None:
    dsn = _dsn()
    account_id = f"vector-restore-{uuid.uuid4()}"
    other_account_id = f"vector-restore-other-{uuid.uuid4()}"
    blue_event = f"vector-restore-blue-{uuid.uuid4()}"
    red_event = f"vector-restore-red-{uuid.uuid4()}"
    green_event = f"vector-restore-green-{uuid.uuid4()}"
    yellow_event = f"vector-restore-yellow-{uuid.uuid4()}"
    embedder = _CountingEmbedder()
    connection = await asyncpg.connect(dsn)
    try:
        async with _catalog(dsn, embedder=embedder) as (archive, catalog):
            await _record(
                archive,
                event_id=blue_event,
                account_id=account_id,
                subject_id=None,
                text="我喜欢蓝色。",
            )
            await _record(
                archive,
                event_id=red_event,
                account_id=account_id,
                subject_id=None,
                text="我喜欢红色。",
            )
            await _record(
                archive,
                event_id=green_event,
                account_id=other_account_id,
                subject_id=None,
                text="我喜欢绿色。",
            )
            await _record(
                archive,
                event_id=yellow_event,
                account_id=other_account_id,
                subject_id=None,
                text="我喜欢黄色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claims = await _claim_ids(catalog, account_id)
            other_claims = await _claim_ids(catalog, other_account_id)
            blue_id = claims["我喜欢蓝色。"]
            red_id = claims["我喜欢红色。"]
            green_id = other_claims["我喜欢绿色。"]
            yellow_id = other_claims["我喜欢黄色。"]

            for claim_id in (blue_id, red_id):
                await catalog.review(
                    MemoryClaimReview(
                        account_id=account_id,
                        claim_id=str(claim_id),
                        action="retract",
                    )
                )
            for claim_id in (green_id,):
                await catalog.review(
                    MemoryClaimReview(
                        account_id=other_account_id,
                        claim_id=str(claim_id),
                        action="retract",
                    )
                )

            assert await _vector_count(connection, account_id, blue_id) == 0
            assert await _vector_count(connection, account_id, red_id) == 0
            assert await _vector_count(connection, other_account_id, green_id) == 0
            assert await _vector_count(connection, other_account_id, yellow_id) == 1

            query = MemorySearchQuery(
                account_id=account_id,
                speaker_class="owner",
                text="天空的颜色",
                kinds=("claim",),
                include_candidates=False,
            )
            assert (await catalog.context(query)).items == ()

            started = time.monotonic()
            confirmed = await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    claim_id=str(blue_id),
                    action="confirm",
                )
            )
            recalled = await catalog.context(query)
            elapsed = time.monotonic() - started

            assert confirmed.status == "confirmed"
            assert [item.item_id for item in recalled.items] == [str(blue_id)]
            assert elapsed < 120, f"confirmation to recall took {elapsed:.3f}s"
            assert await _vector_count(connection, account_id, blue_id) == 1
            vector = await connection.fetchrow(
                "SELECT embedding_model, embedding_dimensions"
                " FROM memory_vector_documents"
                " WHERE account_id = $1 AND item_id = $2",
                account_id,
                blue_id,
            )
            assert vector is not None
            assert (vector["embedding_model"], vector["embedding_dimensions"]) == (
                embedder.model,
                embedder.dimensions,
            )
            # Only the confirmed claim came back; the other retracted claim and
            # every other account keep their previous vector state.
            assert await _vector_count(connection, account_id, red_id) == 0
            assert await _vector_count(connection, other_account_id, green_id) == 0
            assert await _vector_count(connection, other_account_id, yellow_id) == 1
            other_query = MemorySearchQuery(
                account_id=other_account_id,
                speaker_class="owner",
                text="天空的颜色",
                kinds=("claim",),
                include_candidates=False,
            )
            assert (await catalog.context(other_query)).items == ()

            lexical_only = PostgresMemoryCatalog(
                dsn,
                extractor=RuleBasedMemoryExtractor(),
                embedder=None,
                evidence_subject_category_resolver=lambda _event: "adult",
            )
            await lexical_only.initialize()
            try:
                assert (await lexical_only.context(query)).items == ()
            finally:
                await lexical_only.close()
    finally:
        await _cleanup(connection, account_id)
        await _cleanup(connection, other_account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_compile_replay_of_a_confirmation_does_not_embed_again() -> None:
    dsn = _dsn()
    account_id = f"vector-replay-{uuid.uuid4()}"
    event_id = f"vector-replay-turn-{uuid.uuid4()}"
    embedder = _CountingEmbedder()
    connection = await asyncpg.connect(dsn)
    try:
        async with _catalog(dsn, embedder=embedder) as (archive, catalog):
            await _record(
                archive,
                event_id=event_id,
                account_id=account_id,
                subject_id=None,
                text="我喜欢蓝色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claim_id = (await _claim_ids(catalog, account_id))["我喜欢蓝色。"]
            await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    claim_id=str(claim_id),
                    action="retract",
                )
            )
            await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    claim_id=str(claim_id),
                    action="confirm",
                )
            )
            document = await connection.fetchrow(
                "SELECT title, body FROM memory_search_documents"
                " WHERE account_id = $1 AND item_id = $2 AND kind = 'claim'",
                account_id,
                claim_id,
            )
            assert document is not None
            document_text = " ".join(
                part for part in (document["title"], document["body"]) if part
            )
            assert embedder.inputs.count(document_text) == 2
            before_replay = embedder.inputs.count(document_text)

            replayed = await catalog.compile_pending()

            assert replayed.failed_events == 0
            assert embedder.inputs.count(document_text) == before_replay
            assert await _vector_count(connection, account_id, claim_id) == 1
    finally:
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_policy_auto_confirmation_does_not_restore_the_vector() -> None:
    dsn = _dsn()
    account_id = f"vector-policy-restore-{uuid.uuid4()}"
    event_id = f"vector-policy-restore-turn-{uuid.uuid4()}"
    embedder = _CountingEmbedder()
    connection = await asyncpg.connect(dsn)
    try:
        async with _catalog(dsn, embedder=embedder) as (archive, catalog):
            await _record(
                archive,
                event_id=event_id,
                account_id=account_id,
                subject_id=None,
                text="请记住我喜欢雨天散步。",
                memory_write_intent={
                    "kind": "explicit_remember",
                    "policy_version": "explicit-memory-v2",
                },
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claim = await connection.fetchrow(
                "SELECT claim_id, status FROM memory_claims WHERE account_id = $1",
                account_id,
            )
            assert claim is not None and claim["status"] == "confirmed"
            document = await connection.fetchrow(
                "SELECT title, body FROM memory_search_documents"
                " WHERE account_id = $1 AND item_id = $2 AND kind = 'claim'",
                account_id,
                claim["claim_id"],
            )
            assert document is not None
            document_text = " ".join(
                part for part in (document["title"], document["body"]) if part
            )
            # The policy path reuses the review writer; it must not embed the
            # already-written claim a second time.
            assert embedder.inputs.count(document_text) == 1
            assert await _vector_count(connection, account_id, claim["claim_id"]) == 1

            replayed = await catalog.compile_pending()

            assert replayed.failed_events == 0
            assert embedder.inputs.count(document_text) == 1
    finally:
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_confirm_rolls_back_when_embedding_failure_is_fatal() -> None:
    dsn = _dsn()
    account_id = f"vector-restore-fatal-{uuid.uuid4()}"
    event_id = f"vector-restore-fatal-turn-{uuid.uuid4()}"
    embedder = _FlakyEmbedder()
    connection = await asyncpg.connect(dsn)
    try:
        async with _catalog(
            dsn, embedder, fail_on_embedding_error=True
        ) as (archive, catalog):
            await _record(
                archive,
                event_id=event_id,
                account_id=account_id,
                subject_id=None,
                text="我喜欢蓝色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claim_id = (await _claim_ids(catalog, account_id))["我喜欢蓝色。"]
            await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    claim_id=str(claim_id),
                    action="retract",
                )
            )
            evidence_before = int(
                await connection.fetchval(
                    "SELECT count(*) FROM archive_evidence_events WHERE account_id = $1",
                    account_id,
                )
            )
            embedder.unavailable = True

            with pytest.raises(MemoryEmbeddingUnavailableError):
                await catalog.review(
                    MemoryClaimReview(
                        account_id=account_id,
                        claim_id=str(claim_id),
                        action="confirm",
                    )
                )

            claim = await connection.fetchrow(
                "SELECT status FROM memory_claims WHERE account_id = $1 AND claim_id = $2",
                account_id,
                claim_id,
            )
            assert claim is not None and claim["status"] == "retracted"
            assert await _vector_count(connection, account_id, claim_id) == 0
            assert (
                int(
                    await connection.fetchval(
                        "SELECT count(*) FROM archive_evidence_events WHERE account_id = $1",
                        account_id,
                    )
                )
                == evidence_before
            )
    finally:
        await _cleanup(connection, account_id)
        await connection.close()


@pytest.mark.asyncio
async def test_confirm_commits_without_a_vector_when_embedding_is_best_effort() -> None:
    dsn = _dsn()
    account_id = f"vector-restore-best-effort-{uuid.uuid4()}"
    event_id = f"vector-restore-best-effort-turn-{uuid.uuid4()}"
    embedder = _FlakyEmbedder()
    connection = await asyncpg.connect(dsn)
    try:
        async with _catalog(
            dsn, embedder, fail_on_embedding_error=False
        ) as (archive, catalog):
            await _record(
                archive,
                event_id=event_id,
                account_id=account_id,
                subject_id=None,
                text="我喜欢蓝色。",
            )
            assert (await catalog.compile_pending()).failed_events == 0
            claim_id = (await _claim_ids(catalog, account_id))["我喜欢蓝色。"]
            await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    claim_id=str(claim_id),
                    action="retract",
                )
            )
            embedder.unavailable = True

            confirmed = await catalog.review(
                MemoryClaimReview(
                    account_id=account_id,
                    claim_id=str(claim_id),
                    action="confirm",
                )
            )

            assert confirmed.status == "confirmed"
            assert await _vector_count(connection, account_id, claim_id) == 0
            document = await connection.fetchrow(
                "SELECT status FROM memory_search_documents"
                " WHERE account_id = $1 AND item_id = $2 AND kind = 'claim'",
                account_id,
                claim_id,
            )
            assert document is not None and document["status"] == "confirmed"
    finally:
        await _cleanup(connection, account_id)
        await connection.close()
