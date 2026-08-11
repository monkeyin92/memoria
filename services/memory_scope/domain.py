"""Pure contracts for the Memory Scope and family shared-memory domain.

Implements the memory-side of the 2026-08-09 multi-subject remediation plan:

- the five memory spaces of section 6.1 (``session_ephemeral`` /
  ``personal_private`` / ``guardian_summary`` / ``family_shared`` /
  ``legacy_archive``) with ``unknown`` as the fail-closed default;
- section 6.2: every durable write must carry subject resolution plus a
  policy receipt, evidence ids and a consent snapshot;
- section 6.3: the forbidden writes (fixed device->user mapping, parent
  reading verbatim child chat, speaker-failure falling back to the adult
  admin, LLM "worth remembering" as fact, single-party family stories,
  persona settings written into the digital self);
- PR-14 / section 5.4 case 3: pending shared memory lifecycle
  (propose -> per co-subject confirm -> all confirmed or any objection
  freezes -> withdraw makes retrieval invisible; family admin never has
  unlimited read);
- PR-12 / section 11.5: durable writes are fenced by
  ``session_id + epoch + binding + runtime_profile + policy receipt`` so an
  unknown / unconfirmed / cross-subject / late / revoked / expired write
  fails closed.

Enum values are NOT redefined here: ``MemoryScope``, ``PolicyObligation``,
``PolicyEffect``, ``SpeakerState``, ``SubjectCategory``, ``BindingRole`` and
``RelationshipStatus`` are imported from the single canonical generated
contract (ADR-0033 / PR-01) so no parallel enum can drift.

This module has no framework or persistence dependencies so the same rules
are reused by the resolver, the in-memory/SQLite/PostgreSQL adapters and
the service facade.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from packages.contracts.generated.python.multi_subject_contracts import (
    MEMORY_SCOPE_DEFAULT,
    BindingRole,
    BindingRoleValue,
    MemoryScope,
    MemoryScopeValue,
    PolicyEffect,
    PolicyEffectValue,
    PolicyObligation,
    RelationshipStatus,
    SpeakerState,
    SpeakerStateValue,
    SubjectCategory,
    SubjectCategoryValue,
)

from services.policy.receipts import PolicyReceiptV2

#: Canonical enums are re-exported unchanged so every caller of this package
#: consumes exactly the generated ADR-0033 contract (never a local copy).
MemoryScope = MemoryScope
PolicyObligation = PolicyObligation
PolicyEffect = PolicyEffect
SpeakerState = SpeakerState
SubjectCategory = SubjectCategory
BindingRole = BindingRole
RelationshipStatus = RelationshipStatus

#: Scopes whose records are durable and therefore need the full sensitive
#: source set (subject, owner, evidence, receipt, consent snapshot).
DURABLE_SCOPES: frozenset[MemoryScope] = frozenset(
    {
        MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
        MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
        MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
        MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,
    }
)

type MemoryStatus = Literal["candidate", "confirmed", "disputed", "revoked"]
type ProposalStatus = Literal[
    "pending", "approvals_complete", "promoted", "frozen", "withdrawn"
]
type VoteDecision = Literal["confirm", "object"]
type RetentionPolicy = Literal["session_only", "ttl", "indefinite"]

DEFAULT_RETENTION: RetentionPolicy = "indefinite"


#: Canonical obligation members used by this domain (section 5.3 subset).
#: All values are the canonical UPPER_SNAKE strings; no lowercase parallel
#: contract exists anywhere in this package.
OBLIGATION_DO_NOT_PERSIST = PolicyObligation.POLICY_OBLIGATION_DO_NOT_PERSIST
OBLIGATION_PERSIST_AGGREGATE_ONLY = (
    PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY
)
OBLIGATION_RETENTION_TTL = PolicyObligation.POLICY_OBLIGATION_RETENTION_TTL
OBLIGATION_REQUIRE_SPEAKER_CONFIRMATION = (
    PolicyObligation.POLICY_OBLIGATION_REQUIRE_SPEAKER_CONFIRMATION
)
OBLIGATION_REQUIRE_SUBJECT_APPROVAL = (
    PolicyObligation.POLICY_OBLIGATION_REQUIRE_SUBJECT_APPROVAL
)
OBLIGATION_WRITE_POLICY_RECEIPT = (
    PolicyObligation.POLICY_OBLIGATION_WRITE_POLICY_RECEIPT
)

#: Dedicated canonical capabilities for the TWO-STAGE family flow (§6.2 /
#: canonical contract sync).  The contract agent is freezing
#: ``family_shared_memory_proposal`` and ``family_shared_memory_promotion``
#: as independent capability/purpose values (plus ``memory_promotion`` for
#: the personal compiler).  Until the generated contracts + Policy
#: producer ship them, the V2 wire cannot carry them, so the authorization
#: ports fail closed - the memory_capture / memory_promotion paths are
#: NEVER borrowed for family proposal or promotion.
FAMILY_PROPOSAL_CAPABILITY = "family_shared_memory_proposal"
FAMILY_APPROVAL_CAPABILITY = "family_shared_memory_approval"
FAMILY_PROMOTION_CAPABILITY = "family_shared_memory_promotion"


class MemoryScopeError(RuntimeError):
    """Base error for the memory scope domain."""


class MemoryWriteRejected(MemoryScopeError):
    """A durable write was rejected (fail closed); carries the reason."""


class UnresolvedSubjectError(MemoryWriteRejected):
    """No confirmed subject for a private/shared/guardian/legacy write."""


class MissingSensitiveSourceError(MemoryWriteRejected):
    """A durable write lacks evidence ids, policy receipt or consent snapshot."""


class MemoryNotFoundError(MemoryScopeError):
    """No such record or proposal."""


class NotAuthorizedError(MemoryScopeError):
    """The actor is not allowed to read or mutate this memory."""


class CrossFamilyAccessError(NotAuthorizedError):
    """A member of one family space tried to access another family's memory."""


