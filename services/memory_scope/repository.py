"""Storage seam for the memory scope domain.

``MemoryStore`` is the only persistence contract the service depends on.
Adapters: ``InMemoryMemoryStore`` (Control API dev fixtures),
``SqliteMemoryStore`` (local development) and ``PostgresMemoryStore``
(production authority with FORCE RLS, section 11.7 / PR-17).

Repository-level subject scoping (section 13.5): every read path filters by
the caller's own subject id / family space inside the store, so a bug in a
route handler cannot widen the query.

Every call also carries the caller's ``actor_subject_id`` (and where relevant
``actor_family_space_id``): the PostgreSQL adapter pushes these into the
transaction-level app context (``app.memory.*``) evaluated by its RLS
policies and fails closed when a required context value is missing.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from packages.contracts.generated.python.multi_subject_contracts import (
    AgeBandValue,
    BindingRoleValue,
    PolicyActionResourceFence,
    ServiceModeValue,
    SpeakerStateValue,
    SubjectCategoryValue,
)

from services.memory_scope.domain import (
    FAMILY_PROMOTION_CAPABILITY,
    OBLIGATION_REQUIRE_SUBJECT_APPROVAL,
    OBLIGATION_WRITE_POLICY_RECEIPT,
    ConfirmationVote,
    MemoryAuditEvent,
    MemoryOutboxEvent,
    MemoryRecord,
    MemoryRecordStatusEvent,
    MemoryScope,
    ProposalStatus,
    SharedMemoryProposal,
    WriteFence,
    verify_receipt_against_fence,
)
from services.policy.receipts import PolicyReceiptV2

#: Canonical per-vote approval identity for the promotion CAS
#: (subject, approval_snapshot_id, approval_snapshot_revision,
#: approval_snapshot_hash) - the generated PolicyApprovalSnapshotFence
#: tuple, never just a receipt id pair.
ApprovalRevisionRef = tuple[str, str, int, str]


@dataclass(frozen=True, slots=True)
class VerifiedPolicyReceipt:
    """Authoritative verification outcome for a PolicyReceiptV2 (cross-domain
    contract): the verifier reads the receipt AND the current evidence and
    runs the Policy V2 fence validators
    (:func:`services.policy.receipts.receipt_fence_valid` /
    ``exact_evidence_fence_valid``).  ``receipt_fence_valid`` proves the
    literal identity + full-context canonical hash + decision window;
    ``exact_evidence_valid`` additionally proves every referenced
    consent/relationship/binding evidence is still present and active.
    Consumers then match the receipt field-by-field against their fence -
    they never compare the full-context hash to a local fingerprint."""

    receipt: PolicyReceiptV2
    receipt_fence_valid: bool
    exact_evidence_valid: bool


class PolicyReceiptVerifier(Protocol):
    """Authoritative policy-receipt verification (P0-4).

    Implementations look the receipt up in the policy engine's receipt store
    (never from caller-supplied fingerprints), validate it against the
    CURRENT evidence with the Policy V2 fence validators and return the
    verified outcome; ``None`` means the receipt does not exist.  The
    service then matches the returned receipt field-by-field against the
    presented write fence.
    """

    async def verify(
        self,
        receipt_id: str,
        *,
        fence: WriteFence,
        actor_subject_id: str,
        capability: str,
        now: datetime,
    ) -> VerifiedPolicyReceipt | None: ...


class InMemoryPolicyReceiptVerifier:
    """Development/test receipt store: receipts must be registered from the
    authoritative policy-engine side with an EXPLICIT verified assertion
    (the test is the verifier); the service only sees registered rows (a
    caller can never self-register through the service).  The registered
    ``receipt_fence_valid`` / ``exact_evidence_valid`` flags stand in for
    the Policy V2 fence validators that the production verifier runs."""

    def __init__(self, lock: asyncio.Lock | None = None) -> None:
        self._receipts: dict[str, VerifiedPolicyReceipt] = {}
        #: Shared serialization lock (P0): the promotion commit authority
        #: and EVERY mutation (register/revoke/clear) use the SAME lock, so
        #: a direct verifier revoke can never slip into the commit window
        #: between the last current-evidence check and the store write.
        self.lock = lock or asyncio.Lock()

    async def register(
        self,
        receipt: PolicyReceiptV2,
        *,
        receipt_fence_valid: bool = True,
        exact_evidence_valid: bool = True,
    ) -> None:
        async with self.lock:
            self._receipts[receipt.receipt_id] = VerifiedPolicyReceipt(
                receipt=receipt,
                receipt_fence_valid=receipt_fence_valid,
                exact_evidence_valid=exact_evidence_valid,
            )

    async def revoke(self, receipt_id: str) -> None:
        async with self.lock:
            self._receipts.pop(receipt_id, None)

    async def clear(self) -> None:
        async with self.lock:
            self._receipts.clear()

    async def verify(
        self,
        receipt_id: str,
        *,
        fence: WriteFence,
        actor_subject_id: str,
        capability: str,
        now: datetime,
    ) -> VerifiedPolicyReceipt | None:
        return self._receipts.get(receipt_id)


class FamilyMembershipVerifier(Protocol):
    """Authoritative family membership source (fourth review): proves that
    every subject belongs to the same family space under the current
    binding manifest version.  Callers can never self-declare co-subjects or
    family scope."""

    async def verify_membership(
        self,
        *,
        family_space_id: str,
        subject_ids: tuple[str, ...],
        binding_version: int,
        now: datetime,
    ) -> bool: ...

    def current_revision(
        self, family_space_id: str, binding_version: int
    ) -> int | None:
        """Current authoritative membership revision for the family under a
        binding version (bumped on every re-issue/revocation); the
        promotion commit authority CAS-matches it."""
        ...

    def current_hash(
        self, family_space_id: str, binding_version: int
    ) -> str | None:
        """Current authoritative membership snapshot canonical hash (the
        PolicyActionResourceFence contract binds the snapshot
        id/revision/hash - the promotion commit authority matches all
        three, so a re-issued snapshot with the same revision number but a
        different hash fails closed)."""
        ...


class ConsentSnapshotVerifier(Protocol):
    """Authoritative consent snapshot source (fourth review): a consent
    snapshot id must exist, be granted, and cover exactly the relevant
    subjects.  Caller-supplied cover tuples are never trusted."""

    async def verify_consent(
        self,
        snapshot_id: str,
        *,
        scope: str,
        covers_subjects: tuple[str, ...],
        now: datetime,
    ) -> bool: ...

    def current_revision(self, snapshot_id: str) -> int | None:
        """Current authoritative consent snapshot revision (bumped on every
        re-issue/revocation); the promotion commit authority CAS-matches
        it so a revoke can never race into the commit window."""
        ...

    def current_hash(self, snapshot_id: str) -> str | None:
        """Current authoritative consent snapshot canonical hash (the
        PolicyActionResourceFence contract binds snapshot id/revision/hash;
        a re-issued snapshot with the same revision number but a different
        hash must never pass the promotion commit CAS)."""
        ...


class RelationshipGrantResolver(Protocol):
    """Authoritative relationship-grant source for reads (fourth review):
    guardian summary and legacy archive visibility is resolved from the
    identity/relationship service, never from caller-supplied grant sets."""

    async def guardian_of(
        self, *, actor_subject_id: str, now: datetime
    ) -> frozenset[str]: ...

    async def legacy_grants_for(
        self, *, actor_subject_id: str, now: datetime
    ) -> frozenset[str]: ...


class ProposalAuthorizationPort(Protocol):
    """Authoritative family-shared PROPOSAL authorization (§6.2
    family_shared_memory_proposal / canonical contract sync): the proposal
    stage is authorized by its OWN receipt bound to the original complete
    capture fence (provenance).  Current revocation is decided by this port
    against the CURRENT evidence revisions - an expired capture receipt is
    provenance, never replayed as final authority.  A memory_capture /
    memory_promotion / private receipt can never authorize a proposal."""

    async def verify_proposal(
        self,
        receipt_id: str,
        *,
        fence: WriteFence,
        actor_subject_id: str,
        family_space_id: str,
        now: datetime,
    ) -> bool: ...


class InMemoryProposalAuthorizationPort:
    """Development/test proposal authorization (test-is-the-verifier);
    nothing is accepted by default."""

    def __init__(self) -> None:
        self._authorized: set[tuple[str, str]] = set()

    def register(self, receipt_id: str, family_space_id: str) -> None:
        self._authorized.add((receipt_id, family_space_id))

    def revoke(self, receipt_id: str, family_space_id: str) -> None:
        self._authorized.discard((receipt_id, family_space_id))

    async def verify_proposal(
        self,
        receipt_id: str,
        *,
        fence: WriteFence,
        actor_subject_id: str,
        family_space_id: str,
        now: datetime,
    ) -> bool:
        return (receipt_id, family_space_id) in self._authorized


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    """One voter's immutable approval evidence (actor = voter): the receipt
    id plus the fence the approval decision was issued under, PLUS the
    canonical approval snapshot identity (subject / snapshot id / revision
    / hash) the policy decision referenced.  Every confirm vote carries its
    own; the promotion fence's ``approval_snapshots`` must record-set-equal
    the persisted votes on this triple."""

    receipt_id: str
    fence: WriteFence
    approval_snapshot_id: str
    approval_snapshot_revision: int
    approval_snapshot_hash: str

    def __post_init__(self) -> None:
        if not self.receipt_id.strip():
            raise ValueError("ApprovalEvidence receipt_id must not be empty")
        if not self.approval_snapshot_id.strip():
            raise ValueError(
                "ApprovalEvidence approval_snapshot_id must not be empty"
            )
        if (
            not isinstance(self.approval_snapshot_revision, int)
            or isinstance(self.approval_snapshot_revision, bool)
            or self.approval_snapshot_revision < 1
        ):
            raise ValueError(
                "ApprovalEvidence approval_snapshot_revision must be an "
                "integer >= 1"
            )
        if (
            not isinstance(self.approval_snapshot_hash, str)
            or len(self.approval_snapshot_hash) != 64
            or any(
                c not in "0123456789abcdef"
                for c in self.approval_snapshot_hash.lower()
            )
        ):
            raise ValueError(
                "ApprovalEvidence approval_snapshot_hash must be a 64-char "
                "hex digest"
            )


class ApprovalAuthorizationPort(Protocol):
    """Authoritative per-vote approval authorization: each co-subject's
    confirmation is backed by their OWN immutable approval evidence
    (actor = voter).  An unconfigured port fails closed."""

    async def verify_approval(
        self,
        proposal_id: str,
        *,
        actor_subject_id: str,
        now: datetime,
    ) -> ApprovalEvidence | None: ...


class InMemoryApprovalAuthorizationPort:
    """Development/test per-vote approval (test-is-the-verifier); nothing
    is accepted by default."""

    def __init__(self) -> None:
        self._approved: dict[tuple[str, str], ApprovalEvidence] = {}

    def register(
        self,
        proposal_id: str,
        actor_subject_id: str,
        evidence: ApprovalEvidence,
    ) -> None:
        self._approved[(proposal_id, actor_subject_id)] = evidence

    def revoke(self, proposal_id: str, actor_subject_id: str) -> None:
        self._approved.pop((proposal_id, actor_subject_id), None)

    async def verify_approval(
        self,
        proposal_id: str,
        *,
        actor_subject_id: str,
        now: datetime,
    ) -> ApprovalEvidence | None:
        return self._approved.get((proposal_id, actor_subject_id))


@dataclass(frozen=True, slots=True)
class PromotionAuthorization:
    """Fresh final-promotion authorization (family_shared_memory_promotion):
    the receipt id AND the fresh promotion fence (current family owner /
    current evidence revisions) it was issued under, EXPLICITLY bound to
    the proposal id, the family space, the exact required subjects and the
    exact per-vote approval evidence revisions.  ``action_resource_fence``
    is the CANONICAL ``PolicyActionResourceFence`` from the generated
    contract (never a locally copied fence): the consumer matches every
    field (capability/purpose/action_resource_id==proposal_id, proposal
    revision, required approval subjects + approval snapshots, capture
    evidence ids/hash, consent/membership id+revision+hash, generation /
    turn / tool epoch, validity) against the proposal's AUTHORITATIVE
    persisted state.  The consent/membership SNAPSHOT IDS are also bound
    here: a "revision number coincidence" on a DIFFERENT snapshot can never
    bypass the commit CAS.  The port and the consumer field-check every one
    of these; a wrong proposal / missing vote / replaced approval receipt /
    duplicate or out-of-order revision is rejected."""

    receipt_id: str
    fence: WriteFence
    action_resource_fence: PolicyActionResourceFence
    proposal_id: str
    family_space_id: str
    required_subject_ids: tuple[str, ...]
    approval_revisions: tuple[ApprovalRevisionRef, ...]
    consent_snapshot_id: str
    membership_snapshot_id: str
    #: Current authoritative revision tokens CAS-matched at commit time
    #: (REQUIRED positive integers - a missing token can never bypass the
    #: CAS): the promotion only commits when the CURRENT consent snapshot /
    #: membership revision still equal these tokens (a revoke or a
    #: re-issue in between fails closed).
    consent_revision: int
    membership_revision: int

    def __post_init__(self) -> None:
        for name, value in (
            ("consent_revision", self.consent_revision),
            ("membership_revision", self.membership_revision),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
            ):
                raise ValueError(
                    f"{name} must be a positive integer (revocation CAS)"
                )
        if not self.receipt_id.strip():
            raise ValueError("promotion authorization receipt_id must not be empty")
        if not self.consent_snapshot_id.strip():
            raise ValueError(
                "promotion authorization consent_snapshot_id must not be empty"
            )
        if not self.membership_snapshot_id.strip():
            raise ValueError(
                "promotion authorization membership_snapshot_id must not be empty"
            )
        if self.action_resource_fence.capability != FAMILY_PROMOTION_CAPABILITY:
            raise ValueError(
                "promotion authorization action fence capability must be "
                f"{FAMILY_PROMOTION_CAPABILITY}"
            )
        if self.action_resource_fence.purpose != FAMILY_PROMOTION_CAPABILITY:
            raise ValueError(
                "promotion authorization action fence purpose must be "
                f"{FAMILY_PROMOTION_CAPABILITY}"
            )
        fence_subjects = {
            snapshot.subject_id
            for snapshot in self.action_resource_fence.approval_snapshots
        }
        if fence_subjects != set(self.required_subject_ids):
            raise ValueError(
                "promotion authorization approval snapshots must cover "
                "exactly the required subjects"
            )
        if self.action_resource_fence.action_resource_id != self.proposal_id:
            raise ValueError(
                "promotion authorization action_resource_id must equal "
                "the proposal id"
            )
        if self.action_resource_fence.proposal_id != self.proposal_id:
            raise ValueError(
                "promotion authorization fence proposal_id must equal the "
                "proposal id"
            )
        if (
            self.action_resource_fence.consent_snapshot_id
            != self.consent_snapshot_id
        ):
            raise ValueError(
                "promotion authorization consent snapshot id must match "
                "the action fence"
            )
        if (
            self.action_resource_fence.membership_snapshot_id
            != self.membership_snapshot_id
        ):
            raise ValueError(
                "promotion authorization membership snapshot id must match "
                "the action fence"
            )


class PromotionAuthorizationPort(Protocol):
    """Authoritative family-shared PROMOTION authorization (§6.2
    family_shared_memory_promotion / canonical contract sync).  The final
    promotion of a fully-confirmed proposal requires a FRESH promotion
    receipt bound to a fresh promotion fence (current family owner, full
    approval snapshot revisions, current relationships/consent).  A
    proposal receipt, a memory_capture receipt, a personal memory_promotion
    receipt or any private/personal receipt can NEVER stand in for it.
    Until the Policy producer implements the capability, an unconfigured
    port fails closed and no record is generated."""

    async def verify_promotion(
        self,
        *,
        proposal_id: str,
        family_space_id: str,
        required_subject_ids: tuple[str, ...],
        approval_revisions: tuple[ApprovalRevisionRef, ...],
        now: datetime,
    ) -> PromotionAuthorization | None:
        """Request a promotion authorization bound to the EXACT approval
        set.  The implementation must verify the proposal_id / family /
        required subjects / approval revisions field-by-field; an
        authorization issued for a different proposal, a missing or
        replaced approval receipt, or a duplicate/out-of-order revision
        fails closed."""
        ...


class PromotionActionFenceVerifier(Protocol):
    """PRODUCTION seam (main review): the Policy sub-agent's
    transaction-bound exact action verifier / commit API for
    ``family_shared_memory_promotion``.  The implementation reads the
    receipt AND the current evidence, runs the Policy V2 fence validators
    (``receipt_fence_valid`` / exact-evidence) and returns the verified
    outcome, or ``None`` when the authorization does not exist / does not
    verify at ``now``.

    DEPENDENCY POINT with the Policy sub-agent: this interface consumes
    the canonical generated ``PolicyActionResourceFence`` (never a locally
    copied fence); the Policy side is responsible for issuing the fresh
    promotion receipt bound to this exact action fence (proposal id +
    revision, required approval subjects + full approval snapshots,
    capture evidence, consent/membership id+revision+hash, generation /
    turn / tool epoch, validity window and canonical hash).  Until that
    interface is wired, production wiring stays UNCONFIGURED and the final
    promotion fails closed - no record is produced."""

    async def verify_promotion_action(
        self,
        *,
        proposal: SharedMemoryProposal,
        votes: tuple[ConfirmationVote, ...],
        authorization: PromotionAuthorization,
        now: datetime,
    ) -> VerifiedPolicyReceipt | None: ...


class InMemoryPromotionAuthorizationPort:
    """Development/test promotion authorization (test-is-the-verifier);
    nothing is accepted by default."""

    def __init__(self, lock: asyncio.Lock | None = None) -> None:
        self._authorized: dict[str, PromotionAuthorization] = {}
        #: Shared serialization lock (P0): revocation of a fresh promotion
        #: authorization serializes with the commit authority's lock.
        self.lock = lock or asyncio.Lock()

    async def register(
        self, authorization: PromotionAuthorization
    ) -> None:
        async with self.lock:
            self._authorized[authorization.proposal_id] = authorization

    async def revoke(self, proposal_id: str) -> None:
        async with self.lock:
            self._authorized.pop(proposal_id, None)

    async def verify_promotion(
        self,
        *,
        proposal_id: str,
        family_space_id: str,
        required_subject_ids: tuple[str, ...],
        approval_revisions: tuple[ApprovalRevisionRef, ...],
        now: datetime,
    ) -> PromotionAuthorization | None:
        authorization = self._authorized.get(proposal_id)
        if authorization is None:
            return None
        if authorization.proposal_id != proposal_id:
            return None
        if authorization.family_space_id != family_space_id:
            return None
        if authorization.required_subject_ids != tuple(required_subject_ids):
            return None
        if authorization.approval_revisions != tuple(approval_revisions):
            return None
        return authorization


class PromotionCommitAuthority(Protocol):
    """Transaction-bound promotion commit coordinator (P0): serializes the
    LAST current-evidence check with the store write under one lock /
    version CAS, so a consent revoke, membership revision bump or
    promotion-receipt revoke can NEVER race into the window between
    revalidation and the memory-record insert.  The exact approval-revision
    CAS in the store AND this current-authority CAS must BOTH pass before
    any promotion commits.

    If the current Policy/Consent authority cannot be checked in the same
    transaction as the PostgreSQL insert, production wiring MUST stay
    unconfigured and the finalizer fails closed - there is no
    "auditable window" in which a revoked authorization may still write."""

    async def commit(
        self,
        *,
        proposal: SharedMemoryProposal,
        votes: tuple[ConfirmationVote, ...],
        actor_subject_id: str,
        audit: tuple[MemoryAuditEvent, ...],
        now: datetime,
    ) -> tuple[bool, str | None]:
        """Re-check the CURRENT consent snapshot revision, membership
        revision and the fresh promotion authorization under the authority
        lock, then CAS-promote.  Returns ``(True, None)`` on commit,
        ``(False, reason)`` on any current-authority failure (the proposal
        is frozen with a rejection audit and NO record is produced)."""
        ...


class InMemoryPromotionCommitAuthority:
    """TEST / REFERENCE-ONLY commit authority (main review): it MUST NOT
    be the production App default wiring.  One asyncio lock is shared by
    every revoke/update and the commit itself, making the last check +
    write deterministic and serial.  Production wiring is the Policy
    sub-agent's ``PromotionActionFenceVerifier`` (transaction-bound exact
    action verifier); until that interface is wired, production family
    promotion stays fail-closed - the Postgres/Policy/Consent authority
    cannot join the insert transaction yet."""

    #: This coordinator is test/reference only: it verifies in the app
    #: process and commits through the store's OWN pool transaction - it
    #: is NOT a same-connection seam with the Policy/Consent authority.
    #: The service refuses to use it with a store that requires a
    #: same-transaction authority (PostgreSQL), so production wiring can
    #: never fake cross-database atomicity.
    same_transaction_seam: bool = False

    def __init__(
        self,
        *,
        store: MemoryStore,
        consent_verifier: ConsentSnapshotVerifier,
        membership_verifier: FamilyMembershipVerifier,
        promotion_authorizer: PromotionAuthorizationPort,
        receipt_verifier: PolicyReceiptVerifier,
        promotion_action_verifier: PromotionActionFenceVerifier | None = None,
        lock: asyncio.Lock | None = None,
        in_commit_gate: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._store = store
        self._consent_verifier = consent_verifier
        self._membership_verifier = membership_verifier
        self._promotion_authorizer = promotion_authorizer
        self._receipt_verifier = receipt_verifier
        self._promotion_action_verifier = promotion_action_verifier
        self._lock = lock or asyncio.Lock()
        self._in_commit_gate = in_commit_gate

    async def register_consent(
        self, snapshot_id: str, covers_subjects: frozenset[str]
    ) -> None:
        # The verifier mutation itself acquires the SHARED lock (P0): a
        # direct ``consent_verifier.register`` outside the authority
        # serializes against the commit the same way.
        await cast(Any, self._consent_verifier).register(
            snapshot_id, covers_subjects
        )

    async def register_membership(
        self, *, family_space_id: str, binding_version: int, subjects: frozenset[str]
    ) -> None:
        await cast(Any, self._membership_verifier).register(
            family_space_id=family_space_id,
            binding_version=binding_version,
            subjects=subjects,
        )

    async def register_promotion(self, authorization: PromotionAuthorization) -> None:
        await cast(Any, self._promotion_authorizer).register(authorization)

    async def revoke_consent(self, snapshot_id: str) -> None:
        await cast(Any, self._consent_verifier).revoke(snapshot_id)

    async def revoke_membership(
        self, family_space_id: str, *, binding_version: int
    ) -> None:
        await cast(Any, self._membership_verifier).revoke(
            family_space_id=family_space_id, binding_version=binding_version
        )

    async def revoke_promotion(self, proposal_id: str) -> None:
        await cast(Any, self._promotion_authorizer).revoke(proposal_id)

    async def revoke_receipt(self, receipt_id: str) -> None:
        """Revoke the promotion RECEIPT itself (the in-lock receipt
        revalidation must then fail closed even when the authorization map
        still holds)."""
        await cast(Any, self._receipt_verifier).revoke(receipt_id)

    def _assert_shared_lock(self) -> None:
        """Fail closed when a verifier was constructed WITHOUT the same
        lock as the commit authority: a mutation on a different lock could
        bypass the serialization (P0)."""
        for verifier in (
            self._consent_verifier,
            self._membership_verifier,
            self._promotion_authorizer,
            self._receipt_verifier,
        ):
            if getattr(verifier, "lock", None) is not self._lock:
                raise RuntimeError(
                    "commit authority and verifier do not share the same "
                    "lock; a bypass mutation could race into the commit "
                    "window - wiring must fail closed"
                )

    async def commit(
        self,
        *,
        proposal: SharedMemoryProposal,
        votes: tuple[ConfirmationVote, ...],
        actor_subject_id: str,
        audit: tuple[MemoryAuditEvent, ...],
        now: datetime,
    ) -> tuple[bool, str | None]:
        self._assert_shared_lock()
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
        async with self._lock:
            # 1) CURRENT consent snapshot revision.
            consent_ok = await self._consent_verifier.verify_consent(
                proposal.consent_snapshot_id,
                scope="memory",
                covers_subjects=required_subjects,
                now=now,
            )
            if not consent_ok:
                await self._freeze(proposal, "consent revoked before commit", now)
                return False, "consent revoked before commit"
            # 2) CURRENT membership revision.
            membership_ok = await self._membership_verifier.verify_membership(
                family_space_id=proposal.family_space_id,
                subject_ids=required_subjects,
                binding_version=proposal.binding_version,
                now=now,
            )
            if not membership_ok:
                await self._freeze(
                    proposal, "membership revoked before commit", now
                )
                return False, "membership revoked before commit"
            # 3) FRESH promotion authorization still valid under the same
            #    lock.
            authorization = await self._promotion_authorizer.verify_promotion(
                proposal_id=proposal.proposal_id,
                family_space_id=proposal.family_space_id,
                required_subject_ids=required_subjects,
                approval_revisions=approval_revisions,
                now=now,
            )
            if authorization is None:
                await self._freeze(
                    proposal, "promotion authorization revoked before commit", now
                )
                return False, "promotion authorization revoked before commit"
            # 3b) PRODUCTION SEAM: the Policy sub-agent's transaction-bound
            #     exact action verifier (unconfigured -> the test/reference
            #     field-check below is the only gate; production wiring
            #     must provide it or stay fail-closed).
            if self._promotion_action_verifier is not None:
                verified_action = (
                    await self._promotion_action_verifier.verify_promotion_action(
                        proposal=proposal,
                        votes=votes,
                        authorization=authorization,
                        now=now,
                    )
                )
                if (
                    verified_action is None
                    or not verified_action.receipt_fence_valid
                    or not verified_action.exact_evidence_valid
                ):
                    await self._freeze(
                        proposal,
                        "promotion action verifier rejected the authorization "
                        "before commit",
                        now,
                    )
                    return (
                        False,
                        "promotion action verifier rejected the authorization "
                        "before commit",
                    )
            # 3c) Canonical PolicyActionResourceFence field-by-field match
            #     against the proposal's AUTHORITATIVE persisted state: a
            #     receipt/fence that references a different proposal id /
            #     revision, a different consent snapshot id (even with the
            #     same revision number), a different membership hash, a
            #     wrong approval snapshot set/order, wrong capture evidence
            #     or a drifted generation/turn/tool epoch fails closed.
            fence_ok, fence_reason = self._check_action_fence(
                proposal, votes, authorization, now
            )
            if not fence_ok:
                await self._freeze(proposal, fence_reason or "fence mismatch", now)
                return False, fence_reason or "fence mismatch"
            # 4) In-lock RECEIPT revalidation (P0): the authorization map is
            #    only an index - the authoritative PolicyReceiptV2 must
            #    still verify at ``now`` (receipt_fence_valid /
            #    exact_evidence_valid) and field-match capability / purpose /
            #    resource owner / every fence identity / expiry.  A revoked
            #    or expired receipt fails closed even when the authorization
            #    map still holds.
            verifier = self._receipt_verifier
            verified = await verifier.verify(
                authorization.receipt_id,
                fence=authorization.fence,
                actor_subject_id=authorization.fence.actor_subject_id,
                capability=FAMILY_PROMOTION_CAPABILITY,
                now=now,
            )
            if (
                verified is None
                or not verified.receipt_fence_valid
                or not verified.exact_evidence_valid
            ):
                await self._freeze(
                    proposal, "promotion receipt revoked/invalid before commit", now
                )
                return False, "promotion receipt revoked/invalid before commit"
            family_obligations = {
                OBLIGATION_REQUIRE_SUBJECT_APPROVAL,
                OBLIGATION_WRITE_POLICY_RECEIPT,
            }
            if not family_obligations.issubset(
                {obligation.code for obligation in verified.receipt.obligations}
            ):
                await self._freeze(
                    proposal,
                    "promotion receipt missing family obligations before commit",
                    now,
                )
                return (
                    False,
                    "promotion receipt missing family obligations before commit",
                )
            if not verify_receipt_against_fence(
                verified.receipt,
                fence=authorization.fence,
                actor_subject_id=authorization.fence.actor_subject_id,
                capability=FAMILY_PROMOTION_CAPABILITY,
                purpose=FAMILY_PROMOTION_CAPABILITY,
                now=now,
                expected_resource_owner_id=proposal.family_space_id,
                check_expiry=True,
            ):
                await self._freeze(
                    proposal, "promotion receipt mismatch/expired before commit", now
                )
                return False, "promotion receipt mismatch/expired before commit"
            # 5) Current-authority revision CAS (REQUIRED positive tokens,
            #    unconditional exact compare): a re-issued consent or
            #    membership snapshot (verify still passes but the revision
            #    moved) fails closed too.
            current_consent_revision = self._consent_verifier.current_revision(
                proposal.consent_snapshot_id
            )
            if current_consent_revision != authorization.consent_revision:
                await self._freeze(
                    proposal, "consent revision changed before commit", now
                )
                return False, "consent revision changed before commit"
            current_membership_revision = (
                self._membership_verifier.current_revision(
                    proposal.family_space_id, proposal.binding_version
                )
            )
            if current_membership_revision != authorization.membership_revision:
                await self._freeze(
                    proposal, "membership revision changed before commit", now
                )
                return False, "membership revision changed before commit"
            # 6) Exact approval-revision CAS + commit in the store.  The
            #    in-commit gate (deterministic concurrency tests only)
            #    pauses AFTER every current-authority check while the lock
            #    is held: a revoker task waiting on the same lock can only
            #    apply AFTER this commit - there is no intermediate window.
            if self._in_commit_gate is not None:
                await self._in_commit_gate()
            from dataclasses import replace as _replace

            committed_audit = tuple(
                _replace(
                    event,
                    payload={
                        **event.payload,
                        "promotion_receipt_id": authorization.receipt_id,
                    },
                )
                for event in audit
            )
            committed = await self._store.finalize_promotion_atomically(
                proposal.proposal_id,
                promotion_receipt_id=authorization.receipt_id,
                promotion_fence_context_hash=authorization.fence.fingerprint(),
                required_subject_ids=required_subjects,
                approval_revisions=approval_revisions,
                actor_subject_id=actor_subject_id,
                family_space_id=proposal.family_space_id,
                audit=committed_audit,
                now=now,
            )
            if not committed:
                return False, "approval CAS or concurrent finalizer lost"
            return True, None

    async def _freeze(
        self, proposal: SharedMemoryProposal, reason: str, now: datetime
    ) -> None:
        from services.memory_scope.domain import MemoryAuditEvent, MemoryOutboxEvent

        await self._store.freeze_proposal_atomically(
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
                    payload={"proposal_id": proposal.proposal_id},
                    created_at=now,
                ),
            ),
            now=now,
        )

    def _check_action_fence(
        self,
        proposal: SharedMemoryProposal,
        votes: tuple[ConfirmationVote, ...],
        authorization: PromotionAuthorization,
        now: datetime,
    ) -> tuple[bool, str | None]:
        """Canonical ``PolicyActionResourceFence`` field-by-field match
        against the proposal's AUTHORITATIVE persisted state (main
        review).  The fence hashes are STRICTLY recomputed via
        ``services.policy.action_fence.verify_action_resource_fence`` (a
        forged ``action_evidence_hash`` / ``canonical_hash`` on an
        otherwise well-formed generated model is rejected), then every
        structural field is matched.  Every mismatch returns
        ``(False, reason)``; the caller freezes the proposal and produces
        NO record."""
        from services.policy.action_fence import verify_action_resource_fence

        fence = authorization.action_resource_fence
        if not verify_action_resource_fence(fence):
            return False, "promotion fence canonical hashes are invalid"
        required_subjects = tuple(
            dict.fromkeys((proposal.proposer_subject_id, *proposal.co_subject_ids))
        )
        if fence.action_resource_id != proposal.proposal_id:
            return False, "promotion fence action_resource_id mismatch"
        if fence.proposal_id != proposal.proposal_id:
            return False, "promotion fence proposal_id mismatch"
        if fence.action_revision != proposal.proposal_revision:
            return False, "promotion fence action_revision mismatch"
        if fence.proposal_revision != proposal.proposal_revision:
            return False, "promotion fence proposal_revision mismatch"
        if fence.family_space_id != proposal.family_space_id:
            return False, "promotion fence family_space_id mismatch"
        if fence.family_owner_subject_id != proposal.proposer_subject_id:
            return False, "promotion fence family_owner_subject_id mismatch"
        if set(fence.required_approval_subject_ids) != set(required_subjects):
            return False, "promotion fence required_approval_subject_ids mismatch"
        # Canonical invariant: the PROMOTION action fence carries NO
        # capture evidence ids/hash (the proposal fence does; the capture
        # evidence set is bound there and the promotion receipt binds the
        # proposal revision that carries it).
        if fence.capture_evidence_ids:
            return False, "promotion fence capture_evidence_ids must be empty"
        if fence.capture_evidence_hash is not None:
            return False, "promotion fence capture_evidence_hash must be null"
        if fence.consent_snapshot_id != proposal.consent_snapshot_id:
            return False, "promotion fence consent_snapshot_id mismatch"
        if fence.consent_snapshot_revision != proposal.consent_snapshot_revision:
            return False, "promotion fence consent_snapshot_revision mismatch"
        if fence.consent_snapshot_hash != proposal.consent_snapshot_hash:
            return False, "promotion fence consent_snapshot_hash mismatch"
        if fence.membership_snapshot_id != proposal.membership_snapshot_id:
            return False, "promotion fence membership_snapshot_id mismatch"
        if (
            fence.membership_snapshot_revision
            != proposal.membership_snapshot_revision
        ):
            return False, "promotion fence membership_snapshot_revision mismatch"
        if fence.membership_snapshot_hash != proposal.membership_snapshot_hash:
            return False, "promotion fence membership_snapshot_hash mismatch"
        if fence.generation_id != proposal.generation:
            return False, "promotion fence generation_id mismatch"
        if fence.turn_id != (proposal.turn_id or 0):
            return False, "promotion fence turn_id mismatch"
        if fence.tool_epoch != proposal.tool_epoch:
            return False, "promotion fence tool_epoch mismatch"
        if not (fence.issued_at <= now < fence.valid_until):
            return False, "promotion fence validity window expired"
        # Approval snapshots must record-set-equal the persisted votes on
        # subject / snapshot id / revision / hash (a replaced approval
        # receipt, a wrong snapshot set or an out-of-order/duplicate set
        # fails closed).  Count and uniqueness are checked EXPLICITLY so a
        # duplicate persisted vote can never be swallowed by set
        # comparison, and the snapshot subjects must exactly cover the
        # required tuple.
        confirm_votes = [vote for vote in votes if vote.decision == "confirm"]
        if len(confirm_votes) != len(fence.approval_snapshots):
            return False, "promotion fence approval_snapshots count mismatch"
        if len({vote.subject_id for vote in confirm_votes}) != len(confirm_votes):
            return False, "duplicate confirm vote subjects"
        if (
            len({snapshot.subject_id for snapshot in fence.approval_snapshots})
            != len(fence.approval_snapshots)
        ):
            return False, "duplicate approval snapshot subjects"
        if (
            {snapshot.subject_id for snapshot in fence.approval_snapshots}
            != set(required_subjects)
        ):
            return False, "approval snapshots do not exactly cover required subjects"
        fence_snapshots = {
            (
                snapshot.subject_id,
                snapshot.snapshot_id,
                snapshot.revision,
                snapshot.canonical_hash,
            )
            for snapshot in fence.approval_snapshots
        }
        vote_snapshots = {
            (
                vote.subject_id,
                vote.approval_snapshot_id,
                vote.approval_snapshot_revision,
                vote.approval_snapshot_hash,
            )
            for vote in votes
            if vote.decision == "confirm"
        }
        if fence_snapshots != vote_snapshots:
            return False, "promotion fence approval_snapshots mismatch"
        return True, None


class InMemoryFamilyMembershipVerifier:
    """Development/test membership store (fourth review)."""

    def __init__(self, lock: asyncio.Lock | None = None) -> None:
        self._members: dict[tuple[str, int], frozenset[str]] = {}
        self._revisions: dict[tuple[str, int], int] = {}
        self._hashes: dict[tuple[str, int], str] = {}
        #: Shared serialization lock (P0): mutations serialize with the
        #: promotion commit authority.
        self.lock = lock or asyncio.Lock()

    async def register(
        self,
        *,
        family_space_id: str,
        binding_version: int,
        subjects: frozenset[str],
        snapshot_hash: str | None = None,
    ) -> None:
        async with self.lock:
            key = (family_space_id, binding_version)
            self._members[key] = subjects
            self._revisions[key] = self._revisions.get(key, 0) + 1
            # Deterministic test hash unless the caller pins an explicit
            # canonical hash (the production verifier computes it from the
            # snapshot bytes).
            self._hashes[key] = snapshot_hash or hashlib.sha256(
                f"membership:{family_space_id}:{binding_version}".encode()
            ).hexdigest()

    async def revoke(self, *, family_space_id: str, binding_version: int) -> None:
        async with self.lock:
            key = (family_space_id, binding_version)
            self._members.pop(key, None)
            self._revisions.pop(key, None)
            self._hashes.pop(key, None)

    def current_revision(
        self, family_space_id: str, binding_version: int
    ) -> int | None:
        return self._revisions.get((family_space_id, binding_version))

    def current_hash(
        self, family_space_id: str, binding_version: int
    ) -> str | None:
        return self._hashes.get((family_space_id, binding_version))

    async def verify_membership(
        self,
        *,
        family_space_id: str,
        subject_ids: tuple[str, ...],
        binding_version: int,
        now: datetime,
    ) -> bool:
        members = self._members.get((family_space_id, binding_version))
        if members is None:
            return False
        return set(subject_ids).issubset(members)


class InMemoryConsentSnapshotVerifier:
    """Development/test consent snapshot store (fourth review)."""

    def __init__(self, lock: asyncio.Lock | None = None) -> None:
        self._snapshots: dict[str, frozenset[str]] = {}
        self._revisions: dict[str, int] = {}
        self._hashes: dict[str, str] = {}
        #: Shared serialization lock (P0): mutations serialize with the
        #: promotion commit authority.
        self.lock = lock or asyncio.Lock()

    async def register(
        self,
        snapshot_id: str,
        covers_subjects: frozenset[str],
        *,
        snapshot_hash: str | None = None,
    ) -> None:
        async with self.lock:
            self._snapshots[snapshot_id] = covers_subjects
            self._revisions[snapshot_id] = self._revisions.get(snapshot_id, 0) + 1
            self._hashes[snapshot_id] = snapshot_hash or hashlib.sha256(
                f"consent:{snapshot_id}".encode()
            ).hexdigest()

    async def clear(self) -> None:
        async with self.lock:
            self._snapshots.clear()
            self._revisions.clear()
            self._hashes.clear()

    async def revoke(self, snapshot_id: str) -> None:
        async with self.lock:
            self._snapshots.pop(snapshot_id, None)
            self._revisions.pop(snapshot_id, None)
            self._hashes.pop(snapshot_id, None)

    def current_revision(self, snapshot_id: str) -> int | None:
        return self._revisions.get(snapshot_id)

    def current_hash(self, snapshot_id: str) -> str | None:
        return self._hashes.get(snapshot_id)

    async def verify_consent(
        self,
        snapshot_id: str,
        *,
        scope: str,
        covers_subjects: tuple[str, ...],
        now: datetime,
    ) -> bool:
        covered = self._snapshots.get(snapshot_id)
        if covered is None:
            return False
        return set(covers_subjects).issubset(covered)


class InMemoryRelationshipGrantResolver:
    """Development/test grant resolver (fourth review)."""

    def __init__(self) -> None:
        self._guardian: dict[str, frozenset[str]] = {}
        self._legacy: dict[str, frozenset[str]] = {}

    def register_guardian(self, actor_subject_id: str, wards: frozenset[str]) -> None:
        self._guardian[actor_subject_id] = wards

    def register_legacy_grant(
        self, actor_subject_id: str, owners: frozenset[str]
    ) -> None:
        self._legacy[actor_subject_id] = owners

    async def guardian_of(
        self, *, actor_subject_id: str, now: datetime
    ) -> frozenset[str]:
        return self._guardian.get(actor_subject_id, frozenset())

    async def legacy_grants_for(
        self, *, actor_subject_id: str, now: datetime
    ) -> frozenset[str]:
        return self._legacy.get(actor_subject_id, frozenset())


@dataclass(frozen=True, slots=True)
class MemoryAuthoritySnapshot:
    """One immutable authority view for a memory HTTP operation.

    Session Runtime supplies the signed profile and mutable generation fence
    from one read transaction. Identity then supplies the exact binding
    manifest version referenced by that profile. Routes consume this object
    once; they never stitch together subject, family, consent or fence fields
    from multiple reads or from client claims.
    """

    fence: WriteFence
    active_subject_id: str
    subject_category: SubjectCategoryValue
    age_band: AgeBandValue
    speaker_state: SpeakerStateValue
    speaker_confidence: float | None
    registered: bool
    actor_binding_role: BindingRoleValue
    actor_binding_roles: tuple[BindingRoleValue, ...]
    family_space_id: str | None
    consent_snapshot_id: str | None
    profile_revision: int
    generation_id: int
    turn_id: int
    tool_epoch: int
    policy_bundle_version: str
    policy_receipt_ids: tuple[str, ...]
    service_mode: ServiceModeValue


class MemorySessionAuthorityPort(Protocol):
    """Current Session + Identity authority for memory operations.

    Implementations must return one internally consistent snapshot or fail
    closed. ``None`` means that no RLS-visible current voice session exists;
    authority outages and revision mismatches should raise.
    """

    async def current(
        self,
        *,
        actor_id: str,
        session_id: str,
        now: datetime,
    ) -> MemoryAuthoritySnapshot | None: ...

    async def current_consent_snapshot_id(
        self,
        *,
        actor_subject_id: str,
        now: datetime,
    ) -> str | None:
        """Authoritative current consent snapshot id for the session
        subject (wiring P0): the route/context uses it; the body cannot
        pick a different consent snapshot."""
        ...


class MemoryOutboxDispatcherPort(Protocol):
    """Transactional-outbox dispatcher (wiring C8): ONLY a successful
    dispatch acknowledges the event; failures/retries keep the row pending
    with lease/reclaim semantics.  An unconfigured dispatcher makes the
    memory worker NOT ready (fail closed - events are never dropped)."""

    async def dispatch(self, event: MemoryOutboxEvent) -> bool: ...


class MemoryStore(Protocol):
    """Persistence contract used by ``MemoryScopeService``.

    Records and status transitions are append-only: rows are never updated
    in place and deletion is forbidden; withdrawal is a status event that
    makes the record invisible to every read path.
    """

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    # -- records ---------------------------------------------------------

    async def persist_record(
        self,
        record: MemoryRecord,
        *,
        actor_family_space_id: str | None,
        status_events: tuple[MemoryRecordStatusEvent, ...] = (),
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
        connection: object | None = None,
    ) -> None: ...

    async def get_record(
        self,
        record_id: str,
        *,
        actor_subject_id: str,
        actor_family_space_id: str | None = None,
        grant_owner_id: str | None = None,
        grant_scope: str | None = None,
    ) -> MemoryRecord | None: ...

    async def list_records_for_subject(
        self,
        subject_id: str,
        scopes: tuple[MemoryScope, ...] | None = None,
        *,
        actor_subject_id: str,
        actor_family_space_id: str | None = None,
        grant_owner_id: str | None = None,
        grant_scope: str | None = None,
        include_revoked: bool = False,
    ) -> tuple[MemoryRecord, ...]: ...

    async def list_records_in_family(
        self,
        family_space_id: str,
        subject_id: str,
        scopes: tuple[MemoryScope, ...] | None = None,
        *,
        actor_subject_id: str,
        actor_family_space_id: str | None,
        include_revoked: bool = False,
    ) -> tuple[MemoryRecord, ...]: ...

    async def get_status_events(
        self, record_id: str, *, actor_subject_id: str
    ) -> tuple[MemoryRecordStatusEvent, ...]: ...

    # -- shared memory proposals ------------------------------------------

    async def persist_proposal(
        self,
        proposal: SharedMemoryProposal,
        *,
        actor_family_space_id: str | None,
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
    ) -> None: ...

    async def get_proposal(
        self,
        proposal_id: str,
        *,
        actor_subject_id: str,
        actor_family_space_id: str,
    ) -> SharedMemoryProposal | None: ...

    async def update_proposal_status(
        self,
        proposal_id: str,
        status: ProposalStatus,
        *,
        actor_subject_id: str,
        resolved_at: datetime | None = None,
    ) -> None: ...

    async def persist_vote(
        self,
        vote: ConfirmationVote,
        *,
        proposal_status: ProposalStatus | None = None,
        resolved_at: datetime | None = None,
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
        promote_record: MemoryRecord | None = None,
        promote_status_events: tuple[MemoryRecordStatusEvent, ...] = (),
    ) -> None: ...

    async def vote_and_transition(
        self,
        vote: ConfirmationVote,
        *,
        actor_family_space_id: str | None,
        audit: tuple[MemoryAuditEvent, ...],
        now: datetime,
    ) -> str:
        """Atomic vote + authoritative transition (P0-F): inside ONE
        transaction the proposal is locked, the vote is recorded, and the
        FULL authoritative vote set is read to decide the transition -
        any objection freezes, the FULL confirm set moves the proposal to
        ``approvals_complete`` (the vote-acceptance transaction NEVER
        promotes - the separate promotion finalizer performs the CAS
        ``approvals_complete -> promoted`` with a DISTINCT fresh promotion
        receipt, at most once), otherwise the proposal stays ``pending``.
        Terminal outbox events (stable ids) are written only when this
        transaction performs a real state jump.  Returns the resulting
        status: ``pending`` / ``frozen`` / ``approvals_complete``, or
        ``terminal`` when the proposal state does not accept this vote
        (confirm requires ``pending``; object requires ``pending`` or
        ``approvals_complete``)."""
        ...

    async def freeze_proposal_atomically(
        self,
        proposal_id: str,
        *,
        actor_subject_id: str,
        family_space_id: str,
        reason: str,
        audit: tuple[MemoryAuditEvent, ...],
        outbox: tuple[MemoryOutboxEvent, ...],
        now: datetime,
    ) -> bool:
        """Fail-closed freeze (main architecture review): when the final
        confirmation's authoritative re-verification fails (receipt
        revoked/expired, consent replaced, membership lost, fence changed),
        the pending proposal is atomically moved to ``frozen`` with the
        stable audit/outbox events and NEVER produces a record.  Returns
        ``False`` when the proposal was already terminal (idempotent
        no-op)."""
        ...

    async def finalize_promotion_atomically(
        self,
        proposal_id: str,
        *,
        promotion_receipt_id: str,
        promotion_fence_context_hash: str,
        required_subject_ids: tuple[str, ...],
        approval_revisions: tuple[ApprovalRevisionRef, ...],
        actor_subject_id: str,
        family_space_id: str,
        audit: tuple[MemoryAuditEvent, ...],
        now: datetime,
    ) -> bool:
        """Promotion finalizer CAS (THREE authority actions / §6.2 Memory
        Compiler): inside ONE transaction the proposal AND its append-only
        vote set are re-locked; the promotion only commits when the
        proposal is still ``pending``, every required confirm is present
        with its EXACT persisted approval_receipt_id matching the expected
        revisions (any drift / missing / empty approval fails closed and
        produces NO record), and no objection exists.  The fresh
        family_shared_memory_promotion receipt id, its fence hash and the
        exact approval evidence refs are persisted WITH the promoted
        record - audit/outbox content is derived from the transaction's
        authoritative rows.  Concurrent finalizers produce at most one
        record (unique partial index on shared_proposal_id).  Returns
        ``True`` when THIS call promoted, ``False`` otherwise."""
        ...

    async def list_votes(
        self, proposal_id: str, *, actor_subject_id: str
    ) -> tuple[ConfirmationVote, ...]: ...

    async def list_proposals_for_subject(
        self,
        subject_id: str,
        statuses: tuple[ProposalStatus, ...] | None = None,
        *,
        actor_subject_id: str,
        actor_family_space_id: str,
    ) -> tuple[SharedMemoryProposal, ...]: ...

    async def persist_withdrawal(
        self,
        proposal_id: str,
        *,
        actor_subject_id: str,
        record_id: str | None = None,
        status_events: tuple[MemoryRecordStatusEvent, ...] = (),
        outbox: tuple[MemoryOutboxEvent, ...] = (),
        audit: tuple[MemoryAuditEvent, ...] = (),
    ) -> None: ...

    # -- outbox / audit ---------------------------------------------------

    async def append_outbox(self, event: MemoryOutboxEvent) -> None: ...

    async def append_audit(self, event: MemoryAuditEvent) -> None: ...

    async def list_pending_outbox(self, limit: int = 100) -> tuple[MemoryOutboxEvent, ...]: ...

    async def mark_outbox_processed(self, outbox_id: str) -> None: ...
