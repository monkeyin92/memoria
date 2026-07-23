from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
from services.voice_profile.domain import ProviderVoiceDeletionUnsupportedError
from services.voice_profile.doubao_voice_clone import (
    DOUBAO_VOICE_CLONE_MODEL,
    DoubaoVoiceCloneClient,
    DoubaoVoiceCloneConfig,
)


@pytest.mark.asyncio
async def test_clone_downloads_signed_sample_polls_and_uses_explicit_response_field() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                content=b"RIFF-signed-sample",
                headers={"Content-Type": "audio/wav"},
            )
        body = json.loads(request.content)
        if request.url.path.endswith("/voice_clone"):
            assert body["audio"] == {
                "data": base64.b64encode(b"RIFF-signed-sample").decode("ascii"),
                "format": "wav",
            }
            assert body["speaker_id"] == "custom_speaker_id"
            assert body["custom_speaker_id"] == "m123456789"
            assert body["model_type"] == 5
            assert "extra_params" not in body
            return httpx.Response(200, json={"status": 1})
        assert body == {
            "speaker_id": "custom_speaker_id",
            "custom_speaker_id": "m123456789",
            "model_type": 5,
        }
        return httpx.Response(
            200,
            json={
                "status": 2,
                "result": {
                    "synth_ready_speaker": "icl-ready-001",
                    "ExpireTime": 4_102_444_800_000,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DoubaoVoiceCloneClient(
            DoubaoVoiceCloneConfig(
                endpoint="https://openspeech.test/api/v3/tts/voice_clone",
                query_endpoint="https://openspeech.test/api/v3/tts/get_voice",
                api_key="independent-control-api-key",
                timeout_s=1,
                poll_interval_s=0.001,
                synth_ready_id_mode="response_field",
                synth_ready_id_field="result.synth_ready_speaker",
                expires_at_field="result.ExpireTime",
            ),
            client=http_client,
        )
        voice = await client.create_voice(
            target_model=DOUBAO_VOICE_CLONE_MODEL,
            prefix="m123456789",
            sample_url="https://control.test/v1/voices/provider-samples/sample?token=signed",
        )

    assert voice.voice_id == "icl-ready-001"
    assert voice.target_model == DOUBAO_VOICE_CLONE_MODEL
    assert voice.expires_at == datetime(2100, 1, 1, tzinfo=UTC)
    assert [request.method for request in requests] == ["GET", "POST", "POST"]
    assert all(
        request.headers.get("X-Api-Key") != "independent-control-api-key"
        for request in requests
        if request.method == "GET"
    )
    assert all(
        request.headers.get("X-Api-Key") == "independent-control-api-key"
        and request.headers.get("X-Api-Request-Id")
        for request in requests
        if request.method == "POST"
    )


@pytest.mark.asyncio
async def test_clone_fails_closed_when_synth_ready_id_mapping_is_unverified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                content=b"RIFF",
                headers={"Content-Type": "audio/wav"},
            )
        return httpx.Response(200, json={"status": 2, "speaker_id": "not-safe-to-guess"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DoubaoVoiceCloneClient(
            DoubaoVoiceCloneConfig(
                endpoint="https://openspeech.test/api/v3/tts/voice_clone",
                query_endpoint="https://openspeech.test/api/v3/tts/get_voice",
                api_key="independent-control-api-key",
                timeout_s=1,
                expires_at_field="result.ExpireTime",
            ),
            client=http_client,
        )
        with pytest.raises(RuntimeError, match="synth-ready speaker ID is unverified"):
            await client.create_voice(
                target_model=DOUBAO_VOICE_CLONE_MODEL,
                prefix="m123456789",
                sample_url="https://control.test/v1/voices/provider-samples/sample?token=signed",
            )


@pytest.mark.asyncio
async def test_clone_does_not_promote_failed_training_to_a_voice() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                content=b"RIFF",
                headers={"Content-Type": "audio/wav"},
            )
        return httpx.Response(200, json={"status": 3})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DoubaoVoiceCloneClient(
            DoubaoVoiceCloneConfig(
                endpoint="https://openspeech.test/api/v3/tts/voice_clone",
                query_endpoint="https://openspeech.test/api/v3/tts/get_voice",
                api_key="independent-control-api-key",
                timeout_s=1,
                expires_at_field="result.ExpireTime",
            ),
            client=http_client,
        )
        with pytest.raises(RuntimeError, match="training failed"):
            await client.create_voice(
                target_model=DOUBAO_VOICE_CLONE_MODEL,
                prefix="m123456789",
                sample_url="https://control.test/sample",
            )


@pytest.mark.asyncio
async def test_clone_marks_provider_deletion_as_unsupported_not_completed() -> None:
    client = DoubaoVoiceCloneClient(
        DoubaoVoiceCloneConfig(
            endpoint="https://openspeech.test/api/v3/tts/voice_clone",
            query_endpoint="https://openspeech.test/api/v3/tts/get_voice",
            api_key="independent-control-api-key",
            expires_at_field="result.ExpireTime",
        )
    )

    with pytest.raises(ProviderVoiceDeletionUnsupportedError, match="manual reconciliation"):
        await client.delete_voice(voice_id="icl-ready-001")


@pytest.mark.asyncio
async def test_clone_rejects_unsigned_or_oversized_samples() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                content=b"x",
                headers={"Content-Type": "audio/wav", "Content-Length": str(10 * 1024 * 1024 + 1)},
            )
        raise AssertionError("provider request must not run")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DoubaoVoiceCloneClient(
            DoubaoVoiceCloneConfig(
                endpoint="https://openspeech.test/api/v3/tts/voice_clone",
                query_endpoint="https://openspeech.test/api/v3/tts/get_voice",
                api_key="independent-control-api-key",
                expires_at_field="result.ExpireTime",
            ),
            client=http_client,
        )
        with pytest.raises(ValueError, match="must use HTTPS"):
            await client.create_voice(
                target_model=DOUBAO_VOICE_CLONE_MODEL,
                prefix="m123456789",
                sample_url="http://control.test/sample",
            )
        with pytest.raises(ValueError, match="must not exceed 10 MiB"):
            await client.create_voice(
                target_model=DOUBAO_VOICE_CLONE_MODEL,
                prefix="m123456789",
                sample_url="https://control.test/sample",
            )


def test_clone_requires_an_explicit_provider_expiry_contract() -> None:
    with pytest.raises(ValueError, match="expiry field path is required"):
        DoubaoVoiceCloneConfig(
            endpoint="https://openspeech.test/api/v3/tts/voice_clone",
            query_endpoint="https://openspeech.test/api/v3/tts/get_voice",
            api_key="independent-control-api-key",
        )


@pytest.mark.asyncio
async def test_clone_rejects_expired_provider_voice() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                content=b"RIFF",
                headers={"Content-Type": "audio/wav"},
            )
        return httpx.Response(
            200,
            json={"status": 2, "result": {"ExpireTime": 946_684_800_000}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DoubaoVoiceCloneClient(
            DoubaoVoiceCloneConfig(
                endpoint="https://openspeech.test/api/v3/tts/voice_clone",
                query_endpoint="https://openspeech.test/api/v3/tts/get_voice",
                api_key="independent-control-api-key",
                synth_ready_id_mode="custom_speaker_id",
                expires_at_field="result.ExpireTime",
            ),
            client=http_client,
        )
        with pytest.raises(RuntimeError, match="already expired"):
            await client.create_voice(
                target_model=DOUBAO_VOICE_CLONE_MODEL,
                prefix="m123456789",
                sample_url="https://control.test/sample",
            )