class ProposalStateError(MemoryScopeError):
    """The shared-memory proposal is not in the required state."""


class AlreadyVotedError(ProposalStateError):
    """A co-subject tried to vote twice on the same proposal."""


class RawTranscriptDeniedError(NotAuthorizedError):
    """Guardians receive aggregate summaries only, never verbatim chat."""


class WriteFenceError(MemoryScopeError):
    """A durable write fence (session/binding/profile/epoch/receipt) failed."""


class WriteFenceMissingError(WriteFenceError):
    """A durable write arrived without any session/binding/profile fence."""


class WriteFenceMismatchError(WriteFenceError):
    """The policy receipt was issued for a different fence (epoch/binding/
    profile/session changed, or a late receipt is being reused)."""


class WriteFenceExpiredError(WriteFenceError):
    """The binding/profile the receipt was issued for has expired."""


class ActorNotAuthorizedError(NotAuthorizedError):
    """The writing actor is not the subject (or the fenced binding role does
    not authorize the write)."""


class ReceiptNotVerifiedError(WriteFenceError):
    """The policy receipt could not be verified against an authoritative
    receipt store (missing, mismatched actor/subject/fence, wrong capability,
    stale epoch or expired)."""


@dataclass(frozen=True, slots=True)
class SubjectContext:
    """Who is (probably) speaking right now, per section 5.3.

    ``registered`` distinguishes a known account subject from an
    unregistered guest; ``guardian_relationship_active`` is the relationship
    snapshot flag required before any guardian summary is allowed.
    """

    active_subject_id: str | None
    subject_category: SubjectCategoryValue = SubjectCategory.SUBJECT_CATEGORY_UNKNOWN.value
    speaker_state: SpeakerStateValue = SpeakerState.SPEAKER_STATE_UNKNOWN.value
    speaker_confidence: float | None = None
    registered: bool = True
    family_space_id: str | None = None
    guardian_relationship_active: bool = False


