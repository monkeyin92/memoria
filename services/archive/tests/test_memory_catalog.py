from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.domain import ContextQuery, EvidenceEvent, MemoryReview
from services.archive.life_archive import LifeArchive
from services.archive.memory_catalog import MemoryCatalog, SubjectCategoryUnresolved
from services.archive.memory_domain import (
    AccountWriteRejectedError,
    ExtractedClaim,
    ExtractedKnowledge,
    ExtractedPerson,
    ExtractedTimeline,
    MemoryClaimReview,
    MemoryExtraction,
    MemorySearchQuery,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor


class FlakyExtractor:
    version = "flaky-rules-v1"

    def __init__(self) -> None:
        self.attempts = 0
        self.delegate = RuleBasedMemoryExtractor()

    async def extract(self, event: EvidenceEvent):  # type: ignore[no-untyped-def]
        self.attempts += 1
        if self.attempts == 1:
            raise TimeoutError("temporary model timeout")
        return await self.delegate.extract(event)


class AliasExtractor:
    version = "alias-test-v1"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        alias = "妈妈" if event.event_id == "alias-source-001" else "阿梅"
        return MemoryExtraction(
            claims=(
                ExtractedClaim(
                    domain_category="daily_life",
                    subject_key="mother:李梅",
                    predicate="alias",
                    value=alias,
                    confidence=0.9,
                ),
            ),
            people=(
                ExtractedPerson(
                    display_name="李梅",
                    relationship_to_owner="mother",
                    canonical_key="mother:李梅",
                    aliases=(alias,),
                ),
            ),
            extractor_version=self.version,
        )


class CountingExtractor:
    version = "counting-test-v1"

    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        del event
        self.calls += 1
        return MemoryExtraction(extractor_version=self.version)


class ContextualClaimExtractor:
    version = "contextual-claim-v1"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        return MemoryExtraction(
            claims=(
                ExtractedClaim(
                    domain_category="daily_life",
                    subject_key="self",
                    predicate="travel_destination",
                    value="南京",
                    confidence=0.95,
                ),
            ),
            timeline=(
                ExtractedTimeline(
                    title="南京散心",
                    domain_category="daily_life",
                    event_start=event.occurred_at,
                ),
            ),
            knowledge=(
                ExtractedKnowledge(
                    domain_category="daily_life",
                    question="旅行目的地是什么？",
                    answer="南京",
                ),
            ),
            extractor_version=self.version,
        )


class SingleValueClaimExtractor:
    version = "single-value-claim-v1"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        return MemoryExtraction(
            claims=(
                ExtractedClaim(
                    domain_category="life_story",
                    subject_key="self",
                    predicate="age",
                    value="60" if event.event_id.endswith("60") else "61",
                    confidence=0.95,
                ),
            ),
            extractor_version=self.version,
        )


@asynccontextmanager
async def _reject_account_write(_: str) -> AsyncIterator[None]:
    raise AccountWriteRejectedError("account deletion is in progress")
    yield  # pragma: no cover


async def _record(
    archive: LifeArchive,
    *,
    event_id: str,
    text: str,
    speaker_class: str = "owner",
    minute: int = 0,
    explicit_memory: bool = False,
) -> None:
    payload: dict[str, object] = {
        "text": text,
        "interaction_mode": "companion",
        "prompt_kind": "spontaneous",
        "owner_projection_eligible": speaker_class == "owner",
        "tool_epoch": 0,
    }
    if explicit_memory:
        payload["memory_write_intent"] = {
            "kind": "explicit_remember",
            "policy_version": "explicit-memory-v2",
        }
    await archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id="account-memory",
            session_id="session-memory",
            turn_id=minute + 1,
            generation_id=minute + 1,
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 7, 19, 10, minute, tzinfo=UTC),
            speaker_class=speaker_class,  # type: ignore[arg-type]
            source="test",
            payload=payload,
        )
    )


