"""Outbox-driven archive projection for committed tutor aggregates.

The aggregate commit is durable and atomic in the guardian store.  Archive
delivery is rebuilt from the outbox here, so a failed archive write leaves a
pending row that any later worker (or the routes, best-effort) retries
exactly once.  An idempotency conflict is only accepted when the existing
archive event carries the identical canonical payload.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import cast

from services.archive.domain import EvidenceEvent, IdempotencyConflictError, LifeArchivePort
from services.tutor.authority import TutorEvidenceRejected
from services.tutor.domain import PendingTutorCommit, TutorPracticeCommitPort


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _matches(committed: dict[str, object], existing: EvidenceEvent) -> bool:
    return (
        existing.event_id == committed.get("event_id")
        and existing.account_id == committed.get("account_id")
        and existing.event_type == committed.get("event_type")
        and existing.source == committed.get("source")
        and existing.session_id == committed.get("session_id")
        and _canonical(dict(existing.payload))
        == _canonical(dict(cast(dict[str, object], committed["payload"])))
    )


async def drain_commit_outbox(
    store: TutorPracticeCommitPort,
    archive: LifeArchivePort,
    *,
    worker_id: str,
    subject_id: str | None = None,
    limit: int = 64,
) -> int:
    """Claim, deliver, and finalize outbox rows; returns delivered count.

    A claim is leased per worker pass: concurrent drains never double-deliver.
    Rows that cannot be delivered (archive failure, or an archive event that
    exists with different content) are released back to pending for the next
    worker pass.
    """

    delivered = 0
    claimed = await store.claim_commit_events(
        worker_id=worker_id,
        subject_id=subject_id,
        limit=limit,
    )
    for item in claimed:
        try:
            await _deliver_one(store, archive, item)
        except TutorEvidenceRejected:
            await store.release_commit_claim(event_id=item.event_id)
            continue
        except IdempotencyConflictError:
            await store.release_commit_claim(event_id=item.event_id)
            continue
        await store.mark_commit_delivered(event_id=item.event_id)
        delivered += 1
    return delivered


async def _deliver_one(
    store: TutorPracticeCommitPort,
    archive: LifeArchivePort,
    item: PendingTutorCommit,
) -> None:
    payload = item.archive_payload
    event = EvidenceEvent(
        event_id=str(payload["event_id"]),
        account_id=str(payload["account_id"]),
        event_type=str(payload["event_type"]),
        occurred_at=datetime.fromisoformat(str(payload["occurred_at"])),
        speaker_class=str(payload["speaker_class"]),  # type: ignore[arg-type]
        source=str(payload["source"]),
        session_id=(
            str(payload.get("session_id"))
            if payload.get("session_id") is not None
            else None
        ),
        payload=dict(payload["payload"]),
    )
    try:
        await archive.record(event)
    except IdempotencyConflictError:
        existing = await archive.event(
            account_id=event.account_id,
            event_id=event.event_id,
        )
        if existing is None or not _matches(payload, existing):
            # Same event id, different content: never treat as delivered.
            raise TutorEvidenceRejected("outbox_conflict") from None
    await store.mark_commit_delivered(event_id=item.event_id)
