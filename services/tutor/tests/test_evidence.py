"""Server-owned assessment evidence, vault, receipts, gate, and rubric."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyActionResourceFence,
    PolicyEffect,
    PolicyObligationSpec,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyReceiptV2 as ContractPolicyReceiptV2,
)
from services.policy.receipt_store import InMemoryPolicyReceiptWriter
from services.policy.receipts import PolicyReceiptV2
from services.tutor.authority import (
    EVALUATOR_VERSION,
    FencedAssessmentAuthority,
    TutorAssessmentEvidence,
    TutorAssessmentSignals,
    TutorAssessmentVault,
    TutorEvidenceGate,
    TutorEvidenceRejected,
    TutorFenceSnapshot,
    TutorReceiptExpectation,
    TutorReceiptVerification,
    TutorScoringRubric,
    TutorTurnCounters,
    policy_action_resource_fence,
)
from services.tutor.catalog import lesson_task

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
_SIGNING_KEY = b"assessment-signing-key-32-bytes-minimum"


def _fence(**overrides: object) -> TutorFenceSnapshot:
    values: dict[str, object] = {
        "voice_session_id": "voice-session-1",
        "actor_id": "actor-1",
        "active_subject_id": "subject-1",
        "device_id": "device-1",
        "binding_id": "binding-1",
        "binding_version": 3,
        "subject_revision": 2,
        "session_epoch": 4,
        "runtime_profile_id": "rp_1",
        "policy_receipt_ids": ("receipt-tutor", "receipt-memory"),
        "expires_at": NOW + timedelta(minutes=5),
        "resolved_at": NOW - timedelta(seconds=30),
    }
    values.update(overrides)
    return TutorFenceSnapshot(**values)  # type: ignore[arg-type]


def _evidence(**overrides: object) -> TutorAssessmentEvidence:
    values: dict[str, object] = {
        "event_id": "tutor-evidence-1",
        "subject_id": "subject-1",
        "actor_id": "actor-1",
        "voice_session_id": "voice-session-1",
        "device_id": "device-1",
        "binding_id": "binding-1",
        "binding_version": 3,
        "subject_revision": 2,
        "task_id": "english-past-story",
        "focus": "tutor_english",
        "occurred_at": NOW,
        "duration_seconds": 600,
        "session_epoch": 4,
        "runtime_profile_id": "rp_1",
        "generation_id": 7,
        "turn_id": 3,
        "tool_epoch": 11,
        "policy_receipt_id": "receipt-tutor",
        "criterion_ids": ("english-past-story.criterion.v1",),
        "correctness_score": 0.95,
        "evaluator_version": EVALUATOR_VERSION,
        "support_level": "none",
        "completion": "completed",
        "attempt_count": 3,
    }
    values.update(overrides)
    return TutorAssessmentEvidence(**values)  # type: ignore[arg-type]


def _signed(**overrides: object) -> TutorAssessmentEvidence:
    return _evidence(**overrides).signed(_SIGNING_KEY)


def _gate() -> TutorEvidenceGate:
    return TutorEvidenceGate(signing_key=_SIGNING_KEY)


def test_evidence_requires_the_complete_server_fence() -> None:
    with pytest.raises(ValueError, match="policy_receipt_id"):
        _evidence(policy_receipt_id="")
    with pytest.raises(ValueError, match="session_epoch"):
        _evidence(session_epoch=0)
    with pytest.raises(ValueError, match="turn_id"):
        _evidence(turn_id=-1)
    with pytest.raises(ValueError, match="subject_id"):
        _evidence(subject_id=" ")
    with pytest.raises(ValueError, match="device_id"):
        _evidence(device_id="")
    with pytest.raises(ValueError, match="binding_version"):
        _evidence(binding_version=0)
    with pytest.raises(ValueError, match="duration_seconds"):
        _evidence(duration_seconds=4 * 60 * 60)
    with pytest.raises(ValueError, match="support_level"):
        _evidence(support_level="guessed")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="criterion_ids"):
        _evidence(criterion_ids=())
    with pytest.raises(ValueError, match="correctness_score"):
        _evidence(correctness_score=1.5)


def test_evidence_hash_is_stable_but_signature_is_the_authority_boundary() -> None:
    first = _signed()
    second = _signed()
    assert first.content_sha256 == second.content_sha256
    assert first.signature == second.signature
    # A caller fabricating fields gets a different (valid-looking) hash but
    # cannot forge the HMAC signature without the server key.
    forged = _signed(generation_id=8)
    assert forged.content_sha256 != first.content_sha256
    assert forged.signature != first.signature
    unsigned = _evidence()
    assert unsigned.signature == ""


def test_evidence_content_hash_is_receipt_independent() -> None:
    # The action fence's evidence hash is computable before the receipt is
    # verified: re-issuing a receipt must not change the evidence digest.
    assert _evidence().content_sha256 == _evidence(policy_receipt_id="pending").content_sha256


def test_vault_is_server_owned_and_tokens_are_one_time() -> None:
    vault = TutorAssessmentVault()
    assessment_id = vault.put(_signed())
    assert vault.get(assessment_id) is not None
    assert vault.get("unknown-token") is None
    assert vault.consume(assessment_id, event_id="tutor-evidence-1") is True
    assert vault.consume(assessment_id, event_id="tutor-evidence-1") is True
    assert vault.consume(assessment_id, event_id="tutor-evidence-other") is False
    assert vault.consume("unknown-token", event_id="tutor-evidence-1") is False


def test_gate_rejects_stale_cross_subject_forged_replayed_expired_evidence() -> None:
    gate = _gate()
    fence = _fence()

    gate.verify(
        evidence=_signed(),
        fence=fence,
        known_event_ids=(),
        now=NOW,
        receipt_expires_at=NOW + timedelta(minutes=4),
    )

    stale = _signed(session_epoch=fence.session_epoch - 1)
    with pytest.raises(TutorEvidenceRejected, match="stale_session_fence"):
        gate.verify(evidence=stale, fence=fence, known_event_ids=(), now=NOW)
    cross_subject = _signed(subject_id="subject-other")
    with pytest.raises(TutorEvidenceRejected, match="cross_subject_evidence"):
        gate.verify(evidence=cross_subject, fence=fence, known_event_ids=(), now=NOW)
    actor_mismatch = _signed(actor_id="actor-other")
    with pytest.raises(TutorEvidenceRejected, match="actor_mismatch"):
        gate.verify(evidence=actor_mismatch, fence=fence, known_event_ids=(), now=NOW)
    forged = _signed(runtime_profile_id="rp_forged")
    with pytest.raises(TutorEvidenceRejected, match="stale_session_fence"):
        gate.verify(evidence=forged, fence=fence, known_event_ids=(), now=NOW)
    device_forged = _signed(device_id="device-other")
    with pytest.raises(TutorEvidenceRejected, match="evidence_fence_mismatch"):
        gate.verify(evidence=device_forged, fence=fence, known_event_ids=(), now=NOW)
    binding_forged = _signed(binding_version=99)
    with pytest.raises(TutorEvidenceRejected, match="evidence_fence_mismatch"):
        gate.verify(evidence=binding_forged, fence=fence, known_event_ids=(), now=NOW)
    replayed = _signed()
    with pytest.raises(TutorEvidenceRejected, match="replayed_evidence"):
        gate.verify(
            evidence=replayed,
            fence=fence,
            known_event_ids=(replayed.event_id,),
            now=NOW,
        )
    bad_receipt = _signed(policy_receipt_id="receipt-stolen")
    with pytest.raises(TutorEvidenceRejected, match="unauthorized_policy_receipt"):
        gate.verify(evidence=bad_receipt, fence=fence, known_event_ids=(), now=NOW)
    with pytest.raises(TutorEvidenceRejected, match="expired_session_fence"):
        gate.verify(
            evidence=_signed(),
            fence=fence,
            known_event_ids=(),
            now=fence.expires_at,
        )
    before_fence = _signed(occurred_at=fence.resolved_at - timedelta(seconds=1))
    with pytest.raises(TutorEvidenceRejected, match="evidence_before_fence"):
        gate.verify(evidence=before_fence, fence=fence, known_event_ids=(), now=NOW)
    after_receipt = _signed(occurred_at=NOW + timedelta(minutes=4, seconds=30))
    with pytest.raises(TutorEvidenceRejected, match="evidence_after_fence"):
        gate.verify(
            evidence=after_receipt,
            fence=fence,
            known_event_ids=(),
            now=NOW,
            receipt_expires_at=NOW + timedelta(minutes=4),
        )


def test_gate_rejects_forged_envelope_and_unknown_issuer() -> None:
    gate = _gate()
    evidence = _signed()
    # Tamper with the signed payload after issuance: signature no longer
    # matches, even though the object was produced by the server.
    tampered = replace(evidence, subject_id="subject-forged")
    with pytest.raises(TutorEvidenceRejected, match="forged_envelope"):
        gate.verify(
            evidence=tampered,
            fence=_fence(),
            known_event_ids=(),
            now=NOW,
        )
    wrong_issuer = _signed(issuer="memoria.evil.v1")
    with pytest.raises(TutorEvidenceRejected, match="unknown_issuer"):
        gate.verify(
            evidence=wrong_issuer,
            fence=_fence(),
            known_event_ids=(),
            now=NOW,
        )


def _action_expectation(**overrides: object) -> TutorReceiptExpectation:
    values: dict[str, object] = {
        "capability": "tutor",
        "purpose": "runtime_sensitive_action",
        "actor_id": "actor-1",
        "subject_id": "subject-1",
        "device_id": "device-1",
        "binding_id": "binding-1",
        "binding_version": 3,
        "session_id": "voice-session-1",
        "session_epoch": 4,
        "runtime_profile_id": "rp_1",
        "subject_revision": 2,
        "action_resource_id": "tutor-evidence-1",
        "action_revision": 2,
    }
    values.update(overrides)
    return TutorReceiptExpectation(**values)  # type: ignore[arg-type]


def _action_fence(**overrides: object) -> PolicyActionResourceFence:
    values: dict[str, object] = {
        "capability": "tutor",
        "purpose": "runtime_sensitive_action",
        "action_resource_id": "tutor-evidence-1",
        "action_revision": 2,
        "generation_id": 7,
        "turn_id": 3,
        "tool_epoch": 11,
        "issued_at": NOW,
        "valid_until": NOW + timedelta(minutes=5),
    }
    values.update(overrides)
    return policy_action_resource_fence(**values)  # type: ignore[arg-type]


def _obligation(code: str) -> PolicyObligationSpec:
    from packages.contracts.generated.python.multi_subject_contracts import (
        ObligationParams,
    )

    return PolicyObligationSpec(
        code=code,  # type: ignore[arg-type]
        params=ObligationParams(
            max_session_seconds=None,
            retention_ttl_seconds=None,
            quiet_hours=None,
            extras=(),
        ),
    )


def _action_receipt(
    writer: InMemoryPolicyReceiptWriter,
    *,
    receipt_id: str = "receipt-tutor",
    expectation: TutorReceiptExpectation | None = None,
    action_fence: PolicyActionResourceFence | None = None,
    **overrides: object,
) -> ContractPolicyReceiptV2:
    expectation = expectation or _action_expectation()
    overrides = dict(overrides)
    explicit_fence = overrides.pop("action_fence", None)
    if explicit_fence is not None and not isinstance(
        explicit_fence,
        PolicyActionResourceFence,
    ):
        raise TypeError("action_fence must be a PolicyActionResourceFence")
    if action_fence is None:
        action_fence = _action_fence(
            capability=str(overrides.get("capability", expectation.capability)),
            purpose=str(overrides.get("purpose", expectation.purpose)),
        )
    elif explicit_fence is not None:
        action_fence = explicit_fence
    values: dict[str, object] = {
        "receipt_id": receipt_id,
        "actor_id": expectation.actor_id,
        "subject_id": expectation.subject_id,
        "resource_owner_id": expectation.subject_id,
        "device_id": expectation.device_id,
        "capability": expectation.capability,
        "purpose": expectation.purpose,
        "effect": PolicyEffect.POLICY_EFFECT_ALLOW_WITH_OBLIGATIONS.value,
        "reason_code": "tutor_action_authorized",
        "obligations": tuple(_obligation(code) for code in expectation.required_obligations),
        "policy_version": "v2",
        "context_hash": "c" * 64,
        "action_resource_fence": action_fence,
        "action_fence_hash": action_fence.canonical_hash,
        "consent_snapshot_ids": (),
        "consent_snapshot_revisions": (),
        "relationship_snapshot_ids": (),
        "relationship_snapshot_revisions": (),
        "binding_id": expectation.binding_id,
        "binding_version": expectation.binding_version,
        "binding_canonical_hash": None,
        "session_id": expectation.session_id,
        "session_epoch": expectation.session_epoch,
        "runtime_profile_id": expectation.runtime_profile_id,
        "subject_revision": expectation.subject_revision,
        "device_trust": "trusted",
        "data_classification": "ephemeral",
        "safety_state": "normal",
        "jurisdiction": "CN",
        "created_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=4)).isoformat(),
        "exact_fence": False,
    }
    values.update(overrides)
    if isinstance(values.get("expires_at"), datetime):
        values["expires_at"] = values["expires_at"].isoformat()
    receipt = ContractPolicyReceiptV2(**values)  # type: ignore[arg-type]
    writer.write(receipt)
    return receipt


class _StrictActionReceiptVerifier:
    """Test double for the Policy transaction-bound verifier seam.

    Implements the exact contract the Policy port must satisfy against the
    canonical generated PolicyReceiptV2: existence, capability/purpose,
    every literal fence field, the generated PolicyActionResourceFence
    equality, the validity window, and the required obligations.
    """

    def __init__(self, writer: InMemoryPolicyReceiptWriter) -> None:
        self._writer = writer

    async def verify_receipt(
        self,
        *,
        receipt_id: str,
        expectation: TutorReceiptExpectation,
        action_fence: PolicyActionResourceFence,
        now: datetime,
        connection: object | None = None,
    ) -> TutorReceiptVerification:
        receipt = self._writer.get(receipt_id)
        if receipt is None:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_not_found",
            )
        if not hasattr(receipt, "action_resource_fence"):
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_version_unsupported",
            )
        if receipt.action_resource_fence is None:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_fence_missing",
            )
        if receipt.effect not in {"allow", "allow_with_obligations"}:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_not_allow",
            )
        if receipt.capability != expectation.capability:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_capability_mismatch",
            )
        if receipt.purpose != expectation.purpose:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_purpose_mismatch",
            )
        literal_fields = {
            "actor_id": expectation.actor_id,
            "subject_id": expectation.subject_id,
            "resource_owner_id": expectation.subject_id,
            "device_id": expectation.device_id,
            "binding_id": expectation.binding_id,
            "binding_version": expectation.binding_version,
            "session_id": expectation.session_id,
            "session_epoch": expectation.session_epoch,
            "runtime_profile_id": expectation.runtime_profile_id,
            "subject_revision": expectation.subject_revision,
        }
        for name, expected in literal_fields.items():
            if getattr(receipt, name) != expected:
                return TutorReceiptVerification(
                    ok=False,
                    receipt_id=receipt_id,
                    reason=f"receipt_fence_mismatch:{name}",
                )
        if receipt.action_resource_fence != action_fence:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_action_fence_mismatch",
            )
        if not receipt.created_at <= now < receipt.expires_at:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason="receipt_expired",
            )
        receipt_obligations = {
            str(obligation.code) for obligation in receipt.obligations
        }
        missing = [
            code
            for code in expectation.required_obligations
            if code not in receipt_obligations
        ]
        if missing:
            return TutorReceiptVerification(
                ok=False,
                receipt_id=receipt_id,
                reason=f"receipt_obligations_missing:{','.join(missing)}",
            )
        return TutorReceiptVerification(
            ok=True,
            receipt_id=receipt_id,
            expires_at=receipt.expires_at,
        )


@pytest.mark.asyncio
async def test_action_receipt_verifier_is_field_exact_and_fence_bound() -> None:
    writer = InMemoryPolicyReceiptWriter()
    verifier = _StrictActionReceiptVerifier(writer)
    _action_receipt(writer)

    ok = await verifier.verify_receipt(
        receipt_id="receipt-tutor",
        expectation=_action_expectation(),
        action_fence=_action_fence(),
        now=NOW,
    )
    assert ok.ok is True
    assert ok.expires_at is not None

    async def rejects(
        receipt_id: str,
        *,
        expectation: TutorReceiptExpectation | None = None,
        action_fence: PolicyActionResourceFence | None = None,
        reason: str,
    ) -> None:
        result = await verifier.verify_receipt(
            receipt_id=receipt_id,
            expectation=expectation or _action_expectation(),
            action_fence=action_fence or _action_fence(),
            now=NOW,
        )
        assert result.ok is False
        assert result.reason == reason

    await rejects("receipt-missing", reason="receipt_not_found")
    _action_receipt(writer, receipt_id="receipt-wrong-capability", capability="voice_clone_use")
    await rejects("receipt-wrong-capability", reason="receipt_capability_mismatch")
    _action_receipt(
        writer,
        receipt_id="receipt-wrong-purpose",
        purpose="runtime_profile_issue",
    )
    await rejects("receipt-wrong-purpose", reason="receipt_purpose_mismatch")
    _action_receipt(
        writer,
        receipt_id="receipt-expired",
        expires_at=NOW - timedelta(seconds=1),
    )
    await rejects("receipt-expired", reason="receipt_expired")
    _action_receipt(writer, receipt_id="receipt-wrong-actor", actor_id="actor-other")
    await rejects("receipt-wrong-actor", reason="receipt_fence_mismatch:actor_id")
    _action_receipt(writer, receipt_id="receipt-wrong-epoch", session_epoch=3)
    await rejects("receipt-wrong-epoch", reason="receipt_fence_mismatch:session_epoch")
    _action_receipt(
        writer,
        receipt_id="receipt-old-turn",
        action_fence=_action_fence(turn_id=2),
    )
    await rejects("receipt-old-turn", reason="receipt_action_fence_mismatch")
    _action_receipt(
        writer,
        receipt_id="receipt-no-obligation",
        obligations=(),
    )
    await rejects(
        "receipt-no-obligation",
        reason="receipt_obligations_missing:WRITE_SUBJECT_SCOPED_PROGRESS",
    )
    _action_receipt(
        writer,
        receipt_id="receipt-denied",
        effect=PolicyEffect.POLICY_EFFECT_DENY.value,
    )
    await rejects("receipt-denied", reason="receipt_not_allow")
    # A legacy receipt whose producer never bound the real turn counters
    # (default zero-counter fence) can never authorize the write.
    legacy_fence = _action_fence(generation_id=0, turn_id=0, tool_epoch=0)
    writer.write(
        PolicyReceiptV2(
            receipt_id="receipt-legacy",
            actor_id="actor-1",
            subject_id="subject-1",
            resource_owner_id="subject-1",
            device_id="device-1",
            capability="tutor",
            purpose="runtime_sensitive_action",
            effect="allow_with_obligations",
            reason_code="legacy",
            obligations=(),
            policy_version="v2",
            context_hash="0" * 64,
            consent_snapshot_ids=(),
            consent_snapshot_revisions=(),
            relationship_snapshot_ids=(),
            relationship_snapshot_revisions=(),
            binding_id="binding-1",
            binding_version=3,
            binding_canonical_hash=None,
            session_id="voice-session-1",
            session_epoch=4,
            runtime_profile_id="rp_1",
            subject_revision=2,
            device_trust="trusted",
            data_classification="ephemeral",
            safety_state="normal",
            jurisdiction="CN",
            created_at=(NOW - timedelta(minutes=1)).isoformat(),
            expires_at=(NOW + timedelta(minutes=4)).isoformat(),
            exact_fence=False,
            action_resource_fence=legacy_fence,
            action_fence_hash=legacy_fence.canonical_hash,
        )
    )
    # Legacy receipts without a real action fence fall back to a default
    # zero-counter fence: never equal to the current turn, so they fail.
    await rejects("receipt-legacy", reason="receipt_action_fence_mismatch")


def test_rubric_requires_correctness_attempts_and_catalog_criteria_for_mastery() -> None:
    rubric = TutorScoringRubric()
    task = lesson_task("english-past-story")
    assert task is not None

    mastered = rubric.score(_signed(), task)
    assert mastered == ("mastered", "past-story")
    # Completed but wrong answer must never be mastered.
    wrong = rubric.score(_signed(correctness_score=0.4), task)
    assert wrong == ("struggled", "past-story")
    # No correctness evidence: cannot prove mastery.
    unscored = rubric.score(_signed(correctness_score=None), task)
    assert unscored == ("attempted", "past-story")
    # Not enough independent attempts.
    few_attempts = rubric.score(_signed(attempt_count=1), task)
    assert few_attempts == ("attempted", "past-story")
    # High-level help caps the outcome.
    guided = rubric.score(_signed(support_level="minimal_hint"), task)
    assert guided == ("supported", "past-story")
    fully_guided = rubric.score(_signed(support_level="full_guidance"), task)
    assert fully_guided == ("supported", "past-story")
    # Stale evaluator version cannot mint mastery.
    stale_evaluator = rubric.score(_signed(evaluator_version="tutor-rubric-v1"), task)
    assert stale_evaluator == ("attempted", "past-story")
    partial = rubric.score(_signed(completion="partial", support_level="hint"), task)
    assert partial == ("struggled", "past-story")
    partial_alone = rubric.score(_signed(completion="partial", support_level="none"), task)
    assert partial_alone == ("attempted", "past-story")
    gave_up = rubric.score(_signed(completion="gave_up"), task)
    assert gave_up == ("gave_up", "past-story")


def test_rubric_never_mints_mastery_for_open_criteria() -> None:
    rubric = TutorScoringRubric()
    homework = lesson_task("homework-self-guided")
    assert homework is not None
    outcome = rubric.score(
        _signed(
            task_id="homework-self-guided",
            focus="tutor_homework",
            criterion_ids=("homework-self-guided.criterion.v1",),
            correctness_score=0.99,
            attempt_count=5,
        ),
        homework,
    )
    assert outcome == ("attempted", "homework-planning")


def test_rubric_rejects_evidence_not_bound_to_server_catalog() -> None:
    rubric = TutorScoringRubric()
    task = lesson_task("english-past-story")
    assert task is not None
    with pytest.raises(ValueError, match="evidence task does not match"):
        rubric.score(_signed(task_id="english-not-in-catalog"), task)
    with pytest.raises(ValueError, match="not in the server catalog"):
        rubric.score(
            _signed(criterion_ids=("english-weekly-recap.criterion.v1",)),
            task,
        )


@pytest.mark.asyncio
async def test_fenced_authority_mints_once_per_turn_and_is_idempotent() -> None:
    authority = FencedAssessmentAuthority(signing_key=_SIGNING_KEY)
    fence = _fence()
    task = lesson_task("english-past-story")
    assert task is not None
    counters = TutorTurnCounters(generation_id=7, turn_id=3, tool_epoch=11)
    authority.set_turn_state(
        fence.voice_session_id,
        counters=counters,
        signals=TutorAssessmentSignals(
            subject_id="subject-1",
            runtime_profile_id="rp_1",
            session_epoch=4,
            task_id="english-past-story",
            evaluator_version=EVALUATOR_VERSION,
            generation_id=7,
            turn_id=3,
            tool_epoch=11,
            support_level="none",
            completion="completed",
            correctness_score=0.95,
            attempt_count=3,
            criterion_ids=("english-past-story.criterion.v1",),
        ),
    )
    writer = InMemoryPolicyReceiptWriter()
    _action_receipt(writer)
    verifier = _StrictActionReceiptVerifier(writer)
    template = _action_expectation()

    first = await authority.issue_assessment(
        fence=fence,
        task=task,
        duration_seconds=600,
        now=NOW,
        event_id="tutor-evidence-1",
        receipt_template=template,
        receipt_verifier=verifier,
    )
    assert first.receipt_id == "receipt-tutor"
    assert first.receipt_expires_at is not None
    assert first.action_fence.canonical_hash == _action_fence().canonical_hash
    retry = await authority.issue_assessment(
        fence=fence,
        task=task,
        duration_seconds=600,
        now=NOW,
        event_id="tutor-evidence-1",
        receipt_template=template,
        receipt_verifier=verifier,
    )
    assert retry == first
    evidence = await authority.fetch_assessment(assessment_id=first.assessment_id)
    assert evidence is not None
    assert evidence.signature
    assert evidence.policy_receipt_id == "receipt-tutor"
    _gate().verify(
        evidence=evidence,
        fence=fence,
        known_event_ids=(),
        now=NOW,
        receipt_expires_at=first.receipt_expires_at,
    )
    # A second, different event for the same already-consumed turn fence is
    # rejected: no TOCTOU window for the same turn.
    with pytest.raises(TutorEvidenceRejected, match="stale_event_counters"):
        await authority.issue_assessment(
            fence=fence,
            task=task,
            duration_seconds=600,
            now=NOW,
            event_id="tutor-evidence-2",
            receipt_template=template,
            receipt_verifier=verifier,
        )


@pytest.mark.asyncio
async def test_concurrent_issues_serialize_without_deadlock_exactly_one_wins() -> None:
    authority = FencedAssessmentAuthority(signing_key=_SIGNING_KEY)
    fence = _fence(policy_receipt_ids=("receipt-tutor-a", "receipt-tutor-b"))
    task = lesson_task("english-past-story")
    assert task is not None
    authority.set_turn_state(
        fence.voice_session_id,
        counters=TutorTurnCounters(generation_id=7, turn_id=3, tool_epoch=11),
        signals=TutorAssessmentSignals(
            subject_id="subject-1",
            runtime_profile_id="rp_1",
            session_epoch=4,
            task_id="english-past-story",
            evaluator_version=EVALUATOR_VERSION,
            generation_id=7,
            turn_id=3,
            tool_epoch=11,
            support_level="none",
            completion="completed",
            correctness_score=0.95,
            attempt_count=3,
            criterion_ids=("english-past-story.criterion.v1",),
        ),
    )
    writer = InMemoryPolicyReceiptWriter()
    _action_receipt(
        writer,
        receipt_id="receipt-tutor-a",
        action_fence=_action_fence(action_resource_id="tutor-evidence-a"),
    )
    _action_receipt(
        writer,
        receipt_id="receipt-tutor-b",
        action_fence=_action_fence(action_resource_id="tutor-evidence-b"),
    )
    verifier = _StrictActionReceiptVerifier(writer)
    template_a = _action_expectation(action_resource_id="tutor-evidence-a")
    template_b = _action_expectation(action_resource_id="tutor-evidence-b")

    async def issue(event_id: str, template: TutorReceiptExpectation) -> str:
        grant = await asyncio.wait_for(
            authority.issue_assessment(
                fence=fence,
                task=task,
                duration_seconds=600,
                now=NOW,
                event_id=event_id,
                receipt_template=template,
                receipt_verifier=verifier,
            ),
            timeout=5,
        )
        return grant.assessment_id

    results = await asyncio.gather(
        issue("tutor-evidence-a", template_a),
        issue("tutor-evidence-b", template_b),
        return_exceptions=True,
    )
    wins = [item for item in results if isinstance(item, str)]
    rejections = [
        item
        for item in results
        if isinstance(item, TutorEvidenceRejected)
        and item.code == "stale_event_counters"
    ]
    assert len(wins) == 1
    assert len(rejections) == 1


@pytest.mark.asyncio
async def test_authority_rejects_signal_fence_mismatches() -> None:
    authority = FencedAssessmentAuthority(signing_key=_SIGNING_KEY)
    fence = _fence()
    task = lesson_task("english-past-story")
    assert task is not None
    counters = TutorTurnCounters(generation_id=7, turn_id=4, tool_epoch=12)
    writer = InMemoryPolicyReceiptWriter()
    _action_receipt(writer)
    verifier = _StrictActionReceiptVerifier(writer)
    template = _action_expectation()

    def signals(**overrides: object) -> TutorAssessmentSignals:
        values: dict[str, object] = {
            "subject_id": "subject-1",
            "runtime_profile_id": "rp_1",
            "session_epoch": 4,
            "task_id": "english-past-story",
            "evaluator_version": EVALUATOR_VERSION,
            "generation_id": 7,
            "turn_id": 4,
            "tool_epoch": 12,
            "support_level": "none",
            "completion": "completed",
            "correctness_score": 0.95,
            "attempt_count": 3,
            "criterion_ids": ("english-past-story.criterion.v1",),
        }
        values.update(overrides)
        return TutorAssessmentSignals(**values)  # type: ignore[arg-type]

    cases = {
        "old signal on new counters": signals(turn_id=3),
        "cross-task signals": signals(task_id="english-weekly-recap"),
        "cross-subject signals": signals(subject_id="subject-other"),
        "stale runtime profile": signals(runtime_profile_id="rp_old"),
        "stale evaluator": signals(evaluator_version="tutor-rubric-v1"),
        "no signals installed": None,
    }
    for name, wrong in cases.items():
        if wrong is not None:
            authority.set_turn_state(
                fence.voice_session_id,
                counters=counters,
                signals=wrong,
            )
        else:
            with authority._state_lock:
                authority._turn_states.clear()
                authority._current.pop(fence.voice_session_id, None)
        with pytest.raises(
            TutorEvidenceRejected,
            match="assessment_signal_fence_mismatch|stale_event_counters",
        ):
            await authority.issue_assessment(
                fence=fence,
                task=task,
                duration_seconds=600,
                now=NOW,
                event_id=f"tutor-evidence-{name}",
                receipt_template=template,
                receipt_verifier=verifier,
            )
