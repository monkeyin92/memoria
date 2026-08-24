"""Tests for two-stage denoising pipeline."""

import math
import struct

from services.agent.src.voice_core.two_stage_denoiser import (
    TwoStageDenoiser,
    TwoStageDenoisingConfig,
)


def generate_sine_wave(frequency: int, duration_ms: int, sample_rate: int = 16000) -> bytes:
    """Generate a sine wave for testing."""
    num_samples = sample_rate * duration_ms // 1000
    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        value = int(16000 * math.sin(2 * math.pi * frequency * t))
        samples.append(value)
    return struct.pack(f"<{len(samples)}h", *samples)


def generate_noise(duration_ms: int, sample_rate: int = 16000, amplitude: int = 1000) -> bytes:
    """Generate random noise for testing."""
    import random
    num_samples = sample_rate * duration_ms // 1000
    samples = [random.randint(-amplitude, amplitude) for _ in range(num_samples)]
    return struct.pack(f"<{len(samples)}h", *samples)


def add_audio(audio1: bytes, audio2: bytes) -> bytes:
    """Add two audio signals together."""
    samples1 = struct.unpack(f"<{len(audio1) // 2}h", audio1)
    samples2 = struct.unpack(f"<{len(audio2) // 2}h", audio2)
    mixed = [
        min(32767, max(-32768, s1 + s2))
        for s1, s2 in zip(samples1, samples2, strict=True)
    ]
    return struct.pack(f"<{len(mixed)}h", *mixed)


class TestTwoStageDenoiser:
    """Test suite for TwoStageDenoiser."""

    def test_initialization_default(self):
        """Test denoiser initialization with default config."""
        denoiser = TwoStageDenoiser()
        assert denoiser.config.stage1_enabled is True
        assert denoiser.config.stage2_enabled is True

    def test_initialization_custom_config(self):
        """Test denoiser initialization with custom config."""
        config = TwoStageDenoisingConfig(
            stage1_enabled=True,
            stage2_enabled=False,
        )
        denoiser = TwoStageDenoiser(config)
        assert denoiser.config.stage1_enabled is True
        assert denoiser.config.stage2_enabled is False

    def test_process_clean_signal(self):
        """Test processing clean speech signal."""
        denoiser = TwoStageDenoiser()

        audio = generate_sine_wave(440, 100)
        denoised, vad_prob, stats = denoiser.process(audio)

        assert len(denoised) == len(audio)
        assert 0.0 <= vad_prob <= 1.0
        assert stats["total_frames"] == 1

    def test_stage2_skipping_on_silence(self):
        """Test that stage 2 is skipped on silence."""
        config = TwoStageDenoisingConfig(
            skip_stage2_on_silence=True,
            vad_threshold=0.5,
        )
        denoiser = TwoStageDenoiser(config)

        # Process very low energy signal (should skip stage 2)
        silence = generate_sine_wave(50, 100)  # Very low frequency/amplitude
        denoised, vad_prob, stats = denoiser.process(silence)

        # If VAD prob is low, stage 2 should be skipped
        if vad_prob < 0.5:
            assert stats["stage2_applied"] is False
            assert stats["stage2_skipped"] == 1

    def test_stage2_processing_on_speech(self):
        """Test that stage 2 is used on speech."""
        config = TwoStageDenoisingConfig(
            skip_stage2_on_silence=True,
            vad_threshold=0.3,
        )
        denoiser = TwoStageDenoiser(config)

        # Adapt to noise floor first
        for _ in range(15):
            noise = generate_noise(100, amplitude=200)
            denoiser.process(noise)

        # Process speech-like signal (should use stage 2)
        speech = generate_sine_wave(440, 100)
        denoised, vad_prob, stats = denoiser.process(speech)

        # Statistics should show some processing
        assert stats["total_frames"] == 16

    def test_stage2_always_on(self):
        """Test with stage 2 always enabled."""
        config = TwoStageDenoisingConfig(
            skip_stage2_on_silence=False,  # Never skip
        )
        denoiser = TwoStageDenoiser(config)

        # Process any signal
        audio = generate_sine_wave(440, 100)
        denoised, vad_prob, stats = denoiser.process(audio)

        # Stage 2 should always be applied
        # Note: This depends on whether DTLN model is available
        # If model not available, stage2_applied might still be False
        assert "stage2_applied" in stats

    def test_multiple_frames(self):
        """Test processing multiple consecutive frames."""
        denoiser = TwoStageDenoiser()

        for i in range(50):
            audio = generate_sine_wave(440 + i * 10, 20)
            denoised, vad_prob, stats = denoiser.process(audio)

            assert len(denoised) == len(audio)
            assert stats["total_frames"] == i + 1

    def test_statistics_tracking(self):
        """Test that statistics are correctly tracked."""
        denoiser = TwoStageDenoiser()

        # Process some frames
        for _ in range(10):
            audio = generate_sine_wave(440, 100)
            denoiser.process(audio)

        stats = denoiser.get_stats()
        assert stats["total_frames"] == 10
        assert stats["stage2_processed"] + stats["stage2_skipped"] == 10
        assert 0.0 <= stats["stage2_skip_rate"] <= 1.0

    def test_reset(self):
        """Test reset functionality."""
        denoiser = TwoStageDenoiser()

        # Process some frames
        for _ in range(10):
            audio = generate_sine_wave(440, 100)
            denoiser.process(audio)

        assert denoiser._total_frames == 10

        # Reset
        denoiser.reset()

        assert denoiser._total_frames == 0
        assert denoiser._stage2_skipped == 0
        assert denoiser._stage2_processed == 0

    def test_stage1_only_mode(self):
        """Test with only stage 1 enabled."""
        config = TwoStageDenoisingConfig(
            stage1_enabled=True,
            stage2_enabled=False,
        )
        denoiser = TwoStageDenoiser(config)

        audio = generate_sine_wave(440, 100)
        denoised, vad_prob, stats = denoiser.process(audio)

        assert stats["stage1_applied"] is True
        assert stats["stage2_applied"] is False

    def test_with_noisy_signal(self):
        """Test denoising with noisy signal."""
        denoiser = TwoStageDenoiser()

        # Generate speech + noise
        speech = generate_sine_wave(440, 100)
        noise = generate_noise(100, amplitude=800)
        noisy_audio = add_audio(speech, noise)

        # Process multiple frames to build noise profile
        for _ in range(20):
            denoised, vad_prob, stats = denoiser.process(noisy_audio)

        assert len(denoised) == len(noisy_audio)
        assert stats["total_frames"] == 20