@dataclass(frozen=True, slots=True)
class WriteFence:
    """Session/binding/profile fence every durable write must carry
    (PR-12 / section 11.5).

    ``fingerprint`` binds the policy receipt to one concrete session epoch,
    binding role and runtime profile; any change (speaker switch bumped the
    epoch, the binding was replaced, the profile rotated) invalidates the
    receipt so late or cross-subject writes fail closed.
    """

    session_id: str
    epoch: int
    binding_id: str
    binding_role: BindingRoleValue
    runtime_profile_id: str
    #: The writing actor (who calls the write API).  Bound into the
    #: fingerprint so a receipt issued for another actor cannot be reused.
    actor_subject_id: str
    #: The confirmed active subject the memory belongs to.
    active_subject_id: str
    #: Binding manifest version (PR-03 / canonical BindingManifest:
    #: integer >= 1, REQUIRED).  A replaced binding rotates it.  Omitting it
    #: must fail construction - an unknown binding is never silently treated
    #: as v1.  Validated in ``__post_init__``.
    binding_version: int
    #: Device the session runs on (P0 receipt contract); bound into the
    #: fingerprint so a receipt issued for another device is rejected.
    device_id: str = ""
    #: Subject revision at write time (P0 receipt contract).
    subject_revision: int = 0
    #: Family space the actor's binding is scoped to (P1: the authoritative
    #: actor family scope, never the row's self-declared family).
    family_space_id: str | None = None
    generation_id: str | None = None
    turn_id: int | None = None
    valid_until: datetime | None = None

    def fingerprint(self) -> str:
        """Stable fingerprint of the identity-relevant fence fields."""
        digest = hashlib.sha256()
        digest.update(self.session_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(self.epoch).encode("ascii"))
        digest.update(b"\x00")
        digest.update(self.binding_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.binding_role.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.runtime_profile_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.actor_subject_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.active_subject_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(self.binding_version).encode("ascii"))
        digest.update(b"\x00")
        digest.update(self.device_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(self.subject_revision).encode("ascii"))
        digest.update(b"\x00")
        digest.update((self.family_space_id or "").encode("utf-8"))
        digest.update(b"\x00")
        digest.update((self.generation_id or "").encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(self.turn_id or "").encode("ascii"))
        digest.update(b"\x00")
        digest.update(
            (self.valid_until.isoformat() if self.valid_until else "").encode("utf-8")
        )
        return digest.hexdigest()

    def __post_init__(self) -> None:
        if not isinstance(self.binding_version, int) or self.binding_version < 1:
            raise ValueError(
                "binding_version must be an integer >= 1 (canonical "
                "BindingManifest semantics)"
            )
        if isinstance(self.binding_version, bool):
            raise ValueError("binding_version must be an integer >= 1")
        if (
            not isinstance(self.subject_revision, int)
            or isinstance(self.subject_revision, bool)
            or self.subject_revision < 0
        ):
            raise ValueError("subject_revision must be an integer >= 0")

    def is_expired(self, now: datetime) -> bool:
        return self.valid_until is not None and now >= self.valid_until


@dataclass(frozen=True, slots=True)
class PolicyDecisionInput:
    """A policy decision for MEMORY_CAPTURE / MEMORY_PROMOTION (section 6.2)."""

    effect: PolicyEffect = PolicyEffect.POLICY_EFFECT_DENY
    capability: str = "memory_capture"
    reason_code: str = "no_decision"
    receipt_id: str | None = None
    obligations: tuple[PolicyObligation, ...] = ()
    policy_version: str = "policy-v2"

    def has_obligation(self, obligation: PolicyObligation) -> bool:
        return obligation in self.obligations


@dataclass(frozen=True, slots=True)
class ConsentSnapshotInput:
    """Consent evidence snapshot referenced by durable writes (section 8.7)."""

    snapshot_id: str | None = None
    granted: bool = False
    scope: str = "memory"
    covers_subjects: tuple[str, ...] = ()


