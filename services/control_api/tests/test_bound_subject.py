"""A device-bound owner claim is honoured only on its own confirming profile."""

from __future__ import annotations

from typing import Any

import pytest
from services.control_api.app.bound_subject import (
    DEVICE_BOUND_SUBJECT_REASON,
    DEVICE_BOUND_SUBJECT_UNVERIFIED,
    claims_device_bound_subject,
    runtime_profile_trusts_bound_subject,
)
from services.control_api.app.routes.interaction import (
    ResponsePlanSpeakerDecision,
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


def test_confirmed_bound_subject_with_private_recall_is_trusted() -> None:
    assert runtime_profile_trusts_bound_subject(_profile()) is True
    assert runtime_profile_trusts_bound_subject(_profile(), subject_id="person-child") is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"active_subject_id": None},
        {"active_subject_id": "unknown"},
        {"active_subject_id": "  "},
        {"speaker_state": "unconfirmed"},
        {"service_mode": "unknown_safe"},
        {"capabilities": ["chat"]},
        {"capabilities": None},
    ],
)
def test_bound_subject_trust_fails_closed(overrides: dict[str, Any]) -> None:
    assert runtime_profile_trusts_bound_subject(_profile(**overrides)) is False


def test_bound_subject_trust_is_for_the_named_subject_only() -> None:
    assert runtime_profile_trusts_bound_subject(_profile(), subject_id="person-adult") is False
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


def test_trusted_profile_keeps_the_device_bound_owner() -> None:
    decision = _trusted_speaker_decision(_claim(), _scope(trusted=True))

    assert decision.classification == "owner"
    assert decision.reason_code == DEVICE_BOUND_SUBJECT_REASON


@pytest.mark.parametrize("scope", [None, _scope(trusted=False)])
def test_unconfirmed_profile_downgrades_the_claim(scope: _SubjectMemoryScope | None) -> None:
    decision = _trusted_speaker_decision(_claim(), scope)

    assert decision.classification == "uncertain"
    assert decision.reason_code == DEVICE_BOUND_SUBJECT_UNVERIFIED


def test_other_speaker_decisions_pass_through() -> None:
    voice_owner = _claim(reason="owner_match")

    assert _trusted_speaker_decision(voice_owner, None) is voice_owner
