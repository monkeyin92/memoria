"""Promotion concurrency, commit-authority serialization and canonical
promotion-fence tests for MemoryScopeService (PR-12/PR-14).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from services.memory_scope.domain import (
    MemoryScope,
    ProposalStateError,
    SharedMemoryProposal,
)
from services.memory_scope.in_memory_store import InMemoryMemoryStore
from services.memory_scope.repository import (
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
from services.memory_scope.tests.service_helpers import (
    _fence,
    _make_promotion_action_fence,
    _propose,
)

# Re-exported (not redefined) so pytest discovers the fixture by name.
from services.memory_scope.tests.service_helpers import (
    service as service,
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
