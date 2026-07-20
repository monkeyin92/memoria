"""CAM++ ONNX inference and bounded acoustic-quality observations."""

from __future__ import annotations

import hashlib
import importlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class SpeakerModelEmbedding:
    vector: tuple[float, ...]
    speech_ms: int
    snr_db: float
    quality_score: float
    replay_risk: float
    synthetic_risk: float
    risk_assessment: Literal["verified", "unavailable"] = "unavailable"

    def __post_init__(self) -> None:
        if len(self.vector) != 192 or not all(math.isfinite(value) for value in self.vector):
            raise ValueError("CAM++ must return 192 finite embedding values")
        if self.speech_ms < 0 or not math.isfinite(self.snr_db):
            raise ValueError("speaker acoustic metrics are invalid")
        if not all(
            math.isfinite(value) and 0 <= value <= 1
            for value in (self.quality_score, self.replay_risk, self.synthetic_risk)
        ):
            raise ValueError("speaker quality and risk values must be between zero and one")
        if self.risk_assessment not in {"verified", "unavailable"}:
            raise ValueError("speaker risk assessment state is invalid")


class SpeakerModelEngine(Protocol):
    model_version: str

    def embed(self, pcm: bytes, *, sample_rate: int) -> SpeakerModelEmbedding: ...


class CampPlusOnnxEngine:
    """Run the official 192-dimensional CAM++ model on 80-bin Kaldi fbank."""

    def __init__(
        self,
        model_path: Path,
        *,
        model_version: str,
        session: Any | None = None,
        sha256_path: Path | None = None,
    ) -> None:
        if not model_version.strip():
            raise ValueError("speaker model version is required")
        if sha256_path is not None:
            self._verify_sha256(model_path, sha256_path)
        if session is None:
            if not model_path.is_file():
                raise FileNotFoundError(f"speaker ONNX model not found: {model_path}")
            onnxruntime = importlib.import_module("onnxruntime")
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            session = onnxruntime.InferenceSession(
                str(model_path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            inputs = session.get_inputs()
            outputs = session.get_outputs()
            if (
                len(inputs) != 1
                or inputs[0].name != "feature"
                or inputs[0].type != "tensor(float)"
                or len(inputs[0].shape) != 3
                or inputs[0].shape[2] != 80
                or len(outputs) != 1
                or outputs[0].name != "embedding"
                or outputs[0].type != "tensor(float)"
                or len(outputs[0].shape) != 2
                or outputs[0].shape[1] != 192
            ):
                raise ValueError("CAM++ ONNX input/output contract is invalid")
        self.model_version = model_version
        self._session = session

    @staticmethod
    def _verify_sha256(model_path: Path, sha256_path: Path) -> None:
        if not model_path.is_file() or not sha256_path.is_file():
            raise FileNotFoundError("speaker ONNX model or digest sidecar is missing")
        expected = sha256_path.read_text(encoding="ascii").strip()
        if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
            raise ValueError("speaker ONNX digest sidecar is invalid")
        digest = hashlib.sha256()
        with model_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("speaker ONNX model digest does not match sidecar")

    def embed(self, pcm: bytes, *, sample_rate: int) -> SpeakerModelEmbedding:
        waveform = self._decode_and_resample(pcm, sample_rate=sample_rate)
        features = self._fbank(waveform)
        raw_outputs: list[Any] = self._session.run(
            ["embedding"],
            {"feature": features[np.newaxis, :, :]},
        )
        if len(raw_outputs) != 1:
            raise ValueError("CAM++ returned an unexpected output count")
        vector = np.asarray(raw_outputs[0], dtype=np.float32).reshape(-1)
        if vector.size != 192 or not np.all(np.isfinite(vector)):
            raise ValueError("CAM++ returned an invalid embedding")
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-12:
            raise ValueError("CAM++ returned a zero embedding")
        vector /= norm
        speech_ms, snr_db, quality, replay_risk, synthetic_risk = self._acoustic_metrics(
            waveform
        )
        return SpeakerModelEmbedding(
            vector=tuple(float(value) for value in vector),
            speech_ms=speech_ms,
            snr_db=snr_db,
            quality_score=quality,
            replay_risk=replay_risk,
            synthetic_risk=synthetic_risk,
            risk_assessment="unavailable",
        )

    @staticmethod
    def _decode_and_resample(pcm: bytes, *, sample_rate: int) -> NDArray[np.float32]:
        if not pcm or len(pcm) % 2 or sample_rate != 16_000:
            raise ValueError("speaker PCM must be even-sized 16 kHz s16le")
        waveform = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32_768.0
        return waveform

    @staticmethod
    def _fbank(waveform: NDArray[np.float32]) -> NDArray[np.float32]:
        kaldi = importlib.import_module("kaldi_native_fbank")
        options = kaldi.FbankOptions()
        options.frame_opts.samp_freq = 16_000
        options.frame_opts.dither = 0.0
        options.mel_opts.num_bins = 80
        extractor = kaldi.OnlineFbank(options)
        extractor.accept_waveform(16_000, waveform.tolist())
        extractor.input_finished()
        if extractor.num_frames_ready < 2:
            raise ValueError("speaker PCM is too short for CAM++")
        features = np.stack(
            [extractor.get_frame(index) for index in range(extractor.num_frames_ready)]
        ).astype(np.float32)
        features -= np.mean(features, axis=0, keepdims=True)
        return features

    @staticmethod
    def _acoustic_metrics(
        waveform: NDArray[np.float32],
    ) -> tuple[int, float, float, float, float]:
        frame_size = 400
        frame_shift = 160
        if waveform.size < frame_size:
            return (0, 0.0, 0.0, 1.0, 1.0)
        frame_count = 1 + (waveform.size - frame_size) // frame_shift
        frames = np.lib.stride_tricks.sliding_window_view(waveform, frame_size)[
            ::frame_shift
        ][:frame_count]
        rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-12)
        db = 20.0 * np.log10(rms)
        noise_db = float(np.percentile(db, 20))
        threshold = max(-50.0, noise_db + 6.0)
        voiced = db >= threshold
        if not np.any(voiced) and float(np.percentile(db, 80)) > -45.0:
            voiced = db >= noise_db
        speech_ms = int(np.count_nonzero(voiced) * 10)
        signal_db = float(np.percentile(db[voiced], 70)) if np.any(voiced) else noise_db
        snr_db = float(np.clip(signal_db - noise_db, 0.0, 60.0))
        clipping_ratio = float(np.mean(np.abs(waveform) >= 0.995))
        duration_score = min(speech_ms / 2_000.0, 1.0)
        snr_score = min(snr_db / 20.0, 1.0)
        clipping_score = max(0.0, 1.0 - clipping_ratio * 20.0)
        quality = float(np.clip(0.45 * duration_score + 0.35 * snr_score + 0.2 * clipping_score, 0, 1))

        # CAM++ has no anti-spoof head. The sentinel risks remain fail-closed and the
        # explicit unavailable state prevents these values from being treated as evidence.
        return speech_ms, snr_db, quality, 1.0, 1.0
