"""Crisis-notify delivery stages projected from outbox state.

``channel_accepted`` is WeChat subscribe-message accept only.
``guardian_confirmed`` requires a read credential (``read_at``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from services.guardian.crisis import GuardianNotification, NotificationStatus

CrisisDeliveryStage = Literal[
    "recorded",
    "queued",
    "channel_accepted",
    "fail_or_auth_insufficient",
    "guardian_confirmed",
]

CrisisDeliveryResult = Literal[
    "recorded",
    "queued",
    "pin_only",
    "pending_retry",
    "channel_accepted",
    "no_subscription",
    "failed",
    "guardian_not_eligible",
    "guardian_confirmed",
    "duplicate_ignored",
]


@dataclass(frozen=True, slots=True)
class CrisisDeliveryStageEvent:
    stage: CrisisDeliveryStage
    event_id: str
    at: datetime
    result: CrisisDeliveryResult
    attempt: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CrisisDeliveryReceipt:
    notification_id: str
    crisis_event_id: str
    evidence_event_id: str
    current_stage: CrisisDeliveryStage
    stages: tuple[CrisisDeliveryStageEvent, ...]
    attempts: int
    last_error_code: str | None
    channel_accepted: bool
    guardian_confirmed: bool


def crisis_evidence_event_id(crisis_event_id: str) -> str:
    return f"guardian-crisis:{crisis_event_id}"


def project_crisis_delivery_receipt(
    notification: GuardianNotification,
    *,
    evidence_event_id: str | None = None,
    occurred_at: datetime | None = None,
) -> CrisisDeliveryReceipt:

    evidence_id = (
        evidence_event_id
        or notification.evidence_event_id
        or crisis_evidence_event_id(notification.crisis_event_id)
    )
    recorded_at = occurred_at or notification.occurred_at or notification.created_at
    stages: list[CrisisDeliveryStageEvent] = [
        CrisisDeliveryStageEvent(
            stage="recorded",
            event_id=evidence_id,
            at=recorded_at,
            result="recorded",
        ),
        CrisisDeliveryStageEvent(
            stage="queued",
            event_id=notification.notification_id,
            at=notification.created_at,
            result=_queued_result(notification.status, notification.attempts),
            attempt=notification.attempts if notification.attempts > 0 else None,
            reason=_queued_reason(notification),
        ),
    ]

    if notification.status in {"failed", "no_subscription"}:
        fail_at = notification.settled_at or notification.created_at
        stages.append(
            CrisisDeliveryStageEvent(
                stage="fail_or_auth_insufficient",
                event_id=f"{notification.notification_id}:settle",
                at=fail_at,
                result=_fail_result(notification.status, notification.last_error_code),
                attempt=notification.attempts if notification.attempts > 0 else None,
                reason=notification.last_error_code,
            )
        )
        current: CrisisDeliveryStage = "fail_or_auth_insufficient"
    elif notification.status == "delivered":
        if notification.delivered_at is None:
            raise ValueError("delivered notification requires delivered_at")
        stages.append(
            CrisisDeliveryStageEvent(
                stage="channel_accepted",
                event_id=f"{notification.notification_id}:channel",
                at=notification.delivered_at,
                result="channel_accepted",
                attempt=notification.attempts if notification.attempts > 0 else None,
            )
        )
        current = "channel_accepted"
    else:
        current = "queued"

    if notification.read_at is not None:
        stages.append(
            CrisisDeliveryStageEvent(
                stage="guardian_confirmed",
                event_id=f"{notification.notification_id}:read",
                at=notification.read_at,
                result="guardian_confirmed",
            )
        )
        current = "guardian_confirmed"

    return CrisisDeliveryReceipt(
        notification_id=notification.notification_id,
        crisis_event_id=notification.crisis_event_id,
        evidence_event_id=evidence_id,
        current_stage=current,
        stages=tuple(stages),
        attempts=notification.attempts,
        last_error_code=notification.last_error_code,
        channel_accepted=notification.status == "delivered",
        guardian_confirmed=notification.read_at is not None,
    )


def crisis_delivery_receipt_payload(receipt: CrisisDeliveryReceipt) -> dict[str, object]:
    return {
        "notification_id": receipt.notification_id,
        "crisis_event_id": receipt.crisis_event_id,
        "evidence_event_id": receipt.evidence_event_id,
        "current_stage": receipt.current_stage,
        "attempts": receipt.attempts,
        "last_error_code": receipt.last_error_code,
        "channel_accepted": receipt.channel_accepted,
        "guardian_confirmed": receipt.guardian_confirmed,
        "stages": [
            {
                "stage": event.stage,
                "event_id": event.event_id,
                "at": event.at.isoformat(),
                "result": event.result,
                "attempt": event.attempt,
                "reason": event.reason,
            }
            for event in receipt.stages
        ],
    }


def duplicate_delivery_result(*, prior_status: NotificationStatus) -> CrisisDeliveryResult:
    if prior_status == "delivered":
        return "duplicate_ignored"
    if prior_status in {"failed", "no_subscription", "pin_only"}:
        return "duplicate_ignored"
    return "pending_retry"


def _queued_result(status: NotificationStatus, attempts: int) -> CrisisDeliveryResult:
    if status == "pin_only":
        return "pin_only"
    if status == "pending" and attempts > 0:
        return "pending_retry"
    return "queued"


def _queued_reason(notification: GuardianNotification) -> str | None:
    if notification.status == "pending" and notification.last_error_code:
        return notification.last_error_code
    if notification.status == "pin_only":
        return "attempt_push_false"
    return None


def _fail_result(
    status: NotificationStatus, last_error_code: str | None
) -> CrisisDeliveryResult:
    if status == "no_subscription":
        return "no_subscription"
    if last_error_code == "guardian_not_eligible":
        return "guardian_not_eligible"
    return "failed"


__all__ = [
    "CrisisDeliveryReceipt",
    "CrisisDeliveryResult",
    "CrisisDeliveryStage",
    "CrisisDeliveryStageEvent",
    "crisis_delivery_receipt_payload",
    "crisis_evidence_event_id",
    "duplicate_delivery_result",
    "project_crisis_delivery_receipt",
]
