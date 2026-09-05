from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from services.agent.src.agent import _apply_cached_voice_profile
from services.agent.src.agent_voice_profile import align_tts_voice_to_policy
from services.agent.src.generation_output_policy import (
    frozen_companion_clone_permitted,
    generation_voice_reject_reason,
)
from services.agent.src.mode_policy_client import ModePolicy, ModePolicyClient
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig
from services.agent.src.voice_profile_client import (
    VoiceProfileClient,
    VoiceProfileClientConfig,
)


def _speaker_sha256(voice_id: str) -> str:
    return hashlib.sha256(voice_id.encode()).hexdigest()


def _self_preview_policy(*, fallback_profile_id: str = "bright_peer") -> ModePolicy:
    return ModePolicy(
        mode="self_preview",
        policy_version="s8-v1",
        companion_style_id=None,
        style_version=None,
        references=tuple(
            sorted(
                {
                    "voice_profile_id": "voice-profile-personal",
                    "voice_provider": "volcengine_doubao",
                    "voice_model": "seed-icl-2.0",
                    "voice_resource_id": "seed-icl-2.0",
                    "voice_speaker_sha256": _speaker_sha256("S_personal_synth_ready"),
                    "fallback_voice_profile_id": fallback_profile_id,
                    "fallback_voice_provider": "volcengine_doubao",
                    "fallback_voice_model": "seed-tts-2.0",
                    "fallback_voice_resource_id": "seed-tts-2.0",
                }.items()
            )
        ),
        capabilities=(),
        companion_style=None,
    )


def _legacy_policy(*, voice_allowed: bool) -> ModePolicy:
    references: dict[str, str | bool | None] = {
        "voice_profile_id": "voice-profile-personal" if voice_allowed else None,
        "voice_profile_version": "3" if voice_allowed else None,
        "voice_provider": "volcengine_doubao" if voice_allowed else None,
        "voice_model": "seed-icl-2.0" if voice_allowed else None,
        "voice_resource_id": "seed-icl-2.0" if voice_allowed else None,
        "voice_provider_expires_at": ("2027-07-23T00:00:00+00:00" if voice_allowed else None),
        "voice_speaker_sha256": (
            _speaker_sha256("S_personal_synth_ready") if voice_allowed else None
        ),
        "fallback_voice_profile_id": "bright_peer",
        "fallback_voice_provider": "volcengine_doubao",
        "fallback_voice_model": "seed-tts-2.0",
        "fallback_voice_resource_id": "seed-tts-2.0",
        "legacy_voice_allowed": voice_allowed,
    }
    return ModePolicy(
        mode="legacy",
        policy_version="s9-v1",
        companion_style_id=None,
        style_version=None,
        references=tuple(sorted(references.items())),
        capabilities=(),
        companion_style=None,
    )


@pytest.mark.asyncio
async def test_resolution_sends_only_session_id_and_caches_active_profile() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["token"] = request.headers.get("X-Memoria-Internal-Token")
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "mode": "active",
                "profile_id": "voice-profile-001",
                "provider": "volcengine_doubao",
                "voice_kind": "personal",
                "model": "seed-icl-2.0",
                "resource_id": "seed-icl-2.0",
                "voice_id": "S_voice_profile_001",
                "speaker_sha256": _speaker_sha256("S_voice_profile_001"),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
                timeout_s=0.2,
            ),
            client=http_client,
        )
        assert await client.refresh(session_id="session-001")
        profile = client.cached(session_id="session-001")

    assert observed == {
        "token": "voice-internal-token",
        "body": {"session_id": "session-001"},
    }
    assert profile is not None
    assert profile.profile_id == "voice-profile-001"
    assert profile.voice_id == "S_voice_profile_001"
    assert profile.voice_kind == "personal"
    assert profile.resource_id == "seed-icl-2.0"


