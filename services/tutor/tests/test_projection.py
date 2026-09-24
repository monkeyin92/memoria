"""Outbox archive projection keeps the practising subject on the archive row."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.tutor.domain import PendingTutorCommit
from services.tutor.projection import drain_commit_outbox

NOW = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)


class _Outbox:
    def __init__(self, *items: PendingTutorCommit) -> None:
        self.pending = list(items)
        self.delivered: list[str] = []
        self.released: list[str] = []

    async def claim_commit_events(self, **_: Any) -> tuple[PendingTutorCommit, ...]:
        claimed, self.pending = tuple(self.pending), []
        return claimed

    async def mark_commit_delivered(self, *, event_id: str) -> None:
        self.delivered.append(event_id)

    async def release_commit_claim(self, *, event_id: str) -> None:
        self.released.append(event_id)

    async def commit_aggregate(self, **_: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


def _pending(event_id: str) -> PendingTutorCommit:
    return PendingTutorCommit(
        event_id=event_id,
        kind="tutor.practice_turn_recorded",
        subject_id="subject-s",
        actor_id="owner-o",
        archive_payload={
            "event_id": event_id,
            "account_id": "owner-o",
            "event_type": "tutor.practice_turn_recorded",
            "occurred_at": NOW.isoformat(),
            "speaker_class": "owner",
            "source": "tutor.control_api",
            "session_id": "session-1",
            "payload": {"subject_id": "subject-s", "outcome": "attempted"},
        },
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_archived_practice_names_the_subject(tmp_path: Path) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    outbox = _Outbox(_pending("tutor-practice-event:1"))

    assert await drain_commit_outbox(outbox, archive, worker_id="worker") == 1

    event = await archive.event(account_id="owner-o", event_id="tutor-practice-event:1")
    assert event is not None
    assert event.subject_id == "subject-s"


@pytest.mark.asyncio
async def test_row_archived_before_subject_attribution_still_settles(
    tmp_path: Path,
) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    item = _pending("tutor-practice-event:legacy")
    payload = item.archive_payload
    await archive.record(
        EvidenceEvent(
            event_id=str(payload["event_id"]),
            account_id=str(payload["account_id"]),
            event_type=str(payload["event_type"]),
            occurred_at=NOW,
            speaker_class="owner",
            source=str(payload["source"]),
            session_id=str(payload["session_id"]),
            payload=dict(payload["payload"]),
        )
    )
    outbox = _Outbox(item)

    # A retry of a row delivered before the projection carried subject_id is
    # the same logical event: it settles instead of looping as a conflict.
    assert await drain_commit_outbox(outbox, archive, worker_id="worker") == 1
    assert outbox.released == []
