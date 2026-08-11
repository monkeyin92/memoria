"""Policy Engine V2: deterministic, evidence-fenced multi-subject decisions.

The decision matrix follows the corrected edition of the shared CONTRACT:

* unknown_safe / unconfirmed subjects get ephemeral chat / english_practice
  only; tutor is denied and every sensitive capability is denied.
* confirmed subjects with ``subject_category=unknown`` never fall into the
  plain adult allow path.
* minor capabilities require governance (active guardian relationship
  evidence) + authoritative consent evidence + an active binding fence;
  voice_clone_use / digital_self_preview / legacy_grant_create /
  device_ownership_transfer are hard-forbidden; payment / raw_audio_retention /
  model_training_contribution are default-denied.
* adult sensitive capabilities require actor==subject==resource_owner,
  authoritative subject-owned consent evidence (purpose and data-classification
  consistent with the context), a matching active binding fence and
  device_trust ∈ {trusted, verified}.
* offline / revoked / untrusted devices deny every sensitive capability.
  Exception: crisis_notification remains evaluable on ``offline`` (degraded but
  trusted lineage) devices so the emergency path survives degraded service;
  untrusted/revoked devices still fail closed (§10.6 spirit).
* crisis_notification only emits minimal-notification obligations — the engine
  never executes notifications.

Policy owns ``device_transfer`` as the canonical purpose for
``device_ownership_transfer``; Identity now exposes the same frozen value.

The engine consumes only verified evidence through
``services.policy.evidence`` ports; ``relationship_roles``/``consent_kinds``
are compatibility fields and are never authoritative.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from packages.contracts.generated.python.multi_subject_contracts import (
    CapabilityValue,
    PolicyDecision,
    PolicyEffectValue,
    PolicyObligationSpec,
    PolicyObligationValue,
    PolicyReceiptV2,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    ObligationParams as GeneratedObligationParams,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyObligation as GeneratedPolicyObligation,
)

from services.policy.action_fence import capture_authority_hash
from services.policy.context import (
    PolicyContext,
    canonical_runtime_purpose_for_capability,
    context_hash,
    effective_action_resource_fence,
    is_resource_scoped_action,
)
from services.policy.evidence import (
    BindingEvidencePort,
    ConsentEvidencePort,
    ConsentSnapshotEvidencePort,
    RelationshipEvidencePort,
    active_relationship_for,
    binding_fence_ok,
    effective_consents_for,
    is_guardian_of,
)
from services.policy.producer import require_policy_producer
from services.policy.receipts import PolicyReceiptWriterV2

type Capability = CapabilityValue
type PolicyEffect = PolicyEffectValue
type PolicyObligationCode = PolicyObligationValue

__all__ = ["PolicyContext", "PolicyDecision", "PolicyEngine"]


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


#: Sensitive capabilities (§9.4): denied on offline/revoked/untrusted devices.
SENSITIVE_CAPABILITIES: Final[frozenset[Capability]] = frozenset(
    {
        "memory_capture",
        "memory_promotion",
        "family_shared_memory_proposal",
        "family_shared_memory_approval",
        "family_shared_memory_promotion",
        "memory_recall_private",
        "guardian_summary_view",
        "voice_profile_create",
        "voice_clone_use",
        "digital_self_preview",
        "legacy_grant_create",
        "payment",
        "raw_audio_retention",
        "model_training_contribution",
        "crisis_notification",
        "device_ownership_transfer",
    }
)


def privacy_action_requires_allow_receipt(action: str) -> bool:
    """Object/withdraw are authenticated privacy actions, never allow-gated."""
    if action in {"object", "withdraw"}:
        return False
    raise ValueError("unknown family privacy action")


#: Minor hard-forbidden set — no evidence can flip these.
MINOR_HARD_FORBIDDEN: Final[frozenset[Capability]] = frozenset(
    {
        "voice_clone_use",
        "digital_self_preview",
        "legacy_grant_create",
        "device_ownership_transfer",
    }
)

#: Minor default-denied set — never opened by profile or admin claims.
MINOR_DEFAULT_DENIED: Final[frozenset[Capability]] = frozenset(
    {"payment", "raw_audio_retention", "model_training_contribution"}
)

#: Minor capabilities allowed under governance + authoritative consent evidence.
MINOR_GOVERNED_ALLOWED: Final[frozenset[Capability]] = frozenset(
    {
        "chat",
        "tutor",
        "english_practice",
        "memory_capture",
        "memory_recall_private",
        "voice_profile_create",
    }
)

#: Devices that may exercise sensitive capabilities (crisis has its own path).
TRUSTED_DEVICE_TRUSTS: Final[frozenset[str]] = frozenset({"trusted", "verified"})

CRISIS_SIGNAL_STATES: Final[frozenset[str]] = frozenset({"self_crisis"})

CRISIS_RELATION_RULES: Final[dict[tuple[str, str], frozenset[str]]] = {
    ("minor", "student_minor"): frozenset({"guardian_of"}),
    ("adult", "adult_companion"): frozenset({"emergency_contact_for"}),
    ("adult", "adult_archive"): frozenset({"emergency_contact_for"}),
    ("adult", "senior_companion"): frozenset({"delegate_for"}),
}

CRISIS_RECIPIENT_ROLES: Final[dict[str, str]] = {
    "guardian_of": "guardian",
    "emergency_contact_for": "emergency_contact",
    "delegate_for": "delegate",
}

ADULT_SENSITIVE_RULES: Final[dict[Capability, tuple[str, tuple[PolicyObligationCode, ...]]]] = {
    "voice_profile_create": (
        "voice_profile",
        (
            "REQUIRE_STEP_UP_AUTH",
            "REQUIRE_SUBJECT_APPROVAL",
            "WRITE_POLICY_RECEIPT",
            "NO_MODEL_TRAINING",
        ),
    ),
    "voice_clone_use": (
        "voice_clone",
        (
            "REQUIRE_STEP_UP_AUTH",
            "REQUIRE_SUBJECT_APPROVAL",
            "WRITE_POLICY_RECEIPT",
            "NO_MODEL_TRAINING",
        ),
    ),
    "digital_self_preview": (
        "digital_self",
        (
            "REQUIRE_STEP_UP_AUTH",
            "REQUIRE_SUBJECT_APPROVAL",
            "AI_IDENTITY_CLARIFICATION",
            "WRITE_POLICY_RECEIPT",
        ),
    ),
    "legacy_grant_create": (
        "legacy",
        (
            "REQUIRE_STEP_UP_AUTH",
            "REQUIRE_SUBJECT_APPROVAL",
            "AI_IDENTITY_CLARIFICATION",
            "WRITE_POLICY_RECEIPT",
        ),
    ),
    "payment": (
        "payment",
        (
            "REQUIRE_STEP_UP_AUTH",
            "REQUIRE_SUBJECT_APPROVAL",
            "WRITE_POLICY_RECEIPT",
        ),
    ),
    "raw_audio_retention": (
        "raw_audio",
        (
            "REQUIRE_STEP_UP_AUTH",
            "REQUIRE_SUBJECT_APPROVAL",
            "RETENTION_TTL",
            "NO_MODEL_TRAINING",
            "WRITE_POLICY_RECEIPT",
        ),
    ),
    "model_training_contribution": (
        "model_training",
        (
            "REQUIRE_STEP_UP_AUTH",
            "REQUIRE_SUBJECT_APPROVAL",
            "RETENTION_TTL",
            "WRITE_POLICY_RECEIPT",
        ),
    ),
    "device_ownership_transfer": (
        "device_transfer",
        (
            "REQUIRE_STEP_UP_AUTH",
            "REQUIRE_SUBJECT_APPROVAL",
            "WRITE_POLICY_RECEIPT",
        ),
    ),
}


def _params(
    *,
    max_session_seconds: int | None = None,
    retention_ttl_seconds: int | None = None,
    quiet_hours: tuple[str, str] | None = None,
    extras: tuple[tuple[str, str], ...] = (),
) -> GeneratedObligationParams:
    return GeneratedObligationParams(
        max_session_seconds=max_session_seconds,
        retention_ttl_seconds=retention_ttl_seconds,
        quiet_hours=quiet_hours,
        extras=extras,
    )


_OBLIGATION_PARAMS: Final[dict[PolicyObligationCode, GeneratedObligationParams]] = {
    "MAX_SESSION_SECONDS": _params(max_session_seconds=1800),
    "QUIET_HOURS": _params(quiet_hours=("21:30", "06:30")),
    "RETENTION_TTL": _params(retention_ttl_seconds=30 * 24 * 3600),
}


def _obligations(*codes: PolicyObligationCode) -> tuple[PolicyObligationSpec, ...]:
    return tuple(
        PolicyObligationSpec(
            code=GeneratedPolicyObligation(code),
            params=_OBLIGATION_PARAMS.get(code, _params()),
        )
        for code in codes
    )


def _select_consent(
    consents: tuple[ConsentEvidencePort, ...],
) -> ConsentEvidencePort:
    """Select one effective grant independently of caller tuple ordering."""
    return min(
        consents,
        key=lambda evidence: (
            evidence.snapshot_id,
            evidence.version,
            evidence.canonical_hash,
        ),
    )


def _current_consent_snapshots(
    context: PolicyContext,
    consents: tuple[ConsentEvidencePort, ...],
) -> tuple[ConsentSnapshotEvidencePort, ...]:
    """Resolve selected historical grants through current global snapshot heads."""
    if not consents:
        return ()
    snapshots = tuple(
        sorted(
            (
                snapshot
                for snapshot in context.consent_snapshot_evidence
                if snapshot.subject_id == context.subject_id
                and snapshot.binding_id == context.binding_id
                and snapshot.binding_version == context.binding_version
                and snapshot.is_current_at(context.evaluated_at)
                and any(snapshot.contains(consent) for consent in consents)
            ),
            key=lambda item: (item.snapshot_id, item.revision, item.canonical_hash),
        )
    )
    if any(not any(snapshot.contains(consent) for snapshot in snapshots) for consent in consents):
        return ()
    return snapshots


def _select_relationship(
    relationships: tuple[RelationshipEvidencePort, ...],
) -> RelationshipEvidencePort:
    """Select one active relationship independently of caller tuple ordering."""
    return min(
        relationships,
        key=lambda evidence: (
            evidence.snapshot_id,
            evidence.revision,
            evidence.canonical_hash,
        ),
    )


@dataclass(frozen=True, slots=True)
class _PolicyEvaluation:
    effect: PolicyEffect
    reason_code: str
    obligations: tuple[PolicyObligationSpec, ...] = ()
    consents: tuple[ConsentEvidencePort, ...] = ()
    consent_snapshots: tuple[ConsentSnapshotEvidencePort, ...] = ()
    relationships: tuple[RelationshipEvidencePort, ...] = ()
    binding_evidence: BindingEvidencePort | None = None


@dataclass(frozen=True, slots=True)
class _EvaluationBundle:
    decision: PolicyDecision
    evaluation: _PolicyEvaluation


class PolicyEngine:
    """Return a deterministic, evidence-fenced decision; callers execute
    obligations (the engine itself never executes anything)."""

    def __init__(
        self,
        *,
        policy_version: str = "multi-subject-v2",
        receipt_ttl: timedelta = timedelta(minutes=5),
        receipt_id_factory: Callable[[], str] | None = None,
        receipt_writer: PolicyReceiptWriterV2 | None = None,
    ) -> None:
        if not policy_version.strip():
            raise ValueError("policy_version must not be empty")
        if receipt_ttl <= timedelta(0):
            raise ValueError("receipt_ttl must be positive")
        self.policy_version = policy_version
        self.receipt_ttl = receipt_ttl
        self._receipt_id_factory = receipt_id_factory or (lambda: str(uuid.uuid4()))
        # Compatibility-only synchronous seam for unit tests and the existing
        # InMemoryPolicyReceiptWriter. Production persistence belongs to the
        # async repository/DecisionService boundary; the engine never adapts or
        # waits on async writers.
        self._receipt_writer = receipt_writer

    def _evaluate(self, context: PolicyContext) -> _PolicyEvaluation:
        if is_resource_scoped_action(context.capability):
            return self._decide_action_resource(context)
        if context.capability == "crisis_notification":
            return self._decide_crisis(context)
        if context.capability == "guardian_summary_view":
            return self._decide_guardian_summary(context)
        if context.subject_id is None or context.speaker_state != "confirmed":
            return self._decide_unconfirmed(context)
        if context.subject_category == "unknown":
            return self._decide_category_unknown(context)
        if context.subject_category == "minor":
            return self._decide_minor(context)
        return self._decide_adult(context)

    def evaluate_bundle(self, context: PolicyContext) -> _EvaluationBundle:
        """Compute private evidence participation plus canonical public decision."""
        evaluation = self._evaluate_authoritative(context)
        decision = self._decision_for(context, evaluation)
        return _EvaluationBundle(decision=decision, evaluation=evaluation)

    def _evaluate_authoritative(self, context: PolicyContext) -> _PolicyEvaluation:
        evaluation = self._evaluate(context)
        if (
            evaluation.effect in {"allow", "allow_with_obligations"}
            and evaluation.consents
            and not evaluation.consent_snapshots
        ):
            evaluation = _PolicyEvaluation(
                effect="deny",
                reason_code="current_consent_snapshot_required",
            )
        return evaluation

    def decide(self, context: PolicyContext) -> PolicyDecision:
        bundle = self.evaluate_bundle(context)
        if self._receipt_writer is not None:
            self._receipt_writer.write(
                self._receipt_for_evaluation(
                    context,
                    bundle.decision,
                    bundle.evaluation,
                )
            )
        return bundle.decision

    def deny(self, context: PolicyContext, *, reason_code: str) -> PolicyDecision:
        """Record an explicit fail-closed decision at an outer authority fence."""
        evaluation = self._decision(context, effect="deny", reason_code=reason_code)
        decision = self._decision_for(context, evaluation)
        if self._receipt_writer is not None:
            self._receipt_writer.write(self._receipt_for_evaluation(context, decision, evaluation))
        return decision

    def receipt_for(
        self,
        context: PolicyContext,
        decision: PolicyDecision,
    ) -> PolicyReceiptV2:
        """Build the immutable V2 receipt for a decision.

        DecisionService persists this receipt before exposing the decision to
        production callers; this method itself performs no durable write.
        """
        evaluation = self._evaluate_authoritative(context)
        expected = self._decision_for(
            context,
            evaluation,
            receipt_id=decision.receipt_id,
            require_guard=False,
        )
        if expected != decision:
            raise ValueError("decision does not match the evaluated policy context")
        return self._receipt_for_evaluation(context, decision, evaluation)

    def receipt_id_for(self, context: PolicyContext) -> str:
        """Return the deterministic receipt identity for one policy context.

        Action executors use this before evaluating mutable evidence so an
        exact retry can lock and replay the already-persisted immutable
        receipt.  The underlying identity deliberately excludes evaluation
        time and evidence snapshots while retaining the caller's stable
        idempotency key and authority scope.
        """
        return self._receipt_id(context)

    def _receipt_for_evaluation(
        self,
        context: PolicyContext,
        decision: PolicyDecision,
        evaluation: _PolicyEvaluation,
    ) -> PolicyReceiptV2:
        require_policy_producer("PolicyReceiptV2")
        binding = evaluation.binding_evidence or context.binding_evidence
        return PolicyReceiptV2.model_validate(
            {
                "receipt_id": decision.receipt_id,
                "actor_id": context.actor_id,
                "subject_id": context.subject_id,
                "resource_owner_id": context.resource_owner_id,
                "device_id": context.device_id,
                "capability": context.capability,
                "purpose": context.purpose,
                "effect": decision.effect.value,
                "reason_code": decision.reason_code,
                "obligations": [item.model_dump(mode="json") for item in decision.obligations],
                "policy_version": decision.policy_version,
                "context_hash": decision.context_hash,
                "action_resource_fence": decision.action_resource_fence.model_dump(mode="json"),
                "action_fence_hash": decision.action_fence_hash,
                "consent_snapshot_ids": [item.snapshot_id for item in evaluation.consent_snapshots],
                "consent_snapshot_revisions": [
                    item.revision for item in evaluation.consent_snapshots
                ],
                "relationship_snapshot_ids": [
                    item.snapshot_id for item in evaluation.relationships
                ],
                "relationship_snapshot_revisions": [
                    item.revision for item in evaluation.relationships
                ],
                "binding_id": context.binding_id,
                "binding_version": context.binding_version,
                "binding_canonical_hash": (binding.canonical_hash if binding is not None else None),
                "session_id": context.session_id,
                "session_epoch": context.session_epoch,
                "runtime_profile_id": context.runtime_profile_id,
                "subject_revision": context.subject_revision,
                "device_trust": context.device_trust,
                "data_classification": context.data_classification,
                "safety_state": context.safety_state,
                "jurisdiction": context.jurisdiction,
                "created_at": _rfc3339(decision.created_at),
                "expires_at": _rfc3339(decision.expires_at),
                "exact_fence": binding is not None,
            }
        )

    def receipt_valid(
        self,
        decision: PolicyDecision,
        *,
        context: PolicyContext,
        now: datetime,
    ) -> bool:
        return (
            decision.policy_version == self.policy_version
            and decision.context_hash == context_hash(context)
            and decision.created_at is not None
            and decision.expires_at is not None
            and decision.created_at <= now < decision.expires_at
        )

    # ------------------------------------------------------------------
    # matrix paths
    # ------------------------------------------------------------------

    def _decide_action_resource(self, context: PolicyContext) -> _PolicyEvaluation:
        fence = context.action_resource_fence
        if (
            context.subject_id is None
            or context.speaker_state != "confirmed"
            or context.actor_id != context.subject_id
        ):
            return self._decision(context, effect="deny", reason_code="action_subject_mismatch")
        if fence is None:
            return self._decision(context, effect="deny", reason_code="action_fence_required")
        if context.device_trust not in TRUSTED_DEVICE_TRUSTS:
            return self._decision(context, effect="deny", reason_code="device_untrusted")
        if not binding_fence_ok(
            context.binding_id,
            context.binding_version,
            context.evaluated_at,
            context.binding_evidence,
        ):
            return self._decision(context, effect="deny", reason_code="binding_evidence_required")
        consents = tuple(
            consent
            for consent in effective_consents_for(
                context.subject_id,
                context.binding_id,
                context.binding_version,
                context.capability,
                context.evaluated_at,
                context.consent_evidence,
            )
            if consent.purpose == context.purpose
            and consent.resource_owner_id == context.resource_owner_id
        )
        consent_snapshots = _current_consent_snapshots(context, consents)
        if not consents or not any(
            snapshot.snapshot_id == fence.consent_snapshot_id
            and snapshot.revision == fence.consent_snapshot_revision
            and snapshot.canonical_hash == fence.consent_snapshot_hash
            for snapshot in consent_snapshots
        ):
            return self._decision(
                context, effect="deny", reason_code="action_consent_fence_invalid"
            )
        if context.capability == "memory_promotion":
            if context.resource_owner_id != context.subject_id:
                return self._decision(context, effect="deny", reason_code="resource_owner_required")
            return self._decision(
                context,
                effect="allow_with_obligations",
                reason_code="memory_promotion_authorized",
                obligations=_obligations("WRITE_POLICY_RECEIPT", "NO_MODEL_TRAINING"),
                consents=(_select_consent(consents),),
                binding_evidence=context.binding_evidence,
            )

        membership = context.membership_evidence
        if (
            membership is None
            or not membership.is_active_at(context.evaluated_at)
            or membership.snapshot_id != fence.membership_snapshot_id
            or membership.revision != fence.membership_snapshot_revision
            or membership.canonical_hash != fence.membership_snapshot_hash
            or membership.family_space_id != fence.family_space_id
            or membership.family_owner_subject_id != fence.family_owner_subject_id
            or membership.binding_id != context.binding_id
            or membership.binding_version != context.binding_version
            or context.resource_owner_id != fence.family_owner_subject_id
            or context.actor_id not in membership.subject_ids
            or not set(fence.required_approval_subject_ids).issubset(membership.subject_ids)
        ):
            return self._decision(context, effect="deny", reason_code="membership_fence_invalid")

        if context.capability == "family_shared_memory_proposal":
            captures = context.capture_evidence
            records = tuple(
                (item.evidence_id, item.revision, item.canonical_hash)
                for item in captures
                if item.is_active_at(context.evaluated_at)
                and item.subject_id == context.subject_id
                and item.binding_id == context.binding_id
                and item.binding_version == context.binding_version
            )
            if (
                not records
                or tuple(sorted(item[0] for item in records)) != fence.capture_evidence_ids
                or capture_authority_hash(records) != fence.capture_evidence_hash
            ):
                return self._decision(
                    context, effect="deny", reason_code="capture_evidence_invalid"
                )
            reason_code = "family_shared_memory_proposal_authorized"
        else:
            proposal = context.proposal_evidence
            if (
                proposal is None
                or not proposal.is_active_at(context.evaluated_at)
                or proposal.proposal_id != fence.proposal_id
                or proposal.revision != fence.proposal_revision
                or proposal.family_space_id != fence.family_space_id
                or proposal.family_owner_subject_id != fence.family_owner_subject_id
                or set(proposal.required_approval_subject_ids)
                != set(fence.required_approval_subject_ids)
            ):
                return self._decision(context, effect="deny", reason_code="proposal_fence_invalid")
            if context.capability == "family_shared_memory_approval":
                if (
                    fence.voter_subject_id != context.actor_id
                    or fence.approval_decision is None
                    or fence.approval_decision.value != "confirm"
                ):
                    return self._decision(
                        context, effect="deny", reason_code="approval_voter_mismatch"
                    )
                reason_code = "family_shared_memory_approval_authorized"
            else:
                current = {
                    (item.subject_id, item.snapshot_id, item.revision, item.canonical_hash)
                    for item in context.approval_evidence
                    if item.is_active_at(context.evaluated_at)
                    and item.proposal_id == fence.proposal_id
                    and item.proposal_revision == fence.proposal_revision
                    and item.decision == "confirm"
                }
                fenced = {
                    (item.subject_id, item.snapshot_id, item.revision, item.canonical_hash)
                    for item in fence.approval_snapshots
                }
                if current != fenced:
                    return self._decision(
                        context, effect="deny", reason_code="approval_set_invalid"
                    )
                reason_code = "family_shared_memory_promotion_authorized"
        return self._decision(
            context,
            effect="allow_with_obligations",
            reason_code=reason_code,
            obligations=_obligations("WRITE_POLICY_RECEIPT", "NO_MODEL_TRAINING"),
            consents=(_select_consent(consents),),
            binding_evidence=context.binding_evidence,
        )

    def _decide_crisis(self, context: PolicyContext) -> _PolicyEvaluation:
        if context.purpose != canonical_runtime_purpose_for_capability(
            context.capability
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="consent_purpose_mismatch",
            )
        if context.subject_id is None or context.speaker_state != "confirmed":
            return self._decision(
                context,
                effect="deny",
                reason_code="crisis_subject_unconfirmed",
            )
        if context.safety_state not in CRISIS_SIGNAL_STATES:
            return self._decision(
                context,
                effect="deny",
                reason_code="crisis_notification_forbidden",
            )
        # Emergency path fails closed on untrusted/revoked devices; "offline"
        # (degraded but previously verified) stays evaluable (§10.6).
        if context.device_trust in {"untrusted", "revoked"}:
            return self._decision(
                context,
                effect="deny",
                reason_code="device_untrusted",
            )
        allowed_types = CRISIS_RELATION_RULES.get(
            (context.subject_category, context.current_session_mode), frozenset()
        )
        relationships = tuple(
            relationship
            for relationship in context.relationship_evidence
            if relationship.relation_type in allowed_types
            and relationship.target_person_id == context.subject_id
            and relationship.is_active_at(context.evaluated_at)
        )
        if not relationships:
            return self._decision(
                context,
                effect="deny",
                reason_code="crisis_notification_forbidden",
            )
        if not binding_fence_ok(
            context.binding_id,
            context.binding_version,
            context.evaluated_at,
            context.binding_evidence,
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="binding_evidence_required",
            )
        relationship = _select_relationship(relationships)
        # The engine only produces minimal-notification obligations; it never
        # executes the notification itself.
        return self._decision(
            context,
            effect="allow_with_obligations",
            reason_code="crisis_notification_authorized",
            obligations=(
                PolicyObligationSpec(
                    code=GeneratedPolicyObligation.POLICY_OBLIGATION_NOTIFY_EMERGENCY_CONTACT,
                    params=_params(
                        extras=(
                            ("intent_kind", "crisis_notification"),
                            (
                                "recipient_role",
                                CRISIS_RECIPIENT_ROLES[relationship.relation_type],
                            ),
                        )
                    ),
                ),
                *_obligations(
                    "MINIMAL_NOTIFICATION_CONTENT",
                    "WRITE_POLICY_RECEIPT",
                ),
            ),
            relationships=(relationship,),
            binding_evidence=context.binding_evidence,
        )

    def _decide_guardian_summary(self, context: PolicyContext) -> _PolicyEvaluation:
        if context.purpose != canonical_runtime_purpose_for_capability(
            context.capability
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="consent_purpose_mismatch",
            )
        if context.subject_category != "minor" or context.actor_id == context.subject_id:
            return self._decision(
                context,
                effect="deny",
                reason_code="guardian_summary_forbidden",
            )
        if context.resource_owner_id != context.subject_id:
            return self._decision(
                context,
                effect="deny",
                reason_code="resource_owner_required",
            )
        guardians = active_relationship_for(
            "guardian_of",
            context.subject_id or "",
            context.evaluated_at,
            context.relationship_evidence,
        )
        actor_guardians = tuple(
            guardian
            for guardian in guardians
            if is_guardian_of(
                guardian,
                context.actor_id,
                context.subject_id or "",
            )
        )
        if not actor_guardians:
            return self._decision(
                context,
                effect="deny",
                reason_code="guardian_summary_forbidden",
            )
        consents = effective_consents_for(
            context.subject_id or "",
            context.binding_id,
            context.binding_version,
            "guardian_summary_view",
            context.evaluated_at,
            context.consent_evidence,
        )
        if not consents:
            return self._decision(
                context,
                effect="deny",
                reason_code="guardian_summary_forbidden",
            )
        consents = tuple(consent for consent in consents if consent.purpose == context.purpose)
        if not consents:
            return self._decision(
                context,
                effect="deny",
                reason_code="consent_purpose_mismatch",
            )
        if context.device_trust not in TRUSTED_DEVICE_TRUSTS:
            return self._decision(
                context,
                effect="deny",
                reason_code="device_untrusted",
            )
        if not binding_fence_ok(
            context.binding_id,
            context.binding_version,
            context.evaluated_at,
            context.binding_evidence,
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="binding_evidence_required",
            )
        return self._decision(
            context,
            effect="allow_with_obligations",
            reason_code="guardian_summary_authorized",
            obligations=_obligations(
                "REDACT_TRANSCRIPT",
                "MINIMAL_NOTIFICATION_CONTENT",
                "WRITE_POLICY_RECEIPT",
            ),
            consents=(_select_consent(consents),),
            relationships=(_select_relationship(actor_guardians),),
            binding_evidence=context.binding_evidence,
        )

    def _decide_unconfirmed(self, context: PolicyContext) -> _PolicyEvaluation:
        if context.capability == "chat":
            return self._decision(
                context,
                effect="allow_with_obligations",
                reason_code="unknown_safe_ephemeral",
                obligations=_obligations(
                    "DO_NOT_PERSIST",
                    "DO_NOT_WRITE_LEARNING_PROGRESS",
                    "NO_MODEL_TRAINING",
                    "REQUIRE_SPEAKER_CONFIRMATION",
                ),
            )
        if context.capability == "english_practice":
            return self._decision(
                context,
                effect="allow_with_obligations",
                reason_code="unknown_safe_ephemeral",
                obligations=_obligations(
                    "DO_NOT_PERSIST",
                    "DO_NOT_WRITE_LEARNING_PROGRESS",
                    "NO_MODEL_TRAINING",
                    "REQUIRE_SPEAKER_CONFIRMATION",
                ),
            )
        return self._decision(
            context,
            effect="deny",
            reason_code="subject_unconfirmed",
        )

    def _decide_category_unknown(self, context: PolicyContext) -> _PolicyEvaluation:
        if context.capability in {"chat", "english_practice"}:
            return self._decide_unconfirmed(context)
        return self._decision(
            context,
            effect="deny",
            reason_code="subject_category_unverified",
        )

    def _decide_minor(self, context: PolicyContext) -> _PolicyEvaluation:
        if context.capability in MINOR_HARD_FORBIDDEN:
            return self._decision(
                context,
                effect="deny",
                reason_code="minor_capability_forbidden",
            )
        if context.capability in MINOR_DEFAULT_DENIED:
            return self._decision(
                context,
                effect="deny",
                reason_code="minor_capability_denied",
            )
        if context.capability not in MINOR_GOVERNED_ALLOWED:
            return self._decision(
                context,
                effect="deny",
                reason_code="capability_forbidden",
            )
        if (
            context.capability in SENSITIVE_CAPABILITIES
            and context.purpose
            != canonical_runtime_purpose_for_capability(context.capability)
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="consent_purpose_mismatch",
            )
        subject_id = context.subject_id or ""
        if context.capability == "memory_recall_private" and (
            context.actor_id != subject_id or context.resource_owner_id != subject_id
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="private_memory_subject_only",
            )
        if context.capability == "memory_capture" and (
            context.actor_id != subject_id or context.resource_owner_id != subject_id
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="memory_consent_required",
            )
        if context.capability == "tutor" and context.actor_id != subject_id:
            return self._decision(
                context,
                effect="deny",
                reason_code="tutor_subject_mismatch",
            )
        if (
            context.capability
            in {"memory_capture", "memory_recall_private", "voice_profile_create"}
            and context.resource_owner_id != subject_id
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="resource_owner_required",
            )
        if (
            context.capability in SENSITIVE_CAPABILITIES
            and context.device_trust not in TRUSTED_DEVICE_TRUSTS
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="device_untrusted",
            )
        consents = effective_consents_for(
            subject_id,
            context.binding_id,
            context.binding_version,
            context.capability,
            context.evaluated_at,
            context.consent_evidence,
        )
        if not consents:
            reason = (
                "memory_consent_required"
                if context.capability in {"memory_capture", "memory_recall_private"}
                else "consent_evidence_required"
            )
            return self._decision(context, effect="deny", reason_code=reason)
        if context.capability in SENSITIVE_CAPABILITIES:
            consents = tuple(consent for consent in consents if consent.purpose == context.purpose)
            if not consents:
                return self._decision(
                    context,
                    effect="deny",
                    reason_code="consent_purpose_mismatch",
                )
        guardians = active_relationship_for(
            "guardian_of",
            subject_id,
            context.evaluated_at,
            context.relationship_evidence,
        )
        if not guardians:
            return self._decision(
                context,
                effect="deny",
                reason_code="guardian_evidence_required",
            )
        if not binding_fence_ok(
            context.binding_id,
            context.binding_version,
            context.evaluated_at,
            context.binding_evidence,
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="binding_evidence_required",
            )
        evidence_args = {
            "consents": (_select_consent(consents),),
            "relationships": (_select_relationship(guardians),),
            "binding_evidence": context.binding_evidence,
        }
        if context.capability == "chat":
            return self._decision(
                context,
                effect="allow_with_obligations",
                reason_code="minor_companion_guarded",
                obligations=_obligations(
                    "MAX_SESSION_SECONDS",
                    "QUIET_HOURS",
                    "DEPENDENCY_GUARD",
                    "AI_IDENTITY_CLARIFICATION",
                    "NO_MODEL_TRAINING",
                ),
                **evidence_args,  # type: ignore[arg-type]
            )
        if context.capability == "english_practice":
            return self._decision(
                context,
                effect="allow_with_obligations",
                reason_code="minor_english_practice_guarded",
                obligations=_obligations(
                    "WRITE_SUBJECT_SCOPED_PROGRESS",
                    "MAX_SESSION_SECONDS",
                    "QUIET_HOURS",
                    "NO_MODEL_TRAINING",
                    "WRITE_POLICY_RECEIPT",
                ),
                **evidence_args,  # type: ignore[arg-type]
            )
        if context.capability == "tutor":
            return self._decision(
                context,
                effect="allow_with_obligations",
                reason_code="minor_tutor_guarded",
                obligations=_obligations(
                    "WRITE_SUBJECT_SCOPED_PROGRESS",
                    "MAX_SESSION_SECONDS",
                    "QUIET_HOURS",
                    "NO_MODEL_TRAINING",
                    "WRITE_POLICY_RECEIPT",
                ),
                **evidence_args,  # type: ignore[arg-type]
            )
        if context.capability == "memory_capture":
            return self._decision(
                context,
                effect="allow_with_obligations",
                reason_code="minor_memory_minimized",
                obligations=_obligations(
                    "PERSIST_AGGREGATE_ONLY",
                    "RETENTION_TTL",
                    "NO_MODEL_TRAINING",
                    "WRITE_POLICY_RECEIPT",
                ),
                **evidence_args,  # type: ignore[arg-type]
            )
        if context.capability == "memory_recall_private":
            return self._decision(
                context,
                effect="allow_with_obligations",
                reason_code="private_memory_subject_authorized",
                obligations=_obligations("WRITE_POLICY_RECEIPT"),
                **evidence_args,  # type: ignore[arg-type]
            )
        # voice_profile_create: voiceprint enrollment only (not cloning).
        return self._decision(
            context,
            effect="allow_with_obligations",
            reason_code="minor_voice_profile_enrolled",
            obligations=_obligations(
                "NO_MODEL_TRAINING",
                "WRITE_POLICY_RECEIPT",
            ),
            **evidence_args,  # type: ignore[arg-type]
        )

    def _decide_adult(self, context: PolicyContext) -> _PolicyEvaluation:
        if context.capability == "chat":
            return self._decision(
                context,
                effect="allow",
                reason_code="companion_chat_allowed",
            )
        if context.capability == "english_practice":
            return self._decision(
                context,
                effect="allow_with_obligations",
                reason_code="adult_english_practice_authorized",
                obligations=_obligations(
                    "WRITE_SUBJECT_SCOPED_PROGRESS",
                    "WRITE_POLICY_RECEIPT",
                ),
            )
        if context.capability == "tutor":
            if context.actor_id != context.subject_id:
                return self._decision(
                    context,
                    effect="deny",
                    reason_code="tutor_subject_mismatch",
                )
            return self._decision(
                context,
                effect="allow_with_obligations",
                reason_code="tutor_subject_authorized",
                obligations=_obligations(
                    "WRITE_SUBJECT_SCOPED_PROGRESS",
                    "WRITE_POLICY_RECEIPT",
                ),
            )
        if context.capability == "memory_recall_private":
            return self._decide_adult_memory(context, recall=True)
        if context.capability == "memory_capture":
            return self._decide_adult_memory(context, recall=False)
        sensitive_rule = ADULT_SENSITIVE_RULES.get(context.capability)
        if sensitive_rule is not None:
            _, obligations = sensitive_rule
            return self._decide_adult_sensitive(
                context,
                obligations=_obligations(*obligations),
            )
        return self._decision(
            context,
            effect="deny",
            reason_code="capability_forbidden",
        )

    def _decide_adult_memory(
        self,
        context: PolicyContext,
        *,
        recall: bool,
    ) -> _PolicyEvaluation:
        subject_id = context.subject_id or ""
        if context.purpose != canonical_runtime_purpose_for_capability(
            context.capability
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="consent_purpose_mismatch",
            )
        if context.actor_id != subject_id or context.resource_owner_id != subject_id:
            reason = "private_memory_subject_only" if recall else "memory_consent_required"
            return self._decision(context, effect="deny", reason_code=reason)
        if context.device_trust not in TRUSTED_DEVICE_TRUSTS:
            return self._decision(
                context,
                effect="deny",
                reason_code="device_untrusted",
            )
        consents = effective_consents_for(
            subject_id,
            context.binding_id,
            context.binding_version,
            context.capability,
            context.evaluated_at,
            context.consent_evidence,
        )
        if not consents:
            return self._decision(
                context,
                effect="deny",
                reason_code="memory_consent_required",
            )
        consents = tuple(consent for consent in consents if consent.purpose == context.purpose)
        if not consents:
            return self._decision(
                context,
                effect="deny",
                reason_code="consent_purpose_mismatch",
            )
        if not binding_fence_ok(
            context.binding_id,
            context.binding_version,
            context.evaluated_at,
            context.binding_evidence,
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="binding_evidence_required",
            )
        if recall:
            return self._decision(
                context,
                effect="allow_with_obligations",
                reason_code="private_memory_subject_authorized",
                obligations=_obligations("WRITE_POLICY_RECEIPT"),
                consents=(_select_consent(consents),),
                binding_evidence=context.binding_evidence,
            )
        return self._decision(
            context,
            effect="allow_with_obligations",
            reason_code="adult_memory_authorized",
            obligations=_obligations(
                "RETENTION_TTL",
                "NO_MODEL_TRAINING",
                "WRITE_POLICY_RECEIPT",
            ),
            consents=(_select_consent(consents),),
            binding_evidence=context.binding_evidence,
        )

    def _decide_adult_sensitive(
        self,
        context: PolicyContext,
        *,
        obligations: tuple[PolicyObligationSpec, ...],
    ) -> _PolicyEvaluation:
        subject_id = context.subject_id or ""
        if context.purpose != canonical_runtime_purpose_for_capability(
            context.capability
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="consent_purpose_mismatch",
            )
        if context.actor_id != subject_id or context.resource_owner_id != subject_id:
            return self._decision(
                context,
                effect="deny",
                reason_code="subject_consent_required",
            )
        if context.device_trust not in TRUSTED_DEVICE_TRUSTS:
            return self._decision(
                context,
                effect="deny",
                reason_code="device_untrusted",
            )
        consents = effective_consents_for(
            subject_id,
            context.binding_id,
            context.binding_version,
            context.capability,
            context.evaluated_at,
            context.consent_evidence,
        )
        if not consents:
            return self._decision(
                context,
                effect="deny",
                reason_code="subject_consent_required",
            )
        purpose_consents = tuple(
            consent for consent in consents if consent.purpose == context.purpose
        )
        if not purpose_consents:
            return self._decision(
                context,
                effect="deny",
                reason_code="consent_purpose_mismatch",
            )
        consent = _select_consent(purpose_consents)
        declared_classification = dict(consent.params.extras).get("data_classification")
        if (
            declared_classification is not None
            and declared_classification != context.data_classification
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="consent_classification_mismatch",
            )
        if not binding_fence_ok(
            context.binding_id,
            context.binding_version,
            context.evaluated_at,
            context.binding_evidence,
        ):
            return self._decision(
                context,
                effect="deny",
                reason_code="binding_evidence_required",
            )
        return self._decision(
            context,
            effect="allow_with_obligations",
            reason_code="adult_subject_authorized",
            obligations=obligations,
            consents=(consent,),
            binding_evidence=context.binding_evidence,
        )

    def _decision(
        self,
        context: PolicyContext,
        *,
        effect: PolicyEffect,
        reason_code: str,
        obligations: tuple[PolicyObligationSpec, ...] = (),
        consents: tuple[ConsentEvidencePort, ...] = (),
        relationships: tuple[RelationshipEvidencePort, ...] = (),
        binding_evidence: BindingEvidencePort | None = None,
    ) -> _PolicyEvaluation:
        return _PolicyEvaluation(
            effect=effect,
            reason_code=reason_code,
            obligations=obligations,
            consents=consents,
            consent_snapshots=_current_consent_snapshots(context, consents),
            relationships=relationships,
            binding_evidence=binding_evidence,
        )

    def _decision_for(
        self,
        context: PolicyContext,
        evaluation: _PolicyEvaluation,
        *,
        receipt_id: str | None = None,
        require_guard: bool = True,
    ) -> PolicyDecision:
        if require_guard:
            require_policy_producer("PolicyDecision")
        action_fence = effective_action_resource_fence(context)
        return PolicyDecision.model_validate(
            {
                "capability": context.capability,
                "purpose": context.purpose,
                "effect": evaluation.effect,
                "reason_code": evaluation.reason_code,
                "obligations": [item.model_dump(mode="json") for item in evaluation.obligations],
                "policy_version": self.policy_version,
                "receipt_id": receipt_id or self._receipt_id(context),
                "context_hash": context_hash(context),
                "action_resource_fence": action_fence.model_dump(mode="json"),
                "action_fence_hash": action_fence.canonical_hash,
                "created_at": _rfc3339(context.evaluated_at),
                "expires_at": _rfc3339(context.evaluated_at + self.receipt_ttl),
            }
        )

    def _receipt_id(self, context: PolicyContext) -> str:
        if context.idempotency_key is None:
            return self._receipt_id_factory()
        # Scope isolates a caller's key globally while deliberately excluding
        # retry time, evidence snapshots and mutable runtime observations.
        action_fence = effective_action_resource_fence(context)
        scope = (
            self.policy_version,
            context.actor_id,
            context.subject_id,
            context.resource_owner_id,
            context.device_id,
            context.binding_id,
            context.binding_version,
            context.session_id,
            context.capability,
            context.purpose,
            action_fence.action_resource_id,
            action_fence.action_revision,
            action_fence.family_space_id,
            action_fence.family_owner_subject_id,
            action_fence.proposal_id,
            action_fence.proposal_revision,
            action_fence.voter_subject_id,
        )
        material = json.dumps(
            (scope, context.idempotency_key),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"receipt-{hashlib.sha256(material).hexdigest()}"