@pytest.mark.asyncio
async def test_resolver_failure_evicts_cached_personal_voice_immediately() -> None:
    response = {
        "mode": "active",
        "profile_id": "voice-profile-001",
        "provider": "volcengine_doubao",
        "voice_kind": "personal",
        "model": "seed-icl-2.0",
        "resource_id": "seed-icl-2.0",
        "voice_id": "S_voice_profile_001",
        "speaker_sha256": _speaker_sha256("S_voice_profile_001"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if response["mode"] == "error":
            raise httpx.ReadTimeout("slow resolver", request=request)
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )
        assert await client.refresh(session_id="session-001")
        assert client.cached(session_id="session-001") is not None
        for failure_mode in ("error", "invalid"):
            response["mode"] = failure_mode
            assert not await client.refresh(session_id="session-001")
            assert client.cached(session_id="session-001") is None
            response.clear()
            response.update(
                {
                    "mode": "active",
                    "profile_id": "voice-profile-001",
                    "provider": "volcengine_doubao",
                    "voice_kind": "personal",
                    "model": "seed-icl-2.0",
                    "resource_id": "seed-icl-2.0",
                    "voice_id": "S_voice_profile_001",
                    "speaker_sha256": _speaker_sha256("S_voice_profile_001"),
                }
            )
            assert await client.refresh(session_id="session-001")
        response.clear()
        response.update(
            {
                "mode": "fallback",
                "profile_id": None,
                "provider": None,
                "voice_kind": None,
                "model": None,
                "resource_id": None,
                "voice_id": None,
                "speaker_sha256": None,
            }
        )
        assert await client.refresh(session_id="session-001")
        assert client.cached(session_id="session-001") is None


@pytest.mark.asyncio
async def test_http_5xx_evicts_cached_personal_voice_and_restores_baseline() -> None:
    failing = False

    def handler(_: httpx.Request) -> httpx.Response:
        if failing:
            return httpx.Response(503, json={"detail": "unavailable"})
        return httpx.Response(
            200,
            json={
                "mode": "active",
                "profile_id": "voice-profile-personal",
                "provider": "volcengine_doubao",
                "voice_kind": "personal",
                "model": "seed-icl-2.0",
                "resource_id": "seed-icl-2.0",
                "voice_id": "S_personal_synth_ready",
                "speaker_sha256": _speaker_sha256("S_personal_synth_ready"),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )
        tts = DoubaoTTS(
            DoubaoTTSConfig(
                api_key="test",
                voice_profile="warm_companion",
                speaker="zh_male_yangguangqingnian_uranus_bigtts",
            )
        )
        try:
            assert await client.refresh(session_id="session-personal")
            _apply_cached_voice_profile(
                tts_plugin=tts,
                client=client,
                session_id="session-personal",
                mode="self_preview",
                policy=_self_preview_policy(),
            )
            assert tts.current_voice_kind == "personal"

            failing = True
            assert not await client.refresh(session_id="session-personal")
            _apply_cached_voice_profile(
                tts_plugin=tts,
                client=client,
                session_id="session-personal",
                mode="self_preview",
                policy=_self_preview_policy(),
            )

            assert client.cached(session_id="session-personal") is None
            assert tts.current_voice == "zh_female_tianmeitaozi_uranus_bigtts"
            assert tts.current_voice_profile_id == "bright_peer"
            assert tts.current_voice_kind == "designed"
        finally:
            await tts.aclose()


@pytest.mark.asyncio
async def test_cosyvoice_clone_is_not_applied_on_doubao_tts() -> None:
    voice_id = "cosyvoice-v3.5-flash-clone-owner001"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "mode": "active",
                "profile_id": "voice-profile-legacy",
                "provider": "alibaba_model_studio",
                "voice_kind": "personal",
                "model": "cosyvoice-v3.5-flash",
                "resource_id": "cosyvoice-v3.5-flash",
                "voice_id": voice_id,
                "speaker_sha256": _speaker_sha256(voice_id),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )
        tts = DoubaoTTS(
            DoubaoTTSConfig(
                api_key="test",
                voice_profile="warm_companion",
                speaker="zh_male_yangguangqingnian_uranus_bigtts",
            )
        )
        try:
            assert await client.refresh(session_id="session-legacy")
            _apply_cached_voice_profile(
                tts_plugin=tts,
                client=client,
                session_id="session-legacy",
                mode="self_preview",
                policy=_self_preview_policy(),
            )
            assert tts.current_voice_kind == "designed"
            assert tts.current_voice_profile_id == "warm_companion"
        finally:
            await tts.aclose()


@pytest.mark.asyncio
async def test_legacy_personal_voice_revocation_switches_to_frozen_designed_fallback() -> None:
    revoked = False

    def handler(_: httpx.Request) -> httpx.Response:
        if revoked:
            return httpx.Response(409, json={"detail": {"code": "legacy_voice_unavailable"}})
        return httpx.Response(
            200,
            json={
                "mode": "active",
                "profile_id": "voice-profile-personal",
                "provider": "volcengine_doubao",
                "voice_kind": "personal",
                "model": "seed-icl-2.0",
                "resource_id": "seed-icl-2.0",
                "voice_id": "S_personal_synth_ready",
                "speaker_sha256": _speaker_sha256("S_personal_synth_ready"),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )
        tts = DoubaoTTS(
            DoubaoTTSConfig(
                api_key="test",
                voice_profile="warm_companion",
                speaker="zh_male_yangguangqingnian_uranus_bigtts",
            )
        )
        try:
            assert await client.refresh(session_id="session-legacy")
            _apply_cached_voice_profile(
                tts_plugin=tts,
                client=client,
                session_id="session-legacy",
                mode="legacy",
                policy=_legacy_policy(voice_allowed=True),
            )
            assert tts.current_voice_kind == "personal"

            revoked = True
            assert not await client.refresh(session_id="session-legacy")
            _apply_cached_voice_profile(
                tts_plugin=tts,
                client=client,
                session_id="session-legacy",
                mode="legacy",
                policy=_legacy_policy(voice_allowed=True),
            )
            assert client.cached(session_id="session-legacy") is None
            assert tts.current_voice_profile_id == "bright_peer"
            assert tts.current_voice == "zh_female_tianmeitaozi_uranus_bigtts"
            assert tts.current_voice_kind == "designed"
        finally:
            await tts.aclose()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provider", None),
        ("voice_kind", None),
        ("voice_kind", "designed"),
        ("model", "seed-tts-2.0"),
        ("resource_id", "seed-tts-2.0"),
        ("voice_id", "zh_male_yangguangqingnian_uranus_bigtts"),
    ),
)
@pytest.mark.asyncio
async def test_personal_voice_requires_exact_provider_marker_and_clone_resource(
    field: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "mode": "active",
        "profile_id": "voice-profile-personal",
        "provider": "volcengine_doubao",
        "voice_kind": "personal",
        "model": "seed-icl-2.0",
        "resource_id": "seed-icl-2.0",
        "voice_id": "S_personal_synth_ready",
        "speaker_sha256": _speaker_sha256("S_personal_synth_ready"),
    }
    payload[field] = value

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )

        assert not await client.refresh(session_id=f"session-invalid-{field}")
        assert client.cached(session_id=f"session-invalid-{field}") is None


