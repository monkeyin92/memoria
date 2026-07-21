from __future__ import annotations

import json

import httpx
import pytest
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
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
                "model": "cosyvoice-v3.5-flash",
                "voice_id": "cosyvoice-v3.5-flash-clone-owner001",
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
    assert profile.voice_id == "cosyvoice-v3.5-flash-clone-owner001"


@pytest.mark.asyncio
async def test_fallback_or_failure_clears_clone_before_next_tts() -> None:
    response = {
        "mode": "active",
        "profile_id": "voice-profile-001",
        "model": "cosyvoice-v3.5-flash",
        "voice_id": "cosyvoice-v3.5-flash-clone-owner001",
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
        response["mode"] = "error"
        assert not await client.refresh(session_id="session-001")
        assert client.cached(session_id="session-001") is None


@pytest.mark.asyncio
async def test_designed_companion_voice_resolves_from_approved_local_registry() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "mode": "designed",
                "profile_id": "bright_peer",
                "model": "cosyvoice-v3.5-flash",
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
    assert profile.voice_id.startswith("cosyvoice-v3.5-flash-vd-brightpeer-")


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
