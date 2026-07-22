"""Doubao bidirectional TTS integration against a local binary-protocol server."""

from __future__ import annotations

import asyncio

import pytest
from livekit.agents import APIConnectOptions
from livekit.agents.types import USERDATA_TIMED_TRANSCRIPT
from scripts import provider_smoke_test
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.doubao_tts import (
    DoubaoBeforeAudioError,
    DoubaoPCMContinuityError,
    DoubaoTTS,
    DoubaoTTSConfig,
    DoubaoTTSPool,
)
from services.agent.src.providers.doubao_voice_catalog import catalog_by_id
from services.agent.tests.integration.mock_servers import MockDoubaoServer

_VOICES = catalog_by_id()


def _config(server: MockDoubaoServer, **overrides: object) -> DoubaoTTSConfig:
    values: dict[str, object] = {
        "api_key": "test",
        "ws_url": server.ws_url,
        "speaker": _VOICES["warm_companion"].speaker_id,
        "pool_size": 1,
    }
    values.update(overrides)
    return DoubaoTTSConfig(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_incremental_tasks_and_two_sessions_reuse_one_connection() -> None:
    server = MockDoubaoServer()
    server.start()
    tts = DoubaoTTS(_config(server))
    try:
        await tts.pool.warm(1)
        first = await tts.synthesize_stream_text(
            ["你", "好"],
            fence=GenerationFence("reuse", 1, 1, 0),
        )
        second = await tts.synthesize_stream_text(
            ["再见"],
            fence=GenerationFence("reuse", 2, 2, 0),
        )

        assert first.pcm and first.words
        assert second.pcm and second.words
        assert server.connections == 1
        assert server.sessions == 2
        assert server.task_requests == [["你", "好"], ["再见"]]
    finally:
        await tts.aclose()
        server.stop()


@pytest.mark.asyncio
async def test_first_audio_timeout_retries_with_a_fresh_connection() -> None:
    server = MockDoubaoServer(scenario="slow_once")
    server.start()
    tts = DoubaoTTS(_config(server, first_audio_timeout_s=0.05))
    try:
        result = await tts.synthesize_stream_text(
            ["首包超时后重试"],
            fence=GenerationFence("retry", 1, 1, 0),
        )

        assert result.pcm and result.words
        assert server.connections == 2
    finally:
        await tts.aclose()
        server.stop()


@pytest.mark.asyncio
async def test_cancel_sends_cancel_session_and_clears_fence() -> None:
    server = MockDoubaoServer(scenario="slow")
    server.start()
    tts = DoubaoTTS(_config(server, first_audio_timeout_s=1.0))
    cancel = asyncio.Event()
    fence = GenerationFence("cancel", 1, 1, 0)
    try:
        await tts.pool.warm(1)
        task = asyncio.create_task(
            tts.synthesize_stream_text(["取消测试"], fence=fence, cancel_event=cancel)
        )
        for _ in range(100):
            if tts.pool.active_by_fence:
                break
            await asyncio.sleep(0.01)
        active = next(iter(tts.pool.active_by_fence.values()))
        cancel.set()
        result = await asyncio.wait_for(task, timeout=1)
        for _ in range(100):
            if server.canceled_sessions:
                break
            await asyncio.sleep(0.01)

        assert result.discarded
        assert active.cancel_sent
        assert server.canceled_sessions
        assert not tts.pool.active_by_fence
        assert tts.pool.discarded_count == 1
        for _ in range(100):
            if tts.pool.available_approx == 1:
                break
            await asyncio.sleep(0.01)
        assert tts.pool.available_approx == 1
        assert server.connections == 2

        server.scenario = "happy"
        resumed = await tts.synthesize_stream_text(
            ["补池连接复用"],
            fence=GenerationFence("cancel", 2, 2, 0),
        )
        assert resumed.pcm and resumed.words
        assert server.connections == 2
    finally:
        await tts.aclose()
        server.stop()


@pytest.mark.asyncio
async def test_provider_smoke_synthesizes_then_cancels_an_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = MockDoubaoServer(scenario="slow_after_first")
    server.start()
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DOUBAO_TTS_MOCK_WS_URL", server.ws_url)
    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "test")
    monkeypatch.setenv("DOUBAO_TTS_VOICE_PROFILE", "warm_companion")
    try:
        pcm_16k = await provider_smoke_test.smoke_doubao()

        assert pcm_16k
        assert server.sessions == 2
        assert len(server.canceled_sessions) == 1
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_pool_discard_refills_in_background_and_shutdown_waits_for_it() -> None:
    server = MockDoubaoServer()
    server.start()
    pool = DoubaoTTSPool(_config(server))
    try:
        await pool.warm(1)
        conn = await pool.acquire()

        await pool.discard(conn, reason="error")

        for _ in range(100):
            if pool.available_approx == 1:
                break
            await asyncio.sleep(0.01)
        assert pool.available_approx == 1
        assert server.connections == 2
    finally:
        await pool.aclose()
        assert not pool._refill_tasks
        server.stop()


