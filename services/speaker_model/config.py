"""Runtime configuration for the isolated speaker model service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SpeakerModelSettings:
    token: str
    model_path: Path
    model_version: str
    model_sha256_path: Path | None = None
    max_audio_bytes: int = 4 * 1024 * 1024
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        if len(self.token) < 16:
            raise ValueError("speaker model token must contain at least 16 characters")
        if not self.model_version.strip():
            raise ValueError("speaker model version is required")
        if self.max_audio_bytes < 2 or self.max_concurrency < 1:
            raise ValueError("speaker model limits must be positive")

    @classmethod
    def from_env(cls) -> SpeakerModelSettings:
        model_path = Path(
            os.environ.get(
                "MEMORIA_SPEAKER_MODEL_PATH",
                "/models/campplus_cn_common.onnx",
            )
        )
        sha256_path = os.environ.get(
            "MEMORIA_SPEAKER_MODEL_SHA256_PATH",
            f"{model_path}.sha256",
        ).strip()
        return cls(
            token=os.environ.get("MEMORIA_SPEAKER_MODEL_TOKEN", ""),
            model_path=model_path,
            model_version=os.environ.get(
                "MEMORIA_SPEAKER_MODEL_VERSION",
                "campplus-cn-common@v1.0.0+ckpt.3388cf5f+onnx.7a39d2e5e566+fbank.v1",
            ),
            model_sha256_path=Path(sha256_path) if sha256_path else None,
            max_audio_bytes=int(
                os.environ.get("MEMORIA_SPEAKER_MODEL_MAX_AUDIO_BYTES", 4 * 1024 * 1024)
            ),
            max_concurrency=int(os.environ.get("MEMORIA_SPEAKER_MODEL_MAX_CONCURRENCY", "1")),
        )
