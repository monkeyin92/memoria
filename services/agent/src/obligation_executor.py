"""Code-level execution of canonical persistence obligations (P0-6).

Policy obligations are not prompt decoration: DO_NOT_PERSIST, aggregate-only
and no-training obligations must be enforced where evidence is written.  UI
subtitles stay live, but the long-term archive/evidence sinks only receive
what the signed RuntimeProfile allows, always annotated with the frozen
subject/profile/receipt identity (remediation doc 4.3/6.2).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from services.agent.src.receipt_evidence import (
    VerifiedReceiptEvidence,
    evidence_from_policy_receipt,
)
from services.agent.src.runtime_profile import VerifiedRuntimeProfile
from services.policy.receipts import PolicyReceiptV2

_AGGREGATE_OBLIGATIONS = frozenset({"PERSIST_AGGREGATE_ONLY", "REDACT_TRANSCRIPT"})
# Subjects whose own memory grant is enough to archive their turns.  A minor is
# in only because Policy issues the grant to a child solely under an active
# guardian memory consent.
_GRANT_SUBJECT_CATEGORIES = frozenset({"adult", "minor"})
_ACTION_PERSISTENCE_CAPABILITIES = frozenset(
    {"memory_capture", "raw_audio_retention", "model_training_contribution"}
)
_EXECUTABLE_ACTION_OBLIGATIONS = frozenset(
    {
        "DO_NOT_PERSIST",
        "PERSIST_AGGREGATE_ONLY",
        "REDACT_TRANSCRIPT",
        "NO_MODEL_TRAINING",
        "RETENTION_TTL",
        # The Session action-policy transaction has already inserted and
        # locked this immutable receipt before returning it. The commit port
        # receives its id in evidence_refs and revalidates it transactionally.
        "WRITE_POLICY_RECEIPT",
    }
)


@dataclass(frozen=True, slots=True)
class PersistenceDecision:
    allowed: bool
    aggregate_only: bool = False
    no_model_training: bool = True
    raw_audio_allowed: bool = False
    device_id: str | None = None
    subject_revision: int | None = None
    subject_id: str | None = None
    runtime_profile_id: str | None = None
    session_epoch: int = 0
    actor_id: str | None = None
    binding_id: str | None = None
    binding_version: int | None = None
    memory_scope: str = "unknown"
    memory_capture_receipt_id: str | None = None
    raw_audio_receipt_id: str | None = None
    training_receipt_id: str | None = None


def decide_persistence(
    profile: VerifiedRuntimeProfile | None,
    *,
    memory_capture_evidence: VerifiedReceiptEvidence | None = None,
    raw_audio_evidence: VerifiedReceiptEvidence | None = None,
    training_evidence: VerifiedReceiptEvidence | None = None,
    action_obligations: Iterable[str] = (),
    session_memory_grant: bool = False,
) -> PersistenceDecision:
    """Decide whether (and how) evidence may be persisted for one fence.

    Memory persistence has two authorities.  One is a verified
    ``memory_capture`` receipt for a profile that lists the capability.  The
    other is ``session_memory_grant``: the caller has checked that the fence's
    signed profile grants ``memory_recall_private`` to its confirmed subject
    on the bound device.

    Control never lists ``memory_capture`` in a Runtime Profile: it is an
    action-time capability, and Control's action fence admits exactly one
    legal step per authorization, which cannot carry a stream of archive
    events.  So without the grant no production session could ever persist.

    Policy decides ``memory_recall_private`` and ``memory_capture`` under the
    same consent, delegation/guardian and trusted-device rules for an adult
    and for a minor, and both carry the same retention and no-training
    obligations, so the grant stands for either.  For a minor it exists only
    while the guardian's long-term-memory consent holds.  Control and the
    Agent both read it as history eligibility; the Archive re-verifies the
    profile, the subject's category and the guardian's active consent before
    it keeps anything, and compiles a minor's memory through its narrow
    projection filter.  A subject of unknown category never persists.
    """

    if profile is None:
        return PersistenceDecision(allowed=False)
    obligations = set(profile.profile.obligations) | set(action_obligations)
    capabilities = set(profile.profile.capabilities)
    # NO_MODEL_TRAINING defaults to true; only an explicit
    # model_training_contribution capability can ever make it false (audit 5).
    no_model_training = "NO_MODEL_TRAINING" in obligations or not (
        "model_training_contribution" in capabilities and training_evidence is not None
    )
    mode = profile.profile.service_mode
    memory_scope = {
        "family_shared": "family_shared",
        "student_minor": "personal_private",
        "adult_companion": "personal_private",
        "senior_companion": "personal_private",
        "unknown_safe": "unknown",
    }.get(mode, "unknown")
    common = PersistenceDecision(
        allowed=False,
        no_model_training=no_model_training,
        raw_audio_allowed=(
            "raw_audio_retention" in capabilities and raw_audio_evidence is not None
        ),
        device_id=profile.profile.device_id,
        subject_revision=profile.profile.subject_revision,
        subject_id=profile.profile.active_subject_id,
        runtime_profile_id=profile.profile.runtime_profile_id,
        session_epoch=profile.profile.session_epoch,
        actor_id=profile.profile.actor_id,
        binding_id=profile.profile.binding_id,
        binding_version=profile.profile.binding_version,
        memory_scope=memory_scope,
        memory_capture_receipt_id=(
            memory_capture_evidence.receipt_id
            if memory_capture_evidence is not None
            else None
        ),
        raw_audio_receipt_id=(
            raw_audio_evidence.receipt_id if raw_audio_evidence is not None else None
        ),
        training_receipt_id=(
            training_evidence.receipt_id if training_evidence is not None else None
        ),
    )
    receipt_authorized = (
        "memory_capture" in capabilities and memory_capture_evidence is not None
    )
    grant_authorized = (
        session_memory_grant
        and "memory_recall_private" in capabilities
        and profile.profile.speaker_state == "confirmed"
        and profile.profile.active_subject_id is not None
        and profile.profile.subject_category in _GRANT_SUBJECT_CATEGORIES
        and mode != "unknown_safe"
    )
    if "DO_NOT_PERSIST" in obligations or not (receipt_authorized or grant_authorized):
        return common
    return PersistenceDecision(
        allowed=True,
        aggregate_only=bool(_AGGREGATE_OBLIGATIONS & obligations),
        no_model_training=no_model_training,
        raw_audio_allowed=common.raw_audio_allowed,
        device_id=common.device_id,
        subject_revision=common.subject_revision,
        subject_id=common.subject_id,
        runtime_profile_id=common.runtime_profile_id,
        session_epoch=common.session_epoch,
        actor_id=common.actor_id,
        binding_id=common.binding_id,
        binding_version=common.binding_version,
        memory_scope=common.memory_scope,
        memory_capture_receipt_id=common.memory_capture_receipt_id,
        raw_audio_receipt_id=common.raw_audio_receipt_id,
        training_receipt_id=common.training_receipt_id,
    )


def execute_action_obligations(
    profile: VerifiedRuntimeProfile,
    receipt: PolicyReceiptV2,
) -> bool:
    """Execute the obligations attached to an action-time policy receipt.

    An ``allow_with_obligations`` receipt is not an allow shortcut.  The
    existing persistence executor is the only Agent-side implementation of
    these obligations, so persistence capabilities must pass it with the
    action receipt converted into verified evidence.  Any obligation outside
    that executor's supported set fails closed; a future tool must add a
    concrete executor before it can be exposed as a side effect.
    """

    codes = {obligation.code for obligation in receipt.obligations}
    if not codes:
        return receipt.effect == "allow"
    if not codes <= _EXECUTABLE_ACTION_OBLIGATIONS:
        return False
    if receipt.capability not in _ACTION_PERSISTENCE_CAPABILITIES:
        return False
    if "DO_NOT_PERSIST" in codes:
        return False
    try:
        evidence = evidence_from_policy_receipt(
            receipt,
            capability=receipt.capability,
            profile=profile.profile,
        )
    except ValueError:
        return False
    capability = receipt.capability
    decision = decide_persistence(
        profile,
        action_obligations=codes,
        memory_capture_evidence=evidence if capability == "memory_capture" else None,
        raw_audio_evidence=evidence if capability == "raw_audio_retention" else None,
        training_evidence=(
            evidence if capability == "model_training_contribution" else None
        ),
    )
    return decision.allowed


__all__ = [
    "PersistenceDecision",
    "decide_persistence",
    "execute_action_obligations",
]
