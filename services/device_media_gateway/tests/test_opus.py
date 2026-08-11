from __future__ import annotations

import math

import pytest

from services.device_media_gateway.opus import OpusDecoder, OpusEncoder


@pytest.mark.parametrize("sample_rate", [16_000, 24_000])
def test_pyav_opus_roundtrip_keeps_fixed_mono_20ms_frames(sample_rate: int) -> None:
    frame_samples = sample_rate // 50
    encoder = OpusEncoder(sample_rate=sample_rate, frame_samples=frame_samples)
    decoder = OpusDecoder(
        sample_rate=sample_rate,
        frame_samples=frame_samples,
        max_packet_bytes=4_096,
    )
    packets: list[bytes] = []
    for offset in range(6):
        pcm = b"".join(
            int(
                8_000 * math.sin(2 * math.pi * 440 * (offset * frame_samples + i) / sample_rate)
            ).to_bytes(2, "little", signed=True)
            for i in range(frame_samples)
        )
        packet = encoder.encode(pcm)
        assert packet
        assert len(packet) <= 4_096
        packets.append(packet)

    decoded = [frame for packet in packets for frame in decoder.decode(packet)]
    assert decoded
    assert all(len(frame) == frame_samples * 2 for frame in decoded)