@pytest.mark.asyncio
async def test_compiler_rejects_a_pending_event_after_the_account_deletion_fence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="fenced-memory", text="这条记忆来得太晚。")
    extractor = CountingExtractor()
    catalog = MemoryCatalog.sqlite(
        path,
        extractor=extractor,
        account_guard=_reject_account_write,
    )

    report = await catalog.compile_pending()

    assert report.failed_events == 1
    assert extractor.calls == 0
    assert (
        await catalog.search(MemorySearchQuery(account_id="account-memory", speaker_class="owner"))
    ).items == ()


@pytest.mark.asyncio
async def test_owner_evidence_builds_traceable_timeline_and_knowledge_while_guest_isolated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="memory-family-001",
        text="我们家的家训是答应别人的事一定做到。",
    )
    await _record(
        archive,
        event_id="memory-work-001",
        text="做项目复盘时，我习惯先找事实，再讨论责任。",
        minute=1,
    )
    await _record(
        archive,
        event_id="memory-guest-001",
        text="我是访客，这句话不能进入主人的人生知识库。",
        speaker_class="guest",
        minute=2,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())

    report = await catalog.compile_pending()
    owner_search = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="家训",
        )
    )
    guest_search = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="guest",
            text="家训",
        )
    )
    timeline = await catalog.timeline(account_id="account-memory", limit=20)

    assert report.compiled_events == 2
    assert report.ignored_events == 1
    assert owner_search.items[0].category == "family_principle"
    assert owner_search.items[0].domain_category == "family_principle"
    assert owner_search.items[0].memory_kind in {
        "semantic",
        "episodic",
        "procedural",
    }
    assert owner_search.items[0].observed_at is not None
    assert owner_search.items[0].source_event_ids == ("memory-family-001",)
    assert owner_search.items[0].source_event_id == "memory-family-001"
    assert guest_search.items == ()
    assert {item.source_event_id for item in timeline} == {
        "memory-family-001",
        "memory-work-001",
    }
    assert all(item.source_event_id != "memory-guest-001" for item in timeline)


@pytest.mark.asyncio
async def test_people_aliases_do_not_merge_same_name_across_different_relationships(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="person-mother-001",
        text="我妈妈叫李梅，今年60岁。",
    )
    await _record(
        archive,
        event_id="person-mother-002",
        text="我母亲李梅今年61岁了。",
        minute=1,
    )
    await _record(
        archive,
        event_id="person-colleague-001",
        text="我的同事李梅负责财务。",
        minute=2,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())

    await catalog.compile_pending()
    people = await catalog.people(account_id="account-memory")
    review = await catalog.review_queue(account_id="account-memory")

    assert [(person.display_name, person.relationship_to_owner) for person in people] == [
        ("李梅", "mother"),
        ("李梅", "colleague"),
    ]
    mother = people[0]
    assert set(mother.aliases) >= {"妈妈", "母亲", "李梅"}
    age_conflicts = [item for item in review if item.reason == "conflicting_values"]
    assert len(age_conflicts) == 2
    assert {item.value for item in age_conflicts} == {"60", "61"}
    assert all(item.source_event_id for item in age_conflicts)


@pytest.mark.asyncio
async def test_claim_review_controls_context_and_retraction_propagates_to_search(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="wisdom-001",
        text="我认为做重大决定前应该睡一晚再答复。",
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    await catalog.compile_pending()
    queue = await catalog.review_queue(account_id="account-memory")
    claim = next(item for item in queue if item.kind == "claim")

    confirmed = await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claim.item_id,
            action="confirm",
        )
    )
    context = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="重大决定",
            include_candidates=False,
        )
    )
    await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claim.item_id,
            action="retract",
        )
    )
    after_retract = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="重大决定",
        )
    )

    assert confirmed.status == "confirmed"
    assert {(item.kind, item.status) for item in context.items} == {
        ("claim", "confirmed"),
        ("knowledge", "confirmed"),
        ("episode", "confirmed"),
    }
    assert after_retract.items == ()


