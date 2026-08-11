"""Small external interface of the memory scope domain (PR-12/PR-14).

``MemoryScopeService`` owns the authorization and lifecycle rules; route
handlers and the agent only call these methods.  Everything unknown fails
closed with an exception carrying a stable ``reason_code``.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

from packages.contracts.generated.python.multi_subject_contracts import (
    BindingRole,
    BindingRoleValue,
    MemoryScope,
)

from services.memory_scope.domain import (
    FAMILY_APPROVAL_CAPABILITY,
    FAMILY_PROMOTION_CAPABILITY,
    FAMILY_PROPOSAL_CAPABILITY,
    OBLIGATION_REQUIRE_SUBJECT_APPROVAL,
    OBLIGATION_WRITE_POLICY_RECEIPT,
    ActorNotAuthorizedError,
    ConfirmationVote,
    CrossFamilyAccessError,
    MemoryAuditEvent,
    MemoryNotFoundError,
    MemoryOutboxEvent,
    MemoryRecord,
    MemoryRecordStatusEvent,
    MemoryWriteDraft,
    MemoryWriteRejected,
    NotAuthorizedError,
    PolicyEffect,
    PolicyObligation,
    ProposalStateError,
    ReceiptNotVerifiedError,
    ResolutionContext,
    RetentionPolicy,
    ScopeResolution,
    SharedMemoryProposal,
    SharedVisibility,
    VoteDecision,
    WriteFence,
    WriteFenceExpiredError,
    WriteFenceMismatchError,
    WriteFenceMissingError,
    ensure_sensitive_source,
    verify_receipt_against_fence,
)
from services.memory_scope.repository import (
    ApprovalAuthorizationPort,
    ApprovalEvidence,
    ConsentSnapshotVerifier,
    FamilyMembershipVerifier,
    MemoryStore,
    PolicyReceiptVerifier,
    PromotionAuthorization,
    PromotionAuthorizationPort,
    PromotionCommitAuthority,
    ProposalAuthorizationPort,
    RelationshipGrantResolver,
)
from services.memory_scope.resolver import MemoryScopeResolver
from services.policy.receipts import PolicyReceiptV2


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryScopeService:
    """Facade over ``MemoryStore``; the only object routes should hold."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        resolver: MemoryScopeResolver | None = None,
        receipt_verifier: PolicyReceiptVerifier | None = None,
        family_membership_verifier: FamilyMembershipVerifier | None = None,
        consent_verifier: ConsentSnapshotVerifier | None = None,
        grant_resolver: RelationshipGrantResolver | None = None,
        proposal_authorizer: ProposalAuthorizationPort | None = None,
        approval_authorizer: ApprovalAuthorizationPort | None = None,
        promotion_authorizer: PromotionAuthorizationPort | None = None,
        promotion_commit_authority: PromotionCommitAuthority | None = None,
        pre_promotion_commit_gate: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver or MemoryScopeResolver()
        self._receipt_verifier = receipt_verifier
        self._family_membership_verifier = family_membership_verifier
        self._consent_verifier = consent_verifier
        self._grant_resolver = grant_resolver
        self._proposal_authorizer = proposal_authorizer
        self._approval_authorizer = approval_authorizer
        self._promotion_authorizer = promotion_authorizer
        self._promotion_commit_authority = promotion_commit_authority
        self._pre_promotion_commit_gate = pre_promotion_commit_gate

    @property
    def store(self) -> MemoryStore:
        """Exposed for tests/operators; routes should stay on the facade."""
        return self._store

    @property
    def receipt_verifier(self) -> PolicyReceiptVerifier | None:
        """Authoritative receipt verifier (tests/operators)."""
        return self._receipt_verifier

    @property
    def family_membership_verifier(self) -> FamilyMembershipVerifier | None:
        return self._family_membership_verifier

    @property
    def consent_verifier(self) -> ConsentSnapshotVerifier | None:
        return self._consent_verifier

    @property
    def grant_resolver(self) -> RelationshipGrantResolver | None:
        return self._grant_resolver

    @property
    def promotion_authorizer(self) -> PromotionAuthorizationPort | None:
        return self._promotion_authorizer

    @property
    def promotion_commit_authority(self) -> PromotionCommitAuthority | None:
        return self._promotion_commit_authority

    @property
    def proposal_authorizer(self) -> ProposalAuthorizationPort | None:
        return self._proposal_authorizer

    @property
    def approval_authorizer(self) -> ApprovalAuthorizationPort | None:
        return self._approval_authorizer

    # -- scope resolution -------------------------------------------------

    def resolve_scope(self, context: ResolutionContext) -> ScopeResolution:
        """Ask the resolver; routes may preview before writing."""
        return self._resolver.resolve(context)

    async def accept_ephemeral(self, context: ResolutionContext) -> None:
        """Validate session-ephemeral context; nothing is persisted."""
        resolution = self._resolver.resolve(context)
        if resolution.scope is not MemoryScope.MEMORY_SCOPE_SESSION_EPHEMERAL:
            raise MemoryWriteRejected(
                f"ephemeral rejected: {resolution.reason_code}"
            )

    # -- durable capture --------------------------------------------------

    async def capture(
        self,
        context: ResolutionContext,
        draft: MemoryWriteDraft,
        *,
        actor_subject_id: str,
    ) -> MemoryRecord:
        """Resolve and persist one durable memory record, fail closed."""

        now = _now()
        fence = self._enforce_write_fence(
            context, actor_subject_id=actor_subject_id, now=now
        )

        subject = context.subject
        subject_id = subject.active_subject_id
        if subject_id is None:
            raise MemoryWriteRejected("subject_id missing")
        if context.policy.receipt_id is None:
            raise MemoryWriteRejected("policy receipt missing")
        if context.consent.snapshot_id is None:
            raise MemoryWriteRejected("consent snapshot missing")
        ensure_sensitive_source(
            subject_id=subject_id,
            resource_owner_id=subject_id,
            source_evidence_ids=draft.source_evidence_ids,
            policy_receipt_id=context.policy.receipt_id,
            consent_snapshot_id=context.consent.snapshot_id,
        )

        # P0 contract: the verified receipt is the ONLY authority for the
        # decision (effect/obligations/purpose) - the caller's policy input
        # is a routing hint only and is replaced before resolution.
        receipt = await self._verify_receipt(
            context=context,
            fence=fence,
            actor_subject_id=actor_subject_id,
            capability="memory_capture",
            now=now,
        )
        if receipt is None:
            raise MemoryWriteRejected("policy receipt not verified")
        if context.consent.snapshot_id not in receipt.consent_snapshot_ids:
            raise MemoryWriteRejected(
                "consent snapshot was not referenced by the verified policy receipt"
            )
        authoritative = replace(
            context,
            policy=replace(
                context.policy,
                effect=PolicyEffect.from_value(receipt.effect)
                or PolicyEffect.POLICY_EFFECT_DENY,
                obligations=self._authoritative_obligation_codes(receipt),
                receipt_id=receipt.receipt_id,
            ),
        )
        resolution = self._resolver.resolve(authoritative)
        if not resolution.persistable:
            raise MemoryWriteRejected(
                f"memory write rejected: {resolution.reason_code}"
            )
        if context.requested_scope is MemoryScope.MEMORY_SCOPE_FAMILY_SHARED:
            raise MemoryWriteRejected(
                "family_shared writes must go through propose_shared +"
                " per-co-subject confirmation"
            )
        if (
            context.requested_scope is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY
            and "raw_transcript" in draft.payload
        ):
            raise MemoryWriteRejected(
                "guardian_summary payload must not contain raw transcripts"
            )
        # Fourth review: the consent snapshot is verified against the
        # authoritative consent service; the caller's cover tuple is never
        # trusted on its own.
        await self._verify_consent_authoritative(
            snapshot_id=context.consent.snapshot_id,
            scope="memory",
            covers_subjects=(subject_id,),
            now=now,
        )

        if fence is None:  # pragma: no cover - resolver already denied
            raise WriteFenceMissingError("write fence required for durable capture")
        self._enforce_actor_binding_role(
            actor_subject_id=actor_subject_id,
            subject_id=subject_id,
            requested_scope=context.requested_scope,
            fence=fence,
        )

        retention, retention_expires_at = self._retention_policy(
            receipt, now=now
        )
        record_id = _new_id()
        record = MemoryRecord(
            record_id=record_id,
            scope=cast(MemoryScope, context.requested_scope),
            subject_id=subject_id,
            resource_owner_id=subject_id,
            family_space_id=subject.family_space_id,
            source_evidence_ids=draft.source_evidence_ids,
            policy_receipt_id=context.policy.receipt_id,
            consent_snapshot_id=context.consent.snapshot_id,
            memory_type=draft.memory_type,
            confidence=draft.confidence,
            status="confirmed",
            retention=cast(RetentionPolicy, retention),
            retention_expires_at=retention_expires_at,
            payload={**draft.payload, "content": draft.content},
            created_by_actor_id=actor_subject_id,
            created_at=now,
        )
        status_event = MemoryRecordStatusEvent(
            event_id=_new_id(),
            record_id=record_id,
            status="confirmed",
            reason_code="captured",
            created_at=now,
        )
        await self._store.persist_record(
            record,
            actor_family_space_id=fence.family_space_id,
            status_events=(status_event,),
            outbox=(
                MemoryOutboxEvent(
                    outbox_id=_new_id(),
                    event_id=f"{record_id}:captured",
                    topic="memory.record.captured",
                    payload={
                        "record_id": record_id,
                        "scope": record.scope.value,
                        "subject_id": subject_id,
                        "policy_receipt_id": record.policy_receipt_id,
                    },
                    created_at=now,
                ),
            ),
            audit=(
                MemoryAuditEvent(
                    event_id=_new_id(),
                    action="memory.capture",
                    actor_subject_id=actor_subject_id,
                    subject_id=subject_id,
                    record_id=record_id,
                    proposal_id=None,
                    payload={"scope": record.scope.value, "reason": resolution.reason_code},
                    created_at=now,
                ),
            ),
        )
        return record

    # -- family shared lifecycle (PR-14) ----------------------------------

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

    # -- retrieval with authorization --------------------------------------

    async def get(
        self,
        record_id: str,
        actor_subject_id: str,
        *,
        actor_family_space_id: str | None = None,
    ) -> MemoryRecord | None:
        """Read one record with the actor's grants; None when invisible."""
        now = _now()
        record = await self._store.get_record(
            record_id,
            actor_subject_id=actor_subject_id,
            actor_family_space_id=actor_family_space_id,
        )
        if record is None or not record.is_visible(now):
            record = await self._grant_scoped_get(
                record_id=record_id,
                actor_subject_id=actor_subject_id,
                actor_family_space_id=actor_family_space_id,
                now=now,
            )
            if record is None:
                return None
        return await self._authorize_record(
            record, actor_subject_id, now=now
        )

    async def _grant_scoped_get(
        self,
        *,
        record_id: str,
        actor_subject_id: str,
        actor_family_space_id: str | None,
        now: datetime,
    ) -> MemoryRecord | None:
        """Fifth review: guardian/legacy reads go through authoritative
        grants and a narrow grant-aware store context (never the worker
        role); the RLS pins owner + scope."""
        resolver = self._grant_resolver
        if resolver is None:
            return None
        candidates: list[tuple[str, str]] = []
        for ward in await resolver.guardian_of(
            actor_subject_id=actor_subject_id, now=now
        ):
            candidates.append((ward, "guardian_summary"))
        for owner in await resolver.legacy_grants_for(
            actor_subject_id=actor_subject_id, now=now
        ):
            candidates.append((owner, "legacy_archive"))
        for grant_owner_id, grant_scope in candidates:
            record = await self._store.get_record(
                record_id,
                actor_subject_id=actor_subject_id,
                actor_family_space_id=actor_family_space_id,
                grant_owner_id=grant_owner_id,
                grant_scope=grant_scope,
            )
            if record is not None and record.is_visible(now):
                return record
        return None

    async def list_for_subject(
        self,
        subject_id: str,
        actor_subject_id: str,
        scopes: tuple[MemoryScope, ...] | None = None,
        *,
        actor_family_space_id: str | None = None,
    ) -> tuple[MemoryRecord, ...]:
        """List records scoped to *subject_id* the actor may actually see."""
        now = _now()
        records = await self._store.list_records_for_subject(
            subject_id,
            scopes,
            actor_subject_id=actor_subject_id,
            actor_family_space_id=actor_family_space_id,
        )
        visible: list[MemoryRecord] = []
        for record in records:
            if not record.is_visible(now):
                continue
            authorized = await self._authorize_record(
                record,
                actor_subject_id,
                now=now,
            )
            if authorized is not None:
                visible.append(authorized)
        # Fifth review: guardian/legacy grants are resolved from the
        # authoritative relationship service and read through a narrow
        # grant-aware store context (never the worker role).
        grant_records = await self._grant_scoped_records(
            subject_id=subject_id,
            actor_subject_id=actor_subject_id,
            scopes=scopes,
            actor_family_space_id=actor_family_space_id,
            now=now,
        )
        for record in grant_records:
            if record.record_id not in {item.record_id for item in visible}:
                visible.append(record)
        return tuple(visible)

    async def _grant_scoped_records(
        self,
        *,
        subject_id: str,
        actor_subject_id: str,
        scopes: tuple[MemoryScope, ...] | None,
        actor_family_space_id: str | None,
        now: datetime,
    ) -> tuple[MemoryRecord, ...]:
        resolver = self._grant_resolver
        if resolver is None:
            return ()
        grants: list[tuple[str, str]] = []
        if subject_id in await resolver.guardian_of(
            actor_subject_id=actor_subject_id, now=now
        ):
            grants.append((subject_id, "guardian_summary"))
        if subject_id in await resolver.legacy_grants_for(
            actor_subject_id=actor_subject_id, now=now
        ):
            grants.append((subject_id, "legacy_archive"))
        result: list[MemoryRecord] = []
        for grant_owner_id, grant_scope in grants:
            if scopes is not None and MemoryScope.from_value(grant_scope) not in scopes:
                continue
            records = await self._store.list_records_for_subject(
                subject_id,
                scopes,
                actor_subject_id=actor_subject_id,
                actor_family_space_id=actor_family_space_id,
                grant_owner_id=grant_owner_id,
                grant_scope=grant_scope,
            )
            for record in records:
                if not record.is_visible(now):
                    continue
                if grant_scope == "guardian_summary":
                    record = _summary_only(record)
                result.append(record)
        return tuple(result)

    async def list_family_memories(
        self,
        family_space_id: str,
        actor_subject_id: str,
        *,
        actor_family_space_id: str | None,
    ) -> tuple[MemoryRecord, ...]:
        """Family-shared memories visible to a member of that family.

        A member of another family space is rejected outright
        (``CrossFamilyAccessError``) and the store query itself is scoped to
        the family, so a route bug cannot widen the query.
        """
        if actor_family_space_id != family_space_id:
            raise CrossFamilyAccessError(
                f"actor family {actor_family_space_id} does not match "
                f"family {family_space_id}"
            )
        now = _now()
        records = await self._store.list_records_in_family(
            family_space_id,
            actor_subject_id,
            scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
            actor_subject_id=actor_subject_id,
            actor_family_space_id=actor_family_space_id,
        )
        visible: list[MemoryRecord] = []
        for record in records:
            if not record.is_visible(now):
                continue
            authorized = await self._authorize_record(
                record,
                actor_subject_id,
                now=now,
            )
            if authorized is not None:
                visible.append(authorized)
        return tuple(visible)

    async def shared_visibility(
        self,
        proposal_id: str,
        actor_subject_id: str,
        *,
        actor_family_space_id: str,
    ) -> SharedVisibility:
        """What *actor* may see of a shared memory (pending/frozen/withdrawn
        are never content-visible; family admin is not privileged)."""
        proposal = await self._store.get_proposal(
            proposal_id,
            actor_subject_id=actor_subject_id,
            actor_family_space_id=actor_family_space_id,
        )
        if proposal is None:
            return SharedVisibility(
                visible=False, scope=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED, reason_code="not_found"
            )
        if actor_subject_id not in proposal.all_confirmable_subjects:
            return SharedVisibility(
                visible=False,
                scope=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
                reason_code="not_a_member",
            )
        if proposal.status == "promoted":
            return SharedVisibility(
                visible=True,
                scope=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
                reason_code="promoted",
            )
        if proposal.status == "frozen":
            return SharedVisibility(
                visible=False,
                scope=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
                reason_code="frozen_by_objection",
            )
        if proposal.status == "withdrawn":
            return SharedVisibility(
                visible=False,
                scope=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
                reason_code="withdrawn",
            )
        return SharedVisibility(
            visible=False,
            scope=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
            reason_code="pending_confirmation",
        )

    # -- internals ---------------------------------------------------------

    async def _authorize_record(
        self,
        record: MemoryRecord,
        actor_subject_id: str,
        *,
        now: datetime,
    ) -> MemoryRecord | None:
        scope = record.scope
        if scope is MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE:
            if actor_subject_id not in (record.subject_id, record.resource_owner_id):
                return None
            return record
        if scope is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY:
            if actor_subject_id == record.resource_owner_id:
                return record
            # Fourth review: guardian grants come from the authoritative
            # relationship service; without one, non-owner reads fail closed.
            resolver = self._grant_resolver
            if resolver is None:
                return None
            wards = await resolver.guardian_of(
                actor_subject_id=actor_subject_id, now=now
            )
            if record.resource_owner_id not in wards:
                return None
            return _summary_only(record)
        if scope is MemoryScope.MEMORY_SCOPE_FAMILY_SHARED:
            if actor_subject_id not in (
                record.subject_id,
                record.resource_owner_id,
                *record.co_subject_ids,
            ):
                return None
            if record.shared_proposal_id is None:
                return None
            proposal = await self._store.get_proposal(
                record.shared_proposal_id,
                actor_subject_id=actor_subject_id,
                actor_family_space_id=cast(str, record.family_space_id),
            )
            if proposal is None or proposal.status != "promoted":
                return None
            return record
        if scope is MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE:
            resolver = self._grant_resolver
            grantees: frozenset[str] = frozenset()
            if resolver is not None:
                grantees = await resolver.legacy_grants_for(
                    actor_subject_id=actor_subject_id, now=now
                )
            if actor_subject_id not in (
                record.resource_owner_id,
                *grantees,
            ):
                return None
            return record
        return None

    # -- write fences (PR-12 / section 11.5) ------------------------------

    def _enforce_write_fence(
        self,
        context: ResolutionContext,
        *,
        actor_subject_id: str,
        now: datetime,
    ) -> WriteFence:
        fence = context.fence
        if fence is None:
            raise WriteFenceMissingError(
                "durable memory write requires a session/binding/profile fence"
            )
        active_subject_id = context.subject.active_subject_id
        if not active_subject_id:
            raise WriteFenceMissingError("fence requires a confirmed active subject")
        self._enforce_write_fence_for_context(
            fence=fence,
            actor_subject_id=active_subject_id,
            requested_scope=context.requested_scope,
            now=now,
        )
        # P0-4: the fence must bind the writing actor and the confirmed
        # active subject so a receipt cannot be moved across callers or
        # subjects.  Actor != subject is only allowed for scopes whose
        # binding role explicitly authorizes it (guardian summary).
        if fence.actor_subject_id != actor_subject_id:
            raise WriteFenceMismatchError(
                "fence actor does not match the writing actor"
            )
        if fence.active_subject_id != active_subject_id:
            raise WriteFenceMismatchError(
                "fence active subject does not match the confirmed subject"
            )
        if (
            context.subject.family_space_id is not None
            and fence.family_space_id != context.subject.family_space_id
        ):
            raise WriteFenceMismatchError(
                "fence family scope does not match the confirmed subject's family"
            )
        policy = context.policy
        if policy.receipt_id is None:
            raise MemoryWriteRejected("policy receipt missing")
        return fence

    def _enforce_write_fence_for_context(
        self,
        *,
        fence: WriteFence,
        actor_subject_id: str,
        requested_scope: MemoryScope,
        now: datetime,
    ) -> None:
        if fence is None:
            raise WriteFenceMissingError(
                "durable memory write requires a session/binding/profile fence"
            )
        if not fence.session_id or not fence.binding_id or not fence.runtime_profile_id:
            raise WriteFenceMissingError(
                "fence must carry session_id, binding_id and runtime_profile_id"
            )
        if not fence.actor_subject_id or not fence.active_subject_id:
            raise WriteFenceMissingError(
                "fence must carry actor_subject_id and active_subject_id"
            )
        if fence.epoch is None or fence.epoch < 1:
            raise WriteFenceMissingError("fence epoch must be >= 1 (P0-4)")
        if fence.is_expired(now):
            raise WriteFenceExpiredError(
                "binding/profile fence expired; write fails closed"
            )
        if requested_scope is MemoryScope.MEMORY_SCOPE_FAMILY_SHARED:
            if not actor_subject_id:
                raise ActorNotAuthorizedError("family-shared write needs an actor")

    async def _verify_receipt(
        self,
        *,
        context: ResolutionContext | None,
        fence: WriteFence,
        actor_subject_id: str,
        capability: str,
        now: datetime,
        receipt_id: str | None = None,
    ) -> PolicyReceiptV2 | None:
        """Verify the policy receipt against the authoritative receipt
        store (P0-4 / cross-domain contract).  The verifier validates the
        receipt against the CURRENT evidence with the Policy V2 fence
        validators; the consumer additionally requires both verified flags
        (sensitive writes need the exact evidence fence) and matches the
        identity fields field-by-field.  The full-context ``context_hash``
        is never compared to the local fence fingerprint.  Without an
        injected verifier every durable write fails closed."""
        verifier = self._receipt_verifier
        if verifier is None:
            raise ReceiptNotVerifiedError(
                "no authoritative policy receipt verifier configured; "
                "durable memory writes fail closed"
            )
        receipt_id = receipt_id or (context.policy.receipt_id if context else None)
        if not receipt_id:
            raise MemoryWriteRejected("policy receipt missing")
        record = await verifier.verify(
            receipt_id,
            fence=fence,
            actor_subject_id=actor_subject_id,
            capability=(
                "guardian_summary_view"
                if context is not None
                and context.requested_scope
                is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY
                else capability
            ),
            now=now,
        )
        if record is None:
            raise ReceiptNotVerifiedError(f"policy receipt {receipt_id} not found")
        if not record.receipt_fence_valid or not record.exact_evidence_valid:
            raise ReceiptNotVerifiedError(
                f"policy receipt {receipt_id} is not currently fence-valid "
                "(context/evidence changed, expired, revoked or superseded)"
            )
        if not verify_receipt_against_fence(
            record.receipt,
            fence=fence,
            actor_subject_id=actor_subject_id,
            capability=(
                "guardian_summary_view"
                if context is not None
                and context.requested_scope
                is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY
                else capability
            ),
            purpose=(
                "guardian_summary"
                if context is not None
                and context.requested_scope
                is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY
                else "memory_capture"
            ),
            now=now,
            expected_resource_owner_id=(
                fence.active_subject_id
                if context is not None
                and context.requested_scope
                not in (
                    MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
                    MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,
                )
                else None
            ),
        ):
            raise ReceiptNotVerifiedError(
                f"policy receipt {receipt_id} does not match the write fence"
            )
        return record.receipt

    @staticmethod
    def _authoritative_obligation_codes(
        receipt: PolicyReceiptV2,
    ) -> tuple[PolicyObligation, ...]:
        """Canonical obligation codes from the verified receipt (the caller
        can never inject obligations)."""
        codes: list[PolicyObligation] = []
        for obligation in receipt.obligations:
            member = PolicyObligation.from_value(obligation.code)
            if member is not None and member not in codes:
                codes.append(member)
        return tuple(codes)

    def _retention_policy(
        self,
        receipt: PolicyReceiptV2,
        *,
        now: datetime,
    ) -> tuple[str, datetime | None]:
        """P0-3/P0 contract: retention comes ONLY from the verified
        receipt's RETENTION_TTL obligation parameters - never from a caller
        field."""
        for obligation in receipt.obligations:
            if obligation.code != "RETENTION_TTL":
                continue
            ttl = obligation.params.retention_ttl_seconds
            if ttl is None or ttl <= 0:
                raise MemoryWriteRejected(
                    "RETENTION_TTL requires a positive retention_ttl_seconds "
                    "in the verified policy receipt"
                )
            return "ttl", now + timedelta(seconds=ttl)
        return "indefinite", None

    async def _verify_family_membership(
        self,
        *,
        family_space_id: str,
        subject_ids: tuple[str, ...],
        binding_version: int,
        now: datetime,
    ) -> None:
        """Fourth review: family membership is authoritative; without a
        verifier every family-scoped write/vote fails closed."""
        verifier = self._family_membership_verifier
        if verifier is None:
            raise MemoryWriteRejected(
                "no authoritative family membership verifier configured; "
                "family-scoped writes fail closed"
            )
        if not await verifier.verify_membership(
            family_space_id=family_space_id,
            subject_ids=subject_ids,
            binding_version=binding_version,
            now=now,
        ):
            raise MemoryWriteRejected(
                f"family membership not verified for {sorted(subject_ids)} "
                f"in family {family_space_id} (binding v{binding_version})"
            )

    async def _reverify_proposal_authorization(
        self,
        proposal: SharedMemoryProposal,
        voter_subject_id: str,
        *,
        now: datetime,
    ) -> ApprovalEvidence:
        """Per-vote authorization right before a CONFIRM vote is recorded
        (three authority actions (proposal / per-vote approval / final promotion)): (1) the proposal authorization remains currently
        valid (provenance fence is immutable - drift fails; current
        revocation is decided by the proposal authorizer against current
        evidence revisions, the capture receipt's short-lived window is
        never replayed as final authority), and (2) THIS voter's OWN FRESH
        approval evidence (actor = voter, family_shared_memory_approval
        capability, must be valid AT voted_at).  On any failure the pending
        proposal is atomically frozen with a precise audit/outbox trail,
        then the error propagates.  Returns the voter's immutable approval
        evidence (receipt id + the canonical approval snapshot
        id/revision/hash triple) carried by the vote.  The final promotion
        is NOT authorized here - the promotion finalizer requests a
        DISTINCT fresh
        family_shared_memory_promotion receipt only after the full
        append-only approval set exists."""
        fence = WriteFence(
            session_id=proposal.session_id,
            epoch=proposal.epoch,
            binding_id=proposal.binding_id,
            binding_role=cast(BindingRoleValue, proposal.binding_role),
            runtime_profile_id=proposal.runtime_profile_id,
            actor_subject_id=proposal.proposer_subject_id,
            active_subject_id=proposal.proposer_subject_id,
            binding_version=proposal.binding_version,
            device_id=proposal.device_id,
            subject_revision=proposal.subject_revision,
            family_space_id=proposal.family_space_id,
            generation_id=proposal.generation_id,
            turn_id=proposal.turn_id,
            valid_until=proposal.valid_until,
        )
        if fence.fingerprint() != proposal.fence_context_hash:
            raise MemoryWriteRejected(
                "proposal fence snapshot is inconsistent; refusing to vote"
            )
        try:
            proposal_receipt = await self._verified_proposal_receipt(
                proposal.proposal_policy_receipt_id,
                fence=fence,
                actor_subject_id=proposal.proposer_subject_id,
                family_space_id=proposal.family_space_id,
                now=now,
            )
            if proposal_receipt is None:
                raise MemoryWriteRejected(
                    "family proposal authorization no longer valid"
                )
            if (
                proposal.consent_snapshot_id
                not in proposal_receipt.consent_snapshot_ids
            ):
                raise MemoryWriteRejected(
                    "consent snapshot no longer referenced by the policy receipt"
                )
            approval = await self._verified_approval_evidence(
                proposal.proposal_id,
                voter_subject_id=voter_subject_id,
                family_space_id=proposal.family_space_id,
                now=now,
            )
            if approval is None:
                raise MemoryWriteRejected(
                    "this co-subject's approval evidence is missing or no "
                    "longer valid"
                )
            await self._verify_family_membership(
                family_space_id=proposal.family_space_id,
                subject_ids=tuple(
                    dict.fromkeys(
                        (
                            proposal.proposer_subject_id,
                            *proposal.co_subject_ids,
                        )
                    )
                ),
                binding_version=proposal.binding_version,
                now=now,
            )
            await self._verify_consent_authoritative(
                snapshot_id=proposal.consent_snapshot_id,
                scope="memory",
                covers_subjects=tuple(
                    dict.fromkeys(
                        (
                            proposal.proposer_subject_id,
                            *proposal.co_subject_ids,
                        )
                    )
                ),
                now=now,
            )
        except (MemoryWriteRejected, ReceiptNotVerifiedError) as exc:
            state = await self._freeze_unauthorized_proposal(
                proposal, reason=str(exc), now=now
            )
            raise ProposalStateError(
                "family-shared authorization revoked before confirmation; "
                f"proposal is {state}, no record promoted"
            ) from exc
        return approval


    async def _verified_proposal_receipt(
        self,
        receipt_id: str,
        *,
        fence: WriteFence,
        actor_subject_id: str,
        family_space_id: str,
        now: datetime,
    ) -> PolicyReceiptV2 | None:
        """family_shared_memory_proposal authorization (three authority actions (proposal / per-vote approval / final promotion)):
        the ONLY authority is the injected ProposalAuthorizationPort (the
        Policy producer's dedicated proposal decision).  memory_capture /
        memory_promotion / private receipts are never borrowed; an
        unconfigured port or an unregistered authorization fails closed.
        The receipt is field-checked (capability/purpose/identity) even
        when the test verifier ignores the requested capability; the
        capture receipt's short-lived window is provenance, so expiry is
        NOT replayed here (current revocation is the port's job)."""
        authorizer = self._proposal_authorizer
        if authorizer is None:
            return None
        if not await authorizer.verify_proposal(
            receipt_id,
            fence=fence,
            actor_subject_id=actor_subject_id,
            family_space_id=family_space_id,
            now=now,
        ):
            return None
        verifier = self._receipt_verifier
        if verifier is None:
            return None
        record = await verifier.verify(
            receipt_id,
            fence=fence,
            actor_subject_id=actor_subject_id,
            capability=FAMILY_PROPOSAL_CAPABILITY,
            now=now,
        )
        if record is None:
            return None
        if not record.receipt_fence_valid or not record.exact_evidence_valid:
            return None
        receipt = record.receipt
        if not verify_receipt_against_fence(
            receipt,
            fence=fence,
            actor_subject_id=actor_subject_id,
            capability=FAMILY_PROPOSAL_CAPABILITY,
            purpose=FAMILY_PROPOSAL_CAPABILITY,
            now=now,
            expected_resource_owner_id=family_space_id,
            check_expiry=False,
        ):
            return None
        return receipt


    async def _verified_approval_evidence(
        self,
        proposal_id: str,
        *,
        voter_subject_id: str,
        family_space_id: str,
        now: datetime,
    ) -> ApprovalEvidence | None:
        """This voter's OWN immutable approval evidence (actor = voter).
        The receipt is field-checked against its own approval fence
        (capability/purpose/actor/subject/owner/device/binding/session/
        epoch/profile/revision + obligations) even when the test verifier
        ignores the requested capability."""
        authorizer = self._approval_authorizer
        if authorizer is None:
            return None
        evidence = await authorizer.verify_approval(
            proposal_id,
            actor_subject_id=voter_subject_id,
            now=now,
        )
        if evidence is None:
            return None
        verifier = self._receipt_verifier
        if verifier is None:
            return None
        record = await verifier.verify(
            evidence.receipt_id,
            fence=evidence.fence,
            actor_subject_id=voter_subject_id,
            capability=FAMILY_APPROVAL_CAPABILITY,
            now=now,
        )
        if record is None:
            return None
        if not record.receipt_fence_valid or not record.exact_evidence_valid:
            return None
        receipt = record.receipt
        if not verify_receipt_against_fence(
            receipt,
            fence=evidence.fence,
            actor_subject_id=voter_subject_id,
            capability=FAMILY_APPROVAL_CAPABILITY,
            purpose=FAMILY_APPROVAL_CAPABILITY,
            now=now,
            expected_resource_owner_id=family_space_id,
            check_expiry=True,
        ):
            return None
        return evidence

    async def _finalize_shared_promotion(
        self,
        proposal: SharedMemoryProposal,
        *,
        votes: tuple[ConfirmationVote, ...],
        now: datetime,
    ) -> None:
        """Promotion finalizer (§6.2 Memory Compiler, three authority
        actions: proposal / per-vote approval / final promotion): ONLY
        AFTER the append-only vote set contains every required confirm and
        no objection, the finalizer first runs the service-level
        prevalidation (``_verified_promotion_authorization``) and then the
        transaction-bound ``PromotionCommitAuthority`` re-checks the
        CURRENT consent snapshot revision, membership revision, the
        promotion receipt and the FRESH family_shared_memory_promotion
        authorization under ONE lock and CAS-promotes.  A revoke/update
        that lands between the last revalidation and the insert is
        serialized by that same lock and fails closed: proposal frozen,
        rejection audit, ZERO records.  An unconfigured authority (e.g.
        PostgreSQL, where the Policy/Consent database cannot join the
        insert transaction yet) fails closed - there is no auditable
        window in which a revoked authorization may still write."""
        authority = self._promotion_commit_authority
        if authority is None:
            raise MemoryWriteRejected(
                "family promotion commit authority is not wired; final "
                "promotion fails closed until the Policy/Consent authority "
                "can be checked in the same transaction"
            )
        if not self._promotion_authority_compatible(self._store, authority):
            raise MemoryWriteRejected(
                "family promotion commit authority is not a same-connection "
                "transaction seam with the Policy/Consent authority; the "
                "store requires one, so final promotion fails closed (503) "
                "until the Policy sub-agent's seam is wired - cross-database "
                "atomicity is never faked"
            )
        required_subjects = tuple(
            dict.fromkeys((proposal.proposer_subject_id, *proposal.co_subject_ids))
        )
        approval_revisions = tuple(
            sorted(
                (
                    vote.subject_id,
                    vote.approval_snapshot_id,
                    vote.approval_snapshot_revision,
                    vote.approval_snapshot_hash,
                )
                for vote in votes
                if vote.decision == "confirm"
            )
        )
        # Service-level prevalidation (fast fail OUTSIDE the authority
        # lock): the DISTINCT fresh promotion receipt must verify now with
        # capability/purpose/identity/obligations.  The authority re-runs
        # the same checks under its lock - the prevalidation is never the
        # final authority.
        authorization = await self._verified_promotion_authorization(
            proposal_id=proposal.proposal_id,
            family_space_id=proposal.family_space_id,
            required_subject_ids=required_subjects,
            approval_revisions=approval_revisions,
            now=now,
        )
        if authorization is None:
            state = await self._freeze_unauthorized_proposal(
                proposal,
                reason="promotion authorization unavailable/revoked "
                "before commit",
                now=now,
            )
            raise ProposalStateError(
                "family promotion rejected before commit: no fresh "
                f"family_shared_memory_promotion authorization; proposal "
                f"is {state}, no record promoted"
            )
        # Deterministic concurrency-test gate: pauses AFTER prevalidation
        # but BEFORE entering the commit authority, so a revocation can be
        # linearized first and the commit must then fail closed.
        if self._pre_promotion_commit_gate is not None:
            await self._pre_promotion_commit_gate()
        promoted, reason = await authority.commit(
            proposal=proposal,
            votes=votes,
            actor_subject_id=proposal.proposer_subject_id,
            audit=(
                MemoryAuditEvent(
                    event_id=f"audit:{proposal.proposal_id}:promoted",
                    action="memory.shared.promoted",
                    actor_subject_id=proposal.proposer_subject_id,
                    subject_id=proposal.proposer_subject_id,
                    record_id=None,
                    proposal_id=proposal.proposal_id,
                    payload={},
                    created_at=now,
                ),
            ),
            now=now,
        )
        if not promoted:
            current = await self._store.get_proposal(
                proposal.proposal_id,
                actor_subject_id=proposal.proposer_subject_id,
                actor_family_space_id=proposal.family_space_id,
            )
            if current is not None and current.status == "promoted":
                return
            state = current.status if current is not None else "unknown"
            raise ProposalStateError(
                "family promotion rejected before commit: "
                f"{reason or 'unknown'}; proposal is {state}, "
                "no record promoted"
            )

    @staticmethod
    def _promotion_authority_compatible(
        store: object, authority: object
    ) -> bool:
        """A store that requires a same-transaction authority (PostgreSQL:
        the Policy/Consent authority cannot join the insert transaction
        from the app process) may ONLY use an authority that IS that
        same-connection seam.  Test/reference coordinators (which verify
        first and commit through their own pool transaction) are refused -
        production promotion fails closed instead of faking cross-database
        atomicity."""
        if not getattr(store, "requires_same_transaction_authority", False):
            return True
        return bool(getattr(authority, "same_transaction_seam", False))


    async def _verified_promotion_authorization(
        self,
        *,
        proposal_id: str,
        family_space_id: str,
        required_subject_ids: tuple[str, ...],
        approval_revisions: tuple[tuple[str, str, int, str], ...],
        now: datetime,
    ) -> PromotionAuthorization | None:
        """FRESH final-promotion authorization: a DISTINCT
        family_shared_memory_promotion receipt bound to a FRESH promotion
        fence AND to this proposal_id + the exact required subjects + the
        exact per-vote approval revisions - never the proposal receipt or
        the proposal capture fence.  The receipt is field-checked for
        capability/purpose/identity/obligations and must be valid at
        ``now``.  Until the Policy producer implements the capability, an
        unconfigured port fails closed."""
        authorizer = self._promotion_authorizer
        if authorizer is None:
            return None
        authorization = await authorizer.verify_promotion(
            proposal_id=proposal_id,
            family_space_id=family_space_id,
            required_subject_ids=required_subject_ids,
            approval_revisions=approval_revisions,
            now=now,
        )
        if authorization is None:
            return None
        # Consumer field-by-field check: the authorization must be bound to
        # THIS proposal / family / exact required subjects / exact approval
        # revisions - a wrong proposal, a missing vote, a replaced approval
        # receipt or a duplicate/out-of-order revision fails closed.
        if authorization.proposal_id != proposal_id:
            return None
        if authorization.family_space_id != family_space_id:
            return None
        if authorization.required_subject_ids != tuple(required_subject_ids):
            return None
        if authorization.approval_revisions != tuple(approval_revisions):
            return None
        verifier = self._receipt_verifier
        if verifier is None:
            return None
        fence = authorization.fence
        record = await verifier.verify(
            authorization.receipt_id,
            fence=fence,
            actor_subject_id=fence.actor_subject_id,
            capability=FAMILY_PROMOTION_CAPABILITY,
            now=now,
        )
        if record is None:
            return None
        if not record.receipt_fence_valid or not record.exact_evidence_valid:
            return None
        receipt = record.receipt
        family_obligations = {
            OBLIGATION_REQUIRE_SUBJECT_APPROVAL,
            OBLIGATION_WRITE_POLICY_RECEIPT,
        }
        if not family_obligations.issubset(
            {obligation.code for obligation in receipt.obligations}
        ):
            return None
        if not verify_receipt_against_fence(
            receipt,
            fence=fence,
            actor_subject_id=fence.actor_subject_id,
            capability=FAMILY_PROMOTION_CAPABILITY,
            purpose=FAMILY_PROMOTION_CAPABILITY,
            now=now,
            expected_resource_owner_id=family_space_id,
            check_expiry=True,
        ):
            return None
        return authorization


    async def _freeze_unauthorized_proposal(
        self,
        proposal: SharedMemoryProposal,
        *,
        reason: str,
        now: datetime,
    ) -> str:
        """Atomically freeze a pending proposal whose authorization no
        longer holds (fail-closed, never promotes) with a precise
        audit/outbox trail; terminal proposals are idempotent no-ops.
        Returns the OBSERVED proposal status after the freeze attempt:
        a lost freeze CAS (for example a concurrent finalizer already
        promoted the proposal) is never misreported as ``frozen`` - the
        caller reports the real terminal state instead."""
        frozen = await self._store.freeze_proposal_atomically(
            proposal.proposal_id,
            actor_subject_id=proposal.proposer_subject_id,
            family_space_id=proposal.family_space_id,
            reason=reason,
            audit=(
                MemoryAuditEvent(
                    event_id=f"audit:{proposal.proposal_id}:frozen:authorization",
                    action="memory.shared.frozen",
                    actor_subject_id=proposal.proposer_subject_id,
                    subject_id=proposal.proposer_subject_id,
                    record_id=None,
                    proposal_id=proposal.proposal_id,
                    payload={"reason": reason},
                    created_at=now,
                ),
            ),
            outbox=(
                MemoryOutboxEvent(
                    outbox_id=f"{proposal.proposal_id}:frozen:authorization",
                    event_id=f"{proposal.proposal_id}:frozen:authorization",
                    topic="memory.shared.frozen",
                    payload={
                        "proposal_id": proposal.proposal_id,
                        "family_space_id": proposal.family_space_id,
                    },
                    created_at=now,
                ),
            ),
            now=now,
        )
        if frozen:
            return "frozen"
        current = await self._store.get_proposal(
            proposal.proposal_id,
            actor_subject_id=proposal.proposer_subject_id,
            actor_family_space_id=proposal.family_space_id,
        )
        return current.status if current is not None else "unknown"

    async def _verify_consent_authoritative(
        self,
        *,
        snapshot_id: str,
        scope: str,
        covers_subjects: tuple[str, ...],
        now: datetime,
    ) -> None:
        """Fourth review: consent snapshots are authoritative; caller cover
        tuples are never trusted on their own."""
        verifier = self._consent_verifier
        if verifier is None:
            raise MemoryWriteRejected(
                "no authoritative consent verifier configured; "
                "memory writes fail closed"
            )
        if not await verifier.verify_consent(
            snapshot_id,
            scope=scope,
            covers_subjects=covers_subjects,
            now=now,
        ):
            raise MemoryWriteRejected(
                f"consent snapshot {snapshot_id} does not cover "
                f"{sorted(covers_subjects)}"
            )

    def _enforce_actor_binding_role(
        self,
        *,
        actor_subject_id: str,
        subject_id: str,
        requested_scope: MemoryScope,
        fence: WriteFence,
    ) -> None:
        """The writing actor must be the subject, or a fenced binding role
        that explicitly authorizes writing for the scope (guardian summary)."""
        if requested_scope is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY:
            if fence.binding_role != BindingRole.BINDING_ROLE_GUARDIAN.value:
                raise ActorNotAuthorizedError(
                    "guardian_summary writes require a guardian binding role"
                )
            return
        if actor_subject_id != subject_id:
            raise ActorNotAuthorizedError(
                f"actor {actor_subject_id} is not the subject {subject_id}"
            )


def _summary_only(record: MemoryRecord) -> MemoryRecord:
    """Strip any raw content from a guardian projection (section 6.5)."""
    payload = dict(record.payload)
    payload.pop("content", None)
    payload.pop("raw_transcript", None)
    return replace(record, payload=payload)