def verify_receipt_against_fence(
    receipt: PolicyReceiptV2,
    *,
    fence: WriteFence,
    actor_subject_id: str,
    capability: str,
    purpose: str,
    now: datetime,
    expected_resource_owner_id: str | None = None,
    check_expiry: bool = True,
) -> bool:
    """Pure, deterministic PolicyReceiptV2-vs-fence match (P0 contract).

    The V2 receipt is the single authoritative wire (no parallel receipt
    type): actor/subject/resource owner/device/binding/session/epoch/
    profile/subject revision, purpose, capability, exact-fence and expiry
    must all agree, otherwise the write fails closed.  The Policy V2
    ``context_hash`` covers the FULL PolicyContext and can never equal a
    local fence fingerprint, so it is NOT compared here: the authoritative
    verifier runs :func:`services.policy.receipts.receipt_fence_valid` /
    ``exact_evidence_fence_valid`` against the current evidence and the
    consumer only requires its verified result (plus this field check).
    ``expected_resource_owner_id`` is scope-specific (private/guardian
    summaries are owned by the subject; family-space ownership is never
    masqueraded as a person): pass it only when the scope defines a person
    owner.  ``check_expiry=False`` is used ONLY for provenance re-checks
    (the family proposal receipt at vote time): current revocation is
    decided by the authorization port against current evidence revisions,
    while the capture receipt's 5-minute window must never make an
    otherwise-valid asynchronous confirmation unconfirmable.
    """
    if receipt.effect == PolicyEffect.POLICY_EFFECT_DENY.value:
        return False
    if receipt.capability != capability:
        return False
    if receipt.purpose != purpose:
        return False
    if receipt.actor_id != actor_subject_id:
        return False
    if receipt.subject_id != fence.active_subject_id:
        return False
    if (
        expected_resource_owner_id is not None
        and receipt.resource_owner_id != expected_resource_owner_id
    ):
        return False
    if receipt.device_id != fence.device_id:
        return False
    if receipt.session_id != fence.session_id:
        return False
    if receipt.session_epoch != fence.epoch:
        return False
    if receipt.runtime_profile_id != fence.runtime_profile_id:
        return False
    if receipt.binding_id != fence.binding_id:
        return False
    if receipt.binding_version != fence.binding_version:
        return False
    if receipt.subject_revision != fence.subject_revision:
        return False
    if not receipt.exact_fence:
        return False
    if check_expiry and not receipt.created_at <= now < receipt.expires_at:
        return False
    return True


@dataclass(frozen=True, slots=True)
class CoSubjectContext:
    """Other subjects involved in a family-shared memory (section 6.4/PR-14)."""

    subject_ids: tuple[str, ...] = ()
    confirmed_subject_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    """Everything the resolver needs to decide a memory scope."""

    subject: SubjectContext
    policy: PolicyDecisionInput
    consent: ConsentSnapshotInput = ConsentSnapshotInput()
    co_subjects: CoSubjectContext = CoSubjectContext()
    requested_scope: MemoryScope = MEMORY_SCOPE_DEFAULT
    fence: WriteFence | None = None


@dataclass(frozen=True, slots=True)
class ScopeResolution:
    """Fail-closed outcome of ``MemoryScopeResolver.resolve``.

    ``persistable`` is true only for durable scopes with a complete
    sensitive-source set.  Ephemeral results carry ``persistable=False``:
    they are session-local by definition and never enter long-term memory.
    """

    scope: MemoryScope
    persistable: bool
    reason_code: str
    raw_transcript_allowed: bool = False
    retention: RetentionPolicy = "indefinite"


