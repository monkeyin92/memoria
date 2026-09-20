"""Subject-scoped read contracts for the archive ledger and the catalog.

The first isolation batch keeps every projection account-keyed, so an explicit
``subject_id`` is enforced from the evidence lineage instead of a stored scope
column.  In this project NULL means "no confirmed speaker", never "the account
owner", and an item merged from several sessions is hidden as a whole unless
every one of its sources belongs to the requested subject.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.archive.domain import (
    ContextQuery,
    EvidenceEvent,
    EvidenceNotFoundError,
    MemoryReview,
)
from services.archive.life_archive import LifeArchive
from services.archive.memory_catalog import MemoryCatalog
from services.archive.memory_domain import (
    MemoryClaimReview,
    MemoryExtraction,
    MemorySearchQuery,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog

_ACCOUNT = "account-subject-scope"
_WORK_A = "做项目复盘时，我习惯先找事实，再讨论责任。"
_WORK_B = "这个项目收尾阶段，客户要求重新评估范围。"
_MOTHER_A = "我妈妈叫李梅，今年60岁。"
_MOTHER_B = "我母亲李梅今年61岁了。"


class PinnedEpisodeExtractor:
    """Rule-based extraction pinned to one episode identity.

    Two events sharing the canonical key consolidate into one episode even when
    they carry different speakers, which is exactly the cross-subject merge the
    scope has to catch.
    """

    version = "subject-scope-episode-v1"

    def __init__(self) -> None:
        self._delegate = RuleBasedMemoryExtractor()

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        extraction = await self._delegate.extract(event)
        return replace(
            extraction,
            timeline=tuple(
                replace(item, canonical_key="shared-subject-episode")
                for item in extraction.timeline
            ),
            extractor_version=self.version,
        )


async def _record(
    archive: Any,
    *,
    event_id: str,
    text: str,
    subject_id: str | None,
    account_id: str = _ACCOUNT,
    session_id: str = "session-shared",
    minute: int = 0,
) -> None:
    await archive.record(
        EvidenceEvent(
            event_id=event_id,
            account_id=account_id,
            session_id=session_id,
            turn_id=minute + 1,
            generation_id=minute + 1,
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 8, 1, 10, minute, tzinfo=UTC),
            speaker_class="owner",
            source="subject-scope-test",
            subject_id=subject_id,
            payload={
                "text": text,
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
            },
        )
    )


def _claim_row(path: Path, claim_id: str) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM memory_claims WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _episode_evidence_statuses(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT source_event_id, status FROM episode_evidence"
        ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _timeline_statuses(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT source_event_id, status FROM timeline_entries"
        ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _age_claim_ids(path: Path) -> dict[str, str]:
    """Age-claim ids keyed by extracted value ("60", "61", ...)."""

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT claim_id, value FROM memory_claims WHERE predicate = 'age'"
        ).fetchall()
    return {str(row[1]): str(row[0]) for row in rows}


def _claim_id_for_event(path: Path, *, account_id: str, source_event_id: str) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT claim_id FROM memory_claims"
            " WHERE account_id = ? AND source_event_id = ?",
            (account_id, source_event_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _episode_id_for_event(path: Path, *, account_id: str, source_event_id: str) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT episode_id FROM episode_evidence"
            " WHERE account_id = ? AND source_event_id = ?",
            (account_id, source_event_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _link_event_to_episode(
    path: Path,
    *,
    account_id: str,
    episode_id: str,
    source_event_id: str,
) -> None:
    """Point an episode at one more evidence row inside the same account."""

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO episode_evidence"
            " (episode_id, account_id, source_event_id, status)"
            " VALUES (?, ?, ?, 'candidate')",
            (episode_id, account_id, source_event_id),
        )


def _merge_source_into_claim_document(
    path: Path,
    *,
    claim_id: str,
    source_event_id: str,
) -> None:
    """Simulate a claim projection merged from a second speaker's event."""

    with sqlite3.connect(path) as connection:
        document = connection.execute(
            "SELECT document_id FROM memory_search_documents"
            " WHERE kind = 'claim' AND item_id = ?",
            (claim_id,),
        ).fetchone()
        assert document is not None
        connection.execute(
            "INSERT OR IGNORE INTO memory_search_document_sources"
            " (document_id, account_id, source_event_id) VALUES (?, ?, ?)",
            (str(document[0]), _ACCOUNT, source_event_id),
        )


