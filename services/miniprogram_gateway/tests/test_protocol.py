from __future__ import annotations

import json
from pathlib import Path

import pytest
from services.miniprogram_gateway.protocol import (
    CLIENT_AUDIO_TRACE_DETAIL_FIELDS,
    CLIENT_AUDIO_TRACE_NAMES,
    CLIENT_AUDIO_TRACE_PROTOCOL_VERSION,
    GENERATION_HEADER_SIZE,
    GENERATION_PROTOCOL_VERSION,
    HEADER_SIZE,
    MAX_AUDIO_PAYLOAD_BYTES,
    PROTOCOL_VERSION,
    FrameType,
    ProtocolError,
    decode_pcm_frame,
    encode_pcm_frame,
)

CONTRACT = json.loads(
    (Path(__file__).parents[3] / "packages" / "contracts" / "miniprogram-media.json").read_text(
        encoding="utf-8"
    )
)


def test_pcm_media_constants_match_shared_contract() -> None:
    binary = CONTRACT["binary"]

    assert PROTOCOL_VERSION == binary["protocol_version"]
    assert GENERATION_PROTOCOL_VERSION == binary["generation_protocol_version"]
    assert HEADER_SIZE == binary["header_size"]
    assert GENERATION_HEADER_SIZE == binary["generation_header_size"]
    assert MAX_AUDIO_PAYLOAD_BYTES == binary["max_audio_payload_bytes"]
    assert int(FrameType.UPLINK_AUDIO) == binary["frame_types"]["uplink_audio"]
    assert int(FrameType.DOWNLINK_AUDIO) == binary["frame_types"]["downlink_audio"]
    client_trace = CONTRACT["control_events"]["client_audio_trace"]
    assert CLIENT_AUDIO_TRACE_PROTOCOL_VERSION == client_trace["protocol_version"]
    assert CLIENT_AUDIO_TRACE_NAMES == frozenset(client_trace["names"])
    assert CLIENT_AUDIO_TRACE_DETAIL_FIELDS == frozenset(client_trace["detail_fields"])


@pytest.mark.parametrize("vector", CONTRACT["golden_frames"], ids=lambda value: value["name"])
def test_pcm_media_golden_frames_stay_compatible(vector: dict[str, object]) -> None:
    frame_type = FrameType[str(vector["frame_type"]).upper()]
    raw = bytes.fromhex(str(vector["frame_hex"]))

    frame = decode_pcm_frame(raw, expected_type=frame_type)

    assert frame.sequence == vector["sequence"]
    assert frame.timestamp_ms == vector["timestamp_ms"]
    assert frame.generation_id == vector["generation_id"]
    assert frame.payload.hex() == vector["payload_hex"]
    assert (
        encode_pcm_frame(
            frame.frame_type,
            sequence=frame.sequence,
            timestamp_ms=frame.timestamp_ms,
            generation_id=frame.generation_id,
            payload=frame.payload,
        )
        == raw
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
    assert frame.generation_id is None


def test_downlink_pcm_frame_round_trip_binds_generation() -> None:
    encoded = encode_pcm_frame(
        FrameType.DOWNLINK_AUDIO,
        sequence=8,
        generation_id=3,
        timestamp_ms=12_346,
        payload=b"\x04\x05",
    )

    frame = decode_pcm_frame(encoded, expected_type=FrameType.DOWNLINK_AUDIO)

    assert frame.sequence == 8
    assert frame.generation_id == 3
    assert frame.timestamp_ms == 12_346
    assert frame.payload == b"\x04\x05"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "header is incomplete"),
        (b"\x01\x01\x00\x00", "header is incomplete"),
        (b"\x09\x01\x00\x00" + b"\x00" * 16, "unsupported PCM frame type"),
        (b"\x01\x03\x00\x00" + b"\x00" * 16, "unsupported PCM protocol version"),
        (b"\x01\x02\x00\x00" + b"\x00" * 16, "generation header is incomplete"),
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
