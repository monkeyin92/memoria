from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.episode_consolidator import (
    EpisodeCandidate,
    EpisodeConsolidator,
    ExistingEpisode,
)
from services.archive.life_archive import LifeArchive
from services.archive.memory_catalog import MemoryCatalog
from services.archive.memory_domain import (
    ExtractedClaim,
    ExtractedTimeline,
    MemoryClaimReview,
    MemoryExtraction,
    MemorySearchQuery,
)


class CanonicalEpisodeExtractor:
    version = "canonical-episode-test-v1"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        text = str(event.payload["text"])
        return MemoryExtraction(
            claims=(
                ExtractedClaim(
                    domain_category="work_experience",
                    subject_key="self",
                    predicate="work_experience",
                    value=text,
                    confidence=0.8,
                ),
            ),
            timeline=(
                ExtractedTimeline(
                    title=text,
                    domain_category="work_experience",
                    event_start=datetime(2013, 4, 1, tzinfo=UTC),
                    event_end=datetime(2013, 12, 31, tzinfo=UTC),
                    canonical_key="university-campus-delivery-startup",
                ),
            ),
            extractor_version=self.version,
        )


def _candidate(
    *,
    title: str,
    start: datetime,
    canonical_key: str = "",
    entity_ids: tuple[str, ...] = (),
) -> EpisodeCandidate:
    return EpisodeCandidate(
        account_id="episode-account",
        source_event_id="candidate-source",
        session_id="new-session",
        title=title,
        domain_category="work_experience",
        event_start=start,
        event_end=None,
        canonical_key=canonical_key,
        entity_ids=entity_ids,
        salience=0.7,
        sensitivity="personal",
    )


def test_canonical_key_merges_the_same_episode_across_sessions() -> None:
    start = datetime(2013, 4, 1, tzinfo=UTC)
    existing = ExistingEpisode(
        episode_id="episode-campus-startup",
        consolidation_key=(
            "canonical:work_experience:universitycampusdeliverystartup"
        ),
        title="大学期间做校园外卖创业",
        domain_category="work_experience",
        event_start=start,
        event_end=datetime(2013, 12, 31, tzinfo=UTC),
        entity_ids=(),
    )

    selected = EpisodeConsolidator().choose(
        _candidate(
            title="那次创业失败让我以后更重视现金流",
            start=start + timedelta(days=200),
            canonical_key="university-campus-delivery-startup",
        ),
        (existing,),
    )

    assert selected == existing


def test_canonical_keys_are_scoped_by_domain_category() -> None:
    work = _candidate(
        title="校园外卖创业",
        start=datetime(2013, 4, 1, tzinfo=UTC),
        canonical_key="campus-delivery",
    )
    life = EpisodeCandidate(
        account_id=work.account_id,
        source_event_id="life-source",
        session_id=work.session_id,
        title=work.title,
        domain_category="life_story",
        event_start=work.event_start,
        event_end=work.event_end,
        canonical_key=work.canonical_key,
        entity_ids=work.entity_ids,
        salience=work.salience,
        sensitivity=work.sensitivity,
    )

    assert work.fallback_key != life.fallback_key


def test_similar_titles_do_not_merge_distinct_events_with_distant_time_and_entities() -> None:
    existing = ExistingEpisode(
        episode_id="episode-2013",
        consolidation_key="legacy:episode-2013",
        title="参加公司年度项目验收",
        domain_category="work_experience",
        event_start=datetime(2013, 7, 1, tzinfo=UTC),
        event_end=None,
        entity_ids=("company-old",),
    )

    selected = EpisodeConsolidator().choose(
        _candidate(
            title="参加公司年度项目验收",
            start=datetime(2026, 7, 1, tzinfo=UTC),
            entity_ids=("company-new",),
        ),
        (existing,),
    )

    assert selected is None


def test_same_session_domain_and_day_do_not_force_distinct_events_to_merge() -> None:
    start = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)
    first = _candidate(
        title="上午参加项目验收",
        start=start,
    )
    existing = ExistingEpisode(
        episode_id="episode-morning-review",
        consolidation_key=first.fallback_key,
        title=first.title,
        domain_category=first.domain_category,
        event_start=first.event_start,
        event_end=first.event_end,
        entity_ids=first.entity_ids,
    )

    selected = EpisodeConsolidator().choose(
        _candidate(
            title="晚上和大学同学聚餐",
            start=start + timedelta(hours=10),
        ),
        (existing,),
    )

    assert selected is None


