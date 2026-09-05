from __future__ import annotations

import json

import httpx
import pytest
from services.agent.src.mode_policy_client import (
    ModePolicyClient,
    ModePolicyClientConfig,
)


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "interaction_mode": "companion",
        "session_focus": "chat",
        "mode_policy_version": "mode-policy-3",
        "companion_style_id": "starlight",
        "companion_style_version": "companion-v1",
        "policy_scope": "session",
        "actor_account_id": None,
        "resource_owner_account_id": None,
        "digital_self_version_id": None,
        "manifest_sha256": None,
        "preview_grant_id": None,
        "perspective": None,
        "relationship_profile_id": None,
        "relationship_profile_version": None,
        "legacy_actor_role": None,
        "legacy_grantee_account_id": None,
        "legacy_grant_id": None,
        "legacy_shell_id": None,
        "legacy_grant_snapshot_sha256": None,
        "legacy_scope_sha256": None,
        "legacy_voice_allowed": None,
        "legacy_expires_at": None,
        "voice_profile_id": None,
        "voice_profile_version": None,
        "voice_provider": None,
        "voice_model": None,
        "voice_resource_id": None,
        "voice_provider_expires_at": None,
        "voice_speaker_sha256": None,
        "fallback_voice_profile_id": None,
        "fallback_voice_provider": None,
        "fallback_voice_model": None,
        "fallback_voice_resource_id": None,
        "capabilities": {
            "conversation": True,
            "private_memory": True,
            "persona": True,
            "persona_low_sensitivity": True,
            "tools": True,
            "history": True,
            "learning": True,
            "voice_profile": True,
        },
    }
    payload.update(overrides)
    return payload


def _profile_for(capabilities: tuple[str, ...]) -> object:
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        TEST_VERIFY_KEY,
        canonical_wire_payload,
        parse_runtime_profile,
    )

    verified = parse_runtime_profile(
        canonical_wire_payload(
            session_id="ses_mode_matrix",
            session_epoch=2,
            active_subject_id="person_parent",
            subject_category="adult",
            age_band="adult",
            service_mode="adult_companion",
            capabilities=list(capabilities),
        ),
        verify_key=TEST_VERIFY_KEY,
    )
    assert verified is not None
    return verified


@pytest.mark.parametrize(
    "capabilities",
    [
        ("chat",),
        ("memory_recall_private",),
        ("guardian_summary_view",),
        ("voice_profile_create",),
        ("voice_clone_use",),
        ("payment",),
        ("device_ownership_transfer",),
        ("crisis_notification",),
        ("digital_self_preview",),
        ("legacy_grant_create",),
        ("raw_audio_retention",),
        ("model_training_contribution",),
    ],
)
def test_derived_policy_never_cross_grants_unrelated_surfaces(
    capabilities: tuple[str, ...],
) -> None:
    """No canonical capability may grant an unrelated ModePolicy surface:
    memory/voice/payment/transfer/crisis receipts never open tools, history,
    learning or conversation beyond their exact mapping."""

    from services.agent.src.mode_policy_client import ModePolicy

    profile = _profile_for(capabilities)
    policy = ModePolicy.from_runtime_profile(profile)
    assert policy.allows_tools("owner") is False
    assert policy.allows_voice_profile() is False
    assert policy.allows_private_persona("owner") is False
    assert policy.allows_low_sensitivity_persona(is_shadow=True) is False
    assert policy.allows_conversation() is ("chat" in capabilities)
    assert policy.history_eligible("owner") is ("memory_recall_private" in capabilities)
    assert policy.owner_projection_eligible("owner") is (
        "memory_recall_private" in capabilities
    )
    assert policy.allows_learning("owner") is False


