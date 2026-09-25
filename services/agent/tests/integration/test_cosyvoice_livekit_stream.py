from __future__ import annotations

import asyncio
import struct

import pytest
from livekit.agents import APIConnectOptions
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
from services.agent.tests.integration.mock_servers import MockCosyVoiceServer


@pytest.mark.asyncio
async def test_livekit_stream_emits_audio_and_timed_transcript() -> None:
    srv = MockCosyVoiceServer()
    srv.start()
    try:
        tts = CosyVoiceTTS(CosyVoiceConfig(api_key="test", ws_url=srv.ws_url, pool_size=1))
        fence = GenerationFence("s", 1, 1, 0)
        alignment_events: list[tuple[GenerationFence, str, str]] = []
        tts.bind_fence(fence)
        tts.set_alignment_callback(
            lambda event_fence, utterance_id, status: alignment_events.append(
                (event_fence, utterance_id, status)
            )
        )
        await tts.pool.warm(1)
        events = []
        async with tts.stream(conn_options=APIConnectOptions(max_retry=0)) as stream:
            stream.push_text("你好，这是流式测试。")
            stream.end_input()
            async for event in stream:
                events.append(event)
        assert events
        assert sum(event.frame.duration for event in events) > 0
        assert [status for _, _, status in alignment_events] == ["started", "degraded"]
        assert all(event_fence.matches(fence) for event_fence, _, _ in alignment_events)
        assert len({utterance_id for _, utterance_id, _ in alignment_events}) == 1
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_livekit_stream_preserves_pcm_across_transport_chunks() -> None:
    srv = MockCosyVoiceServer(scenario="split_pcm")
    srv.start()
    try:
        tts = CosyVoiceTTS(CosyVoiceConfig(api_key="test", ws_url=srv.ws_url, pool_size=1))
        events = []
        async with tts.stream(conn_options=APIConnectOptions(max_retry=0)) as stream:
            stream.push_text("连续 PCM")
            stream.end_input()
            async for event in stream:
                events.append(event)

        expected = b"".join(struct.pack("<480h", *([level] * 480)) for level in (1200, 16000))
        assert b"".join(bytes(event.frame.data) for event in events) == expected
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_livekit_stream_first_audio_timeout_retries_with_buffered_text() -> None:
    srv = MockCosyVoiceServer(scenario="slow_once")
    srv.start()
    try:
        tts = CosyVoiceTTS(
            CosyVoiceConfig(
                api_key="test",
                ws_url=srv.ws_url,
                pool_size=1,
                first_audio_timeout_s=0.05,
            )
        )
        async with tts.stream(
            conn_options=APIConnectOptions(max_retry=1, retry_interval=0.01)
        ) as stream:
            stream.push_text("自动重试")
            stream.end_input()
            events = [event async for event in stream]
        assert events
        assert srv.connections == 2
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_livekit_stream_clone_failure_replays_buffer_with_baseline_voice() -> None:
    srv = MockCosyVoiceServer(scenario="slow_once")
    srv.start()
    try:
        tts = CosyVoiceTTS(
            CosyVoiceConfig(
                api_key="test",
                ws_url=srv.ws_url,
                model="cosyvoice-v3.5-flash",
                voice="cosyvoice-v3.5-flash-vd-warmboy-baseline",
                pool_size=1,
                first_audio_timeout_s=0.05,
            )
        )
        tts.apply_voice_profile(
            model="cosyvoice-v3.5-flash",
            voice="cosyvoice-v3.5-flash-clone-owner001",
        )
        async with tts.stream(
            conn_options=APIConnectOptions(max_retry=1, retry_interval=0.01)
        ) as stream:
            stream.push_text("流式复刻失败后继续回答")
            stream.end_input()
            events = [event async for event in stream]

        assert events
        assert [request["payload"]["parameters"]["voice"] for request in srv.run_requests] == [
            "cosyvoice-v3.5-flash-clone-owner001",
            "cosyvoice-v3.5-flash-vd-warmboy-baseline",
        ]
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_livekit_stream_empty_timestamps_discards_connection() -> None:
    srv = MockCosyVoiceServer(scenario="empty_ts")
    srv.start()
    try:
        tts = CosyVoiceTTS(CosyVoiceConfig(api_key="test", ws_url=srv.ws_url, pool_size=1))
        stream = tts.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("无时间戳")
        stream.end_input()
        with pytest.raises(Exception, match="empty-timestamps"):
            async for _ in stream:
                pass
        await stream.aclose()
        assert tts.pool.discarded_count == 1
        assert not tts.pool.active_by_fence
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_livekit_stream_never_retries_whole_sentence_after_audio_output() -> None:
    srv = MockCosyVoiceServer(scenario="empty_ts")
    srv.start()
    try:
        tts = CosyVoiceTTS(
            CosyVoiceConfig(
                api_key="test",
                ws_url=srv.ws_url,
                model="cosyvoice-v3.5-flash",
                voice="cosyvoice-v3.5-flash-vd-warmboy-baseline",
                pool_size=1,
            )
        )
        tts.apply_voice_profile(
            model="cosyvoice-v3.5-flash",
            voice="cosyvoice-v3.5-flash-clone-owner001",
        )
        stream = tts.stream(conn_options=APIConnectOptions(max_retry=1))
        stream.push_text("已经开始播放的句子不能整句重来")
        stream.end_input()

        with pytest.raises(Exception, match="empty-timestamps"):
            async for _ in stream:
                pass
        await stream.aclose()

        assert len(srv.run_requests) == 1
        assert (
            srv.run_requests[0]["payload"]["parameters"]["voice"]
            == "cosyvoice-v3.5-flash-clone-owner001"
        )
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_livekit_stream_cancel_closes_bound_connection() -> None:
    srv = MockCosyVoiceServer(scenario="slow")
    srv.start()
    try:
        tts = CosyVoiceTTS(
            CosyVoiceConfig(
                api_key="test",
                ws_url=srv.ws_url,
                pool_size=1,
                first_audio_timeout_s=1.0,
            )
        )
        fence = GenerationFence("s", 1, 1, 0)
        tts.bind_fence(fence)
        stream = tts.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("取消流")
        stream.end_input()

        async def collect() -> None:
            async for _ in stream:
                pass

        task = asyncio.create_task(collect())
        for _ in range(100):
            if tts.pool.active_by_fence:
                break
            await asyncio.sleep(0.01)
        assert tts.pool.active_by_fence
        await stream.aclose()
        await task
        assert tts.pool.discarded_count == 1
        assert not tts.pool.active_by_fence
        await tts.aclose()
    finally:
        srv.stop()
