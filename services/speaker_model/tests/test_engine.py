from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray
from services.speaker_model.engine import CampPlusOnnxEngine


class FakeOnnxSession:
    def __init__(self, output: NDArray[np.float32]) -> None:
        self.output = output
        self.input_shape: tuple[int, ...] | None = None

    def run(
        self,
        output_names: list[str],
        inputs: dict[str, NDArray[np.float32]],
    ) -> list[Any]:
        assert output_names == ["embedding"]
        self.input_shape = inputs["feature"].shape
        return [self.output]


class DeterministicCampPlusEngine(CampPlusOnnxEngine):
    @staticmethod
    def _decode_and_resample(pcm: bytes, *, sample_rate: int) -> NDArray[np.float32]:
        _ = (pcm, sample_rate)
        timeline = np.arange(32_000, dtype=np.float32) / 16_000
        return (0.2 * np.sin(2 * np.pi * 220 * timeline)).astype(np.float32)

    @staticmethod
    def _fbank(waveform: NDArray[np.float32]) -> NDArray[np.float32]:
        _ = waveform
        return np.ones((198, 80), dtype=np.float32)


def test_campplus_engine_normalizes_192_dimension_output() -> None:
    output = np.zeros((1, 192), dtype=np.float32)
    output[0, :2] = (3.0, 4.0)
    session = FakeOnnxSession(output)
    engine = DeterministicCampPlusEngine(
        Path("unused.onnx"),
        model_version="campplus-cn-common-test",
        session=session,
    )

    result = engine.embed(b"ignored", sample_rate=16_000)

    assert result.vector[:2] == pytest.approx((0.6, 0.8))
    assert result.vector[2:] == pytest.approx((0.0,) * 190)
    assert session.input_shape == (1, 198, 80)
    assert result.speech_ms >= 800
    assert 0 <= result.quality_score <= 1
    assert result.replay_risk == result.synthetic_risk == 1.0
    assert result.risk_assessment == "unavailable"


def test_campplus_engine_rejects_wrong_embedding_dimension() -> None:
    engine = DeterministicCampPlusEngine(
        Path("unused.onnx"),
        model_version="campplus-cn-common-test",
        session=FakeOnnxSession(np.ones((1, 191), dtype=np.float32)),
    )

    with pytest.raises(ValueError, match="invalid embedding"):
        engine.embed(b"ignored", sample_rate=16_000)


def test_campplus_engine_rejects_model_digest_mismatch(tmp_path: Path) -> None:
    model = tmp_path / "campplus.onnx"
    digest = tmp_path / "campplus.onnx.sha256"
    model.write_bytes(b"candidate-model")
    digest.write_text(hashlib.sha256(b"different-model").hexdigest(), encoding="ascii")

    with pytest.raises(ValueError, match="digest does not match"):
        CampPlusOnnxEngine(
            model,
            model_version="campplus-cn-common-test",
            session=FakeOnnxSession(np.ones((1, 192), dtype=np.float32)),
            sha256_path=digest,
        )