@pytest.mark.asyncio
async def test_voice_is_snapshotted_before_waiting_for_a_connection() -> None:
    server = MockDoubaoServer()
    server.start()
    config = _config(server)
    pool = DoubaoTTSPool(config)
    tts = DoubaoTTS(config, pool)
    try:
        await pool.warm(1)
        held = await pool.acquire()
        synthesis = asyncio.create_task(
            tts.synthesize_stream_text(
                ["音色快照"],
                fence=GenerationFence("voice-snapshot", 1, 1, 0),
            )
        )
        await asyncio.sleep(0)
        tts.apply_voice_profile(
            model="seed-tts-2.0",
            voice=_VOICES["bright_peer"].speaker_id,
        )
        await pool.release(held)
        result = await synthesis

        assert result.pcm
        assert server.speakers == [_VOICES["warm_companion"].speaker_id]
    finally:
        await tts.aclose()
        server.stop()


@pytest.mark.asyncio
async def test_livekit_stream_emits_audio_and_reports_word_alignment() -> None:
    server = MockDoubaoServer(scenario="split_pcm")
    server.start()
    tts = DoubaoTTS(_config(server))
    fence = GenerationFence("livekit", 1, 1, 0)
    alignment: list[str] = []
    tts.bind_fence(fence)
    tts.set_alignment_callback(lambda _fence, _utterance_id, status: alignment.append(status))
    try:
        await tts.pool.warm(1)
        async with tts.stream(conn_options=APIConnectOptions(max_retry=0)) as stream:
            stream.push_text("增量")
            stream.push_text("流式")
            stream.end_input()
            events = [event async for event in stream]

        assert events
        assert sum(event.frame.duration for event in events) > 0
        assert alignment == ["started", "ok"]
        assert server.task_requests == [["增量", "流式"]]
    finally:
        await tts.aclose()
        server.stop()


@pytest.mark.asyncio
async def test_livekit_stream_preserves_pcm_across_odd_transport_chunks() -> None:
    server = MockDoubaoServer(scenario="split_pcm_odd")
    server.start()
    tts = DoubaoTTS(_config(server))
    traces: list[tuple[str, str, dict[str, object] | None]] = []
    tts.set_trace_callback(lambda name, status, detail: traces.append((name, status, detail)))
    try:
        await tts.pool.warm(1)
        async with tts.stream(conn_options=APIConnectOptions(max_retry=0)) as stream:
            stream.push_text("跨分片 PCM")
            stream.end_input()
            events = [event async for event in stream]

        emitted_pcm = b"".join(bytes(event.frame.data) for event in events)
        assert emitted_pcm == server.pcm
        assert len(emitted_pcm) % 2 == 0
        summary = next(detail for name, _, detail in traces if name == "doubao_pcm_summary")
        assert summary == {
            "pcm_bytes": len(server.pcm),
            "chunk_count": 4,
            "odd_chunk_count": 2,
            "min_chunk_bytes": 1,
            "max_chunk_bytes": len(server.pcm) - 257,
        }
    finally:
        await tts.aclose()
        server.stop()


@pytest.mark.asyncio
async def test_synthesize_rejects_odd_total_pcm_without_returning_residual_byte() -> None:
    server = MockDoubaoServer(scenario="odd_pcm")
    server.start()
    tts = DoubaoTTS(_config(server))
    traces: list[tuple[str, str, dict[str, object] | None]] = []
    tts.set_trace_callback(lambda name, status, detail: traces.append((name, status, detail)))
    try:
        with pytest.raises(DoubaoPCMContinuityError, match="PCM total length is odd"):
            await tts.synthesize_stream_text(
                ["奇数 PCM"],
                fence=GenerationFence("odd-pcm", 1, 1, 0),
            )
        summary = next(detail for name, _, detail in traces if name == "doubao_pcm_summary")
        assert summary is not None and summary["pcm_bytes"] % 2 == 1
    finally:
        await tts.aclose()
        server.stop()


