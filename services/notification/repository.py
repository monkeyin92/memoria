"""Storage seam and delivery-channel port for the notification domain.

``NotificationStore`` is the only persistence contract the service depends
on.  Adapters: ``InMemoryNotificationStore`` (unit tests / Control API dev
fixtures), ``SqliteNotificationStore`` (local development) and
``PostgresNotificationStore`` (production authority with FORCE RLS,
section 11.7 / PR-17).

``DeliveryChannelPort`` is the external side of channel delivery: real
WeChat / SMS / phone-call adapters live outside this package and must be
wired by the deployment.  This package ships no credentials and treats a
``channel_receipt_id`` returned by the adapter as the only proof of
delivery.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from services.notification.domain import (
    CancelReason,
    Channel,
    DeliveryAttempt,
    DeliveryReceipt,
    IntentStatus,
    NotificationFence,
    NotificationIntent,
    RecipientBinding,
    RelationshipSnapshot,
)
from services.policy.receipts import PolicyReceiptV2


@dataclass(frozen=True, slots=True)
class VerifiedPolicyReceipt:
    """Authoritative verification outcome for a PolicyReceiptV2 (cross-domain
    contract): the verifier reads the receipt AND the current evidence and
    runs the Policy V2 fence validators
    (:func:`services.policy.receipts.receipt_fence_valid` /
    ``exact_evidence_fence_valid``).  ``receipt_fence_valid`` proves the
    literal identity + full-context canonical hash + decision window;
    ``exact_evidence_valid`` additionally proves every referenced
    consent/relationship/binding evidence is still present and active.
    Consumers then match the receipt field-by-field against their fence -
    they never compare the full-context hash to a local fingerprint."""

    receipt: PolicyReceiptV2
    receipt_fence_valid: bool
    exact_evidence_valid: bool


@dataclass(frozen=True)
class NotificationAuditEvent:
    """Append-only audit trail entry for notification mutations."""

    event_id: str
    action: str
    actor_person_id: str | None
    intent_id: str | None
    recipient_id: str | None
    payload: dict[str, object]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class NotificationOutboxEvent:
    """Transactional outbox entry; ``event_id`` is the idempotency key."""

    outbox_id: str
    event_id: str
    topic: str
    payload: dict[str, object]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ChannelResult:
    """Result returned by an external delivery-channel adapter."""

    ok: bool
    channel_receipt_id: str | None = None
    error_code: str | None = None


class DeliveryChannelPort(Protocol):
    """External channel adapter contract (implemented outside this package).

    ``send`` must only return ``ok=True`` with a ``channel_receipt_id`` when
    the external provider confirmed delivery; returning ``ok=False`` with an
    ``error_code`` must not fabricate a receipt.

    IDEMPOTENCY / CANCELLATION contract (P0):
    - ``attempt.logical_delivery_key``
      (``{intent_id}:{recipient_id}:{channel}``) is the STABLE provider
      idempotency key shared by every retry: the adapter MUST pass it to
      the external provider, so a repeated invocation (attempt N after a
      timeout, or a re-claiming worker's attempt N+1) returns the SAME
      external receipt and never produces a second external side effect.
      The attempt_number is NEVER part of the key.
    - the adapter MUST respond to ``asyncio.CancelledError`` promptly and
      propagate it; a worker's ``asyncio.wait_for`` deadline can only
      cancel the local coroutine - an adapter that swallows cancellation
      can keep running past the lease, so provider-side idempotency is the
      hard guarantee against double delivery, never the local timeout.
    - ``reconcile`` queries the provider for the authoritative outcome of
      a possibly-in-flight logical delivery.  Returning a
      ``ChannelResult`` with ``ok=True`` + the same receipt proves
      delivery; ``ok=False`` with an error proves a definitive
      non-delivery (retry may continue on the SAME key/channel);
      returning ``None`` means UNKNOWN (the provider has no answer) - the
      worker must NOT retry, NOT fall back to another channel and keep the
      attempt ``uncertain`` until an authoritative answer exists.
    """

    channel: Channel

    async def send(
        self,
        *,
        recipient: RecipientBinding,
        content: str,
        attempt: DeliveryAttempt,
    ) -> ChannelResult: ...

    async def reconcile(
        self,
        *,
        logical_delivery_key: str,
        channel_receipt_id: str | None = None,
    ) -> ChannelResult | None: ...


class NotificationReceiptVerifier(Protocol):
    """Authoritative policy-receipt verification for notification decisions
    (P0-4 / cross-domain contract).  Implementations read the receipt from
    the policy engine's receipt store, validate it against the CURRENT
    evidence with the Policy V2 fence validators and return the verified
    outcome; ``None`` means the receipt does not exist."""

    async def verify(
        self,
        receipt_id: str,
        *,
        fence: NotificationFence,
        actor_person_id: str,
        subject_person_id: str,
        capability: str,
        now: datetime,
    ) -> VerifiedPolicyReceipt | None: ...



class RelationshipResolver(Protocol):
    """Authoritative relationship/binding snapshot source (P0-5).
    Recipients are resolved and validated against these snapshots; a
    caller-claimed ``relationship_status`` string is never trusted."""

    async def get_relationship(
        self, relationship_id: str, *, now: datetime
    ) -> RelationshipSnapshot | None: ...


class InMemoryNotificationReceiptVerifier:
    """Development/test receipt store for notification decisions (P0-4).
    Receipts are registered from the authoritative policy-engine side with
    an EXPLICIT verified assertion (the test is the verifier); the service
    only sees registered rows.  The registered ``receipt_fence_valid`` /
    ``exact_evidence_valid`` flags stand in for the Policy V2 fence
    validators that the production verifier runs."""

    def __init__(self) -> None:
        self._receipts: dict[str, VerifiedPolicyReceipt] = {}

    def register(
        self,
        receipt: PolicyReceiptV2,
        *,
        receipt_fence_valid: bool = True,
        exact_evidence_valid: bool = True,
    ) -> None:
        self._receipts[receipt.receipt_id] = VerifiedPolicyReceipt(
            receipt=receipt,
            receipt_fence_valid=receipt_fence_valid,
            exact_evidence_valid=exact_evidence_valid,
        )

    def clear(self) -> None:
        self._receipts.clear()

    async def verify(
        self,
        receipt_id: str,
        *,
        fence: NotificationFence,
        actor_person_id: str,
        subject_person_id: str,
        capability: str,
        now: datetime,
    ) -> VerifiedPolicyReceipt | None:
        return self._receipts.get(receipt_id)


class OperatorAuthorizationPort(Protocol):
    """Operator authorization for cancels/replays (P0 contract): a real
    internal-service step-up + audit check.  ``notification_operator`` is
    NOT a canonical capability, so this port is intentionally separate from
    policy receipts; production wiring is required - an unconfigured port
    fails closed."""

    async def verify_operator(
        self,
        authorization_ref: str,
        *,
        actor_person_id: str,
        now: datetime,
    ) -> bool: ...


class InMemoryOperatorAuthorizationPort:
    """Development/test operator authorization (step-up simulation).
    Authorization refs are registered from the ops side; nothing is
    accepted by default."""

    def __init__(self) -> None:
        self._authorized: set[tuple[str, str]] = set()

    def register(self, authorization_ref: str, actor_person_id: str) -> None:
        self._authorized.add((authorization_ref, actor_person_id))

    async def verify_operator(
        self,
        authorization_ref: str,
        *,
        actor_person_id: str,
        now: datetime,
    ) -> bool:
        return (authorization_ref, actor_person_id) in self._authorized


class InMemoryRelationshipResolver:
    """Development/test relationship snapshot source (P0-5).  Snapshots are
    registered from the authoritative identity/binding side."""

    def __init__(self) -> None:
        self._relationships: dict[str, RelationshipSnapshot] = {}

    def register(self, snapshot: RelationshipSnapshot) -> None:
        self._relationships[snapshot.relationship_id] = snapshot

    def clear(self) -> None:
        self._relationships.clear()

    async def get_relationship(
        self, relationship_id: str, *, now: datetime
    ) -> RelationshipSnapshot | None:
        return self._relationships.get(relationship_id)


class PrincipalAuthorityPort(Protocol):
    """HTTP principal authority (wiring P0): the authenticated actor is
    derived from the request's credentials/session - a route NEVER accepts
    ``actor_person_id`` from the JSON body.  ``None`` means
    unauthenticated (401)."""

    async def authenticate(
        self,
        *,
        headers: Mapping[str, str],
        now: datetime,
    ) -> str | None: ...


class RuntimeSessionAuthorityPort(Protocol):
    """Current runtime session/profile authority (wiring P0): the fence,
    active subject and binding/profile come from the SESSION state, never
    from the JSON body.  ``None`` means the session cannot be
    authenticated/confirmed (403)."""

    async def current_fence(
        self,
        *,
        actor_person_id: str,
        now: datetime,
    ) -> NotificationFence | None: ...

    async def current_active_subject(
        self,
        *,
        actor_person_id: str,
        now: datetime,
    ) -> str | None: ...

    async def current_safety_event(
        self,
        *,
        actor_person_id: str,
        now: datetime,
    ) -> tuple[str, datetime] | None:
        """Authoritative (source_event_id, occurred_at) of the CURRENT
        safety event in the signed session (wiring P0): the route verifies
        the body's source_event_id/occurred_at against this - a caller can
        never attach a receipt to an arbitrary event.  ``None`` = no
        current safety event (403 for crisis enqueue)."""
        ...


class NotificationStore(Protocol):
    """Persistence contract used by ``NotificationService``.

    ``claim_recipient`` must atomically attach ``attempt`` to ``recipient``
    (updating status / attempts / fencing token / lease) or return ``None``
    when another worker won the race or the recipient is no longer claimable.

    Read methods are split (third review):
    - API-scoped reads carry the explicit authenticated subject/person and
      the PostgreSQL adapter scopes them with the ``notification_api`` role
      plus subject/person context (cross-subject id lookups return nothing);
    - ``*_for_worker`` methods carry the global ``notification_worker`` role
      and must only be reached from worker flows or after operator
      authorization inside the service - never from generic queries.

    ``save_intent_atomically`` commits the intent, its recipients, audit and
    outbox rows in ONE transaction (section 11.6).
    """

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

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
        """First-write-wins atomic create: returns ``intent`` when this call
        committed the row set, or ``None`` when a concurrent writer already
        owns the idempotency key (the caller re-reads and compares)."""
        ...

    async def save_intent(self, intent: NotificationIntent) -> NotificationIntent: ...

    async def get_intent(
        self, intent_id: str, *, subject_person_id: str
    ) -> NotificationIntent | None: ...

    async def get_intent_for_worker(
        self, intent_id: str
    ) -> NotificationIntent | None: ...

    async def get_intent_by_idempotency_key(
        self, idempotency_key: str, *, subject_person_id: str
    ) -> NotificationIntent | None: ...

    async def save_recipient(self, recipient: RecipientBinding) -> RecipientBinding: ...

    async def cancel_intent_atomically(
        self,
        *,
        intent: NotificationIntent,
        recipients: tuple[RecipientBinding, ...],
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> NotificationIntent | None:
        """Atomic whole-intent cancellation (P0-D / section 11.6): all
        recipients AND the intent transition together in ONE transaction
        with their audit/outbox rows - never a partially-cancelled intent.
        Authorization is proven by the service before calling."""
        ...

    async def cancel_recipient_atomically(
        self,
        *,
        recipient_id: str,
        recipient: RecipientBinding,
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> tuple[RecipientBinding, NotificationIntent | None] | None:
        """Atomic recipient cancellation (P0-3/P0-4): the recipient only
        transitions to ``cancelled`` when it is not already terminal
        (delivered/dead_lettered/cancelled stay untouched); audit, stable
        outbox and the derived intent status commit in the same transaction.
        Returns ``None`` when the recipient was already terminal (no-op, no
        duplicate side effects on replay)."""
        ...

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
        """Worker fail-closed atomic cancel (TOCTOU gap): cancels the
        recipient AND terminates the leased attempt (``failed`` with a
        stable reason, lease + fencing cleared) in ONE transaction, with
        audit / stable outbox / derived intent status.  The attempt is only
        terminated when it is still ``leased`` to ``fencing_token`` (a
        newer lease is never overwritten); the recipient is only cancelled
        when not terminal.  Repeated calls are idempotent no-ops returning
        ``None`` - no duplicate audit/outbox, no leased-attempt residue."""
        ...

    async def replay_recipient_atomically(
        self,
        *,
        recipient_id: str,
        recipient: RecipientBinding,
        audit: tuple[NotificationAuditEvent, ...],
        outbox: tuple[NotificationOutboxEvent, ...],
        now: datetime,
    ) -> tuple[RecipientBinding, NotificationIntent | None] | None:
        """Atomic manual replay (P0-3): only failed/dead_lettered recipients
        (never terminal, never pending/in_progress) transition back to
        failed with a fresh attempt window; audit + stable outbox + derived
        intent status commit in one transaction."""
        ...

    async def cancel_relationship_atomically(
        self,
        *,
        relationship_id: str,
        relationship_status: str,
        reason: CancelReason,
        actor_person_id: str | None,
        now: datetime,
    ) -> tuple[tuple[RecipientBinding, ...], tuple[NotificationIntent | None, ...]]:
        """Atomic batch cancellation of every recipient bound to a
        relationship (P0-3/P0-5): the recipients are selected from the
        AUTHORITATIVE store inside the transaction and updated with a
        ``relationship_id`` condition; terminal recipients stay untouched;
        audit/outbox rows are written ONLY for actual changes with stable
        event ids (same-content replays are no-ops, different-content
        replays update instead of being silently swallowed); all affected
        intents are re-derived in the same transaction."""
        ...

    async def get_recipient(
        self, recipient_id: str, *, person_id: str
    ) -> RecipientBinding | None: ...

    async def get_recipient_for_worker(
        self, recipient_id: str
    ) -> RecipientBinding | None: ...

    async def list_recipients(
        self, intent_id: str, *, person_id: str
    ) -> tuple[RecipientBinding, ...]: ...

    async def list_recipients_by_intent_subject(
        self, intent_id: str, *, subject_person_id: str
    ) -> tuple[RecipientBinding, ...]:
        """API-scoped read: recipients of an intent the caller owns as its
        subject (join intent->recipients by subject; never the worker role)."""
        ...

    async def list_recipients_for_worker(
        self, intent_id: str
    ) -> tuple[RecipientBinding, ...]: ...

    async def list_recipients_by_relationship(
        self, relationship_id: str
    ) -> tuple[RecipientBinding, ...]: ...

    async def list_due_recipients(
        self, now: datetime, limit: int
    ) -> tuple[RecipientBinding, ...]: ...

    async def claim_recipient(
        self,
        recipient_id: str,
        attempt: DeliveryAttempt,
        now: datetime,
    ) -> tuple[RecipientBinding, DeliveryAttempt] | None: ...

    async def save_attempt(self, attempt: DeliveryAttempt) -> DeliveryAttempt: ...

    async def get_attempt(self, attempt_id: str) -> DeliveryAttempt | None: ...

    async def list_attempts(
        self, recipient_id: str, *, person_id: str
    ) -> tuple[DeliveryAttempt, ...]: ...

    async def list_uncertain_attempts(
        self, limit: int
    ) -> tuple[DeliveryAttempt, ...]:
        """Worker scan of UNKNOWN-outcome attempts (timeout with no
        authoritative reconcile answer yet); the dispatch cycle reconciles
        each one against the SAME stable logical delivery key before any
        re-claim.  Their recipients are excluded from ``list_due_recipients``
        so no retry/fallback happens while the outcome is unknown."""
        ...

    async def save_receipt(self, receipt: DeliveryReceipt) -> DeliveryReceipt: ...

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
        """Atomic delivery completion (fifth review / section 11.6): in ONE
        transaction, guarded by the fencing token, apply the recipient state
        transition, finish the attempt, insert the delivery receipt (when
        delivered), append audit/outbox rows and derive + persist the intent
        status from the same transaction's recipients.  Returns ``None`` when
        the attempt is no longer leased to this fencing token (stale worker)."""
        ...

    async def get_receipt_by_attempt(self, attempt_id: str) -> DeliveryReceipt | None: ...

    async def list_receipts(
        self, intent_id: str, *, person_id: str
    ) -> tuple[DeliveryReceipt, ...]: ...

    async def scan_intents(
        self, statuses: tuple[IntentStatus, ...] | None = None
    ) -> tuple[NotificationIntent, ...]: ...

    async def append_audit(self, event: NotificationAuditEvent) -> None: ...

    async def enqueue_outbox(self, event: NotificationOutboxEvent) -> None: ...


__all__ = [
    "ChannelResult",
    "DeliveryChannelPort",
    "InMemoryOperatorAuthorizationPort",
    "InMemoryNotificationReceiptVerifier",
    "InMemoryRelationshipResolver",
    "NotificationReceiptVerifier",
    "NotificationAuditEvent",
    "NotificationOutboxEvent",
    "NotificationStore",
    "OperatorAuthorizationPort",
    "RelationshipResolver",
]
