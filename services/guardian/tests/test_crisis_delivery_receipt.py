"""Offline fault matrix for crisis delivery receipt stages.

No WeChat channel. No fake 已读 / 家长收到 from enqueue or channel accept.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from services.guardian.crisis import GuardianNotification
from services.guardian.crisis_delivery_receipt import (
    crisis_delivery_receipt_payload,
    duplicate_delivery_result,
    project_crisis_delivery_receipt,
)

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)
CRISIS_ID = "7d1f9c1e-0000-4000-8000-0000000000c1"
NOTE_ID = "7d1f9c1e-0000-4000-8000-0000000000n1"
EVIDENCE_ID = f"guardian-crisis:{CRISIS_ID}"


def _notification(**overrides: object) -> GuardianNotification:
    base: dict[str, object] = {
        "notification_id": NOTE_ID,
        "crisis_event_id": CRISIS_ID,
        "guardian_user_id": "guardian-a",
        "minor_user_id": "minor-a",
        "channel": "wechat_subscription",
        "status": "pending",
        "attempts": 0,
        "created_at": NOW,
        "reply_template_id": None,
        "read_at": None,
        "delivered_at": None,
        "settled_at": None,
        "last_error_code": None,
        "evidence_event_id": EVIDENCE_ID,
        "occurred_at": NOW - timedelta(seconds=1),
    }
    base.update(overrides)
    return GuardianNotification(**base)  # type: ignore[arg-type]


def test_recorded_and_queued_never_look_like_parent_received() -> None:
    receipt = project_crisis_delivery_receipt(_notification())
    assert receipt.current_stage == "queued"
    assert [event.stage for event in receipt.stages] == ["recorded", "queued"]
    assert receipt.channel_accepted is False
    assert receipt.guardian_confirmed is False
    payload = crisis_delivery_receipt_payload(receipt)
    blob = str(payload)
    assert "已读" not in blob
    assert "家长收到" not in blob
    assert payload["guardian_confirmed"] is False
    assert payload["channel_accepted"] is False


def test_pin_only_stays_queued_without_channel_accept() -> None:
    receipt = project_crisis_delivery_receipt(_notification(status="pin_only"))
    assert receipt.current_stage == "queued"
    assert receipt.stages[-1].result == "pin_only"
    assert receipt.channel_accepted is False
    assert receipt.guardian_confirmed is False


def test_no_subscription_is_fail_or_auth_not_delivered() -> None:
    settled = NOW + timedelta(minutes=1)
    receipt = project_crisis_delivery_receipt(
        _notification(
            status="no_subscription",
            attempts=1,
            settled_at=settled,
            last_error_code="no_subscription",
        )
    )
    assert receipt.current_stage == "fail_or_auth_insufficient"
    fail = receipt.stages[-1]
    assert fail.stage == "fail_or_auth_insufficient"
    assert fail.result == "no_subscription"
    assert fail.reason == "no_subscription"
    assert fail.at == settled
    assert receipt.channel_accepted is False
    assert receipt.guardian_confirmed is False


def test_channel_fail_and_auth_refusal_map_to_fail_stage() -> None:
    settled = NOW + timedelta(minutes=2)
    channel_fail = project_crisis_delivery_receipt(
        _notification(
            status="failed",
            attempts=3,
            settled_at=settled,
            last_error_code="wechat_40037",
        )
    )
    assert channel_fail.current_stage == "fail_or_auth_insufficient"
    assert channel_fail.stages[-1].result == "failed"
    assert channel_fail.stages[-1].reason == "wechat_40037"

    revoked = project_crisis_delivery_receipt(
        _notification(
            status="failed",
            attempts=1,
            settled_at=settled,
            last_error_code="guardian_not_eligible",
        )
    )
    assert revoked.stages[-1].result == "guardian_not_eligible"
    assert revoked.channel_accepted is False
    assert revoked.guardian_confirmed is False


def test_pending_retry_keeps_queued_with_attempt_and_reason() -> None:
    receipt = project_crisis_delivery_receipt(
        _notification(status="pending", attempts=2, last_error_code="wechat_-1")
    )
    assert receipt.current_stage == "queued"
    queued = receipt.stages[1]
    assert queued.result == "pending_retry"
    assert queued.attempt == 2
    assert queued.reason == "wechat_-1"
    assert receipt.guardian_confirmed is False


def test_channel_accepted_is_not_guardian_confirmed() -> None:
    delivered_at = NOW + timedelta(minutes=3)
    receipt = project_crisis_delivery_receipt(
        _notification(
            status="delivered",
            attempts=1,
            delivered_at=delivered_at,
            settled_at=delivered_at,
            read_at=None,
        )
    )
    assert receipt.current_stage == "channel_accepted"
    assert receipt.channel_accepted is True
    assert receipt.guardian_confirmed is False
    assert [event.stage for event in receipt.stages] == [
        "recorded",
        "queued",
        "channel_accepted",
    ]
    payload = crisis_delivery_receipt_payload(receipt)
    assert payload["guardian_confirmed"] is False
    assert "已读" not in str(payload)


def test_guardian_confirmed_requires_read_at() -> None:
    delivered_at = NOW + timedelta(minutes=3)
    read_at = NOW + timedelta(minutes=5)
    without_read = project_crisis_delivery_receipt(
        _notification(
            status="delivered",
            delivered_at=delivered_at,
            settled_at=delivered_at,
            read_at=None,
        )
    )
    assert without_read.guardian_confirmed is False
    assert "guardian_confirmed" not in {event.stage for event in without_read.stages}

    with_read = project_crisis_delivery_receipt(
        _notification(
            status="delivered",
            delivered_at=delivered_at,
            settled_at=delivered_at,
            read_at=read_at,
            attempts=1,
        )
    )
    assert with_read.current_stage == "guardian_confirmed"
    assert with_read.guardian_confirmed is True
    assert with_read.stages[-1].event_id == f"{NOTE_ID}:read"
    assert with_read.stages[-1].at == read_at


def test_duplicate_settle_does_not_escalate_success() -> None:
    assert duplicate_delivery_result(prior_status="delivered") == "duplicate_ignored"
    assert duplicate_delivery_result(prior_status="failed") == "duplicate_ignored"
    assert duplicate_delivery_result(prior_status="no_subscription") == "duplicate_ignored"
    assert duplicate_delivery_result(prior_status="pending") == "pending_retry"


def test_delivered_without_delivered_at_is_rejected() -> None:
    with pytest.raises(ValueError, match="delivered_at"):
        project_crisis_delivery_receipt(_notification(status="delivered", delivered_at=None))


@pytest.mark.parametrize(
    ("status", "error", "expected_stage", "expected_result"),
    [
        ("pending", None, "queued", "queued"),
        ("pin_only", None, "queued", "pin_only"),
        ("no_subscription", "no_subscription", "fail_or_auth_insufficient", "no_subscription"),
        ("failed", "sender_error", "fail_or_auth_insufficient", "failed"),
        ("failed", "guardian_not_eligible", "fail_or_auth_insufficient", "guardian_not_eligible"),
        ("delivered", None, "channel_accepted", "channel_accepted"),
    ],
)
def test_fault_matrix_stage_and_result(
    status: str,
    error: str | None,
    expected_stage: str,
    expected_result: str,
) -> None:
    delivered_at = NOW + timedelta(minutes=1) if status == "delivered" else None
    settled_at = NOW + timedelta(minutes=1) if status != "pending" and status != "pin_only" else None
    receipt = project_crisis_delivery_receipt(
        _notification(
            status=status,
            last_error_code=error,
            delivered_at=delivered_at,
            settled_at=settled_at,
            attempts=1 if status != "pending" or error else 0,
        )
    )
    assert receipt.current_stage == expected_stage
    assert receipt.stages[-1].result == expected_result
    assert receipt.guardian_confirmed is False
