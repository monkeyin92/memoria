"""Shared PolicyReceiptV2 construction helpers for memory_scope tests.

The memory domain consumes the authoritative V2 receipt contract
(services/policy/receipts.py).  The full-context ``context_hash`` is a
64-hex canonical hash of the complete PolicyContext and is NEVER derived
from the local fence fingerprint here; the in-memory test verifier
explicitly asserts the fence/evidence verification outcome instead.
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
from services.memory_scope.domain import WriteFence
from services.policy.action_fence import build_default_action_resource_fence


def now() -> datetime:
    return datetime(2026, 8, 9, 10, 0, tzinfo=UTC)


#: Valid canonical 64-hex context hash (full PolicyContext digest).  Tests
#: must never compute it from a local fence fingerprint.
CONTEXT_HASH = "a" * 64
BINDING_CANONICAL_HASH = "b" * 64


def _obligation_to_spec(obligation: object) -> PolicyObligationSpec:
    """Canonical-only obligation conversion (the generated
    ``PolicyObligationSpec`` / ``PolicyObligation`` are the ONLY wire
    types - a legacy dataclass obligation is rejected, never silently
    converted)."""
    if isinstance(obligation, PolicyObligationSpec):
        return obligation
    if isinstance(obligation, PolicyObligation):
        return PolicyObligationSpec(
            code=obligation,
            params=ObligationParams(
                max_session_seconds=None,
                retention_ttl_seconds=None,
                quiet_hours=None,
                extras=(),
            ),
        )
    if isinstance(obligation, str):
        return PolicyObligationSpec(
            code=PolicyObligation(obligation),
            params=ObligationParams(
                max_session_seconds=None,
                retention_ttl_seconds=None,
                quiet_hours=None,
                extras=(),
            ),
        )
    raise TypeError(
        f"unsupported obligation wire type: {type(obligation).__name__}; "
        "only generated PolicyObligation/PolicyObligationSpec are accepted"
    )


def _rfc3339(value: datetime) -> str:
    """Canonical wire timestamp (the generated V2 model requires RFC3339
    strings with T separator and Z/offset)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_memory_receipt(
    *,
    receipt_id: str = "receipt-1",
    actor_subject_id: str = "person-a",
    subject_id: str | None = None,
    resource_owner_id: str | None = None,
    fence: WriteFence | None = None,
    capability: str = "memory_capture",
    purpose: str = "memory_capture",
    effect: str = "allow_with_obligations",
    obligations: tuple[object, ...] = (),
    consent_snapshot_ids: tuple[str, ...] = ("consent-1",),
    consent_snapshot_revisions: tuple[int, ...] = (1,),
    relationship_snapshot_ids: tuple[str, ...] = (),
    relationship_snapshot_revisions: tuple[int, ...] | None = None,
    context_hash: str = CONTEXT_HASH,
    binding_canonical_hash: str | None = BINDING_CANONICAL_HASH,
    expires_at: datetime | None = None,
    exact_fence: bool = True,
    binding_version: int | None = None,
    created_at: datetime | None = None,
    now: datetime | None = None,
    action_resource_fence: object | None = None,
) -> PolicyReceiptV2:
    """Full valid V2 receipt defaulting identity fields to the standard
    test fence (session-1 / binding-1 / profile-1 / person-a)."""
    effective_fence = fence or WriteFence(
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_role="primary_subject",
        runtime_profile_id="profile-1",
        actor_subject_id=actor_subject_id,
        active_subject_id=subject_id or actor_subject_id,
        binding_version=1,
        device_id="device-1",
    )
    if (
        not isinstance(effective_fence.epoch, int)
        or isinstance(effective_fence.epoch, bool)
        or effective_fence.epoch < 1
    ):
        # Field-specific fail-closed error BEFORE the canonical action
        # fence derivation (which would otherwise report action_revision
        # for an epoch=0 fence): the test asserts the field name.
        raise ValueError("session_epoch must be an integer >= 1")
    # MemoryScopeService runs on the real clock (no injectable ``now``), so
    # the receipt must default to the real current time; explicit tests
    # override expires_at for expiry scenarios.
    created = created_at
    if created is None:
        # MemoryScopeService runs on the real clock (no injectable ``now``);
        # anchor the receipt just BEFORE that clock so the decision window
        # always covers the write moment.  Explicit expiry tests pass
        # expires_at instead.
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
    subject_id_resolved = subject_id or effective_fence.active_subject_id
    resource_owner_resolved = resource_owner_id or subject_id_resolved
    if action_resource_fence is None:
        # memory_capture / guardian_summary receipts: derive the bounded
        # action identity through the AUTHORITATIVE builder (hashes are
        # recomputed by the strict verifier - never hand-fabricated).
        action_resource_fence = build_default_action_resource_fence(
            capability=capability_enum.value,
            purpose=purpose_enum.value,
            actor_id=actor_subject_id,
            subject_id=subject_id_resolved,
            resource_owner_id=resource_owner_resolved,
            device_id=effective_fence.device_id,
            binding_id=effective_fence.binding_id,
            binding_version=(
                binding_version
                if binding_version is not None
                else effective_fence.binding_version
            ),
            session_id=effective_fence.session_id,
            session_epoch=effective_fence.epoch,
            generation_id=0,
            turn_id=0,
            tool_epoch=0,
            evaluated_at=created,
        )
    canonical_obligations = tuple(
        _obligation_to_spec(obligation) for obligation in obligations
    )
    return PolicyReceiptV2(
        receipt_id=receipt_id,
        actor_id=actor_subject_id,
        subject_id=subject_id_resolved,
        resource_owner_id=resource_owner_resolved,
        device_id=effective_fence.device_id,
        capability=capability_enum,
        purpose=purpose_enum,
        effect=effect_enum,
        reason_code="subject_authorized",
        obligations=canonical_obligations,
        policy_version="multi-subject-v2",
        context_hash=context_hash,
        action_resource_fence=action_resource_fence,
        action_fence_hash=action_resource_fence.canonical_hash,
        consent_snapshot_ids=consent_snapshot_ids,
        consent_snapshot_revisions=consent_snapshot_revisions,
        relationship_snapshot_ids=relationship_snapshot_ids,
        relationship_snapshot_revisions=evidence_revisions,
        binding_id=effective_fence.binding_id,
        binding_version=(
            binding_version
            if binding_version is not None
            else effective_fence.binding_version
        ),
        binding_canonical_hash=binding_canonical_hash,
        session_id=effective_fence.session_id,
        session_epoch=effective_fence.epoch,
        runtime_profile_id=effective_fence.runtime_profile_id,
        subject_revision=effective_fence.subject_revision,
        device_trust=DeviceTrust.DEVICE_TRUST_TRUSTED,
        data_classification=DataClassification.DATA_CLASSIFICATION_PRIVATE,
        safety_state=SafetyState.SAFETY_STATE_NORMAL,
        jurisdiction="CN",
        created_at=_rfc3339(created),
        expires_at=_rfc3339(expires),
        exact_fence=exact_fence,
    )
