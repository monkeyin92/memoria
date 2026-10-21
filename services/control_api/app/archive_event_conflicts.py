"""Archive event_id conflicts that clients must tell apart from each other."""

from __future__ import annotations

from fastapi import HTTPException

from services.archive.domain import IdempotencyConflictError

EVENT_ID_CONTENT_CONFLICT = "event_id_content_conflict"


def event_id_conflict(_exc: IdempotencyConflictError) -> HTTPException:
    """409 naming an event_id whose stored content differs from this write.

    Idempotent duplicates never reach here: the archive returns them as a
    duplicate success.

    The detail used to be plain text, so no client could count this cause
    apart from the idempotent duplicate without matching prose.
    """

    return HTTPException(status_code=409, detail={"code": EVENT_ID_CONTENT_CONFLICT})