@pytest.mark.asyncio
async def test_livekit_stream_emits_only_final_scaled_word_alignment() -> None:
    server = MockDoubaoServer(scenario="scaled_ts")
    server.start()
    tts = DoubaoTTS(_config(server))
    fence = GenerationFence("livekit-scaled", 1, 1, 0)
    alignment: list[str] = []
    tts.bind_fence(fence)
    tts.set_alignment_callback(lambda _fence, _utterance_id, status: alignment.append(status))
    try:
        await tts.pool.warm(1)
        async with tts.stream(conn_options=APIConnectOptions(max_retry=0)) as stream:
            stream.push_text("字幕缩放")
            stream.end_input()
            events = [event async for event in stream]

        transcripts = [
            word
            for event in events
            for word in event.frame.userdata.get(USERDATA_TIMED_TRANSCRIPT, [])
        ]
        audio_duration_s = sum(event.frame.duration for event in events)
        assert transcripts
        assert transcripts[-1].end_time == pytest.approx(audio_duration_s, abs=0.001)
        assert alignment == ["started", "scaled"]
    finally:
        await tts.aclose()
        server.stop()


@pytest.mark.asyncio
async def test_livekit_stream_starts_audio_timeout_after_first_text() -> None:
    server = MockDoubaoServer()
    server.start()
    tts = DoubaoTTS(_config(server, first_audio_timeout_s=0.05))
    try:
        await tts.pool.warm(1)
        async with tts.stream(conn_options=APIConnectOptions(max_retry=0)) as stream:
            await asyncio.sleep(0.1)
            stream.push_text("延迟到达的首个文本")
            stream.end_input()
            events = [event async for event in stream]

        assert events
        assert server.connections == 1
        assert server.task_requests == [["延迟到达的首个文本"]]
    finally:
        await tts.aclose()
        server.stop()


@pytest.mark.asyncio
async def test_livekit_stream_rejects_empty_input_without_waiting_for_audio_timeout() -> None:
    server = MockDoubaoServer()
    server.start()
    tts = DoubaoTTS(_config(server, first_audio_timeout_s=1.0, total_timeout_s=20.0))
    stream = tts.stream(conn_options=APIConnectOptions(max_retry=0))
    try:
        await tts.pool.warm(1)
        stream.end_input()

        async def consume() -> None:
            async for _ in stream:
                pass

        with pytest.raises(DoubaoBeforeAudioError, match="empty-input"):
            await asyncio.wait_for(consume(), timeout=0.5)
        assert server.task_requests == [[]]
    finally:
        await stream.aclose()
        await tts.aclose()
        server.stop()


@pytest.mark.asyncio
async def test_livekit_stream_retries_first_audio_timeout() -> None:
    server = MockDoubaoServer(scenario="slow_once")
    server.start()
    tts = DoubaoTTS(_config(server, first_audio_timeout_s=0.05))
    try:
        async with tts.stream(
            conn_options=APIConnectOptions(max_retry=1, retry_interval=0.01)
        ) as stream:
            stream.push_text("自动重试")
            stream.end_input()
            events = [event async for event in stream]

        assert events
        assert server.connections == 2
    finally:
        await tts.aclose()
        server.stop()


@pytest.mark.asyncio
async def test_livekit_stream_does_not_replay_after_audio_without_subtitles() -> None:
    server = MockDoubaoServer(scenario="empty_ts")
    server.start()
    tts = DoubaoTTS(_config(server))
    try:
        stream = tts.stream(conn_options=APIConnectOptions(max_retry=1, retry_interval=0.01))
        stream.push_text("已经开始播放的句子不能整句重来")
        stream.end_input()
        with pytest.raises(Exception, match="empty-timestamps"):
            async for _ in stream:
                pass
        await stream.aclose()

        assert server.sessions == 1
        assert server.task_requests == [["已经开始播放的句子不能整句重来"]]
    finally:
        await tts.aclose()
        server.stop()
