from __future__ import annotations

from datetime import UTC, datetime

from services.guardian.child_archive_recall import recall_ready_archive_views
from services.guardian.child_archive_review import build_fact_view


def _view(
    *,
    record_id: str,
    text: str,
    status: str,
    reason_code: str,
    provenance: str,
    fact_version: int = 1,
    field: str = "preference",
) -> object:
    return build_fact_view(
        record_id=record_id,
        field=field,
        text=text,
        created_at=datetime(2026, 11, 4, tzinfo=UTC),
        retention_expires_at=None,
        payload={
            "provenance": provenance,
            "source_session_id": "sess-1",
            "rule_version": "preference-extract-v2",
            "fact_version": fact_version,
            "polarity": "positive",
        },
        status=status,
        reason_code=reason_code,
        handled_at=datetime(2026, 11, 4, tzinfo=UTC),
        confidence=1.0 if status == "confirmed" else 0.75,
    )


def test_only_parent_confirmed_archive_facts_are_recall_ready() -> None:
    confirmed = _view(
        record_id="charch:ok",
        text="喜欢画画",
        status="confirmed",
        reason_code="parent_confirm",
        provenance="parent_confirm",
        fact_version=2,
    )
    pending = _view(
        record_id="charch:pending",
        text="喜欢恐龙",
        status="candidate",
        reason_code="child_archive_auto_extract",
        provenance="auto_extract",
    )
    withdrawn = _view(
        record_id="charch:gone",
        text="旧称呼",
        status="revoked",
        reason_code="parent_withdraw",
        provenance="parent_confirm",
        field="appellation",
    )
    ready = recall_ready_archive_views((confirmed, pending, withdrawn))
    assert [view.record_id for view in ready] == ["charch:ok"]


def test_auto_extract_confirmed_without_parent_reason_stays_out() -> None:
    illegal = _view(
        record_id="charch:bad",
        text="喜欢恐龙",
        status="confirmed",
        reason_code="child_archive_auto_extract",
        provenance="auto_extract",
    )
    assert recall_ready_archive_views((illegal,)) == ()
