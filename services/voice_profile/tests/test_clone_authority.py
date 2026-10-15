from __future__ import annotations

from datetime import UTC, datetime

import pytest
from services.voice_profile.clone_authority import (
    CloneAdmission,
    VoiceCloneAuthority,
    VoiceCloneDisabledError,
    consent_standing,
)
from services.voice_profile.domain import VoiceConsent

_GRANTED = datetime(2026, 6, 1, tzinfo=UTC)


def _consent(*, notice_version: str, revoked_at: datetime | None = None) -> VoiceConsent:
    return VoiceConsent(
        account_id="acct",
        policy_version="voice-policy",
        notice_version=notice_version,
        granted_at=_GRANTED,
        revoked_at=revoked_at,
    )


@pytest.mark.hermetic
def test_consent_standing_follows_revocation_then_the_notice_allowlist() -> None:
    current = VoiceCloneAuthority(enabled=True).admit().notice_version
    assert consent_standing(None) == "absent"
    assert consent_standing(_consent(notice_version=current, revoked_at=_GRANTED)) == "revoked"
    assert consent_standing(_consent(notice_version="pre-separate-notice")) == "notice_required"
    assert consent_standing(_consent(notice_version="not-a-notice")) == "notice_required"
    assert consent_standing(_consent(notice_version=current)) == "valid"


@pytest.mark.hermetic
def test_disabled_authority_refuses_admission() -> None:
    with pytest.raises(VoiceCloneDisabledError) as caught:
        VoiceCloneAuthority(enabled=False).admit()
    assert caught.value.code == "voice_clone_disabled"


@pytest.mark.hermetic
def test_clone_admission_cannot_be_forged() -> None:
    with pytest.raises(TypeError):
        CloneAdmission(object(), "x")
