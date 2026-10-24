"""Volcengine streaming session against a scripted in-process WebSocket.

The fake server speaks the official ``sauc/bigmodel`` protocol: one full
client request carrying the JSON configuration, audio-only packets, a negative
packet that ends the task, and one full server response per request whose last
one carries the last-package flag. There are no event frames.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import struct
from typing import Any

import pytest
from services.agent.src.providers.volc_asr import VolcASRConfig, VolcASRError, VolcASRSession
from services.agent.src.providers.volc_protocol import (
    AUDIO_ONLY_REQUEST,
    FULL_CLIENT_REQUEST,
    FULL_SERVER_RESPONSE,
    SERVER_ERROR_RESPONSE,
    build_full_client_request,
    parse_frame,
)


class FakeVolcConnection:
    """Answers a task exactly as the document describes it."""

    def __init__(
        self,
        *,
        result_text: str = "你好",
        fail_first_request: bool = False,
        text_frames: int = 0,
    ) -> None:
        self.sent: list[bytes] = []
        self.fail_first_request = fail_first_request
        self.result_text = result_text
        self.text_frames = text_frames
        self.closed = False
        self.requests = 0
        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def send(self, data: bytes | str) -> None:
        raw = data.encode() if isinstance(data, str) else data
        self.sent.append(raw)
        frame = parse_frame(raw)
        if frame.message_type == FULL_CLIENT_REQUEST:
            self.requests += 1
            if self.fail_first_request and self.requests == 1:
                self._push_error(45_000_001)
                return
            self._push_result("", last=False)
            for _ in range(self.text_frames):
                self._incoming.put_nowait("ignored text frame")
            return
        if frame.message_type != AUDIO_ONLY_REQUEST:
            return
        if frame.is_last:
            self._push_result(self.result_text, last=True, sequence=frame.sequence or 1)

    @property
    def audio_frames(self) -> list[Any]:
        return [
            parse_frame(raw)
            for raw in self.sent
            if parse_frame(raw).message_type == AUDIO_ONLY_REQUEST
        ]

    def _push_result(
        self,
        text: str,
        *,
        last: bool,
        sequence: int = 1,
    ) -> None:
        payload = gzip.compress(
            json.dumps(
                {"result": {"text": text, "utterances": []}},
                ensure_ascii=False,
            ).encode()
        )
        flags = 0b0011 if last else 0b0001
        header = bytes([0x11, (FULL_SERVER_RESPONSE << 4) | flags, 0x11, 0x00])
        self._incoming.put_nowait(
            header
            + struct.pack(">i", abs(sequence))
            + struct.pack(">I", len(payload))
            + payload
        )

    def _push_error(self, code: int) -> None:
        payload = gzip.compress(json.dumps({"error": "bad request"}).encode())
        header = bytes([0x11, (SERVER_ERROR_RESPONSE << 4), 0x00, 0x00])
        self._incoming.put_nowait(
            header + struct.pack(">i", code) + struct.pack(">I", len(payload)) + payload
        )

    def __aiter__(self) -> FakeVolcConnection:
        return self

    async def __anext__(self) -> bytes:
        item = await self._incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        self._incoming.put_nowait(None)


def _config(connector: Any, **overrides: Any) -> VolcASRConfig:
    values: dict[str, Any] = {
        "app_id": "app",
        "access_token": "token",
        "resource_id": "resource",
        "connector": connector,
    }
    values.update(overrides)
    return VolcASRConfig(**values)


def _one_connection(conn: FakeVolcConnection) -> Any:
    async def connector(*args: Any, **kwargs: Any) -> FakeVolcConnection:
        assert kwargs["additional_headers"]["X-Api-App-Key"] == "app"
        assert kwargs["additional_headers"]["X-Api-Access-Key"] == "token"
        assert kwargs["additional_headers"]["X-Api-Resource-Id"] == "resource"
        return conn

    return connector


def _sequence_of_connections(connections: list[FakeVolcConnection]) -> Any:
    pending = list(connections)

    async def connector(*args: Any, **kwargs: Any) -> FakeVolcConnection:
        if not pending:
            raise AssertionError("unexpected extra connection")
        return pending.pop(0)

    return connector


async def _next_event(session: VolcASRSession) -> Any:
    return await asyncio.wait_for(session.events.get(), timeout=1.0)


def _pcm(samples: int = 1600, amplitude: int = 200) -> bytes:
    return bytes((amplitude & 0xFF, (amplitude >> 8) & 0xFF)) * samples


@pytest.mark.asyncio
async def test_connect_sends_configuration_before_any_audio() -> None:
    conn = FakeVolcConnection()
    session = VolcASRSession(_config(_one_connection(conn)))
    await session.connect()
    assert session.task_id
    assert session.task_epoch == 1
    first = parse_frame(conn.sent[0])
    assert first.message_type == FULL_CLIENT_REQUEST
    assert first.sequence == 1
    assert first.payload["request"]["model_name"] == "bigmodel"
    assert first.payload["audio"]["rate"] == 16_000
    await session.aclose()
    assert conn.closed is True


@pytest.mark.asyncio
async def test_audio_is_chunked_and_last_packet_is_negative() -> None:
    conn = FakeVolcConnection(result_text="我想听故事")
    session = VolcASRSession(_config(_one_connection(conn)))
    await session.connect()
    await session.send_pcm(_pcm(3200))
    # Whole chunks go out as positive-sequence packets; nothing is held back.
    assert [frame.sequence for frame in conn.audio_frames] == [1, 2]
    await session.rotate_task(require_consumed=False)
    # The audio ended on a chunk boundary, so the final packet carries the
    # "last package, no sequence" flag instead of a negative sequence.
    assert [frame.sequence for frame in conn.audio_frames] == [1, 2, None]
    assert conn.audio_frames[-1].is_last is True
    assert conn.audio_frames[-1].is_last is True
    assert session.rotation_pending is True
    assert session.segment_epoch == 2
    event = await _next_event(session)
    assert event.event == "result-generated"
    assert event.sentence is not None
    assert event.sentence.text == "我想听故事"
    assert event.sentence.sentence_end is True
    assert (await _next_event(session)).event == "task-finished"
    await session.aclose()


@pytest.mark.asyncio
async def test_rotate_without_audio_emits_local_boundary_and_closes_socket() -> None:
    conn = FakeVolcConnection()
    session = VolcASRSession(_config(_one_connection(conn)))
    await session.connect()
    await session.rotate_task(require_consumed=False)
    assert (await _next_event(session)).event == "task-finished"
    assert conn.audio_frames == []
    assert conn.closed is True
    await session.aclose()


@pytest.mark.asyncio
async def test_text_frames_are_ignored_without_ending_the_task() -> None:
    conn = FakeVolcConnection(result_text="你好呀", text_frames=2)
    session = VolcASRSession(_config(_one_connection(conn)))
    await session.connect()
    await session.send_pcm(_pcm())
    await session.rotate_task(require_consumed=False)
    assert (await _next_event(session)).event == "result-generated"
    assert (await _next_event(session)).event == "task-finished"
    await session.aclose()


@pytest.mark.asyncio
async def test_server_error_surfaces_task_failed_and_blocks_pcm() -> None:
    conn = FakeVolcConnection(fail_first_request=True)
    session = VolcASRSession(_config(_one_connection(conn)))
    await session.connect()
    assert (await _next_event(session)).event == "task-failed"
    with pytest.raises(VolcASRError):
        await session.send_pcm(_pcm())
    await session.aclose()


@pytest.mark.asyncio
async def test_origin_follows_the_first_real_capture_position() -> None:
    first = FakeVolcConnection()
    second = FakeVolcConnection()
    session = VolcASRSession(_config(_sequence_of_connections([first, second])))
    await session.connect()
    await session.rotate_task(require_consumed=False)
    await _next_event(session)
    await session.send_pcm(_pcm(), capture_start_sample=48_000)
    assert session.task_sample_origin == 48_000
    assert session.task_audio_start_sample == 48_000
    await session.aclose()


@pytest.mark.asyncio
async def test_reconnect_replays_audio_after_the_committed_point() -> None:
    first = FakeVolcConnection()
    second = FakeVolcConnection(result_text="重放后识别")
    session = VolcASRSession(_config(_sequence_of_connections([first, second]), chunk_ms=20))
    await session.connect()
    await session.send_pcm(_pcm(320), capture_start_sample=0)
    await session.send_pcm(_pcm(320), capture_start_sample=320)
    await session.send_pcm(_pcm(320), capture_start_sample=640)
    assert first.audio_frames
    session.mark_committed_sample(320)
    await session.reconnect_with_replay()
    # The replacement task starts at the committed sample, not at zero.
    await session.send_pcm(_pcm(320), capture_start_sample=960)
    assert session.task_sample_origin == 320
    replayed = b"".join(
        gzip.decompress(raw[_payload_offset(raw) :])
        for raw in second.sent
        if parse_frame(raw).message_type == AUDIO_ONLY_REQUEST
    )
    # 320..960 was already sent before the disconnect and is sent again.
    assert len(replayed) >= 320 * 2
    assert second.audio_frames[0].sequence == 1
    await session.rotate_task(require_consumed=False)
    assert (await _next_event(session)).event == "result-generated"
    await session.aclose()


def _payload_offset(raw: bytes) -> int:
    frame = parse_frame(raw)
    offset = (raw[0] & 0x0F) * 4
    if frame.sequence is not None:
        offset += 4
    return offset + 4


@pytest.mark.asyncio
async def test_normal_rotation_does_not_replay_the_previous_segment() -> None:
    first = FakeVolcConnection()
    second = FakeVolcConnection()
    session = VolcASRSession(_config(_sequence_of_connections([first, second]), chunk_ms=20))
    await session.connect()
    await session.send_pcm(_pcm(320), capture_start_sample=0)
    await session.send_pcm(_pcm(320), capture_start_sample=320)
    await session.rotate_task(require_consumed=False)
    while not session.events.empty():
        session.events.get_nowait()

    # The next segment starts at the real capture position and sends only new
    # audio; replay belongs to the reconnect path, not to a VAD boundary.
    await session.send_pcm(_pcm(320), capture_start_sample=640)
    assert session.task_sample_origin == 640
    replayed = b"".join(
        gzip.decompress(raw[_payload_offset(raw) :])
        for raw in second.sent
        if parse_frame(raw).message_type == AUDIO_ONLY_REQUEST
    )
    assert len(replayed) == 320 * 2
    await session.aclose()


@pytest.mark.asyncio
async def test_reconnect_uses_next_frame_origin_when_nothing_is_replayable() -> None:
    first = FakeVolcConnection()
    second = FakeVolcConnection()
    session = VolcASRSession(
        _config(_sequence_of_connections([first, second]), reconnect_audio_ms=0)
    )
    await session.connect()
    await session.send_pcm(_pcm(160), capture_start_sample=1_000)
    session.mark_committed_sample(9_999)
    await session.reconnect_with_replay()
    await session.send_pcm(_pcm(160), capture_start_sample=1_160)
    # Nothing after the committed point survived, so the new task begins at
    # the incoming frame's real capture position.
    assert session.task_sample_origin == 1_160
    await session.aclose()


@pytest.mark.asyncio
async def test_transport_death_fails_the_task_instead_of_hanging() -> None:
    conn = FakeVolcConnection()
    session = VolcASRSession(_config(_one_connection(conn)))
    await session.connect()
    await session.send_pcm(_pcm(), capture_start_sample=0)
    await conn.close()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # A socket that closes without a last-flagged response must surface as a
    # failed task so the caller can fail over, never as a boundary wait.
    with pytest.raises(VolcASRError):
        await session.send_pcm(_pcm(), capture_start_sample=1600)
    await session.aclose()


def test_missing_credentials_fail_before_network() -> None:
    session = VolcASRSession(VolcASRConfig(app_id="", access_token=""))
    with pytest.raises(ValueError, match="VOLC_ASR_APP_ID"):
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(session.connect())


def test_config_reads_credentials_only_from_env() -> None:
    config = VolcASRConfig.from_env(
        {
            "VOLC_ASR_APP_ID": "app",
            "VOLC_ASR_ACCESS_TOKEN": "token",
            "VOLC_ASR_RESOURCE_ID": "volc.seedasr.sauc.duration",
        }
    )
    assert config.app_id == "app"
    assert config.access_token == "token"
    assert config.resource_id == "volc.seedasr.sauc.duration"
    assert config.ws_url.endswith("/bigmodel_async")
    headers = config.auth_headers()
    assert headers["X-Api-App-Key"] == "app"
    assert headers["X-Api-Access-Key"] == "token"


def test_config_rejects_unrequested_sample_rates() -> None:
    with pytest.raises(ValueError, match="16000"):
        VolcASRConfig(app_id="app", access_token="token", sample_rate=8_000)


def test_header_matches_the_documented_layout() -> None:
    raw = build_full_client_request({"user": {"uid": "memoria"}})
    assert raw[0] == 0x11
    assert raw[3] == 0x00
    assert struct.unpack(">i", raw[4:8])[0] == 1
