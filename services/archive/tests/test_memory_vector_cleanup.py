"""Retracting a claim must also forget its pgvector embedding."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import MemoryClaimReview, MemorySearchQuery
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog


class _Embedder:
    model = "vector-cleanup-test-v1"
    dimensions = 2

    async def embed(self, text: str) -> tuple[float, ...]:
        return (1.0, 0.0) if "蓝色" in text else (0.0, 1.0)


def _dsn() -> str:
    dsn = os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("set MEMORIA_TEST_POSTGRES_DSN for the pgvector cleanup contract")
    return dsn


@asynccontextmanager
async def _catalog(dsn: str) -> AsyncIterator[tuple[PostgresLifeArchive, PostgresMemoryCatalog]]:
    archive = PostgresLifeArchive(dsn)
    catalog = PostgresMemoryCatalog(
        dsn,
        extractor=RuleBasedMemoryExtractor(),
        embedder=_Embedder(),
        require_vector=True,
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
            # The review replay reactivates the row; a full projection rebuild
            # is what re-embeds it, so normal compile leaves it without a vector.
            assert await _vector_count(connection, account_id, claim_id) == 0
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
