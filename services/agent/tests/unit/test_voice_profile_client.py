from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from services.agent.src.agent import _apply_cached_voice_profile
from services.agent.src.mode_policy_client import ModePolicy
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
async def test_legacy_cosyvoice_active_profile_is_never_cached_for_doubao() -> None:
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
                "voice_id": "cosyvoice-v3.5-flash-clone-owner001",
                "speaker_sha256": _speaker_sha256("cosyvoice-v3.5-flash-clone-owner001"),
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
        assert not await client.refresh(session_id="session-legacy")
        assert client.cached(session_id="session-legacy") is None


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
    )
    assert tts.current_voice == "cosyvoice-v3.5-flash-clone-owner001"
    assert tts.current_rate == 1.0
    tts.use_baseline_voice()

    assert tts.current_model == "cosyvoice-v3.5-flash"
    assert tts.current_voice == "cosyvoice-v3.5-flash-vd-warmboy-baseline"
    assert tts.current_rate == 1.0
    await tts.aclose()
