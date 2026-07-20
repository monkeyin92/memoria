from __future__ import annotations

import base64
import os
import wave
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from services.control_api.app.main import create_app as create_control_app
from services.speaker.authority import SpeakerAuthority
from services.speaker.campplus_http import CampPlusHTTPEmbeddingAdapter
from services.speaker_model.app import create_app as create_model_app
from services.speaker_model.config import SpeakerModelSettings
from services.speaker_model.engine import CampPlusOnnxEngine

_MODEL_VERSION = "campplus-cn-common@v1.0.0+ckpt.3388cf5f+onnx.7a39d2e5e566+fbank.v1"


def _pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (
            1,
            2,
            16_000,
        )
        return source.readframes(source.getnframes())


@pytest.mark.skipif(
    not os.environ.get("MEMORIA_CAMPLUS_ONNX_PATH")
    or not os.environ.get("MEMORIA_CAMPLUS_OFFICIAL_WAV_DIR"),
    reason="real CAM++ vertical slice requires explicit local model and official WAV paths",
)
@pytest.mark.asyncio
async def test_control_api_enrolls_and_classifies_through_real_campplus_onnx(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path = Path(os.environ["MEMORIA_CAMPLUS_ONNX_PATH"])
    wav_dir = Path(os.environ["MEMORIA_CAMPLUS_OFFICIAL_WAV_DIR"])
    speaker1_a = _pcm(wav_dir / "speaker1_a_cn_16k.wav")
    speaker1_b = _pcm(wav_dir / "speaker1_b_cn_16k.wav")
    speaker2_a = _pcm(wav_dir / "speaker2_a_cn_16k.wav")
    model_token = "speaker-model-vertical-test-token"
    model_app = create_model_app(
        settings=SpeakerModelSettings(
            token=model_token,
            model_path=model_path,
            model_version=_MODEL_VERSION,
        ),
        engine=CampPlusOnnxEngine(model_path, model_version=_MODEL_VERSION),
    )

    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_SPEAKER_INTERNAL_TOKEN", "test-speaker-internal-token")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    control_app = create_control_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=model_app),
        base_url="http://speaker-model.test",
    ) as model_client:
        control_app.state.speaker_authority = SpeakerAuthority.sqlite(
            tmp_path / "speakers.sqlite3",
            template_key=Fernet.generate_key().decode("ascii"),
            adapter=CampPlusHTTPEmbeddingAdapter(
                endpoint="http://speaker-model.test/v1/embeddings/speaker",
                token=model_token,
                model_version=_MODEL_VERSION,
                timeout_s=2.0,
                client=model_client,
            ),
            owner_threshold=0.78,
            guest_threshold=0.45,
            classify_timeout_s=2.0,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control_app),
            base_url="http://control.test",
        ) as client:
            identity = (
                await client.post(
                    "/v1/auth/register",
                    json={"username": "real-campplus-owner", "password": "safe-password"},
                )
            ).json()
            owner_headers = {"Authorization": f"Bearer {identity['access_token']}"}
            enrolled = await client.post(
                "/v1/speakers/enrollments",
                headers=owner_headers,
                json={
                    "consent_policy_version": "speaker-biometric-v1",
                    "consent_accepted": True,
                    "samples": [
                        {
                            "audio_base64": base64.b64encode(sample).decode("ascii"),
                            "sample_rate": 16_000,
                        }
                        for sample in (speaker1_a, speaker1_b, speaker1_a)
                    ],
                },
            )
            assert enrolled.status_code == 201, enrolled.text
            profile_id = enrolled.json()["profile_id"]
            internal = {"X-Memoria-Speaker-Token": "test-speaker-internal-token"}
            activation_rejected = await client.post(
                f"/v1/speakers/{profile_id}/activate",
                headers=internal,
                json={
                    "account_id": identity["user_id"],
                    "evaluation_ref": "local-plumbing-only-not-production-evaluation",
                    "sample_count": 200,
                    "far": 0.02,
                    "frr": 0.08,
                    "eer": 0.05,
                    "unknown_rejection": 0.93,
                    "passed": True,
                },
            )
            session = (
                await client.post(
                    "/v1/sessions",
                    headers=owner_headers,
                    json={"voice_backend": "cascade"},
                )
            ).json()

            async def classify(sample: bytes) -> httpx.Response:
                return await client.post(
                    "/v1/speakers/classify",
                    headers=internal,
                    json={
                        "session_id": session["session_id"],
                        "audio_base64": base64.b64encode(sample).decode("ascii"),
                        "sample_rate": 16_000,
                    },
                )

            owner = await classify(speaker1_b)
            guest = await classify(speaker2_a)

    assert enrolled.json()["status"] == "shadow"
    assert activation_rejected.status_code == 409
    assert "anti-spoof assessment is unavailable" in activation_rejected.text
    assert owner.status_code == guest.status_code == 200
    assert owner.json()["classification"] == "uncertain"
    assert owner.json()["reason_code"] == "shadow_owner_candidate"
    assert owner.json()["score"] >= 0.78
    assert owner.json()["permissions"]["read_private_memory"] is False
    assert guest.json()["classification"] == "uncertain"
    assert guest.json()["reason_code"] == "shadow_guest_candidate"
    assert guest.json()["score"] <= 0.45
    assert guest.json()["permissions"]["write_long_term_memory"] is False
