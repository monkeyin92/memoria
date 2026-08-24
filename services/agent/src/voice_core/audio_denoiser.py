"""Lightweight audio denoiser for real-time voice processing.

Uses RNNoise for CPU-friendly noise suppression before ASR.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DenoiserConfig:
    """Configuration for audio denoiser."""

    sample_rate: int = 16_000
    frame_size: int = 320  # 20ms at 16kHz, RNNoise expects 10ms frames (160 samples)
    enabled: bool = True
    vad_threshold: float = 0.5  # RNNoise also provides VAD probability

    def __post_init__(self) -> None:
        if self.sample_rate not in (16_000, 48_000):
            raise ValueError("RNNoise supports 16kHz or 48kHz sample rates")
        if self.frame_size <= 0:
            raise ValueError("frame_size must be positive")


class AudioDenoiser:
    """Lightweight denoiser using spectral subtraction or RNNoise.

    This implementation provides a fallback spectral subtraction method
    until RNNoise bindings are integrated.
    """

    def __init__(self, config: DenoiserConfig | None = None) -> None:
        self.config = config or DenoiserConfig()
        self._noise_profile: list[float] | None = None
        self._frame_count = 0
        self._adaptation_frames = 10  # Adapt noise profile for first N frames
        self._rnnoise_available = False

        if not self.config.enabled:
            return

        # Try to import rnnoise if available
        try:
            import rnnoise  # type: ignore
            self._rnnoise_state = rnnoise.RNNoise()
            self._rnnoise_available = True
            logger.info("RNNoise initialized successfully")
        except ImportError:
            logger.warning(
                "RNNoise not available, using fallback spectral subtraction. "
                "Install rnnoise-python for better performance: pip install rnnoise-python"
            )

    def process(self, audio_bytes: bytes) -> tuple[bytes, float]:
        """Process audio frame and return denoised audio + VAD probability.

        Args:
            audio_bytes: 16-bit PCM audio data

        Returns:
            (denoised_audio_bytes, vad_probability)
        """
        if not self.config.enabled:
            return audio_bytes, 1.0

        # Convert bytes to samples
        if len(audio_bytes) % 2:
            raise ValueError("Audio must be 16-bit PCM")

        samples = struct.unpack(f"<{len(audio_bytes) // 2}h", audio_bytes)

        if self._rnnoise_available:
            return self._process_rnnoise(samples)
        else:
            return self._process_spectral_subtraction(samples)

    def _process_rnnoise(self, samples: tuple[int, ...]) -> tuple[bytes, float]:
        """Process using RNNoise library."""
        # RNNoise expects float32 input in [-1, 1] range
        float_samples = [s / 32768.0 for s in samples]

        # Process in 10ms chunks (160 samples at 16kHz)
        chunk_size = 160
        denoised = []
        vad_probs = []

        for i in range(0, len(float_samples), chunk_size):
            chunk = float_samples[i:i + chunk_size]
            if len(chunk) < chunk_size:
                # Pad last chunk
                chunk.extend([0.0] * (chunk_size - len(chunk)))

            # RNNoise returns (denoised_chunk, vad_prob)
            denoised_chunk, vad_prob = self._rnnoise_state.process_frame(chunk)
            denoised.extend(denoised_chunk)
            vad_probs.append(vad_prob)

        # Convert back to int16
        int_samples = [int(max(-32768, min(32767, s * 32768))) for s in denoised[:len(samples)]]
        denoised_bytes = struct.pack(f"<{len(int_samples)}h", *int_samples)
        avg_vad = sum(vad_probs) / len(vad_probs) if vad_probs else 0.0

        return denoised_bytes, avg_vad

    def _process_spectral_subtraction(self, samples: tuple[int, ...]) -> tuple[bytes, float]:
        """Fallback: simple spectral subtraction-based noise reduction."""
        # Calculate RMS energy
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5

        # Estimate noise floor during first few frames
        if self._frame_count < self._adaptation_frames:
            if self._noise_profile is None:
                self._noise_profile = list(samples)
            else:
                # Running average of noise
                alpha = 0.3
                self._noise_profile = [
                    (1 - alpha) * n + alpha * s
                    for n, s in zip(
                        self._noise_profile,
                        samples[: len(self._noise_profile)],
                        strict=True,
                    )
                ]
            self._frame_count += 1

        # Simple noise gate
        if self._noise_profile:
            noise_rms = (sum(n * n for n in self._noise_profile) / len(self._noise_profile)) ** 0.5
            snr = rms / (noise_rms + 1.0)

            if snr < 1.5:  # Low SNR, apply aggressive reduction
                reduction_factor = 0.3
            elif snr < 3.0:  # Medium SNR
                reduction_factor = 0.6
            else:  # High SNR, minimal reduction
                reduction_factor = 0.9

            # Apply reduction
            denoised = [int(s * reduction_factor) for s in samples]
            denoised_bytes = struct.pack(f"<{len(denoised)}h", *denoised)

            # Estimate VAD from SNR
            vad_prob = min(1.0, max(0.0, (snr - 1.0) / 3.0))
            return denoised_bytes, vad_prob

        # No noise profile yet, pass through
        return struct.pack(f"<{len(samples)}h", *samples), 0.5

    def reset(self) -> None:
        """Reset denoiser state."""
        self._noise_profile = None
        self._frame_count = 0
        if self._rnnoise_available:
            import rnnoise
            self._rnnoise_state = rnnoise.RNNoise()


__all__ = ["AudioDenoiser", "DenoiserConfig"]
