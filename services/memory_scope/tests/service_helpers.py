"""Shared fixtures and builders for the split MemoryScopeService tests.

``test_service_capture_reads.py``, ``test_service_lifecycle_authority.py`` and
``test_service_promotion.py`` split the lifecycle and authorization suite by
behavior; each imports its common subject, policy, fence and receipt builders
plus the ``service`` fixture from here.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    BindingRole,
    PolicyActionResourceFence,
    PolicyEffect,
    PolicyObligation,
)
from services.memory_scope.domain import (
    ConsentSnapshotInput,
    CoSubjectContext,
    MemoryScope,
    MemoryWriteDraft,
    PolicyDecisionInput,
    ResolutionContext,
    SharedMemoryProposal,
    SubjectContext,
    WriteFence,
)
from services.memory_scope.in_memory_store import InMemoryMemoryStore
from services.memory_scope.repository import (
    ApprovalEvidence,
    InMemoryApprovalAuthorizationPort,
    InMemoryConsentSnapshotVerifier,
    InMemoryFamilyMembershipVerifier,
    InMemoryPolicyReceiptVerifier,
    InMemoryPromotionAuthorizationPort,
    InMemoryPromotionCommitAuthority,
    InMemoryProposalAuthorizationPort,
    InMemoryRelationshipGrantResolver,
    PromotionAuthorization,
)
from services.memory_scope.service import MemoryScopeService
from services.memory_scope.tests.receipt_helpers import make_memory_receipt


def _subject(
    subject_id: str,
    *,
    category: str = "adult",
    speaker: str = "confirmed",
    registered: bool = True,
    family: str | None = None,
    guardian_active: bool = False,
) -> SubjectContext:
    return SubjectContext(
        active_subject_id=subject_id,
        subject_category=category,  # type: ignore[arg-type]
        speaker_state=speaker,  # type: ignore[arg-type]
        registered=registered,
        family_space_id=family,
        guardian_relationship_active=guardian_active,
    )


def _policy(
    *,
    effect: PolicyEffect = PolicyEffect.POLICY_EFFECT_ALLOW_WITH_OBLIGATIONS,
    obligations: tuple[PolicyObligation, ...] = (),
    receipt: str | None = "receipt-1",
) -> PolicyDecisionInput:
    return PolicyDecisionInput(
        effect=effect,
        receipt_id=receipt,
        obligations=obligations,
    )


_FENCE = WriteFence(
    session_id="session-1",
    epoch=1,
    binding_id="binding-1",
    binding_role=BindingRole.BINDING_ROLE_PRIMARY_SUBJECT.value,
    runtime_profile_id="profile-1",
    actor_subject_id="person-a",
    active_subject_id="person-a",
    binding_version=1,
    device_id="device-1",
)

_NO_FENCE: object = object()


def _fence(**changes: object) -> WriteFence:
    return replace(_FENCE, **changes)  # type: ignore[arg-type]


def _consent(*covers: str, granted: bool = True) -> ConsentSnapshotInput:
    return ConsentSnapshotInput(
        snapshot_id="consent-1" if granted else None,
        granted=granted,
        covers_subjects=covers,
    )


def _context(
    subject: SubjectContext,
    *,
    requested: MemoryScope,
    policy: PolicyDecisionInput | None = None,
    consent: ConsentSnapshotInput | None = None,
    co_subjects: CoSubjectContext | None = None,
    fence: WriteFence | object = _NO_FENCE,
) -> ResolutionContext:
    effective_fence = fence if fence is not _NO_FENCE else _FENCE
    policy = policy if policy is not None else _policy()
    return ResolutionContext(
        subject=subject,
        policy=policy,
        consent=consent or _consent(subject.active_subject_id or "x"),
        co_subjects=co_subjects or CoSubjectContext(),
        requested_scope=requested,
        fence=effective_fence,  # type: ignore[arg-type]
    )


def _draft(content: str = "今天一起去了公园") -> MemoryWriteDraft:
    return MemoryWriteDraft(
        content=content,
        source_evidence_ids=("evidence-1", "evidence-2"),
    )


async def _propose(service: MemoryScopeService, **kwargs: object) -> SharedMemoryProposal:
    fence = _fence(family_space_id=kwargs.get("family_space_id"))
    family = kwargs.get("family_space_id")
    assert family is not None
    # Three authority actions: the proposal stage is authorized by the
    # proposal port (family_shared_memory_proposal); every confirm vote
    # later carries its OWN fresh approval evidence and the finalizer a
    # DISTINCT fresh promotion authorization (registered by
    # _register_family_approvals below).
    proposal_authorizer = service.proposal_authorizer
    assert proposal_authorizer is not None
    proposal_authorizer.register("receipt-1", str(family))
    await _register_family_proposal_receipt(service, fence, str(family))
    await _register_membership(
        service,
        str(family),
        (kwargs.get("actor_subject_id", "person-a"), *kwargs.get("co_subject_ids", ())),  # type: ignore[arg-type]
    )
    await _register_consent(
        service,
        (kwargs.get("actor_subject_id", "person-a"), *kwargs.get("co_subject_ids", ())),  # type: ignore[arg-type]
    )
    proposal = await service.propose_shared(
        fence=fence,
        **kwargs,  # type: ignore[arg-type]
    )
    await _register_family_approvals(service, proposal)
    return proposal


async def _register_family_proposal_receipt(
    service: MemoryScopeService,
    fence: WriteFence,
    family: str,
    *,
    receipt_id: str = "receipt-1",
) -> None:
    """Register a family_shared_memory_proposal receipt (the dedicated
    proposal capability; a memory_capture receipt can never authorize a
    family proposal)."""
    verifier = service.receipt_verifier
    assert verifier is not None
    proposal_receipt = make_memory_receipt(
        receipt_id=receipt_id,
        fence=fence,
        actor_subject_id=fence.actor_subject_id,
        subject_id=fence.active_subject_id,
        resource_owner_id=family,
        consent_snapshot_ids=("consent-1",),
    )
    _retag_receipt(
        proposal_receipt,
        capability="family_shared_memory_proposal",
        purpose="family_shared_memory_proposal",
    )
    await verifier.register(proposal_receipt)


def _retag_receipt(receipt, *, capability: str, purpose: str) -> None:
    """Test-only capability/purpose retag: the generated contract does not
    carry the family capabilities yet, so PolicyReceiptV2.__post_init__
    rejects them; the test verifier explicitly asserts the retagged values
    (test-is-the-verifier)."""
    object.__setattr__(receipt, "capability", capability)
    object.__setattr__(receipt, "purpose", purpose)


def _make_promotion_action_fence(
    proposal: SharedMemoryProposal,
    *,
    approval_snapshots: dict[str, tuple[str, int, str]],
    issued_at: datetime,
    valid_until: datetime,
    **overrides: object,
) -> PolicyActionResourceFence:
    """Build the CANONICAL generated ``PolicyActionResourceFence`` for a
    fresh promotion, aligned with the proposal's AUTHORITATIVE persisted
    state.  Both fence hashes are derived by the AUTHORITATIVE builder
    (``services.policy.action_fence.build_action_resource_fence`` - never
    hand-fabricated), so ``verify_action_resource_fence`` accepts the
    result.  ``overrides`` lets adversarial tests drift one structural
    field at a time (wrong proposal id / revision, wrong consent snapshot
    id with the same revision, wrong membership hash, wrong approval
    snapshot set / order, wrong generation/turn/tool epoch, expired
    window) and prove the commit fails closed."""
    from services.policy.action_fence import (
        build_action_resource_fence,
        build_approval_snapshot_fence,
    )

    required_subjects = tuple(
        dict.fromkeys((proposal.proposer_subject_id, *proposal.co_subject_ids))
    )
    values: dict[str, object] = dict(
        capability="family_shared_memory_promotion",
        purpose="family_shared_memory_promotion",
        action_resource_id=proposal.proposal_id,
        action_revision=proposal.proposal_revision,
        family_space_id=proposal.family_space_id,
        family_owner_subject_id=proposal.proposer_subject_id,
        proposal_id=proposal.proposal_id,
        proposal_revision=proposal.proposal_revision,
        generation_id=proposal.generation,
        turn_id=proposal.turn_id or 0,
        tool_epoch=proposal.tool_epoch,
        issued_at=issued_at,
        valid_until=valid_until,
        required_approval_subject_ids=required_subjects,
        approval_snapshots=tuple(
            build_approval_snapshot_fence(
                subject_id=subject,
                snapshot_id=snapshot[0],
                revision=snapshot[1],
                canonical_hash=snapshot[2],
            )
            for subject, snapshot in sorted(approval_snapshots.items())
        ),
        consent_snapshot_id=proposal.consent_snapshot_id,
        consent_snapshot_revision=proposal.consent_snapshot_revision or 1,
        consent_snapshot_hash=proposal.consent_snapshot_hash or "b" * 64,
        membership_snapshot_id=proposal.membership_snapshot_id,
        membership_snapshot_revision=proposal.membership_snapshot_revision or 1,
        membership_snapshot_hash=proposal.membership_snapshot_hash or "a" * 64,
    )
    values.update(overrides)
    return build_action_resource_fence(**values)  # type: ignore[arg-type]


async def _register_family_approvals(
    service: MemoryScopeService,
    proposal: SharedMemoryProposal,
) -> None:
    """Three authority actions registration (test-is-the-verifier): every
    confirmable subject gets its OWN fresh family_shared_memory_approval
    evidence, and the proposal gets one DISTINCT fresh
    family_shared_memory_promotion authorization bound to the full approval
    set AND the current consent/membership revision tokens (the commit
    authority CAS-matches those tokens - a missing token can never bypass
    the CAS)."""
    verifier = service.receipt_verifier
    approval_authorizer = service.approval_authorizer
    promotion_authorizer = service.promotion_authorizer
    assert verifier is not None
    assert approval_authorizer is not None
    assert promotion_authorizer is not None
    subjects = (proposal.proposer_subject_id, *proposal.co_subject_ids)
    approval_snapshots: dict[str, tuple[str, int, str]] = {}
    for subject in subjects:
        approval_fence = _fence(
            actor_subject_id=subject,
            active_subject_id=proposal.proposer_subject_id,
            family_space_id=proposal.family_space_id,
            session_id="approval-s1",
        )
        approval_snapshot_hash = hashlib.sha256(
            f"approval-snap:{proposal.proposal_id}:{subject}".encode()
        ).hexdigest()
        approval_snapshots[subject] = (
            f"approval-snap-{subject}",
            1,
            approval_snapshot_hash,
        )
        approval_receipt = make_memory_receipt(
            receipt_id=f"approval-{subject}",
            fence=approval_fence,
            actor_subject_id=subject,
            subject_id=proposal.proposer_subject_id,
            resource_owner_id=proposal.family_space_id,
            consent_snapshot_ids=("consent-1",),
        )
        _retag_receipt(
            approval_receipt,
            capability="family_shared_memory_approval",
            purpose="family_shared_memory_approval",
        )
        await verifier.register(approval_receipt)
        approval_authorizer.register(
            proposal.proposal_id,
            subject,
            ApprovalEvidence(
                receipt_id=approval_receipt.receipt_id,
                fence=approval_fence,
                approval_snapshot_id=approval_snapshots[subject][0],
                approval_snapshot_revision=approval_snapshots[subject][1],
                approval_snapshot_hash=approval_snapshots[subject][2],
            ),
        )
    promotion_fence = _fence(
        actor_subject_id=proposal.proposer_subject_id,
        active_subject_id=proposal.proposer_subject_id,
        family_space_id=proposal.family_space_id,
        session_id="promotion-s1",
    )
    promotion_receipt = make_memory_receipt(
        receipt_id="promotion-1",
        fence=promotion_fence,
        actor_subject_id=proposal.proposer_subject_id,
        subject_id=proposal.proposer_subject_id,
        resource_owner_id=proposal.family_space_id,
        consent_snapshot_ids=("consent-1",),
        obligations=(
            PolicyObligation.POLICY_OBLIGATION_REQUIRE_SUBJECT_APPROVAL,
            PolicyObligation.POLICY_OBLIGATION_WRITE_POLICY_RECEIPT,
        ),
    )
    _retag_receipt(
        promotion_receipt,
        capability="family_shared_memory_promotion",
        purpose="family_shared_memory_promotion",
    )
    await verifier.register(promotion_receipt)
    consent_verifier = service.consent_verifier
    membership_verifier = service.family_membership_verifier
    if consent_verifier is None or membership_verifier is None:
        raise AssertionError("family approvals need verifiers")
    consent_revision = consent_verifier.current_revision("consent-1")
    membership_revision = membership_verifier.current_revision(
        proposal.family_space_id, proposal.binding_version
    )
    if consent_revision is None or membership_revision is None:
        raise AssertionError(
            "consent/membership must be registered before the promotion "
            "authorization (revision tokens are required positive ints)"
        )
    now = datetime.now(UTC)
    action_fence = _make_promotion_action_fence(
        proposal,
        approval_snapshots=approval_snapshots,
        issued_at=now - timedelta(seconds=1),
        valid_until=now + timedelta(minutes=10),
    )
    await promotion_authorizer.register(
        PromotionAuthorization(
            receipt_id=promotion_receipt.receipt_id,
            fence=promotion_fence,
            action_resource_fence=action_fence,
            proposal_id=proposal.proposal_id,
            family_space_id=proposal.family_space_id,
            required_subject_ids=tuple(subjects),
            approval_revisions=tuple(
                sorted(
                    (
                        subject,
                        approval_snapshots[subject][0],
                        approval_snapshots[subject][1],
                        approval_snapshots[subject][2],
                    )
                    for subject in subjects
                )
            ),
            consent_snapshot_id=proposal.consent_snapshot_id,
            membership_snapshot_id=proposal.membership_snapshot_id,
            consent_revision=consent_revision,
            membership_revision=membership_revision,
        ),
    )


async def _register_membership(
    service: MemoryScopeService,
    family_space_id: str,
    subjects: tuple[str, ...],
    *,
    binding_version: int = 1,
) -> None:
    verifier = service.family_membership_verifier
    assert verifier is not None
    await verifier.register(
        family_space_id=family_space_id,
        binding_version=binding_version,
        subjects=frozenset(subjects),
    )


async def _register_consent(
    service: MemoryScopeService,
    covers_subjects: tuple[str, ...],
    *,
    snapshot_id: str = "consent-1",
) -> None:
    verifier = service.consent_verifier
    assert verifier is not None
    await verifier.register(snapshot_id, frozenset(covers_subjects))


async def _register_receipt(
    service: MemoryScopeService,
    fence: WriteFence,
    *,
    receipt_id: str = "receipt-1",
    capability: str = "memory_capture",
    effect: PolicyEffect = PolicyEffect.POLICY_EFFECT_ALLOW_WITH_OBLIGATIONS,
    obligations: tuple[PolicyObligation, ...] = (),
    actor_subject_id: str | None = None,
    subject_id: str | None = None,
    expires_at: datetime | None = None,
    binding_version: int | None = None,
    receipt_fence_valid: bool = True,
    exact_evidence_valid: bool = True,
    resource_owner_id: str | None = None,
    purpose: str = "memory_capture",
) -> None:
    verifier = service.receipt_verifier
    assert verifier is not None
    await verifier.register(
        make_memory_receipt(
            receipt_id=receipt_id,
            effect=effect,
            capability=capability,
            purpose=purpose,
            obligations=obligations,
            actor_subject_id=actor_subject_id or fence.actor_subject_id,
            subject_id=subject_id or fence.active_subject_id,
            resource_owner_id=resource_owner_id,
            fence=fence,
            binding_version=(
                binding_version if binding_version is not None else fence.binding_version
            ),
            expires_at=expires_at,
        ),
        receipt_fence_valid=receipt_fence_valid,
        exact_evidence_valid=exact_evidence_valid,
    )


@pytest.fixture
async def service() -> MemoryScopeService:
    store = InMemoryMemoryStore()
    await store.initialize()
    # ONE lock shared by every verifier mutation AND the commit authority:
    # a direct verifier revoke/register serializes against the commit
    # window the same way an authority mutation does (P0).
    lock = asyncio.Lock()
    receipt_verifier = InMemoryPolicyReceiptVerifier(lock=lock)
    consent_verifier = InMemoryConsentSnapshotVerifier(lock=lock)
    membership_verifier = InMemoryFamilyMembershipVerifier(lock=lock)
    promotion_authorizer = InMemoryPromotionAuthorizationPort(lock=lock)
    yield MemoryScopeService(
        store,
        receipt_verifier=receipt_verifier,
        family_membership_verifier=membership_verifier,
        consent_verifier=consent_verifier,
        grant_resolver=InMemoryRelationshipGrantResolver(),
        proposal_authorizer=InMemoryProposalAuthorizationPort(),
        approval_authorizer=InMemoryApprovalAuthorizationPort(),
        promotion_authorizer=promotion_authorizer,
        promotion_commit_authority=InMemoryPromotionCommitAuthority(
            store=store,
            consent_verifier=consent_verifier,
            membership_verifier=membership_verifier,
            promotion_authorizer=promotion_authorizer,
            receipt_verifier=receipt_verifier,
            lock=lock,
        ),
    )
    await store.close()