def test_derived_policy_learning_requires_tutor_surface_and_no_persistence() -> None:
    """Learning derives from tutor/english_practice plus the signed
    no-learning-progress obligation, never from memory_capture."""

    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        TEST_VERIFY_KEY,
        canonical_wire_payload,
        parse_runtime_profile,
    )

    for capabilities, obligations, expected in (
        (("chat", "tutor"), (), True),
        (("chat", "tutor"), ("DO_NOT_WRITE_LEARNING_PROGRESS",), False),
        (("chat", "english_practice"), (), True),
        (("chat", "memory_capture"), (), False),
    ):
        verified = parse_runtime_profile(
            canonical_wire_payload(
                session_id="ses_mode_matrix",
                session_epoch=2,
                active_subject_id="person_parent",
                subject_category="adult",
                age_band="adult",
                service_mode="adult_companion",
                capabilities=list(capabilities),
                obligations=list(obligations),
            ),
            verify_key=TEST_VERIFY_KEY,
        )
        assert verified is not None
        policy = ModePolicy.from_runtime_profile(verified)
        assert policy.allows_learning("owner") is expected


def test_identity_rotation_keeps_previous_companion_style() -> None:
    from services.agent.src.mode_policy_client import (
        ModePolicy,
        mode_policy_after_identity_rotation,
    )
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        TEST_VERIFY_KEY,
        canonical_wire_payload,
        parse_runtime_profile,
    )

    previous = ModePolicy.companion_for_test(
        policy_version="s1",
        private_context=True,
        owner_evidence=True,
        tools=False,
        voice_profile=False,
        shadow_low_sensitivity_persona=False,
    )
    verified = parse_runtime_profile(
        canonical_wire_payload(
            session_id="ses_rotate_style",
            session_epoch=3,
            active_subject_id="person_parent",
            subject_category="adult",
            age_band="adult",
            service_mode="adult_companion",
            capabilities=["chat", "memory_recall_private"],
            persona={
                "persona_id": "person_parent",
                "version": 4,
                "relationship_stage": "familiar",
            },
        ),
        verify_key=TEST_VERIFY_KEY,
    )
    assert verified is not None
    derived = ModePolicy.from_runtime_profile(verified)
    assert derived.mode == "companion"
    assert derived.companion_style_id is None

    rotated = mode_policy_after_identity_rotation(previous, verified)
    assert rotated.mode == "companion"
    assert rotated.companion_style_id == "starlight"
    assert rotated.companion_style is not None