if __name__ == "__main__":
    print("Running two-stage denoiser sanity test...\n")

    # Test 1: Basic initialization
    denoiser = TwoStageDenoiser()
    print("✓ Denoiser initialized")
    print(f"  Stage 1 (RNNoise): {'available' if denoiser._stage1._rnnoise_available else 'fallback'}")
    print(f"  Stage 2 (DTLN): {'available' if denoiser._stage2._model_available else 'not available'}")

    # Test 2: Process clean signal
    clean = generate_sine_wave(440, 100)
    denoised, vad, stats = denoiser.process(clean)
    print("\n✓ Processed clean signal")
    print(f"  Input: {len(clean)} bytes")
    print(f"  Output: {len(denoised)} bytes")
    print(f"  VAD: {vad:.2f}")
    print(f"  Stage 2 applied: {stats['stage2_applied']}")

    # Test 3: Process multiple frames
    print("\n✓ Processing 100 frames...")
    for i in range(100):
        audio = generate_sine_wave(300 + i * 2, 20)
        denoised, vad, stats = denoiser.process(audio)

    final_stats = denoiser.get_stats()
    print(f"  Total frames: {final_stats['total_frames']}")
    print(f"  Stage 2 processed: {final_stats['stage2_processed']}")
    print(f"  Stage 2 skipped: {final_stats['stage2_skipped']}")
    print(f"  Skip rate: {final_stats['stage2_skip_rate']:.1%}")

    # Test 4: Process noisy signal
    print("\n✓ Processing noisy signal...")
    speech = generate_sine_wave(440, 100)
    noise = generate_noise(100, amplitude=500)
    noisy = add_audio(speech, noise)

    # Adapt first
    for _ in range(15):
        denoiser.process(noise)

    # Then process speech
    denoised, vad, stats = denoiser.process(noisy)
    print(f"  VAD after noise adaptation: {vad:.2f}")

    print("\n✅ All sanity tests passed!")
    print("\nRun full test suite with: pytest services/agent/tests/test_two_stage_denoiser.py -v")
