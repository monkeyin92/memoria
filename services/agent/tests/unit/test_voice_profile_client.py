from __future__ import annotations

import json

import httpx
import pytest
from services.agent.src.agent import _apply_cached_voice_profile
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig
from services.agent.src.voice_profile_client import (
    VoiceProfileClient,
    VoiceProfileClientConfig,
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
                "model": "seed-tts-2.0",
                "voice_id": "zh_male_yangguangqingnian_uranus_bigtts",
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
    assert profile.voice_id == "zh_male_yangguangqingnian_uranus_bigtts"


@pytest.mark.asyncio
async def test_failure_keeps_last_approved_voice_until_explicit_fallback() -> None:
    response = {
        "mode": "active",
        "profile_id": "voice-profile-001",
        "model": "seed-tts-2.0",
        "voice_id": "zh_male_yangguangqingnian_uranus_bigtts",
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
            cached = client.cached(session_id="session-001")
            assert cached is not None
            assert cached.voice_id == "zh_male_yangguangqingnian_uranus_bigtts"
        response.clear()
        response.update(
            {
                "mode": "fallback",
                "profile_id": None,
                "model": None,
                "voice_id": None,
            }
        )
        assert await client.refresh(session_id="session-001")
        assert client.cached(session_id="session-001") is None


@pytest.mark.asyncio
async def test_legacy_cosyvoice_active_profile_is_never_cached_for_doubao() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "mode": "active",
                "profile_id": "voice-profile-legacy",
                "model": "cosyvoice-v3.5-flash",
                "voice_id": "cosyvoice-v3.5-flash-clone-owner001",
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


@pytest.mark.asyncio
async def test_designed_companion_voice_resolves_from_approved_local_registry() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "mode": "designed",
                "profile_id": "bright_peer",
                "model": "seed-tts-2.0",
                "voice_id": None,
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
                "model": "seed-tts-2.0",
                "voice_id": None,
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