@pytest.mark.asyncio
async def test_fetch_freezes_companion_policy_from_the_authoritative_session_response() -> None:
    seen: dict[str, object] = {}
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        TEST_VERIFY_KEY,
        canonical_wire_payload,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["token"] = request.headers.get("X-Memoria-Internal-Token")
        return httpx.Response(
            200,
            json={
                **_payload(),
                "runtime_profile": canonical_wire_payload(
                    session_id="session-001",
                    service_mode="adult_companion",
                    subject_category="adult",
                    age_band="adult",
                    active_subject_id="person-owner",
                    speaker_state="confirmed",
                    session_epoch=1,
                    capabilities=["chat", "tutor", "memory_recall_private"],
                ),
            },
        )

    client = ModePolicyClient(
        ModePolicyClientConfig(
            endpoint="https://control.test/v1/interaction/session-policy",
            internal_token="interaction-token",
            runtime_profile_verify_key=TEST_VERIFY_KEY,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    policy = await client.fetch(session_id="session-001")

    assert seen == {
        "path": "/v1/interaction/session-policy",
        "body": {"session_id": "session-001"},
        "token": "interaction-token",
    }
    assert policy.available is True
    assert policy.mode == "companion"
    assert policy.session_focus == "chat"
    assert policy.policy_version == "cn-minor-v5"
    assert policy.companion_style_prompt is not None
    assert "价值观" in policy.companion_style_prompt


def test_owner_salutation_is_only_available_from_a_valid_companion_policy() -> None:
    companion = ModePolicyClient._parse(_payload(owner_display_name="主人"))

    assert companion.available is True
    assert companion.owner_salutation == "主人"
    assert not ModePolicyClient._parse(_payload(owner_display_name=True)).available


def test_tutor_focus_is_frozen_and_invalid_or_cross_mode_focus_fails_closed() -> None:
    tutor = ModePolicyClient._parse(_payload(session_focus="tutor_english"))

    assert tutor.available is True
    assert tutor.session_focus == "tutor_english"
    assert not ModePolicyClient._parse(_payload(session_focus="unknown")).available
    assert not ModePolicyClient._parse(
        _payload(interaction_mode="archive", session_focus="tutor_homework")
    ).available


@pytest.mark.parametrize(
    ("companion_id", "question_frequency", "interview_depth"),
    [
        ("starlight", "occasional", "light"),
        ("taoxi", "occasional", "light"),
        ("mianmian", "rare", "light"),
        ("axu", "rare", "light"),
        ("xuanmo", "rare", "on_explicit_invitation"),
        ("zhiyao", "frequent", "structured"),
        ("yanxi", "frequent", "structured"),
    ],
)
def test_companion_style_catalog_parity_reaches_the_agent_prompt(
    companion_id: str, question_frequency: str, interview_depth: str
) -> None:
    policy = ModePolicyClient._parse(_payload(companion_style_id=companion_id))

    assert policy.available is True
    assert policy.companion_style is not None
    assert policy.companion_style.question_frequency == question_frequency
    assert policy.companion_style.interview_depth == interview_depth


def test_self_preview_trusts_conversation_ceiling_but_denies_private_capabilities() -> None:
    policy = ModePolicyClient._parse(
        _payload(
            interaction_mode="self_preview",
            mode_policy_version="s8-v1",
            companion_style_id=None,
            companion_style_version=None,
            digital_self_version_id="version-001",
            manifest_sha256="a" * 64,
            preview_grant_id="grant-001",
            perspective="child",
            fallback_voice_profile_id="warm_companion",
            fallback_voice_provider="volcengine_doubao",
            fallback_voice_model="seed-tts-2.0",
            fallback_voice_resource_id="seed-tts-2.0",
            capabilities={
                "conversation": True,
                "private_memory": False,
                "persona": False,
                "persona_low_sensitivity": False,
                "tools": False,
                "history": False,
                "learning": False,
                "voice_profile": True,
            },
        )
    )

    assert policy.available is True
    assert policy.mode == "self_preview"
    assert policy.allows_conversation() is True
    assert policy.allows_private_context("owner") is False
    assert policy.allows_private_persona("owner") is False
    assert policy.allows_tools("owner") is False
    assert policy.history_eligible("owner") is False
    assert policy.owner_projection_eligible("owner") is False
    assert policy.companion_style_prompt is None


def test_self_preview_freezes_an_exact_personal_voice_contract() -> None:
    payload = _payload(
        interaction_mode="self_preview",
        mode_policy_version="s8-v1",
        companion_style_id=None,
        companion_style_version=None,
        digital_self_version_id="version-001",
        manifest_sha256="a" * 64,
        preview_grant_id="grant-001",
        perspective="owner",
        voice_profile_id="voice-profile-1",
        voice_profile_version=2,
        voice_provider="volcengine_doubao",
        voice_model="seed-icl-2.0",
        voice_resource_id="seed-icl-2.0",
        voice_provider_expires_at="2026-08-01T00:00:00+00:00",
        voice_speaker_sha256="b" * 64,
        fallback_voice_profile_id="warm_companion",
        fallback_voice_provider="volcengine_doubao",
        fallback_voice_model="seed-tts-2.0",
        fallback_voice_resource_id="seed-tts-2.0",
        capabilities={
            "conversation": True,
            "private_memory": False,
            "persona": False,
            "persona_low_sensitivity": False,
            "tools": False,
            "history": False,
            "learning": False,
            "voice_profile": True,
        },
    )

    policy = ModePolicyClient._parse(payload)

    assert policy.available is True
    assert dict(policy.references) == {
        "actor_account_id": None,
        "digital_self_version_id": "version-001",
        "legacy_actor_role": None,
        "legacy_expires_at": None,
        "legacy_grant_id": None,
        "legacy_grant_snapshot_sha256": None,
        "legacy_grantee_account_id": None,
        "legacy_scope_sha256": None,
        "legacy_shell_id": None,
        "legacy_voice_allowed": None,
        "manifest_sha256": "a" * 64,
        "owner_display_name": None,
        "perspective": "owner",
        "preview_grant_id": "grant-001",
        "relationship_profile_id": None,
        "relationship_profile_version": None,
        "resource_owner_account_id": None,
        "voice_model": "seed-icl-2.0",
        "voice_profile_id": "voice-profile-1",
        "voice_profile_version": "2",
        "voice_provider": "volcengine_doubao",
        "voice_provider_expires_at": "2026-08-01T00:00:00+00:00",
        "voice_resource_id": "seed-icl-2.0",
        "voice_speaker_sha256": "b" * 64,
        "fallback_voice_profile_id": "warm_companion",
        "fallback_voice_provider": "volcengine_doubao",
        "fallback_voice_model": "seed-tts-2.0",
        "fallback_voice_resource_id": "seed-tts-2.0",
    }
    assert not ModePolicyClient._parse({**payload, "voice_profile_version": None}).available
    assert not ModePolicyClient._parse({**payload, "voice_resource_id": "seed-tts-2.0"}).available
    assert not ModePolicyClient._parse(
        {**payload, "voice_provider_expires_at": "2026-08-01T08:00:00+08:00"}
    ).available


@pytest.mark.parametrize("invalid_version", [True, False, "2", 0, -1])
def test_personal_voice_version_rejects_non_positive_or_non_integer_values(
    invalid_version: object,
) -> None:
    policy = ModePolicyClient._parse(
        _payload(
            interaction_mode="self_preview",
            mode_policy_version="s8-v1",
            companion_style_id=None,
            companion_style_version=None,
            digital_self_version_id="version-001",
            manifest_sha256="a" * 64,
            preview_grant_id="grant-001",
            perspective="owner",
            voice_profile_id="voice-profile-1",
            voice_profile_version=invalid_version,
            voice_provider="volcengine_doubao",
            voice_model="seed-icl-2.0",
            voice_resource_id="seed-icl-2.0",
            voice_provider_expires_at="2026-08-01T00:00:00+00:00",
            voice_speaker_sha256="b" * 64,
            fallback_voice_profile_id="warm_companion",
            fallback_voice_provider="volcengine_doubao",
            fallback_voice_model="seed-tts-2.0",
            fallback_voice_resource_id="seed-tts-2.0",
            capabilities={
                "conversation": True,
                "private_memory": False,
                "persona": False,
                "persona_low_sensitivity": False,
                "tools": False,
                "history": False,
                "learning": False,
                "voice_profile": True,
            },
        )
    )

    assert policy.available is False
    assert policy.unavailable_reason == "payload_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("voice_speaker_sha256", None),
        ("voice_speaker_sha256", "A" * 64),
        ("voice_speaker_sha256", "b" * 63),
        ("fallback_voice_profile_id", None),
        ("fallback_voice_provider", "other"),
        ("fallback_voice_model", "seed-icl-2.0"),
        ("fallback_voice_resource_id", "seed-icl-2.0"),
    ],
)
def test_self_preview_voice_contract_rejects_partial_or_forged_fields(
    field: str,
    value: object,
) -> None:
    payload = _payload(
        interaction_mode="self_preview",
        mode_policy_version="s8-v1",
        companion_style_id=None,
        companion_style_version=None,
        digital_self_version_id="version-001",
        manifest_sha256="a" * 64,
        preview_grant_id="grant-001",
        perspective="owner",
        voice_profile_id="voice-profile-1",
        voice_profile_version=2,
        voice_provider="volcengine_doubao",
        voice_model="seed-icl-2.0",
        voice_resource_id="seed-icl-2.0",
        voice_provider_expires_at="2026-08-01T00:00:00+00:00",
        voice_speaker_sha256="b" * 64,
        fallback_voice_profile_id="warm_companion",
        fallback_voice_provider="volcengine_doubao",
        fallback_voice_model="seed-tts-2.0",
        fallback_voice_resource_id="seed-tts-2.0",
        capabilities={
            "conversation": True,
            "private_memory": False,
            "persona": False,
            "persona_low_sensitivity": False,
            "tools": False,
            "history": False,
            "learning": False,
            "voice_profile": True,
        },
    )
    payload[field] = value

    assert not ModePolicyClient._parse(payload).available


