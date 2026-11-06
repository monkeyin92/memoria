"""Shared proposal lifecycle, approval state and authoritative evidence
tests for MemoryScopeService (PR-12/PR-14).
"""

from __future__ import annotations

import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    BindingRole,
    PolicyObligation,
)
from services.memory_scope.domain import (
    AlreadyVotedError,
    CrossFamilyAccessError,
    MemoryNotFoundError,
    MemoryScope,
    MemoryWriteRejected,
    NotAuthorizedError,
    ProposalStateError,
)
from services.memory_scope.in_memory_store import InMemoryMemoryStore
from services.memory_scope.repository import (
    InMemoryApprovalAuthorizationPort,
    InMemoryConsentSnapshotVerifier,
    InMemoryFamilyMembershipVerifier,
    InMemoryPolicyReceiptVerifier,
    InMemoryPromotionAuthorizationPort,
    InMemoryProposalAuthorizationPort,
    InMemoryRelationshipGrantResolver,
)
from services.memory_scope.service import MemoryScopeService
from services.memory_scope.tests.service_helpers import (
    _FENCE,
    _context,
    _draft,
    _fence,
    _policy,
    _propose,
    _register_consent,
    _register_family_proposal_receipt,
    _register_membership,
    _register_receipt,
    _subject,
)

# Re-exported (not redefined) so pytest discovers the fixture by name.
from services.memory_scope.tests.service_helpers import (
    service as service,
)


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
