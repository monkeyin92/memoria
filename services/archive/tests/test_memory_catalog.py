"""Memory catalog compile/search/review contract on PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
from services.archive.domain import ContextQuery, EvidenceEvent, MemoryReview
from services.archive.memory_catalog import SubjectCategoryUnresolved
from services.archive.memory_domain import (
    AccountWriteRejectedError,
    ExtractedClaim,
    ExtractedKnowledge,
    ExtractedPerson,
    ExtractedTimeline,
    MemoryClaimReview,
    MemoryExtraction,
    MemorySearchQuery,
    content_query_terms,
    edge_content_query_terms,
    lexical_query_terms,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog

MakeCatalog = Callable[..., Awaitable[PostgresMemoryCatalog]]
OwnerSql = Callable[..., list[tuple[Any, ...]]]


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
    archive: PostgresLifeArchive,
    *,
    event_id: str,
    text: str,
    speaker_class: str = "owner",
    minute: int = 0,
    explicit_memory: bool = False,
    account_id: str = "account-memory",
    subject_id: str | None = None,
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
            account_id=account_id,
            subject_id=subject_id,
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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(archive, event_id="fenced-memory", text="这条记忆来得太晚。")
    extractor = CountingExtractor()
    catalog = await make_catalog(
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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
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
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())

    report = await catalog.compile_pending()
    owner_search = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="家训",
            include_candidates=True,
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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
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
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())

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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(
        archive,
        event_id="wisdom-001",
        text="我认为做重大决定前应该睡一晚再答复。",
    )
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())
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
async def test_search_defaults_to_confirmed_non_conflicting_claims(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(archive, event_id="search-age-60", text="请记住我今年60岁。")
    await _record(archive, event_id="search-age-61", text="请记住我今年61岁。", minute=1)
    catalog = await make_catalog(extractor=SingleValueClaimExtractor())
    await catalog.compile_pending()

    default_search = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            kinds=("claim",),
        )
    )
    candidate_search = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            kinds=("claim",),
            include_candidates=True,
        )
    )

    assert default_search.items == ()
    assert {
        (item.source_event_id, item.status, item.conflict_state)
        for item in candidate_search.items
    } == {
        ("search-age-60", "candidate", "active"),
        ("search-age-61", "candidate", "active"),
    }

    claims = {
        item.source_event_id: item.item_id
        for item in await catalog.review_queue(account_id="account-memory")
    }
    await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claims["search-age-60"],
            action="confirm",
        )
    )
    await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claims["search-age-61"],
            action="dispute",
        )
    )

    conflicted_default = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            kinds=("claim",),
        )
    )
    conflicted_context = await catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            kinds=("claim",),
            include_candidates=True,
        )
    )
    conflicted_opt_in = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            kinds=("claim",),
            include_candidates=True,
        )
    )

    assert conflicted_default.items == ()
    assert conflicted_context.items == ()
    assert {
        (item.source_event_id, item.status, item.conflict_state)
        for item in conflicted_opt_in.items
    } == {
        ("search-age-60", "confirmed", "active"),
        ("search-age-61", "disputed", "active"),
    }

    await catalog.review(
        MemoryClaimReview(
            account_id="account-memory",
            claim_id=claims["search-age-61"],
            action="retract",
        )
    )
    resolved_default = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            kinds=("claim",),
        )
    )
    resolved_context = await catalog.context(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            kinds=("claim",),
        )
    )

    assert [(item.source_event_id, item.status, item.conflict_state) for item in resolved_default.items] == [
        ("search-age-60", "confirmed", "none")
    ]
    assert [(item.source_event_id, item.status) for item in resolved_context.items] == [
        ("search-age-60", "confirmed")
    ]


@pytest.mark.asyncio
async def test_contextual_source_text_supports_a_natural_cross_day_recall_query(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(
        archive,
        event_id="contextual-source-001",
        text="前几天聊到旅行，我说下次想去南京。",
    )
    catalog = await make_catalog(extractor=ContextualClaimExtractor())
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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(
        archive,
        event_id="contextual-projections-001",
        text="前几天聊到旅行，最后还是想去南京散散心。",
    )
    catalog = await make_catalog(extractor=ContextualClaimExtractor())
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


def test_content_query_terms_drop_function_word_ngrams_but_never_everything() -> None:
    terms = lexical_query_terms("那时候最难忘的事是什么？")

    assert {"什么", "是什么"} <= set(terms)
    content = content_query_terms(terms)
    assert "什么" not in content and "是什么" not in content
    assert {"难忘", "时候"} <= set(content)
    # A query made only of function words keeps its terms rather than matching nothing.
    assert content_query_terms(lexical_query_terms("那是什么")) == lexical_query_terms("那是什么")


def test_edge_content_query_terms_keep_named_things_and_drop_word_fragments() -> None:
    terms = lexical_query_terms("家里的橘色小猫怎么称呼？")
    edge = edge_content_query_terms(terms)

    # "里的" straddles a word boundary (家里的 / 客厅里的): a fragment, not a name.
    assert "里的" in terms and "里的" not in edge
    assert {"橘色", "小猫", "称呼"} <= set(edge)
    assert all(term[0] not in "的了么什怎" and term[-1] not in "的了么什怎" for term in edge)
    # Names, years and terms of art keep their exact-match value.
    assert {"刘老师", "刘老", "老师"} <= set(edge_content_query_terms(lexical_query_terms("刘老师是我的什么人？")))
    assert "2016" in edge_content_query_terms(lexical_query_terms("2016年发生了什么？"))
    # Unlike content_query_terms this may return nothing; embeddings still rank the query.
    assert edge_content_query_terms(lexical_query_terms("那是什么")) == ()


@pytest.mark.asyncio
async def test_lexical_rank_ignores_function_words_in_knowledge_boilerplate(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    """Without embeddings, "是什么" must not lift the rule knowledge question over the claim."""

    await _record(archive, event_id="lexical-bank-001", text="我在银行工作了十年。")
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())
    await catalog.compile_pending()

    result = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="在银行工作最难忘的是什么？",
            include_candidates=True,
        )
    )

    kinds = [item.kind for item in result.items]
    assert sorted(kinds) == ["claim", "episode", "knowledge"]
    assert kinds[0] != "knowledge"
    # Every projection repeats the sentence, so each hits the same content terms.
    assert len({int(item.score) for item in result.items}) == 1


@pytest.mark.asyncio
async def test_explicit_low_sensitivity_memory_is_immediately_confirmed(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(
        archive,
        event_id="explicit-memory-low-risk",
        text="请记住我喜欢雨天散步。",
        explicit_memory=True,
    )
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())

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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(
        archive,
        event_id="minor-family-conflict",
        text="我和爸爸最近总吵架。",
    )
    catalog = await make_catalog(
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
async def test_minor_projection_keeps_safe_study_progress(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(
        archive,
        event_id="minor-study-progress",
        text="我今天练习了英语口语，过去式还是薄弱点。",
    )
    catalog = await make_catalog(
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
async def test_minor_projection_drops_study_sentence_with_teacher_scolding(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(
        archive,
        event_id="minor-scolded-math",
        text="我今天练习了数学，被老师骂了。",
    )
    catalog = await make_catalog(
        extractor=RuleBasedMemoryExtractor(),
        subject_category_resolver=lambda _: "minor",
    )

    report = await catalog.compile_pending()
    context = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="数学 老师",
            include_candidates=True,
        )
    )

    assert report.failed_events == 0
    assert context.items == ()


@pytest.mark.asyncio
async def test_minor_explicit_daily_preference_confirms_without_sensitive_capture(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(
        archive,
        event_id="minor-reading",
        text="请帮我记住我喜欢阅读。",
        explicit_memory=True,
    )
    await _record(
        archive,
        event_id="minor-criticized",
        text="我今天被老师批评了，好难过。",
        minute=1,
    )
    await _record(
        archive,
        event_id="minor-family",
        text="我和爸爸最近总吵架。",
        minute=2,
    )
    catalog = await make_catalog(
        extractor=RuleBasedMemoryExtractor(),
        subject_category_resolver=lambda _: "minor",
    )

    report = await catalog.compile_pending()
    confirmed = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="阅读",
            include_candidates=False,
        )
    )
    sensitive = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="难过 爸爸",
            include_candidates=True,
        )
    )

    assert report.failed_events == 0
    assert {(item.domain_category, item.status, item.source_event_id) for item in confirmed.items} == {
        ("daily_life", "confirmed", "minor-reading")
    }
    assert sensitive.items == ()


@pytest.mark.asyncio
async def test_explicit_sensitive_or_conflicting_memory_stays_in_review_queue(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
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
    sensitive_catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())
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

    await _record(
        archive,
        event_id="explicit-age-60",
        text="请记住我今年60岁。",
        explicit_memory=True,
        account_id="account-conflict",
    )
    conflict_catalog = await make_catalog(extractor=SingleValueClaimExtractor())
    await conflict_catalog.compile_pending()
    await _record(
        archive,
        event_id="explicit-age-61",
        text="请记住我今年61岁。",
        minute=1,
        explicit_memory=True,
        account_id="account-conflict",
    )
    await conflict_catalog.compile_pending()

    queue = await conflict_catalog.review_queue(account_id="account-conflict")
    assert [(item.value, item.status) for item in queue] == [
        ("60", "candidate"),
        ("61", "candidate"),
    ]


@pytest.mark.asyncio
async def test_single_value_claims_do_not_conflict_across_subjects(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
    owner_sql: OwnerSql,
    archive_dsn: str,
) -> None:
    """Two self-claims that name different speakers are not one fact.

    Both rows stay under subject_key="self" inside the login account.  The
    child's age must neither mark the owner's claim as a conflict nor count as
    an existing value that blocks the owner's own confirmation.
    """

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
    catalog = await make_catalog(
        extractor=SingleValueClaimExtractor(),
        # Both speakers are adults here.  The point of the test is the fold,
        # not the minor allowlist; a missing identity must not be what hides
        # the child's row.
        evidence_subject_category_resolver=lambda _event: "adult",
    )
    await catalog.compile_pending()

    rows = owner_sql(
        "SELECT source_event_id, value, conflict_state, status FROM memory_claims"
        " WHERE predicate = 'age' ORDER BY source_event_id"
    )
    by_event = {
        str(event_id): {"value": value, "conflict_state": conflict, "status": status}
        for event_id, value, conflict, status in rows
    }
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
    connection = await asyncpg.connect(archive_dsn)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.account_id', $1, true)", "account-memory"
            )
            owner_existing = await PostgresMemoryCatalog._existing_single_value_claims(
                connection, event=owner_event, extraction=extraction
            )
            child_existing = await PostgresMemoryCatalog._existing_single_value_claims(
                connection,
                event=child_event,
                extraction=replace(
                    extraction,
                    claims=(replace(extraction.claims[0], value="61"),),
                ),
            )
    finally:
        await connection.close()
    assert owner_existing == ("60",)
    assert child_existing == ("61",)


@pytest.mark.asyncio
async def test_default_resolver_still_compiles_a_named_other_subject(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    """None from the default resolver is unknown, not "do not project".

    A catalog with no evidence resolver used to compile every accepted event.
    Naming another subject must not change that: only an explicit unresolved
    signal skips the projection.
    """

    await _record(
        archive,
        event_id="other-subject-fact",
        text="我习惯先找事实，再讨论责任。",
        subject_id="person-other",
    )
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())

    report = await catalog.compile_pending()

    assert report.compiled_events == 1
    assert report.ignored_events == 0
    queue = await catalog.review_queue(account_id="account-memory")
    assert [item.source_event_id for item in queue] == ["other-subject-fact"]


@pytest.mark.asyncio
async def test_unresolved_other_subject_is_ignored_without_a_projection(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
    owner_sql: OwnerSql,
) -> None:
    """The sentinel, not None, is what withholds a long-term projection."""

    await _record(
        archive,
        event_id="owner-fact",
        text="我习惯先找事实，再讨论责任。",
        subject_id="account-memory",
    )
    await _record(
        archive,
        event_id="missing-person-fact",
        text="我习惯先核对范围，再开始动手。",
        minute=1,
        subject_id="person-missing",
    )

    def resolve(event: EvidenceEvent) -> str | None:
        if event.subject_id == "person-missing":
            raise SubjectCategoryUnresolved(event.subject_id)
        return None

    catalog = await make_catalog(
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
    receipts = owner_sql(
        "SELECT outcome FROM memory_compile_receipts WHERE event_id = %s",
        "missing-person-fact",
    )
    assert receipts == [("ignored",)]


@pytest.mark.asyncio
async def test_async_category_resolver_runs_on_the_compiler_loop(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    """An awaitable resolver is awaited in place, not dispatched to another loop.

    Production identity pools are bound to the loop that created them.  A
    resolver that sees a different loop would skip every non-account subject.
    """

    await _record(
        archive,
        event_id="child-fact",
        text="我习惯先核对范围，再开始动手。",
        subject_id="person-child",
    )
    compiler_loop = asyncio.get_running_loop()
    seen_loops: list[asyncio.AbstractEventLoop] = []

    async def resolve(event: EvidenceEvent) -> str | None:
        seen_loops.append(asyncio.get_running_loop())
        assert event.subject_id == "person-child"
        return "adult"

    catalog = await make_catalog(
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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
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
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())

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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
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
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())
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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(
        archive,
        event_id="archive-review-target",
        text="我在杭州读过书。",
    )
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())
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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
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
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())
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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(archive, event_id="alias-source-001", text="我妈妈叫李梅。")
    await _record(
        archive,
        event_id="alias-source-002",
        text="我也会叫妈妈阿梅。",
        minute=1,
    )
    catalog = await make_catalog(extractor=AliasExtractor())
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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(
        archive,
        event_id="retry-001",
        text="我们家的家训是先听完别人说话。",
    )
    extractor = FlakyExtractor()
    # The outbox backs a failed task off before it may be claimed again; keep
    # that delay short so the retry is observable here.
    catalog = await make_catalog(
        extractor=extractor, outbox_retry_base_s=0.1, outbox_retry_max_s=0.1
    )

    first = await catalog.compile_pending()
    backed_off = await catalog.compile_pending()
    await asyncio.sleep(0.2)
    second = await catalog.compile_pending()
    third = await catalog.compile_pending()
    claims = await catalog.search(
        MemorySearchQuery(
            account_id="account-memory",
            speaker_class="owner",
            text="先听完",
            kinds=("claim",),
            include_candidates=True,
        )
    )

    assert (first.failed_events, second.compiled_events) == (1, 1)
    assert backed_off == third == type(third)()
    assert extractor.attempts == 2
    assert len(claims.items) == 1


@pytest.mark.asyncio
async def test_correction_is_confirmed_searchable_and_written_as_audit_evidence(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    await _record(
        archive,
        event_id="correction-001",
        text="我认为做决定前应该等一天。",
    )
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())
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
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
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
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())

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
                include_candidates=True,
            )
        )
    ).items[0]
    assert episode.memory_kind == "episodic"
    assert episode.source_event_ids == ("episode-work-001", "episode-work-002")
    assert episode.stability > 0.5


@pytest.mark.asyncio
async def test_person_alias_is_recallable_and_stays_gated_by_status(
    archive: PostgresLifeArchive,
    make_catalog: MakeCatalog,
) -> None:
    """A confirmed nickname must be able to answer "who is that?".

    The alias lives in ``person_aliases``, which no read path searches, so the
    person needs a search projection or the owner can confirm the utterance and
    still never recall it by the name the family actually uses. Candidate
    people stay out of the confirmed-only context.
    """

    await _record(
        archive,
        event_id="alias-person-001",
        text="我妈妈叫李梅，家里人也叫她阿梅。",
    )
    catalog = await make_catalog(extractor=RuleBasedMemoryExtractor())
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
