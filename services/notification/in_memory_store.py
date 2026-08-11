"""In-memory adapter for the notification store seam.

Suitable for unit tests and Control API development fixtures only (never
production: section 11.7).  Thread-safe for a single event loop; claims are
guarded by a reentrant lock so lease + fencing tokens stay consistent under
concurrent workers.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime

from services.notification.domain import (
    CancelReason,
    DeliveryAttempt,
    DeliveryReceipt,
    IntentStatus,
    NotificationConflictError,
    NotificationIntent,
    RecipientBinding,
    RelationshipStatusValue,
)
from services.notification.repository import (
    NotificationAuditEvent,
    NotificationOutboxEvent,
)

#: Explicit marker: this adapter must never back production traffic.
DEV_TEST_ONLY: bool = True


class InMemoryNotificationStore:
    """In-memory implementation of the ``NotificationStore`` protocol."""

    def __init__(self) -> None:
        self._fail_after_step: str | None = None
        self._lock = threading.RLock()
        self._intents: dict[str, NotificationIntent] = {}
        self._intents_by_key: dict[str, str] = {}
        self._recipients: dict[str, RecipientBinding] = {}
        self._attempts: dict[str, DeliveryAttempt] = {}
        self._receipts: dict[str, DeliveryReceipt] = {}
        self._receipts_by_attempt: dict[str, str] = {}
        self._audit: list[NotificationAuditEvent] = []
        self._outbox: dict[str, NotificationOutboxEvent] = {}

    def intents_by_key(self) -> dict[str, str]:
        with self._lock:
            return dict(self._intents_by_key)

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def save_intent_atomically(
        self,
        intent: NotificationIntent,
        *,
        recipients: tuple[RecipientBinding, ...],
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        authenticated_actor_person_id: str | None = None,
        authenticated_subject_person_id: str | None = None,
    ) -> NotificationIntent | None:
        """First-write-wins single critical-section commit of intent +
        recipients + audit + outbox (section 11.6): returns the intent when
        this call won, or ``None`` when another writer already owns the
        idempotency key.  Any failure rolls back everything written."""
        if (
            authenticated_actor_person_id is not None
            and authenticated_actor_person_id != intent.actor_person_id
        ):
            raise ValueError("authenticated actor does not match intent actor")
        if (
            authenticated_subject_person_id is not None
            and authenticated_subject_person_id != intent.subject_person_id
        ):
            raise ValueError("authenticated subject does not match intent subject")
        with self._lock:
            existing_owner = self._intents_by_key.get(intent.idempotency_key)
            if existing_owner is not None and existing_owner != intent.intent_id:
                return None
            new_intent_keys: list[str] = []
            new_idem_keys: list[str] = []
            new_recipient_keys: list[str] = []
            audit_start = len(self._audit)
            new_outbox_keys: list[str] = []
            try:
                self._intents[intent.intent_id] = intent
                new_intent_keys.append(intent.intent_id)
                self._intents_by_key[intent.idempotency_key] = intent.intent_id
                new_idem_keys.append(intent.idempotency_key)
                if self._fail_after_step == "intent":
                    raise RuntimeError("injected failure after intent")
                for recipient in recipients:
                    self._recipients[recipient.recipient_id] = recipient
                    new_recipient_keys.append(recipient.recipient_id)
                if self._fail_after_step == "recipients":
                    raise RuntimeError("injected failure after recipients")
                for event in audit:
                    self._audit.append(event)
                for out_event in outbox:
                    self._outbox[out_event.outbox_id] = out_event
                    new_outbox_keys.append(out_event.outbox_id)
            except BaseException:
                for key in new_intent_keys:
                    self._intents.pop(key, None)
                for key in new_idem_keys:
                    self._intents_by_key.pop(key, None)
                for key in new_recipient_keys:
                    self._recipients.pop(key, None)
                del self._audit[audit_start:]
                for key in new_outbox_keys:
                    self._outbox.pop(key, None)
                raise
            return intent

    async def save_intent(self, intent: NotificationIntent) -> NotificationIntent:
        with self._lock:
            existing_key = self._intents_by_key.get(intent.idempotency_key)
            if existing_key is not None and existing_key != intent.intent_id:
                raise NotificationConflictError(
                    f"idempotency key {intent.idempotency_key} already used by "
                    f"{existing_key}"
                )
            self._intents[intent.intent_id] = intent
            self._intents_by_key[intent.idempotency_key] = intent.intent_id
            return intent

    async def get_intent(
        self, intent_id: str, *, subject_person_id: str
    ) -> NotificationIntent | None:
        with self._lock:
            intent = self._intents.get(intent_id)
            if intent is None or intent.subject_person_id != subject_person_id:
                return None
            return intent

    async def get_intent_for_worker(self, intent_id: str) -> NotificationIntent | None:
        with self._lock:
            return self._intents.get(intent_id)

    async def get_intent_by_idempotency_key(
        self, idempotency_key: str, *, subject_person_id: str
    ) -> NotificationIntent | None:
        with self._lock:
            intent_id = self._intents_by_key.get(idempotency_key)
            if intent_id is None:
                return None
            intent = self._intents.get(intent_id)
            if intent is None or intent.subject_person_id != subject_person_id:
                return None
            return intent

    async def save_recipient(self, recipient: RecipientBinding) -> RecipientBinding:
        with self._lock:
            self._recipients[recipient.recipient_id] = recipient
            return recipient

    async def get_recipient(
        self, recipient_id: str, *, person_id: str
    ) -> RecipientBinding | None:
        with self._lock:
            recipient = self._recipients.get(recipient_id)
            if recipient is None or recipient.person_id != person_id:
                return None
            return recipient

    async def get_recipient_for_worker(
        self, recipient_id: str
    ) -> RecipientBinding | None:
        with self._lock:
            return self._recipients.get(recipient_id)

    async def list_recipients(
        self, intent_id: str, *, person_id: str
    ) -> tuple[RecipientBinding, ...]:
        with self._lock:
            selected = [
                item
                for item in self._recipients.values()
                if item.intent_id == intent_id and item.person_id == person_id
            ]
            return tuple(sorted(selected, key=lambda item: item.created_at))

    async def list_recipients_for_worker(
        self, intent_id: str
    ) -> tuple[RecipientBinding, ...]:
        with self._lock:
            selected = [
                item
                for item in self._recipients.values()
                if item.intent_id == intent_id
            ]
            return tuple(sorted(selected, key=lambda item: item.created_at))

    async def list_recipients_by_intent_subject(
        self, intent_id: str, *, subject_person_id: str
    ) -> tuple[RecipientBinding, ...]:
        with self._lock:
            intent = self._intents.get(intent_id)
            if intent is None or intent.subject_person_id != subject_person_id:
                return ()
            return await self.list_recipients_for_worker(intent_id)

    async def list_recipients_by_relationship(
        self, relationship_id: str
    ) -> tuple[RecipientBinding, ...]:
        with self._lock:
            selected = [
                item
                for item in self._recipients.values()
                if item.relationship_id == relationship_id
            ]
            return tuple(sorted(selected, key=lambda item: item.created_at))

    async def list_due_recipients(
        self, now: datetime, limit: int
    ) -> tuple[RecipientBinding, ...]:
        with self._lock:
            uncertain_recipients = {
                attempt.recipient_id
                for attempt in self._attempts.values()
                if attempt.status == "uncertain"
            }
            selected = [
                item
                for item in self._recipients.values()
                if item.status in ("pending", "failed", "in_progress")
                and item.recipient_id not in uncertain_recipients
                and item.is_due(now)
            ]
            selected.sort(key=lambda item: item.created_at)
            return tuple(selected[:limit])

    async def claim_recipient(
        self,
        recipient_id: str,
        attempt: DeliveryAttempt,
        now: datetime,
    ) -> tuple[RecipientBinding, DeliveryAttempt] | None:
        """Atomic claim with the same CAS semantics as SQLite: only
        pending/failed at the expected attempt count, or an expired lease
        of the same attempt count, may be claimed."""
        with self._lock:
            current = self._recipients.get(recipient_id)
            if current is None:
                return None
            expected_attempts = attempt.attempt_number - 1
            if current.status in ("pending", "failed") and (
                current.attempts == expected_attempts
            ):
                pass
            elif (
                current.status == "in_progress"
                and current.leased_until is not None
                and current.leased_until <= now
                and current.attempts == expected_attempts
            ):
                pass
            else:
                return None
            if not current.is_due(now):
                return None
            updated = replace(
                current,
                status="in_progress",
                attempts=attempt.attempt_number,
                fencing_token=attempt.fencing_token,
                leased_until=attempt.leased_until,
                next_attempt_at=None,
                updated_at=now,
            )
            self._recipients[recipient_id] = updated
            self._attempts[attempt.attempt_id] = attempt
            return (updated, attempt)

    async def save_attempt(self, attempt: DeliveryAttempt) -> DeliveryAttempt:
        with self._lock:
            self._attempts[attempt.attempt_id] = attempt
            return attempt

    async def get_attempt(self, attempt_id: str) -> DeliveryAttempt | None:
        with self._lock:
            return self._attempts.get(attempt_id)

    async def list_attempts(
        self, recipient_id: str, *, person_id: str
    ) -> tuple[DeliveryAttempt, ...]:
        with self._lock:
            recipient = self._recipients.get(recipient_id)
            if recipient is None or recipient.person_id != person_id:
                return ()
            selected = [
                item
                for item in self._attempts.values()
                if item.recipient_id == recipient_id
            ]
            return tuple(sorted(selected, key=lambda item: item.attempt_number))

    async def list_uncertain_attempts(
        self, limit: int
    ) -> tuple[DeliveryAttempt, ...]:
        with self._lock:
            selected = [
                attempt
                for attempt in self._attempts.values()
                if attempt.status == "uncertain"
            ]
            selected.sort(key=lambda item: item.started_at)
            return tuple(selected[:limit])

    async def save_receipt(self, receipt: DeliveryReceipt) -> DeliveryReceipt:
        with self._lock:
            existing_id = self._receipts_by_attempt.get(receipt.attempt_id)
            if existing_id is not None:
                return self._receipts[existing_id]
            self._receipts[receipt.receipt_id] = receipt
            self._receipts_by_attempt[receipt.attempt_id] = receipt.receipt_id
            return receipt

    async def complete_attempt_atomically(
        self,
        *,
        attempt_id: str,
        fencing_token: str,
        recipient: RecipientBinding,
        finished_attempt: DeliveryAttempt,
        receipt: DeliveryReceipt | None,
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> tuple[RecipientBinding, NotificationIntent | None] | None:
        """P0-A/P0-B/P0-C: complete ONLY when the attempt is still leased to
        this fencing token AND the current recipient row still carries the
        same token/status/attempt count; every mutation (recipient, attempt,
        receipt, audit, outbox, intent + terminal outbox) commits together
        and rolls back as one on any injected failure."""
        with self._lock:
            snapshot = (
                dict(self._recipients),
                dict(self._attempts),
                dict(self._receipts),
                dict(self._receipts_by_attempt),
                list(self._audit),
                dict(self._outbox),
                dict(self._intents),
                dict(self._intents_by_key),
            )
            try:
                from services.notification.domain import derive_intent_status

                leased = self._attempts.get(attempt_id)
                if (
                    leased is None
                    or leased.status not in ("leased", "uncertain")
                    or leased.fencing_token != fencing_token
                ):
                    return None
                if (
                    leased.intent_id != recipient.intent_id
                    or leased.recipient_id != recipient.recipient_id
                    or leased.attempt_number != finished_attempt.attempt_number
                ):
                    return None
                current = self._recipients.get(recipient.recipient_id)
                if (
                    current is None
                    or current.fencing_token != fencing_token
                    or current.status != "in_progress"
                    or current.attempts != finished_attempt.attempt_number
                ):
                    return None
                self._recipients[recipient.recipient_id] = recipient
                if self._fail_after_step == "complete-recipient":
                    raise RuntimeError("injected failure after recipient")
                self._attempts[attempt_id] = finished_attempt
                if self._fail_after_step == "complete-attempt":
                    raise RuntimeError("injected failure after attempt")
                if receipt is not None:
                    self._receipts[receipt.receipt_id] = receipt
                    self._receipts_by_attempt[receipt.attempt_id] = receipt.receipt_id
                if self._fail_after_step == "complete-receipt":
                    raise RuntimeError("injected failure after receipt")
                self._audit.extend(audit)
                if self._fail_after_step == "complete-audit":
                    raise RuntimeError("injected failure after audit")
                for event in outbox:
                    self._outbox[event.outbox_id] = event
                if self._fail_after_step == "complete-outbox":
                    raise RuntimeError("injected failure after outbox")
                intent = self._intents.get(recipient.intent_id)
                if intent is not None:
                    selected = [
                        item
                        for item in self._recipients.values()
                        if item.intent_id == recipient.intent_id
                    ]
                    derived = derive_intent_status(
                        tuple(sorted(selected, key=lambda item: item.created_at))
                    )
                    if derived != intent.status:
                        intent = replace(
                            intent,
                            status=derived,
                            updated_at=now,
                            delivered_at=(
                                now if derived == "delivered" else intent.delivered_at
                            ),
                        )
                        self._intents[intent.intent_id] = intent
                        if derived in ("delivered", "dead_lettered", "cancelled"):
                            # P0-B: terminal outbox event with a STABLE id
                            # written inside the same transaction (idempotent).
                            event_id = (
                                f"{intent.idempotency_key}:intent.{derived}"
                            )
                            self._outbox[event_id] = NotificationOutboxEvent(
                                outbox_id=event_id,
                                event_id=event_id,
                                topic=f"notification.intent.{derived}",
                                payload=intent.to_dict(),
                                created_at=now,
                            )
                if self._fail_after_step == "complete-intent":
                    raise RuntimeError("injected failure after intent")
                return (recipient, intent)
            except BaseException:
                (
                    self._recipients,
                    self._attempts,
                    self._receipts,
                    self._receipts_by_attempt,
                    self._audit,
                    self._outbox,
                    self._intents,
                    self._intents_by_key,
                ) = snapshot
                raise

    async def get_receipt_by_attempt(
        self, attempt_id: str
    ) -> DeliveryReceipt | None:
        with self._lock:
            receipt_id = self._receipts_by_attempt.get(attempt_id)
            if receipt_id is None:
                return None
            return self._receipts.get(receipt_id)

    async def list_receipts(
        self, intent_id: str, *, person_id: str
    ) -> tuple[DeliveryReceipt, ...]:
        with self._lock:
            intent = self._intents.get(intent_id)
            if intent is None or intent.subject_person_id != person_id:
                return ()
            selected = [
                item
                for item in self._receipts.values()
                if item.intent_id == intent_id
            ]
            return tuple(sorted(selected, key=lambda item: item.delivered_at))

    async def cancel_intent_atomically(
        self,
        *,
        intent: NotificationIntent,
        recipients: tuple[RecipientBinding, ...],
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> NotificationIntent | None:
        with self._lock:
            snapshot = (
                dict(self._recipients),
                dict(self._intents),
                dict(self._intents_by_key),
                list(self._audit),
                dict(self._outbox),
            )
            try:
                from services.notification.domain import derive_intent_status

                for recipient in recipients:
                    self._recipients[recipient.recipient_id] = recipient
                if self._fail_after_step == "cancel-recipients":
                    raise RuntimeError("injected failure after recipients cancelled")
                derived = derive_intent_status(
                    tuple(
                        sorted(
                            (
                                item
                                for item in self._recipients.values()
                                if item.intent_id == intent.intent_id
                            ),
                            key=lambda item: item.created_at,
                        )
                    )
                )
                if derived != intent.status:
                    intent = replace(
                        intent,
                        status=derived,
                        updated_at=now,
                        delivered_at=(
                            now if derived == "delivered" else intent.delivered_at
                        ),
                    )
                self._intents[intent.intent_id] = intent
                if self._fail_after_step == "cancel-intent":
                    raise RuntimeError("injected failure after intent cancelled")
                for event in audit:
                    self._audit.append(event)
                for out_event in outbox:
                    self._outbox[out_event.outbox_id] = out_event
                return intent
            except BaseException:
                (
                    self._recipients,
                    self._intents,
                    self._intents_by_key,
                    self._audit,
                    self._outbox,
                ) = snapshot
                raise

    async def cancel_recipient_atomically(
        self,
        *,
        recipient_id: str,
        recipient: RecipientBinding,
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> tuple[RecipientBinding, NotificationIntent | None] | None:
        with self._lock:
            current = self._recipients.get(recipient_id)
            if current is None or current.status in (
                "delivered",
                "dead_lettered",
                "cancelled",
            ):
                return None
            self._recipients[recipient_id] = recipient
            self._audit.extend(audit)
            for event in outbox:
                self._outbox[event.outbox_id] = event
            from services.notification.domain import derive_intent_status

            intent = self._intents.get(recipient.intent_id)
            if intent is not None:
                selected = [
                    item
                    for item in self._recipients.values()
                    if item.intent_id == recipient.intent_id
                ]
                derived = derive_intent_status(
                    tuple(sorted(selected, key=lambda item: item.created_at))
                )
                if derived != intent.status:
                    intent = replace(
                        intent,
                        status=derived,
                        updated_at=now,
                        delivered_at=(
                            now if derived == "delivered" else intent.delivered_at
                        ),
                    )
                    self._intents[intent.intent_id] = intent
            return (recipient, intent)

    async def cancel_recipient_and_attempt_atomically(
        self,
        *,
        recipient_id: str,
        attempt_id: str,
        fencing_token: str,
        recipient: RecipientBinding,
        finished_attempt: DeliveryAttempt,
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> tuple[RecipientBinding, NotificationIntent | None] | None:
        """Worker fail-closed cancel that ALSO terminates the leased attempt
        atomically (no leased residue): the attempt is finished only while
        still leased to this fencing token; the recipient is cancelled only
        while not terminal; repeated calls are idempotent no-ops."""
        with self._lock:
            snapshot = (
                dict(self._recipients),
                dict(self._attempts),
                list(self._audit),
                dict(self._outbox),
                dict(self._intents),
                dict(self._intents_by_key),
            )
            try:
                from services.notification.domain import derive_intent_status

                current = self._recipients.get(recipient_id)
                if current is None or current.status in (
                    "delivered",
                    "dead_lettered",
                    "cancelled",
                ):
                    return None
                self._recipients[recipient_id] = recipient
                leased = self._attempts.get(attempt_id)
                if (
                    leased is not None
                    and leased.status == "leased"
                    and leased.fencing_token == fencing_token
                ):
                    self._attempts[attempt_id] = finished_attempt
                self._audit.extend(audit)
                for event in outbox:
                    self._outbox[event.outbox_id] = event
                intent = self._intents.get(recipient.intent_id)
                if intent is not None:
                    selected = [
                        item
                        for item in self._recipients.values()
                        if item.intent_id == recipient.intent_id
                    ]
                    derived = derive_intent_status(
                        tuple(sorted(selected, key=lambda item: item.created_at))
                    )
                    if derived != intent.status:
                        intent = replace(
                            intent,
                            status=derived,
                            updated_at=now,
                            delivered_at=(
                                now if derived == "delivered" else intent.delivered_at
                            ),
                        )
                        self._intents[intent.intent_id] = intent
                return (recipient, intent)
            except BaseException:
                (
                    self._recipients,
                    self._attempts,
                    self._audit,
                    self._outbox,
                    self._intents,
                    self._intents_by_key,
                ) = snapshot
                raise

    async def replay_recipient_atomically(
        self,
        *,
        recipient_id: str,
        recipient: RecipientBinding,
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> tuple[RecipientBinding, NotificationIntent | None] | None:
        with self._lock:
            current = self._recipients.get(recipient_id)
            if current is None or current.status not in ("failed", "dead_lettered"):
                return None
            if current.relationship_status != "active":
                return None
            # P0-5: transition derived from the DB row facts.
            recipient = replace(
                recipient,
                next_attempt_at=now,
                updated_at=now,
            )
            self._recipients[recipient_id] = recipient
            self._audit.extend(audit)
            for event in outbox:
                self._outbox[event.outbox_id] = event
            from services.notification.domain import derive_intent_status

            intent = self._intents.get(recipient.intent_id)
            if intent is not None:
                selected = [
                    item
                    for item in self._recipients.values()
                    if item.intent_id == recipient.intent_id
                ]
                derived = derive_intent_status(
                    tuple(sorted(selected, key=lambda item: item.created_at))
                )
                if derived != intent.status:
                    intent = replace(
                        intent,
                        status=derived,
                        updated_at=now,
                        delivered_at=(
                            now if derived == "delivered" else intent.delivered_at
                        ),
                    )
                    self._intents[intent.intent_id] = intent
            return (recipient, intent)

    async def cancel_relationship_atomically(
        self,
        *,
        relationship_id: str,
        relationship_status: RelationshipStatusValue,
        reason: CancelReason,
        actor_person_id: str | None,
        now: datetime,
    ) -> tuple[tuple[RecipientBinding, ...], tuple[NotificationIntent | None, ...]]:
        with self._lock:
            from services.notification.domain import (
                cancelled_recipient,
                derive_intent_status,
            )

            candidates = [
                item
                for item in self._recipients.values()
                if item.relationship_id == relationship_id
            ]
            cancelled: list[RecipientBinding] = []
            touched: set[str] = set()
            for current in candidates:
                if current.status in ("delivered", "dead_lettered", "cancelled"):
                    continue
                updated = replace(
                    cancelled_recipient(current, reason=reason, now=now),
                    relationship_status=relationship_status,
                )
                self._recipients[current.recipient_id] = updated
                cancelled.append(updated)
                touched.add(current.intent_id)
            event_id = f"relationship:{relationship_id}.inactive"
            for recipient in cancelled:
                self._audit.append(
                    NotificationAuditEvent(
                        event_id=f"audit:{event_id}:{recipient.recipient_id}",
                        action="recipient.cancel",
                        actor_person_id=actor_person_id,
                        intent_id=recipient.intent_id,
                        recipient_id=recipient.recipient_id,
                        payload={
                            "relationship_id": relationship_id,
                            "status": relationship_status,
                            "reason": reason,
                        },
                        created_at=now,
                    )
                )
            if cancelled:
                self._outbox[event_id] = NotificationOutboxEvent(
                    outbox_id=event_id,
                    event_id=event_id,
                    topic="notification.relationship.inactive",
                    payload={
                        "relationship_id": relationship_id,
                        "status": relationship_status,
                        "reason": reason,
                        "recipient_ids": [item.recipient_id for item in cancelled],
                    },
                    created_at=now,
                )
            intents: list[NotificationIntent | None] = []
            for intent_id in sorted(touched):
                intent = self._intents.get(intent_id)
                if intent is None:
                    intents.append(None)
                    continue
                selected = [
                    item
                    for item in self._recipients.values()
                    if item.intent_id == intent_id
                ]
                derived = derive_intent_status(
                    tuple(sorted(selected, key=lambda item: item.created_at))
                )
                if derived != intent.status:
                    intent = replace(
                        intent,
                        status=derived,
                        updated_at=now,
                        delivered_at=(
                            now if derived == "delivered" else intent.delivered_at
                        ),
                    )
                    self._intents[intent_id] = intent
                intents.append(intent)
            return (tuple(cancelled), tuple(intents))

    async def scan_intents(
        self, statuses: tuple[IntentStatus, ...] | None = None
    ) -> tuple[NotificationIntent, ...]:
        with self._lock:
            selected = [
                item
                for item in self._intents.values()
                if statuses is None or item.status in statuses
            ]
            return tuple(sorted(selected, key=lambda item: item.created_at))

    async def append_audit(self, event: NotificationAuditEvent) -> None:
        with self._lock:
            self._audit.append(event)

    async def enqueue_outbox(self, event: NotificationOutboxEvent) -> None:
        with self._lock:
            self._outbox[event.outbox_id] = event

    # -- test helpers ------------------------------------------------------

    def audit_events(self) -> tuple[NotificationAuditEvent, ...]:
        with self._lock:
            return tuple(sorted(self._audit, key=lambda item: item.created_at))

    def outbox_events(self) -> tuple[NotificationOutboxEvent, ...]:
        with self._lock:
            return tuple(sorted(self._outbox.values(), key=lambda item: item.created_at))


__all__ = ["InMemoryNotificationStore"]
