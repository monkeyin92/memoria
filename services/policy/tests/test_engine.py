from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from services.policy.context import canonical_runtime_purpose_for_capability
from services.policy.engine import PolicyContext, PolicyEngine
from services.policy.obligations import obligation_codes
from services.policy.receipt_store import InMemoryPolicyReceiptWriter
from services.policy.tests.fakes import (
    make_binding,
    make_consent,
    make_consent_snapshot_ref,
    make_relationship,
)


def _context(**overrides: object) -> PolicyContext:
    values: dict[str, object] = {
        "actor_id": "person-admin",
        "subject_id": None,
        "resource_owner_id": None,
        "device_id": "device-1",
        "capability": "voice_clone_use",
        "declared_device_mode": "self_use",
        "current_session_mode": "unknown_safe",
        "relationship_roles": frozenset({"device_admin"}),
        "subject_category": "unknown",
        "age_band": "unknown",
        "speaker_state": "unconfirmed",
        "speaker_confidence": None,
        "consent_kinds": frozenset(),
        "device_trust": "trusted",
        "safety_state": "normal",
        "jurisdiction": "CN",
        "data_classification": "biometric",
        "binding_id": "binding-1",
        "binding_version": 1,
        "session_id": "session-1",
        "session_epoch": 1,
        "runtime_profile_id": "profile-1",
        "subject_revision": 0,
        "evaluated_at": datetime(2026, 8, 9, tzinfo=UTC),
    }
    values.update(overrides)
    if "purpose" not in values:
        values["purpose"] = canonical_runtime_purpose_for_capability(
            values["capability"]
        )
    consents = values.get("consent_evidence", ())
    if consents and "consent_snapshot_evidence" not in values:
        values["consent_snapshot_evidence"] = (
            make_consent_snapshot_ref(
                snapshot_id=consents[0].snapshot_id,  # type: ignore[index,union-attr]
                revision=consents[0].version,  # type: ignore[index,union-attr]
                canonical_hash=consents[0].canonical_hash,  # type: ignore[index,union-attr]
                consents=consents,  # type: ignore[arg-type]
                now=values["evaluated_at"],  # type: ignore[arg-type]
            ),
        )
    return PolicyContext(**values)  # type: ignore[arg-type]


def test_unknown_subject_cannot_use_adult_sensitive_capability() -> None:
    decision = PolicyEngine().decide(_context())

    assert decision.effect == "deny"
    assert decision.reason_code == "subject_unconfirmed"
    assert decision.obligations == ()


def test_explicit_deny_writes_audit_receipt_without_escalation() -> None:
    writer = InMemoryPolicyReceiptWriter()
    engine = PolicyEngine(
        receipt_id_factory=lambda: "receipt-deny-1",
        receipt_writer=writer,
    )

    decision = engine.deny(_context(), reason_code="capability_not_in_runtime_profile")

    assert decision.effect == "deny"
    assert decision.reason_code == "capability_not_in_runtime_profile"
    receipt = writer.get(decision.receipt_id)
    assert receipt is not None
    assert receipt.effect == "deny"
    assert receipt.capability == "voice_clone_use"
    assert receipt.reason_code == "capability_not_in_runtime_profile"
    assert receipt.runtime_profile_id == "profile-1"


def test_unknown_safe_chat_is_ephemeral_and_cannot_train() -> None:
    decision = PolicyEngine().decide(
        _context(capability="chat", data_classification="public")
    )

    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "unknown_safe_ephemeral"
    assert obligation_codes(decision.obligations) == (
        "DO_NOT_PERSIST",
        "DO_NOT_WRITE_LEARNING_PROGRESS",
        "NO_MODEL_TRAINING",
        "REQUIRE_SPEAKER_CONFIRMATION",
    )


