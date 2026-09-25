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
                        "voice_id": "cosyvoice-v3.5-flash-clone-owner-001",
                        "target_model": "cosyvoice-v3.5-flash",
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
            target_model="cosyvoice-v3.5-flash",
            prefix="owner001",
            sample_url="https://control.test/one-time-sample.wav?token=signed",
        )
        await client.delete_voice(voice_id=voice.voice_id)

    assert voice.voice_id == "cosyvoice-v3.5-flash-clone-owner-001"
    assert requests[0]["body"] == {
        "model": "voice-enrollment",
        "input": {
            "action": "create_voice",
            "target_model": "cosyvoice-v3.5-flash",
            "prefix": "owner001",
            "url": "https://control.test/one-time-sample.wav?token=signed",
            "language_hints": ["zh"],
        },
    }
    assert requests[1]["body"] == {
        "model": "voice-enrollment",
        "input": {
            "action": "delete_voice",
            "voice_id": "cosyvoice-v3.5-flash-clone-owner-001",
        },
    }
    assert all(item["authorization"] == "Bearer provider-secret-key" for item in requests)
