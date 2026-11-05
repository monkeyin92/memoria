"""A03 display and recall gates for child-archive parent review."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.guardian.child_archive_review import (
    build_fact_view,
    display_lifecycle,
    recall_eligible,
)


def test_auto_extract_candidate_is_never_labeled_confirmed() -> None:
    lifecycle = display_lifecycle(
        status="candidate",
        reason_code="child_archive_auto_extract",
        provenance="auto_extract",
        retention_expires_at=None,
    )
    assert lifecycle == "auto_pending"
    view = build_fact_view(
        record_id="charch:1",
        field="preference",
        text="喜欢画画",
        created_at=datetime(2026, 11, 4, tzinfo=UTC),
        retention_expires_at=None,
        payload={
            "provenance": "auto_extract",
            "source_session_id": "sess-1",
            "rule_version": "preference-extract-v2",
            "fact_version": 1,
            "polarity": "positive",
        },
        status="candidate",
        reason_code="child_archive_auto_extract",
        handled_at=datetime(2026, 11, 4, tzinfo=UTC),
        confidence=0.75,
    )
    assert view.display_label == "自动提取·待确认"
    assert "已确认" not in view.display_label or view.display_label.startswith("自动")
    assert view.recall_eligible is False


def test_parent_confirm_reason_promotes_display_even_if_payload_stays_auto() -> None:
    lifecycle = display_lifecycle(
        status="confirmed",
        reason_code="parent_confirm",
        provenance="auto_extract",
        retention_expires_at=None,
    )
    assert lifecycle == "parent_confirmed"
    assert recall_eligible(lifecycle) is True


def test_illegal_auto_confirmed_without_parent_reason_stays_pending_label() -> None:
    lifecycle = display_lifecycle(
        status="confirmed",
        reason_code="child_archive_auto_extract",
        provenance="auto_extract",
        retention_expires_at=None,
    )
    assert lifecycle == "auto_pending"
    assert recall_eligible(lifecycle) is False


def test_expired_and_withdrawn_are_not_recalled() -> None:
    expired = display_lifecycle(
        status="confirmed",
        reason_code="parent_confirm",
        provenance="parent_confirm",
        retention_expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    withdrawn = display_lifecycle(
        status="revoked",
        reason_code="parent_withdraw",
        provenance="parent_confirm",
        retention_expires_at=None,
    )
    assert expired == "expired"
    assert withdrawn == "withdrawn"
    assert recall_eligible(expired) is False
    assert recall_eligible(withdrawn) is False