class DeniedResolution:
    """Namespace of canned fail-closed resolutions."""

    UNKNOWN_SUBJECT = ScopeResolution(
        scope=MemoryScope.MEMORY_SCOPE_UNKNOWN,
        persistable=False,
        reason_code="subject_not_confirmed",
    )
    UNREGISTERED_GUEST = ScopeResolution(
        scope=MemoryScope.MEMORY_SCOPE_UNKNOWN,
        persistable=False,
        reason_code="unregistered_guest",
    )
    POLICY_DENIED = ScopeResolution(
        scope=MemoryScope.MEMORY_SCOPE_UNKNOWN,
        persistable=False,
        reason_code="policy_denied",
    )
    UNKNOWN_CATEGORY = ScopeResolution(
        scope=MemoryScope.MEMORY_SCOPE_UNKNOWN,
        persistable=False,
        reason_code="subject_category_unknown",
    )
    MISSING_SOURCE = ScopeResolution(
        scope=MemoryScope.MEMORY_SCOPE_UNKNOWN,
        persistable=False,
        reason_code="missing_sensitive_source",
    )
    CONSENT_MISSING = ScopeResolution(
        scope=MemoryScope.MEMORY_SCOPE_UNKNOWN,
        persistable=False,
        reason_code="consent_missing",
    )
    CO_SUBJECTS_UNCONFIRMED = ScopeResolution(
        scope=MemoryScope.MEMORY_SCOPE_UNKNOWN,
        persistable=False,
        reason_code="co_subjects_unconfirmed",
    )
    GUARDIAN_NOT_ACTIVE = ScopeResolution(
        scope=MemoryScope.MEMORY_SCOPE_UNKNOWN,
        persistable=False,
        reason_code="guardian_relationship_inactive",
    )
    WRITE_FENCE_MISSING = ScopeResolution(
        scope=MemoryScope.MEMORY_SCOPE_UNKNOWN,
        persistable=False,
        reason_code="write_fence_missing",
    )


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """A durable memory row (section 8.7 field set).

    ``status`` transitions are append-only: the store derives the current
    status from status events, and ``revoked``/``withdrawn_at`` marks make
    the record invisible to retrieval (section 13.5).
    """

    record_id: str
    scope: MemoryScope
    subject_id: str
    resource_owner_id: str
    family_space_id: str | None = None
    co_subject_ids: tuple[str, ...] = ()
    source_evidence_ids: tuple[str, ...] = ()
    policy_receipt_id: str = ""
    consent_snapshot_id: str = ""
    #: Full promotion evidence (THREE authority actions): the fresh
    #: family_shared_memory_promotion receipt, the promotion fence hash it
    #: was bound to, and the EXACT per-vote approval evidence refs
    #: ((subject, approval_policy_receipt_id, approval_snapshot_id,
    #: approval_snapshot_revision, approval_snapshot_hash) 5-tuples) that
    #: the finalizer CAS verified against the authoritative vote set.
    #: Persisted with the record so audit/replay can prove WHICH approvals
    #: + promotion decision created it.
    promotion_receipt_id: str | None = None
    promotion_fence_context_hash: str | None = None
    approval_evidence_refs: tuple[tuple[str, str, str, int, str], ...] = ()
    memory_type: str = "semantic"
    confidence: float = 0.5
    status: MemoryStatus = "candidate"
    retention: RetentionPolicy = DEFAULT_RETENTION
    #: Explicit retention expiry derived ONLY from the policy's
    #: ``RETENTION_TTL`` window (``created_at + retention_seconds``).  The
    #: runtime profile/binding ``valid_until`` is a write-time authorization
    #: fence and must never hide an already-legitimate long-term record
    #: (P0-3).
    retention_expires_at: datetime | None = None
    payload: dict[str, object] = field(default_factory=dict)
    created_by_actor_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None
    shared_proposal_id: str | None = None
    withdrawn_at: datetime | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "scope": self.scope.value,
            "subject_id": self.subject_id,
            "resource_owner_id": self.resource_owner_id,
            "family_space_id": self.family_space_id,
            "co_subject_ids": list(self.co_subject_ids),
            "source_evidence_ids": list(self.source_evidence_ids),
            "policy_receipt_id": self.policy_receipt_id,
            "consent_snapshot_id": self.consent_snapshot_id,
            "promotion_receipt_id": self.promotion_receipt_id,
            "promotion_fence_context_hash": self.promotion_fence_context_hash,
            "approval_evidence_refs": [
                list(pair) for pair in self.approval_evidence_refs
            ],
            "memory_type": self.memory_type,
            "confidence": self.confidence,
            "status": self.status,
            "retention": self.retention,
            "retention_expires_at": (
                self.retention_expires_at.isoformat()
                if self.retention_expires_at
                else None
            ),
            "payload": self.payload,
            "created_by_actor_id": self.created_by_actor_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "shared_proposal_id": self.shared_proposal_id,
            "withdrawn_at": self.withdrawn_at.isoformat() if self.withdrawn_at else None,
        }

    def is_visible(self, now: datetime | None = None) -> bool:
        """Visible only when not revoked / withdrawn and not expired."""
        if self.status == "revoked" or self.withdrawn_at is not None:
            return False
        if self.retention_expires_at is not None:
            current = now if now is not None else datetime.now(UTC)
            if current >= self.retention_expires_at:
                return False
        return True


