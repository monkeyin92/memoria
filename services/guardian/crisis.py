"""Privacy-minimized crisis evidence and guardian notification outbox."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from services.archive.domain import EvidenceEvent, LifeArchivePort

NotificationStatus = Literal["pending", "delivered", "failed"]


@dataclass(frozen=True, slots=True)
class CrisisNotificationReceipt:
    crisis_event_id: str
    evidence_event_id: str
    minor_user_id: str
    occurred_at: datetime
    script_version: str
    notification_count: int


@dataclass(frozen=True, slots=True)
class GuardianNotification:
    notification_id: str
    crisis_event_id: str
    guardian_user_id: str
    minor_user_id: str
    channel: Literal["wechat_subscription"]
    status: NotificationStatus
    attempts: int
    created_at: datetime
    delivered_at: datetime | None = None
    last_error_code: str | None = None


class CrisisNotificationStorePort(Protocol):
    async def enqueue_crisis_event(
        self,
        *,
        crisis_event_id: str,
        evidence_event_id: str,
        minor_user_id: str,
        occurred_at: datetime,
        script_version: str,
    ) -> CrisisNotificationReceipt: ...

    async def guardian_notifications(
        self,
        *,
        guardian_user_id: str,
        limit: int = 50,
    ) -> tuple[GuardianNotification, ...]: ...


class CrisisNotificationUnavailableError(RuntimeError):
    """A minor crisis could not be queued for any active guardian."""


class CrisisNotificationService:
    def __init__(
        self,
        store: CrisisNotificationStorePort,
        archive: LifeArchivePort,
    ) -> None:
        self._store = store
        self._archive = archive

    async def record_minor_crisis(
        self,
        *,
        minor_user_id: str,
        session_id: str,
        turn_id: int,
        generation_id: int,
        tool_epoch: int,
        script_version: str,
        occurred_at: datetime | None = None,
    ) -> CrisisNotificationReceipt:
        now = (occurred_at or datetime.now(UTC)).astimezone(UTC)
        fence_key = f"{minor_user_id}:{session_id}:{turn_id}:{generation_id}:{tool_epoch}"
        crisis_event_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:minor-crisis:{fence_key}")
        )
        evidence_event_id = f"guardian-crisis:{crisis_event_id}"
        await self._archive.record(
            EvidenceEvent(
                event_id=evidence_event_id,
                account_id=minor_user_id,
                event_type="guardian.crisis_event",
                occurred_at=now,
                speaker_class="system",
                source="crisis.deterministic_policy",
                session_id=session_id,
                turn_id=turn_id,
                generation_id=generation_id,
                payload={
                    "script_version": script_version,
                    "notification_required": True,
                    "tool_epoch": tool_epoch,
                    "contains_transcript": False,
                    "contains_severity": False,
                },
            )
        )
        receipt = await self._store.enqueue_crisis_event(
            crisis_event_id=crisis_event_id,
            evidence_event_id=evidence_event_id,
            minor_user_id=minor_user_id,
            occurred_at=now,
            script_version=script_version,
        )
        if receipt.notification_count < 1:
            raise CrisisNotificationUnavailableError(
                "minor crisis has no active guardian notification target"
            )
        return receipt


__all__ = [
    "CrisisNotificationReceipt",
    "CrisisNotificationService",
    "CrisisNotificationStorePort",
    "CrisisNotificationUnavailableError",
    "GuardianNotification",
    "NotificationStatus",
]
