from dataclasses import replace

import pytest
from services.control_api.app.mode_policy import (
    LEGACY_POLICY_VERSION,
    FrozenMode,
    ModePolicy,
)


def _legacy(*, actor_role: str = "grantee", voice_allowed: bool = False) -> FrozenMode:
    owner_preview = actor_role == "owner_preview"
    personal = voice_allowed
    return ModePolicy.freeze_legacy(
        actor_account_id="owner-a" if owner_preview else "grantee-a",
        resource_owner_account_id="owner-a",
        actor_role=actor_role,  # type: ignore[arg-type]
        grantee_account_id="grantee-a",
        grant_id="grant-1",
        grant_snapshot_sha256="a" * 64,
        version_id="version-1",
        manifest_sha256="b" * 64,
        relationship_profile_id="relationship-1",
        relationship_profile_version=3,
        scope_sha256="c" * 64,
        shell_id=None if owner_preview else "shell-1",
        voice_allowed=voice_allowed,
        expires_at="2026-08-23T00:00:00+00:00",
        voice_profile_id="voice-1" if personal else None,
        voice_profile_version=2 if personal else None,
        voice_provider="volcengine_doubao" if personal else None,
        voice_model="seed-icl-2.0" if personal else None,
        voice_resource_id="seed-icl-2.0" if personal else None,
        voice_provider_expires_at=(
            "2026-08-22T00:00:00+00:00" if personal else None
        ),
        voice_speaker_sha256="d" * 64 if personal else None,
        fallback_voice_profile_id="warm_companion",
        fallback_voice_provider="volcengine_doubao",
        fallback_voice_model="seed-tts-2.0",
        fallback_voice_resource_id="seed-tts-2.0",
    )


def test_legacy_is_available_only_from_a_complete_frozen_contract() -> None:
    assert ModePolicy.availability("legacy").status == "blocked"

    frozen = _legacy()

    assert frozen.mode_policy_version == LEGACY_POLICY_VERSION
    assert ModePolicy.availability(frozen).status == "available"
    assert ModePolicy.session_context(frozen)["capabilities"] == {
        "conversation": True,
        "private_memory": False,
        "persona": False,
        "persona_low_sensitivity": False,
        "tools": False,
        "history": False,
        "learning": False,
        "voice_profile": False,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"legacy_grant_snapshot_sha256": "forged"},
        {"legacy_scope_sha256": "forged"},
        {"actor_account_id": "stranger"},
        {"legacy_shell_id": None},
        {"relationship_profile_version": None},
        {"legacy_expires_at": "2026-08-23T08:00:00+08:00"},
        {"fallback_voice_model": "seed-icl-2.0"},
    ],
)
def test_legacy_availability_rejects_forged_frozen_authority(
    change: dict[str, object],
) -> None:
    assert ModePolicy.availability(replace(_legacy(), **change)).status == "blocked"


def test_grantee_owner_speaker_is_the_actor_not_the_resource_owner() -> None:
    frozen = _legacy()

    owner_speaker = ModePolicy.effective_capabilities(frozen, speaker_class="owner")
    guest_speaker = ModePolicy.effective_capabilities(frozen, speaker_class="guest")

    assert frozen.actor_account_id == "grantee-a"
    assert frozen.resource_owner_account_id == "owner-a"
    assert owner_speaker.conversation is True
    assert guest_speaker.conversation is False
    assert owner_speaker.private_memory is False
    assert owner_speaker.persona is False
    assert owner_speaker.tools is False
    assert owner_speaker.history is False
    assert owner_speaker.learning is False


def test_legacy_voice_capability_requires_permission_and_exact_personal_snapshot() -> None:
    assert ModePolicy.effective_capabilities(
        _legacy(voice_allowed=False),
        speaker_class="owner",
    ).voice_profile is False
    assert ModePolicy.effective_capabilities(
        _legacy(voice_allowed=True),
        speaker_class="owner",
    ).voice_profile is True

    allowed_without_personal = replace(
        _legacy(voice_allowed=True),
        voice_profile_id=None,
        voice_profile_version=None,
        voice_provider=None,
        voice_model=None,
        voice_resource_id=None,
        voice_provider_expires_at=None,
        voice_speaker_sha256=None,
    )
    assert ModePolicy.availability(allowed_without_personal).status == "available"
    assert ModePolicy.effective_capabilities(
        allowed_without_personal,
        speaker_class="owner",
    ).voice_profile is False


def test_owner_preview_requires_owner_actor_and_has_no_shell() -> None:
    frozen = _legacy(actor_role="owner_preview", voice_allowed=True)

    assert frozen.legacy_shell_id is None
    assert ModePolicy.effective_capabilities(frozen, speaker_class="owner").conversation
    assert not ModePolicy.effective_capabilities(frozen, speaker_class="guest").conversation