def _database_dsn(dsn: str, database: str) -> str:
    """Point a DSN at a throwaway database without touching the shared one."""

    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    credentials = f"{quote(parsed.username or '')}:{quote(parsed.password or '')}@"
    return urlunsplit((parsed.scheme, f"{credentials}{host}", f"/{database}", parsed.query, ""))


async def _drop_database(admin_dsn: str, database: str) -> None:
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture
async def scoped_postgres_dsn() -> AsyncIterator[str]:
    """A throwaway database: the shared test database is never written to."""

    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_subject_scope_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    try:
        yield _database_dsn(admin_dsn, database)
    finally:
        await _drop_database(admin_dsn, database)


@pytest.mark.asyncio
async def test_context_subject_scope_excludes_unclaimed_and_foreign_turns(
    tmp_path: Path,
) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    await _record(archive, event_id="ctx-a", text="甲说的话。", subject_id="subject-a")
    await _record(
        archive,
        event_id="ctx-b",
        text="乙说的话。",
        subject_id="subject-b",
        minute=1,
    )
    await _record(
        archive,
        event_id="ctx-none",
        text="没有确认说话人。",
        subject_id=None,
        minute=2,
    )

    scoped = await archive.context(
        ContextQuery(account_id=_ACCOUNT, speaker_class="owner", subject_id="subject-a")
    )
    assert [item.event_id for item in scoped.evidence] == ["ctx-a"]

    # An unclaimed speaker is not the account owner either.
    unclaimed = await archive.context(
        ContextQuery(account_id=_ACCOUNT, speaker_class="owner", subject_id="unknown")
    )
    assert unclaimed.evidence == ()

    # No scope keeps the account-wide internal behaviour, NULL rows included.
    account_wide = await archive.context(
        ContextQuery(account_id=_ACCOUNT, speaker_class="owner")
    )
    assert {item.event_id for item in account_wide.evidence} == {
        "ctx-a",
        "ctx-b",
        "ctx-none",
    }

    with pytest.raises(ValueError):
        ContextQuery(account_id=_ACCOUNT, speaker_class="owner", subject_id="   ")


@pytest.mark.asyncio
async def test_context_owner_branch_narrows_session_before_the_limit(
    tmp_path: Path,
) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    await _record(
        archive,
        event_id="session-target-turn",
        text="目标会话里最早的一句话。",
        subject_id="subject-a",
        session_id="session-target",
    )
    for index in (1, 2, 3):
        await _record(
            archive,
            event_id=f"session-other-{index}",
            text="其他会话里更新的一句话。",
            subject_id="subject-a",
            session_id="session-other",
            minute=index,
        )

    scoped = await archive.context(
        ContextQuery(
            account_id=_ACCOUNT,
            speaker_class="owner",
            session_id="session-target",
            limit=1,
        )
    )

    assert [item.event_id for item in scoped.evidence] == ["session-target-turn"]


@pytest.mark.asyncio
async def test_evidence_window_subject_scope_excludes_unclaimed_speakers(
    tmp_path: Path,
) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    await _record(archive, event_id="window-a", text="甲的窗口事件。", subject_id="subject-a")
    await _record(
        archive,
        event_id="window-b",
        text="乙的窗口事件。",
        subject_id="subject-b",
        minute=1,
    )
    await _record(
        archive,
        event_id="window-none",
        text="未确认说话人的窗口事件。",
        subject_id=None,
        minute=2,
    )
    window = {
        "occurred_after": datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        "occurred_before": datetime(2026, 8, 1, 11, 0, tzinfo=UTC),
    }

    scoped = await archive.evidence_window(account_id=_ACCOUNT, subject_id="subject-a", **window)
    assert [item.event_id for item in scoped] == ["window-a"]

    account_wide = await archive.evidence_window(account_id=_ACCOUNT, **window)
    assert {item.event_id for item in account_wide} == {
        "window-a",
        "window-b",
        "window-none",
    }

    with pytest.raises(ValueError):
        await archive.evidence_window(account_id=_ACCOUNT, subject_id="", **window)


