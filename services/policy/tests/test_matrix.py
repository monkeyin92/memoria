"""Decision matrix (corrected edition) table-driven coverage."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from services.policy.context import canonical_runtime_purpose_for_capability
from services.policy.engine import PolicyEngine
from services.policy.tests.fakes import (
    make_binding,
    make_consent,
    make_consent_snapshot_ref,
    make_context,
    make_relationship,
)

NOW = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)


def _adult_context(
    *,
    capability: str,
    purpose: str | None = None,
    device_trust: str = "trusted",
    data_classification: str = "private",
    with_evidence: bool = True,
    **overrides: object,
) -> object:
    if purpose is None:
        purpose = canonical_runtime_purpose_for_capability(capability)
    consent = make_consent(
        capability=capability,  # type: ignore[arg-type]
        purpose=purpose,
        now=NOW,
    )
    return make_context(
        capability=capability,  # type: ignore[arg-type]
        purpose=purpose,
        device_trust=device_trust,
        data_classification=data_classification,
        consent_evidence=(consent,) if with_evidence else (),
        binding_evidence=make_binding(now=NOW) if with_evidence else None,
        evaluated_at=NOW,
        **overrides,
    )


def _minor_context(
    *,
    capability: str,
    purpose: str | None = None,
    with_evidence: bool = True,
    **overrides: object,
) -> object:
    if purpose is None:
        purpose = canonical_runtime_purpose_for_capability(capability)
    relationship, consent, binding = _minor_evidence(
        capability=capability, purpose=purpose
    )
    return make_context(
        actor_id="person-child",
        subject_id="person-child",
        resource_owner_id="person-child",
        capability=capability,  # type: ignore[arg-type]
        purpose=purpose,
        current_session_mode="student_minor",
        subject_category="minor",
        age_band="under_14",
        relationship_roles=frozenset({"self"}),
        consent_evidence=(consent,) if with_evidence else (),
        relationship_evidence=(relationship,) if with_evidence else (),
        binding_evidence=binding if with_evidence else None,
        evaluated_at=NOW,
        **overrides,
    )


def _minor_evidence(
    *,
    capability: str,
    purpose: str = "user_request",
) -> tuple[object, object, object]:
    relationship = make_relationship(
        relation_type="guardian_of",
        target_person_id="person-child",
        now=NOW,
    )
    consent = make_consent(
        consent_id=f"consent-{capability}",
        snapshot_id=f"snap-{capability}",
        subject_id="person-child",
        actor_id="person-parent",
        actor_kind="guardian",
        capability=capability,  # type: ignore[arg-type]
        purpose=purpose,
        now=NOW,
    )
    return relationship, consent, make_binding(now=NOW)


ADULT_SENSITIVE = [
    "voice_profile_create",
    "voice_clone_use",
    "digital_self_preview",
    "legacy_grant_create",
    "payment",
    "raw_audio_retention",
    "model_training_contribution",
    "device_ownership_transfer",
]


@pytest.mark.parametrize("capability", ADULT_SENSITIVE)
def test_adult_sensitive_capability_allowed_with_authoritative_evidence(
    capability: str,
) -> None:
    decision = PolicyEngine().decide(
        _adult_context(capability=capability)  # type: ignore[arg-type]
    )
    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "adult_subject_authorized"
    assert decision.obligations


@pytest.mark.parametrize("capability", ADULT_SENSITIVE)
def test_adult_sensitive_capability_denied_without_evidence(capability: str) -> None:
    decision = PolicyEngine().decide(
        _adult_context(capability=capability, with_evidence=False)  # type: ignore[arg-type]
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "subject_consent_required"


@pytest.mark.parametrize(
    "device_trust",
    ["offline", "untrusted", "revoked"],
)
def test_offline_revoked_or_untrusted_device_denies_sensitive_capability(
    device_trust: str,
) -> None:
    decision = PolicyEngine().decide(
        _adult_context(
            capability="voice_clone_use", device_trust=device_trust  # type: ignore[arg-type]
        )
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "device_untrusted"


def test_verified_device_allows_sensitive_capability() -> None:
    decision = PolicyEngine().decide(
        _adult_context(
            capability="voice_clone_use", device_trust="verified"  # type: ignore[arg-type]
        )
    )
    assert decision.effect == "allow_with_obligations"


def test_sensitive_capability_requires_consent_purpose_match() -> None:
    context = _adult_context(  # type: ignore[var-annotated]
        capability="voice_clone_use", purpose="voice_clone"
    )
    consent = make_consent(
        capability="voice_clone_use", purpose="user_request", now=NOW
    )
    decision = PolicyEngine().decide(
        replace(
            context,
            consent_evidence=(consent,),
        )
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "consent_purpose_mismatch"


@pytest.mark.parametrize("capability", ADULT_SENSITIVE)
def test_sensitive_capability_rejects_noncanonical_context_purpose(
    capability: str,
) -> None:
    decision = PolicyEngine().decide(
        _adult_context(
            capability=capability,
            purpose="user_request",
        )
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "consent_purpose_mismatch"


def test_device_transfer_requires_canonical_consent_purpose_and_receipts_it() -> None:
    consent = make_consent(
        capability="device_ownership_transfer",
        purpose="device_transfer",
        now=NOW,
    )
    context = make_context(
        capability="device_ownership_transfer",
        purpose="device_transfer",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    engine = PolicyEngine()
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    assert decision.effect == "allow_with_obligations"
    assert receipt.purpose == "device_transfer"
    assert receipt.consent_snapshot_ids == (consent.snapshot_id,)


def test_device_transfer_rejects_non_matching_consent_purpose() -> None:
    consent = make_consent(
        capability="device_ownership_transfer",
        purpose="user_request",
        now=NOW,
    )
    decision = PolicyEngine().decide(
        make_context(
            capability="device_ownership_transfer",
            purpose="device_transfer",
            consent_evidence=(consent,),
            binding_evidence=make_binding(now=NOW),
            evaluated_at=NOW,
        )
    )

    assert (decision.effect, decision.reason_code) == (
        "deny",
        "consent_purpose_mismatch",
    )


def test_device_transfer_selects_exact_purpose_from_multiple_consents() -> None:
    wrong = make_consent(
        consent_id="consent-a-wrong",
        snapshot_id="snapshot-a-wrong",
        capability="device_ownership_transfer",
        purpose="user_request",
        now=NOW,
    )
    matching = make_consent(
        consent_id="consent-z-matching",
        snapshot_id="snapshot-z-matching",
        capability="device_ownership_transfer",
        purpose="device_transfer",
        now=NOW,
    )
    context = make_context(
        capability="device_ownership_transfer",
        purpose="device_transfer",
        consent_evidence=(wrong, matching),
        consent_snapshot_evidence=(
            make_consent_snapshot_ref(
                snapshot_id="current-transfer-snapshot",
                revision=7,
                canonical_hash="7" * 64,
                consents=(wrong, matching),
                now=NOW,
            ),
        ),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    engine = PolicyEngine()
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    assert decision.effect == "allow_with_obligations"
    assert receipt.consent_snapshot_ids == ("current-transfer-snapshot",)


def test_device_ownership_transfer_is_not_a_canonical_purpose() -> None:
    with pytest.raises(ValueError, match="purpose"):
        make_context(
            capability="device_ownership_transfer",
            purpose="device_ownership_transfer",
            evaluated_at=NOW,
        )


def test_identity_transfer_purpose_matches_policy_canonical_value() -> None:
    """Identity and Policy freeze the same transfer purpose."""
    from services.identity.authority import TRANSFER_PURPOSE

    assert TRANSFER_PURPOSE == "device_transfer"


def test_sensitive_capability_requires_data_classification_match() -> None:
    context = _adult_context(capability="voice_clone_use")  # type: ignore[var-annotated]
    consent = make_consent(
        capability="voice_clone_use",
        purpose="voice_clone",
        data_classification="biometric",
        now=NOW,
    )
    decision = PolicyEngine().decide(
        replace(context, consent_evidence=(consent,))
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "consent_classification_mismatch"


def test_actor_mismatch_denies_sensitive_capability() -> None:
    context = _adult_context(capability="voice_clone_use")  # type: ignore[var-annotated]
    decision = PolicyEngine().decide(replace(context, actor_id="person-other"))
    assert decision.effect == "deny"
    assert decision.reason_code == "subject_consent_required"


def test_missing_binding_evidence_denies_sensitive_capability() -> None:
    context = _adult_context(capability="voice_clone_use")  # type: ignore[var-annotated]
    decision = PolicyEngine().decide(replace(context, binding_evidence=None))
    assert decision.effect == "deny"
    assert decision.reason_code == "binding_evidence_required"


def test_missing_resource_owner_denies_adult_sensitive_capability() -> None:
    context = _adult_context(capability="voice_clone_use")  # type: ignore[var-annotated]

    decision = PolicyEngine().decide(replace(context, resource_owner_id=None))

    assert decision.effect == "deny"
    assert decision.reason_code == "subject_consent_required"


MINOR_HARD_FORBIDDEN = [
    "voice_clone_use",
    "digital_self_preview",
    "legacy_grant_create",
    "device_ownership_transfer",
]


@pytest.mark.parametrize("capability", MINOR_HARD_FORBIDDEN)
def test_minor_hard_forbidden_capabilities_deny_even_with_evidence(
    capability: str,
) -> None:
    decision = PolicyEngine().decide(
        _minor_context(capability=capability)  # type: ignore[arg-type]
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "minor_capability_forbidden"


@pytest.mark.parametrize(
    "capability",
    ["payment", "raw_audio_retention", "model_training_contribution"],
)
def test_minor_default_denied_capabilities_stay_denied(capability: str) -> None:
    decision = PolicyEngine().decide(
        _minor_context(capability=capability)  # type: ignore[arg-type]
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "minor_capability_denied"


@pytest.mark.parametrize(
    "capability",
    ["chat", "tutor", "english_practice"],
)
def test_minor_regular_capabilities_require_governance_and_evidence(
    capability: str,
) -> None:
    denied = PolicyEngine().decide(
        _minor_context(capability=capability, with_evidence=False)  # type: ignore[arg-type]
    )
    assert denied.effect == "deny"
    assert denied.reason_code in {
        "consent_evidence_required",
        "guardian_evidence_required",
        "tutor_subject_mismatch",
    }
    allowed = PolicyEngine().decide(
        _minor_context(capability=capability)  # type: ignore[arg-type]
    )
    assert allowed.effect == "allow_with_obligations"


def test_minor_voice_profile_create_allowed_as_enrollment_only() -> None:
    decision = PolicyEngine().decide(
        _minor_context(capability="voice_profile_create")  # type: ignore[arg-type]
    )
    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "minor_voice_profile_enrolled"
    assert [o.code for o in decision.obligations] == [
        "NO_MODEL_TRAINING",
        "WRITE_POLICY_RECEIPT",
    ]


def test_minor_voice_profile_create_denied_without_evidence() -> None:
    decision = PolicyEngine().decide(
        _minor_context(capability="voice_profile_create", with_evidence=False)  # type: ignore[arg-type]
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "consent_evidence_required"


def test_minor_memory_capture_allowed_with_minimized_obligations() -> None:
    decision = PolicyEngine().decide(
        _minor_context(capability="memory_capture")  # type: ignore[arg-type]
    )
    assert decision.effect == "allow_with_obligations"
    assert [o.code for o in decision.obligations] == [
        "PERSIST_AGGREGATE_ONLY",
        "RETENTION_TTL",
        "NO_MODEL_TRAINING",
        "WRITE_POLICY_RECEIPT",
    ]


def test_minor_memory_capture_denied_without_consent_evidence() -> None:
    decision = PolicyEngine().decide(
        _minor_context(capability="memory_capture", with_evidence=False)  # type: ignore[arg-type]
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "memory_consent_required"


def test_minor_private_memory_recall_requires_subject_only() -> None:
    context = _minor_context(capability="memory_recall_private")  # type: ignore[var-annotated]
    decision = PolicyEngine().decide(replace(context, actor_id="person-parent"))
    assert decision.effect == "deny"
    assert decision.reason_code == "private_memory_subject_only"


def test_minor_guardian_summary_allowed_with_guardian_evidence() -> None:
    relationship = make_relationship(
        relation_type="guardian_of",
        source_person_id="person-parent",
        target_person_id="person-child",
        now=NOW,
    )
    consent = make_consent(
        consent_id="consent-summary",
        subject_id="person-child",
        actor_id="person-parent",
        actor_kind="guardian",
        capability="guardian_summary_view",
        purpose="guardian_summary",
        now=NOW,
    )
    decision = PolicyEngine().decide(
        make_context(
            actor_id="person-parent",
            subject_id="person-child",
            resource_owner_id="person-child",
            capability="guardian_summary_view",
            current_session_mode="student_minor",
            subject_category="minor",
            age_band="under_14",
            relationship_roles=frozenset({"guardian"}),
            relationship_evidence=(relationship,),
            consent_evidence=(consent,),
            binding_evidence=make_binding(now=NOW),
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "guardian_summary_authorized"
    assert [o.code for o in decision.obligations] == [
        "REDACT_TRANSCRIPT",
        "MINIMAL_NOTIFICATION_CONTENT",
        "WRITE_POLICY_RECEIPT",
    ]


def test_guardian_summary_denied_for_wrong_guardian_person() -> None:
    relationship = make_relationship(
        relation_type="guardian_of",
        source_person_id="person-other-parent",
        target_person_id="person-child",
        now=NOW,
    )
    consent = make_consent(
        subject_id="person-child",
        actor_id="person-parent",
        actor_kind="guardian",
        capability="guardian_summary_view",
        purpose="user_request",
        now=NOW,
    )
    decision = PolicyEngine().decide(
        make_context(
            actor_id="person-parent",
            subject_id="person-child",
            resource_owner_id="person-child",
            capability="guardian_summary_view",
            current_session_mode="student_minor",
            subject_category="minor",
            age_band="under_14",
            relationship_roles=frozenset({"guardian"}),
            relationship_evidence=(relationship,),
            consent_evidence=(consent,),
            binding_evidence=make_binding(now=NOW),
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "guardian_summary_forbidden"


def test_unknown_safe_english_practice_is_ephemeral() -> None:
    decision = PolicyEngine().decide(
        make_context(
            subject_id=None,
            resource_owner_id=None,
            capability="english_practice",
            current_session_mode="unknown_safe",
            subject_category="unknown",
            speaker_state="unconfirmed",
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "unknown_safe_ephemeral"
    assert [o.code for o in decision.obligations] == [
        "DO_NOT_PERSIST",
        "DO_NOT_WRITE_LEARNING_PROGRESS",
        "NO_MODEL_TRAINING",
        "REQUIRE_SPEAKER_CONFIRMATION",
    ]


def test_unknown_safe_chat_has_complete_ephemeral_obligations() -> None:
    decision = PolicyEngine().decide(
        make_context(
            subject_id=None,
            resource_owner_id=None,
            capability="chat",
            current_session_mode="unknown_safe",
            subject_category="unknown",
            age_band="unknown",
            speaker_state="unconfirmed",
            speaker_confidence=None,
            evaluated_at=NOW,
        )
    )

    assert decision.reason_code == "unknown_safe_ephemeral"
    assert [obligation.code for obligation in decision.obligations] == [
        "DO_NOT_PERSIST",
        "DO_NOT_WRITE_LEARNING_PROGRESS",
        "NO_MODEL_TRAINING",
        "REQUIRE_SPEAKER_CONFIRMATION",
    ]


def test_unknown_safe_tutor_is_denied() -> None:
    decision = PolicyEngine().decide(
        make_context(
            subject_id=None,
            resource_owner_id=None,
            capability="tutor",
            current_session_mode="unknown_safe",
            subject_category="unknown",
            speaker_state="unconfirmed",
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "subject_unconfirmed"


def test_confirmed_unknown_category_never_gets_sensitive_capabilities() -> None:
    for capability in ADULT_SENSITIVE + ["memory_capture", "memory_recall_private"]:
        decision = PolicyEngine().decide(
            make_context(
                actor_id="person-unverified",
                subject_id="person-unverified",
                resource_owner_id="person-unverified",
                capability=capability,  # type: ignore[arg-type]
                purpose=(
                    "memory_capture"
                    if capability == "memory_capture"
                    else "user_request"
                ),
                current_session_mode="unknown_safe",
                subject_category="unknown",
                age_band="unknown",
                evaluated_at=NOW,
            )
        )
        assert decision.effect == "deny"
        assert decision.reason_code == "subject_category_unverified"


def test_confirmed_unknown_category_tutor_is_denied() -> None:
    decision = PolicyEngine().decide(
        make_context(
            actor_id="person-unverified",
            subject_id="person-unverified",
            resource_owner_id="person-unverified",
            capability="tutor",
            current_session_mode="unknown_safe",
            subject_category="unknown",
            age_band="unknown",
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "subject_category_unverified"


def test_confirmed_unknown_category_chat_stays_ephemeral() -> None:
    decision = PolicyEngine().decide(
        make_context(
            actor_id="person-unverified",
            subject_id="person-unverified",
            resource_owner_id="person-unverified",
            capability="chat",
            current_session_mode="unknown_safe",
            subject_category="unknown",
            age_band="unknown",
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "unknown_safe_ephemeral"
    assert [obligation.code for obligation in decision.obligations] == [
        "DO_NOT_PERSIST",
        "DO_NOT_WRITE_LEARNING_PROGRESS",
        "NO_MODEL_TRAINING",
        "REQUIRE_SPEAKER_CONFIRMATION",
    ]


def test_forged_bare_roles_never_authorize_sensitive_capabilities() -> None:
    decision = PolicyEngine().decide(
        make_context(
            actor_id="person-child",
            subject_id="person-child",
            resource_owner_id="person-child",
            capability="voice_clone_use",
            current_session_mode="student_minor",
            subject_category="minor",
            age_band="under_14",
            relationship_roles=frozenset({"guardian", "device_admin", "self"}),
            consent_kinds=frozenset({"voice_clone"}),
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "minor_capability_forbidden"


def test_forged_bare_consent_kinds_never_authorize_adult_sensitive() -> None:
    decision = PolicyEngine().decide(
        make_context(
            capability="voice_clone_use",
            relationship_roles=frozenset({"self", "primary_subject"}),
            consent_kinds=frozenset({"voice_clone"}),
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "subject_consent_required"


def test_forged_bare_roles_never_authorize_guardian_summary() -> None:
    decision = PolicyEngine().decide(
        make_context(
            actor_id="person-parent",
            subject_id="person-child",
            resource_owner_id="person-child",
            capability="guardian_summary_view",
            current_session_mode="student_minor",
            subject_category="minor",
            age_band="under_14",
            relationship_roles=frozenset({"guardian"}),
            consent_kinds=frozenset({"guardian_summary"}),
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "guardian_summary_forbidden"


def test_crisis_notification_emits_minimal_obligations_only() -> None:
    relationship = make_relationship(
        relation_type="emergency_contact_for",
        target_person_id="person-child",
        now=NOW,
    )
    decision = PolicyEngine().decide(
        make_context(
            actor_id="safety-kernel",
            subject_id="person-child",
            resource_owner_id="person-child",
            capability="crisis_notification",
            safety_state="self_crisis",
            data_classification="safety_minimum",
            relationship_evidence=(relationship,),
            binding_evidence=make_binding(now=NOW),
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "crisis_notification_authorized"
    assert [o.code for o in decision.obligations] == [
        "NOTIFY_EMERGENCY_CONTACT",
        "MINIMAL_NOTIFICATION_CONTENT",
        "WRITE_POLICY_RECEIPT",
    ]
    assert dict(decision.obligations[0].params.extras) == {
        "intent_kind": "crisis_notification",
        "recipient_role": "emergency_contact",
    }


def test_crisis_notification_requires_crisis_safety_signal() -> None:
    relationship = make_relationship(
        relation_type="guardian_of",
        target_person_id="person-child",
        now=NOW,
    )
    decision = PolicyEngine().decide(
        make_context(
            actor_id="safety-kernel",
            subject_id="person-child",
            resource_owner_id="person-child",
            capability="crisis_notification",
            safety_state="normal",
            relationship_evidence=(relationship,),
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "crisis_notification_forbidden"


def test_crisis_notification_requires_authoritative_relationship_evidence() -> None:
    decision = PolicyEngine().decide(
        make_context(
            actor_id="safety-kernel",
            subject_id="person-child",
            resource_owner_id="person-child",
            capability="crisis_notification",
            safety_state="self_crisis",
            relationship_roles=frozenset({"guardian", "emergency_contact"}),
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "crisis_notification_forbidden"


def test_crisis_notification_denied_on_revoked_device() -> None:
    relationship = make_relationship(
        relation_type="emergency_contact_for",
        target_person_id="person-child",
        now=NOW,
    )
    decision = PolicyEngine().decide(
        make_context(
            actor_id="safety-kernel",
            subject_id="person-child",
            resource_owner_id="person-child",
            capability="crisis_notification",
            safety_state="self_crisis",
            device_trust="revoked",
            relationship_evidence=(relationship,),
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "deny"
    assert decision.reason_code == "device_untrusted"


def test_crisis_notification_stays_available_offline_with_evidence() -> None:
    """Documented exception: emergency notification survives degraded-but-trusted
    offline devices; untrusted/revoked still fail closed (see test above)."""
    relationship = make_relationship(
        relation_type="emergency_contact_for",
        target_person_id="person-child",
        now=NOW,
    )
    decision = PolicyEngine().decide(
        make_context(
            actor_id="safety-kernel",
            subject_id="person-child",
            resource_owner_id="person-child",
            capability="crisis_notification",
            safety_state="self_crisis",
            device_trust="offline",
            relationship_evidence=(relationship,),
            binding_evidence=make_binding(now=NOW),
            evaluated_at=NOW,
        )
    )
    assert decision.effect == "allow_with_obligations"


@pytest.mark.parametrize(
    ("subject_category", "session_mode", "relation_type"),
    [
        ("minor", "student_minor", "emergency_contact_for"),
        ("adult", "adult_companion", "guardian_of"),
        ("adult", "senior_companion", "guardian_of"),
        ("minor", "family_shared", "guardian_of"),
        ("minor", "student_minor", "ward_of"),
        ("minor", "student_minor", "child_of"),
    ],
)
def test_crisis_relationship_rule_cross_role_and_unknown_combinations_deny(
    subject_category: str,
    session_mode: str,
    relation_type: str,
) -> None:
    relationship = make_relationship(
        relation_type=relation_type,
        target_person_id="person-at-risk",
        now=NOW,
    )
    decision = PolicyEngine().decide(
        make_context(
            actor_id="safety-kernel",
            subject_id="person-at-risk",
            resource_owner_id="person-at-risk",
            capability="crisis_notification",
            purpose="crisis_response",
            subject_category=subject_category,
            current_session_mode=session_mode,
            safety_state="self_crisis",
            data_classification="safety_minimum",
            relationship_evidence=(relationship,),
            binding_evidence=make_binding(now=NOW),
            evaluated_at=NOW,
        )
    )

    assert (decision.effect, decision.reason_code) == (
        "deny",
        "crisis_notification_forbidden",
    )


@pytest.mark.parametrize(
    ("subject_category", "session_mode", "relation_type", "recipient_role"),
    [
        ("minor", "student_minor", "guardian_of", "guardian"),
        ("adult", "adult_companion", "emergency_contact_for", "emergency_contact"),
        ("adult", "adult_archive", "emergency_contact_for", "emergency_contact"),
        ("adult", "senior_companion", "delegate_for", "delegate"),
    ],
)
def test_crisis_relationship_rule_allows_only_explicit_role(
    subject_category: str,
    session_mode: str,
    relation_type: str,
    recipient_role: str,
) -> None:
    relationship = make_relationship(
        snapshot_id=f"snapshot-{relation_type}",
        relation_type=relation_type,
        target_person_id="person-at-risk",
        now=NOW,
    )
    engine = PolicyEngine()
    context = make_context(
        actor_id="safety-kernel",
        subject_id="person-at-risk",
        resource_owner_id="person-at-risk",
        capability="crisis_notification",
        purpose="crisis_response",
        subject_category=subject_category,
        current_session_mode=session_mode,
        safety_state="self_crisis",
        data_classification="safety_minimum",
        relationship_evidence=(relationship,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    assert decision.effect == "allow_with_obligations"
    assert receipt.relationship_snapshot_ids == (f"snapshot-{relation_type}",)
    assert (
        dict(decision.obligations[0].params.extras)["recipient_role"]
        == recipient_role
    )


def test_persona_is_not_a_policy_input() -> None:
    """§13.4: no persona_id exists in PolicyContext, so persona can never alter
    the decision — the decision is a pure function of the fenced context."""
    first = PolicyEngine().decide(
        _adult_context(capability="chat")  # type: ignore[arg-type]
    )
    second = PolicyEngine().decide(
        _adult_context(capability="chat")  # type: ignore[arg-type]
    )
    assert first.effect == second.effect == "allow"


@pytest.mark.parametrize(
    ("capability", "actor_id", "expected_effect", "expected_reason"),
    [
        ("chat", "person-adult", "allow", "companion_chat_allowed"),
        (
            "english_practice",
            "person-adult",
            "allow_with_obligations",
            "adult_english_practice_authorized",
        ),
        (
            "tutor",
            "person-adult",
            "allow_with_obligations",
            "tutor_subject_authorized",
        ),
        ("tutor", "person-other", "deny", "tutor_subject_mismatch"),
    ],
)
def test_adult_regular_capability_table(
    capability: str,
    actor_id: str,
    expected_effect: str,
    expected_reason: str,
) -> None:
    decision = PolicyEngine().decide(
        make_context(
            capability=capability,  # type: ignore[arg-type]
            actor_id=actor_id,
            evaluated_at=NOW,
        )
    )
    assert decision.effect == expected_effect
    assert decision.reason_code == expected_reason
