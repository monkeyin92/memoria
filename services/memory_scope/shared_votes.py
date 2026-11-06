"""Shared-proposal vote lifecycle seam of ``MemoryScopeService``.

``SharedVoteMixin`` is composed into ``MemoryScopeService``; the store, the
consent and family-membership verifiers, the write fence and the proposal and
promotion authorization helpers stay owned by the facade and are only declared
here for type checking.  The module exists so the family shared proposal
lifecycle can be read on its own, without the read, promotion and retention
code around it.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast

from packages.contracts.generated.python.multi_subject_contracts import MemoryScope

from services.memory_scope.domain import (
    ConfirmationVote,
    MemoryAuditEvent,
    MemoryNotFoundError,
    MemoryOutboxEvent,
    MemoryRecordStatusEvent,
    MemoryWriteRejected,
    NotAuthorizedError,
    ProposalStateError,
    SharedMemoryProposal,
    WriteFenceMismatchError,
)
from services.memory_scope.durable_capture import _new_id, _now

if TYPE_CHECKING:
    from datetime import datetime

    from services.memory_scope.domain import VoteDecision, WriteFence
    from services.memory_scope.repository import (
        ApprovalEvidence,
        ConsentSnapshotVerifier,
        FamilyMembershipVerifier,
        MemoryStore,
    )
    from services.policy.receipts import PolicyReceiptV2


class SharedVoteMixin:
    """Run the family shared-proposal vote lifecycle."""

    if TYPE_CHECKING:
        # Shared state and helpers owned by the MemoryScopeService facade.
        _store: MemoryStore
        _consent_verifier: ConsentSnapshotVerifier | None
        _family_membership_verifier: FamilyMembershipVerifier | None

        def _enforce_write_fence_for_context(
            self,
            *,
            fence: WriteFence,
            actor_subject_id: str,
            requested_scope: MemoryScope,
            now: datetime,
        ) -> None: ...

        async def _verify_family_membership(
            self,
            *,
            family_space_id: str,
            subject_ids: tuple[str, ...],
            binding_version: int,
            now: datetime,
        ) -> None: ...

        async def _verify_consent_authoritative(
            self,
            *,
            snapshot_id: str,
            scope: str,
            covers_subjects: tuple[str, ...],
            now: datetime,
        ) -> None: ...

        async def _verified_proposal_receipt(
            self,
            receipt_id: str,
            *,
            fence: WriteFence,
            actor_subject_id: str,
            family_space_id: str,
            now: datetime,
        ) -> PolicyReceiptV2 | None: ...

        async def _reverify_proposal_authorization(
            self,
            proposal: SharedMemoryProposal,
            voter_subject_id: str,
            *,
            now: datetime,
        ) -> ApprovalEvidence: ...

        async def _finalize_shared_promotion(
            self,
            proposal: SharedMemoryProposal,
            *,
            votes: tuple[ConfirmationVote, ...],
            now: datetime,
        ) -> None: ...

    async def propose_shared(
        self,
        *,
        actor_subject_id: str,
        family_space_id: str,
        co_subject_ids: tuple[str, ...],
        title: str,
        content: str,
        source_evidence_ids: tuple[str, ...],
        policy_receipt_id: str,
        consent_snapshot_id: str,
        fence: WriteFence,
    ) -> SharedMemoryProposal:
        """Propose a family-shared memory; stays pending until every
        co-subject confirms individually (section 5.4 case 3).

        ``fence`` binds the write to the current session epoch, binding role
        and runtime profile; the policy receipt must have been issued for
        exactly this fence (PR-12)."""

        now = _now()
        self._enforce_write_fence_for_context(
            fence=fence,
            actor_subject_id=actor_subject_id,
            requested_scope=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
            now=now,
        )
        if fence.family_space_id != family_space_id:
            raise WriteFenceMismatchError(
                "fence family scope does not match the requested family space"
            )
        # Main architecture review: a family-shared proposal may ONLY be
        # issued from a DEDICATED promotion authorization (§6.2
        # MEMORY_PROMOTION) - the Policy producer's memory_promotion
        # decision.  The memory_capture decision path is NEVER borrowed for
        # family promotion; an unconfigured authorizer fails closed.
        receipt = await self._verified_proposal_receipt(
            policy_receipt_id,
            fence=fence,
            actor_subject_id=actor_subject_id,
            family_space_id=family_space_id,
            now=now,
        )
        if receipt is None:
            raise MemoryWriteRejected(
                "family promotion is not authorized by a MEMORY_PROMOTION "
                "policy decision; a memory_capture receipt cannot propose "
                "a family-shared memory"
            )
        if consent_snapshot_id not in receipt.consent_snapshot_ids:
            raise MemoryWriteRejected(
                "consent snapshot was not referenced by the verified "
                "family-shared policy receipt"
            )
        # Fourth review: co-subjects and family scope are NOT caller
        # self-declarations - the authoritative family membership service
        # must prove proposer + every co-subject belong to the same family
        # under the current binding version, and the consent snapshot must
        # exist and cover them.
        await self._verify_family_membership(
            family_space_id=family_space_id,
            subject_ids=tuple(dict.fromkeys((actor_subject_id, *co_subject_ids))),
            binding_version=fence.binding_version,
            now=now,
        )
        await self._verify_consent_authoritative(
            snapshot_id=consent_snapshot_id,
            scope="memory",
            covers_subjects=tuple(dict.fromkeys((actor_subject_id, *co_subject_ids))),
            now=now,
        )
        if not co_subject_ids:
            raise MemoryWriteRejected("family_shared needs at least one co-subject")
        if not source_evidence_ids:
            raise MemoryWriteRejected("missing source_evidence_ids")
        if not policy_receipt_id:
            raise MemoryWriteRejected("missing policy receipt")
        if not consent_snapshot_id:
            raise MemoryWriteRejected("missing consent snapshot")
        if actor_subject_id in co_subject_ids:
            raise MemoryWriteRejected("actor must not be listed as its own co-subject")

        proposal_id = _new_id()
        now = _now()
        # Canonical exact-action evidence (PolicyActionResourceFence
        # contract): the AUTHORITATIVE consent/membership snapshot
        # identity + revision + hash come from the verifiers (never from
        # the caller); the capture evidence digest is the deterministic
        # canonical digest of the verified source evidence ids.  The final
        # promotion fence must match every one of these field-by-field.
        consent_verifier = self._consent_verifier
        membership_verifier = self._family_membership_verifier
        if consent_verifier is None or membership_verifier is None:
            raise MemoryWriteRejected(
                "family proposal needs authoritative consent/membership "
                "verifiers; writes fail closed"
            )
        consent_revision = consent_verifier.current_revision(consent_snapshot_id)
        consent_hash = consent_verifier.current_hash(consent_snapshot_id)
        membership_revision = membership_verifier.current_revision(
            family_space_id, fence.binding_version
        )
        membership_hash = membership_verifier.current_hash(
            family_space_id, fence.binding_version
        )
        if (
            consent_revision is None
            or consent_hash is None
            or membership_revision is None
            or membership_hash is None
        ):
            raise MemoryWriteRejected(
                "family proposal consent/membership snapshot revision/hash "
                "unavailable; writes fail closed"
            )
        capture_evidence_hash = hashlib.sha256(
            b"\x00".join(
                evidence.encode("utf-8") for evidence in source_evidence_ids
            )
        ).hexdigest()
        proposal = SharedMemoryProposal(
            proposal_id=proposal_id,
            family_space_id=family_space_id,
            proposer_subject_id=actor_subject_id,
            co_subject_ids=co_subject_ids,
            binding_version=fence.binding_version,
            session_id=fence.session_id,
            epoch=fence.epoch,
            binding_id=fence.binding_id,
            binding_role=fence.binding_role,
            runtime_profile_id=fence.runtime_profile_id,
            device_id=fence.device_id,
            subject_revision=fence.subject_revision,
            fence_context_hash=fence.fingerprint(),
            generation_id=fence.generation_id,
            turn_id=fence.turn_id,
            valid_until=fence.valid_until,
            title=title,
            content=content,
            source_evidence_ids=source_evidence_ids,
            proposal_policy_receipt_id=policy_receipt_id,
            consent_snapshot_id=consent_snapshot_id,
            proposal_revision=1,
            capture_evidence_hash=capture_evidence_hash,
            consent_snapshot_revision=consent_revision,
            consent_snapshot_hash=consent_hash,
            membership_snapshot_id=f"membership:{family_space_id}:"
            f"{fence.binding_version}",
            membership_snapshot_revision=membership_revision,
            membership_snapshot_hash=membership_hash,
            status="pending",
            created_at=now,
        )
        await self._store.persist_proposal(
            proposal,
            actor_family_space_id=fence.family_space_id,
            outbox=(
                MemoryOutboxEvent(
                    outbox_id=_new_id(),
                    event_id=f"{proposal_id}:proposed",
                    topic="memory.shared.proposed",
                    payload={
                        "proposal_id": proposal_id,
                        "family_space_id": family_space_id,
                        "proposer_subject_id": actor_subject_id,
                        "co_subject_ids": list(co_subject_ids),
                    },
                    created_at=now,
                ),
            ),
            audit=(
                MemoryAuditEvent(
                    event_id=_new_id(),
                    action="memory.shared.propose",
                    actor_subject_id=actor_subject_id,
                    subject_id=actor_subject_id,
                    record_id=None,
                    proposal_id=proposal_id,
                    payload={"family_space_id": family_space_id},
                    created_at=now,
                ),
            ),
        )
        return proposal

    async def confirm_shared(
        self, proposal_id: str, actor_subject_id: str, *, actor_family_space_id: str
    ) -> SharedMemoryProposal:
        """One co-subject confirms; when all confirm, the record is promoted.
        ``actor_subject_id`` is the ONLY voter identity accepted - a third
        party can never cast a vote for someone else (fourth/fifth review)."""
        proposal = await self._vote_shared(
            proposal_id,
            actor_subject_id,
            actor_family_space_id=actor_family_space_id,
            decision="confirm",
        )
        # Three authority actions (proposal / per-vote approval / final
        # promotion): when the vote set is now complete, run the promotion
        # finalizer (distinct fresh family_shared_memory_promotion
        # authorization + CAS promotion).  A non-final vote only appends.
        votes = await self._store.list_votes(
            proposal_id, actor_subject_id=actor_subject_id
        )
        confirmable = set(proposal.all_confirmable_subjects)
        confirmers = {
            vote.subject_id for vote in votes if vote.decision == "confirm"
        }
        objectors = {
            vote.subject_id for vote in votes if vote.decision == "object"
        }
        if not objectors and confirmers == confirmable:
            await self._finalize_shared_promotion(
                proposal,
                votes=votes,
                now=_now(),
            )
            return cast(
                SharedMemoryProposal,
                await self._store.get_proposal(
                    proposal_id,
                    actor_subject_id=actor_subject_id,
                    actor_family_space_id=actor_family_space_id,
                ),
            )
        return proposal

    async def object_shared(
        self, proposal_id: str, actor_subject_id: str, *, actor_family_space_id: str
    ) -> SharedMemoryProposal:
        """Any co-subject objection freezes the proposal (frozen)."""
        return await self._vote_shared(
            proposal_id,
            actor_subject_id,
            actor_family_space_id=actor_family_space_id,
            decision="object",
        )

    async def _vote_shared(
        self,
        proposal_id: str,
        actor_subject_id: str,
        *,
        actor_family_space_id: str,
        decision: VoteDecision,
    ) -> SharedMemoryProposal:
        proposal = await self._store.get_proposal(
            proposal_id,
            actor_subject_id=actor_subject_id,
            actor_family_space_id=actor_family_space_id,
        )
        if proposal is None:
            raise MemoryNotFoundError(f"proposal {proposal_id} not found")
        if actor_subject_id not in proposal.all_confirmable_subjects:
            raise NotAuthorizedError(
                f"{actor_subject_id} is not a co-subject of this memory"
            )
        if decision == "confirm" and proposal.status != "pending":
            raise ProposalStateError(
                f"proposal is {proposal.status}; confirm only accepts "
                "pending proposals"
            )
        if decision == "object" and proposal.status not in (
            "pending",
            "approvals_complete",
        ):
            raise ProposalStateError(
                f"proposal is {proposal.status}; object only accepts "
                "pending or approvals_complete proposals"
            )

        now = _now()
        # Main architecture review: every confirmation re-verifies the FULL
        # proposal authorization (all co-subjects, the complete immutable
        # fence, the policy receipt's exact evidence fence, the SAME consent
        # snapshot) against the CURRENT authoritative sources.  When the
        # authorization was revoked/expired/superseded after the proposal
        # was created, the proposal is atomically FROZEN with a precise
        # audit/outbox trail and can never promote a record.
        approval_receipt_id = ""
        approval_snapshot_id = ""
        approval_snapshot_revision = 0
        approval_snapshot_hash = ""
        if decision == "confirm":
            # Three authority actions: a confirm vote carries the voter's
            # OWN fresh approval evidence (actor = voter).  An objection
            # needs no approval/promotion receipt - it stays a privacy
            # fail-closed action.
            approval = await self._reverify_proposal_authorization(
                proposal,
                actor_subject_id,
                now=now,
            )
            approval_receipt_id = approval.receipt_id
            approval_snapshot_id = approval.approval_snapshot_id
            approval_snapshot_revision = approval.approval_snapshot_revision
            approval_snapshot_hash = approval.approval_snapshot_hash
        vote = ConfirmationVote(
            proposal_id=proposal_id,
            subject_id=actor_subject_id,
            decision=decision,
            voted_at=now,
            evidence_id=_new_id(),
            approval_receipt_id=approval_receipt_id,
            approval_snapshot_id=approval_snapshot_id,
            approval_snapshot_revision=approval_snapshot_revision,
            approval_snapshot_hash=approval_snapshot_hash,
        )
        # P0-F: the store decides the transition inside ONE transaction from
        # the full authoritative vote set; the service never pre-reads votes
        # and never emits terminal events itself.
        outcome = await self._store.vote_and_transition(
            vote,
            actor_family_space_id=actor_family_space_id,
            audit=(
                MemoryAuditEvent(
                    event_id=f"audit:{proposal_id}:vote:{actor_subject_id}",
                    action=(
                        "memory.shared.object"
                        if decision == "object"
                        else "memory.shared.confirm"
                    ),
                    actor_subject_id=actor_subject_id,
                    subject_id=proposal.proposer_subject_id,
                    record_id=None,
                    proposal_id=proposal_id,
                    payload=(
                        {"state": "frozen"}
                        if decision == "object"
                        else {"state": "approvals_complete", "all_subjects": True}
                    ),
                    created_at=now,
                ),
            ),
            now=now,
        )
        if outcome == "terminal":
            raise ProposalStateError(
                f"proposal state no longer accepts {decision} votes "
                "(confirm requires pending; object requires pending or "
                "approvals_complete)"
            )
        return cast(
            SharedMemoryProposal,
            await self._store.get_proposal(
                proposal_id,
                actor_subject_id=actor_subject_id,
                actor_family_space_id=actor_family_space_id,
            ),
        )

    async def withdraw_shared(
        self,
        proposal_id: str,
        actor_subject_id: str,
        *,
        actor_family_space_id: str,
    ) -> SharedMemoryProposal:
        """Withdraw: proposal -> withdrawn and record -> revoked, so the
        memory disappears from every retrieval path (section 13.5)."""
        proposal = await self._store.get_proposal(
            proposal_id,
            actor_subject_id=actor_subject_id,
            actor_family_space_id=actor_family_space_id,
        )
        if proposal is None:
            raise MemoryNotFoundError(f"proposal {proposal_id} not found")
        if proposal.status == "withdrawn":
            return proposal
        if actor_subject_id not in proposal.all_confirmable_subjects:
            raise NotAuthorizedError(
                f"{actor_subject_id} cannot withdraw a memory they are not part of"
            )
        now = _now()
        await self._verify_family_membership(
            family_space_id=proposal.family_space_id,
            subject_ids=(actor_subject_id,),
            binding_version=proposal.binding_version,
            now=now,
        )
        status_events: tuple[MemoryRecordStatusEvent, ...] = ()
        record_id: str | None = None
        records = await self._store.list_records_in_family(
            proposal.family_space_id,
            proposal.proposer_subject_id,
            scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
            actor_subject_id=actor_subject_id,
            actor_family_space_id=actor_family_space_id,
        )
        for record in records:
            if record.shared_proposal_id == proposal_id:
                record_id = record.record_id
                status_events = (
                    MemoryRecordStatusEvent(
                        event_id=_new_id(),
                        record_id=record.record_id,
                        status="revoked",
                        reason_code="shared_memory_withdrawn",
                        created_at=now,
                    ),
                )
                break
        await self._store.persist_withdrawal(
            proposal_id,
            actor_subject_id=actor_subject_id,
            record_id=record_id,
            status_events=status_events,
            outbox=(
                MemoryOutboxEvent(
                    outbox_id=_new_id(),
                    event_id=f"{proposal_id}:withdrawn",
                    topic="memory.shared.withdrawn",
                    payload={
                        "proposal_id": proposal_id,
                        "record_id": record_id,
                    },
                    created_at=now,
                ),
            ),
            audit=(
                MemoryAuditEvent(
                    event_id=_new_id(),
                    action="memory.shared.withdraw",
                    actor_subject_id=actor_subject_id,
                    subject_id=proposal.proposer_subject_id,
                    record_id=record_id,
                    proposal_id=proposal_id,
                    payload={"state": "withdrawn"},
                    created_at=now,
                ),
            ),
        )
        return cast(
            SharedMemoryProposal,
            await self._store.get_proposal(
                proposal_id,
                actor_subject_id=actor_subject_id,
                actor_family_space_id=actor_family_space_id,
            ),
        )