def test_same_session_related_turns_can_extend_one_episode_without_a_canonical_key() -> None:
    start = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)
    first = _candidate(
        title="做项目时，我先确认目标。",
        start=start,
    )
    existing = ExistingEpisode(
        episode_id="episode-project-review",
        consolidation_key=first.fallback_key,
        title=first.title,
        domain_category=first.domain_category,
        event_start=first.event_start,
        event_end=first.event_end,
        entity_ids=first.entity_ids,
    )

    selected = EpisodeConsolidator().choose(
        _candidate(
            title="项目复盘时，我再记录偏差。",
            start=start + timedelta(minutes=1),
        ),
        (existing,),
    )

    assert selected == existing


def test_explicit_disjoint_entities_block_an_implicit_same_session_merge() -> None:
    start = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)
    first = _candidate(
        title="参加公司年度项目验收",
        start=start,
        entity_ids=("company-old",),
    )
    existing = ExistingEpisode(
        episode_id="episode-old-company",
        consolidation_key=first.fallback_key,
        title=first.title,
        domain_category=first.domain_category,
        event_start=first.event_start,
        event_end=first.event_end,
        entity_ids=first.entity_ids,
    )

    selected = EpisodeConsolidator().choose(
        _candidate(
            title="参加公司年度项目验收",
            start=start + timedelta(hours=2),
            entity_ids=("company-new",),
        ),
        (existing,),
    )

    assert selected is None


async def _record(
    archive: LifeArchive,
    *,
    event_id: str,
    session_id: str,
    text: str,
    minute: int,
) -> None:
    await archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id="episode-account",
            session_id=session_id,
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 7, 28, 10, minute, tzinfo=UTC),
            speaker_class="owner",
            source="episode-test",
            payload={
                "text": text,
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
            },
        )
    )


@pytest.mark.asyncio
async def test_cross_session_evidence_consolidates_and_retraction_only_hides_its_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(
        archive,
        event_id="episode-source-start",
        session_id="episode-session-a",
        text="大学时我和老王开始做校园外卖创业。",
        minute=0,
    )
    await _record(
        archive,
        event_id="episode-source-outcome",
        session_id="episode-session-b",
        text="那个项目后来失败了，让我以后特别重视现金流。",
        minute=1,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=CanonicalEpisodeExtractor())
    await catalog.compile_pending()
    queue = await catalog.review_queue(account_id="episode-account")
    claims = {item.source_event_id: item for item in queue}
    for source_event_id in ("episode-source-start", "episode-source-outcome"):
        await catalog.review(
            MemoryClaimReview(
                account_id="episode-account",
                claim_id=claims[source_event_id].item_id,
                action="confirm",
            )
        )

    before = await catalog.context(
        MemorySearchQuery(
            account_id="episode-account",
            speaker_class="owner",
            kinds=("episode",),
        )
    )
    assert len(before.items) == 1
    assert set(before.items[0].source_event_ids) == {
        "episode-source-start",
        "episode-source-outcome",
    }
    assert "校园外卖创业" in before.items[0].snippet
    assert "重视现金流" in before.items[0].snippet

    await catalog.review(
        MemoryClaimReview(
            account_id="episode-account",
            claim_id=claims["episode-source-start"].item_id,
            action="retract",
        )
    )
    after_one = await catalog.context(
        MemorySearchQuery(
            account_id="episode-account",
            speaker_class="owner",
            kinds=("episode",),
        )
    )

    assert len(after_one.items) == 1
    assert after_one.items[0].status == "confirmed"
    assert set(after_one.items[0].source_event_ids) == {
        "episode-source-start",
        "episode-source-outcome",
    }
    assert "校园外卖创业" not in after_one.items[0].snippet
    assert "重视现金流" in after_one.items[0].snippet
    assert after_one.items[0].stability == 0.5

    await catalog.review(
        MemoryClaimReview(
            account_id="episode-account",
            claim_id=claims["episode-source-outcome"].item_id,
            action="retract",
        )
    )
    after_all = await catalog.context(
        MemorySearchQuery(
            account_id="episode-account",
            speaker_class="owner",
            kinds=("episode",),
        )
    )
    assert after_all.items == ()