@pytest.mark.asyncio
async def test_contextual_source_text_supports_a_natural_cross_day_recall_query(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="contextual-source-001",
        text="前几天聊到旅行，我说下次想去南京。",
    )
    catalog = MemoryCatalog.sqlite(path, extractor=ContextualClaimExtractor())
    await catalog.compile_pending()
    claim = (await catalog.review_queue(account_id="account-memory"))[0]
    await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claim.item_id,
            action="confirm",
        )
    )

    result = await catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="之前聊的旅行最后想去哪？",
            include_candidates=False,
        )
    )

    assert result.items
    assert result.items[0].item_id == claim.item_id
    assert result.items[0].snippet == "南京"
    assert "前几天聊到旅行" not in result.items[0].snippet
    assert result.items[0].source_event_ids == ("contextual-source-001",)


@pytest.mark.asyncio
async def test_episode_and_knowledge_context_improve_recall_without_leaking_the_prefix(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="contextual-projections-001",
        text="前几天聊到旅行，最后还是想去南京散散心。",
    )
    catalog = MemoryCatalog.sqlite(path, extractor=ContextualClaimExtractor())
    await catalog.compile_pending()
    claim = (await catalog.review_queue(account_id="account-memory"))[0]
    await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claim.item_id,
            action="confirm",
        )
    )

    result = await catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="前几天聊到旅行",
            kinds=("episode", "knowledge"),
            include_candidates=False,
        )
    )

    assert {(item.kind, item.snippet) for item in result.items} == {
        ("episode", "南京散心"),
        ("knowledge", "南京"),
    }
    assert all("前几天聊到旅行" not in item.snippet for item in result.items)


@pytest.mark.asyncio
async def test_explicit_low_sensitivity_memory_is_immediately_confirmed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="explicit-memory-low-risk",
        text="请记住我喜欢雨天散步。",
        explicit_memory=True,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())

    await catalog.compile_pending()
    audit_events = tuple(
        event
        for event in (
            await archive.context(ContextQuery(account_id="account-memory", speaker_class="owner"))
        ).evidence
        if event.source == "system.memory_write_policy"
    )
    assert len(audit_events) == 1
    assert dict(audit_events[0].payload) == {
        "target_id": audit_events[0].payload["target_id"],
        "action": "confirm",
        "previous_value": "我喜欢雨天散步。",
        "source_event_id": "explicit-memory-low-risk",
        "policy_version": "explicit-memory-v2",
        "reason": "explicit-memory-low-risk",
        "tool_epoch": 0,
    }
    replay = await catalog.compile_pending()
    assert (replay.compiled_events, replay.failed_events) == (1, 0)
    context = await catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="雨天散步",
            kinds=("claim",),
            include_candidates=False,
        )
    )

    assert [(item.status, item.source_event_id) for item in context.items] == [
        ("confirmed", "explicit-memory-low-risk")
    ]
    assert await catalog.review_queue(account_id="account-memory") == ()


@pytest.mark.asyncio
async def test_minor_projection_uses_authoritative_category_and_drops_sensitive_candidates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "minor-memory.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="minor-family-conflict",
        text="我和爸爸最近总吵架。",
    )
    catalog = MemoryCatalog.sqlite(
        path,
        extractor=RuleBasedMemoryExtractor(),
        subject_category_resolver=lambda _: "minor",
    )

    report = await catalog.compile_pending()
    context = await catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="爸爸 吵架",
            include_candidates=True,
        )
    )

    assert report.failed_events == 0
    assert context.items == ()


@pytest.mark.asyncio
async def test_minor_projection_keeps_safe_study_progress(tmp_path: Path) -> None:
    path = tmp_path / "minor-study.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="minor-study-progress",
        text="我今天练习了英语口语，过去式还是薄弱点。",
    )
    catalog = MemoryCatalog.sqlite(
        path,
        extractor=RuleBasedMemoryExtractor(),
        subject_category_resolver=lambda _: "minor",
    )

    report = await catalog.compile_pending()
    context = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="过去式",
            include_candidates=True,
        )
    )

    assert report.failed_events == 0
    assert [(item.domain_category, item.source_event_id) for item in context.items] == [
        ("study_progress", "minor-study-progress"),
        ("study_progress", "minor-study-progress"),
        ("study_progress", "minor-study-progress"),
    ]


