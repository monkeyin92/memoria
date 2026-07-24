from __future__ import annotations

import pytest
from services.miniprogram_gateway.protocol import (
    FrameType,
    ProtocolError,
    decode_pcm_frame,
    encode_pcm_frame,
)


def test_pcm_frame_round_trip_uses_explicit_direction_and_sequence() -> None:
    encoded = encode_pcm_frame(
        FrameType.UPLINK_AUDIO,
        sequence=7,
        timestamp_ms=12_345,
        payload=b"\x00\x01\x02\x03",
    )

    frame = decode_pcm_frame(encoded, expected_type=FrameType.UPLINK_AUDIO)

    assert frame.frame_type is FrameType.UPLINK_AUDIO
    assert frame.sequence == 7
    assert frame.timestamp_ms == 12_345
    assert frame.payload == b"\x00\x01\x02\x03"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "header is incomplete"),
        (b"\x01\x01\x00\x00", "header is incomplete"),
        (b"\x09\x01\x00\x00" + b"\x00" * 16, "unsupported PCM frame type"),
        (b"\x01\x02\x00\x00" + b"\x00" * 16, "unsupported PCM protocol version"),
        (b"\x01\x01\x00\x01" + b"\x00" * 16, "flags must be zero"),
    ],
)
def test_pcm_frame_rejects_invalid_headers(raw: bytes, message: str) -> None:
    with pytest.raises(ProtocolError, match=message):
        decode_pcm_frame(raw)


def test_pcm_frame_rejects_length_and_wrong_direction() -> None:
    encoded = encode_pcm_frame(
        FrameType.DOWNLINK_AUDIO,
        sequence=1,
        timestamp_ms=2,
        payload=b"\x00\x00",
    )
    tampered_length = bytearray(encoded)
    tampered_length[19] = 3

    with pytest.raises(ProtocolError, match="payload length"):
        decode_pcm_frame(tampered_length)
    with pytest.raises(ProtocolError, match="unexpected PCM frame direction"):
        decode_pcm_frame(encoded, expected_type=FrameType.UPLINK_AUDIO)
