"""Integration seam: real Consent Authority evidence flows into Policy Engine V2.

This is the cross-agent conformance test: the frozen evidence dataclasses
implemented by ``services/consent`` (Agent A) must satisfy the structural
ports consumed by ``services/policy`` (Agent B) and drive the V2 decision
matrix, receipts and exact-fence invalidation with real store data.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from services.consent.authority import ConsentAuthority, SubjectProof
from services.consent.evidence import BindingEvidence, ConsentParams, RelationshipEvidence
from services.consent.in_memory_store import InMemoryConsentStore
from services.policy.context import PolicyContext
from services.policy.engine import PolicyEngine
from services.policy.obligations import obligation_codes
from services.policy.receipts import exact_evidence_fence_valid
from services.policy.tests.fakes import make_consent_snapshot_ref

NOW = datetime(2026, 8, 9, 8, 0, 0, tzinfo=UTC)


class _DirectionalRelationshipAdapter:
    """Expose Identity-directional fields while Agent A removes old aliases."""

    def __init__(self, evidence: RelationshipEvidence) -> None:
        self._evidence = evidence

    def __getattr__(self, name: str) -> object:
        return getattr(self._evidence, name)

    @property
    def guardian_person_id(self) -> str:
        return self._evidence.source_person_id

    @property
    def subject_person_id(self) -> str:
        return self._evidence.target_person_id


class _Resolver:
    """Test adapter proving Policy consumes Agent A's resolved evidence types."""

    def __init__(
        self,
        *,
        subject: SubjectProof,
        binding: BindingEvidence,
        relationships: tuple[RelationshipEvidence, ...] = (),
    ) -> None:
        self._subject = subject
        self._binding = binding
        self._relationships = relationships

    def resolve_subject(self, candidate: SubjectProof) -> SubjectProof:
        if candidate.subject_id != self._subject.subject_id:
            raise LookupError(candidate.subject_id)
        return self._subject

    def resolve_binding(
        self, candidate: BindingEvidence, subject_id: str
    ) -> BindingEvidence:
        if (
            candidate.binding_id != self._binding.binding_id
            or candidate.version != self._binding.version
            or subject_id != self._subject.subject_id
        ):
            raise LookupError((candidate.binding_id, candidate.version, subject_id))
        return self._binding

    def resolve_relationships(
        self,
        candidates: tuple[RelationshipEvidence, ...],
        actor_id: str,
        subject_id: str,
        binding_id: str,
    ) -> tuple[RelationshipEvidence, ...]:
        if subject_id != self._subject.subject_id or binding_id != self._binding.binding_id:
            raise LookupError((actor_id, subject_id, binding_id))
        return candidates


def _binding(
    *,
    declared_mode: str = "self_use",
    version: int = 1,
    status: str = "active",
) -> BindingEvidence:
    return BindingEvidence(
        binding_id="bd_1",
        version=version,
        device_id="dev_1",
        status=status,  # type: ignore[arg-type]
        declared_mode=declared_mode,  # type: ignore[arg-type]
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=365),
        canonical_hash="",
    )


def _relationship(
    *,
    source_person_id: str,
    target_person_id: str,
    status: str = "active",
) -> RelationshipEvidence:
    return RelationshipEvidence(
        relationship_id="rel_1",
        snapshot_id="rs_1",
        revision=1,
        relation_type="guardian_of",
        status=status,  # type: ignore[arg-type]
        source_person_id=source_person_id,
        target_person_id=target_person_id,
        binding_id="bd_1",
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=365),
        canonical_hash="",
    )


def _adult_self_voice_clone() -> tuple[ConsentAuthority, object, object]:
    subject = SubjectProof(
        subject_id="person_adult",
        subject_category="adult",
        age_evidence_status="verified",
    )
    binding = _binding()
    authority = ConsentAuthority(
        InMemoryConsentStore(),
        resolver=_Resolver(subject=subject, binding=binding),
    )
    offer = authority.create_offer(
        capability="voice_clone_use",
        subject_id="person_adult",
        actor_id="person_adult",
        resource_owner_id="person_adult",
        purpose="voice_clone",
        params=ConsentParams(),
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=30),
        policy_version="policy-adult-v1",
        created_at=NOW,
    )
    result = authority.grant(
        offer,
        expected_version=offer.version,
        subject=subject,
        binding=binding,
        actor_kind="subject",
        now=NOW,
    )
    return authority, result, result.evidence


