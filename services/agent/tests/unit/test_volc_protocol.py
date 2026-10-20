"""Pure codec tests for the Volcengine streaming ASR protocol."""

from __future__ import annotations

import gzip
import json
import struct

import pytest
from services.agent.src.providers.volc_protocol import (
    FULL_CLIENT_REQUEST,
    POS_SEQUENCE,
    VolcEvent,
    build_audio,
    build_event,
    build_task_request,
    extract_transcript,
    parse_frame,
)


def test_event_frame_round_trips_event_and_payload() -> None:
    frame = parse_frame(build_event(VolcEvent.TASK_REQUEST, {"audio": {"rate": 16_000}}))
    assert frame.message_type == FULL_CLIENT_REQUEST
    assert frame.event == int(VolcEvent.TASK_REQUEST)
    assert frame.payload == {"audio": {"rate": 16_000}}
    assert frame.sequence is None


def test_audio_frame_uses_positive_sequence_and_gzip_pcm() -> None:
    pcm = b"\x01\x02" * 1600
    frame = parse_frame(build_audio(pcm, sequence=7))
    assert frame.sequence == 7
    assert frame.payload == {}
    raw = build_audio(pcm, sequence=7)
    size = struct.unpack(">I", raw[8:12])[0]
    assert size == len(gzip.compress(pcm))
    assert gzip.decompress(raw[12 : 12 + size]) == pcm
    assert raw[1] & 0x0F == POS_SEQUENCE


def test_last_audio_frame_carries_negative_sequence() -> None:
    frame = parse_frame(build_audio(b"\x00\x00" * 1600, sequence=3, last=True))
    assert frame.sequence == -3


def test_task_request_pins_pcm_16k_and_end_window() -> None:
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
    assert payload["request"]["boosting_table_id"] == "stop-table"
    assert payload["user"]["uid"] == "memoria"


def test_task_request_omits_absent_boosting_table() -> None:
    assert "boosting_table_id" not in build_task_request()["request"]


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


def test_extract_transcript_treats_full_result_as_final() -> None:
    transcript = extract_transcript({"result": {"text": "你好", "result_type": "full"}})
    assert transcript is not None
    assert transcript.definite is True


def test_extract_transcript_ignores_empty_text() -> None:
    assert extract_transcript({"result": {"text": "   "}}) is None
    assert extract_transcript({}) is None


def test_parse_frame_rejects_truncated_or_text_frames() -> None:
    with pytest.raises(ValueError):
        parse_frame(b"\x11")
    with pytest.raises(ValueError):
        parse_frame(b"\x11\x10\x10\x00")
    with pytest.raises(ValueError):
        parse_frame("not binary")


def test_parse_frame_surfaces_server_error_code() -> None:
    payload = gzip.compress(json.dumps({"error": "busy"}).encode())
    header = bytes([0x11, (0xF << 4) | 0x0, 0x11, 0x00])
    frame = parse_frame(header + struct.pack(">i", 55000031) + struct.pack(">I", len(payload)) + payload)
    assert frame.error_code == 55_000_031
    assert frame.payload == {"error": "busy"}
