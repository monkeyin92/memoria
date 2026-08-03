from __future__ import annotations

import json
import struct

import pytest
from services.agent.src.providers.doubao_protocol import (
    EventType,
    MessageType,
    ServerMessage,
    align_subtitle_words,
    build_client_message,
    build_start_session_payload,
    build_task_request_payload,
    parse_server_message,
    parse_subtitle_words,
    pcm_duration_ms,
)


def _server_frame(
    *,
    message_type: int,
    event: int,
    payload: bytes,
    session_id: str = "",
    connect_id: str = "",
    serialization: int = 1,
) -> bytes:
    frame = bytearray((0x11, (message_type << 4) | 0x04, serialization << 4, 0))
    frame.extend(struct.pack(">i", event))
    if event not in {1, 2, 50, 51, 52}:
        encoded_session = session_id.encode()
        frame.extend(struct.pack(">I", len(encoded_session)))
        frame.extend(encoded_session)
    if event in {50, 51, 52}:
        encoded_connect = connect_id.encode()
        frame.extend(struct.pack(">I", len(encoded_connect)))
        frame.extend(encoded_connect)
    frame.extend(struct.pack(">I", len(payload)))
    frame.extend(payload)
    return bytes(frame)


def test_client_connection_and_session_frames() -> None:
    connection = build_client_message(EventType.START_CONNECTION)
    assert connection[:4] == b"\x11\x14\x10\x00"
    assert struct.unpack_from(">i", connection, 4)[0] == EventType.START_CONNECTION

    session = build_client_message(
        EventType.START_SESSION,
        session_id="session-1",
        payload={"hello": "世界"},
    )
    assert struct.unpack_from(">i", session, 4)[0] == EventType.START_SESSION
    sid_size = struct.unpack_from(">I", session, 8)[0]
    assert session[12 : 12 + sid_size] == b"session-1"
    payload_size = struct.unpack_from(">I", session, 12 + sid_size)[0]
    payload = session[16 + sid_size : 16 + sid_size + payload_size]
    assert json.loads(payload) == {"hello": "世界"}


def test_session_event_requires_session_id() -> None:
    with pytest.raises(ValueError, match="session_id"):
        build_client_message(EventType.FINISH_SESSION)


def test_parse_connection_audio_and_subtitle_frames() -> None:
    connected = parse_server_message(
        _server_frame(
            message_type=MessageType.FULL_SERVER_RESPONSE,
            event=EventType.CONNECTION_STARTED,
            payload=b"{}",
            connect_id="connect-1",
        )
    )
    assert connected.connect_id == "connect-1"
    assert connected.json_payload() == {}

    audio = parse_server_message(
        _server_frame(
            message_type=MessageType.AUDIO_ONLY_SERVER,
            event=EventType.TTS_RESPONSE,
            session_id="session-1",
            payload=b"\x00\x01",
            serialization=0,
        )
    )
    assert audio.payload == b"\x00\x01"
    assert audio.session_id == "session-1"

    subtitle_payload = json.dumps(
        {
            "words": [
                {"word": "你", "startTime": 0.01, "endTime": 0.12},
                {"word": "好", "startTime": 0.12, "endTime": 0.25},
            ]
        }
    ).encode()
    subtitle = parse_server_message(
        _server_frame(
            message_type=MessageType.FULL_SERVER_RESPONSE,
            event=EventType.TTS_SUBTITLE,
            session_id="session-1",
            payload=subtitle_payload,
        )
    )
    assert [
        (word.text, word.begin_ms, word.end_ms) for word in parse_subtitle_words(subtitle.payload)
    ] == [
        ("你", 10, 120),
        ("好", 120, 250),
    ]


def test_request_payloads_and_pcm_duration() -> None:
    start = build_start_session_payload(
        speaker="voice-id",
        uid="uid-1",
        context_texts=("自然说话",),
        pitch=1,
    )
    params = start["req_params"]
    assert params["speaker"] == "voice-id"
    assert params["audio_params"]["format"] == "pcm"
    assert params["audio_params"]["enable_subtitle"] is True
    assert params["context_texts"] == ["自然说话"]
    assert params["post_process"] == {"pitch": 1}
    assert json.loads(params["additions"])["disable_markdown_filter"] is True
    assert build_task_request_payload("你好")["req_params"]["text"] == "你好"
    assert pcm_duration_ms(b"\0" * 48000) == 1000
    words = parse_subtitle_words({"words": [{"word": "你", "startTime": 0, "endTime": 0.5}]})
    aligned, status = align_subtitle_words(words, pcm_duration_ms_value=1000)
    assert status == "scaled"
    assert aligned[-1].end_ms == 1000


def test_rejects_truncated_or_compressed_frames() -> None:
    with pytest.raises(ValueError, match="shorter"):
        parse_server_message(b"\x11")
    compressed = bytearray(
        _server_frame(
            message_type=MessageType.FULL_SERVER_RESPONSE,
            event=EventType.CONNECTION_STARTED,
            payload=b"{}",
        )
    )
    compressed[2] = 0x11
    with pytest.raises(ValueError, match="compressed"):
        parse_server_message(bytes(compressed))


def test_server_payload_and_protocol_validation_edges() -> None:
    empty = ServerMessage(0, 0, 0, "", "", b"")
    assert empty.json_payload() == {}
    with pytest.raises(ValueError, match="JSON object"):
        ServerMessage(0, 0, 0, "", "", b"[]").json_payload()

    bad_header = bytearray(b"\x01\x00\x00\x00\x00\x00\x00\x00")
    with pytest.raises(ValueError, match="protocol header"):
        parse_server_message(bytes(bad_header))
    truncated_header = bytearray(b"\x13\x00\x00\x00\x00\x00\x00\x00")
    with pytest.raises(ValueError, match="truncated.*header"):
        parse_server_message(bytes(truncated_header))

    unsupported_type = bytearray(
        _server_frame(
            message_type=MessageType.FULL_SERVER_RESPONSE,
            event=EventType.CONNECTION_STARTED,
            payload=b"{}",
        )
    )
    unsupported_type[1] = 0x34
    with pytest.raises(ValueError, match="unsupported.*message type"):
        parse_server_message(bytes(unsupported_type))

    audio_json = _server_frame(
        message_type=MessageType.AUDIO_ONLY_SERVER,
        event=EventType.TTS_RESPONSE,
        session_id="session-1",
        payload=b"{}",
        serialization=1,
    )
    with pytest.raises(ValueError, match="audio frame.*raw"):
        parse_server_message(audio_json)
    unsupported_serialization = _server_frame(
        message_type=MessageType.FULL_SERVER_RESPONSE,
        event=EventType.TTS_RESPONSE,
        session_id="session-1",
        payload=b"{}",
        serialization=2,
    )
    with pytest.raises(ValueError, match="response serialization"):
        parse_server_message(unsupported_serialization)


def test_subtitle_parser_handles_nested_and_malformed_words() -> None:
    assert parse_subtitle_words([]) == ()
    assert parse_subtitle_words({"payload": {"words": []}}) == ()
    assert parse_subtitle_words({"payload": {}}) == ()
    words = parse_subtitle_words(
        {
            "words": [
                "bad",
                {"word": 1, "startTime": 0, "endTime": 1},
                {"word": "倒序", "startTime": 1, "endTime": 0},
                {"word": "好", "startTime": 0, "endTime": 0.1},
            ]
        }
    )
    assert [(word.text, word.begin_ms, word.end_ms) for word in words] == [("好", 0, 100)]