def _context(
    *,
    capability: str,
    evidence: object,
    actor_id: str = "person_adult",
    subject_id: str = "person_adult",
    subject_category: str = "adult",
    age_band: str = "adult",
    data_classification: str = "biometric",
    current_session_mode: str = "adult_companion",
    binding: BindingEvidence | None = None,
    relationships: tuple[RelationshipEvidence, ...] = (),
    device_trust: str = "trusted",
    purpose: str = "user_request",
    evaluated_at: datetime = NOW,
) -> PolicyContext:
    consent_snapshot_evidence = (
        make_consent_snapshot_ref(
            snapshot_id=evidence.snapshot_id,  # type: ignore[attr-defined]
            revision=evidence.version,  # type: ignore[attr-defined]
            canonical_hash=evidence.canonical_hash,  # type: ignore[attr-defined]
            consents=(evidence,),  # type: ignore[arg-type]
            now=evaluated_at,
        ),
    )
    return PolicyContext(
        actor_id=actor_id,
        subject_id=subject_id,
        resource_owner_id=subject_id,
        device_id="dev_1",
        capability=capability,  # type: ignore[arg-type]
        purpose=purpose,
        declared_device_mode="self_use",
        current_session_mode=current_session_mode,  # type: ignore[arg-type]
        subject_category=subject_category,  # type: ignore[arg-type]
        age_band=age_band,  # type: ignore[arg-type]
        speaker_state="confirmed",
        speaker_confidence=0.99,
        device_trust=device_trust,
        safety_state="normal",
        jurisdiction="CN",
        data_classification=data_classification,
        binding_id="bd_1",
        binding_version=1,
        session_id="ses_1",
        session_epoch=1,
        runtime_profile_id="rp_1",
        subject_revision=0,
        evaluated_at=evaluated_at,
        consent_evidence=(evidence,),  # type: ignore[arg-type]
        consent_snapshot_evidence=consent_snapshot_evidence,
        relationship_evidence=relationships,
        binding_evidence=binding,
    )


def test_real_evidence_types_construct_strict_policy_context() -> None:
    # Conformance proof: Agent A's frozen dataclasses carry every field and
    # method the PolicyContext structural validation requires.
    _, result, _ = _adult_self_voice_clone()
    context = _context(
        capability="voice_clone_use",
        evidence=result.evidence,
        binding=result.snapshot.binding,
    )
    assert context.consent_evidence[0].snapshot_id == result.evidence.snapshot_id
    assert context.binding_evidence is not None


def test_adult_self_voice_clone_with_authority_evidence_allowed() -> None:
    _, result, _ = _adult_self_voice_clone()
    decision = PolicyEngine().decide(
        _context(
            capability="voice_clone_use",
            evidence=result.evidence,
            binding=result.snapshot.binding,
            purpose="voice_clone",
        )
    )
    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "adult_subject_authorized"
    assert "REQUIRE_STEP_UP_AUTH" in obligation_codes(decision.obligations)


def test_revoked_authority_consent_denies_and_breaks_exact_fence() -> None:
    authority, result, _ = _adult_self_voice_clone()
    revoked = authority.revoke(
        result.evidence.consent_id,
        actor_id="person_adult",
        actor_kind="subject",
        now=NOW + timedelta(minutes=1),
    )
    context = _context(
        capability="voice_clone_use",
        evidence=revoked.evidence,
        binding=revoked.snapshot.binding,
        purpose="voice_clone",
        evaluated_at=NOW + timedelta(minutes=2),
    )
    assert PolicyEngine().decide(context).effect == "deny"

    # The original allow receipt must not survive the revocation.
    original_context = _context(
        capability="voice_clone_use",
        evidence=result.evidence,
        binding=result.snapshot.binding,
        purpose="voice_clone",
    )
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-fence-1")
    decision = engine.decide(original_context)
    receipt = engine.receipt_for(original_context, decision)
    assert exact_evidence_fence_valid(
        receipt, context=original_context, now=NOW + timedelta(minutes=1)
    )
    assert not exact_evidence_fence_valid(
        receipt,
        context=replace(original_context, consent_evidence=(revoked.evidence,)),
        now=NOW + timedelta(minutes=2),
    )


