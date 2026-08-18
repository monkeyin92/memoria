"""Fail-closed RuntimeProfile parsing: HMAC verification and epoch gating."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
from services.agent.src.runtime_profile import (
    VERIFY_KEY_ENV,
    RuntimeProfile,
    is_expired_profile_payload,
    parse_runtime_profile,
    subject_context_from_profile,
    verify_runtime_profile_signature,
)
from services.agent.tests.unit.runtime_profile_test_helpers import (
    TEST_VERIFY_KEY,
    UNKNOWN_SAFE_OBLIGATIONS,
    canonical_wire_payload,
)


def test_valid_profile_parses_with_canonical_values() -> None:
    verified = parse_runtime_profile(
        canonical_wire_payload(), verify_key=TEST_VERIFY_KEY
    )
    assert verified is not None
    profile = verified.profile
    assert profile.runtime_profile_id == "rp_01J_test"
    assert profile.active_subject_id == "person_child"
    assert profile.subject_category == "minor"
    assert profile.speaker_state == "confirmed"
    assert profile.service_mode == "student_minor"
    assert profile.session_epoch == 2
    assert profile.capabilities == ("chat", "tutor", "english_practice")
    assert profile.binding_id == "bind_01J_test"
    assert profile.binding_version == 1
    assert profile.subject_revision == 1
    assert profile.age_band == "under_14"
    assert profile.persona_assignment_id == "starlight:v4"
    assert profile.persona_id == "starlight"
    assert profile.persona_version == 4
    assert profile.relationship_stage == "familiar"
    assert profile.policy_bundle_version == "cn-minor-v5"
    assert profile.policy_receipt_ids == ("receipt_1",)
    assert profile.session_id == "ses_01J_test"
    assert profile.device_id == "dev_01J_test"
    assert not profile.is_expired(datetime.now(UTC))


def test_backend_wire_fixture_verifies_with_expected_key() -> None:
    """The exact backend-shaped payload verifies against the shared key."""

    payload = canonical_wire_payload()
    assert verify_runtime_profile_signature(payload, TEST_VERIFY_KEY) is True
    assert parse_runtime_profile(payload, verify_key=TEST_VERIFY_KEY) is not None


@pytest.mark.parametrize(
    "tamper",
    [
        {"subject_category": "adult"},
        {"service_mode": "adult_companion"},
        {"speaker_state": "unconfirmed"},
        {"session_epoch": 3},
        {"active_subject_id": "person_parent"},
        {"capabilities": ["chat", "tutor"]},
        {"obligations": ["REQUIRE_GUARDIAN_APPROVAL"]},
        {"expires_at": "2099-02-01T00:00:00+00:00"},
        {"persona": {"persona_id": "axu", "version": 1, "relationship_stage": "new"}},
        {"policy_receipt_ids": ["receipt_2"]},
        {"extra_unsigned_field": "smuggled"},
    ],
)
def test_any_field_tampering_fails_verification(tamper: dict[str, object]) -> None:
    payload = canonical_wire_payload()
    payload.update(copy.deepcopy(tamper))
    assert verify_runtime_profile_signature(payload, TEST_VERIFY_KEY) is False
    assert parse_runtime_profile(payload, verify_key=TEST_VERIFY_KEY) is None


def test_wrong_or_missing_key_fails_closed() -> None:
    payload = canonical_wire_payload()
    assert parse_runtime_profile(payload, verify_key="wrong-key") is None
    assert parse_runtime_profile(payload, verify_key=None) is None
    assert parse_runtime_profile(payload, verify_key="") is None
    assert parse_runtime_profile(payload) is None


def test_verified_runtime_profile_cannot_be_constructed_outside_factory() -> None:
    """P0-1: the verified proof type is only produced by the verifier factory."""

    from services.agent.src.runtime_profile import VerifiedRuntimeProfile

    profile = RuntimeProfile(
        runtime_profile_id="rp",
        actor_id="a",
        binding_id="b",
        binding_version=1,
        subject_revision=1,
        active_subject_id="person",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        speaker_confidence=0.9,
        service_mode="adult_companion",
        persona_assignment_id="p:v1",
        persona_id="starlight",
        persona_version=4,
        relationship_stage="familiar",
        policy_bundle_version="v5",
        policy_receipt_ids=(),
        session_epoch=1,
        issued_at=datetime(2098, 1, 1, tzinfo=UTC),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        session_id="ses",
        device_id="dev",
    )
    with pytest.raises(TypeError, match="only be created|missing 1 required"):
        VerifiedRuntimeProfile(profile, "deadbeef")  # type: ignore[call-arg]


def test_missing_signature_field_fails_closed() -> None:
    payload = canonical_wire_payload()
    del payload["signature"]
    assert verify_runtime_profile_signature(payload, TEST_VERIFY_KEY) is False
    assert parse_runtime_profile(payload, verify_key=TEST_VERIFY_KEY) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"runtime_profile_id": ""},
        {"signature_schema": "runtime-profile-v2"},
        {"session_epoch": 0},
        {"binding_version": 0},
        {"persona": {"persona_id": "starlight", "version": 0, "relationship_stage": "familiar"}},
        {"age_band": "made_up"},
        {"subject_category": "senior"},  # senior is never a signed category
        {"actor_id": ""},
        {"binding_id": ""},
        {"persona_assignment_id": ""},
        {"persona": {"persona_id": "", "version": 4, "relationship_stage": "familiar"}},
        {"persona": {"persona_id": "starlight", "version": 4, "relationship_stage": ""}},
        {"policy_bundle_version": ""},
        {"session_id": ""},
        {"device_id": ""},
        {"service_mode": "tutor_english"},  # tutor is not a canonical mode
        {"service_mode": "made_up"},
        {"speaker_state": "maybe"},
        {"session_epoch": -1},
        {"session_epoch": "2"},
        {"expires_at": "not-a-timestamp"},
        {"expires_at": "2026-01-01T00:00:00"},  # no timezone
    ],
)
def test_malformed_profile_fails_closed(overrides: dict[str, object]) -> None:
    assert (
        parse_runtime_profile(canonical_wire_payload(**overrides), verify_key=TEST_VERIFY_KEY)
        is None
    )


def test_expired_profile_fails_closed() -> None:
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert (
        parse_runtime_profile(canonical_wire_payload(expires_at=past), verify_key=TEST_VERIFY_KEY)
        is None
    )
    assert is_expired_profile_payload(canonical_wire_payload(expires_at=past)) is True
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert (
        parse_runtime_profile(
            canonical_wire_payload(
                expires_at=future,
                issued_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            ),
            verify_key=TEST_VERIFY_KEY,
        )
        is not None
    )
    assert (
        is_expired_profile_payload(
            canonical_wire_payload(
                expires_at=future,
                issued_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            )
        )
        is False
    )


def test_non_mapping_and_unknown_keys_fail_closed() -> None:
    assert parse_runtime_profile(None) is None
    assert parse_runtime_profile("rp") is None
    assert parse_runtime_profile([]) is None
    # Unsigned extra keys change the canonical bytes: verification fails.
    payload = canonical_wire_payload()
    payload["can_view_memory"] = True
    payload["secret_note"] = "parent-private-history"
    assert parse_runtime_profile(payload, verify_key=TEST_VERIFY_KEY) is None
    # Even re-signed, extra fields are rejected by the generated contract.
    from services.agent.tests.unit.runtime_profile_test_helpers import sign_wire_payload

    signed = canonical_wire_payload()
    signed["secret_note"] = "parent-private-history"
    signed["signature"] = sign_wire_payload(signed, TEST_VERIFY_KEY)
    assert parse_runtime_profile(signed, verify_key=TEST_VERIFY_KEY) is None


def test_subject_context_never_falls_back_to_owner_or_speaker() -> None:
    assert subject_context_from_profile(None) == {
        "subject_category": "unknown",
        "is_confirmed": False,
    }
    verified = parse_runtime_profile(canonical_wire_payload(), verify_key=TEST_VERIFY_KEY)
    assert verified is not None
    context = subject_context_from_profile(verified.profile)
    assert context["subject_category"] == "minor"
    assert context["is_confirmed"] is True
    # A named subject with an unconfirmed speaker is a signed inconsistency.
    assert (
        parse_runtime_profile(
            canonical_wire_payload(
                speaker_state="unconfirmed",
                service_mode="family_shared",
                subject_category="adult",
                age_band="adult",
                active_subject_id="person_adult",
            ),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )


def test_all_canonical_service_modes_are_accepted() -> None:
    cases = {
        "student_minor": dict(subject_category="minor", age_band="under_14"),
        "adult_companion": dict(subject_category="adult", age_band="adult"),
        "senior_companion": dict(subject_category="adult", age_band="adult"),
        "family_shared": dict(subject_category="adult", age_band="adult"),
        "adult_archive": dict(subject_category="adult", age_band="adult"),
        "self_preview": dict(subject_category="adult", age_band="adult"),
        "legacy_access": dict(subject_category="adult", age_band="adult"),
        "unknown_safe": dict(
            subject_category="unknown",
            age_band="unknown",
            active_subject_id=None,
            speaker_state="unconfirmed",
            capabilities=["chat"],
            obligations=UNKNOWN_SAFE_OBLIGATIONS,
        ),
    }
    for mode, overrides in cases.items():
        verified = parse_runtime_profile(
            canonical_wire_payload(service_mode=mode, **overrides),
            verify_key=TEST_VERIFY_KEY,
        )
        assert verified is not None and verified.profile.service_mode == mode


def test_null_subject_allowed_only_for_unknown_safe_unconfirmed() -> None:
    """P0-3: a signed unknown_safe profile may carry a null subject and still
    freeze its epoch/obligations; a confirmed speaker must name the subject."""

    unknown_safe = canonical_wire_payload(
        active_subject_id=None,
        subject_category="unknown",
        age_band="unknown",
        speaker_state="unconfirmed",
        service_mode="unknown_safe",
        capabilities=["chat"],
        obligations=UNKNOWN_SAFE_OBLIGATIONS,
    )
    verified = parse_runtime_profile(unknown_safe, verify_key=TEST_VERIFY_KEY)
    assert verified is not None
    profile = verified.profile
    assert profile.active_subject_id is None
    assert profile.service_mode == "unknown_safe"
    assert profile.session_epoch == 2
    # Unknown-safe keeps the canonical safe obligations verbatim (audit 2).
    assert set(profile.obligations) == set(UNKNOWN_SAFE_OBLIGATIONS)
    assert profile.capabilities == ("chat",)

    confirmed_no_subject = canonical_wire_payload(active_subject_id=None)
    assert (
        parse_runtime_profile(confirmed_no_subject, verify_key=TEST_VERIFY_KEY) is None
    )
    non_safe_no_subject = canonical_wire_payload(
        active_subject_id=None, speaker_state="unconfirmed"
    )
    assert parse_runtime_profile(non_safe_no_subject, verify_key=TEST_VERIFY_KEY) is None


def test_unknown_safe_english_practice_is_allowed() -> None:
    """Remediation 4.3: unknown-safe keeps chat + temporary English practice;
    tutor and learning-progress persistence are outside the safe surface."""

    verified = parse_runtime_profile(
        canonical_wire_payload(
            active_subject_id=None,
            subject_category="unknown",
            age_band="unknown",
            speaker_state="unconfirmed",
            service_mode="unknown_safe",
            session_epoch=1,
            capabilities=["chat", "english_practice"],
            obligations=UNKNOWN_SAFE_OBLIGATIONS,
        ),
        verify_key=TEST_VERIFY_KEY,
    )
    assert verified is not None
    assert set(verified.profile.capabilities) == {"chat", "english_practice"}
    # English practice stays temporary: unknown-safe never persists evidence
    # or learning progress (canonical obligations + no memory capability).
    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.runtime_profile_gate import RuntimeProfileGate

    gate = RuntimeProfileGate(
        metrics=MetricsRegistry(),
        bump_epoch=lambda epoch: GenerationFence(
            session_id="ses_01J_test",
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            session_epoch=epoch,
        ),
        expected_session_id="ses_01J_test",
        expected_device_id="dev_01J_test",
        verify_key=TEST_VERIFY_KEY,
    )
    fence = GenerationFence(
        session_id="ses_01J_test",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    assert gate.apply(verified, fence) is not None
    decision = gate.persistence_decision(fence, current_fence=fence)
    assert decision.allowed is False
    assert decision.no_model_training is True


def test_unknown_safe_chat_tutor_request_degrades_to_chat_only() -> None:
    """A signed unknown-safe payload asking for tutor is inconsistent with the
    safe surface: the whole profile fails closed and only ordinary chat (no
    tutor capability, no learning-progress persistence) remains possible."""

    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.runtime_profile_gate import RuntimeProfileGate

    payload = canonical_wire_payload(
        active_subject_id=None,
        subject_category="unknown",
        age_band="unknown",
        speaker_state="unconfirmed",
        service_mode="unknown_safe",
        session_epoch=1,
        capabilities=["chat", "tutor"],
        obligations=UNKNOWN_SAFE_OBLIGATIONS,
    )
    assert parse_runtime_profile(payload, verify_key=TEST_VERIFY_KEY) is None
    gate = RuntimeProfileGate(
        metrics=MetricsRegistry(),
        bump_epoch=lambda epoch: GenerationFence(
            session_id="ses_01J_test",
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            session_epoch=epoch,
        ),
        expected_session_id="ses_01J_test",
        expected_device_id="dev_01J_test",
        verify_key=TEST_VERIFY_KEY,
    )
    fence = GenerationFence(
        session_id="ses_01J_test",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=1,
    )
    assert gate.apply(payload, fence) is None
    assert gate.current is None


def test_missing_subject_category_degrades_to_unknown_safe() -> None:
    """P1-2/P0-2: a missing required category fails closed to unknown_safe."""

    payload = canonical_wire_payload()
    del payload["subject_category"]
    from services.agent.tests.unit.runtime_profile_test_helpers import sign_wire_payload

    payload["signature"] = sign_wire_payload(payload, TEST_VERIFY_KEY)
    assert parse_runtime_profile(payload, verify_key=TEST_VERIFY_KEY) is None


def test_zero_subject_revision_unknown_safe_profile_is_accepted() -> None:
    """P0-1 golden: Control issues unknown-safe profiles with revision 0."""

    verified = parse_runtime_profile(
        canonical_wire_payload(
            subject_revision=0,
            active_subject_id=None,
            subject_category="unknown",
            age_band="unknown",
            speaker_state="unconfirmed",
        service_mode="unknown_safe",
        session_epoch=1,
        capabilities=["chat"],
        obligations=UNKNOWN_SAFE_OBLIGATIONS,
        ),
        verify_key=TEST_VERIFY_KEY,
    )
    assert verified is not None
    assert verified.profile.subject_revision == 0
    assert verified.profile.service_mode == "unknown_safe"
    assert verified.profile.session_epoch == 1


def _runtime_profile_gate_for_test(**overrides: object) -> object:
    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.runtime_profile_gate import RuntimeProfileGate

    defaults = {
        "expected_session_id": "ses_01J_test",
        "expected_device_id": "dev_01J_test",
        "expected_actor_id": "actor_01J_test",
        "expected_binding_id": "bind_01J_test",
        "expected_binding_version": 1,
        "expected_active_subject_id": "person_child",
        "expected_subject_fence_enabled": True,
        "expected_subject_revision": 1,
        "verify_key": TEST_VERIFY_KEY,
    }
    defaults.update(overrides)
    return RuntimeProfileGate(
        metrics=MetricsRegistry(),
        bump_epoch=lambda epoch: GenerationFence(
            session_id="ses_01J_test",
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            session_epoch=epoch,
        ),
        **defaults,
    )


def _runtime_profile_fence(epoch: int = 2) -> object:
    from services.agent.src.contracts.ids import GenerationFence

    return GenerationFence(
        session_id="ses_01J_test",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        session_epoch=epoch,
    )


@pytest.mark.parametrize(
    "expected",
    [
        {"expected_actor_id": "other-actor"},
        {"expected_binding_id": "other-binding"},
        {"expected_binding_version": 2},
        {"expected_active_subject_id": "other-subject"},
        {"expected_subject_revision": 2},
    ],
)
def test_runtime_profile_gate_requires_exact_subject_binding_fence(
    expected: dict[str, object],
) -> None:
    verified = parse_runtime_profile(canonical_wire_payload(), verify_key=TEST_VERIFY_KEY)
    assert verified is not None
    gate = _runtime_profile_gate_for_test(**expected)

    assert gate.apply(verified, _runtime_profile_fence()) is None
    assert gate.current is None


@pytest.mark.parametrize(
    ("expected_subject", "profile_subject", "subject_revision", "accepted"),
    [
        ("person_child", "person_child", 1, True),
        ("person_child", None, 0, False),
        ("", "person_child", 1, False),
        ("", None, 0, True),
    ],
)
def test_runtime_profile_gate_subject_fence_four_boundaries(
    expected_subject: str | None,
    profile_subject: str | None,
    subject_revision: int,
    accepted: bool,
) -> None:
    """P0-2: an enabled device subject fence compares named and empty
    subjects strictly in all four combinations; the empty wire subject and a
    None profile subject (unknown_safe) are the same fence value."""

    if profile_subject is None:
        payload = canonical_wire_payload(
            active_subject_id=None,
            subject_category="unknown",
            age_band="unknown",
            speaker_state="unconfirmed",
            service_mode="unknown_safe",
            session_epoch=1,
            subject_revision=0,
            capabilities=["chat"],
            obligations=UNKNOWN_SAFE_OBLIGATIONS,
        )
        fence = _runtime_profile_fence(epoch=1)
    else:
        payload = canonical_wire_payload(active_subject_id=profile_subject)
        fence = _runtime_profile_fence(epoch=2)
    verified = parse_runtime_profile(payload, verify_key=TEST_VERIFY_KEY)
    assert verified is not None
    gate = _runtime_profile_gate_for_test(
        expected_active_subject_id=expected_subject,
        expected_subject_revision=subject_revision,
    )
    applied = gate.apply(verified, fence)
    assert (applied is not None) is accepted
    if not accepted:
        assert gate.current is None


def test_runtime_profile_gate_disabled_subject_fence_ignores_subject() -> None:
    """H5/non-device sessions keep the historical behavior: no subject
    fence means the signed profile subject is never compared."""

    verified = parse_runtime_profile(
        canonical_wire_payload(active_subject_id="person_parent"),
        verify_key=TEST_VERIFY_KEY,
    )
    assert verified is not None
    gate = _runtime_profile_gate_for_test(
        expected_active_subject_id="person_child",
        expected_subject_fence_enabled=False,
    )
    assert gate.apply(verified, _runtime_profile_fence()) is not None


def test_runtime_profile_gate_does_not_trim_subject_fence_values() -> None:
    verified = parse_runtime_profile(
        canonical_wire_payload(active_subject_id="person_child"),
        verify_key=TEST_VERIFY_KEY,
    )
    assert verified is not None
    gate = _runtime_profile_gate_for_test(
        expected_active_subject_id=" person_child",
    )

    assert gate.apply(verified, _runtime_profile_fence()) is None
    assert gate.current is None


def test_runtime_profile_gate_rejects_expired_and_late_epoch_profiles() -> None:
    from services.agent.src.runtime_profile_gate import RuntimeProfileGate

    now = datetime.now(UTC)
    expired = canonical_wire_payload(
        issued_at=(now - timedelta(minutes=2)).isoformat(),
        expires_at=(now - timedelta(minutes=1)).isoformat(),
    )
    gate = _runtime_profile_gate_for_test(clock=lambda: now)
    assert gate.apply(expired, _runtime_profile_fence(), now=now) is None
    assert gate.current is None

    current = parse_runtime_profile(canonical_wire_payload(), verify_key=TEST_VERIFY_KEY)
    assert current is not None
    gate = _runtime_profile_gate_for_test()
    assert gate.apply(current, _runtime_profile_fence()) is not None
    late = canonical_wire_payload(session_epoch=1)
    assert gate.apply(late, _runtime_profile_fence(), now=now) is None
    assert gate.current is current
    assert isinstance(gate, RuntimeProfileGate)


def test_runtime_profile_gate_rejects_same_epoch_identity_replay() -> None:
    gate = _runtime_profile_gate_for_test()
    fence = _runtime_profile_fence()
    current = parse_runtime_profile(canonical_wire_payload(), verify_key=TEST_VERIFY_KEY)
    assert current is not None
    assert gate.apply(current, fence) is not None

    replay = canonical_wire_payload(
        active_subject_id="person_parent",
        subject_category="adult",
        age_band="adult",
        service_mode="adult_companion",
    )
    assert gate.apply(replay, fence) is None
    assert gate.current is None


def test_runtime_profile_gate_reports_capability_denial_without_relaxing_gate() -> None:
    gate = _runtime_profile_gate_for_test()
    fence = _runtime_profile_fence()
    current = parse_runtime_profile(canonical_wire_payload(), verify_key=TEST_VERIFY_KEY)
    assert current is not None
    assert gate.apply(current, fence) is not None

    assert (
        gate.permits_reason(
            fence,
            current_fence=fence,
            capability="memory_recall_private",
        )
        == "capability_not_in_runtime_profile"
    )
    assert not gate.permits(
        fence,
        current_fence=fence,
        capability="memory_recall_private",
    )
    assert (
        gate.metrics.get(
            "runtime_profile_capability_denied_total",
            {"reason": "capability_not_in_runtime_profile"},
        )
        == 1
    )


def test_runtime_profile_gate_distinguishes_missing_profile_and_stale_fence() -> None:
    gate = _runtime_profile_gate_for_test()
    current_fence = _runtime_profile_fence()

    assert (
        gate.permits_reason(
            current_fence,
            current_fence=current_fence,
            capability="memory_recall_private",
        )
        == "profile_missing"
    )
    assert (
        gate.permits_reason(
            _runtime_profile_fence(epoch=1),
            current_fence=current_fence,
            capability="memory_recall_private",
        )
        == "fence_epoch_mismatch"
    )


def test_runtime_profile_gate_reports_unconfirmed_speaker_reason() -> None:
    fence = _runtime_profile_fence(epoch=1)
    unknown_safe = parse_runtime_profile(
        canonical_wire_payload(
            active_subject_id=None,
            subject_category="unknown",
            age_band="unknown",
            speaker_state="unconfirmed",
            service_mode="unknown_safe",
            session_epoch=1,
            subject_revision=0,
            capabilities=["chat"],
            obligations=UNKNOWN_SAFE_OBLIGATIONS,
        ),
        verify_key=TEST_VERIFY_KEY,
    )
    assert unknown_safe is not None
    gate = _runtime_profile_gate_for_test(
        expected_active_subject_id=None, expected_subject_revision=0
    )
    assert gate.apply(unknown_safe, fence) is not None
    assert (
        gate.permits_reason(fence, current_fence=fence, capability="chat")
        == "speaker_unconfirmed"
    )


def test_duplicate_or_zero_revision_contract_violations_fail_closed() -> None:
    """P0-2: duplicate lists and 0 binding/persona/epoch revisions reject."""

    assert (
        parse_runtime_profile(
            canonical_wire_payload(capabilities=["chat", "chat"]),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )
    assert (
        parse_runtime_profile(
            canonical_wire_payload(obligations=["DO_NOT_PERSIST", "DO_NOT_PERSIST"]),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )
    assert (
        parse_runtime_profile(
            canonical_wire_payload(policy_receipt_ids=["r1", "r1"]),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )
    assert (
        parse_runtime_profile(
            canonical_wire_payload(binding_version=0),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )
    assert (
        parse_runtime_profile(
            canonical_wire_payload(
                persona={"persona_id": "starlight", "version": 0, "relationship_stage": "familiar"}
            ),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )


def test_space_separated_datetime_is_rejected() -> None:
    """P0-2: the generated contract requires strict RFC3339 datetimes."""

    assert (
        parse_runtime_profile(
            canonical_wire_payload(issued_at="2098-01-01 00:00:00+00:00"),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )


def test_minor_never_carries_adult_capabilities_or_modes() -> None:
    """P0-2: minors cannot hold adult-only capabilities or service modes."""

    assert (
        parse_runtime_profile(
            canonical_wire_payload(
                service_mode="adult_companion",
                subject_category="adult",
                age_band="adult",
                active_subject_id="person_adult",
            ),
            verify_key=TEST_VERIFY_KEY,
        )
        is not None
    )
    assert (
        parse_runtime_profile(
            canonical_wire_payload(
                capabilities=["chat", "tutor", "voice_clone_use"]
            ),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )


def test_minor_can_hold_subject_routing_and_consented_memory_capabilities() -> None:
    """Audit 4: voice profile (subject routing), consented private memory and
    guardian summaries are not structurally adult-only; the call-site gates
    decide execution.  Voice cloning remains structurally forbidden."""

    allowed = parse_runtime_profile(
        canonical_wire_payload(
            capabilities=[
                "chat",
                "tutor",
                "voice_profile_create",
                "memory_recall_private",
                "guardian_summary_view",
            ]
        ),
        verify_key=TEST_VERIFY_KEY,
    )
    assert allowed is not None
    assert (
        parse_runtime_profile(
            canonical_wire_payload(capabilities=["chat", "voice_clone_use"]),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )
    assert (
        parse_runtime_profile(
            canonical_wire_payload(capabilities=["chat", "digital_self_preview"]),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )
    assert (
        parse_runtime_profile(
            canonical_wire_payload(capabilities=["chat", "legacy_grant_create"]),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )
    assert (
        parse_runtime_profile(
            canonical_wire_payload(
                service_mode="adult_companion",
                subject_category="minor",
                age_band="under_14",
                active_subject_id="person_child",
            ),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )


def test_unknown_safe_without_do_not_persist_is_rejected() -> None:
    """P0-2: unknown_safe must state DO_NOT_PERSIST."""

    assert (
        parse_runtime_profile(
            canonical_wire_payload(
                active_subject_id=None,
                subject_category="unknown",
                age_band="unknown",
                speaker_state="unconfirmed",
                service_mode="unknown_safe",
                session_epoch=1,
                capabilities=["chat"],
                obligations=[],
            ),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )


def test_unknown_category_never_guesses_mode() -> None:
    """Audit 2: a signed non-safe mode with unknown category is rejected."""

    payload = canonical_wire_payload(
        subject_category="unknown",
        service_mode="student_minor",
        age_band="unknown",
        active_subject_id=None,
        speaker_state="unconfirmed",
    )
    assert parse_runtime_profile(payload, verify_key=TEST_VERIFY_KEY) is None


def test_non_canonical_capability_or_obligation_fails_closed() -> None:
    """P1-1: unknown capability/obligation values reject the whole profile."""

    assert (
        parse_runtime_profile(
            canonical_wire_payload(capabilities=["chat", "read_private_memory"]),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )
    assert (
        parse_runtime_profile(
            canonical_wire_payload(obligations=["SNEAKY_OBLIGATION"]),
            verify_key=TEST_VERIFY_KEY,
        )
        is None
    )


def test_verify_key_env_constant_is_exported() -> None:
    assert VERIFY_KEY_ENV == "MEMORIA_RUNTIME_PROFILE_VERIFY_KEY"


def test_runtime_profile_requires_aware_expiry() -> None:

    with pytest.raises(ValueError):
        RuntimeProfile(
            runtime_profile_id="rp",
            actor_id="a",
            binding_id="b",
            binding_version=1,
            subject_revision=1,
            active_subject_id="person",
            subject_category="adult",
            age_band="adult",
            speaker_state="confirmed",
            speaker_confidence=None,
            service_mode="adult_companion",
            persona_assignment_id="p:v1",
            persona_id="starlight",
            persona_version=4,
            relationship_stage="familiar",
            policy_bundle_version="v5",
            policy_receipt_ids=(),
            session_epoch=1,
            issued_at=datetime(2098, 1, 1, tzinfo=UTC),
            expires_at=datetime(2099, 1, 1),  # naive
            session_id="ses",
            device_id="dev",
        )
