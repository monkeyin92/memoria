"""Volcengine streaming session against a scripted in-process WebSocket."""

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
    VolcEvent,
    parse_frame,
)


class FakeVolcConnection:
    def __init__(
        self,
        *,
        result_text: str = "你好",
        fail_task: bool = False,
    ) -> None:
        self.sent: list[bytes] = []
        self.fail_task = fail_task
        self.result_text = result_text
        self.closed = False
        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def send(self, data: bytes | str) -> None:
        raw = data.encode() if isinstance(data, str) else data
        self.sent.append(raw)
        frame = parse_frame(raw)
        if frame.message_type != FULL_CLIENT_REQUEST:
            return
        if frame.event == VolcEvent.START_CONNECTION:
            self._push_event(VolcEvent.CONNECTION_STARTED)
        elif frame.event == VolcEvent.START_SESSION:
            self._push_event(VolcEvent.SESSION_STARTED)
        elif frame.event == VolcEvent.TASK_REQUEST:
            if self.fail_task:
                self._push_error(55_000_031)
                return
            self._push_event(VolcEvent.TASK_STARTED)
            self._push_result(self.result_text, definite=True)
        elif frame.event == VolcEvent.FINISH_SESSION:
            self._push_event(VolcEvent.TASK_FINISHED)

    def _push_event(self, event: VolcEvent) -> None:
        payload = gzip.compress(b"{}")
        header = bytes([0x11, (0x9 << 4) | 0x05, 0x11, 0x00])
        self._incoming.put_nowait(
            header
            + struct.pack(">i", 1)
            + struct.pack(">i", int(event))
            + struct.pack(">I", len(payload))
            + payload
        )

    def _push_result(self, text: str, *, definite: bool) -> None:
        payload = gzip.compress(
            json.dumps({"result": {"text": text, "definite": definite}}).encode()
        )
        header = bytes([0x11, (0x9 << 4) | 0x01, 0x11, 0x00])
        self._incoming.put_nowait(
            header + struct.pack(">i", 1) + struct.pack(">I", len(payload)) + payload
        )

    def _push_error(self, code: int) -> None:
        payload = gzip.compress(json.dumps({"error": "busy"}).encode())
        header = bytes([0x11, (0xF << 4), 0x11, 0x00])
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


def _config(conn: FakeVolcConnection, **overrides: Any) -> VolcASRConfig:
    async def connector(*args: Any, **kwargs: Any) -> FakeVolcConnection:
        assert kwargs["additional_headers"]["X-Api-App-Key"] == "app"
        assert kwargs["additional_headers"]["X-Api-Access-Key"] == "token"
        assert kwargs["additional_headers"]["X-Api-Resource-Id"] == "resource"
        return conn

    values: dict[str, Any] = {
        "app_id": "app",
        "access_token": "token",
        "resource_id": "resource",
        "connector": connector,
    }
    values.update(overrides)
    return VolcASRConfig(**values)


async def _next_event(session: VolcASRSession) -> Any:
    return await asyncio.wait_for(session.events.get(), timeout=1.0)


@pytest.mark.asyncio
async def test_connect_sends_handshake_and_emits_primary_result() -> None:
    conn = FakeVolcConnection(result_text="我想听故事")
    session = VolcASRSession(_config(conn))
    await session.connect()
    assert session.task_id
    assert session.task_epoch == 1

    event = await _next_event(session)
    assert event.event == "result-generated"
    assert event.sentence is not None
    assert event.sentence.text == "我想听故事"
    assert event.sentence.sentence_end is True
    await session.aclose()
    assert conn.closed is True


@pytest.mark.asyncio
async def test_send_pcm_batches_100ms_and_rotate_flushes_last_packet() -> None:
    conn = FakeVolcConnection(result_text="")
    session = VolcASRSession(_config(conn))
    await session.connect()
    await session.send_pcm(b"\x00\x00" * 1600)
    audio = [parse_frame(frame) for frame in conn.sent]
    audio = [frame for frame in audio if frame.message_type == AUDIO_ONLY_REQUEST]
    assert [frame.sequence for frame in audio] == [1]

    await session.send_pcm(b"\x00\x00" * 800, capture_start_sample=1600)
    audio = [parse_frame(frame) for frame in conn.sent]
    audio = [frame for frame in audio if frame.message_type == AUDIO_ONLY_REQUEST]
    assert [frame.sequence for frame in audio] == [1]

    await session.rotate_task(require_consumed=False)
    audio = [parse_frame(frame) for frame in conn.sent]
    audio = [frame for frame in audio if frame.message_type == AUDIO_ONLY_REQUEST]
    assert [frame.sequence for frame in audio] == [1, -2]
    assert session.rotation_pending is True
    assert session.segment_epoch == 2
    assert (await _next_event(session)).event == "task-finished"
    await session.aclose()


@pytest.mark.asyncio
async def test_rotate_without_audio_still_closes_provider_task() -> None:
    conn = FakeVolcConnection(result_text="")
    session = VolcASRSession(_config(conn))
    await session.connect()
    await session.rotate_task(require_consumed=False)
    assert (await _next_event(session)).event == "task-finished"
    assert not any(parse_frame(frame).message_type == AUDIO_ONLY_REQUEST for frame in conn.sent)
    await session.aclose()


@pytest.mark.asyncio
async def test_task_error_surfaces_task_failed_and_blocks_pcm() -> None:
    conn = FakeVolcConnection(fail_task=True)
    session = VolcASRSession(_config(conn))
    with pytest.raises(VolcASRError):
        await session.connect()
    await session.aclose()


@pytest.mark.asyncio
async def test_missing_credentials_fail_before_network() -> None:
    session = VolcASRSession(VolcASRConfig(app_id="", access_token=""))
    with pytest.raises(ValueError, match="VOLC_ASR_APP_ID"):
        await session.connect()


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