def test_guardian_granted_minor_memory_capture_allowed_with_obligations() -> None:
    consent_relationship = _DirectionalRelationshipAdapter(
        _relationship(
            source_person_id="person_guardian",
            target_person_id="person_minor",
        )
    )
    subject = SubjectProof(
        subject_id="person_minor",
        subject_category="minor",
        age_evidence_status="verified",
    )
    binding = _binding(declared_mode="parent_for_child")
    authority = ConsentAuthority(
        InMemoryConsentStore(),
        resolver=_Resolver(
            subject=subject,
            binding=binding,
            relationships=(consent_relationship,),  # type: ignore[arg-type]
        ),
    )
    offer = authority.create_offer(
        capability="memory_capture",
        subject_id="person_minor",
        actor_id="person_guardian",
        resource_owner_id="person_minor",
        purpose="memory_capture",
        params=ConsentParams(),
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=365),
        policy_version="policy-cn-minor-v5",
        created_at=NOW,
    )
    granted = authority.grant(
        offer,
        expected_version=offer.version,
        subject=subject,
        binding=binding,
        relationships=(consent_relationship,),  # type: ignore[arg-type]
        actor_kind="guardian",
        now=NOW,
    )
    context = _context(
        capability="memory_capture",
        evidence=granted.evidence,
        actor_id="person_minor",
        subject_id="person_minor",
        subject_category="minor",
        age_band="under_14",
        data_classification="private",
        current_session_mode="student_minor",
        binding=granted.snapshot.binding,
        relationships=(consent_relationship,),  # type: ignore[arg-type]
        purpose="memory_capture",
    )
    decision = PolicyEngine().decide(context)
    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "minor_memory_minimized"
    assert "PERSIST_AGGREGATE_ONLY" in obligation_codes(decision.obligations)
    assert "NO_MODEL_TRAINING" in obligation_codes(decision.obligations)


def test_minor_voice_clone_hard_denied_even_with_authority_evidence() -> None:
    # A guardian can never grant voice_clone_use, but even if evidence arrives
    # the matrix must hard-deny for minors.
    _, result, _ = _adult_self_voice_clone()
    context = _context(
        capability="voice_clone_use",
        evidence=result.evidence,
        binding=result.snapshot.binding,
        actor_id="person_minor",
        subject_id="person_minor",
        subject_category="minor",
        age_band="under_14",
        current_session_mode="student_minor",
        purpose="voice_clone",
    )
    decision = PolicyEngine().decide(context)
    assert decision.effect == "deny"
    assert decision.reason_code == "minor_capability_forbidden"


def test_binding_version_bump_invalidates_receipt_fence() -> None:
    _, result, _ = _adult_self_voice_clone()
    context = _context(
        capability="voice_clone_use",
        evidence=result.evidence,
        binding=result.snapshot.binding,
        purpose="voice_clone",
    )
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-binding-1")
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)
    assert exact_evidence_fence_valid(
        receipt, context=context, now=NOW + timedelta(minutes=1)
    )

    bumped_binding = _binding(version=2)
    bumped = replace(context, binding_version=2, binding_evidence=bumped_binding)
    assert not exact_evidence_fence_valid(
        receipt, context=bumped, now=NOW + timedelta(minutes=1)
    )
    assert PolicyEngine().decide(bumped).effect == "deny"


def test_untrusted_device_denies_sensitive_capability_despite_consent() -> None:
    _, result, _ = _adult_self_voice_clone()
    context = _context(
        capability="voice_clone_use",
        evidence=result.evidence,
        binding=result.snapshot.binding,
        device_trust="offline",
        purpose="voice_clone",
    )
    decision = PolicyEngine().decide(context)
    assert decision.effect == "deny"
    assert decision.reason_code == "device_untrusted"


def test_device_transfer_offer_grant_policy_and_receipt_freeze_canonical_purpose() -> None:
    subject = SubjectProof("person_adult", "adult", "verified")
    binding = _binding()
    authority = ConsentAuthority(
        InMemoryConsentStore(),
        resolver=_Resolver(subject=subject, binding=binding),
    )
    offer = authority.create_offer(
        capability="device_ownership_transfer",
        subject_id="person_adult",
        actor_id="person_adult",
        resource_owner_id="person_adult",
        purpose="device_transfer",
        params=ConsentParams(),
        valid_from=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(hours=1),
        policy_version="multi-subject-v2",
        created_at=NOW,
    )
    granted = authority.grant(
        offer,
        subject=subject,
        binding=binding,
        actor_kind="subject",
        now=NOW,
    )
    context = _context(
        capability="device_ownership_transfer",
        evidence=granted.evidence,
        binding=granted.snapshot.binding,
        purpose="device_transfer",
        data_classification="private",
    )
    engine = PolicyEngine()
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    assert offer.purpose == granted.evidence.purpose == "device_transfer"
    assert decision.effect == "allow_with_obligations"
    assert receipt.purpose == "device_transfer"
