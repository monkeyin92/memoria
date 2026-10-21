from __future__ import annotations

import pytest
from fastapi import HTTPException
from services.archive.domain import IdempotencyConflictError
from services.control_api.app.archive_event_conflicts import event_id_conflict


def test_content_conflict_409_reports_its_cause_code() -> None:
    with pytest.raises(HTTPException) as raised:
        event_id_conflict(IdempotencyConflictError("different content"))
    assert (raised.value.status_code, raised.value.detail) == (
        409,
        {"code": "event_id_content_conflict"},
    )