@dataclass(frozen=True, slots=True)
class MemoryWriteDraft:
    """Payload for a candidate write before scope resolution."""

    content: str
    source_evidence_ids: tuple[str, ...] = ()
    memory_type: str = "semantic"
    confidence: float = 0.5
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SharedMemoryProposal:
    """A pending family-shared memory (PR-14 lifecycle)."""

    proposal_id: str
    family_space_id: str
    proposer_subject_id: str
    co_subject_ids: tuple[str, ...]
    title: str
    content: str
    #: Binding manifest version the proposal was created under (fourth
    #: review): later votes verify the voter is still a member under the
    #: same binding version.  REQUIRED - a missing version is never treated
    #: as v1 (fifth review).
    binding_version: int
    #: Immutable fence snapshot the proposal was issued under (main
    #: architecture review): the final confirmation re-verifies the policy
    #: receipt / consent / membership against EXACTLY this fence, so an
    #: epoch/binding/device/profile change or a re-issued consent snapshot
    #: after proposal creation can never promote a stale authorization.
    session_id: str
    epoch: int
    binding_id: str
    binding_role: str
    runtime_profile_id: str
    device_id: str
    subject_revision: int
    fence_context_hash: str
    #: Canonical exact-action evidence (PolicyActionResourceFence contract):
    #: the proposal revision, capture evidence digest and the consent /
    #: membership snapshot identity+revision+hash are the AUTHORITATIVE
    #: values the final promotion fence must match field-by-field.  A
    #: promotion authorization that references a different proposal
    #: revision, a different consent snapshot id (even with the same
    #: revision number) or a different membership hash fails closed.
    #: REQUIRED for every non-terminal proposal (pending /
    #: approvals_complete): a missing/zero revision or empty snapshot
    #: identity makes the proposal unconstructable (fail closed), so a
    #: legacy row without canonical evidence can never enter family
    #: promotion.  Terminal rows (frozen/withdrawn/promoted) may keep
    #: legacy zeros for quarantine readability - they are never promoted.
    proposal_revision: int
    capture_evidence_hash: str
    consent_snapshot_revision: int
    consent_snapshot_hash: str
    membership_snapshot_id: str
    membership_snapshot_revision: int
    membership_snapshot_hash: str
    #: Complete immutable fence identity (main architecture review):
    #: generation/turn and the original validity window are part of the
    #: fence the proposal was issued under; the final confirmation
    #: reconstructs EXACTLY this fence and rejects expiry / field drift.
    generation_id: str | None = None
    turn_id: int | None = None
    valid_until: datetime | None = None
    source_evidence_ids: tuple[str, ...] = ()
    proposal_policy_receipt_id: str = ""
    consent_snapshot_id: str = ""
    #: Canonical generation / tool epoch counters (the canonical
    #: PolicyActionResourceFence binds them as integers; the legacy
    #: ``generation_id`` string stays for event semantics).
    generation: int = 0
    tool_epoch: int = 0
    status: ProposalStatus = "pending"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.binding_version, int)
            or isinstance(self.binding_version, bool)
            or self.binding_version < 1
        ):
            raise ValueError(
                "shared memory proposal binding_version must be an integer >= 1"
            )
        if (
            not isinstance(self.epoch, int)
            or isinstance(self.epoch, bool)
            or self.epoch < 1
        ):
            raise ValueError(
                "shared memory proposal epoch must be an integer >= 1"
            )
        if (
            not isinstance(self.subject_revision, int)
            or isinstance(self.subject_revision, bool)
            or self.subject_revision < 0
        ):
            raise ValueError(
                "shared memory proposal subject_revision must be an integer >= 0"
            )
        for name in (
            "session_id",
            "binding_id",
            "binding_role",
            "runtime_profile_id",
            "fence_context_hash",
        ):
            if not getattr(self, name).strip():
                raise ValueError(
                    f"shared memory proposal {name} must not be empty"
                )
        if self.turn_id is not None and (
            not isinstance(self.turn_id, int)
            or isinstance(self.turn_id, bool)
            or self.turn_id < 1
        ):
            raise ValueError(
                "shared memory proposal turn_id must be an integer >= 1"
            )
        if self.valid_until is not None and (
            self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None
        ):
            raise ValueError(
                "shared memory proposal valid_until must be timezone-aware"
            )
        if self.status in ("pending", "approvals_complete"):
            for name, minimum in (
                ("proposal_revision", 1),
                ("consent_snapshot_revision", 1),
                ("membership_snapshot_revision", 1),
                ("generation", 0),
                ("tool_epoch", 0),
            ):
                value = getattr(self, name)
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < minimum
                ):
                    raise ValueError(
                        f"shared memory proposal {name} must be an integer "
                        f">= {minimum} for a non-terminal proposal"
                    )
            for name in (
                "capture_evidence_hash",
                "consent_snapshot_hash",
                "membership_snapshot_hash",
            ):
                value = getattr(self, name)
                if (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(c not in "0123456789abcdef" for c in value.lower())
                ):
                    raise ValueError(
                        f"shared memory proposal {name} must be a 64-char "
                        "hex digest for a non-terminal proposal"
                    )
            if not self.membership_snapshot_id.strip():
                raise ValueError(
                    "shared memory proposal membership_snapshot_id must not "
                    "be empty for a non-terminal proposal"
                )
            if not self.consent_snapshot_id.strip():
                raise ValueError(
                    "shared memory proposal consent_snapshot_id must not "
                    "be empty for a non-terminal proposal"
                )

    @property
    def all_confirmable_subjects(self) -> tuple[str, ...]:
        """Everyone whose confirmation is required (proposer + co-subjects)."""
        return tuple(dict.fromkeys((self.proposer_subject_id, *self.co_subject_ids)))


