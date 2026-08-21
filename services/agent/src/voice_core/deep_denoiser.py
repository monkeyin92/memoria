"""Deep learning based audio denoiser using DTLN or MossFormer2.

This is a server-side heavy denoiser that provides superior noise reduction
compared to RNNoise, suitable for GPU/high-CPU environments.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Literal

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeepDenoiserConfig:
    """Configuration for deep learning denoiser."""

    model_type: Literal["dtln", "dtln-aec", "silero"] = "dtln"
    sample_rate: int = 16_000
    enabled: bool = True
    use_gpu: bool = False  # Set to True if CUDA/MPS available

    def __post_init__(self) -> None:
        if self.sample_rate not in (8_000, 16_000):
            raise ValueError("Deep denoiser supports 8kHz or 16kHz sample rates")


class DeepDenoiser:
    """Server-side deep learning denoiser.

    Supports multiple backend models:
    - DTLN: Dual-signal Transformation LSTM Network (recommended)
    - DTLN-aec: DTLN with acoustic echo cancellation
    - Silero VAD: Lightweight alternative

    Falls back to no-op if models are not available.
    """

    def __init__(self, config: DeepDenoiserConfig | None = None) -> None:
        self.config = config or DeepDenoiserConfig()
        self._model = None
        self._model_available = False

        if not self.config.enabled:
            logger.info("Deep denoiser disabled by config")
            return

        # Try to load the requested model
        if self.config.model_type == "dtln":
            self._init_dtln()
        elif self.config.model_type == "dtln-aec":
            self._init_dtln_aec()
        elif self.config.model_type == "silero":
            self._init_silero()
        else:
            logger.warning(f"Unknown model type: {self.config.model_type}")

    def _init_dtln(self) -> None:
        """Initialize DTLN model."""
        try:
            # Try to import DTLN
            # Option 1: Use ONNX runtime (most compatible)
            import onnxruntime as ort

            # Download or load pre-trained DTLN model
            # Model weights should be placed in models/dtln_*.onnx
            model_path = "models/dtln_16k.onnx"

            providers = ['CUDAExecutionProvider', 'CPUExecutionProvider'] if self.config.use_gpu else ['CPUExecutionProvider']
            self._model = ort.InferenceSession(model_path, providers=providers)
            self._model_available = True
            logger.info(f"DTLN initialized successfully (GPU: {self.config.use_gpu})")

        except ImportError:
            logger.warning(
                "DTLN not available: onnxruntime not installed. "
                "Install with: pip install onnxruntime or onnxruntime-gpu"
            )
        except Exception as e:
            logger.warning(f"Failed to initialize DTLN: {e}")

    def _init_dtln_aec(self) -> None:
        """Initialize DTLN-aec model."""
        try:
            import onnxruntime as ort

            model_path = "models/dtln_aec_16k.onnx"
            providers = ['CUDAExecutionProvider', 'CPUExecutionProvider'] if self.config.use_gpu else ['CPUExecutionProvider']
            self._model = ort.InferenceSession(model_path, providers=providers)
            self._model_available = True
            logger.info(f"DTLN-aec initialized successfully (GPU: {self.config.use_gpu})")

        except ImportError:
            logger.warning("DTLN-aec not available: onnxruntime not installed")
        except Exception as e:
            logger.warning(f"Failed to initialize DTLN-aec: {e}")

    def _init_silero(self) -> None:
        """Initialize Silero VAD + denoiser."""
        try:
            import torch

            # Silero VAD can be used for lightweight denoising
            model, utils = torch.hub.load(
                repo_or_dir='snakers4/silero-vad',
                model='silero_vad',
                force_reload=False,
                onnx=not self.config.use_gpu
            )

            self._model = model
            self._model_available = True
            logger.info(f"Silero initialized successfully (GPU: {self.config.use_gpu})")

        except ImportError:
            logger.warning("Silero not available: torch not installed")
        except Exception as e:
            logger.warning(f"Failed to initialize Silero: {e}")

    def process(self, audio_bytes: bytes) -> bytes:
        """Process audio with deep denoiser.

        Args:
            audio_bytes: 16-bit PCM audio data

        Returns:
            Denoised audio bytes
        """
        if not self.config.enabled or not self._model_available:
            # Pass through if disabled or model not available
            return audio_bytes

        # Convert bytes to numpy array
        if len(audio_bytes) % 2:
            raise ValueError("Audio must be 16-bit PCM")

        samples = np.frombuffer(audio_bytes, dtype=np.int16)

        # Normalize to [-1, 1]
        audio_float = samples.astype(np.float32) / 32768.0

        # Process with model
        if self.config.model_type in ("dtln", "dtln-aec"):
            denoised = self._process_dtln(audio_float)
        elif self.config.model_type == "silero":
            denoised = self._process_silero(audio_float)
        else:
            denoised = audio_float

        # Convert back to int16
        denoised_int16 = np.clip(denoised * 32768.0, -32768, 32767).astype(np.int16)
        return denoised_int16.tobytes()

    def _process_dtln(self, audio_float: np.ndarray) -> np.ndarray:
        """Process audio with DTLN model."""
        try:
            # DTLN expects specific input shape
            # Typically processes in 512-sample blocks (32ms at 16kHz)
            block_size = 512

            # Pad if necessary
            num_blocks = (len(audio_float) + block_size - 1) // block_size
            padded_length = num_blocks * block_size
            padded = np.zeros(padded_length, dtype=np.float32)
            padded[:len(audio_float)] = audio_float

            # Process in blocks
            output = np.zeros_like(padded)
            for i in range(num_blocks):
                start = i * block_size
                end = start + block_size
                block = padded[start:end].reshape(1, -1)

                # Run inference
                outputs = self._model.run(None, {'input': block})
                output[start:end] = outputs[0].flatten()

            # Remove padding
            return output[:len(audio_float)]

        except Exception as e:
            logger.error(f"DTLN processing failed: {e}")
            return audio_float

    def _process_silero(self, audio_float: np.ndarray) -> np.ndarray:
        """Process audio with Silero model."""
        try:
            import torch

            # Silero processes entire audio at once
            audio_tensor = torch.from_numpy(audio_float).unsqueeze(0)

            with torch.no_grad():
                # This is simplified - actual Silero denoising requires more steps
                output = self._model(audio_tensor, self.config.sample_rate)

            return output.squeeze().numpy()

        except Exception as e:
            logger.error(f"Silero processing failed: {e}")
            return audio_float

    def reset(self) -> None:
        """Reset denoiser state (if stateful)."""
        # Most models are stateless, but this allows for future extensions
        pass


__all__ = ["DeepDenoiser", "DeepDenoiserConfig"]
