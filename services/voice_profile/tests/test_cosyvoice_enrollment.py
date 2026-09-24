from __future__ import annotations

import json

import httpx
import pytest
from services.voice_profile.cosyvoice_enrollment import (
    CosyVoiceEnrollmentClient,
    CosyVoiceEnrollmentConfig,
)


@pytest.mark.asyncio
async def test_provider_client_uses_official_enrollment_actions_without_leaking_key() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(
            {
                "authorization": request.headers.get("Authorization"),
                "body": body,
            }
        )
        if body["input"]["action"] == "create_voice":
            return httpx.Response(
                200,
                json={
                    "output": {
                        "voice_id": "qwen-audio-3.1-tts-flash-owner001-abc123",
                        "target_model": "qwen-audio-3.1-tts-flash",
                    }
                },
            )
        return httpx.Response(200, json={"output": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = CosyVoiceEnrollmentClient(
            CosyVoiceEnrollmentConfig(
                endpoint="https://dashscope.test/api/v1/services/audio/tts/customization",
                api_key="provider-secret-key",
                timeout_s=5,
            ),
            client=http_client,
        )
        voice = await client.create_voice(
            target_model="qwen-audio-3.1-tts-flash",
            prefix="owner001",
            sample_url="https://control.test/one-time-sample.wav?token=signed",
        )
        await client.delete_voice(voice_id=voice.voice_id)

    assert voice.voice_id == "qwen-audio-3.1-tts-flash-owner001-abc123"
    assert requests[0]["body"] == {
        "model": "voice-enrollment",
        "input": {
            "action": "create_voice",
            "target_model": "qwen-audio-3.1-tts-flash",
            "prefix": "owner001",
            "url": "https://control.test/one-time-sample.wav?token=signed",
            "language_hints": ["zh"],
        },
    }
    assert requests[1]["body"] == {
        "model": "voice-enrollment",
        "input": {
            "action": "delete_voice",
            "voice_id": "qwen-audio-3.1-tts-flash-owner001-abc123",
        },
    }
    assert all(item["authorization"] == "Bearer provider-secret-key" for item in requests)


def _enrollment_client(http_client: httpx.AsyncClient) -> CosyVoiceEnrollmentClient:
    return CosyVoiceEnrollmentClient(
        CosyVoiceEnrollmentConfig(
            endpoint="https://dashscope.test/api/v1/services/audio/tts/customization",
            api_key="provider-secret-key",
            timeout_s=5,
        ),
        client=http_client,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_model",
    ["cosyvoice-v3.5-flash", "seed-icl-2.0", "qwen-audio-3.1-tts"],
)
async def test_enrollment_rejects_targets_other_than_the_synthesis_model(
    target_model: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(ValueError, match="qwen-audio-3.1-tts-flash"):
            await _enrollment_client(http_client).create_voice(
                target_model=target_model,
                prefix="owner001",
                sample_url="https://control.test/one-time-sample.wav?token=signed",
            )

    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "voice_id",
    [
        "cosyvoice-v3.5-flash-owner001-abc123",
        "qwen-audio-3.1-tts-flash-other-abc123",
        "S_legacyDoubaoSpeaker",
    ],
)
async def test_enrollment_rejects_voice_ids_bound_to_another_model_or_prefix(
    voice_id: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": {
                    "voice_id": voice_id,
                    "target_model": "qwen-audio-3.1-tts-flash",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(RuntimeError, match="another model"):
            await _enrollment_client(http_client).create_voice(
                target_model="qwen-audio-3.1-tts-flash",
                prefix="owner001",
                sample_url="https://control.test/one-time-sample.wav?token=signed",
            )
