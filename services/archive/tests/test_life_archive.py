"""Evidence ledger contract on PostgreSQL (raw-voice cases: test_postgres_archive)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from services.archive.domain import (
    ContextQuery,
    EvidenceEvent,
    IdempotencyConflictError,
    MemoryReview,
)
from services.archive.postgres_archive import PostgresLifeArchive


@pytest.mark.asyncio
async def test_recording_the_same_event_is_idempotent(archive: PostgresLifeArchive) -> None:
    event = EvidenceEvent(
        event_id="event-001",
        account_id="account-001",
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
        speaker_class="owner",
        source="funasr",
        payload={"text": "我小时候住在杭州。"},
    )

    results = [await archive.record(event) for _ in range(100)]

    assert results[0].duplicate is False
    assert all(result.event_id == "event-001" for result in results)
    assert all(result.outbox_id == results[0].outbox_id for result in results)
    assert sum(not result.duplicate for result in results) == 1
    context = await archive.context(
        ContextQuery(account_id="account-001", speaker_class="owner")
    )
    assert [item.event_id for item in context.evidence] == ["event-001"]


@pytest.mark.asyncio
async def test_event_and_turn_event_lookup_return_the_exact_canonical_evidence(archive: PostgresLifeArchive) -> None:
    event = EvidenceEvent(
        event_id="turn-lookup-event",
        account_id="account-001",
        session_id="session-001",
        turn_id=3,
        generation_id=2,
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
        speaker_class="owner",
        source="funasr.authoritative_final",
        payload={"text": "精确查询父话轮。"},
    )
    await archive.record(event)

    by_id = await archive.event(account_id="account-001", event_id=event.event_id)
    by_turn = await archive.turn_event(
        account_id="account-001",
        session_id="session-001",
        turn_id=3,
        generation_id=2,
        event_type="speech.utterance_finalized",
    )

    assert by_id == event
    assert by_turn == event
    assert (
        await archive.turn_event(
            account_id="account-001",
            session_id="session-001",
            turn_id=3,
            generation_id=1,
            event_type="speech.utterance_finalized",
        )
    ) is None


@pytest.mark.asyncio
async def test_reusing_an_event_id_for_different_content_is_rejected(archive: PostgresLifeArchive) -> None:
    original = EvidenceEvent(
        event_id="event-001",
        account_id="account-001",
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
        speaker_class="owner",
        source="funasr",
        payload={"text": "原始内容"},
    )
    conflicting = EvidenceEvent(
        event_id="event-001",
        account_id="account-001",
        event_type="speech.utterance_finalized",
        occurred_at=original.occurred_at,
        speaker_class="owner",
        source="funasr",
        payload={"text": "被替换的内容"},
    )
    await archive.record(original)

    with pytest.raises(IdempotencyConflictError):
        await archive.record(conflicting)


@pytest.mark.asyncio
async def test_review_correction_supersedes_without_rewriting_original_evidence(archive: PostgresLifeArchive) -> None:
    original = EvidenceEvent(
        event_id="event-original",
        account_id="account-001",
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
        speaker_class="owner",
        source="funasr",
        payload={"text": "我在苏州读过书。"},
    )
    await archive.record(original)

    reviewed = await archive.review(
        MemoryReview(
            review_event_id="event-correction",
            account_id="account-001",
            target_id="event-original",
            action="correct",
            corrected_text="我在杭州读过书。",
            occurred_at=datetime(2026, 7, 19, 8, 1, tzinfo=UTC),
        )
    )

    assert reviewed.status == "corrected"
    assert reviewed.current_text == "我在杭州读过书。"
    context = await archive.context(
        ContextQuery(account_id="account-001", speaker_class="owner")
    )
    assert {item.event_id for item in context.evidence} == {
        "event-original",
        "event-correction",
    }
    correction = next(item for item in context.evidence if item.event_id == "event-correction")
    assert correction.supersedes_event_id == "event-original"


@pytest.mark.asyncio
async def test_owner_context_hides_legacy_guest_and_uncertain_evidence(archive: PostgresLifeArchive) -> None:
    occurred_at = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)
    for speaker_class in ("owner", "guest", "uncertain"):
        await archive.record(
            EvidenceEvent(
                event_id=f"legacy-{speaker_class}",
                account_id="account-legacy-speakers",
                event_type="speech.utterance_finalized",
                occurred_at=occurred_at,
                speaker_class=speaker_class,  # type: ignore[arg-type]
                source="legacy-import",
                payload={"text": speaker_class},
            )
        )

    context = await archive.context(
        ContextQuery(account_id="account-legacy-speakers", speaker_class="owner")
    )

    assert [event.event_id for event in context.evidence] == ["legacy-owner"]