@pytest.mark.asyncio
async def test_explicit_sensitive_or_conflicting_memory_stays_in_review_queue(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="explicit-memory-sensitive",
        text="请记住我的银行卡号是6222021234567890123。",
        explicit_memory=True,
    )
    await _record(
        archive,
        event_id="explicit-memory-relationship",
        text="请记住我和妈妈周末一起散步。",
        explicit_memory=True,
        minute=1,
    )
    sensitive_catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    await sensitive_catalog.compile_pending()

    sensitive_context = await sensitive_catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="银行卡",
            kinds=("claim",),
            include_candidates=False,
        )
    )
    sensitive_queue = await sensitive_catalog.review_queue(account_id="account-memory")

    assert sensitive_context.items == ()
    assert [item.source_event_id for item in sensitive_queue] == [
        "explicit-memory-sensitive",
        "explicit-memory-relationship",
    ]

    conflict_path = tmp_path / "conflict.sqlite3"
    conflict_archive = LifeArchive.sqlite(conflict_path)
    await _record(
        conflict_archive,
        event_id="explicit-age-60",
        text="请记住我今年60岁。",
        explicit_memory=True,
    )
    conflict_catalog = MemoryCatalog.sqlite(
        conflict_path,
        extractor=SingleValueClaimExtractor(),
    )
    await conflict_catalog.compile_pending()
    await _record(
        conflict_archive,
        event_id="explicit-age-61",
        text="请记住我今年61岁。",
        minute=1,
        explicit_memory=True,
    )
    await conflict_catalog.compile_pending()

    queue = await conflict_catalog.review_queue(account_id="account-memory")
    assert [(item.value, item.status) for item in queue] == [
        ("60", "candidate"),
        ("61", "candidate"),
    ]


@pytest.mark.asyncio
async def test_single_value_claims_do_not_conflict_across_subjects(tmp_path: Path) -> None:
    """Two self-claims that name different speakers are not one fact.

    Both rows stay under subject_key="self" inside the login account.  The
    child's age must neither mark the owner's claim as a conflict nor count as
    an existing value that blocks the owner's own confirmation.
    """

    path = tmp_path / "subject-claims.sqlite3"
    archive = LifeArchive.sqlite(path)
    await archive.record(
        EvidenceEvent(
            event_id="explicit-age-60",
            account_id="account-memory",
            subject_id="account-memory",
            session_id="session-memory",
            turn_id=1,
            generation_id=1,
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 7, 19, 10, 0, tzinfo=UTC),
            speaker_class="owner",
            source="test",
            payload={
                "text": "请记住我今年60岁。",
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
                "tool_epoch": 0,
                "memory_write_intent": {
                    "kind": "explicit_remember",
                    "policy_version": "explicit-memory-v2",
                },
            },
        )
    )
    await archive.record(
        EvidenceEvent(
            event_id="explicit-age-61",
            account_id="account-memory",
            subject_id="person-child",
            session_id="session-memory",
            turn_id=2,
            generation_id=2,
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 7, 19, 10, 1, tzinfo=UTC),
            speaker_class="owner",
            source="test",
            payload={
                "text": "请记住我今年61岁。",
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
                "tool_epoch": 0,
                "memory_write_intent": {
                    "kind": "explicit_remember",
                    "policy_version": "explicit-memory-v2",
                },
            },
        )
    )
    catalog = MemoryCatalog.sqlite(
        path,
        extractor=SingleValueClaimExtractor(),
        # Both speakers are adults here.  The point of the test is the fold,
        # not the minor allowlist; a missing identity must not be what hides
        # the child's row.
        evidence_subject_category_resolver=lambda _event: "adult",
    )
    await catalog.compile_pending()

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT source_event_id, value, conflict_state, status FROM memory_claims"
            " WHERE predicate = 'age' ORDER BY source_event_id"
        ).fetchall()
    by_event = {str(row["source_event_id"]): row for row in rows}
    assert set(by_event) == {"explicit-age-60", "explicit-age-61"}
    assert str(by_event["explicit-age-60"]["conflict_state"]) == "none"
    assert str(by_event["explicit-age-61"]["conflict_state"]) == "none"
    assert str(by_event["explicit-age-60"]["value"]) == "60"
    assert str(by_event["explicit-age-61"]["value"]) == "61"
    # Age is outside the auto-confirm allowlist, so both rows stay candidates
    # the same way one age claim does.  What must not happen is the other
    # speaker's value showing up as an existing value that would block a
    # confirmation, or the two rows marking each other as a conflict.
    assert str(by_event["explicit-age-60"]["status"]) == "candidate"
    assert str(by_event["explicit-age-61"]["status"]) == "candidate"
    owner_event = await archive.event(account_id="account-memory", event_id="explicit-age-60")
    child_event = await archive.event(account_id="account-memory", event_id="explicit-age-61")
    assert owner_event is not None and child_event is not None
    extraction = MemoryExtraction(
        claims=(
            ExtractedClaim(
                domain_category="life_story",
                subject_key="self",
                predicate="age",
                value="60",
                confidence=0.95,
            ),
        ),
        extractor_version="single-value-claim-v1",
    )
    with catalog._connect() as connection:
        owner_existing = catalog._existing_single_value_claims(
            connection, event=owner_event, extraction=extraction
        )
        child_existing = catalog._existing_single_value_claims(
            connection,
            event=child_event,
            extraction=replace(
                extraction,
                claims=(replace(extraction.claims[0], value="61"),),
            ),
        )
    assert owner_existing == ("60",)
    assert child_existing == ("61",)


