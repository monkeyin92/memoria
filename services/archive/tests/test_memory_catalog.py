from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.domain import ContextQuery, EvidenceEvent, MemoryReview
from services.archive.life_archive import LifeArchive
from services.archive.memory_catalog import MemoryCatalog
from services.archive.memory_domain import (
    AccountWriteRejectedError,
    ExtractedClaim,
    ExtractedPerson,
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
                    category="daily_life",
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
) -> None:
    await archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id="account-memory",
            session_id="session-memory",
            turn_id=minute + 1,
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 7, 19, 10, minute, tzinfo=UTC),
            speaker_class=speaker_class,  # type: ignore[arg-type]
            source="test",
            payload={
                "text": text,
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": speaker_class == "owner",
            },
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
        await catalog.search(
            MemorySearchQuery(account_id="account-memory", speaker_class="owner")
        )
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
    context = await catalog.context(
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
        ("timeline", "confirmed"),
    }
    assert after_retract.items == ()


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

    assert {
        (item.kind, item.source_event_id, item.status) for item in confirmed_context.items
    } == {
        ("claim", "reviewed-family-001", "confirmed"),
        ("knowledge", "reviewed-family-001", "confirmed"),
        ("timeline", "reviewed-family-001", "confirmed"),
    }
    assert {
        (item.source_event_id, item.status) for item in confirmed_timeline
    } == {
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
    for source_event_id in ("alias-source-001", "alias-source-002"):
        await catalog.review(
            MemoryClaimReview(
                account_id="account-memory",
                claim_id=claims[source_event_id].item_id,
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
