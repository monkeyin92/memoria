from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
from services.speaker_model.app import create_app
from services.speaker_model.config import SpeakerModelSettings
from services.speaker_model.engine import SpeakerModelEmbedding


class FakeCampPlusEngine:
    model_version = "campplus-cn-common-test"

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, int]] = []

    def embed(self, pcm: bytes, *, sample_rate: int) -> SpeakerModelEmbedding:
        self.calls.append((pcm, sample_rate))
        return SpeakerModelEmbedding(
            vector=(1.0,) + (0.0,) * 191,
            speech_ms=1_250,
            snr_db=18.5,
            quality_score=0.91,
            replay_risk=1.0,
            synthetic_risk=1.0,
            risk_assessment="unavailable",
        )


@pytest.mark.asyncio
async def test_embedding_endpoint_requires_bearer_and_returns_campplus_contract(
    tmp_path: Path,
) -> None:
    engine = FakeCampPlusEngine()
    app = create_app(
        settings=SpeakerModelSettings(
            token="speaker-model-secret",
            model_path=tmp_path / "campplus.onnx",
            model_version=engine.model_version,
        ),
        engine=engine,
    )
    pcm = b"\x01\x00" * 16_000
    request = {
        "audio_base64": base64.b64encode(pcm).decode("ascii"),
        "encoding": "pcm_s16le",
        "sample_rate": 16_000,
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://speaker-model.test",
    ) as client:
        unauthorized = await client.post("/v1/embeddings/speaker", json=request)
        response = await client.post(
            "/v1/embeddings/speaker",
            headers={"Authorization": "Bearer speaker-model-secret"},
            json=request,
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert payload["model_version"] == engine.model_version
    assert payload["embedding"] == [1.0] + [0.0] * 191
    assert payload["speech_ms"] == 1_250
    assert payload["snr_db"] == 18.5
    assert payload["quality_score"] == 0.91
    assert payload["replay_risk"] == 1.0
    assert payload["synthetic_risk"] == 1.0
    assert payload["risk_assessment"] == "unavailable"
    assert engine.calls == [(pcm, 16_000)]


@pytest.mark.asyncio
async def test_embedding_endpoint_rejects_malformed_or_oversized_pcm(tmp_path: Path) -> None:
    engine = FakeCampPlusEngine()
    app = create_app(
        settings=SpeakerModelSettings(
            token="speaker-model-secret",
            model_path=tmp_path / "campplus.onnx",
            model_version=engine.model_version,
            max_audio_bytes=8,
        ),
        engine=engine,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://speaker-model.test",
        headers={"Authorization": "Bearer speaker-model-secret"},
    ) as client:
        malformed = await client.post(
            "/v1/embeddings/speaker",
            json={"audio_base64": "not-base64", "encoding": "pcm_s16le", "sample_rate": 16_000},
        )
        odd = await client.post(
            "/v1/embeddings/speaker",
            json={
                "audio_base64": base64.b64encode(b"odd").decode("ascii"),
                "encoding": "pcm_s16le",
                "sample_rate": 16_000,
            },
        )
        oversized = await client.post(
            "/v1/embeddings/speaker",
            json={
                "audio_base64": base64.b64encode(b"\x00\x00" * 5).decode("ascii"),
                "encoding": "pcm_s16le",
                "sample_rate": 16_000,
            },
        )
        wrong_encoding = await client.post(
            "/v1/embeddings/speaker",
            json={"audio_base64": "AAAA", "encoding": "wav", "sample_rate": 16_000},
        )
        wrong_sample_rate = await client.post(
            "/v1/embeddings/speaker",
            json={"audio_base64": "AAAA", "encoding": "pcm_s16le", "sample_rate": 24_000},
        )

    assert (malformed.status_code, odd.status_code, oversized.status_code) == (422, 422, 422)
    assert wrong_encoding.status_code == 422
    assert wrong_sample_rate.status_code == 422
    assert engine.calls == []


@pytest.mark.asyncio
async def test_embedding_failure_is_fail_closed_without_leaking_runtime_error(
    tmp_path: Path,
) -> None:
    class FailingEngine(FakeCampPlusEngine):
        def embed(self, pcm: bytes, *, sample_rate: int) -> SpeakerModelEmbedding:
            _ = (pcm, sample_rate)
            raise RuntimeError("onnx path and host details must stay private")

    engine = FailingEngine()
    app = create_app(
        settings=SpeakerModelSettings(
            token="speaker-model-secret",
            model_path=tmp_path / "campplus.onnx",
            model_version=engine.model_version,
        ),
        engine=engine,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://speaker-model.test",
    ) as client:
        response = await client.post(
            "/v1/embeddings/speaker",
            headers={"Authorization": "Bearer speaker-model-secret"},
            json={
                "audio_base64": base64.b64encode(b"\x00\x00" * 16_000).decode("ascii"),
                "encoding": "pcm_s16le",
                "sample_rate": 16_000,
            },
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "speaker model inference unavailable"}
