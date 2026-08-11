"""Lifecycle and authorization tests for MemoryScopeService (PR-12/PR-14)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    BindingRole,
    PolicyActionResourceFence,
    PolicyEffect,
    PolicyObligation,
)
from services.memory_scope.domain import (
    ActorNotAuthorizedError,
    AlreadyVotedError,
    ConsentSnapshotInput,
    CoSubjectContext,
    CrossFamilyAccessError,
    MemoryNotFoundError,
    MemoryRecord,
    MemoryScope,
    MemoryWriteDraft,
    MemoryWriteRejected,
    NotAuthorizedError,
    PolicyDecisionInput,
    ProposalStateError,
    ReceiptNotVerifiedError,
    ResolutionContext,
    SharedMemoryProposal,
    SubjectContext,
    WriteFence,
    WriteFenceExpiredError,
    WriteFenceMissingError,
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


def _register_guardian(
    service: MemoryScopeService, actor_subject_id: str, wards: frozenset[str]
) -> None:
    resolver = service.grant_resolver
    assert resolver is not None
    resolver.register_guardian(actor_subject_id, wards)


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


class TestCapture:
    async def test_capture_private_persists_with_full_sources(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        await _register_receipt(service, _FENCE)
        await _register_consent(service, ("person-a",))
        record = await service.capture(
            _context(subject, requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE),
            _draft(),
            actor_subject_id="person-a",
        )
        assert record.scope is MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE
        assert record.subject_id == "person-a"
        assert record.resource_owner_id == "person-a"
        assert record.source_evidence_ids == ("evidence-1", "evidence-2")
        assert record.policy_receipt_id == "receipt-1"
        assert record.consent_snapshot_id == "consent-1"
        assert record.status == "confirmed"
        assert record.payload["content"] == "今天一起去了公园"

        fetched = await service.get(record.record_id, "person-a")
        assert fetched is not None
        assert fetched.payload["content"] == "今天一起去了公园"

    async def test_capture_rejects_unregistered_guest(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("guest-1", registered=False)
        guest_fence = _fence(
            actor_subject_id="guest-1", active_subject_id="guest-1"
        )
        await _register_receipt(service, guest_fence)
        await _register_consent(service, ("guest-1",))
        with pytest.raises(MemoryWriteRejected, match="unregistered_guest"):
            await service.capture(
                _context(
                    subject,
                    requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                    fence=guest_fence,
                ),
                _draft(),
                actor_subject_id="guest-1",
            )

    async def test_capture_rejects_unconfirmed_speaker(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a", speaker="unconfirmed")
        await _register_receipt(service, _FENCE)
        with pytest.raises(MemoryWriteRejected, match="subject_not_confirmed"):
            await service.capture(
                _context(subject, requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE),
                _draft(),
                actor_subject_id="person-a",
            )

    async def test_capture_rejects_policy_deny(self, service: MemoryScopeService) -> None:
        """A deny-effect receipt can never authorize a durable write: the
        authoritative receipt verification rejects it before resolution."""
        await _register_receipt(
            service,
            _FENCE,
            effect=PolicyEffect.POLICY_EFFECT_DENY,
        )
        with pytest.raises(ReceiptNotVerifiedError, match="does not match"):
            await service.capture(
                _context(
                    _subject("person-a"),
                    requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                ),
                _draft(),
                actor_subject_id="person-a",
            )

    async def test_capture_rejects_missing_evidence(
        self, service: MemoryScopeService
    ) -> None:
        await _register_receipt(service, _FENCE)
        with pytest.raises(MemoryWriteRejected, match="missing_sensitive_source"):
            await service.capture(
                _context(_subject("person-a"), requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE),
                MemoryWriteDraft(content="x"),
                actor_subject_id="person-a",
            )

    async def test_capture_rejects_missing_consent(
        self, service: MemoryScopeService
    ) -> None:
        await _register_receipt(service, _FENCE)
        with pytest.raises(MemoryWriteRejected, match="consent snapshot missing"):
            await service.capture(
                _context(
                    _subject("person-a"),
                    requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                    consent=_consent("person-a", granted=False),
                ),
                _draft(),
                actor_subject_id="person-a",
            )

    async def test_capture_guardian_summary_rejects_raw_transcript(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject(
            "person-child",
            category="minor",
            guardian_active=True,
        )
        guardian_fence = _fence(
            binding_role=BindingRole.BINDING_ROLE_GUARDIAN.value,
            actor_subject_id="parent-1",
            active_subject_id="person-child",
        )
        await _register_receipt(
            service,
            guardian_fence,
            actor_subject_id="parent-1",
            purpose="guardian_summary",
            capability="guardian_summary_view",
            obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
        )
        await _register_consent(service, ("person-child",))
        draft = MemoryWriteDraft(
            content="summary",
            source_evidence_ids=("evidence-1",),
            payload={"raw_transcript": "孩子说：妈妈我不想去上学"},
        )
        with pytest.raises(MemoryWriteRejected, match="raw transcript"):
            await service.capture(
                _context(
                    subject,
                    requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
                    policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,)),
                    fence=guardian_fence,
                ),
                draft,
                actor_subject_id="parent-1",
            )

    async def test_capture_guardian_summary_ok_for_aggregate(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-child", category="minor", guardian_active=True)
        guardian_fence = _fence(
            binding_role=BindingRole.BINDING_ROLE_GUARDIAN.value,
            actor_subject_id="parent-1",
            active_subject_id="person-child",
        )
        await _register_receipt(
            service,
            guardian_fence,
            actor_subject_id="parent-1",
            purpose="guardian_summary",
            capability="guardian_summary_view",
            obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
        )
        await _register_consent(service, ("person-child",))
        record = await service.capture(
            _context(
                subject,
                requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
                policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,)),
                fence=guardian_fence,
            ),
            _draft("本周学习 120 分钟，主题：英语"),
            actor_subject_id="parent-1",
        )
        assert record.scope is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY
        assert record.subject_id == "person-child"

    async def test_capture_family_shared_must_use_lifecycle(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a", family="family-1")
        family_fence = _fence(family_space_id="family-1")
        co = CoSubjectContext(
            subject_ids=("person-b",), confirmed_subject_ids=("person-b",)
        )
        await _register_receipt(service, family_fence)
        await _register_consent(service, ("person-a", "person-b"))
        with pytest.raises(MemoryWriteRejected, match="propose_shared"):
            await service.capture(
                _context(
                    subject,
                    requested=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
                    consent=_consent("person-a", "person-b"),
                    co_subjects=co,
                    fence=family_fence,
                ),
                _draft(),
                actor_subject_id="person-a",
            )

    async def test_capture_emits_outbox_and_audit(
        self, service: MemoryScopeService
    ) -> None:
        await _register_receipt(service, _FENCE)
        await _register_consent(service, ("person-a",))
        record = await service.capture(
            _context(_subject("person-a"), requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE),
            _draft(),
            actor_subject_id="person-a",
        )
        outbox = await service.store.list_pending_outbox()
        assert any(event.event_id == f"{record.record_id}:captured" for event in outbox)
        assert all(event.topic == "memory.record.captured" for event in outbox)


class TestWriteFence:
    """PR-12 / section 11.5 fence enforcement at the service boundary."""

    async def test_capture_without_fence_fails_closed(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        context = _context(
            subject, requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE
        )
        context = replace(context, fence=None)  # type: ignore[arg-type]
        with pytest.raises(WriteFenceMissingError, match="fence"):
            await service.capture(context, _draft(), actor_subject_id="person-a")

    async def test_capture_rejects_stale_receipt_after_epoch_bump(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        await _register_receipt(service, _fence(epoch=1))
        late = _fence(epoch=2)  # speaker switch bumped the epoch
        context = _context(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            fence=late,
        )
        with pytest.raises(ReceiptNotVerifiedError, match="does not match"):
            await service.capture(context, _draft(), actor_subject_id="person-a")

    async def test_capture_rejects_receipt_issued_for_different_binding(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        await _register_receipt(service, _FENCE)
        context = _context(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            fence=_fence(binding_id="binding-2"),
        )
        with pytest.raises(ReceiptNotVerifiedError, match="does not match"):
            await service.capture(context, _draft(), actor_subject_id="person-a")

    async def test_capture_rejects_expired_receipt(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        await _register_receipt(
            service,
            _FENCE,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        context = _context(
            subject, requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE
        )
        with pytest.raises(ReceiptNotVerifiedError, match="does not match"):
            await service.capture(context, _draft(), actor_subject_id="person-a")

    async def test_capture_rejects_legacy_receipt_with_older_binding_version(
        self, service: MemoryScopeService
    ) -> None:
        """P0-4/联审: a receipt issued under an older binding manifest version
        must never be reusable with the current fence."""
        subject = _subject("person-a")
        await _register_receipt(
            service,
            _FENCE,
            binding_version=1,  # legacy receipt
        )
        context = _context(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            fence=_fence(binding_version=2),
        )
        with pytest.raises(ReceiptNotVerifiedError, match="does not match"):
            await service.capture(context, _draft(), actor_subject_id="person-a")

    async def test_capture_rejects_expired_fence(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        context = _context(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            fence=_fence(valid_until=datetime.now(UTC) - timedelta(days=1)),
        )
        with pytest.raises(WriteFenceExpiredError, match="expired"):
            await service.capture(context, _draft(), actor_subject_id="person-a")

    async def test_capture_rejects_actor_who_is_not_the_subject(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-a")
        wrong_actor_fence = _fence(
            actor_subject_id="person-b", active_subject_id="person-a"
        )
        await _register_receipt(
            service,
            wrong_actor_fence,
            actor_subject_id="person-b",
            subject_id="person-a",
        )
        await _register_consent(service, ("person-a",))
        context = _context(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            fence=wrong_actor_fence,
        )
        with pytest.raises(ActorNotAuthorizedError, match="not the subject"):
            await service.capture(context, _draft(), actor_subject_id="person-b")

    async def test_guardian_summary_requires_guardian_binding_role(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-child", category="minor", guardian_active=True)
        primary_fence = _fence(
            actor_subject_id="parent-1", active_subject_id="person-child"
        )
        await _register_receipt(
            service,
            primary_fence,
            actor_subject_id="parent-1",
            purpose="guardian_summary",
            capability="guardian_summary_view",
            obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
        )
        await _register_consent(service, ("person-child",))
        context = _context(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
            policy=_policy(
                obligations=(
                    PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,
                )
            ),
            fence=primary_fence,  # primary-subject role is not enough
        )
        with pytest.raises(ActorNotAuthorizedError, match="guardian binding role"):
            await service.capture(context, _draft(), actor_subject_id="parent-1")

    async def test_propose_shared_rejects_stale_receipt(
        self, service: MemoryScopeService
    ) -> None:
        fence = _fence(family_space_id="family-1")
        await _register_receipt(
            service,
            fence,
            resource_owner_id="family-1",
            obligations=(
                PolicyObligation.POLICY_OBLIGATION_REQUIRE_SUBJECT_APPROVAL,
                PolicyObligation.POLICY_OBLIGATION_WRITE_POLICY_RECEIPT,
            ),
        )
        with pytest.raises(MemoryWriteRejected, match="promotion"):
            await service.propose_shared(
                actor_subject_id="person-a",
                family_space_id="family-1",
                co_subject_ids=("person-b",),
                title="t",
                content="c",
                source_evidence_ids=("evidence-1",),
                policy_receipt_id="receipt-99",
                consent_snapshot_id="consent-1",
                fence=fence,
            )

    async def test_propose_shared_requires_fence(
        self, service: MemoryScopeService
    ) -> None:
        with pytest.raises(WriteFenceMissingError, match="fence"):
            await service.propose_shared(
                actor_subject_id="person-a",
                family_space_id="family-1",
                co_subject_ids=("person-b",),
                title="t",
                content="c",
                source_evidence_ids=("evidence-1",),
                policy_receipt_id="receipt-1",
                consent_snapshot_id="consent-1",
                fence=None,  # type: ignore[arg-type]
            )

    async def test_expired_record_is_invisible_on_read(
        self, service: MemoryScopeService
    ) -> None:
        record = MemoryRecord(
            record_id="expired-1",
            scope=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            subject_id="person-a",
            resource_owner_id="person-a",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
            created_by_actor_id="person-a",
            retention_expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        await service.store.persist_record(
            record, actor_family_space_id=None
        )
        assert await service.get("expired-1", "person-a") is None
        assert await service.list_for_subject("person-a", "person-a") == ()
        assert not record.is_visible(datetime.now(UTC))


class TestParentReadsChildChat:
    """Section 6.5: parent gets summaries, never verbatim chat."""

    async def test_parent_cannot_read_child_private_chat(
        self, service: MemoryScopeService
    ) -> None:
        await _register_receipt(
            service,
            _fence(
                actor_subject_id="person-child",
                active_subject_id="person-child",
            ),
        )
        await _register_consent(service, ("person-child",))
        child_record = await service.capture(
            _context(
                _subject("person-child", category="minor", speaker="confirmed"),
                requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                fence=_fence(
                    actor_subject_id="person-child",
                    active_subject_id="person-child",
                ),
            ),
            _draft("我不想告诉妈妈"),
            actor_subject_id="person-child",
        )
        parent_view = await service.get(child_record.record_id, "parent-1")
        assert parent_view is None
        listed = await service.list_for_subject("person-child", "parent-1")
        assert listed == ()

    async def test_guardian_gets_summary_only_without_content(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-child", category="minor", guardian_active=True)
        guardian_fence = _fence(
            binding_role=BindingRole.BINDING_ROLE_GUARDIAN.value,
            actor_subject_id="parent-1",
            active_subject_id="person-child",
        )
        await _register_receipt(
            service,
            guardian_fence,
            actor_subject_id="parent-1",
            purpose="guardian_summary",
            capability="guardian_summary_view",
            obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
        )
        await _register_consent(service, ("person-child",))
        _register_guardian(
            service, "parent-1", frozenset({"person-child"})
        )
        record = await service.capture(
            _context(
                subject,
                requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
                policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,)),
                fence=guardian_fence,
            ),
            _draft("学习时长 120 分钟"),
            actor_subject_id="parent-1",
        )
        view = await service.get(
            record.record_id,
            "parent-1",
        )
        assert view is not None
        assert "content" not in view.payload
        # The minor subject themselves sees the full summary record.
        own = await service.get(record.record_id, "person-child")
        assert own is not None

    async def test_non_guardian_cannot_read_summary(
        self, service: MemoryScopeService
    ) -> None:
        subject = _subject("person-child", category="minor", guardian_active=True)
        guardian_fence = _fence(
            binding_role=BindingRole.BINDING_ROLE_GUARDIAN.value,
            actor_subject_id="parent-1",
            active_subject_id="person-child",
        )
        await _register_receipt(
            service,
            guardian_fence,
            actor_subject_id="parent-1",
            purpose="guardian_summary",
            capability="guardian_summary_view",
            obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
        )
        await _register_consent(service, ("person-child",))
        record = await service.capture(
            _context(
                subject,
                requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
                policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,)),
                fence=guardian_fence,
            ),
            _draft("学习时长 120 分钟"),
            actor_subject_id="parent-1",
        )
        view = await service.get(record.record_id, "stranger-1")
        assert view is None


class TestSharedLifecycle:
    async def test_pending_until_all_co_subjects_confirm(
        self, service: MemoryScopeService
    ) -> None:
        proposal = await _propose(service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b", "person-c"),
            title="全家旅行",
            content="2026 年夏全家去了杭州",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        assert proposal.status == "pending"
        visibility = await service.shared_visibility(proposal.proposal_id, "person-b", actor_family_space_id="family-1")
        assert not visibility.visible
        assert visibility.reason_code == "pending_confirmation"

        # One confirmation is not enough.
        after_b = await service.confirm_shared(proposal.proposal_id, "person-b", actor_family_space_id="family-1")
        assert after_b.status == "pending"
        assert not (await service.shared_visibility(proposal.proposal_id, "person-b", actor_family_space_id="family-1")).visible

        # Second confirmation still pending (person-c missing).
        after_c = await service.confirm_shared(proposal.proposal_id, "person-c", actor_family_space_id="family-1")
        assert after_c.status == "pending"

        # Proposer confirms last -> promoted and visible.
        after_a = await service.confirm_shared(proposal.proposal_id, "person-a", actor_family_space_id="family-1")
        assert after_a.status == "promoted"
        assert (await service.shared_visibility(proposal.proposal_id, "person-b", actor_family_space_id="family-1")).visible

        records = await service.list_family_memories(
            "family-1",
            "person-b",
            actor_family_space_id="family-1",
        )
        assert len(records) == 1
        assert records[0].scope is MemoryScope.MEMORY_SCOPE_FAMILY_SHARED
        assert records[0].shared_proposal_id == proposal.proposal_id

    async def test_any_objection_freezes_proposal(
        self, service: MemoryScopeService
    ) -> None:
        proposal = await _propose(service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="家庭视频",
            content="记录了孩子不想公开的画面",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        after_object = await service.object_shared(proposal.proposal_id, "person-b", actor_family_space_id="family-1")
        assert after_object.status == "frozen"
        visibility = await service.shared_visibility(proposal.proposal_id, "person-a", actor_family_space_id="family-1")
        assert not visibility.visible
        assert visibility.reason_code == "frozen_by_objection"
        # No record was ever created.
        records = await service.list_family_memories(
            "family-1", "person-a", actor_family_space_id="family-1"
        )
        assert records == ()

    async def test_cannot_vote_on_frozen_proposal(
        self, service: MemoryScopeService
    ) -> None:
        proposal = await _propose(service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.object_shared(proposal.proposal_id, "person-b", actor_family_space_id="family-1")
        with pytest.raises(ProposalStateError):
            await service.confirm_shared(proposal.proposal_id, "person-a", actor_family_space_id="family-1")

    async def test_withdrawn_memory_is_invisible(
        self, service: MemoryScopeService
    ) -> None:
        proposal = await _propose(service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="家庭故事",
            content="外婆的故事",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.confirm_shared(proposal.proposal_id, "person-b", actor_family_space_id="family-1")
        confirmed = await service.confirm_shared(proposal.proposal_id, "person-a", actor_family_space_id="family-1")
        assert confirmed.status == "promoted"
        records = await service.list_family_memories(
            "family-1", "person-b", actor_family_space_id="family-1"
        )
        assert len(records) == 1
        record_id = records[0].record_id

        withdrawn = await service.withdraw_shared(proposal.proposal_id, "person-b", actor_family_space_id="family-1")
        assert withdrawn.status == "withdrawn"

        assert await service.get(record_id, "person-b") is None
        assert (
            await service.list_family_memories(
                "family-1", "person-b", actor_family_space_id="family-1"
            )
            == ()
        )
        visibility = await service.shared_visibility(proposal.proposal_id, "person-b", actor_family_space_id="family-1")
        assert not visibility.visible
        assert visibility.reason_code == "withdrawn"

    async def test_family_admin_has_no_unlimited_read(
        self, service: MemoryScopeService
    ) -> None:
        # Member A's private memory is invisible to the family admin.
        await _register_receipt(service, _fence(family_space_id="family-1"))
        await _register_consent(service, ("person-a",))
        private = await service.capture(
            _context(
                _subject("person-a", family="family-1"),
                requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                fence=_fence(family_space_id="family-1"),
            ),
            _draft("私人日记"),
            actor_subject_id="person-a",
        )
        admin_view = await service.get(private.record_id, "admin-1")
        assert admin_view is None

        # The admin is not a co-subject of the shared memory either.
        proposal = await _propose(service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="旅行",
            content="杭州之行",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.confirm_shared(proposal.proposal_id, "person-b", actor_family_space_id="family-1")
        await service.confirm_shared(proposal.proposal_id, "person-a", actor_family_space_id="family-1")
        visibility = await service.shared_visibility(
            proposal.proposal_id, "admin-1", actor_family_space_id="family-1"
        )
        assert not visibility.visible
        assert visibility.reason_code == "not_a_member"
        with pytest.raises(NotAuthorizedError):
            await service.confirm_shared(
                proposal.proposal_id, "admin-1", actor_family_space_id="family-1"
            )

    async def test_cross_family_access_rejected(
        self, service: MemoryScopeService
    ) -> None:
        proposal = await _propose(service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="旅行",
            content="杭州之行",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.confirm_shared(proposal.proposal_id, "person-b", actor_family_space_id="family-1")
        await service.confirm_shared(proposal.proposal_id, "person-a", actor_family_space_id="family-1")
        with pytest.raises(CrossFamilyAccessError):
            await service.list_family_memories(
                "family-1", "person-c", actor_family_space_id="family-2"
            )
        # A member of the family sees nothing without being a co-subject.
        assert (
            await service.list_family_memories(
                "family-1", "person-c", actor_family_space_id="family-1"
            )
            == ()
        )

    async def test_proposer_cannot_vote_twice(self, service: MemoryScopeService) -> None:
        proposal = await _propose(service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.confirm_shared(proposal.proposal_id, "person-a", actor_family_space_id="family-1")
        with pytest.raises(AlreadyVotedError):
            await service.confirm_shared(proposal.proposal_id, "person-a", actor_family_space_id="family-1")

    async def test_unknown_proposal_not_found(self, service: MemoryScopeService) -> None:
        with pytest.raises(MemoryNotFoundError):
            await service.confirm_shared("missing-proposal", "person-a", actor_family_space_id="family-1")


class TestAuthoritativeVerifiers:
    """Fourth review: family membership, consent snapshots and relationship
    grants are authoritative; caller self-declarations fail closed."""

    async def test_consent_must_cover_subject_via_authoritative_snapshot(
        self, service: MemoryScopeService
    ) -> None:
        await _register_receipt(service, _FENCE)
        # The snapshot exists but does not cover the writing subject.
        await _register_consent(service, ("someone-else",))
        with pytest.raises(MemoryWriteRejected, match="does not cover"):
            await service.capture(
                _context(
                    _subject("person-a"),
                    requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                ),
                _draft(),
                actor_subject_id="person-a",
            )

    async def test_capture_fails_closed_without_consent_verifier(
        self,
    ) -> None:
        """No authoritative consent verifier configured: durable writes fail
        closed even with a well-formed caller context."""
        store = InMemoryMemoryStore()
        await store.initialize()
        service = MemoryScopeService(
            store, receipt_verifier=InMemoryPolicyReceiptVerifier()
        )
        await _register_receipt(service, _FENCE)
        with pytest.raises(MemoryWriteRejected, match="consent verifier"):
            await service.capture(
                _context(
                    _subject("person-a"),
                    requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                ),
                _draft(),
                actor_subject_id="person-a",
            )
        await store.close()

    async def test_cross_family_co_subject_rejected_by_membership(
        self, service: MemoryScopeService
    ) -> None:
        """The caller lists a co-subject that is NOT a verified member of the
        family: the proposal is rejected even though consent 'covers' them."""
        fence = _fence(family_space_id="family-1")
        await _register_family_proposal_receipt(service, fence, "family-1")
        assert service.proposal_authorizer is not None
        service.proposal_authorizer.register("receipt-1", "family-1")
        # Membership verifier only knows person-a (and person-b in another
        # family); person-c is not in family-1.
        await _register_membership(
            service, "family-1", ("person-a", "person-b"), binding_version=1
        )
        await _register_consent(service, ("person-a", "person-b", "person-c"))
        with pytest.raises(MemoryWriteRejected, match="membership"):
            await service.propose_shared(
                actor_subject_id="person-a",
                family_space_id="family-1",
                co_subject_ids=("person-b", "person-c"),
                title="t",
                content="c",
                source_evidence_ids=("evidence-1",),
                policy_receipt_id="receipt-1",
                consent_snapshot_id="consent-1",
                fence=fence,
            )

    async def test_propose_fails_closed_without_membership_verifier(
        self,
    ) -> None:
        store = InMemoryMemoryStore()
        await store.initialize()
        service = MemoryScopeService(
            store,
            receipt_verifier=InMemoryPolicyReceiptVerifier(),
            consent_verifier=InMemoryConsentSnapshotVerifier(),
            proposal_authorizer=InMemoryProposalAuthorizationPort(),
            promotion_authorizer=InMemoryPromotionAuthorizationPort(),
        )
        fence = _fence(family_space_id="family-1")
        await _register_family_proposal_receipt(service, fence, "family-1")
        assert service.proposal_authorizer is not None
        service.proposal_authorizer.register("receipt-1", "family-1")
        await _register_consent(service, ("person-a", "person-b"))
        with pytest.raises(MemoryWriteRejected, match="membership verifier"):
            await service.propose_shared(
                actor_subject_id="person-a",
                family_space_id="family-1",
                co_subject_ids=("person-b",),
                title="t",
                content="c",
                source_evidence_ids=("evidence-1",),
                policy_receipt_id="receipt-1",
                consent_snapshot_id="consent-1",
                fence=fence,
            )
        await store.close()

    async def test_forged_guardian_set_never_grants_read(
        self, service: MemoryScopeService
    ) -> None:
        """The old caller-supplied guardian_for_subjects set is gone: an
        actor without a verified guardian grant cannot read the summary."""
        subject = _subject("person-child", category="minor", guardian_active=True)
        guardian_fence = _fence(
            binding_role=BindingRole.BINDING_ROLE_GUARDIAN.value,
            actor_subject_id="parent-1",
            active_subject_id="person-child",
        )
        await _register_receipt(
            service,
            guardian_fence,
            actor_subject_id="parent-1",
            purpose="guardian_summary",
            capability="guardian_summary_view",
            obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
        )
        await _register_consent(service, ("person-child",))
        record = await service.capture(
            _context(
                subject,
                requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
                policy=_policy(
                    obligations=(
                        PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,
                    )
                ),
                fence=guardian_fence,
            ),
            _draft("学习时长 120 分钟"),
            actor_subject_id="parent-1",
        )
        # No guardian grant registered: even the parent gets nothing.
        assert await service.get(record.record_id, "parent-1") is None

    async def test_third_party_cannot_vote_on_behalf_of_co_subject(
        self, service: MemoryScopeService
    ) -> None:
        """A family member who is not a co-subject cannot cast a vote for a
        co-subject: the voter identity is the only thing the API accepts."""
        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        # 'admin-1' is a member of the family but NOT a co-subject.
        await _register_membership(
            service, "family-1", ("person-a", "person-b", "admin-1")
        )
        with pytest.raises(NotAuthorizedError, match="not a co-subject"):
            await service.confirm_shared(
                proposal.proposal_id, "admin-1", actor_family_space_id="family-1"
            )

    async def test_voter_membership_checked_at_vote_time(
        self, service: MemoryScopeService
    ) -> None:
        """A co-subject removed from the family (e.g. binding revoked) can no
        longer confirm: membership is verified at vote time."""
        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        # Re-register membership without person-b (binding revoked).
        await _register_membership(service, "family-1", ("person-a",))
        with pytest.raises(ProposalStateError, match="frozen"):
            await service.confirm_shared(proposal.proposal_id, "person-b", actor_family_space_id="family-1")
        frozen = await service.store.get_proposal(
            proposal.proposal_id,
            actor_subject_id="person-b",
            actor_family_space_id="family-1",
        )
        assert frozen is not None and frozen.status == "frozen"
        # No vote, no promoted record, exactly one freeze audit/outbox.
        votes = await service.store.list_votes(
            proposal.proposal_id, actor_subject_id="person-a"
        )
        assert votes == ()
        records = await service.store.list_records_for_subject(
            "person-a",
            scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert records == ()
        audits = [
            event
            for event in service.store.audit_events()
            if event.event_id == f"audit:{proposal.proposal_id}:frozen:authorization"
        ]
        assert len(audits) == 1

    async def test_family_propose_fails_closed_without_promotion_port(
        self,
    ) -> None:
        """§6.2 MEMORY_PROMOTION: without the dedicated promotion
        authorizer (the Policy producer's memory_promotion decision) every
        family proposal fails closed - the memory_capture path is never
        borrowed."""
        store = InMemoryMemoryStore()
        await store.initialize()
        service = MemoryScopeService(
            store,
            receipt_verifier=InMemoryPolicyReceiptVerifier(),
            family_membership_verifier=InMemoryFamilyMembershipVerifier(),
            consent_verifier=InMemoryConsentSnapshotVerifier(),
        )
        fence = _fence(family_space_id="family-1")
        await _register_receipt(service, fence, resource_owner_id="family-1")
        await _register_membership(
            service, "family-1", ("person-a", "person-b"), binding_version=1
        )
        await _register_consent(service, ("person-a", "person-b"))
        with pytest.raises(MemoryWriteRejected, match="promotion"):
            await service.propose_shared(
                actor_subject_id="person-a",
                family_space_id="family-1",
                co_subject_ids=("person-b",),
                title="t",
                content="c",
                source_evidence_ids=("evidence-1",),
                policy_receipt_id="receipt-1",
                consent_snapshot_id="consent-1",
                fence=fence,
            )
        await store.close()

    async def test_memory_capture_receipt_cannot_authorize_family_proposal(
        self, service: MemoryScopeService
    ) -> None:
        """Even a family-owned memory_capture receipt with the approval
        obligations cannot propose a family memory: the promotion
        authorizer has not issued a MEMORY_PROMOTION decision for it."""
        fence = _fence(family_space_id="family-1")
        await _register_receipt(
            service,
            fence,
            resource_owner_id="family-1",
            obligations=(
                PolicyObligation.POLICY_OBLIGATION_REQUIRE_SUBJECT_APPROVAL,
                PolicyObligation.POLICY_OBLIGATION_WRITE_POLICY_RECEIPT,
            ),
        )
        await _register_membership(
            service, "family-1", ("person-a", "person-b"), binding_version=1
        )
        await _register_consent(service, ("person-a", "person-b"))
        assert service.promotion_authorizer is not None
        # NOTE: no promotion authorization is registered for receipt-1.
        with pytest.raises(MemoryWriteRejected, match="promotion"):
            await service.propose_shared(
                actor_subject_id="person-a",
                family_space_id="family-1",
                co_subject_ids=("person-b",),
                title="t",
                content="c",
                source_evidence_ids=("evidence-1",),
                policy_receipt_id="receipt-1",
                consent_snapshot_id="consent-1",
                fence=fence,
            )

    async def test_confirmation_freezes_on_revoked_promotion_authorization(
        self, service: MemoryScopeService
    ) -> None:
        """P0-2: a revoked promotion decision after proposal creation freezes
        the proposal atomically - no vote, no record, exactly one
        audit/outbox pair."""
        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        assert service.promotion_authorizer is not None
        await service.promotion_authorizer.revoke(proposal.proposal_id)
        # First confirm: the vote is accepted (per-vote approval only - the
        # final promotion receipt belongs to the finalizer action).
        after_first = await service.confirm_shared(
            proposal.proposal_id,
            "person-a",
            actor_family_space_id="family-1",
        )
        assert after_first.status == "pending"
        # Second confirm completes the vote set: the finalizer now requires
        # the revoked fresh promotion authorization -> frozen, no record.
        with pytest.raises(ProposalStateError, match="frozen"):
            await service.confirm_shared(
                proposal.proposal_id,
                "person-b",
                actor_family_space_id="family-1",
            )
        frozen = await service.store.get_proposal(
            proposal.proposal_id,
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert frozen is not None and frozen.status == "frozen"
        # The confirm votes were accepted (three authority actions); only
        # the record is missing.
        votes = await service.store.list_votes(
            proposal.proposal_id, actor_subject_id="person-a"
        )
        assert len(votes) == 2
        assert (
            await service.store.list_records_for_subject(
                "person-a",
                scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
                actor_subject_id="person-a",
            )
            == ()
        )
        audits = [
            event
            for event in service.store.audit_events()
            if event.event_id == f"audit:{proposal.proposal_id}:frozen:authorization"
        ]
        outbox = [
            event
            for event in service.store.outbox_events()
            if event.event_id == f"{proposal.proposal_id}:frozen:authorization"
        ]
        assert len(audits) == 1 and len(outbox) == 1

    async def test_confirmation_freezes_on_expired_receipt_evidence(
        self, service: MemoryScopeService
    ) -> None:
        """P0-2: an expired/revoked receipt (exact evidence fence broken)
        freezes the proposal instead of leaving it pending."""
        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        # Re-register the receipt with the exact-evidence fence broken.
        await _register_receipt(
            service,
            _fence(family_space_id="family-1"),
            resource_owner_id="family-1",
            exact_evidence_valid=False,
        )
        with pytest.raises(ProposalStateError, match="frozen"):
            await service.confirm_shared(
                proposal.proposal_id,
                "person-b",
                actor_family_space_id="family-1",
            )
        frozen = await service.store.get_proposal(
            proposal.proposal_id,
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert frozen is not None and frozen.status == "frozen"
        assert (
            await service.store.list_votes(
                proposal.proposal_id, actor_subject_id="person-a"
            )
            == ()
        )
        assert (
            await service.store.list_records_for_subject(
                "person-a",
                scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
                actor_subject_id="person-a",
            )
            == ()
        )

    async def test_finalizer_consent_revoked_before_commit_no_record(
        self, service: MemoryScopeService
    ) -> None:
        """Reversed P0: when the current consent authority is revoked
        BEFORE the promotion commit, the finalizer MUST NOT write - the
        proposal freezes with a rejection audit and zero records.  The
        commit authority serializes the last current-evidence check with
        the store write (same lock), so a revoke can never race into the
        window after the check."""
        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.confirm_shared(
            proposal.proposal_id,
            "person-a",
            actor_family_space_id="family-1",
        )
        authority = service.promotion_commit_authority
        assert authority is not None
        # Revoke consent through the SAME authority lock the commit uses:
        # deterministic - the commit re-checks the CURRENT evidence and
        # must fail closed before any insert.
        await authority.revoke_consent("consent-1")
        with pytest.raises(ProposalStateError, match="frozen"):
            await service.confirm_shared(
                proposal.proposal_id,
                "person-b",
                actor_family_space_id="family-1",
            )
        frozen = await service.store.get_proposal(
            proposal.proposal_id,
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert frozen is not None and frozen.status == "frozen"
        assert (
            await service.store.list_records_for_subject(
                "person-a",
                scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
                actor_subject_id="person-a",
                actor_family_space_id="family-1",
            )
            == ()
        )
        # Rejection audit exists (freeze with the authority reason).
        audits = [
            event
            for event in service.store.audit_events()
            if event.event_id == f"audit:{proposal.proposal_id}:frozen:authorization"
        ]
        assert len(audits) == 1

    async def test_finalizer_membership_revision_bumped_before_commit_no_record(
        self, service: MemoryScopeService
    ) -> None:
        """A membership revision bump (binding revoked / re-issued) before
        the promotion commit also fails closed: no record, proposal
        frozen."""
        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.confirm_shared(
            proposal.proposal_id,
            "person-a",
            actor_family_space_id="family-1",
        )
        authority = service.promotion_commit_authority
        assert authority is not None
        await authority.revoke_membership("family-1", binding_version=1)
        with pytest.raises(ProposalStateError, match="frozen"):
            await service.confirm_shared(
                proposal.proposal_id,
                "person-b",
                actor_family_space_id="family-1",
            )
        frozen = await service.store.get_proposal(
            proposal.proposal_id,
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert frozen is not None and frozen.status == "frozen"
        assert (
            await service.store.list_records_for_subject(
                "person-a",
                scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
                actor_subject_id="person-a",
                actor_family_space_id="family-1",
            )
            == ()
        )

    async def test_finalizer_promotion_receipt_revoked_before_commit_no_record(
        self, service: MemoryScopeService
    ) -> None:
        """A fresh promotion authorization revoked before the commit fails
        closed: no record, proposal frozen."""
        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.confirm_shared(
            proposal.proposal_id,
            "person-a",
            actor_family_space_id="family-1",
        )
        authority = service.promotion_commit_authority
        assert authority is not None
        await authority.revoke_promotion(proposal.proposal_id)
        with pytest.raises(ProposalStateError, match="frozen"):
            await service.confirm_shared(
                proposal.proposal_id,
                "person-b",
                actor_family_space_id="family-1",
            )
        frozen = await service.store.get_proposal(
            proposal.proposal_id,
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert frozen is not None and frozen.status == "frozen"
        assert (
            await service.store.list_records_for_subject(
                "person-a",
                scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
                actor_subject_id="person-a",
                actor_family_space_id="family-1",
            )
            == ()
        )



class TestAtomicVotePromotion:
    """Fifth review: concurrent last votes must produce at most one promoted
    record and one state transition."""

    async def test_concurrent_last_votes_promote_once(
        self, service: MemoryScopeService
    ) -> None:
        import asyncio

        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        # person-a already confirmed; person-b's vote is the last one.
        await service.confirm_shared(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        results = await asyncio.gather(
            service.confirm_shared(
                proposal.proposal_id, "person-b", actor_family_space_id="family-1"
            ),
            service.confirm_shared(
                proposal.proposal_id, "person-b", actor_family_space_id="family-1"
            ),
            return_exceptions=True,
        )
        # At most one caller performs the promotion; the other is rejected
        # (already voted / no longer pending) - never two promotions.
        successes = [item for item in results if not isinstance(item, Exception)]
        assert len(successes) <= 1
        records = await service.list_family_memories(
            "family-1", "person-b", actor_family_space_id="family-1"
        )
        assert len(records) == 1
        assert records[0].shared_proposal_id == proposal.proposal_id
        final = await service.shared_visibility(
            proposal.proposal_id, "person-b", actor_family_space_id="family-1"
        )
        assert final.visible

    async def test_three_subject_concurrent_final_confirms_promote_once(
        self, service: MemoryScopeService
    ) -> None:
        import asyncio

        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b", "person-c"),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        # person-a and person-b already confirmed; person-c's vote is the
        # last one - race it twice.
        await service.confirm_shared(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        await service.confirm_shared(
            proposal.proposal_id, "person-b", actor_family_space_id="family-1"
        )
        results = await asyncio.gather(
            service.confirm_shared(
                proposal.proposal_id, "person-c", actor_family_space_id="family-1"
            ),
            service.confirm_shared(
                proposal.proposal_id, "person-c", actor_family_space_id="family-1"
            ),
            return_exceptions=True,
        )
        successes = [item for item in results if not isinstance(item, Exception)]
        assert len(successes) <= 1
        records = await service.list_family_memories(
            "family-1", "person-b", actor_family_space_id="family-1"
        )
        assert len(records) == 1

    async def test_objection_after_confirm_is_terminal_and_frozen(
        self, service: MemoryScopeService
    ) -> None:
        """Any objection freezes the proposal deterministically: a later
        confirm is rejected as terminal and no record is ever created."""
        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.confirm_shared(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        await service.object_shared(
            proposal.proposal_id, "person-b", actor_family_space_id="family-1"
        )
        visibility = await service.shared_visibility(
            proposal.proposal_id, "person-b", actor_family_space_id="family-1"
        )
        assert visibility.reason_code == "frozen_by_objection"
        with pytest.raises(ProposalStateError):
            await service.confirm_shared(
                proposal.proposal_id, "person-b", actor_family_space_id="family-1"
            )
        records = await service.list_family_memories(
            "family-1", "person-b", actor_family_space_id="family-1"
        )
        assert records == ()
        outbox = await service.store.list_pending_outbox()
        assert not any(
            event.topic == "memory.shared.confirmed" for event in outbox
        )

    async def test_intermediate_confirms_do_not_emit_terminal_events(
        self, service: MemoryScopeService
    ) -> None:
        """Non-final confirms never publish a confirmed event or a record."""
        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b", "person-c"),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.confirm_shared(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        await service.confirm_shared(
            proposal.proposal_id, "person-b", actor_family_space_id="family-1"
        )
        pending = await service.shared_visibility(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        assert pending.reason_code == "pending_confirmation"
        outbox = await service.store.list_pending_outbox()
        assert not any(
            event.topic == "memory.shared.confirmed" for event in outbox
        )
        assert not any(event.topic == "memory.shared.frozen" for event in outbox)


class TestPromotionCommitAuthoritySerialization:
    """Sixth-review P0: the transaction-bound PromotionCommitAuthority
    serializes the LAST current-evidence check with the store write under
    ONE lock.  A revocation that linearizes after the service prevalidation
    but before the commit is re-checked inside the lock and fails closed
    (frozen, zero records, rejection audit); a revoker that starts while
    the commit holds the lock can only apply STRICTLY AFTER the commit -
    there is no intermediate window.  Direct verifier mutations (bypassing
    the authority) share the same lock and cannot slip into the window."""

    async def _wired_service(
        self,
        *,
        pre_gate: Callable[[], Awaitable[object]] | None = None,
        in_commit_gate: Callable[[], Awaitable[object]] | None = None,
    ) -> tuple[MemoryScopeService, InMemoryConsentSnapshotVerifier]:
        store = InMemoryMemoryStore()
        await store.initialize()
        lock = asyncio.Lock()
        receipt_verifier = InMemoryPolicyReceiptVerifier(lock=lock)
        consent_verifier = InMemoryConsentSnapshotVerifier(lock=lock)
        membership_verifier = InMemoryFamilyMembershipVerifier(lock=lock)
        promotion_authorizer = InMemoryPromotionAuthorizationPort(lock=lock)
        authority = InMemoryPromotionCommitAuthority(
            store=store,
            consent_verifier=consent_verifier,
            membership_verifier=membership_verifier,
            promotion_authorizer=promotion_authorizer,
            receipt_verifier=receipt_verifier,
            lock=lock,
            in_commit_gate=in_commit_gate,
        )
        service = MemoryScopeService(
            store,
            receipt_verifier=receipt_verifier,
            family_membership_verifier=membership_verifier,
            consent_verifier=consent_verifier,
            grant_resolver=InMemoryRelationshipGrantResolver(),
            proposal_authorizer=InMemoryProposalAuthorizationPort(),
            approval_authorizer=InMemoryApprovalAuthorizationPort(),
            promotion_authorizer=promotion_authorizer,
            promotion_commit_authority=authority,
            pre_promotion_commit_gate=pre_gate,
        )
        return service, consent_verifier

    async def _propose_two(
        self, service: MemoryScopeService
    ) -> SharedMemoryProposal:
        return await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )

    async def test_revocation_between_prevalidation_and_commit_fails_closed(
        self,
    ) -> None:
        """The confirm task pauses AFTER the service prevalidation and
        BEFORE entering the commit authority; a DIRECT verifier revocation
        (bypassing the authority) linearizes first.  The commit re-checks
        the CURRENT consent under the lock and must fail closed: proposal
        frozen, zero records, one rejection audit."""
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pre_gate() -> None:
            entered.set()
            await release.wait()

        service, consent_verifier = await self._wired_service(pre_gate=pre_gate)
        proposal = await self._propose_two(service)
        await service.confirm_shared(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        confirm_task = asyncio.create_task(
            service.confirm_shared(
                proposal.proposal_id,
                "person-b",
                actor_family_space_id="family-1",
            )
        )
        await entered.wait()
        # Bypass: mutate the verifier directly through the service surface
        # instead of the authority - the shared lock still serializes it.
        await cast(Any, service.consent_verifier).revoke("consent-1")
        release.set()
        with pytest.raises(ProposalStateError):
            await confirm_task
        frozen = await service.store.get_proposal(
            proposal.proposal_id,
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert frozen is not None and frozen.status == "frozen"
        assert (
            await service.store.list_records_for_subject(
                "person-a",
                scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
                actor_subject_id="person-a",
                actor_family_space_id="family-1",
            )
            == ()
        )
        audits = [
            event
            for event in service.store.audit_events()
            if event.event_id
            == f"audit:{proposal.proposal_id}:frozen:authorization"
        ]
        assert len(audits) == 1
        assert not await consent_verifier.verify_consent(
            "consent-1",
            scope="memory",
            covers_subjects=("person-a", "person-b"),
            now=datetime.now(UTC),
        )
        await service.store.close()

    async def test_direct_promotion_revoke_bypass_serializes_fail_closed(
        self,
    ) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pre_gate() -> None:
            entered.set()
            await release.wait()

        service, _ = await self._wired_service(pre_gate=pre_gate)
        proposal = await self._propose_two(service)
        await service.confirm_shared(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        confirm_task = asyncio.create_task(
            service.confirm_shared(
                proposal.proposal_id,
                "person-b",
                actor_family_space_id="family-1",
            )
        )
        await entered.wait()
        await cast(Any, service.promotion_authorizer).revoke(proposal.proposal_id)
        release.set()
        with pytest.raises(ProposalStateError):
            await confirm_task
        frozen = await service.store.get_proposal(
            proposal.proposal_id,
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert frozen is not None and frozen.status == "frozen"
        assert (
            await service.store.list_records_for_subject(
                "person-a",
                scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
                actor_subject_id="person-a",
                actor_family_space_id="family-1",
            )
            == ()
        )
        await service.store.close()

    async def test_commit_holds_lock_revoker_applies_strictly_after_commit(
        self,
    ) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def in_commit_gate() -> None:
            entered.set()
            await release.wait()

        service, consent_verifier = await self._wired_service(
            in_commit_gate=in_commit_gate
        )
        proposal = await self._propose_two(service)
        await service.confirm_shared(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        confirm_task = asyncio.create_task(
            service.confirm_shared(
                proposal.proposal_id,
                "person-b",
                actor_family_space_id="family-1",
            )
        )
        await entered.wait()
        revoker = asyncio.create_task(consent_verifier.revoke("consent-1"))
        await asyncio.sleep(0)
        release.set()
        result = await confirm_task
        assert result.status == "promoted"
        await revoker
        records = await service.store.list_records_for_subject(
            "person-a",
            scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert len(records) == 1
        assert not await consent_verifier.verify_consent(
            "consent-1",
            scope="memory",
            covers_subjects=("person-a", "person-b"),
            now=datetime.now(UTC),
        )
        await service.store.close()

    async def test_commit_authority_requires_shared_lock_wiring(
        self,
    ) -> None:
        service, _ = await self._wired_service()
        proposal = await self._propose_two(service)
        await service.confirm_shared(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        votes = tuple(
            vote
            for vote in await service.store.list_votes(
                proposal.proposal_id, actor_subject_id="person-a"
            )
            if vote.subject_id == "person-a"
        )
        miswired = InMemoryPromotionCommitAuthority(
            store=service.store,
            consent_verifier=InMemoryConsentSnapshotVerifier(),
            membership_verifier=InMemoryFamilyMembershipVerifier(),
            promotion_authorizer=InMemoryPromotionAuthorizationPort(),
            receipt_verifier=InMemoryPolicyReceiptVerifier(),
        )
        with pytest.raises(RuntimeError, match="do not share the same lock"):
            await miswired.commit(
                proposal=proposal,
                votes=votes,
                actor_subject_id="person-a",
                audit=(),
                now=datetime.now(UTC),
            )
        await service.store.close()

    def test_promotion_authorization_rejects_missing_or_zero_revision_tokens(
        self,
    ) -> None:
        fence = _fence()
        base = dict(
            receipt_id="promotion-1",
            fence=fence,
            proposal_id="proposal-1",
            family_space_id="family-1",
            required_subject_ids=("person-a", "person-b"),
            approval_revisions=(
                ("person-a", "approval-snap-a", 1, "0" * 64),
            ),
            consent_snapshot_id="consent-1",
            membership_snapshot_id="membership:family-1:1",
        )
        base["action_resource_fence"] = _make_promotion_action_fence(
            _proposal_for_fence(),
            approval_snapshots={
                "person-a": ("approval-snap-a", 1, "0" * 64),
                "person-b": ("approval-snap-b", 1, "0" * 64),
            },
            issued_at=datetime.now(UTC) - timedelta(seconds=1),
            valid_until=datetime.now(UTC) + timedelta(minutes=10),
        )
        for token in ("consent_revision", "membership_revision"):
            with pytest.raises(ValueError, match=token):
                PromotionAuthorization(
                    **{
                        **base,
                        "consent_revision": 1,
                        "membership_revision": 1,
                        token: 0,
                    }
                )  # type: ignore[arg-type]
            with pytest.raises(ValueError, match=token):
                PromotionAuthorization(
                    **{
                        **base,
                        "consent_revision": 1,
                        "membership_revision": 1,
                        token: None,
                    }
                )  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="consent_revision"):
            PromotionAuthorization(
                **base, consent_revision=True, membership_revision=1  # type: ignore[arg-type]
            )


def _proposal_for_fence() -> SharedMemoryProposal:
    """Minimal pending proposal carrying full canonical evidence (used by
    pure fence-construction tests that never persist)."""
    now = datetime.now(UTC)
    return SharedMemoryProposal(
        proposal_id="proposal-1",
        family_space_id="family-1",
        proposer_subject_id="person-a",
        co_subject_ids=("person-b",),
        binding_version=1,
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_role="primary_subject",
        runtime_profile_id="profile-1",
        device_id="device-1",
        subject_revision=1,
        fence_context_hash="f" * 64,
        proposal_revision=1,
        capture_evidence_hash="c" * 64,
        consent_snapshot_revision=1,
        consent_snapshot_hash="b" * 64,
        membership_snapshot_id="membership:family-1:1",
        membership_snapshot_revision=1,
        membership_snapshot_hash="a" * 64,
        generation_id=None,
        turn_id=None,
        valid_until=now + timedelta(minutes=10),
        title="t",
        content="c",
        source_evidence_ids=("evidence-1",),
        proposal_policy_receipt_id="receipt-1",
        consent_snapshot_id="consent-1",
        status="pending",
        created_at=now,
    )


class TestApprovalsCompleteStateTransitions:
    """Canonical contract: pending -> approvals_complete -> promoted (plus
    frozen/withdrawn).  When the promotion authority is missing/blocked the
    proposal stays at approvals_complete - objection and withdrawal must
    remain possible there, confirm must not."""

    async def _service_without_commit_authority(
        self,
    ) -> MemoryScopeService:
        store = InMemoryMemoryStore()
        await store.initialize()
        service = MemoryScopeService(
            store,
            receipt_verifier=InMemoryPolicyReceiptVerifier(),
            family_membership_verifier=InMemoryFamilyMembershipVerifier(),
            consent_verifier=InMemoryConsentSnapshotVerifier(),
            grant_resolver=InMemoryRelationshipGrantResolver(),
            proposal_authorizer=InMemoryProposalAuthorizationPort(),
            approval_authorizer=InMemoryApprovalAuthorizationPort(),
            promotion_authorizer=InMemoryPromotionAuthorizationPort(),
            # No promotion_commit_authority: the finalizer fails closed and
            # the proposal stays at approvals_complete.
        )
        return service

    async def test_approvals_complete_blocks_confirm_allows_object_and_withdraw(
        self,
    ) -> None:
        service = await self._service_without_commit_authority()
        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.confirm_shared(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        with pytest.raises(MemoryWriteRejected, match="commit authority is not wired"):
            await service.confirm_shared(
                proposal.proposal_id,
                "person-b",
                actor_family_space_id="family-1",
            )
        current = await service.store.get_proposal(
            proposal.proposal_id,
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert current is not None and current.status == "approvals_complete"
        with pytest.raises(ProposalStateError, match="confirm only accepts"):
            await service.confirm_shared(
                proposal.proposal_id,
                "person-a",
                actor_family_space_id="family-1",
            )
        after_object = await service.object_shared(
            proposal.proposal_id, "person-b", actor_family_space_id="family-1"
        )
        assert after_object.status == "frozen"
        assert (
            await service.store.list_records_for_subject(
                "person-a",
                scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
                actor_subject_id="person-a",
                actor_family_space_id="family-1",
            )
            == ()
        )
        after_withdraw = await service.withdraw_shared(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        assert after_withdraw.status == "withdrawn"
        await service.store.close()

    async def test_withdraw_allowed_from_approvals_complete(self) -> None:
        service = await self._service_without_commit_authority()
        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.confirm_shared(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        with pytest.raises(MemoryWriteRejected, match="commit authority is not wired"):
            await service.confirm_shared(
                proposal.proposal_id,
                "person-b",
                actor_family_space_id="family-1",
            )
        current = await service.store.get_proposal(
            proposal.proposal_id,
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert current is not None and current.status == "approvals_complete"
        after_withdraw = await service.withdraw_shared(
            proposal.proposal_id, "person-b", actor_family_space_id="family-1"
        )
        assert after_withdraw.status == "withdrawn"
        await service.store.close()


class TestCanonicalPromotionFenceAdversarial:
    """Main review: the final promotion is authorized ONLY by a canonical
    PolicyActionResourceFence whose derived hashes are strict (a forged
    action_evidence_hash / canonical_hash is rejected) and whose every
    structural field matches the proposal's AUTHORITATIVE persisted state
    (wrong proposal id, wrong consent snapshot id with the same revision,
    wrong membership hash, replaced/wrong approval snapshot set, drifted
    generation/turn/tool epoch or an expired window all fail closed:
    proposal frozen, zero records)."""

    async def _wired(self) -> MemoryScopeService:
        store = InMemoryMemoryStore()
        await store.initialize()
        lock = asyncio.Lock()
        receipt_verifier = InMemoryPolicyReceiptVerifier(lock=lock)
        consent_verifier = InMemoryConsentSnapshotVerifier(lock=lock)
        membership_verifier = InMemoryFamilyMembershipVerifier(lock=lock)
        promotion_authorizer = InMemoryPromotionAuthorizationPort(lock=lock)
        authority = InMemoryPromotionCommitAuthority(
            store=store,
            consent_verifier=consent_verifier,
            membership_verifier=membership_verifier,
            promotion_authorizer=promotion_authorizer,
            receipt_verifier=receipt_verifier,
            lock=lock,
        )
        service = MemoryScopeService(
            store,
            receipt_verifier=receipt_verifier,
            family_membership_verifier=membership_verifier,
            consent_verifier=consent_verifier,
            grant_resolver=InMemoryRelationshipGrantResolver(),
            proposal_authorizer=InMemoryProposalAuthorizationPort(),
            approval_authorizer=InMemoryApprovalAuthorizationPort(),
            promotion_authorizer=promotion_authorizer,
            promotion_commit_authority=authority,
        )
        return service

    async def _propose_and_first_confirm(
        self, service: MemoryScopeService
    ) -> SharedMemoryProposal:
        proposal = await _propose(
            service,
            actor_subject_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
        )
        await service.confirm_shared(
            proposal.proposal_id, "person-a", actor_family_space_id="family-1"
        )
        return proposal

    async def _register_tampered(
        self,
        service: MemoryScopeService,
        proposal: SharedMemoryProposal,
        **overrides: object,
    ) -> PromotionAuthorization:
        """Register a promotion authorization whose canonical action fence
        drifted by ``overrides`` (structural tamper) or whose hashes were
        forged (``_hash_tamper``), REPLACING the honest registration: the
        honest entry (keyed by the real proposal id) is revoked first, so
        the tampered authorization - keyed by its OWN (possibly wrong)
        proposal id - is the only one the port can return."""
        assert service.promotion_authorizer is not None
        await service.promotion_authorizer.revoke(proposal.proposal_id)
        now = datetime.now(UTC)
        hash_tamper = overrides.pop("_hash_tamper", None)
        approval_snapshots = {
            subject: (
                f"approval-snap-{subject}",
                1,
                hashlib.sha256(
                    f"approval-snap:{proposal.proposal_id}:{subject}".encode()
                ).hexdigest(),
            )
            for subject in (
                proposal.proposer_subject_id,
                *proposal.co_subject_ids,
            )
        }
        fence = _make_promotion_action_fence(
            proposal,
            approval_snapshots=approval_snapshots,
            issued_at=now - timedelta(seconds=1),
            valid_until=now + timedelta(minutes=10),
            **overrides,
        )
        if hash_tamper is not None:
            fence = fence.model_copy(update={str(hash_tamper): "0" * 64})
        consent_verifier = service.consent_verifier
        membership_verifier = service.family_membership_verifier
        assert consent_verifier is not None and membership_verifier is not None
        consent_revision = consent_verifier.current_revision("consent-1")
        membership_revision = membership_verifier.current_revision(
            proposal.family_space_id, proposal.binding_version
        )
        assert consent_revision is not None and membership_revision is not None
        authorization = PromotionAuthorization(
            receipt_id="promotion-1",
            fence=_fence(
                actor_subject_id=proposal.proposer_subject_id,
                active_subject_id=proposal.proposer_subject_id,
                family_space_id=proposal.family_space_id,
                session_id="promotion-s1",
            ),
            action_resource_fence=fence,
            proposal_id=str(
                overrides.get("proposal_id", proposal.proposal_id)
            ),
            family_space_id=proposal.family_space_id,
            required_subject_ids=("person-a", "person-b"),
            approval_revisions=tuple(
                sorted(
                    (
                        subject,
                        snap[0],
                        snap[1],
                        snap[2],
                    )
                    for subject, snap in approval_snapshots.items()
                )
            ),
            consent_snapshot_id=str(
                overrides.get("consent_snapshot_id", proposal.consent_snapshot_id)
            ),
            membership_snapshot_id=str(
                overrides.get(
                    "membership_snapshot_id", proposal.membership_snapshot_id
                )
            ),
            consent_revision=consent_revision,
            membership_revision=membership_revision,
        )
        await service.promotion_authorizer.register(authorization)
        return authorization

    async def _assert_frozen_zero_records(
        self,
        service: MemoryScopeService,
        proposal: SharedMemoryProposal,
    ) -> None:
        with pytest.raises(ProposalStateError):
            await service.confirm_shared(
                proposal.proposal_id,
                "person-b",
                actor_family_space_id="family-1",
            )
        frozen = await service.store.get_proposal(
            proposal.proposal_id,
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert frozen is not None and frozen.status == "frozen"
        assert (
            await service.store.list_records_for_subject(
                "person-a",
                scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
                actor_subject_id="person-a",
                actor_family_space_id="family-1",
            )
            == ()
        )

    async def test_forged_action_evidence_hash_rejected(self) -> None:
        service = await self._wired()
        proposal = await self._propose_and_first_confirm(service)
        await self._register_tampered(
            service, proposal, _hash_tamper="action_evidence_hash"
        )
        await self._assert_frozen_zero_records(service, proposal)
        await service.store.close()

    async def test_forged_canonical_hash_rejected(self) -> None:
        service = await self._wired()
        proposal = await self._propose_and_first_confirm(service)
        await self._register_tampered(
            service, proposal, _hash_tamper="canonical_hash"
        )
        await self._assert_frozen_zero_records(service, proposal)
        await service.store.close()

    async def test_wrong_proposal_id_rejected(self) -> None:
        service = await self._wired()
        proposal = await self._propose_and_first_confirm(service)
        await self._register_tampered(
            service,
            proposal,
            proposal_id="proposal-OTHER",
            action_resource_id="proposal-OTHER",
        )
        await self._assert_frozen_zero_records(service, proposal)
        await service.store.close()

    async def test_wrong_consent_snapshot_id_same_revision_rejected(
        self,
    ) -> None:
        service = await self._wired()
        proposal = await self._propose_and_first_confirm(service)
        await self._register_tampered(
            service,
            proposal,
            consent_snapshot_id="consent-WRONG",
            consent_snapshot_revision=proposal.consent_snapshot_revision,
        )
        await self._assert_frozen_zero_records(service, proposal)
        await service.store.close()

    async def test_wrong_membership_hash_rejected(self) -> None:
        service = await self._wired()
        proposal = await self._propose_and_first_confirm(service)
        await self._register_tampered(
            service,
            proposal,
            membership_snapshot_hash="f" * 64,
        )
        await self._assert_frozen_zero_records(service, proposal)
        await service.store.close()

    async def test_wrong_generation_turn_tool_epoch_rejected(self) -> None:
        for override in (
            {"generation_id": 7},
            {"turn_id": 7},
            {"tool_epoch": 7},
        ):
            service = await self._wired()
            proposal = await self._propose_and_first_confirm(service)
            await self._register_tampered(service, proposal, **override)
            await self._assert_frozen_zero_records(service, proposal)
            await service.store.close()

    async def test_replaced_approval_snapshot_rejected(self) -> None:
        """The fence references a DIFFERENT approval snapshot identity for
        person-b (receipt swapped / snapshot replaced): the approval
        snapshot record-set comparison fails closed."""
        service = await self._wired()
        proposal = await self._propose_and_first_confirm(service)
        now = datetime.now(UTC)
        approval_snapshots = {
            "person-a": (
                "approval-snap-person-a",
                1,
                hashlib.sha256(
                    f"approval-snap:{proposal.proposal_id}:person-a".encode()
                ).hexdigest(),
            ),
            "person-b": (
                "approval-snap-person-b-SWAPPED",
                2,
                "d" * 64,
            ),
        }
        fence = _make_promotion_action_fence(
            proposal,
            approval_snapshots=approval_snapshots,
            issued_at=now - timedelta(seconds=1),
            valid_until=now + timedelta(minutes=10),
        )
        authorization = PromotionAuthorization(
            receipt_id="promotion-1",
            fence=_fence(
                actor_subject_id=proposal.proposer_subject_id,
                active_subject_id=proposal.proposer_subject_id,
                family_space_id=proposal.family_space_id,
                session_id="promotion-s1",
            ),
            action_resource_fence=fence,
            proposal_id=proposal.proposal_id,
            family_space_id=proposal.family_space_id,
            required_subject_ids=("person-a", "person-b"),
            approval_revisions=(
                ("person-a", "approval-snap-person-a", 1, "0" * 64),
                ("person-b", "approval-snap-person-b-SWAPPED", 2, "d" * 64),
            ),
            consent_snapshot_id=proposal.consent_snapshot_id,
            membership_snapshot_id=proposal.membership_snapshot_id,
            consent_revision=proposal.consent_snapshot_revision,
            membership_revision=proposal.membership_snapshot_revision,
        )
        assert service.promotion_authorizer is not None
        await service.promotion_authorizer.register(authorization)
        await self._assert_frozen_zero_records(service, proposal)
        await service.store.close()

    async def test_expired_fence_window_rejected(self) -> None:
        service = await self._wired()
        proposal = await self._propose_and_first_confirm(service)
        now = datetime.now(UTC)
        approval_snapshots = {
            subject: (
                f"approval-snap-{subject}",
                1,
                hashlib.sha256(
                    f"approval-snap:{proposal.proposal_id}:{subject}".encode()
                ).hexdigest(),
            )
            for subject in ("person-a", "person-b")
        }
        fence = _make_promotion_action_fence(
            proposal,
            approval_snapshots=approval_snapshots,
            issued_at=now - timedelta(minutes=30),
            valid_until=now - timedelta(minutes=20),
        )
        authorization = PromotionAuthorization(
            receipt_id="promotion-1",
            fence=_fence(
                actor_subject_id=proposal.proposer_subject_id,
                active_subject_id=proposal.proposer_subject_id,
                family_space_id=proposal.family_space_id,
                session_id="promotion-s1",
            ),
            action_resource_fence=fence,
            proposal_id=proposal.proposal_id,
            family_space_id=proposal.family_space_id,
            required_subject_ids=("person-a", "person-b"),
            approval_revisions=(
                ("person-a", "approval-snap-person-a", 1, "0" * 64),
                ("person-b", "approval-snap-person-b", 1, "0" * 64),
            ),
            consent_snapshot_id=proposal.consent_snapshot_id,
            membership_snapshot_id=proposal.membership_snapshot_id,
            consent_revision=proposal.consent_snapshot_revision,
            membership_revision=proposal.membership_snapshot_revision,
        )
        assert service.promotion_authorizer is not None
        await service.promotion_authorizer.register(authorization)
        await self._assert_frozen_zero_records(service, proposal)
        await service.store.close()