@pytest.mark.asyncio
async def test_default_resolver_still_compiles_a_named_other_subject(tmp_path: Path) -> None:
    """None from the default resolver is unknown, not "do not project".

    A catalog with no evidence resolver used to compile every accepted event.
    Naming another subject must not change that: only an explicit unresolved
    signal skips the projection.
    """

    path = tmp_path / "default-resolver.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="other-subject-fact", text="我习惯先找事实，再讨论责任。")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE evidence_events SET subject_id = ? WHERE event_id = ?",
            ("person-other", "other-subject-fact"),
        )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())

    report = await catalog.compile_pending()

    assert report.compiled_events == 1
    assert report.ignored_events == 0
    queue = await catalog.review_queue(account_id="account-memory")
    assert [item.source_event_id for item in queue] == ["other-subject-fact"]


@pytest.mark.asyncio
async def test_unresolved_other_subject_is_ignored_without_a_projection(
    tmp_path: Path,
) -> None:
    """The sentinel, not None, is what withholds a long-term projection."""

    path = tmp_path / "unresolved-subject.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="owner-fact", text="我习惯先找事实，再讨论责任。")
    await _record(
        archive,
        event_id="missing-person-fact",
        text="我习惯先核对范围，再开始动手。",
        minute=1,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE evidence_events SET subject_id = ? WHERE event_id = ?",
            ("account-memory", "owner-fact"),
        )
        connection.execute(
            "UPDATE evidence_events SET subject_id = ? WHERE event_id = ?",
            ("person-missing", "missing-person-fact"),
        )

    def resolve(event: EvidenceEvent) -> str | None:
        if event.subject_id == "person-missing":
            raise SubjectCategoryUnresolved(event.subject_id)
        return None

    catalog = MemoryCatalog.sqlite(
        path,
        extractor=RuleBasedMemoryExtractor(),
        evidence_subject_category_resolver=resolve,
    )
    report = await catalog.compile_pending()

    assert report.compiled_events == 1
    assert report.ignored_events == 1
    queue = await catalog.review_queue(account_id="account-memory")
    assert [item.source_event_id for item in queue] == ["owner-fact"]
    stored = await archive.event(account_id="account-memory", event_id="missing-person-fact")
    assert stored is not None
    with sqlite3.connect(path) as connection:
        receipt = connection.execute(
            "SELECT outcome FROM memory_compile_receipts WHERE event_id = ?",
            ("missing-person-fact",),
        ).fetchone()
    assert receipt is not None
    assert receipt[0] == "ignored"