@pytest.mark.asyncio
async def test_designed_companion_voice_resolves_from_approved_local_registry() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "mode": "designed",
                "profile_id": "bright_peer",
                "provider": "volcengine_doubao",
                "voice_kind": "designed",
                "model": "seed-tts-2.0",
                "resource_id": "seed-tts-2.0",
                "voice_id": None,
                "speaker_sha256": None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )
        assert await client.refresh(session_id="session-002")
        profile = client.cached(session_id="session-002")

    assert profile is not None
    assert profile.profile_id == "bright_peer"
    assert profile.model == "seed-tts-2.0"
    assert profile.voice_id == "zh_female_tianmeitaozi_uranus_bigtts"


@pytest.mark.asyncio
async def test_active_personal_voice_rejects_forged_speaker_digest() -> None:
    payload = {
        "mode": "active",
        "profile_id": "voice-profile-personal",
        "provider": "volcengine_doubao",
        "voice_kind": "personal",
        "model": "seed-icl-2.0",
        "resource_id": "seed-icl-2.0",
        "voice_id": "S_personal_synth_ready",
        "speaker_sha256": "f" * 64,
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )

        assert not await client.refresh(session_id="session-forged-digest")
        assert client.cached(session_id="session-forged-digest") is None


@pytest.mark.asyncio
async def test_voice_resolution_rejects_missing_digest_field() -> None:
    payload = {
        "mode": "designed",
        "profile_id": "bright_peer",
        "provider": "volcengine_doubao",
        "voice_kind": "designed",
        "model": "seed-tts-2.0",
        "resource_id": "seed-tts-2.0",
        "voice_id": None,
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )

        assert not await client.refresh(session_id="missing-speaker-digest")


