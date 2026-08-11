from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    Capability as GeneratedCapability,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    CapabilityValue,
    ObligationParams,
    PolicyObligation,
    PolicyObligationSpec,
    PurposeValue,
    RuntimeProfileV2,
)
from services.policy.engine import PolicyEngine
from services.policy.receipt_store import InMemoryPolicyReceiptWriter
from services.session_runtime.profile_service import (
    PROFILE_ISSUE_DEFERRED_CAPABILITIES,
    PROFILE_ISSUE_SESSION_CAPABILITIES,
    RUNTIME_PROFILE_PAYLOAD_SCHEMA,
    InMemoryRuntimeAuthority,
    PersonaAssignment,
    RuntimeProfile,
    RuntimeProfileRejected,
    RuntimeProfileService,
    StartSessionCommand,
    SubjectFacts,
    SwitchSubjectCommand,
    canonical_profile_issue_purpose,
    canonical_runtime_decision_purpose,
    runtime_profile_wire_payload,
    sign_runtime_profile_payload,
)
from services.session_runtime.subject_resolver import (
    BindingSnapshot,
    SubjectCandidate,
)


def _obligation_codes(
    obligations: tuple[PolicyObligationSpec, ...],
) -> set[str]:
    return {obligation.code.value for obligation in obligations}


