"""Convert cached 24 kHz mono PCM listener cues into fresh LiveKit frame streams."""

from __future__ import annotations

from collections.abc import AsyncIterator

from livekit import rtc


async def pcm_frame_source(
    pcm: bytes,
    *,
    sample_rate: int = 24000,
    frame_ms: int = 20,
) -> AsyncIterator[rtc.AudioFrame]:
    samples_per_frame = sample_rate * frame_ms // 1000
    bytes_per_frame = samples_per_frame * 2
    for offset in range(0, len(pcm), bytes_per_frame):
        chunk = pcm[offset : offset + bytes_per_frame]
        if len(chunk) % 2:
            chunk = chunk[:-1]
        samples = len(chunk) // 2
        if samples == 0:
            continue
        yield rtc.AudioFrame(
            data=chunk,
            sample_rate=sample_rate,
            num_channels=1,
            samples_per_channel=samples,
        )
