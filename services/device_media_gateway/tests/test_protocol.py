from __future__ import annotations

import struct

import pytest

from services.device_media_gateway.protocol import (
    HEADER_SIZE,
    UPLINK_FRAME_SAMPLES,
    FrameType,
    ProtocolError,
    decode_audio_frame,
    encode_audio_frame,
    validate_audio_shape,
    validate_device_event,
    validate_device_hello,
)


def test_memoria_audio_frame_v1_is_30_bytes_and_network_order() -> None:
    payload = b"opus"
    raw = encode_audio_frame(
        FrameType.UPLINK_AUDIO,
        stream_epoch=0x01020304,
        sequence=0x05060708,
        sample_start=0x090A0B0C0D0E0F10,
        frame_samples=UPLINK_FRAME_SAMPLES,
        generation_id=0,
        payload=payload,
    )

    assert HEADER_SIZE == 30
    assert len(raw) == HEADER_SIZE + len(payload)
    assert raw[:4] == b"\x01\x01\x00\x00"
    assert struct.unpack("!I", raw[4:8])[0] == 0x01020304
    assert struct.unpack("!Q", raw[12:20])[0] == 0x090A0B0C0D0E0F10
    assert decode_audio_frame(raw) == decode_audio_frame(
        encode_audio_frame(
            FrameType.UPLINK_AUDIO,
            stream_epoch=0x01020304,
            sequence=0x05060708,
            sample_start=0x090A0B0C0D0E0F10,
            frame_samples=UPLINK_FRAME_SAMPLES,
            generation_id=0,
            payload=payload,
        )
    )


def test_audio_frame_rejects_raw_opus_and_wrong_shape() -> None:
    with pytest.raises(ProtocolError, match="header is incomplete"):
        decode_audio_frame(b"\x01\x02\x03")

    raw = encode_audio_frame(
        FrameType.DOWNLINK_AUDIO,
        stream_epoch=1,
        sequence=0,
        sample_start=0,
        frame_samples=UPLINK_FRAME_SAMPLES,
        generation_id=1,
        payload=b"opus",
    )
    tampered = bytearray(raw)
    tampered[0] = 2
    with pytest.raises(ProtocolError, match="version"):
        decode_audio_frame(tampered)

    wrong_shape = decode_audio_frame(raw)
    with pytest.raises(ProtocolError, match="frame_samples"):
        validate_audio_shape(wrong_shape)


def test_device_hello_is_exact_and_event_allowlist_is_strict() -> None:
    hello = {
        "type": "device.hello",
        "version": 1,
        "device_id": "dev-1",
        "firmware_version": "0.1.0",
        "board_profile": "memoria-atk-dnesp32s3-v1",
        "stream_epoch": 3,
        "capabilities": {
            "display": True,
            "microphone": True,
            "speaker": True,
            "device_aec": False,
            "physical_button": True,
        },
        "audio": {
            "uplink_codec": "opus",
            "uplink_sample_rate": 16000,
            "downlink_sample_rate": 24000,
            "channels": 1,
            "frame_ms": 20,
        },
    }
    assert validate_device_hello(hello, device_id="dev-1", stream_epoch=3) == hello

    with pytest.raises(ProtocolError, match="schema"):
        validate_device_hello({**hello, "unexpected": True}, device_id="dev-1", stream_epoch=3)
    with pytest.raises(ProtocolError, match="unsupported device event"):
        validate_device_event({"type": "xiaozhi.listen"}, stream_epoch=3)

    receipt = {
        "type": "playback.ended",
        "stream_epoch": 3,
        "generation_id": 7,
        "played_sample_end": 480,
        "reason": "completed",
    }
    assert validate_device_event(receipt, stream_epoch=3) == receipt

    vad = {"type": "vad.start", "stream_epoch": 3, "sample_position": 320}
    assert validate_device_event(vad, stream_epoch=3) == vad
    with pytest.raises(ProtocolError, match="schema"):
        validate_device_event({**vad, "text": "private"}, stream_epoch=3)
    with pytest.raises(ProtocolError, match="sample_position"):
        validate_device_event({**vad, "sample_position": True}, stream_epoch=3)
