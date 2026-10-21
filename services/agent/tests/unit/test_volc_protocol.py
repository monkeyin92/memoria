"""Pure codec tests for the Volcengine streaming ASR protocol.

Every expectation here is taken from the official document
(https://www.volcengine.com/docs/6561/1354869) or its bundled demo client
(``sauc_python/sauc_websocket_demo.py``), not from this repository's codec.
"""

from __future__ import annotations

import gzip
import json
import logging
import struct

import pytest
from services.agent.src.providers.volc_protocol import (
    AUDIO_ONLY_REQUEST,
    FULL_CLIENT_REQUEST,
    FULL_SERVER_RESPONSE,
    LAST_NO_SEQUENCE,
    LAST_WITH_SEQUENCE,
    NO_SERIALIZATION,
    POS_SEQUENCE,
    SERVER_ERROR_RESPONSE,
    build_audio,
    build_empty_last_audio,
    build_full_client_request,
    build_task_request,
    extract_transcript,
    parse_frame,
)


def test_full_client_request_is_one_json_frame_with_positive_sequence() -> None:
    payload = build_task_request()
    raw = build_full_client_request(payload)
    assert raw[0] == 0x11
    assert raw[1] >> 4 == FULL_CLIENT_REQUEST
    assert raw[1] & 0x0F == POS_SEQUENCE
    assert raw[2] >> 4 == 0b0001
    assert raw[2] & 0x0F == 0b0001
    assert struct.unpack(">i", raw[4:8])[0] == 1
    size = struct.unpack(">I", raw[8:12])[0]
    assert json.loads(gzip.decompress(raw[12 : 12 + size])) == payload
    assert parse_frame(raw).sequence == 1
    assert parse_frame(raw).is_last is False


def test_audio_frame_is_positive_sequence_with_no_serialization() -> None:
    pcm = b"\x01\x02" * 1600
    raw = build_audio(pcm, sequence=7)
    frame = parse_frame(raw)
    assert frame.message_type == AUDIO_ONLY_REQUEST
    assert frame.sequence == 7
    assert frame.is_last is False
    assert frame.payload == {}
    assert raw[2] >> 4 == NO_SERIALIZATION
    size = struct.unpack(">I", raw[8:12])[0]
    assert size == len(gzip.compress(pcm))
    assert gzip.decompress(raw[12 : 12 + size]) == pcm


def test_last_audio_frame_carries_negative_sequence() -> None:
    raw = build_audio(b"\x00\x00" * 1600, sequence=3, last=True)
    assert raw[1] & 0x0F == LAST_WITH_SEQUENCE
    assert struct.unpack(">i", raw[4:8])[0] == -3
    frame = parse_frame(raw)
    assert frame.sequence == -3
    assert frame.is_last is True


def test_empty_last_packet_uses_the_no_sequence_last_flag() -> None:
    """The document's other final-packet form, used by the official Go SDK."""

    raw = build_empty_last_audio()
    assert raw[1] & 0x0F == LAST_NO_SEQUENCE
    frame = parse_frame(raw)
    assert frame.is_last is True
    assert frame.sequence is None
    assert frame.message_type == AUDIO_ONLY_REQUEST


def test_empty_audio_is_only_allowed_on_the_last_packet() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        build_audio(b"", sequence=1)


def test_task_request_pins_pcm_16k_and_nests_hotword_table() -> None:
    payload = build_task_request(
        sample_rate=16_000,
        end_window_ms=550,
        boosting_table_id="stop-table",
    )
    assert payload["audio"] == {
        "format": "pcm",
        "codec": "raw",
        "rate": 16_000,
        "bits": 16,
        "channel": 1,
    }
    assert payload["request"]["end_window_size"] == 550
    assert payload["request"]["enable_nonstream"] is True
    # The document places hotword tables under ``request.corpus``.
    assert payload["request"]["corpus"] == {"boosting_table_id": "stop-table"}
    assert "boosting_table_id" not in payload["request"]
    assert payload["user"]["uid"] == "memoria"


def test_task_request_omits_absent_boosting_table() -> None:
    assert "corpus" not in build_task_request()["request"]


def test_extract_transcript_reads_utterance_word_times() -> None:
    payload = {
        "result": {
            "text": "我想听故事。",
            "utterances": [
                {
                    "start_time": 120,
                    "end_time": 980,
                    "definite": True,
                    "words": [
                        {"text": "我想", "start_time": 120, "end_time": 400},
                        {"text": "听故事", "start_time": 400, "end_time": 980},
                    ],
                }
            ],
        }
    }
    transcript = extract_transcript(payload)
    assert transcript is not None
    assert transcript.text == "我想听故事。"
    assert transcript.definite is True
    assert transcript.begin_ms == 120
    assert transcript.end_ms == 980
    assert transcript.word_times == (("我想", 120, 400), ("听故事", 400, 980))