@pytest.mark.asyncio
async def test_catalog_hides_an_episode_merged_across_subjects(tmp_path: Path) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="merge-a", text=_WORK_A, subject_id="subject-a")
    await _record(
        archive,
        event_id="merge-b",
        text=_WORK_B,
        subject_id="subject-b",
        minute=1,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=PinnedEpisodeExtractor())

    report = await catalog.compile_pending()
    assert report.compiled_events == 2

    unscoped_timeline = await catalog.timeline(account_id=_ACCOUNT)
    assert {item.source_event_id for item in unscoped_timeline} == {"merge-a", "merge-b"}
    assert len({item.episode_id for item in unscoped_timeline}) == 1

    # One shared episode: neither speaker may read it once the scope is explicit.
    assert await catalog.timeline(account_id=_ACCOUNT, subject_id="subject-a") == ()
    assert await catalog.timeline(account_id=_ACCOUNT, subject_id="subject-b") == ()

    unscoped = await catalog.search(
        MemorySearchQuery(account_id=_ACCOUNT, speaker_class="owner")
    )
    assert any(item.kind == "episode" for item in unscoped.items)

    scoped = await catalog.search(
        MemorySearchQuery(account_id=_ACCOUNT, speaker_class="owner", subject_id="subject-a")
    )
    assert scoped.items
    assert all(item.kind != "episode" for item in scoped.items)
    assert all("merge-b" not in item.source_event_ids for item in scoped.items)


@pytest.mark.asyncio
async def test_people_hide_a_person_when_one_alias_came_from_another_subject(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="person-a", text=_MOTHER_A, subject_id="subject-a")
    await _record(
        archive,
        event_id="person-b",
        text=_MOTHER_B,
        subject_id="subject-b",
        minute=1,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())

    await catalog.compile_pending()

    unscoped = await catalog.people(account_id=_ACCOUNT)
    assert [person.display_name for person in unscoped] == ["李梅"]
    assert set(unscoped[0].aliases) >= {"妈妈", "母亲", "李梅"}

    # The person row was created by subject A, but an alias arrived from B: the
    # merged identity is hidden from both instead of leaking the other name.
    assert await catalog.people(account_id=_ACCOUNT, subject_id="subject-a") == ()
    assert await catalog.people(account_id=_ACCOUNT, subject_id="subject-b") == ()


@pytest.mark.asyncio
async def test_scoped_review_cannot_reach_another_subjects_claim(tmp_path: Path) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="claim-a", text=_WORK_A, subject_id="subject-a")
    await _record(
        archive,
        event_id="claim-b",
        text=_WORK_B,
        subject_id="subject-b",
        minute=1,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    await catalog.compile_pending()

    queue_a = await catalog.review_queue(account_id=_ACCOUNT, subject_id="subject-a")
    queue_b = await catalog.review_queue(account_id=_ACCOUNT, subject_id="subject-b")
    queue_all = await catalog.review_queue(account_id=_ACCOUNT)
    assert {item.source_event_id for item in queue_a} == {"claim-a"}
    assert {item.source_event_id for item in queue_b} == {"claim-b"}
    assert {item.source_event_id for item in queue_all} == {"claim-a", "claim-b"}

    claim_a = next(iter(queue_a)).item_id
    claim_b = next(iter(queue_b)).item_id

    # Guessing another subject's claim id is indistinguishable from a miss.
    with pytest.raises(EvidenceNotFoundError):
        await catalog.review(
            MemoryClaimReview(
                account_id=_ACCOUNT,
                claim_id=claim_b,
                action="confirm",
                subject_id="subject-a",
            )
        )
    with pytest.raises(EvidenceNotFoundError):
        await catalog.review(
            MemoryClaimReview(
                account_id=_ACCOUNT,
                claim_id=str(uuid.uuid4()),
                action="confirm",
                subject_id="subject-a",
            )
        )
    assert _claim_row(path, claim_b)["status"] == "candidate"

    reviewed = await catalog.review(
        MemoryClaimReview(
            account_id=_ACCOUNT,
            claim_id=claim_b,
            action="confirm",
            subject_id="subject-b",
        )
    )
    assert reviewed.status == "confirmed"
    review_event = await archive.event(
        account_id=_ACCOUNT,
        event_id=reviewed.review_event_id,
    )
    assert review_event is not None
    assert review_event.subject_id == "subject-b"
    # This claim's episode carries only its own subject, so the ordinary
    # cascade still applies inside the scope.
    assert _episode_evidence_statuses(path) == {
        "claim-a": "candidate",
        "claim-b": "confirmed",
    }

    # A claim whose projection document also merged another speaker's event can
    # never pass on its own single source.
    with sqlite3.connect(path) as connection:
        document_id = connection.execute(
            "SELECT document_id FROM memory_search_documents"
            " WHERE kind = 'claim' AND item_id = ?",
            (claim_a,),
        ).fetchone()
        assert document_id is not None
        connection.execute(
            "INSERT OR IGNORE INTO memory_search_document_sources"
            " (document_id, account_id, source_event_id) VALUES (?, ?, ?)",
            (str(document_id[0]), _ACCOUNT, "claim-b"),
        )

    assert await catalog.review_queue(account_id=_ACCOUNT, subject_id="subject-a") == ()
    with pytest.raises(EvidenceNotFoundError):
        await catalog.review(
            MemoryClaimReview(
                account_id=_ACCOUNT,
                claim_id=claim_a,
                action="confirm",
                subject_id="subject-a",
            )
        )


@pytest.mark.asyncio
async def test_scoped_review_leaves_a_cross_subject_episode_untouched(tmp_path: Path) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="merge-a", text=_WORK_A, subject_id="subject-a")
    await _record(
        archive,
        event_id="merge-b",
        text=_WORK_B,
        subject_id="subject-b",
        minute=1,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=PinnedEpisodeExtractor())
    await catalog.compile_pending()

    queue_b = await catalog.review_queue(account_id=_ACCOUNT, subject_id="subject-b")
    claim_b = next(item for item in queue_b if item.source_event_id == "merge-b").item_id
    await catalog.review(
        MemoryClaimReview(
            account_id=_ACCOUNT,
            claim_id=claim_b,
            action="confirm",
            subject_id="subject-b",
        )
    )

    # The claim itself is the subject's own, so it is confirmed as asked...
    assert _claim_row(path, claim_b)["status"] == "confirmed"
    # ...but its episode merged both speakers, so the cascade must not rewrite
    # the shared episode evidence or the shared timeline entries.
    assert _episode_evidence_statuses(path) == {
        "merge-a": "candidate",
        "merge-b": "candidate",
    }
    assert _timeline_statuses(path) == {
        "merge-a": "candidate",
        "merge-b": "candidate",
    }