@pytest.mark.asyncio
async def test_async_category_resolver_runs_on_the_compiler_loop(tmp_path: Path) -> None:
    """An awaitable resolver is awaited in place, not dispatched to another loop.

    Production identity pools are bound to the loop that created them.  A
    resolver that sees a different loop would skip every non-account subject.
    """

    path = tmp_path / "async-category.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="child-fact", text="我习惯先核对范围，再开始动手。")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE evidence_events SET subject_id = ? WHERE event_id = ?",
            ("person-child", "child-fact"),
        )
    compiler_loop = asyncio.get_running_loop()
    seen_loops: list[asyncio.AbstractEventLoop] = []

    async def resolve(event: EvidenceEvent) -> str | None:
        seen_loops.append(asyncio.get_running_loop())
        assert event.subject_id == "person-child"
        return "adult"

    catalog = MemoryCatalog.sqlite(
        path,
        extractor=RuleBasedMemoryExtractor(),
        evidence_subject_category_resolver=resolve,
    )
    report = await catalog.compile_pending()

    assert seen_loops == [compiler_loop]
    assert report.compiled_events == 1
    assert report.ignored_events == 0
    queue = await catalog.review_queue(account_id="account-memory")
    assert [item.source_event_id for item in queue] == ["child-fact"]


@pytest.mark.asyncio
async def test_explicit_auto_confirmation_uses_a_closed_low_risk_allowlist(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    cases = (
        ("explicit-birth-date", "请记住我出生于1990年1月1日。"),
        ("explicit-marriage", "请记住我结婚了。"),
        ("explicit-health", "请记住我患有糖尿病。"),
        ("explicit-finance", "请记住我的收入是100万元。"),
        ("explicit-legal", "请记住我正在打官司。"),
        ("explicit-biometric", "请记住我的声纹已经录入。"),
        ("explicit-sensitive-suffix", "请记住我喜欢咖啡因为我患有糖尿病。"),
    )
    for minute, (event_id, text) in enumerate(cases):
        await _record(
            archive,
            event_id=event_id,
            text=text,
            minute=minute,
            explicit_memory=True,
        )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())

    await catalog.compile_pending()
    queue = await catalog.review_queue(account_id="account-memory")
    confirmed = await catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            include_candidates=False,
        )
    )

    assert [item.source_event_id for item in queue] == [event_id for event_id, _ in cases]
    assert all(item.status == "candidate" for item in queue)
    assert confirmed.items == ()


