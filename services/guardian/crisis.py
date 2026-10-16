"""Privacy-minimized crisis evidence and guardian notification outbox."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, get_args

from services.archive.domain import EvidenceEvent, LifeArchivePort

NotificationStatus = Literal["pending", "pin_only", "delivered", "failed", "no_subscription"]

# SQL enqueue picks the insert status from attempt_push (pending for
# crisis/victim, pin_only for relay). Push may later move a row to
# delivered / failed / no_subscription. Every legal outbox status stays
# pinned; delivery must never clear the home card.
PINNED_CRISIS_ALERT_STATUSES = frozenset(get_args(NotificationStatus))


def crisis_alert_is_pinned(status: str) -> bool:
    """True when the parent must see this alert on next Mini Program open."""

    return status in PINNED_CRISIS_ALERT_STATUSES


@dataclass(frozen=True, slots=True)
class CrisisNotificationReceipt:
    crisis_event_id: str
    evidence_event_id: str
    minor_user_id: str
    occurred_at: datetime
    script_version: str
    reply_template_id: str
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
    possibly_story_retell: bool = False
    script_interrupted: bool = False
    reply_template_id: str = ""


class CrisisNotificationStorePort(Protocol):
    async def enqueue_crisis_event(
        self,
        *,
        crisis_event_id: str,
        evidence_event_id: str,
        minor_user_id: str,
        occurred_at: datetime,
        script_version: str,
        reply_template_id: str,
        declared_guardian_ids: tuple[str, ...] = (),
        attempt_push: bool = True,
        possibly_story_retell: bool = False,
    ) -> CrisisNotificationReceipt: ...

    async def guardian_notifications(
        self,
        *,
        guardian_user_id: str,
        limit: int = 50,
    ) -> tuple[GuardianNotification, ...]: ...

    async def mark_script_interrupted(self, *, crisis_event_id: str) -> str | None:
        """Set the interrupted flag. Returns the reply template id, or None if missing.

        Callers pass the pin's event id only. Child speech is not accepted.
        """
        ...


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
        reply_template_id: str,
        occurred_at: datetime | None = None,
        declared_guardian_ids: tuple[str, ...] = (),
        attempt_push: bool = True,
        possibly_story_retell: bool = False,
    ) -> CrisisNotificationReceipt:
        """Queue one crisis event for every guardian that is authorized to read it.

        ``declared_guardian_ids`` carries guardians whose ``guardian_of``
        relationship is a one-sided declaration (the guardian confirmed, the
        subject has no account and never confirmed).  They are resolved from
        the Identity authority by the caller and are a distinct, honestly
        labelled basis from an activated guardian link: the declaration is
        never upgraded to a verified link, and it never unlocks consent-gated
        capabilities such as memory retention or session admission.

        ``attempt_push`` is False for relay-tier support requests: the alert
        still pins in the Mini Program as a ``pin_only`` row, which the
        subscribe-message worker never claims.

        ``possibly_story_retell`` is stored on the row. It is not written into
        the parent title. It never skips the fixed script or the notification.

        ``reply_template_id`` and ``script_version`` are ids only. Child text
        never enters this payload or the crisis event row.
        """

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
                    "reply_template_id": reply_template_id,
                    "notification_required": True,
                    "attempt_push": attempt_push,
                    "possibly_story_retell": possibly_story_retell,
                    "tool_epoch": tool_epoch,
                    "contains_transcript": False,
                    "contains_severity": False,
                    "declared_guardian_count": len(declared_guardian_ids),
                },
            )
        )
        receipt = await self._store.enqueue_crisis_event(
            crisis_event_id=crisis_event_id,
            evidence_event_id=evidence_event_id,
            minor_user_id=minor_user_id,
            occurred_at=now,
            script_version=script_version,
            reply_template_id=reply_template_id,
            declared_guardian_ids=declared_guardian_ids,
            attempt_push=attempt_push,
            possibly_story_retell=possibly_story_retell,
        )
        if receipt.notification_count < 1:
            raise CrisisNotificationUnavailableError(
                "minor crisis has no authorized guardian notification target"
            )
        return receipt

    async def mark_script_interrupted(self, *, crisis_event_id: str) -> str | None:
        return await self._store.mark_script_interrupted(crisis_event_id=crisis_event_id)


__all__ = [
    "PINNED_CRISIS_ALERT_STATUSES",
    "CrisisNotificationReceipt",
    "CrisisNotificationService",
    "CrisisNotificationStorePort",
    "CrisisNotificationUnavailableError",
    "GuardianNotification",
    "NotificationStatus",
    "crisis_alert_is_pinned",
]