def test_self_preview_accepts_all_null_personal_voice_with_complete_fallback() -> None:
    policy = ModePolicyClient._parse(
        _payload(
            interaction_mode="self_preview",
            mode_policy_version="s8-v1",
            companion_style_id=None,
            companion_style_version=None,
            digital_self_version_id="version-001",
            manifest_sha256="a" * 64,
            preview_grant_id="grant-001",
            perspective="owner",
            fallback_voice_profile_id="bright_peer",
            fallback_voice_provider="volcengine_doubao",
            fallback_voice_model="seed-tts-2.0",
            fallback_voice_resource_id="seed-tts-2.0",
            capabilities={
                "conversation": True,
                "private_memory": False,
                "persona": False,
                "persona_low_sensitivity": False,
                "tools": False,
                "history": False,
                "learning": False,
                "voice_profile": True,
            },
        )
    )

    assert policy.available
    assert dict(policy.references)["fallback_voice_profile_id"] == "bright_peer"


def _legacy_payload(*, voice_allowed: bool = False) -> dict[str, object]:
    personal = voice_allowed
    return _payload(
        interaction_mode="legacy",
        mode_policy_version="s9-v1",
        companion_style_id=None,
        companion_style_version=None,
        actor_account_id="grantee-a",
        resource_owner_account_id="owner-a",
        digital_self_version_id="version-1",
        manifest_sha256="a" * 64,
        relationship_profile_id="relationship-1",
        relationship_profile_version=3,
        legacy_actor_role="grantee",
        legacy_grantee_account_id="grantee-a",
        legacy_grant_id="grant-1",
        legacy_shell_id="shell-1",
        legacy_grant_snapshot_sha256="b" * 64,
        legacy_scope_sha256="c" * 64,
        legacy_voice_allowed=voice_allowed,
        legacy_expires_at="2026-08-23T00:00:00+00:00",
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
        capabilities={
            "conversation": True,
            "private_memory": False,
            "persona": False,
            "persona_low_sensitivity": False,
            "tools": False,
            "history": False,
            "learning": False,
            "voice_profile": voice_allowed,
        },
    )