@pytest.mark.asyncio
async def test_legacy_v1_policy_confirmation_cannot_promote_a_rebuilt_claim(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await archive.record(
        EvidenceEvent(
            event_id="legacy-v1-source",
            account_id="account-memory",
            session_id="session-memory",
            turn_id=1,
            generation_id=1,
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 8, 7, tzinfo=UTC),
            speaker_class="owner",
            source="legacy-policy-test",
            payload={
                "text": "请记住我喜欢雨天散步。",
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
                "tool_epoch": 0,
                "memory_write_intent": {
                    "kind": "explicit_remember",
                    "policy_version": "explicit-memory-v1",
                },
            },
        )
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    await catalog.compile_pending()
    claim = (await catalog.review_queue(account_id="account-memory"))[0]
    await archive.record(
        EvidenceEvent(
            event_id="legacy-v1-confirmation",
            account_id="account-memory",
            session_id="session-memory",
            turn_id=1,
            generation_id=1,
            event_type="memory.claim_reviewed",
            occurred_at=datetime(2026, 8, 7, tzinfo=UTC),
            speaker_class="system",
            source="system.memory_write_policy",
            payload={
                "target_id": claim.item_id,
                "action": "confirm",
                "previous_value": claim.value,
                "source_event_id": "legacy-v1-source",
                "policy_version": "explicit-memory-v1",
                "reason": "explicit-memory-low-risk",
                "tool_epoch": 0,
            },
        )
    )

    replay = await catalog.compile_pending()
    queue = await catalog.review_queue(account_id="account-memory")

    assert replay.ignored_events == 1
    assert [(item.item_id, item.status) for item in queue] == [(claim.item_id, "candidate")]


@pytest.mark.asyncio
async def test_archive_evidence_review_is_not_replayed_as_a_memory_claim_review(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="archive-review-target",
        text="我在杭州读过书。",
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    await catalog.compile_pending()
    await archive.review(
        MemoryReview(
            review_event_id="archive-review-event",
            account_id="account-memory",
            target_id="archive-review-target",
            action="confirm",
            occurred_at=datetime(2026, 7, 19, 11, 0, tzinfo=UTC),
        )
    )

    report = await catalog.compile_pending()

    assert (report.ignored_events, report.failed_events) == (1, 0)


@pytest.mark.asyncio
async def test_claim_review_moves_same_event_life_projections_without_promoting_other_events(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="reviewed-family-001",
        text="我妈妈叫李梅，我们家的家训是答应别人的事一定做到。",
    )
    await _record(
        archive,
        event_id="unreviewed-life-001",
        text="我在杭州读过书。",
        minute=1,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    await catalog.compile_pending()
    claim = next(
        item
        for item in await catalog.review_queue(account_id="account-memory")
        if item.source_event_id == "reviewed-family-001"
    )

    await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claim.item_id,
            action="confirm",
        )
    )
    confirmed_context = await catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
        )
    )
    confirmed_timeline = await catalog.timeline(account_id="account-memory")
    confirmed_people = await catalog.people(account_id="account-memory")

    assert {(item.kind, item.source_event_id, item.status) for item in confirmed_context.items} == {
        ("claim", "reviewed-family-001", "confirmed"),
        ("knowledge", "reviewed-family-001", "confirmed"),
        ("episode", "reviewed-family-001", "confirmed"),
        # The person named in the confirmed utterance is confirmable too, and
        # its projection is what makes "阿梅是谁？" answerable at all.
        ("person", "reviewed-family-001", "confirmed"),
    }
    assert {(item.source_event_id, item.status) for item in confirmed_timeline} == {
        ("reviewed-family-001", "confirmed"),
        ("unreviewed-life-001", "candidate"),
    }
    assert [(person.display_name, person.status) for person in confirmed_people] == [
        ("李梅", "confirmed")
    ]
    assert set(confirmed_people[0].aliases) == {"妈妈", "母亲", "李梅"}

    await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claim.item_id,
            action="retract",
        )
    )
    retracted_context = await catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
        )
    )
    remaining_timeline = await catalog.timeline(account_id="account-memory")
    remaining_people = await catalog.people(account_id="account-memory")

    assert retracted_context.items == ()
    assert [(item.source_event_id, item.status) for item in remaining_timeline] == [
        ("unreviewed-life-001", "candidate")
    ]
    assert remaining_people == ()


@pytest.mark.asyncio
async def test_retracting_one_source_hides_only_its_alias_from_a_confirmed_person(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="alias-source-001", text="我妈妈叫李梅。")
    await _record(
        archive,
        event_id="alias-source-002",
        text="我也会叫妈妈阿梅。",
        minute=1,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=AliasExtractor())
    await catalog.compile_pending()
    claims = {
        item.source_event_id: item
        for item in await catalog.review_queue(account_id="account-memory")
    }
    await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claims["alias-source-001"].item_id,
            action="confirm",
        )
    )
    partially_confirmed = await catalog.people(account_id="account-memory")
    assert partially_confirmed[0].aliases == ("妈妈",)
    await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claims["alias-source-002"].item_id,
            action="confirm",
        )
    )

    confirmed = await catalog.people(account_id="account-memory")
    assert confirmed[0].status == "confirmed"
    assert set(confirmed[0].aliases) == {"妈妈", "阿梅"}

    await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claims["alias-source-002"].item_id,
            action="retract",
        )
    )
    after_retract = await catalog.people(account_id="account-memory")
    context = await catalog.context(
        MemorySearchQuery(account_id="account-memory", speaker_class="owner")
    )

    assert after_retract[0].status == "confirmed"
    assert after_retract[0].aliases == ("妈妈",)
    assert [item.source_event_id for item in context.items] == ["alias-source-001"]