@pytest.mark.asyncio
async def test_scoped_review_does_not_fold_another_subjects_conflicts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="age-a", text=_MOTHER_A, subject_id="subject-a")
    await _record(archive, event_id="age-b", text=_MOTHER_B, subject_id="subject-b", minute=1)
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    await catalog.compile_pending()

    queue_a = await catalog.review_queue(account_id=_ACCOUNT, subject_id="subject-a")
    queue_b = await catalog.review_queue(account_id=_ACCOUNT, subject_id="subject-b")
    claim_a = next(item for item in queue_a if item.value == "60").item_id
    claim_b = next(item for item in queue_b if item.value == "61").item_id
    # Compilation folded the age conflict across the account; the queue itself
    # already refuses to compare one speaker against another.
    assert all(item.reason == "pending_confirmation" for item in queue_a)
    assert _claim_row(path, claim_a)["conflict_state"] == "active"
    assert _claim_row(path, claim_b)["conflict_state"] == "active"

    await catalog.review(
        MemoryClaimReview(
            account_id=_ACCOUNT,
            claim_id=claim_a,
            action="retract",
            subject_id="subject-a",
        )
    )

    assert _claim_row(path, claim_a)["status"] == "retracted"
    # An account-wide fold would recompute B's state from what is left; the
    # scoped review only rewrites the subject that asked for it.
    assert _claim_row(path, claim_b)["status"] == "candidate"
    assert _claim_row(path, claim_b)["conflict_state"] == "active"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_scoped_conflict_fold_skips_a_claim_merged_with_another_speaker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="age-a", text=_MOTHER_A, subject_id="subject-a")
    await _record(
        archive,
        event_id="age-a2",
        text="我妈妈叫李梅，今年62岁了。",
        subject_id="subject-a",
        minute=1,
    )
    await _record(
        archive,
        event_id="age-b",
        text=_MOTHER_B,
        subject_id="subject-b",
        minute=2,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    await catalog.compile_pending()

    claims = _age_claim_ids(path)
    assert set(claims) == {"60", "61", "62"}
    assert {
        _claim_row(path, claim_id)["conflict_state"] for claim_id in claims.values()
    } == {"active"}

    # Subject A's second age claim also merged subject B's event.
    _merge_source_into_claim_document(path, claim_id=claims["62"], source_event_id="age-b")

    await catalog.review(
        MemoryClaimReview(
            account_id=_ACCOUNT,
            claim_id=claims["60"],
            action="retract",
            subject_id="subject-a",
        )
    )

    assert _claim_row(path, claims["60"])["status"] == "retracted"
    assert _claim_row(path, claims["60"])["conflict_state"] == "none"
    # The merged claim neither decided nor received this subject's fold, and the
    # other speaker's claim stays untouched as well.
    assert _claim_row(path, claims["62"])["conflict_state"] == "active"
    assert _claim_row(path, claims["61"])["conflict_state"] == "active"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_scoped_conflict_probe_ignores_a_claim_merged_with_another_speaker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="probe-a", text=_MOTHER_A, subject_id="subject-a")
    await _record(
        archive,
        event_id="probe-a2",
        text="我妈妈叫李梅，今年62岁了。",
        subject_id="subject-a",
        minute=1,
    )
    await _record(
        archive,
        event_id="probe-b",
        text=_MOTHER_B,
        subject_id="subject-b",
        minute=2,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    await catalog.compile_pending()

    claims = _age_claim_ids(path)
    assert set(claims) == {"60", "61", "62"}
    _merge_source_into_claim_document(path, claim_id=claims["62"], source_event_id="probe-b")

    scoped = await catalog.review_queue(account_id=_ACCOUNT, subject_id="subject-a")
    age_items = [item for item in scoped if item.value in {"60", "61", "62"}]
    assert [item.item_id for item in age_items] == [claims["60"]]
    # The other value sits behind a merged projection, so it can neither label a
    # conflict for this subject nor be listed; the raw column keeps saying
    # "active" because compilation folded the account.
    assert age_items[0].reason == "pending_confirmation"
    assert _claim_row(path, claims["60"])["conflict_state"] == "active"

    unscoped = await catalog.review_queue(account_id=_ACCOUNT)
    assert next(item.reason for item in unscoped if item.item_id == claims["60"]) == (
        "conflicting_values"
    )


@pytest.mark.asyncio
async def test_scoped_review_ignores_an_episode_link_from_another_account(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="link-a", text=_WORK_A, subject_id="subject-a")
    # Another account reuses the same subject string: its evidence must never
    # make this account's episode look like the subject's own evidence.
    await _record(
        archive,
        event_id="link-other",
        text=_WORK_B,
        subject_id="subject-a",
        account_id="account-other",
        minute=1,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=PinnedEpisodeExtractor())
    await catalog.compile_pending()

    episode_id = _episode_id_for_event(path, account_id=_ACCOUNT, source_event_id="link-a")
    _link_event_to_episode(
        path,
        account_id=_ACCOUNT,
        episode_id=episode_id,
        source_event_id="link-other",
    )
    claim_id = _claim_id_for_event(path, account_id=_ACCOUNT, source_event_id="link-a")

    await catalog.review(
        MemoryClaimReview(
            account_id=_ACCOUNT,
            claim_id=claim_id,
            action="confirm",
            subject_id="subject-a",
        )
    )

    assert _claim_row(path, claim_id)["status"] == "confirmed"
    # The linked source resolves in another account, so the episode counts as
    # merged and the review leaves its own evidence row alone.
    assert _episode_evidence_statuses(path)["link-a"] == "candidate"


@pytest.mark.asyncio
async def test_review_does_not_inherit_a_foreign_account_source_subject(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    await _record(archive, event_id="src-a", text=_WORK_A, subject_id="subject-a")
    await _record(
        archive,
        event_id="src-other",
        text=_WORK_B,
        subject_id="subject-a",
        account_id="account-other",
        minute=1,
    )
    catalog = MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor())
    await catalog.compile_pending()

    claim_id = _claim_id_for_event(path, account_id=_ACCOUNT, source_event_id="src-a")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memory_claims SET source_event_id = ? WHERE claim_id = ?",
            ("src-other", claim_id),
        )

    reviewed = await catalog.review(
        MemoryClaimReview(account_id=_ACCOUNT, claim_id=claim_id, action="confirm")
    )

    review_event = await archive.event(
        account_id=_ACCOUNT,
        event_id=reviewed.review_event_id,
    )
    assert review_event is not None
    # The stale reference resolves in another account, so nothing is inherited.
    assert review_event.subject_id is None


