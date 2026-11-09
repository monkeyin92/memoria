"""Device-bound owner claims never unlock private authority from profile alone (A06)."""

from __future__ import annotations

from typing import Any

import pytest
from services.control_api.app.bound_subject import (
    DEVICE_BOUND_SUBJECT_REASON,
    DEVICE_BOUND_SUBJECT_UNVERIFIED,
    claims_device_bound_subject,
    runtime_profile_trusts_bound_subject,
)
from services.control_api.app.response_plan_contract import ResponsePlanSpeakerDecision
from services.control_api.app.routes.interaction import (
    _SubjectMemoryScope,
    _trusted_speaker_decision,
)


def _profile(**overrides: Any) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "active_subject_id": "person-child",
        "speaker_state": "confirmed",
        "service_mode": "student_minor",
        "capabilities": ["chat", "memory_capture", "memory_recall_private"],
    }
    profile.update(overrides)
    return profile


def test_signed_profile_never_trusts_bind_as_speaker() -> None:
    assert runtime_profile_trusts_bound_subject(_profile()) is False
    assert runtime_profile_trusts_bound_subject(_profile(), subject_id="person-child") is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"active_subject_id": None},
        {"active_subject_id": "unknown"},
        {"speaker_state": "unconfirmed"},
        {"service_mode": "unknown_safe"},
        {"capabilities": ["chat"]},
        {},
    ],
)
def test_bound_subject_trust_stays_false_for_any_profile(overrides: dict[str, Any]) -> None:
    assert runtime_profile_trusts_bound_subject(_profile(**overrides)) is False


def test_bound_subject_trust_is_false_without_a_profile() -> None:
    assert runtime_profile_trusts_bound_subject(None) is False


def test_only_an_owner_with_the_binding_reason_claims_the_bound_subject() -> None:
    assert claims_device_bound_subject("owner", DEVICE_BOUND_SUBJECT_REASON) is True
    assert claims_device_bound_subject("owner", "owner_match") is False
    assert claims_device_bound_subject("uncertain", DEVICE_BOUND_SUBJECT_REASON) is False


def _scope(*, trusted: bool) -> _SubjectMemoryScope:
    return _SubjectMemoryScope(
        subject_authority="runtime_profile",
        subject_id="person-child",
        subject_category="minor",
        age_band="under_14",
        retention_allowed=True,
        catalog_readable=True,
        bound_subject_trusted=trusted,
    )


def _claim(
    classification: str = "owner", reason: str = DEVICE_BOUND_SUBJECT_REASON
) -> ResponsePlanSpeakerDecision:
    return ResponsePlanSpeakerDecision.model_validate(
        {"classification": classification, "reason_code": reason}
    )


@pytest.mark.parametrize("trusted", [True, False])
def test_device_bound_owner_claim_is_always_demoted(trusted: bool) -> None:
    decision = _trusted_speaker_decision(_claim(), _scope(trusted=trusted))

    assert decision.classification == "uncertain"
    assert decision.reason_code == DEVICE_BOUND_SUBJECT_UNVERIFIED


def test_missing_scope_demotes_the_device_bound_claim() -> None:
    decision = _trusted_speaker_decision(_claim(), None)

    assert decision.classification == "uncertain"
    assert decision.reason_code == DEVICE_BOUND_SUBJECT_UNVERIFIED


def test_other_speaker_decisions_pass_through() -> None:
    voice_owner = _claim(reason="owner_match")

    assert _trusted_speaker_decision(voice_owner, None) is voice_owner
