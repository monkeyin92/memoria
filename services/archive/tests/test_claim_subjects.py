"""Claim-to-child association on PostgreSQL.

A parent's turn names the child in ``subject_key`` while the evidence names the
parent.  These tests pin the conservative resolution rules and prove that a
correction and a full projection rebuild keep the same association.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from services.archive.claim_subjects import BoundChildSubjectIds
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import (
    ExtractedClaim,
    ExtractedPerson,
    MemoryClaimReview,
    MemoryExtraction,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog
from services.archive.restore_drill import rebuild_postgres_memory_projections
from testing.postgres_harness import TestDatabase

MakeCatalog = Callable[..., Awaitable[PostgresMemoryCatalog]]
OwnerSql = Callable[..., list[tuple[Any, ...]]]

_ACCOUNT = "claim-subject-account"
_CHILD = "claim-subject-child"


def _assert_same_subject(actual: object, expected: str) -> None:
    """Compare ids without putting either one into a failure message."""

    if actual != expected:
        raise AssertionError("claim subject association mismatch")


async def _record(
    archive: PostgresLifeArchive,
    event_id: str,
    text: str,
    *,
    subject_id: str,
    payload: dict[str, object] | None = None,
) -> None:
    await archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id=_ACCOUNT,
            subject_id=subject_id,
            session_id="claim-subject-session",
            turn_id=1,
            generation_id=1,
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            speaker_class="owner",
            source="claim-subject-test",
            payload={
                "text": text,
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
                "tool_epoch": 0,
                **(payload or {}),
            },
        )
    )


def _bound(*children: str) -> BoundChildSubjectIds:
    async def resolve(account_id: str) -> tuple[str, ...]:
        return children if account_id == _ACCOUNT else ()

    return resolve


class _ChildKeyExtractor:
    """LLM-style drift: a ``child:`` key whose person row says ``son``."""

    version = "claim-subject-child-key-v1"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        del event
        return MemoryExtraction(
            claims=(
                ExtractedClaim(
                    domain_category="daily_life",
                    subject_key="child:小明",
                    predicate="age",
                    value="8",
                    confidence=0.9,
                ),
            ),
            people=(
                ExtractedPerson(
                    display_name="小明",
                    relationship_to_owner="son",
                    canonical_key="child:小明",
                    aliases=(),
                ),
            ),
            extractor_version=self.version,
        )


@pytest.mark.asyncio
async def test_evidence_naming_the_child_associates_the_claim(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
    owner_sql: OwnerSql,
) -> None:
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor(), bound_child_subject_ids=_bound(_CHILD))
    await _record(archive, "child-turn", "我喜欢蓝色。", subject_id=_CHILD)

    report = await catalog.compile_pending()

    assert report.failed_events == 0
    rows = owner_sql(
        "SELECT about_subject_id FROM memory_claims"
        " WHERE account_id = %s AND source_event_id = %s",
        _ACCOUNT,
        "child-turn",
    )
    assert len(rows) == 1
    _assert_same_subject(rows[0][0], _CHILD)


@pytest.mark.asyncio
async def test_one_bound_child_absorbs_a_child_relation_claim(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
    owner_sql: OwnerSql,
) -> None:
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor(), bound_child_subject_ids=_bound(_CHILD))
    await _record(
        archive,
        "parent-turn",
        "我儿子叫小明，今年8岁。",
        subject_id=_ACCOUNT,
    )

    report = await catalog.compile_pending()

    assert report.failed_events == 0
    rows = owner_sql(
        "SELECT about_subject_id FROM memory_claims"
        " WHERE account_id = %s AND source_event_id = %s",
        _ACCOUNT,
        "parent-turn",
    )
    marked = [row[0] for row in rows if row[0] == _CHILD]
    assert len(marked) == 1


@pytest.mark.asyncio
async def test_two_bound_children_leave_the_claim_unassociated(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
    owner_sql: OwnerSql,
) -> None:
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor(), bound_child_subject_ids=_bound(_CHILD, "claim-subject-sibling"))
    await _record(
        archive,
        "parent-turn",
        "我儿子叫小明，今年8岁。",
        subject_id=_ACCOUNT,
    )

    report = await catalog.compile_pending()

    assert report.failed_events == 0
    rows = owner_sql(
        "SELECT about_subject_id FROM memory_claims"
        " WHERE account_id = %s AND source_event_id = %s",
        _ACCOUNT,
        "parent-turn",
    )
    assert rows
    assert all(row[0] is None for row in rows)


@pytest.mark.asyncio
async def test_a_non_child_relation_key_is_not_associated(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
    owner_sql: OwnerSql,
) -> None:
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor(), bound_child_subject_ids=_bound(_CHILD))
    await _record(
        archive,
        "parent-turn",
        "我妈妈叫李梅，今年60岁。",
        subject_id=_ACCOUNT,
    )

    report = await catalog.compile_pending()

    assert report.failed_events == 0
    rows = owner_sql(
        "SELECT about_subject_id FROM memory_claims"
        " WHERE account_id = %s AND source_event_id = %s",
        _ACCOUNT,
        "parent-turn",
    )
    assert rows
    assert all(row[0] is None for row in rows)


@pytest.mark.asyncio
async def test_an_unavailable_child_lookup_leaves_the_claim_unassociated(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
    owner_sql: OwnerSql,
) -> None:
    async def unavailable(_account_id: str) -> tuple[str, ...]:
        raise TimeoutError("identity unavailable")

    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor(), bound_child_subject_ids=unavailable)
    await _record(
        archive,
        "parent-turn",
        "我儿子叫小明，今年8岁。",
        subject_id=_ACCOUNT,
    )

    report = await catalog.compile_pending()

    assert report.failed_events == 0
    rows = owner_sql(
        "SELECT about_subject_id FROM memory_claims"
        " WHERE account_id = %s AND source_event_id = %s",
        _ACCOUNT,
        "parent-turn",
    )
    assert rows
    assert all(row[0] is None for row in rows)


@pytest.mark.asyncio
async def test_a_person_relation_marks_a_key_without_the_son_prefix(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
    owner_sql: OwnerSql,
) -> None:
    catalog = await make_catalog(
        extractor=_ChildKeyExtractor(),
        bound_child_subject_ids=_bound(_CHILD),
    )
    await _record(archive, "parent-turn", "", subject_id=_ACCOUNT)

    report = await catalog.compile_pending()

    assert report.failed_events == 0
    rows = owner_sql(
        "SELECT about_subject_id FROM memory_claims"
        " WHERE account_id = %s AND source_event_id = %s",
        _ACCOUNT,
        "parent-turn",
    )
    assert len(rows) == 1
    _assert_same_subject(rows[0][0], _CHILD)


@pytest.mark.asyncio
async def test_a_correction_keeps_the_claim_subject_association(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
    owner_sql: OwnerSql,
) -> None:
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor(), bound_child_subject_ids=_bound(_CHILD))
    await _record(
        archive,
        "parent-turn",
        "我儿子叫小明，今年8岁。",
        subject_id=_ACCOUNT,
    )
    assert (await catalog.compile_pending()).failed_events == 0
    rows = owner_sql(
        "SELECT claim_id FROM memory_claims"
        " WHERE account_id = %s AND about_subject_id = %s",
        _ACCOUNT,
        _CHILD,
    )
    assert len(rows) == 1
    claim_id = str(rows[0][0])

    await catalog.review(
        MemoryClaimReview(
            account_id=_ACCOUNT,
            subject_id=_ACCOUNT,
            claim_id=claim_id,
            action="correct",
            corrected_value="小明今年九岁了。",
        )
    )

    after = owner_sql(
        "SELECT about_subject_id FROM memory_claims"
        " WHERE account_id = %s AND claim_id = %s",
        _ACCOUNT,
        claim_id,
    )
    assert len(after) == 1
    _assert_same_subject(after[0][0], _CHILD)


@pytest.mark.asyncio
async def test_projection_rebuild_reproduces_the_association(
    postgres_database: TestDatabase,
    archive: PostgresLifeArchive,
    owner_sql: OwnerSql,
) -> None:
    app_dsn = postgres_database.role_dsn("memoria_app")
    catalog = PostgresMemoryCatalog(
        app_dsn,
        extractor=RuleBasedMemoryExtractor(),
        compiler_dsn=postgres_database.role_dsn("memoria_compiler"),
        compiler_role="memoria_compiler",
        evidence_subject_category_resolver=lambda event: (
            value if isinstance(value := event.payload.get("subject_category"), str) else None
        ),
        bound_child_subject_ids=_bound(_CHILD),
    )
    await catalog.initialize()
    try:
        await _record(
            archive,
            "child-turn",
            "我喜欢蓝色。",
            subject_id=_CHILD,
            payload={"subject_category": "minor"},
        )
        await _record(
            archive,
            "parent-turn",
            "我儿子叫小明，今年8岁。",
            subject_id=_ACCOUNT,
            payload={"subject_category": "adult"},
        )
        assert (await catalog.compile_pending()).failed_events == 0
    finally:
        await catalog.close()

    report = await rebuild_postgres_memory_projections(postgres_database.owner_dsn())

    assert report.failed_events == 0
    marked = owner_sql(
        "SELECT count(*) FROM memory_claims"
        " WHERE account_id = %s AND about_subject_id = %s",
        _ACCOUNT,
        _CHILD,
    )
    assert marked == [(1,)]

    owner_sql(
        "UPDATE memory_claims SET about_subject_id = NULL WHERE account_id = %s",
        _ACCOUNT,
    )
    report = await rebuild_postgres_memory_projections(
        postgres_database.owner_dsn(),
        bound_child_subject_ids=_bound(_CHILD),
    )
    assert report.failed_events == 0
    marked = owner_sql(
        "SELECT count(*) FROM memory_claims"
        " WHERE account_id = %s AND about_subject_id = %s",
        _ACCOUNT,
        _CHILD,
    )
    assert marked == [(1,)]
