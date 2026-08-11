"""Canonical action/resource fence construction and verification.

The generated ``PolicyActionResourceFence`` and
``PolicyApprovalSnapshotFence`` are the only action-fence output types.  This
module owns their deterministic hashes; callers supply structural fields but
never a self-attesting ``action_evidence_hash`` or ``canonical_hash``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from packages.contracts.generated.python.multi_subject_contracts import (
    CapabilityValue,
    PolicyActionResourceFence,
    PolicyApprovalSnapshotFence,
    PurposeValue,
    SharedMemoryConfirmationDecisionValue,
)

DEFAULT_ACTION_FENCE_TTL = timedelta(minutes=5)


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("action fence timestamps must be aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_authority_hash(
    records: tuple[tuple[str, int, str], ...],
) -> str:
    """Hash capture evidence identity/revision/hash in canonical id order."""
    normalized = sorted(records, key=lambda item: item[0])
    if len({item[0] for item in normalized}) != len(normalized):
        raise ValueError("capture evidence ids must be unique")
    return _digest(
        [
            {"evidence_id": evidence_id, "revision": revision, "canonical_hash": digest}
            for evidence_id, revision, digest in normalized
        ]
    )


def _action_evidence_payload(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"action_fence_schema", "action_evidence_hash", "canonical_hash"}
    }


def _canonical_payload(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "canonical_hash"}


def build_approval_snapshot_fence(
    *,
    subject_id: str,
    snapshot_id: str,
    revision: int,
    canonical_hash: str,
) -> PolicyApprovalSnapshotFence:
    """Construct the generated immutable approval reference."""
    return PolicyApprovalSnapshotFence(
        subject_id=subject_id,
        snapshot_id=snapshot_id,
        revision=revision,
        canonical_hash=canonical_hash,
    )


def build_action_resource_fence(
    *,
    capability: CapabilityValue,
    purpose: PurposeValue,
    action_resource_id: str,
    action_revision: int,
    generation_id: int,
    turn_id: int,
    tool_epoch: int,
    issued_at: datetime,
    valid_until: datetime,
    family_space_id: str | None = None,
    family_owner_subject_id: str | None = None,
    proposal_id: str | None = None,
    proposal_revision: int | None = None,
    voter_subject_id: str | None = None,
    approval_decision: SharedMemoryConfirmationDecisionValue | None = None,
    required_approval_subject_ids: tuple[str, ...] = (),
    approval_snapshots: tuple[PolicyApprovalSnapshotFence, ...] = (),
    capture_evidence_ids: tuple[str, ...] = (),
    capture_evidence_records: tuple[tuple[str, int, str], ...] = (),
    consent_snapshot_id: str | None = None,
    consent_snapshot_revision: int | None = None,
    consent_snapshot_hash: str | None = None,
    membership_snapshot_id: str | None = None,
    membership_snapshot_revision: int | None = None,
    membership_snapshot_hash: str | None = None,
) -> PolicyActionResourceFence:
    """Build a generated fence and derive both hashes from its structure."""
    sorted_approvals = tuple(sorted(approval_snapshots, key=lambda item: item.subject_id))
    if len({item.subject_id for item in sorted_approvals}) != len(sorted_approvals):
        raise ValueError("approval snapshots must be unique by subject_id")
    sorted_required = tuple(sorted(required_approval_subject_ids))
    if capture_evidence_ids and capture_evidence_records:
        raise ValueError("provide capture evidence ids or records, not both")
    sorted_capture_records = tuple(
        sorted(capture_evidence_records, key=lambda item: item[0])
    )
    sorted_capture_ids = tuple(
        item[0] for item in sorted_capture_records
    ) or tuple(sorted(capture_evidence_ids))
    capture_evidence_hash = (
        capture_authority_hash(sorted_capture_records)
        if sorted_capture_records
        else (_digest(list(sorted_capture_ids)) if sorted_capture_ids else None)
    )
    payload: dict[str, object] = {
        "action_fence_schema": "policy-action-resource-fence-v1",
        "capability": capability,
        "purpose": purpose,
        "action_resource_id": action_resource_id,
        "action_revision": action_revision,
        "family_space_id": family_space_id,
        "family_owner_subject_id": family_owner_subject_id,
        "proposal_id": proposal_id,
        "proposal_revision": proposal_revision,
        "voter_subject_id": voter_subject_id,
        "approval_decision": approval_decision,
        "required_approval_subject_ids": list(sorted_required),
        "approval_snapshots": [
            item.model_dump(mode="json") for item in sorted_approvals
        ],
        "capture_evidence_ids": list(sorted_capture_ids),
        "capture_evidence_hash": capture_evidence_hash,
        "consent_snapshot_id": consent_snapshot_id,
        "consent_snapshot_revision": consent_snapshot_revision,
        "consent_snapshot_hash": consent_snapshot_hash,
        "membership_snapshot_id": membership_snapshot_id,
        "membership_snapshot_revision": membership_snapshot_revision,
        "membership_snapshot_hash": membership_snapshot_hash,
        "generation_id": generation_id,
        "turn_id": turn_id,
        "tool_epoch": tool_epoch,
        "issued_at": _rfc3339(issued_at),
        "valid_until": _rfc3339(valid_until),
    }
    payload["action_evidence_hash"] = _digest(_action_evidence_payload(payload))
    payload["canonical_hash"] = _digest(_canonical_payload(payload))
    return PolicyActionResourceFence.model_validate(payload)


def verify_action_resource_fence(fence: PolicyActionResourceFence) -> bool:
    """Strictly recompute both generated fence hashes from structural fields."""
    if not isinstance(fence, PolicyActionResourceFence):
        return False
    payload = fence.model_dump(mode="json")
    expected_evidence = _digest(_action_evidence_payload(payload))
    if fence.action_evidence_hash != expected_evidence:
        return False
    return fence.canonical_hash == _digest(_canonical_payload(payload))


def build_default_action_resource_fence(
    *,
    capability: CapabilityValue,
    purpose: PurposeValue,
    actor_id: str,
    subject_id: str | None,
    resource_owner_id: str | None,
    device_id: str,
    binding_id: str,
    binding_version: int,
    session_id: str,
    session_epoch: int,
    generation_id: int,
    turn_id: int,
    tool_epoch: int,
    evaluated_at: datetime,
    consent_snapshot_id: str | None = None,
    consent_snapshot_revision: int | None = None,
    consent_snapshot_hash: str | None = None,
) -> PolicyActionResourceFence:
    """Derive a bounded action identity for existing non-resource call paths."""
    identity = _digest(
        (
            actor_id,
            subject_id,
            resource_owner_id,
            device_id,
            binding_id,
            binding_version,
            session_id,
            session_epoch,
            capability,
            purpose,
        )
    )
    return build_action_resource_fence(
        capability=capability,
        purpose=purpose,
        action_resource_id=f"action-{identity}",
        action_revision=session_epoch,
        generation_id=generation_id,
        turn_id=turn_id,
        tool_epoch=tool_epoch,
        issued_at=evaluated_at,
        valid_until=evaluated_at + DEFAULT_ACTION_FENCE_TTL,
        consent_snapshot_id=consent_snapshot_id,
        consent_snapshot_revision=consent_snapshot_revision,
        consent_snapshot_hash=consent_snapshot_hash,
    )