_EXPECTED_PROFILE_ISSUE_PURPOSES: dict[CapabilityValue, PurposeValue] = {
    "chat": "runtime_profile_issue",
    "tutor": "runtime_profile_issue",
    "english_practice": "runtime_profile_issue",
    "memory_capture": "memory_capture",
    "memory_promotion": "memory_promotion",
    "family_shared_memory_proposal": "family_shared_memory_proposal",
    "family_shared_memory_approval": "family_shared_memory_approval",
    "family_shared_memory_promotion": "family_shared_memory_promotion",
    "memory_recall_private": "runtime_profile_issue",
    "guardian_summary_view": "runtime_profile_issue",
    "voice_profile_create": "runtime_profile_issue",
    "voice_clone_use": "runtime_profile_issue",
    "digital_self_preview": "runtime_profile_issue",
    "legacy_grant_create": "runtime_profile_issue",
    "payment": "runtime_profile_issue",
    "raw_audio_retention": "runtime_profile_issue",
    "model_training_contribution": "runtime_profile_issue",
    "crisis_notification": "runtime_profile_issue",
    "device_ownership_transfer": "runtime_profile_issue",
}
_EXPECTED_PROFILE_ISSUE_DEFERRED: frozenset[CapabilityValue] = frozenset(
    {
        "memory_capture",
        "memory_promotion",
        "family_shared_memory_proposal",
        "family_shared_memory_approval",
        "family_shared_memory_promotion",
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
_EXPECTED_PROFILE_ISSUE_SESSION: frozenset[CapabilityValue] = frozenset(
    {
        "chat",
        "tutor",
        "english_practice",
        "memory_recall_private",
        "guardian_summary_view",
    }
)
_EXPECTED_RUNTIME_DECISION_PURPOSES: dict[CapabilityValue, PurposeValue] = {
    "chat": "user_request",
    "tutor": "user_request",
    "english_practice": "user_request",
    "memory_capture": "memory_capture",
    "memory_promotion": "memory_promotion",
    "family_shared_memory_proposal": "family_shared_memory_proposal",
    "family_shared_memory_approval": "family_shared_memory_approval",
    "family_shared_memory_promotion": "family_shared_memory_promotion",
    "memory_recall_private": "memory_recall",
    "guardian_summary_view": "guardian_summary",
    "voice_profile_create": "voice_profile",
    "voice_clone_use": "voice_clone",
    "digital_self_preview": "digital_self",
    "legacy_grant_create": "legacy",
    "payment": "payment",
    "raw_audio_retention": "raw_audio",
    "model_training_contribution": "model_training",
    "crisis_notification": "crisis_response",
    "device_ownership_transfer": "device_transfer",
}


def _purpose_matrix_service() -> tuple[
    RuntimeProfileService,
    InMemoryPolicyReceiptWriter,
]:
    writer = InMemoryPolicyReceiptWriter()
    authority = InMemoryRuntimeAuthority(
        bindings=(
            BindingSnapshot(
                binding_id="binding-purpose-matrix",
                device_id="device-purpose-matrix",
                binding_version=1,
                declared_mode="self_use",
                primary_subject_ids=("person-adult",),
                member_subject_ids=("person-adult",),
            ),
        ),
        subjects=(SubjectFacts("person-adult", "adult", "adult", 1),),
        personas=(PersonaAssignment("persona-1", "starlight", 4, "new"),),
    )
    return (
        RuntimeProfileService(
            authority=authority,
            policy=PolicyEngine(
                receipt_writer=writer,
                receipt_id_factory=lambda: "receipt-purpose-matrix",
            ),
            signing_key=b"test-runtime-profile-signing-key",
            profile_id_factory=lambda: "profile-purpose-matrix",
        ),
        writer,
    )


def _issue_one_capability(
    service: RuntimeProfileService,
    capability: CapabilityValue,
) -> RuntimeProfile:
    return service.start(
        StartSessionCommand(
            session_id="session-purpose-matrix",
            device_id="device-purpose-matrix",
            actor_id="person-adult",
            app_claimed_subject_id="person-adult",
            candidates=(),
            requested_capabilities=(capability,),
            now=datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
        )
    )


def test_every_generated_capability_has_one_canonical_policy_purpose() -> None:
    assert tuple(_EXPECTED_PROFILE_ISSUE_PURPOSES) == GeneratedCapability.values()
    assert {
        capability: canonical_profile_issue_purpose(capability)
        for capability in GeneratedCapability.values()
    } == _EXPECTED_PROFILE_ISSUE_PURPOSES
    assert PROFILE_ISSUE_DEFERRED_CAPABILITIES == _EXPECTED_PROFILE_ISSUE_DEFERRED
    assert PROFILE_ISSUE_SESSION_CAPABILITIES == _EXPECTED_PROFILE_ISSUE_SESSION
    assert not (
        PROFILE_ISSUE_DEFERRED_CAPABILITIES
        & PROFILE_ISSUE_SESSION_CAPABILITIES
    )
    assert (
        PROFILE_ISSUE_DEFERRED_CAPABILITIES
        | PROFILE_ISSUE_SESSION_CAPABILITIES
    ) == frozenset(GeneratedCapability.values())


def test_every_generated_capability_has_one_runtime_decision_purpose() -> None:
    assert tuple(_EXPECTED_RUNTIME_DECISION_PURPOSES) == GeneratedCapability.values()
    assert {
        capability: canonical_runtime_decision_purpose(capability)
        for capability in GeneratedCapability.values()
    } == _EXPECTED_RUNTIME_DECISION_PURPOSES


def test_profile_issue_purpose_rejects_unknown_capability() -> None:
    with pytest.raises(ValueError, match="invalid value"):
        canonical_profile_issue_purpose(
            cast(CapabilityValue, "unknown-capability")
        )


@pytest.mark.parametrize(
    ("capability", "expected_purpose"),
    tuple(
        (capability, purpose)
        for capability, purpose in _EXPECTED_PROFILE_ISSUE_PURPOSES.items()
        if capability not in _EXPECTED_PROFILE_ISSUE_DEFERRED
    ),
)
def test_profile_issue_receipt_uses_canonical_capability_purpose(
    capability: CapabilityValue,
    expected_purpose: PurposeValue,
) -> None:
    service, writer = _purpose_matrix_service()
    _issue_one_capability(service, capability)

    receipt = writer.get("receipt-purpose-matrix")
    assert receipt is not None
    assert (receipt.capability.value, receipt.purpose.value) == (
        capability,
        expected_purpose,
    )


@pytest.mark.parametrize(
    "capability",
    tuple(_EXPECTED_PROFILE_ISSUE_DEFERRED),
)
def test_resource_scoped_action_is_not_pre_authorized_by_profile_issue(
    capability: CapabilityValue,
) -> None:
    service, writer = _purpose_matrix_service()

    profile = _issue_one_capability(service, capability)

    assert profile.capabilities == ()
    assert profile.policy_receipt_ids == ()
    assert writer.get("receipt-purpose-matrix") is None


def test_unresolved_session_gets_signed_short_lived_unknown_safe_profile() -> None:
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    authority = InMemoryRuntimeAuthority(
        bindings=(
            BindingSnapshot(
                binding_id="binding-1",
                device_id="device-1",
                binding_version=4,
                declared_mode="self_use",
                primary_subject_ids=("person-adult",),
                member_subject_ids=("person-adult",),
            ),
        ),
        subjects=(
            SubjectFacts(
                subject_id="person-adult",
                category="adult",
                age_band="adult",
                revision=2,
            ),
        ),
        personas=(
            PersonaAssignment(
                assignment_id="persona-assignment-1",
                persona_id="starlight",
                persona_version=4,
                relationship_stage="familiar",
            ),
        ),
    )
    service = RuntimeProfileService(
        authority=authority,
        policy=PolicyEngine(receipt_id_factory=lambda: "receipt-1"),
        signing_key=b"test-runtime-profile-signing-key",
        profile_id_factory=lambda: "runtime-profile-1",
    )

    profile = service.start(
        StartSessionCommand(
            session_id="session-1",
            device_id="device-1",
            actor_id="person-account-owner",
            candidates=(SubjectCandidate("person-adult", 0.71),),
            requested_capabilities=("chat", "tutor"),
            now=now,
        )
    )

    assert profile.runtime_profile_id == "runtime-profile-1"
    assert profile.active_subject_id is None
    assert profile.speaker_state == "unconfirmed"
    assert profile.service_mode == "unknown_safe"
    assert profile.session_epoch == 1
    assert profile.binding_version == 4
    # Unknown-safe keeps chat; tutor is outside the safe surface (4.3).
    assert profile.capabilities == ("chat",)
    assert _obligation_codes(profile.obligations) >= {
        "DO_NOT_PERSIST",
        "DO_NOT_WRITE_LEARNING_PROGRESS",
        "NO_MODEL_TRAINING",
        "REQUIRE_SPEAKER_CONFIRMATION",
    }
    assert profile.expires_at == now + timedelta(minutes=5)
    assert service.verify(profile, now=now + timedelta(minutes=1))


def test_unresolved_session_allows_temporary_english_practice_only() -> None:
    """Remediation 4.3: unknown-safe grants chat + temporary English practice;
    tutor and learning-progress persistence stay rejected."""

    now = datetime(2026, 8, 9, 11, 0, tzinfo=UTC)
    authority = InMemoryRuntimeAuthority(
        bindings=(
            BindingSnapshot(
                binding_id="binding-2",
                device_id="device-2",
                binding_version=1,
                declared_mode="self_use",
                primary_subject_ids=(),
                member_subject_ids=(),
            ),
        ),
        subjects=(),
        personas=(
            PersonaAssignment(
                assignment_id="persona-assignment-2",
                persona_id="starlight",
                persona_version=4,
                relationship_stage="familiar",
            ),
        ),
    )
    service = RuntimeProfileService(
        authority=authority,
        policy=PolicyEngine(receipt_id_factory=lambda: "receipt-2"),
        signing_key=b"test-runtime-profile-signing-key",
        profile_id_factory=lambda: "runtime-profile-2",
    )

    profile = service.start(
        StartSessionCommand(
            session_id="session-2",
            device_id="device-2",
            actor_id="person-account-owner",
            candidates=(),
            requested_capabilities=("chat", "tutor", "english_practice"),
            now=now,
        )
    )

    assert profile.service_mode == "unknown_safe"
    assert profile.active_subject_id is None
    assert profile.capabilities == ("chat", "english_practice")
    assert "DO_NOT_WRITE_LEARNING_PROGRESS" in _obligation_codes(profile.obligations)
    assert "DO_NOT_PERSIST" in _obligation_codes(profile.obligations)
    assert service.verify(profile, now=now + timedelta(minutes=1))


def test_child_parent_child_switches_raise_epoch_and_invalidate_old_profiles() -> None:
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    authority = InMemoryRuntimeAuthority(
        bindings=(
            BindingSnapshot(
                binding_id="binding-family",
                device_id="device-family",
                binding_version=7,
                declared_mode="family_shared",
                primary_subject_ids=("person-child",),
                member_subject_ids=("person-child", "person-parent"),
            ),
        ),
        subjects=(
            SubjectFacts("person-child", "minor", "under_14", 3),
            SubjectFacts("person-parent", "adult", "adult", 5),
        ),
        personas=(
            PersonaAssignment("persona-family", "starlight", 4, "familiar"),
        ),
    )
    profile_ids = iter(("profile-child-1", "profile-parent", "profile-child-2"))
    service = RuntimeProfileService(
        authority=authority,
        policy=PolicyEngine(),
        signing_key=b"test-runtime-profile-signing-key",
        profile_id_factory=lambda: next(profile_ids),
    )
    child = service.start(
        StartSessionCommand(
            session_id="session-family",
            device_id="device-family",
            actor_id="person-child",
            app_claimed_subject_id="person-child",
            candidates=(),
            requested_capabilities=("chat", "tutor"),
            now=now,
        )
    )

    parent = service.switch_subject(
        SwitchSubjectCommand(
            session_id="session-family",
            actor_id="person-parent",
            app_claimed_subject_id="person-parent",
            candidates=(),
            requested_capabilities=("chat", "memory_recall_private"),
            now=now + timedelta(minutes=1),
        )
    )
    child_again = service.switch_subject(
        SwitchSubjectCommand(
            session_id="session-family",
            actor_id="person-child",
            app_claimed_subject_id="person-child",
            candidates=(),
            requested_capabilities=("chat", "tutor"),
            now=now + timedelta(minutes=2),
        )
    )

    assert (child.active_subject_id, child.session_epoch) == ("person-child", 1)
    assert (parent.active_subject_id, parent.session_epoch) == ("person-parent", 2)
    assert (child_again.active_subject_id, child_again.session_epoch) == (
        "person-child",
        3,
    )
    assert not service.verify(child, now=now + timedelta(minutes=2))
    assert not service.verify(parent, now=now + timedelta(minutes=2))
    assert service.verify(child_again, now=now + timedelta(minutes=2))


def test_expired_runtime_profile_cannot_authorize_sensitive_side_effect() -> None:
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    authority = InMemoryRuntimeAuthority(
        bindings=(
            BindingSnapshot(
                binding_id="binding-1",
                device_id="device-1",
                binding_version=1,
                declared_mode="self_use",
                primary_subject_ids=("person-adult",),
                member_subject_ids=("person-adult",),
            ),
        ),
        subjects=(SubjectFacts("person-adult", "adult", "adult", 1),),
        personas=(PersonaAssignment("persona-1", "starlight", 4, "new"),),
    )
    service = RuntimeProfileService(
        authority=authority,
        policy=PolicyEngine(),
        signing_key=b"test-runtime-profile-signing-key",
    )
    profile = service.start(
        StartSessionCommand(
            session_id="session-1",
            device_id="device-1",
            actor_id="person-adult",
            app_claimed_subject_id="person-adult",
            candidates=(),
            requested_capabilities=("voice_clone_use",),
            relationship_roles=frozenset({"self"}),
            consent_kinds=frozenset({"voice_clone"}),
            now=now,
        )
    )

    with pytest.raises(RuntimeProfileRejected, match="runtime_profile_expired"):
        service.authorize(
            profile,
            capability="voice_clone_use",
            now=now + timedelta(minutes=6),
            relationship_roles=frozenset({"self"}),
            consent_kinds=frozenset({"voice_clone"}),
            data_classification="biometric",
        )


def test_decide_on_expired_profile_fails_closed() -> None:
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    authority = InMemoryRuntimeAuthority(
        bindings=(
            BindingSnapshot(
                binding_id="binding-1",
                device_id="device-1",
                binding_version=1,
                declared_mode="self_use",
                primary_subject_ids=("person-adult",),
                member_subject_ids=("person-adult",),
            ),
        ),
        subjects=(SubjectFacts("person-adult", "adult", "adult", 1),),
        personas=(PersonaAssignment("persona-1", "starlight", 4, "new"),),
    )
    service = RuntimeProfileService(
        authority=authority,
        policy=PolicyEngine(),
        signing_key=b"test-runtime-profile-signing-key",
        profile_id_factory=lambda: "profile-expired",
    )
    profile = service.start(
        StartSessionCommand(
            session_id="session-1",
            device_id="device-1",
            actor_id="person-adult",
            app_claimed_subject_id="person-adult",
            candidates=(),
            requested_capabilities=("chat",),
            now=now,
        )
    )

    with pytest.raises(RuntimeProfileRejected, match="runtime_profile_expired"):
        service.decide(
            profile,
            capability="chat",
            now=now + timedelta(minutes=6),
            data_classification="public",
        )


def test_decide_missing_profile_capability_returns_audited_deny() -> None:
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    writer = InMemoryPolicyReceiptWriter()
    authority = InMemoryRuntimeAuthority(
        bindings=(
            BindingSnapshot(
                binding_id="binding-1",
                device_id="device-1",
                binding_version=1,
                declared_mode="self_use",
                primary_subject_ids=("person-adult",),
                member_subject_ids=("person-adult",),
            ),
        ),
        subjects=(SubjectFacts("person-adult", "adult", "adult", 1),),
        personas=(PersonaAssignment("persona-1", "starlight", 4, "new"),),
    )
    receipt_ids = iter(("receipt-issue-chat", "receipt-missing-capability"))
    service = RuntimeProfileService(
        authority=authority,
        policy=PolicyEngine(
            receipt_writer=writer,
            receipt_id_factory=lambda: next(receipt_ids),
        ),
        signing_key=b"test-runtime-profile-signing-key",
        profile_id_factory=lambda: "profile-capabilities",
    )
    profile = service.start(
        StartSessionCommand(
            session_id="session-1",
            device_id="device-1",
            actor_id="person-adult",
            app_claimed_subject_id="person-adult",
            candidates=(),
            requested_capabilities=("chat",),
            now=now,
            relationship_roles=frozenset({"self"}),
            consent_kinds=frozenset({"memory_retention"}),
        )
    )
    assert profile.capabilities == ("chat",)

    decision = service.decide(
        profile,
        capability="memory_recall_private",
        now=now + timedelta(minutes=1),
        relationship_roles=frozenset({"self"}),
        consent_kinds=frozenset({"memory_retention"}),
        data_classification="private",
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "capability_not_in_runtime_profile"
    assert decision.receipt_id == "receipt-missing-capability"
    receipt = writer.get(decision.receipt_id)
    assert receipt is not None
    assert receipt.effect == "deny"
    assert receipt.capability == "memory_recall_private"


def test_subject_category_and_age_band_are_frozen_signed_fields() -> None:
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    authority = InMemoryRuntimeAuthority(
        bindings=(
            BindingSnapshot(
                binding_id="binding-1",
                device_id="device-1",
                binding_version=1,
                declared_mode="self_use",
                primary_subject_ids=("person-adult",),
                member_subject_ids=("person-adult",),
            ),
        ),
        subjects=(SubjectFacts("person-adult", "adult", "adult", 1),),
        personas=(PersonaAssignment("persona-1", "starlight", 4, "new"),),
    )
    profile_ids = iter(("profile-unknown-safe", "profile-confirmed"))
    service = RuntimeProfileService(
        authority=authority,
        policy=PolicyEngine(),
        signing_key=b"test-runtime-profile-signing-key",
        profile_id_factory=lambda: next(profile_ids),
    )

    unknown = service.start(
        StartSessionCommand(
            session_id="session-unknown",
            device_id="device-1",
            actor_id="person-adult",
            candidates=(),
            requested_capabilities=("chat",),
            now=now,
        )
    )
    # unknown-safe with a null active subject is still a valid signed profile.
    assert unknown.active_subject_id is None
    assert unknown.subject_category == "unknown"
    assert unknown.age_band == "unknown"
    assert service.verify(unknown, now=now + timedelta(minutes=1))

    confirmed = service.start(
        StartSessionCommand(
            session_id="session-confirmed",
            device_id="device-1",
            actor_id="person-adult",
            app_claimed_subject_id="person-adult",
            candidates=(),
            requested_capabilities=("chat",),
            now=now,
        )
    )
    assert confirmed.active_subject_id == "person-adult"
    assert confirmed.subject_category == "adult"
    assert confirmed.age_band == "adult"
    assert service.verify(confirmed, now=now + timedelta(minutes=1))

    # Tampering with the frozen age snapshot must invalidate the signature.
    tampered_category = replace(confirmed, subject_category="minor")
    assert not service.verify(tampered_category, now=now + timedelta(minutes=1))
    tampered_age = replace(confirmed, age_band="14_17")
    assert not service.verify(tampered_age, now=now + timedelta(minutes=1))


def test_wire_payload_is_the_single_canonical_signing_input() -> None:
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    authority = InMemoryRuntimeAuthority(
        bindings=(
            BindingSnapshot(
                binding_id="binding-1",
                device_id="device-1",
                binding_version=1,
                declared_mode="self_use",
                primary_subject_ids=("person-adult",),
                member_subject_ids=("person-adult",),
            ),
        ),
        subjects=(SubjectFacts("person-adult", "adult", "adult", 1),),
        personas=(
            PersonaAssignment(
                assignment_id="persona-assignment-1",
                persona_id="starlight",
                persona_version=4,
                relationship_stage="familiar",
            ),
        ),
    )
    service = RuntimeProfileService(
        authority=authority,
        policy=PolicyEngine(),
        signing_key=b"test-runtime-profile-signing-key",
        profile_id_factory=lambda: "profile-wire",
    )
    profile = service.start(
        StartSessionCommand(
            session_id="session-1",
            device_id="device-1",
            actor_id="person-adult",
            app_claimed_subject_id="person-adult",
            candidates=(),
            requested_capabilities=("chat",),
            now=now,
        )
    )

    payload = runtime_profile_wire_payload(profile)
    assert "signature" not in payload
    assert payload["signature_schema"] == RUNTIME_PROFILE_PAYLOAD_SCHEMA
    assert payload["persona_assignment_id"] == "persona-assignment-1"
    assert payload["persona"] == {
        "persona_id": "starlight",
        "version": 4,
        "relationship_stage": "familiar",
    }
    assert payload["subject_category"] == "adult"
    assert payload["age_band"] == "adult"
    assert payload["session_epoch"] == 1
    assert (
        sign_runtime_profile_payload(
            payload,
            signing_key=b"test-runtime-profile-signing-key",
        )
        == profile.signature
    )
    assert service.verify(profile, now=now + timedelta(minutes=1))

    parameterized_profile = replace(
        profile,
        obligations=(
            PolicyObligationSpec(
                code=PolicyObligation.POLICY_OBLIGATION_MAX_SESSION_SECONDS,
                params=ObligationParams(
                    max_session_seconds=1800,
                    retention_ttl_seconds=None,
                    quiet_hours=None,
                    extras=(),
                ),
            ),
        ),
    )
    parameterized_payload = runtime_profile_wire_payload(parameterized_profile)
    assert parameterized_payload["obligations"] == [
        {
            "code": "MAX_SESSION_SECONDS",
            "params": {
                "max_session_seconds": 1800,
                "retention_ttl_seconds": None,
                "quiet_hours": None,
                "extras": [],
            },
        }
    ]
    assert (
        RuntimeProfileV2.model_validate(parameterized_payload).obligations
        == parameterized_profile.obligations
    )


def test_runtime_profile_rejects_legacy_or_unknown_obligation_values() -> None:
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    base = RuntimeProfile(
        runtime_profile_id="profile-obligation-validation",
        device_id="device-1",
        session_id="session-1",
        actor_id="person-adult",
        binding_id="binding-1",
        binding_version=1,
        active_subject_id="person-adult",
        subject_revision=1,
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        speaker_confidence=0.99,
        service_mode="adult_companion",
        persona_assignment_id="persona-assignment-1",
        persona_id="starlight",
        persona_version=1,
        relationship_stage="new",
        policy_bundle_version="policy-v2",
        capabilities=("chat",),
        obligations=(),
        policy_receipt_ids=("receipt-1",),
        session_epoch=1,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        signature="",
    )

    with pytest.raises(ValueError):
        replace(
            base,
            obligations=cast(
                tuple[PolicyObligationSpec, ...],
                ("DO_NOT_PERSIST",),
            ),
        )
    with pytest.raises(ValueError):
        replace(
            base,
            obligations=cast(
                tuple[PolicyObligationSpec, ...],
                (
                    {
                        "code": "NOT_A_CANONICAL_OBLIGATION",
                        "params": {
                            "max_session_seconds": None,
                            "retention_ttl_seconds": None,
                            "quiet_hours": None,
                            "extras": [],
                        },
                    },
                ),
            ),
        )
