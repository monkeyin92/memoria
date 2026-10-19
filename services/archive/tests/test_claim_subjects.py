"""Claim-to-child association on PostgreSQL.

A parent's turn names the child in ``subject_key`` while the evidence names the
parent.  These tests pin the conservative resolution rules, the multi-child
link rows, and prove that a correction and a full projection rebuild keep the
same association.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable
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
_SIBLING = "claim-subject-sibling"


def _assert_same_subject(actual: object, expected: str) -> None:
    """Compare ids without putting either one into a failure message."""

    if actual != expected:
        raise AssertionError("claim subject association mismatch")


def _assert_subject_set(actual: Iterable[str], expected: set[str]) -> None:
    """Compare id sets without putting any id into a failure message."""

    if set(actual) != expected:
        raise AssertionError("claim subject association mismatch")


def _subject_ids(owner_sql: OwnerSql, source_event_id: str) -> list[str]:
    rows = owner_sql(
        """
        SELECT link.subject_id
        FROM memory_claim_subjects link
        JOIN memory_claims claim ON claim.claim_id = link.claim_id
        WHERE claim.account_id = %s AND claim.source_event_id = %s
        ORDER BY link.subject_id
        """,
        _ACCOUNT,
        source_event_id,
    )
    return [str(row[0]) for row in rows]


def _link_count(owner_sql: OwnerSql, subject_id: str) -> int:
    rows = owner_sql(
        "SELECT count(*) FROM memory_claim_subjects"
        " WHERE account_id = %s AND subject_id = %s",
        _ACCOUNT,
        subject_id,
    )
    return int(rows[0][0])


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
    _assert_subject_set(_subject_ids(owner_sql, "child-turn"), {_CHILD})


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
    _assert_subject_set(_subject_ids(owner_sql, "parent-turn"), {_CHILD})


@pytest.mark.asyncio
async def test_two_bound_children_both_associate_a_child_relation_claim(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
    owner_sql: OwnerSql,
) -> None:
    catalog = await make_catalog(
        extractor=RuleBasedMemoryExtractor(),
        bound_child_subject_ids=_bound(_CHILD, _SIBLING),
    )
    await _record(
        archive,
        "parent-turn",
        "我儿子叫小明，今年8岁。",
        subject_id=_ACCOUNT,
    )

    report = await catalog.compile_pending()

    assert report.failed_events == 0
    _assert_subject_set(_subject_ids(owner_sql, "parent-turn"), {_CHILD, _SIBLING})


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
    assert _subject_ids(owner_sql, "parent-turn") == []


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
    assert _subject_ids(owner_sql, "parent-turn") == []


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
    _assert_subject_set(_subject_ids(owner_sql, "parent-turn"), {_CHILD})


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
        """
        SELECT claim.claim_id
        FROM memory_claims claim
        JOIN memory_claim_subjects link ON link.claim_id = claim.claim_id
        WHERE claim.account_id = %s AND link.subject_id = %s
        """,
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
        "SELECT subject_id FROM memory_claim_subjects"
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
        bound_child_subject_ids=_bound(_CHILD, _SIBLING),
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
        assert _link_count(owner_sql, _CHILD) == 1
        assert _link_count(owner_sql, _SIBLING) == 1
    finally:
        await catalog.close()

    # The first rebuild has no identity resolver: it must carry the snapshot
    # across the truncate for both children.
    report = await rebuild_postgres_memory_projections(postgres_database.owner_dsn())

    assert report.failed_events == 0
    assert _link_count(owner_sql, _CHILD) == 1
    assert _link_count(owner_sql, _SIBLING) == 1

    # With no snapshot, recompilation from the identity resolver recreates the
    # same two links.
    owner_sql(
        "DELETE FROM memory_claim_subjects WHERE account_id = %s",
        _ACCOUNT,
    )
    report = await rebuild_postgres_memory_projections(
        postgres_database.owner_dsn(),
        bound_child_subject_ids=_bound(_CHILD, _SIBLING),
    )
    assert report.failed_events == 0
    assert _link_count(owner_sql, _CHILD) == 1
    assert _link_count(owner_sql, _SIBLING) == 1


@pytest.mark.asyncio
async def test_initialization_migrates_about_subject_id_into_claim_subjects(
    archive_dsn: str,
    owner_sql: OwnerSql,
) -> None:
    account = f"claim-subject-migration-{uuid.uuid4()}"
    child = f"claim-subject-migration-child-{uuid.uuid4()}"
    event_id = f"claim-subject-migration-event-{uuid.uuid4()}"
    claim_id = uuid.uuid4()
    owner_sql(
        """
        INSERT INTO archive_evidence_events (
            event_id, account_id, event_type, schema_version, occurred_at,
            speaker_class, source, payload, content_sha256
        ) VALUES (%s, %s, 'speech.utterance_finalized', 1, now(),
                  'owner', 'claim-subject-migration', '{}'::jsonb, repeat('0', 64))
        """,
        event_id,
        account,
    )
    owner_sql(
        "ALTER TABLE memory_claims ADD COLUMN IF NOT EXISTS about_subject_id TEXT"
    )
    owner_sql(
        "CREATE INDEX IF NOT EXISTS idx_pg_memory_claim_about_subject"
        " ON memory_claims(account_id, about_subject_id)"
        " WHERE about_subject_id IS NOT NULL"
    )
    owner_sql(
        """
        INSERT INTO memory_claims (
            claim_id, account_id, category, domain_category, subject_key,
            predicate, value, confidence, sensitive_domain, extractor_version,
            source_event_id, valid_at, valid_from, observed_at, about_subject_id
        ) VALUES (%s, %s, 'daily_life', 'daily_life', 'daughter:legacy',
                  'age', '8', 0.9, 'daily_life', 'claim-subject-migration-v1',
                  %s, now(), now(), now(), %s)
        """,
        claim_id,
        account,
        event_id,
        child,
    )
    try:
        for _ in range(2):
            catalog = PostgresMemoryCatalog(
                archive_dsn,
                extractor=RuleBasedMemoryExtractor(),
            )
            try:
                await catalog.initialize()
            finally:
                await catalog.close()

        columns = owner_sql(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'memory_claims'"
            "   AND column_name = 'about_subject_id'"
        )
        assert columns == [(0,)]
        links = owner_sql(
            "SELECT count(*) FROM memory_claim_subjects"
            " WHERE claim_id = %s AND subject_id = %s",
            claim_id,
            child,
        )
        assert links == [(1,)]
    finally:
        owner_sql(
            "DELETE FROM archive_evidence_events WHERE account_id = %s",
            account,
        )