def test_minor_cannot_gain_voice_clone_from_guardian_or_device_admin_roles() -> None:
    decision = PolicyEngine().decide(
        _context(
            subject_id="person-child",
            resource_owner_id="person-child",
            subject_category="minor",
            age_band="under_14",
            speaker_state="confirmed",
            speaker_confidence=0.99,
            relationship_roles=frozenset({"guardian", "device_admin"}),
            consent_kinds=frozenset({"voice_clone"}),
            current_session_mode="student_minor",
        )
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "minor_capability_forbidden"


def test_adult_voice_clone_requires_subject_owned_consent() -> None:
    decision = PolicyEngine().decide(
        _context(
            actor_id="person-adult",
            subject_id="person-adult",
            resource_owner_id="person-adult",
            subject_category="adult",
            age_band="adult",
            speaker_state="confirmed",
            speaker_confidence=0.99,
            relationship_roles=frozenset({"self", "primary_subject"}),
            current_session_mode="adult_companion",
        )
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "subject_consent_required"


def test_adult_subject_can_use_voice_clone_with_step_up_obligations() -> None:
    # Matrix change (V2): adult sensitive capabilities require authoritative
    # consent evidence (bare consent_kinds are no longer authoritative).
    decision = PolicyEngine().decide(
        _context(
            actor_id="person-adult",
            subject_id="person-adult",
            resource_owner_id="person-adult",
            subject_category="adult",
            age_band="adult",
            speaker_state="confirmed",
            speaker_confidence=0.99,
            relationship_roles=frozenset({"self", "primary_subject"}),
            consent_kinds=frozenset({"voice_clone"}),
            current_session_mode="adult_companion",
            consent_evidence=(
                make_consent(
                    capability="voice_clone_use",
                    purpose="voice_clone",
                    now=_context().evaluated_at,
                ),
            ),
            binding_evidence=make_binding(now=_context().evaluated_at),
        )
    )

    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "adult_subject_authorized"
    assert obligation_codes(decision.obligations) == (
        "REQUIRE_STEP_UP_AUTH",
        "REQUIRE_SUBJECT_APPROVAL",
        "WRITE_POLICY_RECEIPT",
        "NO_MODEL_TRAINING",
    )


def test_subject_or_session_epoch_change_invalidates_policy_receipt() -> None:
    evaluated_at = datetime(2026, 8, 9, tzinfo=UTC)
    context = _context(
        actor_id="person-adult",
        subject_id="person-adult",
        resource_owner_id="person-adult",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        speaker_confidence=0.99,
        relationship_roles=frozenset({"self"}),
        consent_kinds=frozenset({"voice_clone"}),
        current_session_mode="adult_companion",
        evaluated_at=evaluated_at,
    )
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-1")

    decision = engine.decide(context)

    assert decision.receipt_id == "receipt-1"
    # Engine V2 bumps the default policy bundle version.
    assert decision.policy_version == "multi-subject-v2"
    assert engine.receipt_valid(
        decision,
        context=context,
        now=evaluated_at + timedelta(minutes=1),
    )
    assert not engine.receipt_valid(
        decision,
        context=replace(context, subject_id="person-other", session_epoch=2),
        now=evaluated_at + timedelta(minutes=1),
    )


@pytest.mark.parametrize(
    "capability",
    [
        "voice_clone_use",
        "digital_self_preview",
        "legacy_grant_create",
        "device_ownership_transfer",
    ],
)
def test_minor_never_receives_adult_sensitive_capabilities(capability: str) -> None:
    # Matrix change (V2): the hard-forbidden minor set is now only
    # voice_clone_use / digital_self_preview / legacy_grant_create /
    # device_ownership_transfer; voice_profile_create is enrollable under
    # governance (separate test) and payment / raw_audio_retention /
    # model_training_contribution are default-denied with their own reason code.
    decision = PolicyEngine().decide(
        _context(
            capability=capability,
            actor_id="person-child",
            subject_id="person-child",
            resource_owner_id="person-child",
            subject_category="minor",
            age_band="14_17",
            speaker_state="confirmed",
            relationship_roles=frozenset({"self", "primary_subject"}),
            consent_kinds=frozenset(
                {
                    "voice_profile",
                    "voice_clone",
                    "digital_self",
                    "legacy",
                    "payment",
                    "raw_audio",
                    "model_training",
                    "device_transfer",
                }
            ),
            current_session_mode="student_minor",
        )
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "minor_capability_forbidden"


@pytest.mark.parametrize(
    "capability",
    ["payment", "raw_audio_retention", "model_training_contribution"],
)
def test_minor_default_denied_capabilities_keep_minor_denied(capability: str) -> None:
    # Matrix (V2): payment / raw audio / model training stay denied for minors
    # by default and cannot be opened by profile or admin claims.
    decision = PolicyEngine().decide(
        _context(
            capability=capability,
            actor_id="person-child",
            subject_id="person-child",
            resource_owner_id="person-child",
            subject_category="minor",
            age_band="14_17",
            speaker_state="confirmed",
            relationship_roles=frozenset({"self", "primary_subject"}),
            consent_kinds=frozenset(
                {"payment", "raw_audio", "model_training", "device_transfer"}
            ),
            current_session_mode="student_minor",
        )
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "minor_capability_denied"


def test_minor_voice_profile_create_requires_governance_and_evidence() -> None:
    # Matrix change (V2): voice_profile_create (voiceprint enrollment only, not
    # cloning) is allowed for minors under governance + authoritative evidence.
    evaluated_at = _context().evaluated_at
    denied = PolicyEngine().decide(
        _context(
            capability="voice_profile_create",
            actor_id="person-child",
            subject_id="person-child",
            resource_owner_id="person-child",
            subject_category="minor",
            age_band="under_14",
            speaker_state="confirmed",
            relationship_roles=frozenset({"self"}),
            current_session_mode="student_minor",
        )
    )
    allowed = PolicyEngine().decide(
        _context(
            capability="voice_profile_create",
            actor_id="person-child",
            subject_id="person-child",
            resource_owner_id="person-child",
            subject_category="minor",
            age_band="under_14",
            speaker_state="confirmed",
            relationship_roles=frozenset({"self"}),
            current_session_mode="student_minor",
            relationship_evidence=(
                make_relationship(
                    relation_type="guardian_of",
                    target_person_id="person-child",
                    now=evaluated_at,
                ),
            ),
            consent_evidence=(
                make_consent(
                    capability="voice_profile_create",
                    subject_id="person-child",
                    actor_id="person-parent",
                    actor_kind="guardian",
                    purpose="voice_profile",
                    now=evaluated_at,
                ),
            ),
            binding_evidence=make_binding(now=evaluated_at),
        )
    )

    assert (denied.effect, denied.reason_code) == ("deny", "consent_evidence_required")
    assert allowed.effect == "allow_with_obligations"
    assert obligation_codes(allowed.obligations) == (
        "NO_MODEL_TRAINING",
        "WRITE_POLICY_RECEIPT",
    )


def test_guardian_relationship_does_not_grant_private_memory_read() -> None:
    decision = PolicyEngine().decide(
        _context(
            capability="memory_recall_private",
            actor_id="person-parent",
            subject_id="person-child",
            resource_owner_id="person-child",
            subject_category="minor",
            age_band="under_14",
            speaker_state="confirmed",
            relationship_roles=frozenset({"guardian", "device_admin"}),
            consent_kinds=frozenset({"guardian_summary"}),
            current_session_mode="student_minor",
            data_classification="private",
        )
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "private_memory_subject_only"


def test_confirmed_adult_subject_can_read_own_private_memory() -> None:
    # Matrix change (V2): adult private memory recall requires authoritative
    # consent evidence instead of the old plain allow.
    decision = PolicyEngine().decide(
        _context(
            capability="memory_recall_private",
            actor_id="person-adult",
            subject_id="person-adult",
            resource_owner_id="person-adult",
            subject_category="adult",
            age_band="adult",
            speaker_state="confirmed",
            relationship_roles=frozenset({"self"}),
            current_session_mode="adult_companion",
            data_classification="private",
            consent_evidence=(
                make_consent(
                    capability="memory_recall_private",
                    purpose="memory_recall",
                    now=_context().evaluated_at,
                ),
            ),
            binding_evidence=make_binding(now=_context().evaluated_at),
        )
    )

    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "private_memory_subject_authorized"
    assert obligation_codes(decision.obligations) == ("WRITE_POLICY_RECEIPT",)


def test_confirmed_but_unverified_subject_cannot_read_private_memory() -> None:
    decision = PolicyEngine().decide(
        _context(
            capability="memory_recall_private",
            actor_id="person-unverified",
            subject_id="person-unverified",
            resource_owner_id="person-unverified",
            subject_category="unknown",
            age_band="unknown",
            speaker_state="confirmed",
            relationship_roles=frozenset({"self"}),
            current_session_mode="unknown_safe",
            data_classification="private",
        )
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "subject_category_unverified"


def test_minor_memory_capture_requires_consent_and_is_aggregate_only() -> None:
    base = _context(
        capability="memory_capture",
        purpose="memory_capture",
        actor_id="person-child",
        subject_id="person-child",
        resource_owner_id="person-child",
        subject_category="minor",
        age_band="under_14",
        speaker_state="confirmed",
        relationship_roles=frozenset({"self"}),
        current_session_mode="student_minor",
        data_classification="private",
    )

    denied = PolicyEngine().decide(base)
    # Matrix change (V2): minor memory capture additionally requires an active
    # guardian relationship + authoritative consent evidence + active binding.
    evaluated_at = base.evaluated_at
    minor_consent = make_consent(
        capability="memory_capture",
        subject_id="person-child",
        actor_id="person-parent",
        actor_kind="guardian",
        purpose="memory_capture",
        now=evaluated_at,
    )
    allowed = PolicyEngine().decide(
        replace(
            base,
            consent_kinds=frozenset({"memory_retention"}),
            relationship_evidence=(
                make_relationship(
                    relation_type="guardian_of",
                    target_person_id="person-child",
                    now=evaluated_at,
                ),
            ),
            consent_evidence=(minor_consent,),
            consent_snapshot_evidence=(
                make_consent_snapshot_ref(
                    snapshot_id=minor_consent.snapshot_id,
                    revision=minor_consent.version,
                    canonical_hash=minor_consent.canonical_hash,
                    consents=(minor_consent,),
                    now=evaluated_at,
                ),
            ),
            binding_evidence=make_binding(now=evaluated_at),
        )
    )

    assert (denied.effect, denied.reason_code) == ("deny", "memory_consent_required")
    assert allowed.effect == "allow_with_obligations"
    assert obligation_codes(allowed.obligations) == (
        "PERSIST_AGGREGATE_ONLY",
        "RETENTION_TTL",
        "NO_MODEL_TRAINING",
        "WRITE_POLICY_RECEIPT",
    )


def test_adult_memory_capture_keeps_subject_scope_and_retention_obligations() -> None:
    # Matrix change (V2): adult memory capture requires authoritative consent
    # evidence (bare consent_kinds are no longer authoritative).
    decision = PolicyEngine().decide(
        _context(
            capability="memory_capture",
            purpose="memory_capture",
            actor_id="person-adult",
            subject_id="person-adult",
            resource_owner_id="person-adult",
            subject_category="adult",
            age_band="adult",
            speaker_state="confirmed",
            relationship_roles=frozenset({"self"}),
            consent_kinds=frozenset({"memory_retention"}),
            current_session_mode="adult_companion",
            data_classification="private",
            consent_evidence=(
                make_consent(
                    capability="memory_capture",
                    purpose="memory_capture",
                    now=_context().evaluated_at,
                ),
            ),
            binding_evidence=make_binding(now=_context().evaluated_at),
        )
    )

    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "adult_memory_authorized"
    assert obligation_codes(decision.obligations) == (
        "RETENTION_TTL",
        "NO_MODEL_TRAINING",
        "WRITE_POLICY_RECEIPT",
    )


def test_guardian_summary_is_aggregate_only_and_does_not_require_voice_identity() -> None:
    # Matrix change (V2): guardian summary requires authoritative guardian
    # relationship + consent evidence; MINIMAL_NOTIFICATION_CONTENT obligation
    # added; speaker_state normalized to a contract enum value (the old
    # "not_applicable" string is not a valid SpeakerStateValue).
    decision = PolicyEngine().decide(
        _context(
            capability="guardian_summary_view",
            actor_id="person-parent",
            subject_id="person-child",
            resource_owner_id="person-child",
            subject_category="minor",
            age_band="under_14",
            speaker_state="confirmed",
            relationship_roles=frozenset({"guardian"}),
            consent_kinds=frozenset({"guardian_summary"}),
            current_session_mode="student_minor",
            data_classification="aggregate",
            relationship_evidence=(
                make_relationship(
                    relation_type="guardian_of",
                    source_person_id="person-parent",
                    target_person_id="person-child",
                    now=_context().evaluated_at,
                ),
            ),
            consent_evidence=(
                make_consent(
                    capability="guardian_summary_view",
                    subject_id="person-child",
                    actor_id="person-parent",
                    actor_kind="guardian",
                    purpose="guardian_summary",
                    now=_context().evaluated_at,
                ),
            ),
            binding_evidence=make_binding(now=_context().evaluated_at),
        )
    )

    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "guardian_summary_authorized"
    assert obligation_codes(decision.obligations) == (
        "REDACT_TRANSCRIPT",
        "MINIMAL_NOTIFICATION_CONTENT",
        "WRITE_POLICY_RECEIPT",
    )


def test_confirmed_adult_companion_chat_is_allowed_without_identity_repetition() -> None:
    decision = PolicyEngine().decide(
        _context(
            capability="chat",
            actor_id="person-adult",
            subject_id="person-adult",
            resource_owner_id="person-adult",
            subject_category="adult",
            age_band="adult",
            speaker_state="confirmed",
            relationship_roles=frozenset({"self"}),
            current_session_mode="adult_companion",
            data_classification="ephemeral",
        )
    )

    assert decision.effect == "allow"
    assert decision.reason_code == "companion_chat_allowed"
    assert "ai_identity_every_turn" not in obligation_codes(decision.obligations)


def test_minor_chat_has_time_dependency_and_event_identity_guards() -> None:
    # Matrix change (V2): minor chat requires governance (active guardian
    # relationship) + authoritative consent evidence + active binding.
    decision = PolicyEngine().decide(
        _context(
            capability="chat",
            actor_id="person-child",
            subject_id="person-child",
            resource_owner_id="person-child",
            subject_category="minor",
            age_band="under_14",
            speaker_state="confirmed",
            relationship_roles=frozenset({"self"}),
            current_session_mode="student_minor",
            data_classification="ephemeral",
            relationship_evidence=(
                make_relationship(
                    relation_type="guardian_of",
                    target_person_id="person-child",
                    now=_context().evaluated_at,
                ),
            ),
            consent_evidence=(
                make_consent(
                    capability="chat",
                    subject_id="person-child",
                    actor_id="person-parent",
                    actor_kind="guardian",
                    purpose="user_request",
                    now=_context().evaluated_at,
                ),
            ),
            binding_evidence=make_binding(now=_context().evaluated_at),
        )
    )

    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "minor_companion_guarded"
    assert obligation_codes(decision.obligations) == (
        "MAX_SESSION_SECONDS",
        "QUIET_HOURS",
        "DEPENDENCY_GUARD",
        "AI_IDENTITY_CLARIFICATION",
        "NO_MODEL_TRAINING",
    )


def test_unknown_tutor_is_temporary_and_cannot_write_progress() -> None:
    # Matrix change (V2): unknown_safe no longer allows tutor — the corrected
    # decision matrix allows only chat/english_practice in unknown_safe and
    # denies tutor (subject_unconfirmed).
    decision = PolicyEngine().decide(
        _context(
            capability="tutor",
            current_session_mode="unknown_safe",
            data_classification="ephemeral",
        )
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "subject_unconfirmed"
    assert decision.obligations == ()


def test_confirmed_minor_tutor_writes_only_subject_scoped_progress() -> None:
    # Matrix change (V2): minor tutor requires governance + authoritative
    # consent evidence (actor must be the subject themselves).
    decision = PolicyEngine().decide(
        _context(
            capability="tutor",
            actor_id="person-child",
            subject_id="person-child",
            resource_owner_id="person-child",
            subject_category="minor",
            age_band="14_17",
            speaker_state="confirmed",
            relationship_roles=frozenset({"self"}),
            current_session_mode="student_minor",
            data_classification="study_progress",
            relationship_evidence=(
                make_relationship(
                    relation_type="guardian_of",
                    target_person_id="person-child",
                    now=_context().evaluated_at,
                ),
            ),
            consent_evidence=(
                make_consent(
                    capability="tutor",
                    subject_id="person-child",
                    actor_id="person-parent",
                    actor_kind="guardian",
                    purpose="user_request",
                    now=_context().evaluated_at,
                ),
            ),
            binding_evidence=make_binding(now=_context().evaluated_at),
        )
    )

    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "minor_tutor_guarded"
    assert obligation_codes(decision.obligations) == (
        "WRITE_SUBJECT_SCOPED_PROGRESS",
        "MAX_SESSION_SECONDS",
        "QUIET_HOURS",
        "NO_MODEL_TRAINING",
        "WRITE_POLICY_RECEIPT",
    )


@pytest.mark.parametrize(
    ("capability", "consent", "expected_obligations"),
    [
        (
            "voice_profile_create",
            "voice_profile",
            (
                "REQUIRE_STEP_UP_AUTH",
                "REQUIRE_SUBJECT_APPROVAL",
                "WRITE_POLICY_RECEIPT",
                "NO_MODEL_TRAINING",
            ),
        ),
        (
            "digital_self_preview",
            "digital_self",
            (
                "REQUIRE_STEP_UP_AUTH",
                "REQUIRE_SUBJECT_APPROVAL",
                "AI_IDENTITY_CLARIFICATION",
                "WRITE_POLICY_RECEIPT",
            ),
        ),
        (
            "legacy_grant_create",
            "legacy",
            (
                "REQUIRE_STEP_UP_AUTH",
                "REQUIRE_SUBJECT_APPROVAL",
                "AI_IDENTITY_CLARIFICATION",
                "WRITE_POLICY_RECEIPT",
            ),
        ),
        (
            "payment",
            "payment",
            (
                "REQUIRE_STEP_UP_AUTH",
                "REQUIRE_SUBJECT_APPROVAL",
                "WRITE_POLICY_RECEIPT",
            ),
        ),
        (
            "raw_audio_retention",
            "raw_audio",
            (
                "REQUIRE_STEP_UP_AUTH",
                "REQUIRE_SUBJECT_APPROVAL",
                "RETENTION_TTL",
                "NO_MODEL_TRAINING",
                "WRITE_POLICY_RECEIPT",
            ),
        ),
        (
            "model_training_contribution",
            "model_training",
            (
                "REQUIRE_STEP_UP_AUTH",
                "REQUIRE_SUBJECT_APPROVAL",
                "RETENTION_TTL",
                "WRITE_POLICY_RECEIPT",
            ),
        ),
        (
            "device_ownership_transfer",
            "device_transfer",
            (
                "REQUIRE_STEP_UP_AUTH",
                "REQUIRE_SUBJECT_APPROVAL",
                "WRITE_POLICY_RECEIPT",
            ),
        ),
    ],
)
def test_adult_sensitive_capabilities_require_own_consent_and_obligations(
    capability: str,
    consent: str,
    expected_obligations: tuple[str, ...],
) -> None:
    context = _context(
        capability=capability,
        purpose=consent,
        actor_id="person-adult",
        subject_id="person-adult",
        resource_owner_id="person-adult",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        relationship_roles=frozenset({"self"}),
        current_session_mode="adult_companion",
    )

    denied = PolicyEngine().decide(context)
    # Matrix change (V2): the allowed variant now needs authoritative consent
    # evidence (bare consent_kinds are no longer authoritative).
    authority_consent = make_consent(
        capability=capability,  # type: ignore[arg-type]
        purpose=consent,
        now=context.evaluated_at,
    )
    allowed = PolicyEngine().decide(
        replace(
            context,
            consent_kinds=frozenset({consent}),
            consent_evidence=(authority_consent,),
            consent_snapshot_evidence=(
                make_consent_snapshot_ref(
                    snapshot_id=authority_consent.snapshot_id,
                    revision=authority_consent.version,
                    canonical_hash=authority_consent.canonical_hash,
                    consents=(authority_consent,),
                    now=context.evaluated_at,
                ),
            ),
            binding_evidence=make_binding(now=context.evaluated_at),
        )
    )

    assert (denied.effect, denied.reason_code) == ("deny", "subject_consent_required")
    assert allowed.effect == "allow_with_obligations"
    assert allowed.reason_code == "adult_subject_authorized"
    assert obligation_codes(allowed.obligations) == expected_obligations


def test_crisis_notification_never_targets_contacts_for_unknown_speaker() -> None:
    decision = PolicyEngine().decide(
        _context(
            capability="crisis_notification",
            safety_state="self_crisis",
            relationship_roles=frozenset({"emergency_contact"}),
        )
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "crisis_subject_unconfirmed"


def test_confirmed_minor_crisis_notification_is_minimized_and_receipted() -> None:
    # Matrix change (V2): crisis notification requires authoritative
    # relationship evidence (bare relationship_roles are no longer enough).
    decision = PolicyEngine().decide(
        _context(
            capability="crisis_notification",
            actor_id="safety-kernel",
            subject_id="person-child",
            resource_owner_id="person-child",
            subject_category="minor",
            age_band="under_14",
            speaker_state="confirmed",
            speaker_confidence=0.97,
            relationship_roles=frozenset({"guardian", "emergency_contact"}),
            safety_state="self_crisis",
            current_session_mode="student_minor",
            data_classification="safety_minimum",
            relationship_evidence=(
                make_relationship(
                    relation_type="guardian_of",
                    target_person_id="person-child",
                    now=_context().evaluated_at,
                ),
                make_relationship(
                    relationship_id="rel-emergency-1",
                    snapshot_id="rel-snap-emergency-1",
                    relation_type="emergency_contact_for",
                    target_person_id="person-child",
                    now=_context().evaluated_at,
                ),
            ),
            binding_evidence=make_binding(now=_context().evaluated_at),
        )
    )

    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "crisis_notification_authorized"
    assert obligation_codes(decision.obligations) == (
        "NOTIFY_EMERGENCY_CONTACT",
        "MINIMAL_NOTIFICATION_CONTENT",
        "WRITE_POLICY_RECEIPT",
    )


def test_policy_decision_writes_an_immutable_receipt() -> None:
    writer = InMemoryPolicyReceiptWriter()
    context = _context(
        capability="chat",
        actor_id="person-adult",
        subject_id="person-adult",
        resource_owner_id="person-adult",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        relationship_roles=frozenset({"self"}),
        current_session_mode="adult_companion",
    )
    engine = PolicyEngine(
        receipt_id_factory=lambda: "receipt-immutable",
        receipt_writer=writer,
    )

    decision = engine.decide(context)
    receipt = writer.get("receipt-immutable")

    assert receipt is not None
    assert receipt.receipt_id == decision.receipt_id
    assert receipt.effect == "allow"
    assert receipt.context_hash == decision.context_hash
    assert receipt.subject_id == "person-adult"
    assert receipt.session_epoch == 1


def test_same_idempotency_key_and_context_produce_same_receipt_id() -> None:
    context = _context(
        capability="chat",
        idempotency_key="decision-request-1",
    )

    first = PolicyEngine().decide(context)
    second = PolicyEngine().decide(context)

    assert first.receipt_id == second.receipt_id
    assert first.receipt_id.startswith("receipt-")


def test_receipt_id_for_matches_decide_for_idempotent_context() -> None:
    context = _context(
        capability="chat",
        idempotency_key="request-1",
    )
    engine = PolicyEngine()

    assert engine.receipt_id_for(context) == engine.decide(context).receipt_id


def test_same_idempotency_scope_and_key_ignore_retry_time_and_runtime_fields() -> None:
    first_context = _context(
        capability="chat",
        idempotency_key="request-1",
    )
    retry_context = replace(
        first_context,
        evaluated_at=first_context.evaluated_at + timedelta(seconds=1),
        device_trust="verified",
        safety_state="concern",
        data_classification="ephemeral",
        speaker_confidence=0.1,
    )

    first = PolicyEngine().decide(first_context)
    retry = PolicyEngine().decide(retry_context)

    assert first.receipt_id == retry.receipt_id


@pytest.mark.parametrize(
    "scope_change",
    [
        {"actor_id": "person-other"},
        {"subject_id": "person-other"},
        {"resource_owner_id": "person-other"},
        {"device_id": "device-other"},
        {"binding_id": "binding-other"},
        {"binding_version": 2},
        {"session_id": "session-other"},
        {"capability": "english_practice"},
        {"purpose": "runtime_profile_issue"},
    ],
)
def test_same_idempotency_key_is_isolated_by_stable_scope(
    scope_change: dict[str, object],
) -> None:
    original = _context(capability="chat", idempotency_key="request-1")
    changed = replace(original, **scope_change)

    first = PolicyEngine().decide(original)
    second = PolicyEngine().decide(changed)

    assert first.receipt_id != second.receipt_id


def test_different_idempotency_keys_produce_different_receipt_ids() -> None:
    first = PolicyEngine().decide(
        _context(capability="chat", idempotency_key="decision-request-1")
    )
    second = PolicyEngine().decide(
        _context(capability="chat", idempotency_key="decision-request-2")
    )

    assert first.receipt_id != second.receipt_id


def test_without_idempotency_key_receipt_factory_remains_supported() -> None:
    decision = PolicyEngine(receipt_id_factory=lambda: "random-receipt").decide(
        _context(capability="chat")
    )

    assert decision.receipt_id == "random-receipt"
