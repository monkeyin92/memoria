"""Two-stage denoising pipeline: lightweight + deep learning.

Stage 1: RNNoise for fast, CPU-friendly basic denoising
Stage 2: DTLN/MossFormer2 for deep, GPU-accelerated advanced denoising

This provides the best balance of latency, quality, and resource usage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from services.agent.src.voice_core.audio_denoiser import AudioDenoiser, DenoiserConfig
from services.agent.src.voice_core.deep_denoiser import DeepDenoiser, DeepDenoiserConfig

logger = logging.getLogger(__name__)

DenoisingStats = dict[str, bool | int | float]


@dataclass(frozen=True, slots=True)
class TwoStageDenoisingConfig:
    """Configuration for two-stage denoising pipeline."""

    # Stage 1: Lightweight denoiser (RNNoise/spectral)
    stage1_enabled: bool = True
    stage1_config: DenoiserConfig | None = None

    # Stage 2: Deep denoiser (DTLN/MossFormer2)
    stage2_enabled: bool = True
    stage2_config: DeepDenoiserConfig | None = None

    # Performance options
    skip_stage2_on_silence: bool = True  # Skip deep denoiser if VAD says silence
    vad_threshold: float = 0.3  # Threshold for skipping stage 2


class TwoStageDenoiser:
    """Two-stage denoising pipeline for optimal quality and performance.

    Architecture:
        Audio → Stage 1 (RNNoise) → Stage 2 (DTLN) → Clean Audio
                     ↓ VAD prob
                Skip stage 2 if silence

    Benefits:
    - Stage 1 removes most noise quickly (<5ms, <5% CPU)
    - Stage 2 deep cleans only when speech detected (<50ms, GPU/CPU)
    - Total latency: 5-55ms depending on speech presence
    - Quality: State-of-the-art when both stages active
    """

    def __init__(self, config: TwoStageDenoisingConfig | None = None) -> None:
        self.config = config or TwoStageDenoisingConfig()

        # Initialize stage 1: lightweight denoiser
        stage1_config = self.config.stage1_config or DenoiserConfig(
            enabled=self.config.stage1_enabled,
            sample_rate=16_000,
        )
        self._stage1 = AudioDenoiser(stage1_config)

        # Initialize stage 2: deep denoiser
        stage2_config = self.config.stage2_config or DeepDenoiserConfig(
            enabled=self.config.stage2_enabled,
            sample_rate=16_000,
            use_gpu=False,  # Set to True if GPU available
        )
        self._stage2 = DeepDenoiser(stage2_config)

        # Statistics
        self._total_frames = 0
        self._stage2_skipped = 0
        self._stage2_processed = 0

        logger.info(
            f"Two-stage denoiser initialized: "
            f"Stage1={'enabled' if self.config.stage1_enabled else 'disabled'}, "
            f"Stage2={'enabled' if self.config.stage2_enabled else 'disabled'}"
        )

    def process(self, audio_bytes: bytes) -> tuple[bytes, float, DenoisingStats]:
        """Process audio through two-stage denoising pipeline.

        Args:
            audio_bytes: 16-bit PCM audio data

        Returns:
            (denoised_audio_bytes, vad_probability, stats)
        """
        self._total_frames += 1

        # Stage 1: Lightweight denoising (always run if enabled)
        stage1_output, vad_prob = self._stage1.process(audio_bytes)

        # Decide whether to run stage 2
        run_stage2 = (
            self.config.stage2_enabled
            and (not self.config.skip_stage2_on_silence or vad_prob >= self.config.vad_threshold)
        )

        if run_stage2:
            # Stage 2: Deep denoising
            stage2_output = self._stage2.process(stage1_output)
            self._stage2_processed += 1
            final_output = stage2_output
        else:
            # Skip stage 2
            self._stage2_skipped += 1
            final_output = stage1_output

        # Gather statistics
        stats = {
            "stage1_applied": self.config.stage1_enabled,
            "stage2_applied": run_stage2,
            "vad_prob": vad_prob,
            "total_frames": self._total_frames,
            "stage2_processed": self._stage2_processed,
            "stage2_skipped": self._stage2_skipped,
            "stage2_skip_rate": self._stage2_skipped / self._total_frames if self._total_frames > 0 else 0.0,
        }

        return final_output, vad_prob, stats

    def reset(self) -> None:
        """Reset both denoiser stages."""
        self._stage1.reset()
        self._stage2.reset()
        self._total_frames = 0
        self._stage2_skipped = 0
        self._stage2_processed = 0

    def get_stats(self) -> DenoisingStats:
        """Get pipeline statistics."""
        return {
            "total_frames": self._total_frames,
            "stage2_processed": self._stage2_processed,
            "stage2_skipped": self._stage2_skipped,
            "stage2_skip_rate": self._stage2_skipped / self._total_frames if self._total_frames > 0 else 0.0,
            "stage1_available": self._stage1._rnnoise_available,
            "stage2_available": self._stage2._model_available,
        }


__all__ = ["TwoStageDenoiser", "TwoStageDenoisingConfig"]