@dataclass(frozen=True, slots=True)
class ConfirmationVote:
    """One co-subject's decision on a proposal (append-only)."""

    proposal_id: str
    subject_id: str
    decision: VoteDecision
    voted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    evidence_id: str = ""
    #: This voter's OWN immutable approval evidence (actor = voter) - the
    #: per-vote family_shared_memory_approval receipt verified right before
    #: the vote was recorded (THREE authority actions: proposal / per-vote
    #: approval / final promotion).  Confirms MUST carry a non-empty
    #: approval receipt id; objections deliberately carry none.  The final
    #: promotion receipt belongs to the finalizer action only and is NEVER
    #: stored on a vote.
    approval_receipt_id: str = ""
    #: Canonical per-vote approval snapshot identity (PolicyApprovalSnapshotFence
    #: contract): the final promotion fence's ``approval_snapshots`` must
    #: record-set-equal the persisted votes on subject / snapshot id /
    #: revision / hash.  Confirms REQUIRE a non-empty snapshot triple;
    #: objections deliberately carry none.
    approval_snapshot_id: str = ""
    approval_snapshot_revision: int = 0
    approval_snapshot_hash: str = ""

    def __post_init__(self) -> None:
        if self.decision == "confirm":
            if not self.approval_receipt_id.strip():
                raise ValueError(
                    "confirm vote requires a non-empty approval_receipt_id"
                )
            if not self.approval_snapshot_id.strip():
                raise ValueError(
                    "confirm vote requires a non-empty approval_snapshot_id"
                )
            if (
                not isinstance(self.approval_snapshot_revision, int)
                or isinstance(self.approval_snapshot_revision, bool)
                or self.approval_snapshot_revision < 1
            ):
                raise ValueError(
                    "confirm vote approval_snapshot_revision must be an integer >= 1"
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
                    "confirm vote approval_snapshot_hash must be a 64-char hex digest"
                )
        else:
            if (
                self.approval_snapshot_id
                or self.approval_snapshot_revision
                or self.approval_snapshot_hash
            ):
                raise ValueError(
                    "object vote must not carry approval snapshot evidence"
                )