@pytest.mark.asyncio
async def test_doubao_resolver_failure_restores_selected_companion_voice() -> None:
    failing = False

    def handler(request: httpx.Request) -> httpx.Response:
        if failing:
            raise httpx.ReadTimeout("resolver unavailable", request=request)
        return httpx.Response(
            200,
            json={
                "mode": "designed",
                "profile_id": "bright_peer",
                "provider": "volcengine_doubao",
                "voice_kind": "designed",
                "model": "seed-tts-2.0",
                "resource_id": "seed-tts-2.0",
                "voice_id": None,
                "speaker_sha256": None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )
        tts = DoubaoTTS(
            DoubaoTTSConfig(
                api_key="test",
                voice_profile="warm_companion",
                speaker="zh_male_yangguangqingnian_uranus_bigtts",
            )
        )

        assert await client.refresh(session_id="session-taoxi")
        _apply_cached_voice_profile(
            tts_plugin=tts,
            client=client,
            session_id="session-taoxi",
        )
        assert tts.current_voice == "zh_female_tianmeitaozi_uranus_bigtts"

        failing = True
        assert not await client.refresh(session_id="session-taoxi")
        _apply_cached_voice_profile(
            tts_plugin=tts,
            client=client,
            session_id="session-taoxi",
        )

        assert tts.current_voice == "zh_female_tianmeitaozi_uranus_bigtts"
        await tts.aclose()


@pytest.mark.asyncio
async def test_self_preview_applies_selected_designed_fallback_from_resolver() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "mode": "designed",
                    "profile_id": "bright_peer",
                    "provider": "volcengine_doubao",
                    "voice_kind": "designed",
                    "model": "seed-tts-2.0",
                    "resource_id": "seed-tts-2.0",
                    "voice_id": None,
                    "speaker_sha256": None,
                },
            )
        )
    ) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )
        tts = DoubaoTTS(
            DoubaoTTSConfig(
                api_key="test",
                voice_profile="warm_companion",
                speaker="zh_male_yangguangqingnian_uranus_bigtts",
            )
        )
        try:
            assert await client.refresh(session_id="self-preview-fallback")
            _apply_cached_voice_profile(
                tts_plugin=tts,
                client=client,
                session_id="self-preview-fallback",
                mode="self_preview",
                policy=_self_preview_policy(),
            )

            assert tts.current_voice_profile_id == "bright_peer"
            assert tts.current_voice == "zh_female_tianmeitaozi_uranus_bigtts"
            assert tts.current_voice_kind == "designed"
        finally:
            await tts.aclose()


@pytest.mark.asyncio
async def test_doubao_applied_profile_does_not_replace_configured_baseline() -> None:
    tts = DoubaoTTS(
        DoubaoTTSConfig(
            api_key="test",
            voice_profile="warm_companion",
            speaker="zh_male_yangguangqingnian_uranus_bigtts",
        )
    )

    tts.apply_voice_profile(
        model="seed-tts-2.0",
        voice="zh_female_tianmeitaozi_uranus_bigtts",
    )
    assert tts.current_voice == "zh_female_tianmeitaozi_uranus_bigtts"
    tts.use_baseline_voice()

    assert tts.current_voice == "zh_male_yangguangqingnian_uranus_bigtts"
    await tts.aclose()


