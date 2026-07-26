from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np
import pytest
from livekit import rtc
from services.miniprogram_gateway import audio_processing as audio_processing_module
from services.miniprogram_gateway.audio_processing import MiniProgramAudioProcessor

REPO_ROOT = Path(__file__).parents[3]
DOWNLINK_FIXTURE = REPO_ROOT / "apps/h5/public/assets/voices/calm_guide.wav"
NEAR_END_FIXTURE = REPO_ROOT / "apps/h5/public/assets/voices/bright_peer.wav"


def _read_mono_pcm16(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        return np.frombuffer(
            audio.readframes(audio.getnframes()), dtype="<i2"
        ), audio.getframerate()


def _resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    output_length = len(samples) * target_rate // source_rate
    positions = np.arange(output_length, dtype=np.float64) * source_rate / target_rate
    return np.interp(positions, np.arange(len(samples)), samples.astype(np.float64))


def _repeat_normalized(path: Path, *, seconds: int, peak: int) -> tuple[np.ndarray, int]:
    samples, sample_rate = _read_mono_pcm16(path)
    samples = samples.astype(np.float64)
    samples *= peak / max(float(np.max(np.abs(samples))), 1.0)
    repeats = math.ceil(seconds * sample_rate / len(samples))
    return np.tile(samples, repeats)[: seconds * sample_rate], sample_rate


def _run_processor(
    downlink: np.ndarray,
    microphone: np.ndarray,
    *,
    stream_delay_ms: int,
) -> np.ndarray:
    now = 0.0
    processor = MiniProgramAudioProcessor(
        enabled=True,
        downlink_sample_rate=24_000,
        uplink_sample_rate=16_000,
        stream_delay_ms=stream_delay_ms,
        active_window_ms=1_000,
        clock=lambda: now,
    )
    output: list[np.ndarray] = []
    for downlink_offset, uplink_offset in zip(
        range(0, len(downlink), 480),
        range(0, len(microphone), 320),
        strict=True,
    ):
        processor.observe_downlink(
            np.clip(downlink[downlink_offset : downlink_offset + 480], -32768, 32767)
            .astype("<i2")
            .tobytes()
        )
        processed = processor.process_uplink(
            np.clip(microphone[uplink_offset : uplink_offset + 320], -32768, 32767)
            .astype("<i2")
            .tobytes()
        )
        output.append(np.frombuffer(processed, dtype="<i2").copy())
        now += 0.02
    return np.concatenate(output)


def _fixture_signals(
    *, seconds: int = 15, stream_delay_ms: int = 100
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
]:
    downlink, downlink_rate = _repeat_normalized(
        DOWNLINK_FIXTURE,
        seconds=seconds,
        peak=12_000,
    )
    assert downlink_rate == 24_000
    downlink = downlink[: len(downlink) // 480 * 480]
    rendered_at_uplink_rate = _resample(downlink, 24_000, 16_000)

    delay_samples = 16_000 * stream_delay_ms // 1_000
    echo = np.zeros_like(rendered_at_uplink_rate)
    echo[delay_samples:] = rendered_at_uplink_rate[:-delay_samples] * 0.55

    near_end, near_end_rate = _repeat_normalized(
        NEAR_END_FIXTURE,
        seconds=seconds,
        peak=10_000,
    )
    assert near_end_rate == 24_000
    near_end = _resample(near_end, 24_000, 16_000)[: len(echo)] * 0.8
    near_end[: 6 * 16_000] = 0
    return downlink, echo, near_end


def _rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def test_apm_suppresses_pure_playback_echo_before_livekit_uplink() -> None:
    downlink, echo, _near_end = _fixture_signals()

    processed = _run_processor(downlink, echo, stream_delay_ms=100)

    steady_state = slice(5 * 16_000, None)
    attenuation_db = 20 * math.log10(
        max(_rms(processed[steady_state]), 1e-9) / _rms(echo[steady_state])
    )
    assert attenuation_db < -20


def test_apm_preserves_near_end_speech_during_playback_echo() -> None:
    downlink, echo, near_end = _fixture_signals()

    near_end_baseline = _run_processor(downlink, near_end, stream_delay_ms=100)
    double_talk = _run_processor(downlink, echo + near_end, stream_delay_ms=100)

    steady_state = slice(7 * 16_000, None)
    baseline = near_end_baseline[steady_state].astype(np.float64)
    actual = double_talk[steady_state].astype(np.float64)
    assert _rms(baseline) > _rms(near_end[steady_state]) * 0.5
    assert _rms(actual) > _rms(baseline) * 0.5
    assert float(np.corrcoef(actual, baseline)[0, 1]) > 0.75


def test_apm_tolerates_miniprogram_playout_delay_mismatch() -> None:
    downlink, echo, near_end = _fixture_signals(
        seconds=12,
        stream_delay_ms=200,
    )

    pure_echo = _run_processor(downlink, echo, stream_delay_ms=120)
    near_end_baseline = _run_processor(downlink, near_end, stream_delay_ms=120)
    double_talk = _run_processor(downlink, echo + near_end, stream_delay_ms=120)

    steady_state = slice(7 * 16_000, None)
    attenuation_db = 20 * math.log10(
        max(_rms(pure_echo[steady_state]), 1e-9) / _rms(echo[steady_state])
    )
    baseline = near_end_baseline[steady_state].astype(np.float64)
    actual = double_talk[steady_state].astype(np.float64)
    assert attenuation_db < -20
    assert _rms(actual) > _rms(baseline) * 0.5
    assert float(np.corrcoef(actual, baseline)[0, 1]) > 0.7


def test_apm_leaves_uplink_unchanged_without_recent_playback() -> None:
    now = 100.0
    processor = MiniProgramAudioProcessor(
        enabled=True,
        downlink_sample_rate=24_000,
        uplink_sample_rate=16_000,
        stream_delay_ms=120,
        active_window_ms=1_000,
        clock=lambda: now,
    )
    microphone = (np.arange(320, dtype=np.int16) - 160).tobytes()

    assert processor.process_uplink(microphone) == microphone
    processor.observe_downlink(bytes(960))
    now += 1.001
    assert processor.process_uplink(microphone) == microphone


def test_apm_reset_forgets_unsent_reverse_audio() -> None:
    now = 100.0
    processor = MiniProgramAudioProcessor(
        enabled=True,
        downlink_sample_rate=24_000,
        uplink_sample_rate=16_000,
        stream_delay_ms=120,
        active_window_ms=750,
        clock=lambda: now,
    )
    microphone = (np.arange(320, dtype=np.int16) - 160).tobytes()

    processor.observe_downlink(bytes(960))
    processor.reset()

    assert processor.process_uplink(microphone) == microphone


def test_generation_reset_preserves_a_healthy_learned_apm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[FakeApm] = []

    class FakeApm:
        def __init__(self, **_kwargs: object) -> None:
            created.append(self)

        def set_stream_delay_ms(self, _delay_ms: int) -> None:
            pass

        def process_reverse_stream(self, _frame: rtc.AudioFrame) -> None:
            pass

        def process_stream(self, _frame: rtc.AudioFrame) -> None:
            pass

    monkeypatch.setattr(audio_processing_module.rtc, "AudioProcessingModule", FakeApm)
    processor = MiniProgramAudioProcessor(
        enabled=True,
        downlink_sample_rate=24_000,
        uplink_sample_rate=16_000,
        stream_delay_ms=120,
        active_window_ms=750,
    )

    processor.observe_downlink(bytes(960))
    processor.reset()

    assert len(created) == 1
    assert processor._apm is created[0]


def test_apm_keeps_near_end_speech_after_playback_ends() -> None:
    downlink, echo, near_end = _fixture_signals(seconds=9)
    now = 0.0

    def run_tail(*, manual_silence_reverse: bool) -> np.ndarray:
        nonlocal now
        now = 0.0
        processor = MiniProgramAudioProcessor(
            enabled=True,
            downlink_sample_rate=24_000,
            uplink_sample_rate=16_000,
            stream_delay_ms=100,
            active_window_ms=750,
            clock=lambda: now,
        )
        for offset in range(0, 8 * 24_000, 480):
            processor.observe_downlink(
                np.clip(downlink[offset : offset + 480], -32768, 32767).astype("<i2").tobytes()
            )
            uplink_offset = offset * 2 // 3
            processor.process_uplink(
                np.clip(echo[uplink_offset : uplink_offset + 320], -32768, 32767)
                .astype("<i2")
                .tobytes()
            )
            now += 0.02

        output: list[np.ndarray] = []
        near_end_offset = 7 * 16_000
        for index in range(25):
            if manual_silence_reverse and index > 0:
                processor.observe_downlink(bytes(960))
            payload = (
                np.clip(
                    near_end[near_end_offset + index * 320 : near_end_offset + (index + 1) * 320],
                    -32768,
                    32767,
                )
                .astype("<i2")
                .tobytes()
            )
            output.append(np.frombuffer(processor.process_uplink(payload), dtype="<i2").copy())
            now += 0.02
        return np.concatenate(output)

    expected = run_tail(manual_silence_reverse=True)
    actual = run_tail(manual_silence_reverse=False)

    warm_down = slice(5 * 320, None)
    assert _rms(actual[warm_down]) > _rms(expected[warm_down]) * 0.75
    assert float(np.corrcoef(actual[warm_down], expected[warm_down])[0, 1]) > 0.9
