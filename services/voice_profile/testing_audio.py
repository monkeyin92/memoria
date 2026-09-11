"""Synthesize voice-sample fixtures for tests.

Kept outside ``tests/`` because both ``services/voice_profile/tests`` and
``services/control_api/tests`` enroll samples and both must submit audio that
really decodes (the enrollment path now measures the sample instead of
trusting the client's ``duration_ms``).
"""

from __future__ import annotations

import io
import wave
from typing import cast

import av
import numpy as np

DEFAULT_SAMPLE_RATE = 16_000


def speech_like(
    *,
    duration_ms: int = 15_000,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    amplitude: float = 0.2,
    silence_ms: int = 0,
) -> np.ndarray:
    """A deterministic voiced-sounding signal: a slow envelope over a tone.

    Loud enough to read as speech, quiet enough not to clip.
    """
    total = int(sample_rate * duration_ms / 1000)
    timeline = np.arange(total) / sample_rate
    # A 180 Hz carrier under a 3 Hz envelope keeps every 20 ms frame above the
    # silence floor while the overall level stays in a normal recording range.
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * timeline)
    signal = amplitude * np.sin(2 * np.pi * 180.0 * timeline) * envelope
    trailing = int(sample_rate * silence_ms / 1000)
    if trailing:
        signal = np.concatenate([signal, np.zeros(trailing)])
    return signal.astype(np.float32)


def silent(
    *, duration_ms: int = 15_000, sample_rate: int = DEFAULT_SAMPLE_RATE
) -> np.ndarray:
    return np.zeros(int(sample_rate * duration_ms / 1000), dtype=np.float32)


def wav_bytes(
    samples: np.ndarray, *, sample_rate: int = DEFAULT_SAMPLE_RATE
) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(
            (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        )
    return buffer.getvalue()


def mp3_bytes(
    samples: np.ndarray, *, sample_rate: int = DEFAULT_SAMPLE_RATE
) -> bytes:
    """Encode through PyAV so the decode path in the test is the real one."""
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="mp3") as container:
        stream = cast(av.AudioStream, container.add_stream("libmp3lame", rate=sample_rate))
        stream.layout = "mono"
        frame = av.AudioFrame.from_ndarray(
            samples.reshape(1, -1), format="fltp", layout="mono"
        )
        frame.sample_rate = sample_rate
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buffer.getvalue()


def voice_sample_wav(duration_ms: int = 15_000) -> bytes:
    """The default good sample: 15 s of voiced audio as 16 kHz mono WAV."""
    return wav_bytes(speech_like(duration_ms=duration_ms))


def voice_sample_mp3(duration_ms: int = 15_000) -> bytes:
    return mp3_bytes(speech_like(duration_ms=duration_ms))
