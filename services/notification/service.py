"""Application facade for the notification domain.

``NotificationService`` is the only entry point the crisis / emergency /
care pipelines need.  It owns:

- intent creation with content-minimized templates (validated, idempotent,
  and fully decoupled from channel delivery so a notification failure never
  blocks the safety turn response, section 10.6);
- the lease + fencing-token worker protocol (``claim_due`` /
  ``complete_attempt``) with injectable exponential backoff + jitter,
  maximum retries, fallback channels and dead lettering;
- wrong-contact / relationship-inactive cancellation with audited reasons
  that forbid any further delivery;
- append-only audit and transactional-outbox events for every mutation.

Real channel adapters (``DeliveryChannelPort``) live outside this package:
no credentials are stored here and a delivery is only ever recorded with a
``channel_receipt_id`` returned by the adapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from services.notification.domain import (
    ALL_CHANNELS,
    ALL_RECIPIENT_ROLES,
    CANCEL_REASON_BY_RELATIONSHIP_STATUS,
    INACTIVE_RELATIONSHIP_STATUSES,
    INTENT_KIND_RECIPIENT_ROLES,
    INTENT_KIND_TEMPLATES,
    MINIMAL_NOTIFICATION_CONTENT_OBLIGATION,
    NOTIFY_EMERGENCY_CONTACT_OBLIGATION,
    RELATIONSHIP_ACTIVE,
    AttemptStatus,
    BackoffPolicy,
    CancelReason,
    Channel,
    DeliveryAttempt,
    DeliveryReceipt,
    IntentKind,
    IntentStatus,
    NotificationConflictError,
    NotificationFence,
    NotificationFencingError,
    NotificationIntent,
    NotificationNotFoundError,
    NotificationPolicyError,
    NotificationStateError,
    ReceiptNotVerifiedError,
    RecipientBinding,
    RecipientRole,
    RecipientSpec,
    RelationshipInactiveError,
    RelationshipNotVerifiedError,
    RelationshipSnapshot,
    TemplateKey,
    cancelled_recipient,
    classify_failure,
    new_id,
    recipient_evidence_ok,
    render_notification_content,
    replace_recipient,
    validate_template_params,
    verify_notification_receipt,
)
from services.notification.repository import (
    ChannelResult,
    DeliveryChannelPort,
    NotificationAuditEvent,
    NotificationOutboxEvent,
    NotificationReceiptVerifier,
    NotificationStore,
    OperatorAuthorizationPort,
    RelationshipResolver,
)
from services.policy.receipts import PolicyReceiptV2

MAX_RECIPIENTS_PER_INTENT = 20

_OUTBOX_TOPIC = {
    "intent.created": "notification.intent.created",
    "intent.delivered": "notification.intent.delivered",
    "intent.dead_lettered": "notification.intent.dead_lettered",
    "intent.cancelled": "notification.intent.cancelled",
    "recipient.cancelled": "notification.recipient.cancelled",
    "relationship.inactive": "notification.relationship.inactive",
    "manual.replay": "notification.manual_replay",
}


@dataclass(frozen=True, slots=True)
class DispatchSummary:
    """Outcome of one worker dispatch cycle."""

    attempted: int = 0
    delivered: int = 0
    failed: int = 0
    cancelled: int = 0
    uncertain: int = 0


def _now(value: datetime | None) -> datetime:
    return value.astimezone(UTC) if value is not None else datetime.now(UTC)


class NotificationService:
    """Deep-module facade: create, dispatch, complete, cancel, replay."""

    def __init__(
        self,
        store: NotificationStore,
        *,
        lease_seconds: float = 120.0,
        max_retries: int = 3,
        backoff: BackoffPolicy | None = None,
        receipt_verifier: NotificationReceiptVerifier | None = None,
        relationship_resolver: RelationshipResolver | None = None,
        operator_authorizer: OperatorAuthorizationPort | None = None,
        send_timeout_seconds: float = 30.0,
        reconcile_timeout_seconds: float = 5.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if not 0 < send_timeout_seconds < lease_seconds:
            raise ValueError(
                "send_timeout_seconds must be positive and smaller than "
                "lease_seconds so a hanging adapter can never outlive the "
                "lease and race a re-claiming worker"
            )
        if not 0 < reconcile_timeout_seconds < lease_seconds:
            raise ValueError(
                "reconcile_timeout_seconds must be positive and smaller "
                "than lease_seconds so a hanging reconcile provider can "
                "never stall the whole worker cycle"
            )
        if not 1 <= max_retries <= 10:
            raise ValueError("max_retries must be within [1, 10]")
        self._store = store
        self._lease_seconds = lease_seconds
        self._max_retries = max_retries
        self._backoff = backoff if backoff is not None else BackoffPolicy()
        self._receipt_verifier = receipt_verifier
        self._relationship_resolver = relationship_resolver
        self._operator_authorizer = operator_authorizer
        self._send_timeout_seconds = send_timeout_seconds
        self._reconcile_timeout_seconds = reconcile_timeout_seconds
        #: Adapter tasks that outlived their hard deadline (the coroutine
        #: swallowed CancelledError): they are DETACHED - the worker never
        #: waits on them again - and any late external effect is
        #: deduplicated by the STABLE logical delivery key.  Kept for
        #: observability/cleanup; the worker process exit may log pending
        #: tasks for adapters that never return.
        self._detached_sends: set[asyncio.Task[ChannelResult]] = set()
        #: Late reconcile callbacks owned by the service (bounded by the
        #: same shutdown drain as detached sends): they are never fire-
        #: and-forget - the worker shutdown cancels/reaps them so a loop
        #: teardown cannot silently drop a pending late-delivery record.
        self._late_reconcile_tasks: set[asyncio.Task[None]] = set()

    async def shutdown_detached_sends(self, *, timeout: float = 5.0) -> None:
        """Cancel and reap every adapter task that outlived its hard
        deadline (cancellation-swallowing adapters) and every owned late
        reconcile callback.  This is bounded: tasks that still refuse to
        finish after ``timeout`` are cancelled and dropped from the
        tracking sets (their late external effects are covered by the
        STABLE logical delivery key + the reconcile cycle) so the worker
        shutdown can never hang on them.  NOTE: a task that refuses even
        cancellation is NOT reaped by this method - the worker MUST keep
        the adapter's outcome reconcilable via the stable key and must not
        claim the task is gone (observable via the tracking set/logs)."""
        pending = list(self._detached_sends) + list(self._late_reconcile_tasks)
        if not pending:
            return
        self._detached_sends.clear()
        self._late_reconcile_tasks.clear()
        tasks = pending
        for task in tasks:
            task.cancel()
        done, _ = await asyncio.wait(tasks, timeout=timeout)
        for task in tasks:
            if task not in done:
                task.cancel()
                # NOT reaped: the adapter is still running.  Observability
                # only - the stable logical delivery key is the guarantee,
                # never a claim that the task finished.

    @property
    def receipt_verifier(self) -> NotificationReceiptVerifier | None:
        return self._receipt_verifier

    @property
    def relationship_resolver(self) -> RelationshipResolver | None:
        return self._relationship_resolver

    @property
    def operator_authorizer(self) -> OperatorAuthorizationPort | None:
        return self._operator_authorizer

    # ------------------------------------------------------------------
    # Intent creation (decoupled from delivery)
    # ------------------------------------------------------------------

    async def create_intent(
        self,
        *,
        intent_kind: IntentKind,
        subject_person_id: str,
        source_event_id: str,
        idempotency_key: str,
        policy_receipt_id: str,
        fence: NotificationFence,
        template_key: TemplateKey,
        template_params: dict[str, str],
        reason_code: str,
        script_version: str,
        recipients: Sequence[RecipientSpec],
        occurred_at: datetime | None = None,
        actor_person_id: str,
        now: datetime | None = None,
    ) -> NotificationIntent:
        """Create a notification intent without touching any channel.

        Idempotent on ``idempotency_key``: a replay with identical content
        returns the existing intent; a replay with different content raises
        ``NotificationConflictError``.  Never raises because a channel is
        down, and never blocks the safety turn response (section 10.6).
        """
        timestamp = _now(now)
        self._enforce_fence(fence, now=timestamp)
        if not actor_person_id or not actor_person_id.strip():
            raise NotificationPolicyError(
                "actor_person_id is required for notification intents (P0-4)"
            )
        if actor_person_id != fence.actor_person_id:
            raise NotificationPolicyError(
                "actor_person_id must match the fence actor (P0-4)"
            )
        if not idempotency_key or len(idempotency_key) > 128:
            raise NotificationPolicyError("idempotency_key must be 1..128 chars")
        if not policy_receipt_id or len(policy_receipt_id) > 128:
            raise NotificationPolicyError(
                "policy_receipt_id must be 1..128 chars (recipients resolve "
                "against the policy receipt of the crisis/emergency decision)"
            )
        # P0-4: the receipt must be verified against the authoritative
        # receipt store for this exact fence; a caller-supplied string or
        # fingerprint is never trusted.
        receipt = await self._verify_receipt(
            receipt_id=policy_receipt_id,
            fence=fence,
            actor_person_id=actor_person_id,
            subject_person_id=subject_person_id,
            capability="crisis_notification",
            purpose="crisis_response",
            required_obligations=frozenset(
                {
                    NOTIFY_EMERGENCY_CONTACT_OBLIGATION,
                    MINIMAL_NOTIFICATION_CONTENT_OBLIGATION,
                }
            ),
            now=timestamp,
        )
        if receipt is None:
            raise ReceiptNotVerifiedError(
                f"policy receipt {policy_receipt_id} not verified"
            )
        assert receipt is not None
        if intent_kind not in INTENT_KIND_RECIPIENT_ROLES:
            raise NotificationPolicyError(f"unknown intent kind: {intent_kind}")
        if INTENT_KIND_TEMPLATES[intent_kind] != template_key:
            raise NotificationPolicyError(
                f"intent kind {intent_kind} requires template "
                f"{INTENT_KIND_TEMPLATES[intent_kind]}, got {template_key}"
            )
        allowed_roles = INTENT_KIND_RECIPIENT_ROLES[intent_kind]
        if not recipients:
            raise NotificationPolicyError("at least one recipient is required")
        if len(recipients) > MAX_RECIPIENTS_PER_INTENT:
            raise NotificationPolicyError(
                f"at most {MAX_RECIPIENTS_PER_INTENT} recipients per intent"
            )
        resolved: list[tuple[RecipientSpec, RelationshipSnapshot, tuple[Channel, ...]]] = []
        seen_recipients: set[tuple[str, str]] = set()
        for spec in recipients:
            # P0-5 / third review: recipients resolve ONLY against the
            # authoritative relationship service; the caller requests a
            # relationship and channel preferences and nothing else.
            snapshot = await self._verify_recipient_relationship(
                spec=spec,
                subject_person_id=subject_person_id,
                intent_kind=intent_kind,
                allowed_roles=allowed_roles,
                now=timestamp,
            )
            # Policy-crisis rule coupling (red team 12): the resolved
            # relationship must be part of the PolicyReceiptV2 evidence set
            # AND the receipt's parameterized NOTIFY obligation must name
            # exactly this role and intent kind.  Any other active
            # relationship of the same subject cannot ride the same receipt.
            if not recipient_evidence_ok(
                receipt,
                snapshot=snapshot,
                intent_kind=intent_kind,
            ):
                raise NotificationPolicyError(
                    f"relationship {snapshot.relationship_id} "
                    f"(snapshot {snapshot.snapshot_id or '<none>'!r}) is not "
                    "covered by the policy receipt evidence/obligation for "
                    f"intent kind {intent_kind}; recipient fails closed"
                )
            channels = spec.channels or _default_channels(snapshot.role)
            if not channels:
                raise NotificationPolicyError(
                    f"recipient {snapshot.role} has no channels"
                )
            unknown = set(channels) - ALL_CHANNELS
            if unknown:
                raise NotificationPolicyError(
                    f"unknown channels for {snapshot.role}: {sorted(unknown)}"
                )
            if len(channels) != len(set(channels)):
                raise NotificationPolicyError("channels must not repeat")
            # Fifth review: one authoritative (person, role) per intent -
            # duplicate relationships/people must never produce duplicate
            # notifications.
            identity = (snapshot.person_id, snapshot.role)
            if identity in seen_recipients:
                raise NotificationPolicyError(
                    f"duplicate recipient {snapshot.person_id} ({snapshot.role})"
                )
            seen_recipients.add(identity)
            resolved.append((spec, snapshot, channels))

        occurred = occurred_at or timestamp
        if occurred.tzinfo is None or occurred.utcoffset() is None:
            raise NotificationPolicyError(
                "occurred_at must be timezone-aware (P0-5)"
            )
        params = {
            **template_params,
            "occurred_at": occurred.astimezone(UTC).isoformat(),
            "script_version": script_version,
        }
        validate_template_params(template_key, params)

        existing = await self._store.get_intent_by_idempotency_key(
            idempotency_key, subject_person_id=subject_person_id
        )
        if existing is not None:
            existing_recipients = await self._store.list_recipients_by_intent_subject(
                existing.intent_id, subject_person_id=subject_person_id
            )
            if not self._same_intent(
                existing, existing_recipients, intent_kind, subject_person_id,
                source_event_id, policy_receipt_id, template_key, params,
                reason_code, script_version,
                [(spec, snapshot, channels) for spec, snapshot, channels in resolved],
            ):
                raise NotificationConflictError(
                    f"idempotency key {idempotency_key} reused with different content"
                )
            return existing

        intent = NotificationIntent(
            intent_id=new_id(),
            idempotency_key=idempotency_key,
            intent_kind=intent_kind,
            subject_person_id=subject_person_id,
            source_event_id=source_event_id,
            policy_receipt_id=policy_receipt_id,
            session_id=fence.session_id,
            epoch=fence.epoch,
            binding_id=fence.binding_id,
            binding_version=fence.binding_version,
            runtime_profile_id=fence.runtime_profile_id,
            actor_person_id=fence.actor_person_id,
            fence_context_hash=fence.fingerprint(),
            device_id=fence.device_id,
            subject_revision=fence.subject_revision,
            valid_until=fence.valid_until,
            template_key=template_key,
            template_params=dict(params),
            reason_code=reason_code,  # type: ignore[arg-type]
            script_version=script_version,
            occurred_at=occurred,
            status="pending",
            created_at=timestamp,
            updated_at=timestamp,
        )
        bound_recipients: list[RecipientBinding] = []
        for spec, snapshot, channels in resolved:
            bound_recipients.append(
                RecipientBinding(
                    recipient_id=new_id(),
                    intent_id=intent.intent_id,
                    person_id=snapshot.person_id,
                    role=snapshot.role,
                    relationship_id=spec.relationship_id,
                    relationship_status=snapshot.status,
                    relationship_snapshot_id=snapshot.snapshot_id,
                    relationship_revision=snapshot.revision,
                    channels=channels,
                    channel_index=0,
                    status="pending",
                    attempts=0,
                    max_retries=self._max_retries,
                    next_attempt_at=None,
                    leased_until=None,
                    fencing_token=None,
                    last_error_code=None,
                    valid_from=snapshot.valid_from or timestamp,
                    valid_until=snapshot.valid_until,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                )
        # Third review / section 11.6: intent + recipients + audit + outbox
        # commit in ONE store transaction (first-write-wins on the
        # idempotency key); a mid-way failure leaves nothing.
        created = await self._store.save_intent_atomically(
            intent,
            recipients=tuple(bound_recipients),
            audit=(
                NotificationAuditEvent(
                    event_id=f"audit:{idempotency_key}:intent.create",
                    action="intent.create",
                    actor_person_id=actor_person_id,
                    intent_id=intent.intent_id,
                    recipient_id=None,
                    payload=intent.to_dict(),
                    created_at=timestamp,
                ),
            ),
            outbox=(
                NotificationOutboxEvent(
                    outbox_id=new_id(),
                    event_id=f"{idempotency_key}:intent.created",
                    topic=_OUTBOX_TOPIC["intent.created"],
                    payload=intent.to_dict(),
                    created_at=timestamp,
                ),
            ),
            authenticated_actor_person_id=actor_person_id,
            authenticated_subject_person_id=subject_person_id,
        )
        if created is not None:
            return intent
        # A concurrent writer won the idempotency key: re-read the winner
        # (API-scoped) and compare - same content returns it, different
        # content is a stable conflict.
        winner = await self._store.get_intent_by_idempotency_key(
            idempotency_key, subject_person_id=subject_person_id
        )
        if winner is None:
            raise NotificationStateError(
                "concurrent create race: winner intent disappeared"
            )
        winner_recipients = await self._store.list_recipients_by_intent_subject(
            winner.intent_id, subject_person_id=subject_person_id
        )
        if not self._same_intent(
            winner, winner_recipients, intent_kind, subject_person_id,
            source_event_id, policy_receipt_id, template_key, params,
            reason_code, script_version,
            [(spec, snapshot, channels) for spec, snapshot, channels in resolved],
        ):
            raise NotificationConflictError(
                f"idempotency key {idempotency_key} reused with different content"
            )
        return winner

    # ------------------------------------------------------------------
    # Worker protocol: lease + fencing tokens
    # ------------------------------------------------------------------

    async def claim_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 10,
    ) -> tuple[DeliveryAttempt, ...]:
        """Atomically lease all currently due recipients (worker side)."""
        timestamp = _now(now)
        due = await self._store.list_due_recipients(timestamp, limit)
        claimed: list[DeliveryAttempt] = []
        for recipient in due:
            channel = recipient.current_channel()
            attempt = DeliveryAttempt(
                attempt_id=new_id(),
                intent_id=recipient.intent_id,
                recipient_id=recipient.recipient_id,
                attempt_number=recipient.attempts + 1,
                channel=channel,
                # STABLE provider idempotency key shared by every retry of
                # this recipient+channel (NEVER the attempt_number): a late
                # attempt-1 worker and a re-claiming attempt-2 worker
                # deduplicate on the SAME key at the provider.
                logical_delivery_key=f"{recipient.intent_id}:{recipient.recipient_id}:{channel}",
                status="leased",
                fencing_token=new_id(),
                leased_until=timestamp + timedelta(seconds=self._lease_seconds),
                started_at=timestamp,
            )
            result = await self._store.claim_recipient(
                recipient.recipient_id, attempt, timestamp
            )
            if result is not None:
                claimed.append(result[1])
        return tuple(claimed)

    async def complete_attempt(
        self,
        *,
        attempt_id: str,
        fencing_token: str,
        result: str,
        channel_receipt_id: str | None = None,
        error_code: str | None = None,
        actor_person_id: str | None = None,
        now: datetime | None = None,
    ) -> RecipientBinding:
        """Complete a leased attempt (delivered or failed).

        ``result="delivered"`` requires a ``channel_receipt_id`` returned by
        the channel adapter: delivery is never fabricated.  A stale worker
        (mismatched fencing token) is rejected with
        ``NotificationFencingError`` and changes no state.
        """
        timestamp = _now(now)
        attempt = await self._store.get_attempt(attempt_id)
        if attempt is None:
            raise NotificationNotFoundError(f"attempt {attempt_id} does not exist")
        recipient = await self._store.get_recipient_for_worker(attempt.recipient_id)
        if recipient is None:
            raise NotificationNotFoundError(
                f"recipient {attempt.recipient_id} does not exist"
            )
        if recipient.fencing_token != fencing_token:
            raise NotificationFencingError(
                f"fencing token mismatch for attempt {attempt_id}; "
                "a newer lease superseded this attempt"
            )
        if result == "delivered":
            if not channel_receipt_id or len(channel_receipt_id) > 128:
                raise NotificationStateError(
                    "delivery requires a channel_receipt_id from the channel adapter"
                )
            return await self._complete_delivered_atomic(
                recipient, attempt, channel_receipt_id, actor_person_id, timestamp
            )
        if result == "failed":
            if not error_code:
                raise NotificationStateError("failed completion requires an error_code")
            return await self._complete_failed_atomic(
                recipient, attempt, error_code, actor_person_id, timestamp
            )
        if result == "uncertain":
            # Timeout/unknown outcome: the attempt stays OWNED by this
            # fencing token but is no longer leased; the recipient is NOT
            # advanced (no attempt_number bump, no failed/delivered state)
            # and is excluded from re-claiming until a reconcile resolves
            # the authoritative outcome.  Retry/fallback NEVER happens on
            # an unknown outcome.
            return await self._complete_uncertain_atomic(
                recipient, attempt, error_code, actor_person_id, timestamp
            )
        raise NotificationStateError(f"unknown completion result: {result}")

    async def _complete_uncertain_atomic(
        self,
        recipient: RecipientBinding,
        attempt: DeliveryAttempt,
        error_code: str | None,
        actor_person_id: str | None,
        now: datetime,
    ) -> RecipientBinding:
        if not error_code:
            raise NotificationStateError(
                "uncertain completion requires an error_code"
            )
        finished = replace(
            attempt,
            status="uncertain",
            error_code=error_code,
            finished_at=None,
        )
        # The recipient keeps its in_progress state, fencing token and
        # attempts count: the outcome is UNKNOWN, so nothing is advanced
        # and re-claiming is excluded until reconcile resolves it.
        unchanged = replace(recipient, updated_at=now)
        committed = await self._store.complete_attempt_atomically(
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.fencing_token,
            recipient=unchanged,
            finished_attempt=finished,
            receipt=None,
            audit=(
                NotificationAuditEvent(
                    event_id=f"audit:{attempt.attempt_id}:uncertain",
                    action="notification.attempt.uncertain",
                    actor_person_id=actor_person_id,
                    intent_id=attempt.intent_id,
                    recipient_id=attempt.recipient_id,
                    payload={"error_code": error_code},
                    created_at=now,
                ),
            ),
            outbox=(),
            now=now,
        )
        if committed is None:
            raise NotificationFencingError(
                f"fencing token mismatch for attempt {attempt.attempt_id}; "
                "a newer lease superseded this attempt"
            )
        return committed[0]

    async def _complete_or_stale(
        self,
        attempt: DeliveryAttempt,
        *,
        result: str,
        channel_receipt_id: str | None = None,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> RecipientBinding | None:
        """Stale-token-safe completion (P0): a completion whose fencing
        token was superseded by a newer worker's re-claim is a safe no-op -
        it changes no state, never overwrites the newer lease and never
        aborts the batch.  Returns the committed recipient, or ``None``
        when the attempt was already stale."""
        try:
            return await self.complete_attempt(
                attempt_id=attempt.attempt_id,
                fencing_token=attempt.fencing_token,
                result=result,
                channel_receipt_id=channel_receipt_id,
                error_code=error_code,
                now=now,
            )
        except NotificationFencingError:
            return None

    async def dispatch_due(
        self,
        channels: Mapping[Channel, DeliveryChannelPort],
        *,
        now: datetime | None = None,
        limit: int = 10,
    ) -> DispatchSummary:
        """Reference worker cycle: claim -> channel.send -> complete.

        External adapters are looked up by channel name; a missing adapter is
        a permanent failure (dead letter) so misconfiguration never loops.
        """
        summary = DispatchSummary()
        # First resolve any UNKNOWN (timeout) outcomes from previous
        # workers: reconcile asks the provider for the authoritative state
        # under the SAME stable logical delivery key.  Unknown stays
        # uncertain (no retry, no fallback); only a definitive
        # delivered / not-delivered answer moves the attempt on.
        await self._reconcile_uncertain_attempts(channels, now=now)
        attempts = await self.claim_due(now=now, limit=limit)
        for attempt in attempts:
            summary = DispatchSummary(
                attempted=summary.attempted + 1,
                delivered=summary.delivered,
                failed=summary.failed,
                cancelled=summary.cancelled,
                uncertain=summary.uncertain,
            )
            adapter = channels.get(attempt.channel)
            if adapter is None:
                await self._complete_or_stale(
                    attempt,
                    result="failed",
                    error_code="channel_invalid",
                    now=now,
                )
                summary = DispatchSummary(
                    attempted=summary.attempted,
                    delivered=summary.delivered,
                    failed=summary.failed + 1,
                    cancelled=summary.cancelled,
                    uncertain=summary.uncertain,
                )
                continue
            recipient = await self._store.get_recipient_for_worker(attempt.recipient_id)
            intent = await self._store.get_intent_for_worker(attempt.intent_id)
            if recipient is None or intent is None:
                await self._complete_or_stale(
                    attempt,
                    result="failed",
                    error_code="channel_invalid",
                    now=now,
                )
                summary = DispatchSummary(
                    attempted=summary.attempted,
                    delivered=summary.delivered,
                    failed=summary.failed + 1,
                    cancelled=summary.cancelled,
                    uncertain=summary.uncertain,
                )
                continue
            # P0 TOCTOU + lease aging: the relationship, the policy receipt
            # and the fence are re-resolved against the REAL current UTC
            # clock IMMEDIATELY before THIS attempt's external send - a slow
            # earlier send in the batch must never let a later attempt pass
            # an authorization check with a stale timestamp.  A revocation /
            # contact change / dispute / binding bump / receipt or fence
            # expiry between enqueue and dispatch fails closed with a
            # PRECISE restricted reason that flows unchanged into the
            # recipient cancel, the attempt error_code, the audit event and
            # the outbox event.
            recheck_now = datetime.now(UTC)
            reason = await self._relationship_authorization_for_send(
                intent, recipient, now=recheck_now
            )
            if reason is None:
                reason = await self._receipt_authorization_for_send(
                    intent, now=recheck_now
                )
            if reason is not None:
                await self._fail_closed_recipient_and_attempt(
                    recipient, attempt, reason=reason, timestamp=recheck_now
                )
                summary = DispatchSummary(
                    attempted=summary.attempted,
                    delivered=summary.delivered,
                    failed=summary.failed,
                    cancelled=summary.cancelled + 1,
                )
                continue
            # P0 lease race: re-verify ownership and remaining lease against
            # the REAL clock immediately before the external send and bound
            # the send by min(configured timeout, remaining lease), so no
            # adapter call can outlive the lease and race a re-claiming
            # worker.
            current_recipient = await self._store.get_recipient_for_worker(
                attempt.recipient_id
            )
            if (
                current_recipient is None
                or current_recipient.fencing_token != attempt.fencing_token
            ):
                # Another worker re-claimed this recipient (lease raced):
                # we no longer own the attempt; the stale completion CAS
                # would reject us anyway - skip without touching state.
                continue
            remaining_lease = attempt.leased_until - recheck_now
            if remaining_lease <= timedelta(0):
                # Our lease already expired before we could send: fail this
                # attempt atomically ONLY if we still own the token; a
                # re-claimed token makes the completion a safe no-op.
                await self._complete_or_stale(
                    attempt,
                    result="failed",
                    error_code="timeout",
                    now=now,
                )
                summary = DispatchSummary(
                    attempted=summary.attempted,
                    delivered=summary.delivered,
                    failed=summary.failed + 1,
                    cancelled=summary.cancelled,
                    uncertain=summary.uncertain,
                )
                continue
            bounded_timeout = min(
                self._send_timeout_seconds,
                remaining_lease.total_seconds(),
            )
            content = render_notification_content(
                intent.template_key, intent.template_params
            )
            # P0 worker resilience: every external send is bounded by the
            # configured timeout (strictly smaller than the lease).  A
            # raised exception or a timeout fails THIS attempt atomically
            # with a stable retryable error code and the batch continues;
            # every completion treats a stale fencing token (re-claimed by
            # a newer worker) as a safe no-op that changes no state and
            # never aborts the batch.
            send_task = asyncio.create_task(
                adapter.send(
                    recipient=recipient, content=content, attempt=attempt
                )
            )
            # HARD deadline: ``asyncio.wait`` returns when the timeout
            # elapses WITHOUT waiting for the task's cancellation to
            # propagate - a cancellation-swallowing adapter would make
            # ``wait_for`` hang forever.  A detached task may still reach
            # the provider later; the provider-side idempotency key is the
            # STABLE logical delivery key
            # (intent:recipient:channel, shared by every retry - never the
            # attempt_number), which is the hard guarantee that no second
            # external effect is produced.
            done, _ = await asyncio.wait({send_task}, timeout=bounded_timeout)
            if send_task not in done:
                # Cancel first (a well-behaved adapter aborts promptly);
                # a cancellation-swallowing adapter is detached and its
                # LATE result is reconciled against the same logical key.
                send_task.cancel()
                self._detached_sends.add(send_task)
                self._attach_late_result_handler(send_task, attempt)
                committed = await self._complete_or_stale(
                    attempt,
                    result="uncertain",
                    error_code="timeout_unknown",
                    now=now,
                )
                if committed is not None:
                    summary = DispatchSummary(
                        attempted=summary.attempted,
                        delivered=summary.delivered,
                        failed=summary.failed,
                        cancelled=summary.cancelled,
                        uncertain=summary.uncertain + 1,
                    )
                continue
            try:
                outcome: ChannelResult = send_task.result()
            except Exception:
                # CancelledError is NOT caught here: it propagates (the
                # lease/recovery semantics are the worker's shutdown path).
                committed = await self._complete_or_stale(
                    attempt,
                    result="failed",
                    error_code="channel_unavailable",
                    now=now,
                )
                if committed is not None:
                    summary = DispatchSummary(
                        attempted=summary.attempted,
                        delivered=summary.delivered,
                        failed=summary.failed + 1,
                        cancelled=summary.cancelled,
                        uncertain=summary.uncertain,
                    )
                continue
            if outcome.ok and outcome.channel_receipt_id:
                committed = await self._complete_or_stale(
                    attempt,
                    result="delivered",
                    channel_receipt_id=outcome.channel_receipt_id,
                    now=now,
                )
                if committed is not None:
                    summary = DispatchSummary(
                        attempted=summary.attempted,
                        delivered=summary.delivered + 1,
                        failed=summary.failed,
                        cancelled=summary.cancelled,
                        uncertain=summary.uncertain,
                    )
            else:
                committed = await self._complete_or_stale(
                    attempt,
                    result="failed",
                    error_code=outcome.error_code or "channel_unavailable",
                    now=now,
                )
                if committed is not None:
                    summary = DispatchSummary(
                        attempted=summary.attempted,
                        delivered=summary.delivered,
                        failed=summary.failed + 1,
                        cancelled=summary.cancelled,
                        uncertain=summary.uncertain,
                    )
        return summary

    async def _reconcile_uncertain_attempts(
        self,
        channels: Mapping[Channel, DeliveryChannelPort],
        *,
        now: datetime | None = None,
    ) -> None:
        """Resolve UNKNOWN outcomes (P0): for every ``uncertain`` attempt,
        ask the SAME channel adapter for the authoritative outcome of the
        SAME stable logical delivery key.

        - ``ok=True`` + receipt -> the delivery DID happen: complete
          delivered (idempotent, same fencing token CAS).
        - ``ok=False`` + error -> authoritative NOT delivered: complete
          failed (retry continues on the SAME key/channel, never a
          fallback channel).
        - ``None`` -> still UNKNOWN: the attempt stays ``uncertain`` and
          the recipient stays excluded from claiming - NO retry and NO
          fallback until an authoritative answer exists.
        """
        if not channels:
            return
        attempts = await self._store.list_uncertain_attempts(limit=100)
        for attempt in attempts:
            adapter = channels.get(attempt.channel)
            if adapter is None:
                # No adapter for this channel: unknown stays unknown.
                continue
            try:
                outcome = await asyncio.wait_for(
                    adapter.reconcile(
                        logical_delivery_key=attempt.logical_delivery_key
                    ),
                    timeout=self._reconcile_timeout_seconds,
                )
            except (TimeoutError, Exception):
                # A hanging or failing reconcile provider is NOT an
                # authoritative answer: the attempt stays uncertain and
                # the batch continues (bounded by reconcile_timeout).
                continue
            if outcome is None:
                continue
            if outcome.ok and outcome.channel_receipt_id:
                await self._complete_or_stale(
                    attempt,
                    result="delivered",
                    channel_receipt_id=outcome.channel_receipt_id,
                    now=now,
                )
            else:
                await self._complete_or_stale(
                    attempt,
                    result="failed",
                    error_code=outcome.error_code or "channel_unavailable",
                    now=now,
                )

    def _attach_late_result_handler(
        self,
        send_task: asyncio.Task[ChannelResult],
        attempt: DeliveryAttempt,
    ) -> None:
        """Consume the LATE result of a detached send: if the provider
        confirms delivery AFTER the worker already marked the attempt
        ``uncertain``, the same fencing token CAS records the delivery; if
        the attempt was re-claimed or completed in the meantime, the stale
        completion is a safe no-op.  The late task's exception is consumed
        here so it never crashes the loop as an unhandled task error."""

        def _on_done(task: asyncio.Task[ChannelResult]) -> None:
            self._detached_sends.discard(task)
            if task.cancelled():
                return
            try:
                outcome = task.result()
            except Exception:
                return
            if outcome.ok and outcome.channel_receipt_id:
                self._reconcile_late_delivery(attempt, outcome.channel_receipt_id)

        send_task.add_done_callback(_on_done)

    def _reconcile_late_delivery(
        self, attempt: DeliveryAttempt, channel_receipt_id: str
    ) -> None:
        """Owned late-delivery reconcile callback: only succeeds when the
        attempt is STILL uncertain with the SAME fencing token - a
        re-claimed or completed attempt is never overwritten.  The task is
        tracked in the service's owner set so shutdown drains it instead of
        dropping a pending callback at loop teardown."""

        async def _late() -> None:
            try:
                await self._complete_or_stale(
                    attempt,
                    result="delivered",
                    channel_receipt_id=channel_receipt_id,
                )
            except Exception:
                return

        task = asyncio.create_task(_late())
        self._late_reconcile_tasks.add(task)
        task.add_done_callback(self._late_reconcile_tasks.discard)

    async def _relationship_authorization_for_send(
        self,
        intent: NotificationIntent,
        recipient: RecipientBinding,
        *,
        now: datetime,
    ) -> CancelReason | None:
        """Re-resolve the authoritative relationship right before an
        external send (P0 TOCTOU): same subject/person/role/snapshot
        revision, active and valid at ``now``; the persisted binding
        evidence must match exactly.  Returns the PRECISE restricted cancel
        reason when the authorization no longer holds, ``None`` when it is
        still good.  Mismatches map deterministically:
        revoked/unknown/missing -> relationship_revoked,
        expired -> relationship_expired, disputed -> relationship_disputed,
        binding bump -> authorization_revoked."""
        resolver = self._relationship_resolver
        if resolver is None:
            return "authorization_revoked"
        snapshot = await resolver.get_relationship(
            recipient.relationship_id, now=now
        )
        if snapshot is None:
            return "relationship_revoked"
        if snapshot.status in INACTIVE_RELATIONSHIP_STATUSES:
            return CANCEL_REASON_BY_RELATIONSHIP_STATUS.get(
                snapshot.status, "relationship_revoked"
            )
        if not snapshot.is_active(now):
            return "relationship_expired"
        if snapshot.subject_person_id != intent.subject_person_id:
            return "relationship_revoked"
        if snapshot.person_id != recipient.person_id:
            return "relationship_revoked"
        if snapshot.role != recipient.role:
            return "relationship_revoked"
        if not recipient.relationship_snapshot_id:
            return "relationship_revoked"
        if snapshot.snapshot_id != recipient.relationship_snapshot_id:
            return "relationship_revoked"
        if snapshot.revision != recipient.relationship_revision:
            return "relationship_revoked"
        if (
            snapshot.binding_version is not None
            and snapshot.binding_version != intent.binding_version
        ):
            return "authorization_revoked"
        return None

    async def _receipt_authorization_for_send(
        self,
        intent: NotificationIntent,
        *,
        now: datetime,
    ) -> CancelReason | None:
        """Re-verify the policy receipt against the CURRENT evidence right
        before an external send (TOCTOU): consent revocation, binding
        version bump or receipt expiry between enqueue and dispatch fails
        closed.  A missing verifier is a fail-closed condition, never a
        bypass.  Returns the restricted reason:
        receipt expired -> authorization_expired, any other invalidity
        (consent revoked, binding bump, identity mismatch, missing receipt)
        -> authorization_revoked."""
        if self._receipt_verifier is None:
            return "authorization_revoked"
        if intent.valid_until is None:
            # Legacy NULL fence window: fail closed - never treated as
            # unlimited (migrations backfill created_at, which is already
            # in the past for such rows).
            return "authorization_expired"
        fence = NotificationFence(
            session_id=intent.session_id,
            epoch=intent.epoch,
            binding_id=intent.binding_id,
            binding_version=intent.binding_version,
            runtime_profile_id=intent.runtime_profile_id,
            actor_person_id=intent.actor_person_id,
            subject_person_id=intent.subject_person_id,
            device_id=intent.device_id,
            subject_revision=intent.subject_revision,
            valid_until=intent.valid_until,
        )
        if fence.is_expired(now):
            return "authorization_expired"
        record = await self._receipt_verifier.verify(
            receipt_id=intent.policy_receipt_id,
            fence=fence,
            actor_person_id=intent.actor_person_id,
            subject_person_id=intent.subject_person_id,
            capability="crisis_notification",
            now=now,
        )
        if record is None:
            return "authorization_revoked"
        receipt = record.receipt
        if not record.receipt_fence_valid or not record.exact_evidence_valid:
            if not receipt.created_at <= now < receipt.expires_at:
                return "authorization_expired"
            return "authorization_revoked"
        if not verify_notification_receipt(
            receipt,
            fence=fence,
            actor_person_id=intent.actor_person_id,
            subject_person_id=intent.subject_person_id,
            capability="crisis_notification",
            purpose="crisis_response",
            required_obligations=frozenset(
                {
                    NOTIFY_EMERGENCY_CONTACT_OBLIGATION,
                    MINIMAL_NOTIFICATION_CONTENT_OBLIGATION,
                }
            ),
            now=now,
        ):
            if not receipt.created_at <= now < receipt.expires_at:
                return "authorization_expired"
            return "authorization_revoked"
        return None

    async def _fail_closed_recipient_and_attempt(
        self,
        recipient: RecipientBinding,
        attempt: DeliveryAttempt,
        *,
        reason: CancelReason,
        timestamp: datetime,
    ) -> None:
        """Worker-side fail-closed cancel of a recipient whose relationship
        / policy authorization no longer holds at send time: the recipient
        AND the leased attempt terminate in ONE store transaction (no
        leased-attempt residue), with audit + stable outbox + derived
        intent status; no adapter was (or will be) called.  Repeated calls
        are idempotent no-ops."""
        updated = cancelled_recipient(recipient, reason=reason, now=timestamp)
        event_id = f"recipient:{recipient.recipient_id}.cancelled"
        finished_attempt = replace(
            attempt,
            status="failed",
            finished_at=timestamp,
            error_code=reason,
        )
        await self._store.cancel_recipient_and_attempt_atomically(
            recipient_id=recipient.recipient_id,
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.fencing_token,
            recipient=updated,
            finished_attempt=finished_attempt,
            audit=(
                NotificationAuditEvent(
                    event_id=f"audit:{event_id}",
                    action="recipient.cancel",
                    actor_person_id=None,
                    intent_id=recipient.intent_id,
                    recipient_id=recipient.recipient_id,
                    payload={**updated.to_dict(), "reason": reason},
                    created_at=timestamp,
                ),
            ),
            outbox=(
                NotificationOutboxEvent(
                    outbox_id=event_id,
                    event_id=event_id,
                    topic=_OUTBOX_TOPIC["recipient.cancelled"],
                    payload={**updated.to_dict(), "reason": reason},
                    created_at=timestamp,
                ),
            ),
            now=timestamp,
        )

    # ------------------------------------------------------------------
    # Cancellation / relationship fail-closed / manual replay
    # ------------------------------------------------------------------

    async def cancel_recipient(
        self,
        *,
        recipient_id: str,
        reason: CancelReason,
        actor_person_id: str | None = None,
        evidence_ref: str | None = None,
        authorization_ref: str | None = None,
        now: datetime | None = None,
    ) -> RecipientBinding:
        """Cancel a recipient (P0-5, terminal).

        Authorization is never caller-claimed:
        - ``user_request`` / ``wrong_contact``: the recipient themselves;
        - ``operator_override`` / ``wrong_contact``: an operator whose
          ``authorization_ref`` verifies against the authoritative operator
          authorization port (an evidence string is never a permission);
        - ``relationship_*``: only via ``mark_relationship_inactive`` with an
          authoritative relationship snapshot.
        """
        timestamp = _now(now)
        # Third review: the recipient themselves resolves through the
        # person-scoped read; anyone else must first prove operator
        # authorization before reaching the worker-authorized read/write.
        recipient = await self._store.get_recipient(
            recipient_id, person_id=actor_person_id or ""
        )
        if recipient is None:
            await self._require_operator(
                actor_person_id=actor_person_id,
                authorization_ref=authorization_ref,
                now=timestamp,
            )
            recipient = await self._store.get_recipient_for_worker(recipient_id)
        if recipient is None:
            raise NotificationNotFoundError(f"recipient {recipient_id} does not exist")
        if reason not in (
            "wrong_contact",
            "relationship_revoked",
            "relationship_expired",
            "relationship_disputed",
            "operator_override",
            "user_request",
        ):
            raise NotificationPolicyError(f"unknown cancel reason: {reason}")
        await self._authorize_cancel(
            recipient=recipient,
            reason=reason,
            actor_person_id=actor_person_id,
            authorization_ref=authorization_ref,
            now=timestamp,
        )
        updated = cancelled_recipient(recipient, reason=reason, now=timestamp)
        event_id = f"recipient:{recipient_id}.cancelled"
        # P0-3/P1-8: recipient cancel + audit + stable outbox + derived
        # intent status commit in ONE store transaction; a terminal
        # recipient (delivered/dead_lettered/cancelled) is a no-op that
        # never rewrites history or repeats side effects.
        committed = await self._store.cancel_recipient_atomically(
            recipient_id=recipient_id,
            recipient=updated,
            audit=(
                NotificationAuditEvent(
                    event_id=f"audit:{event_id}",
                    action="recipient.cancel",
                    actor_person_id=actor_person_id,
                    intent_id=recipient.intent_id,
                    recipient_id=recipient_id,
                    payload={
                        **updated.to_dict(),
                        "reason": reason,
                        "evidence_ref": evidence_ref,
                    },
                    created_at=timestamp,
                ),
            ),
            outbox=(
                NotificationOutboxEvent(
                    outbox_id=event_id,
                    event_id=event_id,
                    topic=_OUTBOX_TOPIC["recipient.cancelled"],
                    payload={
                        **updated.to_dict(),
                        "reason": reason,
                        "evidence_ref": evidence_ref,
                    },
                    created_at=timestamp,
                ),
            ),
            now=timestamp,
        )
        if committed is None:
            # Already terminal: history is preserved, nothing to do.
            return recipient
        return committed[0]

    async def cancel_intent(
        self,
        *,
        intent_id: str,
        reason: CancelReason,
        actor_person_id: str | None = None,
        evidence_ref: str | None = None,
        authorization_ref: str | None = None,
        now: datetime | None = None,
    ) -> NotificationIntent:
        """Cancel every recipient and the intent itself (terminal).

        Fifth review: authorization first - the intent's subject may cancel
        their own intent; anyone else must first prove operator
        authorization.  The subject path never borrows the worker role for
        the initial read, and never forces recipients through per-recipient
        operator checks."""
        timestamp = _now(now)
        intent = await self._store.get_intent(
            intent_id, subject_person_id=actor_person_id or ""
        )
        if intent is None:
            await self._require_operator(
                actor_person_id=actor_person_id,
                authorization_ref=authorization_ref,
                now=timestamp,
            )
            intent = await self._store.get_intent_for_worker(intent_id)
        if intent is None:
            raise NotificationNotFoundError(f"intent {intent_id} does not exist")
        # P0-D: one atomic store transaction cancels every recipient AND the
        # intent with audit + stable outbox rows; never a partially
        # cancelled intent.  Authorization was proven at the intent level.
        recipients = await self._store.list_recipients_for_worker(intent_id)
        cancelled_recipients: list[RecipientBinding] = []
        for recipient in recipients:
            if recipient.status in ("delivered", "dead_lettered", "cancelled"):
                # P0-4: terminal recipients stay untouched - history and
                # delivered facts are preserved.
                cancelled_recipients.append(recipient)
            else:
                cancelled_recipients.append(
                    cancelled_recipient(recipient, reason=reason, now=timestamp)
                )
        updated = _intent_status(intent, "cancelled", reason=reason, now=timestamp)
        outbox_events: list[NotificationOutboxEvent] = []
        for cancelled in cancelled_recipients:
            outbox_events.append(
                NotificationOutboxEvent(
                    outbox_id=f"{intent.idempotency_key}:recipient.{cancelled.recipient_id}.cancelled",
                    event_id=f"{intent.idempotency_key}:recipient.{cancelled.recipient_id}.cancelled",
                    topic=_OUTBOX_TOPIC["recipient.cancelled"],
                    payload={**cancelled.to_dict(), "reason": reason},
                    created_at=timestamp,
                )
            )
        outbox_events.append(
            NotificationOutboxEvent(
                outbox_id=f"{intent.idempotency_key}:intent.cancelled",
                event_id=f"{intent.idempotency_key}:intent.cancelled",
                topic=_OUTBOX_TOPIC["intent.cancelled"],
                payload={**updated.to_dict(), "reason": reason},
                created_at=timestamp,
            )
        )
        final = await self._store.cancel_intent_atomically(
            intent=updated,
            recipients=tuple(cancelled_recipients),
            audit=(
                NotificationAuditEvent(
                    event_id=f"audit:{intent.idempotency_key}:intent.cancel",
                    action="intent.cancel",
                    actor_person_id=actor_person_id,
                    intent_id=intent_id,
                    recipient_id=None,
                    payload={**updated.to_dict(), "reason": reason},
                    created_at=timestamp,
                ),
            ),
            outbox=tuple(outbox_events),
            now=timestamp,
        )
        # P0-4: the stored intent status is derived from the surviving
        # recipients (terminal recipients stay untouched).
        return final if final is not None else updated

    async def mark_relationship_inactive(
        self,
        *,
        relationship_id: str,
        actor_person_id: str | None = None,
        authorization_ref: str | None = None,
        now: datetime | None = None,
    ) -> tuple[RecipientBinding, ...]:
        """Fail closed on relationship revocation / expiry / dispute (P0-5).

        The status comes from the AUTHORITATIVE relationship snapshot, never
        from a caller-claimed string.  An operator must present a verified
        operator ``authorization_ref``.
        """
        timestamp = _now(now)
        resolver = self._relationship_resolver
        if resolver is None:
            raise RelationshipNotVerifiedError(
                "no authoritative relationship resolver configured; "
                "relationship events fail closed"
            )
        snapshot = await resolver.get_relationship(relationship_id, now=timestamp)
        if snapshot is None:
            raise RelationshipNotVerifiedError(
                f"relationship {relationship_id} not found in the authoritative store"
            )
        status = snapshot.status
        if status not in INACTIVE_RELATIONSHIP_STATUSES:
            raise RelationshipNotVerifiedError(
                f"relationship {relationship_id} status is {status}; "
                "not an inactive status"
            )
        await self._require_operator(
            actor_person_id=actor_person_id,
            authorization_ref=authorization_ref,
            now=timestamp,
        )
        reason = CANCEL_REASON_BY_RELATIONSHIP_STATUS[status]
        # P0-3/P0-5: the store selects the authoritative recipient set
        # inside the transaction, cancels with a relationship_id condition,
        # and writes audit/stable outbox ONLY for actual changes.
        cancelled, _ = await self._store.cancel_relationship_atomically(
            relationship_id=relationship_id,
            relationship_status=status,
            reason=reason,
            actor_person_id=actor_person_id,
            now=timestamp,
        )
        return cancelled

    async def replay_recipient(
        self,
        *,
        recipient_id: str,
        actor_person_id: str,
        authorization_ref: str,
        reason: str = "manual_replay",
        now: datetime | None = None,
    ) -> RecipientBinding:
        """Manual replay of a failed / dead-lettered recipient (third
        review): replay reaches the worker-authorized write path only after
        a VERIFIED operator receipt - a recipient can never replay
        themselves."""
        timestamp = _now(now)
        await self._require_operator(
            actor_person_id=actor_person_id,
            authorization_ref=authorization_ref,
            now=timestamp,
        )
        recipient = await self._store.get_recipient_for_worker(recipient_id)
        if recipient is None:
            raise NotificationNotFoundError(f"recipient {recipient_id} does not exist")
        if recipient.status == "cancelled":
            raise NotificationStateError(
                "cancelled recipients must not be replayed (delivery forbidden)"
            )
        if recipient.status not in ("failed", "dead_lettered"):
            raise NotificationStateError(
                f"replay is only allowed from failed/dead_lettered, "
                f"not {recipient.status}"
            )
        if recipient.relationship_status != RELATIONSHIP_ACTIVE:
            raise RelationshipInactiveError(
                f"relationship {recipient.relationship_id} is not active; "
                "replay fails closed"
            )
        updated = replace_recipient(
            recipient,
            status="failed",
            next_attempt_at=timestamp,
            last_error_code=None,
            updated_at=timestamp,
        )
        event_id = f"recipient:{recipient_id}.replay"
        # P0-3/P1-8: replay + audit + stable outbox + derived intent status
        # commit in ONE transaction.
        committed = await self._store.replay_recipient_atomically(
            recipient_id=recipient_id,
            recipient=updated,
            audit=(
                NotificationAuditEvent(
                    event_id=f"audit:{event_id}",
                    action="manual.replay",
                    actor_person_id=actor_person_id,
                    intent_id=recipient.intent_id,
                    recipient_id=recipient_id,
                    payload={**updated.to_dict(), "reason": reason},
                    created_at=timestamp,
                ),
            ),
            outbox=(
                NotificationOutboxEvent(
                    outbox_id=event_id,
                    event_id=event_id,
                    topic=_OUTBOX_TOPIC["manual.replay"],
                    payload={**updated.to_dict(), "reason": reason},
                    created_at=timestamp,
                ),
            ),
            now=timestamp,
        )
        if committed is None:
            raise NotificationStateError(
                f"replay is only allowed from failed/dead_lettered, "
                f"not {recipient.status}"
            )
        return committed[0]

    async def unsubscribe_recipient(
        self,
        *,
        recipient_id: str,
        actor_person_id: str | None = None,
        now: datetime | None = None,
    ) -> RecipientBinding:
        """Explicit unsubscribe (退订, P0-5): ONLY the recipient themselves
        may opt out; a third party with a mere evidence string is never
        accepted.  The recipient becomes terminal and can never be claimed
        or replayed again."""
        timestamp = _now(now)
        if not actor_person_id:
            raise NotificationPolicyError(
                "unsubscribe requires the recipient's actor_person_id"
            )
        recipient = await self._store.get_recipient(
            recipient_id, person_id=actor_person_id
        )
        if recipient is None:
            raise NotificationNotFoundError(f"recipient {recipient_id} does not exist")
        return await self.cancel_recipient(
            recipient_id=recipient_id,
            reason="user_request",
            actor_person_id=actor_person_id,
            now=timestamp,
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_intent(
        self, intent_id: str, *, actor_person_id: str
    ) -> NotificationIntent:
        """API-scoped read: only the intent's subject may read it; the
        store enforces the subject context (never the worker role)."""
        intent = await self._store.get_intent(
            intent_id, subject_person_id=actor_person_id
        )
        if intent is None:
            raise NotificationNotFoundError(f"intent {intent_id} does not exist")
        return intent

    async def get_intent_by_key(
        self, idempotency_key: str, *, actor_person_id: str
    ) -> NotificationIntent:
        """Resolve an intent by its idempotency key (worker convenience)."""
        intent = await self._store.get_intent_by_idempotency_key(
            idempotency_key, subject_person_id=actor_person_id
        )
        if intent is None:
            raise NotificationNotFoundError(
                f"intent with idempotency key {idempotency_key} does not exist"
            )
        return intent

    async def list_recipients(
        self, intent_id: str, *, actor_person_id: str
    ) -> tuple[RecipientBinding, ...]:
        """API-scoped read: only the recipient themselves sees their binding
        (person context; never the worker role)."""
        return await self._store.list_recipients(
            intent_id, person_id=actor_person_id
        )

    async def list_attempts(
        self, recipient_id: str, *, actor_person_id: str
    ) -> tuple[DeliveryAttempt, ...]:
        if (
            await self._store.get_recipient(
                recipient_id, person_id=actor_person_id
            )
            is None
        ):
            raise NotificationNotFoundError(f"recipient {recipient_id} does not exist")
        return await self._store.list_attempts(
            recipient_id, person_id=actor_person_id
        )

    async def list_receipts(
        self, intent_id: str, *, actor_person_id: str
    ) -> tuple[DeliveryReceipt, ...]:
        return await self._store.list_receipts(
            intent_id, person_id=actor_person_id
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _enforce_fence(fence: NotificationFence, *, now: datetime) -> None:
        """P0-4: the fence must be complete and the epoch strictly positive."""
        if fence is None:
            raise NotificationPolicyError(
                "notification intent requires a session/binding/profile fence"
            )
        if (
            not fence.session_id
            or not fence.binding_id
            or not fence.runtime_profile_id
            or not fence.actor_person_id
            or not fence.subject_person_id
        ):
            raise NotificationPolicyError(
                "fence must carry session_id, binding_id, runtime_profile_id, "
                "actor_person_id and subject_person_id"
            )
        if fence.epoch < 1:
            raise NotificationPolicyError("fence epoch must be >= 1")
        if fence.is_expired(now):
            raise NotificationPolicyError("fence expired; intent creation fails closed")

    async def _verify_receipt(
        self,
        *,
        receipt_id: str,
        fence: NotificationFence,
        actor_person_id: str | None,
        subject_person_id: str,
        capability: str,
        purpose: str,
        required_obligations: frozenset[str],
        now: datetime,
    ) -> PolicyReceiptV2 | None:
        """P0-4: verify the receipt against the authoritative receipt store
        and the CURRENT evidence (cross-domain contract): the verifier runs
        the Policy V2 fence validators and the consumer requires both
        verified flags (crisis delivery needs the exact evidence fence),
        then matches the identity fields field-by-field.  The full-context
        ``context_hash`` is never compared to the local fence fingerprint.
        Without a verifier everything fails closed."""
        verifier = self._receipt_verifier
        if verifier is None:
            raise ReceiptNotVerifiedError(
                "no authoritative receipt verifier configured; "
                "notification intents fail closed"
            )
        actor = actor_person_id or ""
        record = await verifier.verify(
            receipt_id,
            fence=fence,
            actor_person_id=actor,
            subject_person_id=subject_person_id,
            capability=capability,
            now=now,
        )
        if record is None:
            return None
        if not record.receipt_fence_valid or not record.exact_evidence_valid:
            return None
        if not verify_notification_receipt(
            record.receipt,
            fence=fence,
            actor_person_id=actor,
            subject_person_id=subject_person_id,
            capability=capability,
            purpose=purpose,
            required_obligations=required_obligations,
            now=now,
        ):
            return None
        return record.receipt

    async def _verify_recipient_relationship(
        self,
        *,
        spec: RecipientSpec,
        subject_person_id: str,
        intent_kind: IntentKind,
        allowed_roles: frozenset[RecipientRole],
        now: datetime,
    ) -> RelationshipSnapshot:
        """P0-5: recipients resolve against the authoritative relationship /
        binding service.  A caller-claimed active status is never enough."""
        resolver = self._relationship_resolver
        if resolver is None:
            raise RelationshipNotVerifiedError(
                "no authoritative relationship resolver configured; "
                "recipient resolution fails closed"
            )
        snapshot = await resolver.get_relationship(spec.relationship_id, now=now)
        if snapshot is None:
            raise RelationshipNotVerifiedError(
                f"relationship {spec.relationship_id} not found in the "
                "authoritative store"
            )
        if not snapshot.is_active(now):
            raise RelationshipInactiveError(
                f"relationship {spec.relationship_id} is {snapshot.status}; "
                "delivery fails closed"
            )
        if snapshot.subject_person_id != subject_person_id:
            raise RelationshipNotVerifiedError(
                "relationship subject does not match the intent subject"
            )
        if snapshot.role not in ALL_RECIPIENT_ROLES:
            raise RelationshipNotVerifiedError(
                f"relationship role {snapshot.role!r} is not a recipient role"
            )
        if snapshot.role not in allowed_roles:
            raise NotificationPolicyError(
                f"intent kind {intent_kind} does not accept role {snapshot.role}"
            )
        return snapshot

    async def _require_operator(
        self,
        *,
        actor_person_id: str | None,
        authorization_ref: str | None,
        now: datetime,
    ) -> None:
        """P0-5: operator authority comes from the authoritative
        OperatorAuthorizationPort (internal service role + step-up + audit),
        never from an evidence string.  ``notification_operator`` is NOT a
        canonical capability, so a policy receipt can never authorize an
        operator action; an unconfigured port fails closed."""
        if not actor_person_id or not authorization_ref:
            raise NotificationPolicyError(
                "operator action requires actor_person_id and an operator "
                "authorization reference"
            )
        authorizer = self._operator_authorizer
        if authorizer is None:
            raise ReceiptNotVerifiedError(
                "no operator authorization port configured; operator "
                "actions fail closed (production not wired)"
            )
        authorized = await authorizer.verify_operator(
            authorization_ref,
            actor_person_id=actor_person_id,
            now=now,
        )
        if not authorized:
            raise ReceiptNotVerifiedError(
                f"operator authorization {authorization_ref} not verified"
            )

    async def _authorize_cancel(
        self,
        *,
        recipient: RecipientBinding,
        reason: CancelReason,
        actor_person_id: str | None,
        authorization_ref: str | None,
        now: datetime,
    ) -> None:
        """P0-5 cancellation authority: the recipient themselves, or a
        verified operator; relationship events need the authoritative
        snapshot path."""
        if reason in (
            "relationship_revoked",
            "relationship_expired",
            "relationship_disputed",
        ):
            await self._require_operator(
                actor_person_id=actor_person_id,
                authorization_ref=authorization_ref,
                now=now,
            )
            return
        if reason in ("user_request", "wrong_contact"):
            if actor_person_id == recipient.person_id:
                return
            await self._require_operator(
                actor_person_id=actor_person_id,
                authorization_ref=authorization_ref,
                now=now,
            )
            return
        if reason == "operator_override":
            await self._require_operator(
                actor_person_id=actor_person_id,
                authorization_ref=authorization_ref,
                now=now,
            )
            return
        raise NotificationPolicyError(f"unknown cancel reason: {reason}")

    async def _complete_delivered_atomic(
        self,
        recipient: RecipientBinding,
        attempt: DeliveryAttempt,
        channel_receipt_id: str,
        actor_person_id: str | None,
        timestamp: datetime,
    ) -> RecipientBinding:
        receipt = DeliveryReceipt(
            receipt_id=new_id(),
            intent_id=recipient.intent_id,
            recipient_id=recipient.recipient_id,
            attempt_id=attempt.attempt_id,
            channel=attempt.channel,
            channel_receipt_id=channel_receipt_id,
            delivered_at=timestamp,
        )
        updated = replace_recipient(
            recipient,
            status="delivered",
            delivered_at=timestamp,
            delivered_channel=attempt.channel,
            leased_until=None,
            fencing_token=None,
            last_error_code=None,
            updated_at=timestamp,
        )
        result = await self._store.complete_attempt_atomically(
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.fencing_token,
            recipient=updated,
            finished_attempt=_finished_attempt(attempt, "delivered", timestamp),
            receipt=receipt,
            audit=(
                NotificationAuditEvent(
                    event_id=f"audit:{attempt.attempt_id}:delivered",
                    action="recipient.delivered",
                    actor_person_id=actor_person_id,
                    intent_id=recipient.intent_id,
                    recipient_id=recipient.recipient_id,
                    payload=receipt.to_dict(),
                    created_at=timestamp,
                ),
            ),
            outbox=(
                NotificationOutboxEvent(
                    outbox_id=new_id(),
                    event_id=f"{attempt.attempt_id}:delivered",
                    topic="notification.recipient.delivered",
                    payload=receipt.to_dict(),
                    created_at=timestamp,
                ),
            ),
            now=timestamp,
        )
        if result is None:
            raise NotificationFencingError(
                f"fencing token mismatch for attempt {attempt.attempt_id}; "
                "a newer lease superseded this attempt"
            )
        committed, intent = result
        return committed

    async def _complete_failed_atomic(
        self,
        recipient: RecipientBinding,
        attempt: DeliveryAttempt,
        error_code: str,
        actor_person_id: str | None,
        timestamp: datetime,
    ) -> RecipientBinding:
        classification = classify_failure(error_code)
        if classification == "permanent":
            updated = replace_recipient(
                recipient,
                status="dead_lettered",
                leased_until=None,
                fencing_token=None,
                last_error_code=error_code,
                updated_at=timestamp,
            )
        elif classification == "unknown":
            # Unreviewed failure codes land in the dead letter for humans.
            updated = replace_recipient(
                recipient,
                status="dead_lettered",
                leased_until=None,
                fencing_token=None,
                last_error_code=error_code,
                updated_at=timestamp,
            )
        elif recipient.attempts >= recipient.max_retries:
            updated = replace_recipient(
                recipient,
                status="dead_lettered",
                leased_until=None,
                fencing_token=None,
                last_error_code=error_code,
                updated_at=timestamp,
            )
        else:
            has_fallback = recipient.channel_index + 1 < len(recipient.channels)
            next_attempt_at = (
                timestamp if has_fallback else self._backoff.next_attempt_at(
                    recipient.attempts, timestamp
                )
            )
            updated = replace_recipient(
                recipient,
                status="failed",
                channel_index=recipient.channel_index + 1 if has_fallback else recipient.channel_index,
                next_attempt_at=next_attempt_at,
                leased_until=None,
                fencing_token=None,
                last_error_code=error_code,
                updated_at=timestamp,
            )
        result = await self._store.complete_attempt_atomically(
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.fencing_token,
            recipient=updated,
            finished_attempt=_finished_attempt(attempt, "failed", timestamp, error_code),
            receipt=None,
            audit=(
                NotificationAuditEvent(
                    event_id=f"audit:{attempt.attempt_id}:failed",
                    action=f"recipient.{updated.status}",
                    actor_person_id=actor_person_id,
                    intent_id=recipient.intent_id,
                    recipient_id=recipient.recipient_id,
                    payload={
                        **updated.to_dict(),
                        "error_code": error_code,
                        "classification": classification,
                    },
                    created_at=timestamp,
                ),
            ),
            outbox=(
                NotificationOutboxEvent(
                    outbox_id=new_id(),
                    event_id=f"{attempt.attempt_id}:{updated.status}",
                    topic=f"notification.recipient.{updated.status}",
                    payload={
                        **updated.to_dict(),
                        "error_code": error_code,
                        "classification": classification,
                    },
                    created_at=timestamp,
                ),
            ),
            now=timestamp,
        )
        if result is None:
            raise NotificationFencingError(
                f"fencing token mismatch for attempt {attempt.attempt_id}; "
                "a newer lease superseded this attempt"
            )
        committed, intent = result
        return committed

    @staticmethod
    def _same_intent(
        existing: NotificationIntent,
        existing_recipients: tuple[RecipientBinding, ...],
        intent_kind: IntentKind,
        subject_person_id: str,
        source_event_id: str,
        policy_receipt_id: str,
        template_key: TemplateKey,
        params: dict[str, str],
        reason_code: str,
        script_version: str,
        recipients: Sequence[
            tuple[RecipientSpec, RelationshipSnapshot, tuple[Channel, ...]]
        ],
    ) -> bool:
        if (
            existing.intent_kind != intent_kind
            or existing.subject_person_id != subject_person_id
            or existing.source_event_id != source_event_id
            or existing.policy_receipt_id != policy_receipt_id
            or existing.template_key != template_key
            or _content_params(existing.template_params) != _content_params(params)
            or existing.reason_code != reason_code
            or existing.script_version != script_version
        ):
            return False
        if len(existing_recipients) != len(recipients):
            return False
        for bound, (spec, snapshot, channels) in zip(
            existing_recipients, recipients, strict=True
        ):
            if (
                bound.person_id != snapshot.person_id
                or bound.role != snapshot.role
                or bound.relationship_id != spec.relationship_id
                or bound.relationship_status != snapshot.status
                or (
                    snapshot.valid_from is not None
                    and bound.valid_from != snapshot.valid_from
                )
                or bound.valid_until != snapshot.valid_until
                or bound.channels != channels
            ):
                return False
        return True


def _default_channels(role: RecipientRole) -> tuple[Channel, ...]:
    from services.notification.domain import DEFAULT_CHANNEL_ORDER

    return DEFAULT_CHANNEL_ORDER[role]


def _content_params(params: dict[str, str]) -> dict[str, str]:
    """Template params excluding time-derived fields (occurred_at is
    derived from the event and must not break idempotent replay)."""
    return {key: value for key, value in params.items() if key not in ("occurred_at",)}


def _finished_attempt(
    attempt: DeliveryAttempt,
    status: AttemptStatus,
    timestamp: datetime,
    error_code: str | None = None,
) -> DeliveryAttempt:
    return DeliveryAttempt(
        attempt_id=attempt.attempt_id,
        intent_id=attempt.intent_id,
        recipient_id=attempt.recipient_id,
        attempt_number=attempt.attempt_number,
        channel=attempt.channel,
        logical_delivery_key=attempt.logical_delivery_key,
        status=status,
        fencing_token=attempt.fencing_token,
        leased_until=attempt.leased_until,
        started_at=attempt.started_at,
        finished_at=timestamp,
        error_code=error_code,
    )


def _intent_status(
    intent: NotificationIntent,
    status: IntentStatus,
    *,
    reason: CancelReason | None = None,
    delivered_at: datetime | None = None,
    now: datetime,
) -> NotificationIntent:
    return NotificationIntent(
        intent_id=intent.intent_id,
        idempotency_key=intent.idempotency_key,
        intent_kind=intent.intent_kind,
        subject_person_id=intent.subject_person_id,
        source_event_id=intent.source_event_id,
        policy_receipt_id=intent.policy_receipt_id,
        session_id=intent.session_id,
        epoch=intent.epoch,
        binding_id=intent.binding_id,
        binding_version=intent.binding_version,
        runtime_profile_id=intent.runtime_profile_id,
        actor_person_id=intent.actor_person_id,
        fence_context_hash=intent.fence_context_hash,
        template_key=intent.template_key,
        template_params=dict(intent.template_params),
        reason_code=intent.reason_code,
        script_version=intent.script_version,
        occurred_at=intent.occurred_at,
        valid_until=intent.valid_until,
        status=status,
        created_at=intent.created_at,
        updated_at=now,
        cancelled_reason=reason if status == "cancelled" else None,
        cancelled_at=now if status == "cancelled" and reason is not None else None,
        delivered_at=delivered_at,
    )


__all__ = ["DispatchSummary", "NotificationService"]