@pytest.mark.asyncio
async def test_transcript_revision_inherits_the_source_subject(tmp_path: Path) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    await _record(
        archive,
        event_id="revise-target",
        text="我在苏州读过书。",
        subject_id="subject-a",
    )

    reviewed = await archive.review(
        MemoryReview(
            review_event_id="revise-event",
            account_id=_ACCOUNT,
            target_id="revise-target",
            action="correct",
            corrected_text="我在杭州读过书。",
            occurred_at=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
        )
    )

    revision = await archive.event(account_id=_ACCOUNT, event_id=reviewed.review_event_id)
    assert revision is not None
    assert revision.subject_id == "subject-a"
    assert revision.supersedes_event_id == "revise-target"

    scoped = await archive.context(
        ContextQuery(account_id=_ACCOUNT, speaker_class="owner", subject_id="subject-a")
    )
    assert {item.event_id for item in scoped.evidence} == {"revise-target", "revise-event"}


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the subject scope contract",
)
async def test_postgres_subject_scope_fences_reads_and_review(
    scoped_postgres_dsn: str,
) -> None:
    dsn = scoped_postgres_dsn
    suffix = uuid.uuid4().hex[:10]
    account_id = f"pg-subject-scope-{suffix}"
    subject_a = f"subject-a-{suffix}"
    subject_b = f"subject-b-{suffix}"
    archive = PostgresLifeArchive(dsn)
    catalog = PostgresMemoryCatalog(dsn, extractor=PinnedEpisodeExtractor())
    await archive.initialize()
    await catalog.initialize()

    await _record(
        archive,
        event_id=f"pg-merge-a-{suffix}",
        text=_WORK_A,
        subject_id=subject_a,
        account_id=account_id,
        session_id=f"pg-session-a-{suffix}",
    )
    await _record(
        archive,
        event_id=f"pg-merge-b-{suffix}",
        text=_WORK_B,
        subject_id=subject_b,
        account_id=account_id,
        session_id=f"pg-session-b-{suffix}",
        minute=1,
    )
    await _record(
        archive,
        event_id=f"pg-merge-none-{suffix}",
        text="这次复盘没有确认说话人。",
        subject_id=None,
        account_id=account_id,
        session_id=f"pg-session-a-{suffix}",
        minute=2,
    )

    scoped_events = await archive.context(
        ContextQuery(
            account_id=account_id,
            speaker_class="owner",
            session_id=f"pg-session-a-{suffix}",
            subject_id=subject_a,
            limit=50,
        )
    )
    assert {item.event_id for item in scoped_events.evidence} == {f"pg-merge-a-{suffix}"}

    report = await catalog.compile_pending(limit=100)
    assert report.compiled_events == 3

    unscoped_timeline = await catalog.timeline(account_id=account_id)
    assert len({item.episode_id for item in unscoped_timeline}) == 1
    assert await catalog.timeline(account_id=account_id, subject_id=subject_a) == ()
    assert await catalog.timeline(account_id=account_id, subject_id=subject_b) == ()

    scoped_search = await catalog.search(
        MemorySearchQuery(account_id=account_id, speaker_class="owner", subject_id=subject_a)
    )
    assert all(item.kind != "episode" for item in scoped_search.items)
    assert all(
        f"pg-merge-b-{suffix}" not in item.source_event_ids
        for item in scoped_search.items
    )

    queue_b = await catalog.review_queue(account_id=account_id, subject_id=subject_b)
    assert queue_b
    claim_b = queue_b[0].item_id
    with pytest.raises(EvidenceNotFoundError):
        await catalog.review(
            MemoryClaimReview(
                account_id=account_id,
                claim_id=claim_b,
                action="confirm",
                subject_id=subject_a,
            )
        )

    reviewed = await catalog.review(
        MemoryClaimReview(
            account_id=account_id,
            claim_id=claim_b,
            action="confirm",
            subject_id=subject_b,
        )
    )
    review_event = await archive.event(
        account_id=account_id,
        event_id=reviewed.review_event_id,
    )
    assert review_event is not None
    assert review_event.subject_id == subject_b

    connection = await asyncpg.connect(dsn)
    try:
        foreign = await connection.fetchval(
            """
            SELECT count(*) FROM archive_evidence_events evidence
            JOIN archive_evidence_events reviewed
              ON reviewed.event_id = $2
            WHERE evidence.event_id = $1
              AND evidence.subject_id = reviewed.subject_id
            """,
            f"pg-merge-b-{suffix}",
            reviewed.review_event_id,
        )
    finally:
        await connection.close()
    assert foreign == 1
