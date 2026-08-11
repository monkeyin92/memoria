"""Shared PolicyReceiptV2 construction helpers for notification tests.

The notification domain consumes the authoritative V2 receipt contract
(services/policy/receipts.py); these helpers keep every test on the same
wire format with parameterized NOTIFY obligations (recipient_role /
intent_kind) so the Policy-crisis coupling checks are exercised for real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.contracts.generated.python.multi_subject_contracts import (
    Capability,
    DataClassification,
    DeviceTrust,
    ObligationParams,
    PolicyEffect,
    PolicyObligation,
    PolicyObligationSpec,
    PolicyReceiptV2,
    Purpose,
    SafetyState,
)
from services.notification.domain import NotificationFence
from services.policy.action_fence import build_default_action_resource_fence


def now() -> datetime:
    return datetime(2026, 8, 9, 10, 0, tzinfo=UTC)


#: Valid canonical 64-hex hashes (full PolicyContext digest / binding
#: canonical hash).  Tests must never compute these from a local fence
#: fingerprint.
CONTEXT_HASH = "a" * 64
BINDING_CANONICAL_HASH = "b" * 64


def _rfc3339(value: datetime) -> str:
    """Canonical wire timestamp (the generated V2 model requires RFC3339
    strings with T separator and Z/offset)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_notify_obligations(
    *,
    recipient_role: str = "guardian",
    intent_kind: str = "crisis_safety",
) -> tuple[PolicyObligationSpec, ...]:
    """Canonical NOTIFY obligation parameterized for one role/kind plus the
    minimal-content obligation (CONTRACT §5.3)."""
    return notify_obligations_for_roles(
        recipient_roles=(recipient_role,),
        intent_kind=intent_kind,
    )


def notify_obligations_for_roles(
    *,
    recipient_roles: tuple[str, ...],
    intent_kind: str = "crisis_safety",
) -> tuple[PolicyObligationSpec, ...]:
    """Canonical NOTIFY obligations for the distinct recipient roles in one
    receipt.

    The receipt contract requires unique obligations; a single legal NOTIFY
    obligation can still authorize multiple relationship snapshots when they
    collapse to the same recipient role, so we dedupe by role here rather than
    emitting duplicate specs for repeated snapshots.
    """
    unique_roles = tuple(dict.fromkeys(recipient_roles))
    return (
        *(
            PolicyObligationSpec(
                code=PolicyObligation.POLICY_OBLIGATION_NOTIFY_EMERGENCY_CONTACT,
                params=ObligationParams(
                    max_session_seconds=None,
                    retention_ttl_seconds=None,
                    quiet_hours=None,
                    extras=(
                        ("recipient_role", recipient_role),
                        ("intent_kind", intent_kind),
                    ),
                ),
            )
            for recipient_role in unique_roles
        ),
        PolicyObligationSpec(
            code=PolicyObligation.POLICY_OBLIGATION_MINIMAL_NOTIFICATION_CONTENT,
            params=ObligationParams(
                max_session_seconds=None,
                retention_ttl_seconds=None,
                quiet_hours=None,
                extras=(),
            ),
        ),
    )


def make_notification_receipt(
    *,
    receipt_id: str = "policy-receipt-1",
    actor_person_id: str = "actor-1",
    subject_person_id: str = "minor-1",
    fence: NotificationFence | None = None,
    capability: str = "crisis_notification",
    purpose: str = "crisis_response",
    effect: str = "allow",
    obligations: tuple[PolicyObligationSpec, ...] | None = None,
    relationship_snapshot_ids: tuple[str, ...] = ("rel-snap-1",),
    relationship_snapshot_revisions: tuple[int, ...] | None = None,
    consent_snapshot_ids: tuple[str, ...] = (),
    consent_snapshot_revisions: tuple[int, ...] = (),
    context_hash: str | None = None,
    binding_canonical_hash: str | None = BINDING_CANONICAL_HASH,
    expires_at: datetime | None = None,
    exact_fence: bool = True,
    created_at: datetime | None = None,
    now: datetime | None = None,
) -> PolicyReceiptV2:
    """Full valid V2 receipt defaulting every identity field to the standard
    test fence (session-1 / binding-1 / profile-1)."""
    effective_fence = fence or NotificationFence(
        valid_until=datetime.now(UTC) + timedelta(hours=2),
        device_id="device-1",
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_id="profile-1",
        actor_person_id=actor_person_id,
        subject_person_id=subject_person_id,
    )
    created = created_at
    if created is None:
        # Anchor the receipt just BEFORE the service/test clock so a
        # microsecond-skewed ``now`` can never fall outside the decision
        # window; explicit expiry scenarios pass expires_at instead.
        reference = now or datetime.now(UTC)
        created = reference - timedelta(minutes=1)
        if expires_at is not None and expires_at <= created:
            created = expires_at - timedelta(hours=1)
    expires = expires_at or (created + timedelta(hours=2))
    evidence_revisions = (
        relationship_snapshot_revisions
        if relationship_snapshot_revisions is not None
        else tuple(1 for _ in relationship_snapshot_ids)
    )
    capability_enum = Capability(capability)
    purpose_enum = Purpose(purpose)
    effect_enum = PolicyEffect(effect)
    action_resource_fence = build_default_action_resource_fence(
        capability=capability_enum.value,
        purpose=purpose_enum.value,
        actor_id=actor_person_id,
        subject_id=subject_person_id,
        resource_owner_id=subject_person_id,
        device_id=effective_fence.device_id,
        binding_id=effective_fence.binding_id,
        binding_version=effective_fence.binding_version,
        session_id=effective_fence.session_id,
        session_epoch=effective_fence.epoch,
        generation_id=0,
        turn_id=0,
        tool_epoch=0,
        evaluated_at=created,
    )
    canonical_obligations = (
        obligations if obligations is not None else default_notify_obligations()
    )
    return PolicyReceiptV2(
        receipt_id=receipt_id,
        actor_id=actor_person_id,
        subject_id=subject_person_id,
        resource_owner_id=subject_person_id,
        device_id=effective_fence.device_id,
        capability=capability_enum,
        purpose=purpose_enum,
        effect=effect_enum,
        reason_code="crisis_notification_authorized",
        obligations=canonical_obligations,
        policy_version="2026-08-09.1",
        context_hash=context_hash or CONTEXT_HASH,
        action_resource_fence=action_resource_fence,
        action_fence_hash=action_resource_fence.canonical_hash,
        consent_snapshot_ids=consent_snapshot_ids,
        consent_snapshot_revisions=consent_snapshot_revisions,
        relationship_snapshot_ids=relationship_snapshot_ids,
        relationship_snapshot_revisions=evidence_revisions,
        binding_id=effective_fence.binding_id,
        binding_version=effective_fence.binding_version,
        binding_canonical_hash=binding_canonical_hash,
        session_id=effective_fence.session_id,
        session_epoch=effective_fence.epoch,
        runtime_profile_id=effective_fence.runtime_profile_id,
        subject_revision=effective_fence.subject_revision,
        device_trust=DeviceTrust.DEVICE_TRUST_TRUSTED,
        data_classification=DataClassification.DATA_CLASSIFICATION_SAFETY_MINIMUM,
        safety_state=SafetyState.SAFETY_STATE_SELF_CRISIS,
        jurisdiction="CN",
        created_at=_rfc3339(created),
        expires_at=_rfc3339(expires),
        exact_fence=exact_fence,
    )
