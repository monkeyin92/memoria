from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path

import numpy as np
import pytest
from services.agent.src.voice_core.deep_denoiser import (
    _MODEL_SHA256,
    _MODEL_SOURCE_COMMIT,
    DeepDenoiser,
    DeepDenoiserConfig,
)


def _sine_frame(*, frame_index: int, samples: int = 320) -> bytes:
    values = [
        int(12_000 * math.sin(2 * math.pi * 440 * (frame_index * samples + index) / 16_000))
        for index in range(samples)
    ]
    return struct.pack(f"<{len(values)}h", *values)


def test_pinned_dtln_models_have_expected_provenance_and_contract() -> None:
    model_dir = Path(__file__).resolve().parents[2] / "models" / "dtln"
    provenance = json.loads((model_dir / "provenance.json").read_text(encoding="utf-8"))
    assert _MODEL_SOURCE_COMMIT == "1de1f15a8b5b7e1c44905618ff2ef70ca8277fbc"
    assert provenance == {
        "schema_version": 1,
        "upstream": "https://github.com/breizhn/DTLN",
        "commit": _MODEL_SOURCE_COMMIT,
        "license": "MIT",
        "license_file": "LICENSE",
        "sample_rate_hz": 16_000,
        "channels": 1,
        "block_samples": 512,
        "shift_samples": 128,
        "state_scope": "per_media_session",
        "models": {
            name: {
                "upstream_path": f"pretrained_model/{name}",
                "sha256": digest,
            }
            for name, digest in _MODEL_SHA256.items()
        },
    }
    assert (model_dir / provenance["license_file"]).is_file()
    for name, expected in _MODEL_SHA256.items():
        with (model_dir / name).open("rb") as model_file:
            assert hashlib.file_digest(model_file, "sha256").hexdigest() == expected

    denoiser = DeepDenoiser()
    assert denoiser._model_available


def test_dtln_stream_preserves_pcm_length_and_reset_is_session_local() -> None:
    first = DeepDenoiser()
    second = DeepDenoiser()
    frames = [_sine_frame(frame_index=index) for index in range(12)]

    first_output = b"".join(first.process(frame) for frame in frames)
    second_output = b"".join(second.process(frame) for frame in frames)
    assert len(first_output) == sum(map(len, frames))
    assert first_output == second_output
    assert any(first_output)

    first.reset()
    assert b"".join(first.process(frame) for frame in frames) == first_output


def test_dtln_applies_bounded_six_db_output_makeup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denoiser = DeepDenoiser.__new__(DeepDenoiser)
    denoiser.config = DeepDenoiserConfig()
    denoiser._model_available = True
    denoiser._pending_input = np.empty(0, dtype=np.float32)
    denoiser._ready_output = np.empty(0, dtype=np.float32)
    monkeypatch.setattr(denoiser, "_process_shift", lambda shift: shift)
    samples = np.zeros(128, dtype="<i2")
    samples[:4] = (1000, -1000, 20_000, -20_000)

    output = np.frombuffer(denoiser.process(samples.tobytes()), dtype="<i2")

    assert output[:4].tolist() == [2000, -2000, 32767, -32768]


def test_required_dtln_fails_closed_when_models_are_missing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="required DTLN model unavailable"):
        DeepDenoiser(DeepDenoiserConfig(model_dir=tmp_path))

    optional = DeepDenoiser(
        DeepDenoiserConfig(model_dir=tmp_path, required=False)
    )
    pcm = _sine_frame(frame_index=0)
    assert optional.process(pcm) == pcm
