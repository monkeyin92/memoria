"""Pinned stateful DTLN noise suppression for 16 kHz mono PCM."""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
from numpy.typing import NDArray

logger = logging.getLogger(__name__)

_MODEL_SOURCE_COMMIT = "1de1f15a8b5b7e1c44905618ff2ef70ca8277fbc"
_MODEL_SHA256 = {
    "model_1.onnx": "22b91cae3855e5a0620e66a917ca6c82c58db0e842c770f58d86751c5e8d4ae3",
    "model_2.onnx": "e20c92f9233fccf29cddf86970d0d0161a03aebccc26d6f4d5639c4d5ec2e639",
}
_BLOCK_LEN = 512
_BLOCK_SHIFT = 128
_INITIAL_OUTPUT_DELAY = _BLOCK_SHIFT - 1
# The board microphone is pinned at 18 dB to keep its raw noise floor below
# the DTLN input. Restore 12 dB after suppression so FunASR sees enough
# energy at normal speaking distance, with the PCM conversion below providing
# a hard saturation fence.
_OUTPUT_MAKEUP_GAIN = 4.0

FloatArray = NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class DeepDenoiserConfig:
    """DTLN runtime configuration."""

    sample_rate: int = 16_000
    enabled: bool = True
    use_gpu: bool = False
    model_dir: Path | None = None
    required: bool = True

    def __post_init__(self) -> None:
        if self.sample_rate != 16_000:
            raise ValueError("the pinned DTLN model requires 16 kHz audio")


