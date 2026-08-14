"""FunASR protocol integration against local mock WebSocket."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from livekit.agents import APIConnectionError
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession
from services.agent.tests.integration.mock_servers import MockFunASRServer


@pytest.fixture
def funasr_server() -> Iterator[MockFunASRServer]:
    srv = MockFunASRServer(scenario="happy")
    srv.start()
    yield srv
    srv.stop()


@pytest.mark.asyncio
async def test_funasr_happy_path(funasr_server: MockFunASRServer) -> None:
    cfg = FunASRConfig(api_key="test", ws_url=funasr_server.ws_url)
    session = FunASRSession(cfg)
    await session.connect()
    await session.send_pcm(b"\x00\x00" * 2000)
    await session.finish()

    finals = []
    interims = []
    finished = False
    for _ in range(20):
        try:
            ev = await asyncio.wait_for(session.events.get(), timeout=2)
        except TimeoutError:
            break
        if ev.event == "result-generated" and ev.sentence:
            if ev.sentence.sentence_end:
                finals.append(ev.sentence)
            else:
                interims.append(ev.sentence)
        if ev.event == "task-finished":
            finished = True
            break
    await session.aclose()
    assert interims or finals
    assert finals
    assert "实时语音测试" in finals[-1].text
    assert finals[-1].words
    assert finished


@pytest.mark.asyncio
async def test_funasr_task_failed() -> None:
    srv = MockFunASRServer(scenario="fail")
    srv.start()
    try:
        cfg = FunASRConfig(api_key="test", ws_url=srv.ws_url)
        session = FunASRSession(cfg)
        await session.connect()
        failed_ev = None
        for _ in range(5):
            ev = await asyncio.wait_for(session.events.get(), timeout=2)
            if ev.event == "task-failed":
                failed_ev = ev
                break
        assert failed_ev is not None
        assert session.failed is True
        await session.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_funasr_interim_rewrite() -> None:
    srv = MockFunASRServer(scenario="interim_rewrite")
    srv.start()
    try:
        cfg = FunASRConfig(api_key="test", ws_url=srv.ws_url)
        session = FunASRSession(cfg)
        await session.connect()
        await session.send_pcm(b"\x00\x00" * 2000)
        await session.finish()
        texts = []
        for _ in range(20):
            try:
                ev = await asyncio.wait_for(session.events.get(), timeout=2)
            except TimeoutError:
                break
            if ev.sentence:
                texts.append(ev.sentence.text)
            if ev.event == "task-finished":
                break
        await session.aclose()
        assert any("想定" in t for t in texts)
        assert any("订下周" in t for t in texts)
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_funasr_runtime_disconnect_reconnects_and_replays_ring() -> None:
    srv = MockFunASRServer(scenario="disconnect_once")
    srv.start()
    try:
        session = FunASRSession(
            FunASRConfig(api_key="test", ws_url=srv.ws_url, result_timeout_s=1.0)
        )
        await session.connect()
        await session.send_pcm(b"\x00\x00" * 3200)
        await session.finish()

        finals = []
        for _ in range(20):
            ev = await asyncio.wait_for(session.events.get(), timeout=2)
            if ev.event == "result-generated" and ev.sentence and ev.sentence.sentence_end:
                finals.append(ev.sentence)
            if ev.event == "task-finished":
                break
        await session.aclose()

        assert len(srv.tasks_started) == 2
        assert srv.pcm_by_connection[1] >= srv.pcm_by_connection[0]
        assert finals and "实时语音测试" in finals[-1].text
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_funasr_task_start_timeout_closes_socket_and_has_no_recv_task() -> None:
    srv = MockFunASRServer(scenario="handshake_timeout")
    srv.start()
    try:
        session = FunASRSession(
            FunASRConfig(api_key="test", ws_url=srv.ws_url, connect_timeout_s=0.2)
        )
        with pytest.raises(TimeoutError):
            await session._open_once()
        assert len(srv.tasks_started) == 1
        assert await asyncio.to_thread(srv.connection_closed.wait, 1.0)

        assert session._recv_task is None
        assert srv.connections_closed >= 1
        await session.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_funasr_task_boundary_timeout_is_not_extended_by_heartbeats() -> None:
    srv = MockFunASRServer(scenario="heartbeat_stall")
    srv.start()
    try:
        session = FunASRSession(
            FunASRConfig(
                api_key="test",
                ws_url=srv.ws_url,
                result_timeout_s=0.2,
            )
        )
        await session.connect()
        await session.send_pcm(b"\x00\x00" * 2000)
        started = asyncio.get_running_loop().time()
        with pytest.raises(APIConnectionError, match="task boundary timed out"):
            await session.rotate_task()
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 1.0
        assert len(srv.tasks_started) == 1
        await session.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_funasr_task_rotation_can_handoff_queued_tail_to_media_adapter() -> None:
    srv = MockFunASRServer(scenario="task_reuse")
    srv.start()
    try:
        session = FunASRSession(FunASRConfig(api_key="test", ws_url=srv.ws_url))
        await session.connect()
        first_task_id = session.task_id
        await session.send_pcm(b"\x01\x00" * 2000)

        await session.rotate_task(require_consumed=False)

        assert session.task_id != first_task_id
        assert session.task_epoch == 2
        queued = []
        while not session.events.empty():
            queued.append(session.events.get_nowait())
        assert any(
            event.event == "result-generated" and event.task_id == first_task_id for event in queued
        )
        assert any(
            event.event == "task-finished" and event.task_id == first_task_id for event in queued
        )
        await session.aclose()
    finally:
        srv.stop()
