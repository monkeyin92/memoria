from __future__ import annotations

import hashlib
import math
import struct
from pathlib import Path

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
    assert _MODEL_SOURCE_COMMIT == "1de1f15a8b5b7e1c44905618ff2ef70ca8277fbc"
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


def test_required_dtln_fails_closed_when_models_are_missing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="required DTLN model unavailable"):
        DeepDenoiser(DeepDenoiserConfig(model_dir=tmp_path))

    optional = DeepDenoiser(
        DeepDenoiserConfig(model_dir=tmp_path, required=False)
    )
    pcm = _sine_frame(frame_index=0)
    assert optional.process(pcm) == pcm