@pytest.mark.asyncio
async def test_doubao_accepts_only_explicit_personal_clone_resolution() -> None:
    tts = DoubaoTTS(
        DoubaoTTSConfig(
            api_key="test",
            voice_profile="warm_companion",
            speaker="zh_male_yangguangqingnian_uranus_bigtts",
        )
    )
    try:
        tts.apply_voice_profile(
            model="seed-icl-2.0",
            resource_id="seed-icl-2.0",
            voice="S_personal_synth_ready",
            profile_id="voice-profile-personal",
            provider="volcengine_doubao",
            voice_kind="personal",
        )

        assert tts.current_model == "seed-icl-2.0"
        assert tts.current_voice == "S_personal_synth_ready"
        assert tts.current_voice_profile_id == "voice-profile-personal"
        assert tts.current_voice_kind == "personal"

        with pytest.raises(ValueError):
            tts.apply_voice_profile(
                model="seed-icl-2.0",
                resource_id="seed-icl-2.0",
                voice="arbitrary-speaker",
                profile_id=None,
                provider="volcengine_doubao",
                voice_kind="personal",
            )
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_companion_mode_never_applies_a_personal_clone() -> None:
    payload = {
        "mode": "active",
        "profile_id": "voice-profile-personal",
        "provider": "volcengine_doubao",
        "voice_kind": "personal",
        "model": "seed-icl-2.0",
        "resource_id": "seed-icl-2.0",
        "voice_id": "S_personal_companion_rejected",
        "speaker_sha256": _speaker_sha256("S_personal_companion_rejected"),
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )
        tts = DoubaoTTS(
            DoubaoTTSConfig(
                api_key="test",
                voice_profile="warm_companion",
                speaker="zh_male_yangguangqingnian_uranus_bigtts",
            )
        )
        try:
            assert await client.refresh(session_id="companion-personal")
            _apply_cached_voice_profile(
                tts_plugin=tts,
                client=client,
                session_id="companion-personal",
                mode="companion",
            )

            assert tts.current_model == "seed-tts-2.0"
            assert tts.current_voice_profile_id == "warm_companion"
            assert tts.current_voice_kind == "designed"
        finally:
            await tts.aclose()


