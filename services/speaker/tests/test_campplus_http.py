from __future__ import annotations

import base64
import json

import httpx
import pytest
from services.speaker.campplus_http import CampPlusHTTPEmbeddingAdapter


@pytest.mark.asyncio
async def test_campplus_http_adapter_maps_the_internal_embedding_contract() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("Authorization")
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model_version": "campplus-2026-07",
                "embedding": [0.25, 0.75],
                "speech_ms": 1700,
                "snr_db": 18.5,
                "quality_score": 0.91,
                "replay_risk": 0.08,
                "synthetic_risk": 0.06,
                "risk_assessment": "verified",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CampPlusHTTPEmbeddingAdapter(
            endpoint="https://speaker-model.test/v1/embeddings/speaker",
            token="internal-model-token",
            model_version="campplus-2026-07",
            client=client,
        )
        result = await adapter.embed(b"pcm-audio", sample_rate=16000)

    assert observed == {
        "authorization": "Bearer internal-model-token",
        "body": {
            "audio_base64": base64.b64encode(b"pcm-audio").decode("ascii"),
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
        },
    }
    assert result.vector == (0.25, 0.75)
    assert result.speech_ms == 1700
    assert result.quality_score == 0.91
    assert result.synthetic_risk == 0.06
    assert result.risk_assessment == "verified"


@pytest.mark.asyncio
async def test_campplus_enrollment_embed_uses_longer_timeout() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "model_version": "campplus-2026-07",
                "embedding": [0.25, 0.75],
                "speech_ms": 1700,
                "snr_db": 18.5,
                "quality_score": 0.91,
                "replay_risk": 0.08,
                "synthetic_risk": 0.06,
                "risk_assessment": "verified",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CampPlusHTTPEmbeddingAdapter(
            endpoint="https://speaker-model.test/v1/embeddings/speaker",
            token="internal-model-token",
            model_version="campplus-2026-07",
            timeout_s=1.0,
            client=client,
        )
        result = await adapter.embed_enrollment(b"pcm-audio", sample_rate=16000)

    assert adapter._enrollment_timeout.read == 5.0
    assert adapter._timeout.read == 1.0
    assert result.speech_ms == 1700
    assert observed["path"] == "/v1/embeddings/speaker"


@pytest.mark.asyncio
async def test_campplus_http_adapter_rejects_wrong_model_or_invalid_payload() -> None:
    responses = iter(
        [
            httpx.Response(
                200,
                json={
                    "model_version": "unexpected-model",
                    "embedding": [1.0, 0.0],
                    "speech_ms": 1000,
                    "snr_db": 12,
                    "quality_score": 0.8,
                    "replay_risk": 0.1,
                    "synthetic_risk": 0.1,
                    "risk_assessment": "verified",
                },
            ),
            httpx.Response(200, json={"model_version": "campplus-2026-07"}),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CampPlusHTTPEmbeddingAdapter(
            endpoint="https://speaker-model.test/v1/embeddings/speaker",
            token="internal-model-token",
            model_version="campplus-2026-07",
            client=client,
        )
        with pytest.raises(ValueError, match="model version"):
            await adapter.embed(b"pcm", sample_rate=16000)
        with pytest.raises(ValueError, match="invalid embedding response"):
            await adapter.embed(b"pcm", sample_rate=16000)