@dataclass(frozen=True, slots=True)
class SharedVisibility:
    """What an actor may see of a shared memory (section 13.5)."""

    visible: bool
    scope: MemoryScope
    reason_code: str
    summary_only: bool = False


@dataclass(frozen=True, slots=True)
class MemoryOutboxEvent:
    """Transactional outbox entry; ``event_id`` is the idempotency key."""

    outbox_id: str
    event_id: str
    topic: str
    payload: dict[str, object]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class MemoryAuditEvent:
    """Append-only audit trail entry for memory mutations."""

    event_id: str
    action: str
    actor_subject_id: str
    subject_id: str | None
    record_id: str | None
    proposal_id: str | None
    payload: dict[str, object]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class MemoryRecordStatusEvent:
    """Append-only status transition for a memory record."""

    event_id: str
    record_id: str
    status: MemoryStatus
    reason_code: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def ensure_sensitive_source(
    *,
    subject_id: str | None,
    resource_owner_id: str | None,
    source_evidence_ids: tuple[str, ...],
    policy_receipt_id: str | None,
    consent_snapshot_id: str | None,
) -> None:
    """Fail closed when any sensitive source is missing (section 6.2)."""
    missing: list[str] = []
    if not subject_id:
        missing.append("subject_id")
    if not resource_owner_id:
        missing.append("resource_owner_id")
    if not source_evidence_ids:
        missing.append("source_evidence_ids")
    if not policy_receipt_id:
        missing.append("policy_receipt_id")
    if not consent_snapshot_id:
        missing.append("consent_snapshot_id")
    if missing:
        raise MissingSensitiveSourceError(
            "durable memory write rejected: missing_sensitive_source"
            " (missing " + ", ".join(missing) + ")"
        )


__all__ = [
    "ActorNotAuthorizedError",
    "AlreadyVotedError",
    "BindingRole",
    "BindingRoleValue",
    "CoSubjectContext",
    "ConfirmationVote",
    "ConsentSnapshotInput",
    "CrossFamilyAccessError",
    "DEFAULT_RETENTION",
    "DURABLE_SCOPES",
    "DeniedResolution",
    "MemoryAuditEvent",
    "MemoryNotFoundError",
    "MemoryOutboxEvent",
    "MemoryRecord",
    "MemoryRecordStatusEvent",
    "MemoryScope",
    "MemoryScopeError",
    "MemoryScopeValue",
    "MemoryStatus",
    "MemoryWriteDraft",
    "MemoryWriteRejected",
    "MissingSensitiveSourceError",
    "NotAuthorizedError",
    "OBLIGATION_DO_NOT_PERSIST",
    "OBLIGATION_PERSIST_AGGREGATE_ONLY",
    "OBLIGATION_REQUIRE_SPEAKER_CONFIRMATION",
    "OBLIGATION_REQUIRE_SUBJECT_APPROVAL",
    "OBLIGATION_RETENTION_TTL",
    "OBLIGATION_WRITE_POLICY_RECEIPT",
    "PolicyDecisionInput",
    "PolicyEffect",
    "PolicyEffectValue",
    "PolicyObligation",
    "ProposalStateError",
    "ProposalStatus",
    "RawTranscriptDeniedError",
    "ReceiptNotVerifiedError",
    "RelationshipStatus",
    "ResolutionContext",
    "RetentionPolicy",
    "ScopeResolution",
    "SharedMemoryProposal",
    "SharedVisibility",
    "SpeakerState",
    "SpeakerStateValue",
    "SubjectCategory",
    "SubjectCategoryValue",
    "SubjectContext",
    "UnresolvedSubjectError",
    "VoteDecision",
    "WriteFence",
    "WriteFenceError",
    "WriteFenceExpiredError",
    "WriteFenceMismatchError",
    "WriteFenceMissingError",
    "ensure_sensitive_source",
    "verify_receipt_against_fence",
]
