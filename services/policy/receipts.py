"""Generated PolicyReceiptV2 authority and exact-fence verification."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from packages.contracts.generated.python.multi_subject_contracts import PolicyReceiptV2

from services.policy.action_fence import verify_action_resource_fence
from services.policy.context import (
    PolicyContext,
    context_hash,
    effective_action_resource_fence,
)

__all__ = [
    "PolicyReceiptConflictError",
    "PolicyReceiptV2",
    "PolicyReceiptWriterV2",
    "exact_evidence_fence_valid",
    "receipt_authorizes_relationship",
    "receipt_fence_valid",
]


class PolicyReceiptConflictError(ValueError):
    """A receipt_id already exists with different immutable content."""


class PolicyReceiptWriterV2(Protocol):
    """Compatibility-only synchronous writer for generated V2 receipts."""

    def write(self, receipt: PolicyReceiptV2) -> None: ...


def receipt_fence_valid(
    receipt: PolicyReceiptV2,
    *,
    context: PolicyContext,
    now: datetime,
) -> bool:
    """Validate literal decision identity, canonical context and time window."""
    literal_pairs = (
        (receipt.actor_id, context.actor_id),
        (receipt.subject_id, context.subject_id),
        (receipt.resource_owner_id, context.resource_owner_id),
        (receipt.device_id, context.device_id),
        (receipt.capability.value, context.capability),
        (receipt.purpose.value, context.purpose),
        (receipt.binding_id, context.binding_id),
        (receipt.binding_version, context.binding_version),
        (receipt.session_id, context.session_id),
        (receipt.session_epoch, context.session_epoch),
        (receipt.runtime_profile_id, context.runtime_profile_id),
        (receipt.subject_revision, context.subject_revision),
    )
    if any(left != right for left, right in literal_pairs):
        return False
    if not receipt.created_at <= now < receipt.expires_at:
        return False
    current_fence = effective_action_resource_fence(context)
    if (
        not verify_action_resource_fence(receipt.action_resource_fence)
        or receipt.action_fence_hash != receipt.action_resource_fence.canonical_hash
        or receipt.action_resource_fence != current_fence
    ):
        return False
    return receipt.context_hash == context_hash(context)


def exact_evidence_fence_valid(
    receipt: PolicyReceiptV2,
    *,
    context: PolicyContext,
    now: datetime,
) -> bool:
    """Validate exact current consent, relationship, binding and action evidence."""
    if not receipt.exact_fence or not receipt_fence_valid(
        receipt, context=context, now=now
    ):
        return False
    if context.device_trust not in {"trusted", "verified"}:
        return False
    current_consent_snapshots = {
        (evidence.snapshot_id, evidence.revision): evidence
        for evidence in context.consent_snapshot_evidence
    }
    for identity in zip(
        receipt.consent_snapshot_ids,
        receipt.consent_snapshot_revisions,
        strict=True,
    ):
        snapshot = current_consent_snapshots.get(identity)
        if snapshot is None or not snapshot.is_current_at(now):
            return False
        if not any(
            consent.is_effective_at(now) and snapshot.contains(consent)
            for consent in context.consent_evidence
        ):
            return False
    current_relationships = {
        (evidence.snapshot_id, evidence.revision): evidence
        for evidence in context.relationship_evidence
    }
    for identity in zip(
        receipt.relationship_snapshot_ids,
        receipt.relationship_snapshot_revisions,
        strict=True,
    ):
        relationship = current_relationships.get(identity)
        if relationship is None or not relationship.is_active_at(now):
            return False
    binding = context.binding_evidence
    return bool(
        binding is not None
        and binding.binding_id == receipt.binding_id
        and binding.version == receipt.binding_version
        and binding.is_active_at(now)
        and receipt.binding_canonical_hash is not None
        and binding.canonical_hash == receipt.binding_canonical_hash
    )


def receipt_authorizes_relationship(
    receipt: PolicyReceiptV2, relationship_snapshot_id: str
) -> bool:
    """Notification adapter proof that a relationship snapshot was receipted."""
    return (
        receipt.exact_fence
        and isinstance(relationship_snapshot_id, str)
        and relationship_snapshot_id in receipt.relationship_snapshot_ids
    )