@pytest.mark.asyncio
async def test_failed_compilation_retries_idempotently_without_duplicate_projection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="retry-001",
        text="我们家的家训是先听完别人说话。",
    )
    extractor = FlakyExtractor()
    catalog = MemoryCatalog.sqlite(path, extractor=extractor)

    first = await catalog.compile_pending()
    second = await catalog.compile_pending()
    third = await catalog.compile_pending()
    claims = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="先听完",
            kinds=("claim",),
        )
    )

    assert (first.failed_events, second.compiled_events) == (1, 1)
    assert third == type(third)()
    assert extractor.attempts == 2
    assert len(claims.items) == 1


@pytest.mark.asyncio
async def test_correction_is_confirmed_searchable_and_written_as_audit_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="correction-001",
        text="我认为做决定前应该等一天。",
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    await catalog.compile_pending()
    claim = (await catalog.review_queue(account_id="account-memory"))[0]

    corrected = await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claim.item_id,
            action="correct",
            corrected_value="我认为做决定前应该等两天。",
        )
    )
    context = await catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="等两天",
        )
    )
    complete_context = await catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
        )
    )
    evidence = await archive.context(
        ContextQuery(
            account_id="account-memory",
            speaker_class="owner",
            text=claim.item_id,
        )
    )

    assert (corrected.status, corrected.value) == (
        "confirmed",
        "我认为做决定前应该等两天。",
    )
    assert [item.item_id for item in context.items] == [claim.item_id]
    assert [(item.kind, item.title) for item in complete_context.items] == [
        ("claim", "我认为做决定前应该等两天。")
    ]
    assert evidence.evidence[0].event_type == "memory.claim_reviewed"
    assert evidence.evidence[0].payload["previous_value"] == "我认为做决定前应该等一天。"


@pytest.mark.asyncio
async def test_related_turns_share_an_episode_but_keep_individual_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="episode-work-001",
        text="做项目时，我先确认目标。",
    )
    await _record(
        archive,
        event_id="episode-work-002",
        text="项目复盘时，我再记录偏差。",
        minute=1,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())

    await catalog.compile_pending()
    timeline = await catalog.timeline(account_id="account-memory")

    assert len(timeline) == 2
    assert len({item.episode_id for item in timeline}) == 1
    assert {item.source_event_id for item in timeline} == {
        "episode-work-001",
        "episode-work-002",
    }
    episode = (
        await catalog.search(
            MemorySearchQuery(
                account_id="account-memory",
                speaker_class="owner",
                kinds=("episode",),
            )
        )
    ).items[0]
    assert episode.memory_kind == "episodic"
    assert episode.source_event_ids == ("episode-work-001", "episode-work-002")
    assert episode.stability > 0.5


@pytest.mark.asyncio
async def test_person_alias_is_recallable_and_stays_gated_by_status(tmp_path: Path) -> None:
    """A confirmed nickname must be able to answer "who is that?".

    The alias lives in ``person_aliases``, which no read path searches, so the
    person needs a search projection or the owner can confirm the utterance and
    still never recall it by the name the family actually uses. Candidate
    people stay out of the confirmed-only context.
    """

    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="alias-person-001",
        text="我妈妈叫李梅，家里人也叫她阿梅。",
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    await catalog.compile_pending()

    people = await catalog.people(account_id="account-memory")
    assert [(person.display_name, person.status) for person in people] == [
        ("李梅", "candidate")
    ]
    assert "阿梅" in people[0].aliases

    context = await catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="阿梅是谁？",
        )
    )
    assert context.items == (), "a candidate person must stay out of confirmed recall"

    search = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
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