def test_legacy_policy_binds_grantee_actor_without_resource_owner_privileges() -> None:
    policy = ModePolicyClient._parse(_legacy_payload())

    assert policy.available
    assert policy.allows_conversation("owner")
    assert not policy.allows_conversation("guest")
    assert not policy.allows_private_context("owner")
    assert not policy.allows_private_persona("owner")
    assert not policy.allows_tools("owner")
    assert not policy.history_eligible("owner")
    assert not policy.allows_learning("owner")
    assert not policy.allows_voice_profile()
    assert dict(policy.references)["resource_owner_account_id"] == "owner-a"


def test_legacy_personal_voice_capability_requires_exact_allowed_snapshot() -> None:
    policy = ModePolicyClient._parse(_legacy_payload(voice_allowed=True))

    assert policy.available
    assert policy.allows_voice_profile()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("legacy_grant_id", ""),
        ("legacy_grant_snapshot_sha256", "f" * 63),
        ("legacy_scope_sha256", "F" * 64),
        ("actor_account_id", "stranger"),
        ("resource_owner_account_id", "grantee-a"),
        ("legacy_shell_id", None),
        ("relationship_profile_version", 0),
        ("legacy_expires_at", "2026-08-23T08:00:00+08:00"),
        ("fallback_voice_model", "seed-icl-2.0"),
    ],
)
def test_legacy_policy_rejects_forged_authority_and_voice(
    field: str,
    value: object,
) -> None:
    assert not ModePolicyClient._parse({**_legacy_payload(), field: value}).available


def test_policy_rejects_missing_frozen_fallback_field_even_in_companion_mode() -> None:
    payload = _payload()
    payload.pop("fallback_voice_profile_id")

    assert not ModePolicyClient._parse(payload).available


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,status",
    [
        (_payload(interaction_mode="unknown"), 200),
        (_payload(policy_scope="turn"), 200),
        (_payload(digital_self_version_id="client-selected"), 200),
        (_payload(capabilities={"private_memory": True}), 200),
        ({"interaction_mode": "companion"}, 200),
        (_payload(), 404),
    ],
)
async def test_bad_or_missing_policy_fails_closed(payload: dict[str, object], status: int) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    client = ModePolicyClient(
        ModePolicyClientConfig(
            endpoint="https://control.test/v1/interaction/session-policy",
            internal_token="interaction-token",
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    policy = await client.fetch(session_id="session-001")

    assert policy.available is False
    assert policy.allows_private_context("owner") is False
    assert policy.allows_tools("owner") is False
    assert policy.owner_projection_eligible("owner") is False