class DeepDenoiser:
    """One session's DTLN streaming state over shared ONNX sessions."""

    _session_cache: ClassVar[dict[tuple[Path, bool], tuple[Any, Any]]] = {}
    _session_cache_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, config: DeepDenoiserConfig | None = None) -> None:
        self.config = config or DeepDenoiserConfig()
        self._model_available = False
        self._model_1: Any = None
        self._model_2: Any = None
        self._input_names_1: tuple[str, str] = ("", "")
        self._input_names_2: tuple[str, str] = ("", "")
        self._states_1 = np.zeros((1, 2, 128, 2), dtype=np.float32)
        self._states_2 = np.zeros((1, 2, 128, 2), dtype=np.float32)
        self._in_buffer = np.zeros(_BLOCK_LEN, dtype=np.float32)
        self._out_buffer = np.zeros(_BLOCK_LEN, dtype=np.float32)
        self._pending_input = np.empty(0, dtype=np.float32)
        self._ready_output = np.zeros(_INITIAL_OUTPUT_DELAY, dtype=np.float32)

        if self.config.enabled:
            self._initialize_models()

    @property
    def model_dir(self) -> Path:
        if self.config.model_dir is not None:
            return self.config.model_dir.resolve()
        return Path(__file__).resolve().parents[2] / "models" / "dtln"

    def _initialize_models(self) -> None:
        try:
            model_1, model_2 = self._load_shared_sessions(self.model_dir, self.config.use_gpu)
            names_1 = tuple(item.name for item in model_1.get_inputs())
            names_2 = tuple(item.name for item in model_2.get_inputs())
            if len(names_1) != 2 or len(names_2) != 2:
                raise RuntimeError("DTLN models must expose audio and recurrent-state inputs")
            self._model_1 = model_1
            self._model_2 = model_2
            self._input_names_1 = (names_1[0], names_1[1])
            self._input_names_2 = (names_2[0], names_2[1])
            self._model_available = True
            logger.info(
                "DTLN initialized source_commit=%s model_dir=%s gpu=%s",
                _MODEL_SOURCE_COMMIT,
                self.model_dir,
                self.config.use_gpu,
            )
        except Exception as exc:
            if self.config.required:
                raise RuntimeError(f"required DTLN model unavailable: {exc}") from exc
            logger.warning("optional DTLN model unavailable: %s", exc)

    @classmethod
    def _load_shared_sessions(cls, model_dir: Path, use_gpu: bool) -> tuple[Any, Any]:
        key = (model_dir, use_gpu)
        with cls._session_cache_lock:
            cached = cls._session_cache.get(key)
            if cached is not None:
                return cached

            model_paths = tuple(model_dir / name for name in _MODEL_SHA256)
            for path in model_paths:
                if not path.is_file():
                    raise FileNotFoundError(path)
                with path.open("rb") as model_file:
                    digest = hashlib.file_digest(model_file, "sha256").hexdigest()
                if digest != _MODEL_SHA256[path.name]:
                    raise RuntimeError(f"DTLN checksum mismatch: {path.name}")

            import onnxruntime as ort  # type: ignore[import-untyped]

            providers = (
                ["CUDAExecutionProvider", "CPUExecutionProvider"]
                if use_gpu
                else ["CPUExecutionProvider"]
            )
            model_1 = ort.InferenceSession(str(model_paths[0]), providers=providers)
            model_2 = ort.InferenceSession(str(model_paths[1]), providers=providers)
            cls._validate_model_contract(model_1, model_2)
            cls._session_cache[key] = (model_1, model_2)
            return model_1, model_2

    @staticmethod
    def _validate_model_contract(model_1: Any, model_2: Any) -> None:
        expected = (
            ((1, 1, 257), (1, 2, 128, 2), (1, 1, 257), (1, 2, 128, 2)),
            ((1, 1, 512), (1, 2, 128, 2), (1, 1, 512), (1, 2, 128, 2)),
        )
        for model, contract in zip((model_1, model_2), expected, strict=True):
            actual = tuple(
                tuple(item.shape) for item in (*model.get_inputs(), *model.get_outputs())
            )
            if actual != contract:
                raise RuntimeError(f"unexpected DTLN ONNX contract: {actual!r}")

    def process(self, audio_bytes: bytes) -> bytes:
        """Denoise arbitrary-length PCM while preserving its byte length."""

        if not self.config.enabled or not self._model_available:
            return audio_bytes
        if len(audio_bytes) % 2:
            raise ValueError("audio must be 16-bit PCM")

        samples = np.frombuffer(audio_bytes, dtype="<i2").astype(np.float32) / 32768.0
        self._pending_input = np.concatenate((self._pending_input, samples))
        produced: list[FloatArray] = []
        while self._pending_input.size >= _BLOCK_SHIFT:
            shift = self._pending_input[:_BLOCK_SHIFT]
            self._pending_input = self._pending_input[_BLOCK_SHIFT:]
            produced.append(self._process_shift(shift))
        if produced:
            self._ready_output = np.concatenate((self._ready_output, *produced))

        requested = samples.size
        if self._ready_output.size < requested:
            raise RuntimeError("DTLN output buffer lost its fixed latency invariant")
        output = self._ready_output[:requested]
        self._ready_output = self._ready_output[requested:]
        pcm = np.clip(
            output * (32768.0 * _OUTPUT_MAKEUP_GAIN),
            -32768,
            32767,
        ).astype("<i2")
        return bytes(pcm.tobytes())

    def _process_shift(self, shift: FloatArray) -> FloatArray:
        self._in_buffer[:-_BLOCK_SHIFT] = self._in_buffer[_BLOCK_SHIFT:]
        self._in_buffer[-_BLOCK_SHIFT:] = shift
        spectrum = np.fft.rfft(self._in_buffer)
        magnitude = np.abs(spectrum).reshape(1, 1, -1).astype(np.float32)
        phase = np.angle(spectrum)

        output_1 = self._model_1.run(
            None,
            {
                self._input_names_1[0]: magnitude,
                self._input_names_1[1]: self._states_1,
            },
        )
        self._states_1 = np.asarray(output_1[1], dtype=np.float32)
        estimated = magnitude * np.asarray(output_1[0]) * np.exp(1j * phase)
        block = np.fft.irfft(estimated).reshape(1, 1, -1).astype(np.float32)

        output_2 = self._model_2.run(
            None,
            {
                self._input_names_2[0]: block,
                self._input_names_2[1]: self._states_2,
            },
        )
        self._states_2 = np.asarray(output_2[1], dtype=np.float32)
        self._out_buffer[:-_BLOCK_SHIFT] = self._out_buffer[_BLOCK_SHIFT:]
        self._out_buffer[-_BLOCK_SHIFT:] = 0
        self._out_buffer += np.asarray(output_2[0], dtype=np.float32).reshape(-1)
        return self._out_buffer[:_BLOCK_SHIFT].copy()

    def reset(self) -> None:
        """Reset only this media session's recurrent and overlap state."""

        self._states_1.fill(0)
        self._states_2.fill(0)
        self._in_buffer.fill(0)
        self._out_buffer.fill(0)
        self._pending_input = np.empty(0, dtype=np.float32)
        self._ready_output = np.zeros(_INITIAL_OUTPUT_DELAY, dtype=np.float32)


__all__ = [
    "DeepDenoiser",
    "DeepDenoiserConfig",
    "_MODEL_SHA256",
    "_MODEL_SOURCE_COMMIT",
]