def test_extract_transcript_requires_every_utterance_to_be_definite() -> None:
    payload = {
        "result": {
            "text": "你好呀",
            "utterances": [
                {"text": "你好", "start_time": 0, "end_time": 200, "definite": True},
                {"text": "呀", "start_time": 200, "end_time": 400, "definite": False},
            ],
        }
    }
    transcript = extract_transcript(payload)
    assert transcript is not None
    assert transcript.definite is False


def test_extract_transcript_trusts_a_result_level_definite_flag() -> None:
    payload = {
        "result": {
            "text": "你好呀",
            "definite": True,
            "utterances": [
                {"text": "你好", "start_time": 0, "end_time": 200, "definite": False},
            ],
        }
    }
    transcript = extract_transcript(payload)
    assert transcript is not None
    assert transcript.definite is True


def test_extract_transcript_ignores_empty_text() -> None:
    assert extract_transcript({"result": {"text": "   "}}) is None
    assert extract_transcript({}) is None


def test_parse_frame_reads_sequence_and_last_flag_from_server_response() -> None:
    payload = gzip.compress(json.dumps({"result": {"text": "好了"}}).encode())
    header = bytes([0x11, (FULL_SERVER_RESPONSE << 4) | 0b0011, 0x11, 0x00])
    frame = parse_frame(
        header
        + struct.pack(">i", 4)
        + struct.pack(">I", len(payload))
        + payload
    )
    assert frame.message_type == FULL_SERVER_RESPONSE
    assert frame.sequence == 4
    assert frame.is_last is True
    assert frame.payload["result"]["text"] == "好了"


def test_parse_frame_accepts_raw_audio_frames_without_json() -> None:
    header = bytes([0x11, (FULL_SERVER_RESPONSE << 4) | 0b0000, 0x00, 0x00])
    body = b"\x00\x01\x02"
    frame = parse_frame(header + struct.pack(">I", len(body)) + body)
    assert frame.payload == {}
    assert frame.sequence is None
    assert frame.is_last is False


def test_parse_frame_rejects_truncated_or_text_frames() -> None:
    with pytest.raises(ValueError):
        parse_frame(b"\x11")
    with pytest.raises(ValueError):
        parse_frame(b"\x11\x10\x10\x00")
    with pytest.raises(ValueError):
        parse_frame("not binary")


@pytest.mark.parametrize(
    ("body", "cause_type"),
    [
        (b"not gzip", OSError),
        (gzip.compress(b'{"result": {"text": "x"}}')[:-2], EOFError),
    ],
)
def test_parse_frame_reports_corrupt_gzip_payload(
    body: bytes,
    cause_type: type[BaseException],
) -> None:
    header = bytes([0x11, (FULL_SERVER_RESPONSE << 4) | POS_SEQUENCE, 0x11, 0x00])
    with pytest.raises(ValueError, match="gzip decompression failed") as caught:
        parse_frame(
            header + struct.pack(">i", 1) + struct.pack(">I", len(body)) + body
        )
    assert isinstance(caught.value.__cause__, cause_type)


@pytest.mark.parametrize("decoded", [[{"text": "x"}], "scalar", 42])
def test_parse_frame_treats_non_object_json_as_empty(
    decoded: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = gzip.compress(json.dumps(decoded).encode())
    header = bytes([0x11, (FULL_SERVER_RESPONSE << 4), 0x11, 0x00])
    logger_name = "services.agent.src.providers.volc_protocol"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        frame = parse_frame(header + struct.pack(">I", len(body)) + body)
    assert frame.payload == {}
    messages = [
        record.getMessage() for record in caplog.records if record.name == logger_name
    ]
    assert messages == [
        f"volc ASR frame payload is not a JSON object: {type(decoded).__name__}"
    ]


def test_parse_frame_surfaces_server_error_code() -> None:
    payload = gzip.compress(json.dumps({"error": "busy"}).encode())
    header = bytes([0x11, (SERVER_ERROR_RESPONSE << 4) | 0x0, 0x11, 0x00])
    frame = parse_frame(
        header + struct.pack(">i", 55000031) + struct.pack(">I", len(payload)) + payload
    )
    assert frame.error_code == 55_000_031
    assert frame.payload == {"error": "busy"}
