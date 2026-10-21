from __future__ import annotations

import pytest
from services.archive.domain import IdempotencyConflictError
from services.control_api.app.archive_event_conflicts import event_id_conflict


@pytest.mark.hermetic
def test_content_conflict_409_reports_its_cause_code() -> None:
    conflict = event_id_conflict(IdempotencyConflictError("different content"))
    assert (conflict.status_code, conflict.detail) == (
        409,
        {"code": "event_id_content_conflict"},
    )
