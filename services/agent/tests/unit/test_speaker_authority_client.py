from __future__ import annotations

import base64
import json

import httpx
import pytest
from services.agent.src.speaker_authority_client import (
    SpeakerAuthorityClient,
    SpeakerAuthorityClientConfig,
)


@pytest.mark.asyncio
async def test_client_classifies_session_audio_without_sending_account_id() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["token"] = request.headers.get("X-Memoria-Speaker-Token")
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "classification": "guest",
                "score": 0.1,
                "quality_score": 0.92,
                "reason_code": "owner_mismatch",
                "model_version": "campplus-v1",
                "template_version": 3,
                "profile_id": "profile-003",
                "permissions": {
                    "normal_conversation": True,
                    "read_private_memory": False,
                    "write_long_term_memory": False,
                    "sensitive_actions": False,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint="https://control.test/v1/speakers/classify",
                internal_token="speaker-internal-token",
                timeout_s=0.4,
            ),
            client=http_client,
        )
        decision = await client.classify(
            session_id="session-001",
            pcm=b"\x00\x01\x02\x03",
            sample_rate=16000,
        )

    assert observed == {
        "token": "speaker-internal-token",
        "body": {
            "audio_base64": base64.b64encode(b"\x00\x01\x02\x03").decode("ascii"),
            "sample_rate": 16000,
            "session_id": "session-001",
        },
    }
    assert decision.classification == "guest"
    assert decision.permissions.normal_conversation is True
    assert decision.permissions.read_private_memory is False


@pytest.mark.asyncio
async def test_client_rejects_malformed_authority_response() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    ) as http_client:
        client = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint="https://control.test/v1/speakers/classify",
                internal_token="speaker-internal-token",
            ),
            client=http_client,
        )
        with pytest.raises(ValueError, match="invalid speaker authority response"):
            await client.classify(
                session_id="session-001",
                pcm=b"\x00\x01",
                sample_rate=16000,
            )