def _companion_clone_policy(*, provider: str, model: str, voice_id: str) -> ModePolicy:
    payload = {
        "interaction_mode": "companion",
        "session_focus": "chat",
        "mode_policy_version": "s2-v1",
        "companion_style_id": "taoxi",
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
        "voice_profile_id": "voice-profile-personal",
        "voice_profile_version": 2,
        "voice_provider": provider,
        "voice_model": model,
        "voice_resource_id": model if provider == "alibaba_model_studio" else "seed-icl-2.0",
        "voice_provider_expires_at": (
            None if provider == "alibaba_model_studio" else "2027-07-23T00:00:00+00:00"
        ),
        "voice_speaker_sha256": _speaker_sha256(voice_id),
        "fallback_voice_profile_id": "bright_peer",
        "fallback_voice_provider": "volcengine_doubao",
        "fallback_voice_model": "seed-tts-2.0",
        "fallback_voice_resource_id": "seed-tts-2.0",
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
    policy = ModePolicyClient._parse(payload)
    assert policy.available
    return policy


@pytest.mark.asyncio
async def test_companion_mode_applies_a_matching_frozen_personal_clone() -> None:
    voice_id = "S_personal_companion_allowed"
    policy = _companion_clone_policy(
        provider="volcengine_doubao",
        model="seed-icl-2.0",
        voice_id=voice_id,
    )
    payload = {
        "mode": "active",
        "profile_id": "voice-profile-personal",
        "provider": "volcengine_doubao",
        "voice_kind": "personal",
        "model": "seed-icl-2.0",
        "resource_id": "seed-icl-2.0",
        "voice_id": voice_id,
        "speaker_sha256": _speaker_sha256(voice_id),
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )
        tts = DoubaoTTS(
            DoubaoTTSConfig(
                api_key="test",
                voice_profile="warm_companion",
                speaker="zh_male_yangguangqingnian_uranus_bigtts",
            )
        )
        try:
            assert await client.refresh(session_id="companion-clone")
            _apply_cached_voice_profile(
                tts_plugin=tts,
                client=client,
                session_id="companion-clone",
                mode="companion",
                policy=policy,
            )
            assert tts.current_voice_kind == "personal"
            assert tts.current_voice == voice_id
            assert frozen_companion_clone_permitted(policy) is True
            assert (
                generation_voice_reject_reason(
                    policy,
                    personal_voice_permitted=True,
                    profile_id="voice-profile-personal",
                    resource_id="seed-icl-2.0",
                    speaker_sha256=_speaker_sha256(voice_id),
                    voice_kind="personal",
                )
                is None
            )
        finally:
            await tts.aclose()


@pytest.mark.asyncio
async def test_wake_aligns_tts_to_the_frozen_catalog_companion() -> None:
    policy = ModePolicyClient._parse(
        {
            "interaction_mode": "companion",
            "session_focus": "chat",
            "mode_policy_version": "s2-v1",
            "companion_style_id": "taoxi",
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
    )
    tts = DoubaoTTS(
        DoubaoTTSConfig(
            api_key="test",
            voice_profile="warm_companion",
            speaker="zh_male_yangguangqingnian_uranus_bigtts",
        )
    )
    try:
        align_tts_voice_to_policy(tts, policy, personal_voice_permitted=False)
        assert tts.current_voice_profile_id == "bright_peer"
        assert tts.current_voice_kind == "designed"
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_voice_profile_client_accepts_a_cosyvoice_personal_clone() -> None:
    voice_id = "cosyvoice-v3.5-flash-clone-owner001"
    payload = {
        "mode": "active",
        "profile_id": "voice-profile-personal",
        "provider": "alibaba_model_studio",
        "voice_kind": "personal",
        "model": "cosyvoice-v3.5-flash",
        "resource_id": "cosyvoice-v3.5-flash",
        "voice_id": voice_id,
        "speaker_sha256": _speaker_sha256(voice_id),
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http_client:
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint="https://control.test/v1/voices/session-resolution",
                internal_token="voice-internal-token",
            ),
            client=http_client,
        )
        assert await client.refresh(session_id="companion-cosyvoice")
        cached = client.cached(session_id="companion-cosyvoice")
        assert cached is not None
        assert cached.provider == "alibaba_model_studio"
        assert cached.voice_kind == "personal"
        assert cached.voice_id == voice_id


@pytest.mark.asyncio
async def test_tts_switches_to_approved_clone_and_restores_exact_baseline() -> None:
    config = CosyVoiceConfig(
        api_key="test",
        ws_url="wss://example.test",
        model="cosyvoice-v3.5-flash",
        voice="cosyvoice-v3.5-flash-vd-warmboy-baseline",
        rate=1.0,
    )
    tts = CosyVoiceTTS(config)

    tts.apply_voice_profile(
        model="cosyvoice-v3.5-flash",
        voice="cosyvoice-v3.5-flash-clone-owner001",
        profile_id="voice-profile-personal",
        provider="alibaba_model_studio",
        voice_kind="personal",
        resource_id="cosyvoice-v3.5-flash",
    )
    assert tts.current_voice == "cosyvoice-v3.5-flash-clone-owner001"
    assert tts.current_voice_kind == "personal"
    assert tts.current_voice_profile_id == "voice-profile-personal"
    assert tts.current_rate == 1.0
    tts.use_baseline_voice()

    assert tts.current_model == "cosyvoice-v3.5-flash"
    assert tts.current_voice == "cosyvoice-v3.5-flash-vd-warmboy-baseline"
    assert tts.current_rate == 1.0
    await tts.aclose()
