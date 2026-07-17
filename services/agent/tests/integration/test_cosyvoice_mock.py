"""CosyVoice mock integration: PCM, timestamps, cancel discards connection."""

from __future__ import annotations

import asyncio

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoicePool, CosyVoiceTTS
from services.agent.tests.integration.mock_servers import MockCosyVoiceServer


@pytest.mark.asyncio
async def test_cosyvoice_happy_pcm_and_words() -> None:
    srv = MockCosyVoiceServer(scenario="happy")
    srv.start()
    try:
        cfg = CosyVoiceConfig(api_key="test", ws_url=srv.ws_url, pool_size=1)
        tts = CosyVoiceTTS(cfg)
        await tts.pool.warm(1)
        fence = GenerationFence("s", 1, 1, 0)
        result = await tts.synthesize_stream_text(
            ["你好，这是语音合成测试。"],
            fence=fence,
        )
        assert len(result.pcm) > 0
        assert result.words
        assert result.discarded is False
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_cosyvoice_cancel_discards_connection() -> None:
    srv = MockCosyVoiceServer(scenario="happy")
    srv.start()
    try:
        cfg = CosyVoiceConfig(api_key="test", ws_url=srv.ws_url, pool_size=1)
        pool = CosyVoicePool(cfg)
        tts = CosyVoiceTTS(cfg, pool)
        await pool.warm(1)
        avail_before = pool.available_approx
        fence = GenerationFence("s", 1, 1, 0)
        cancel = asyncio.Event()
        cancel.set()  # cancel immediately after acquire
        result = await tts.synthesize_stream_text(
            ["不应该完整播完"],
            fence=fence,
            cancel_event=cancel,
        )
        assert result.discarded is True
        assert pool.discarded_count >= 1
        # cancelled connection not returned as healthy reuse of same conn
        await tts.aclose()
        _ = avail_before
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_cosyvoice_late_timestamps() -> None:
    srv = MockCosyVoiceServer(scenario="late_ts")
    srv.start()
    try:
        cfg = CosyVoiceConfig(api_key="test", ws_url=srv.ws_url, pool_size=1)
        tts = CosyVoiceTTS(cfg)
        await tts.pool.warm(1)
        result = await tts.synthesize_stream_text(
            ["晚到时间戳"],
            fence=GenerationFence("s", 1, 1, 0),
        )
        assert result.pcm
        assert result.alignment_status in ("scaled", "degraded", "ok")
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["slow_once", "empty_ts_once"])
async def test_cosyvoice_retries_once_with_fresh_connection(scenario: str) -> None:
    srv = MockCosyVoiceServer(scenario=scenario)
    srv.start()
    try:
        cfg = CosyVoiceConfig(
            api_key="test",
            ws_url=srv.ws_url,
            pool_size=1,
            first_audio_timeout_s=0.05,
        )
        tts = CosyVoiceTTS(cfg)
        result = await tts.synthesize_stream_text(
            ["重试语音"],
            fence=GenerationFence("s", 1, 1, 0),
        )
        assert result.pcm
        assert result.words
        assert srv.connections == 2
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_pool_shutdown_does_not_refill_and_clears_active_binding() -> None:
    srv = MockCosyVoiceServer()
    srv.start()
    try:
        cfg = CosyVoiceConfig(api_key="test", ws_url=srv.ws_url, pool_size=1)
        pool = CosyVoicePool(cfg)
        await pool.warm(1)
        conn = await pool.acquire()
        fence = GenerationFence("s", 1, 1, 0)
        pool.bind_active(fence, conn)
        await pool.aclose()
        await asyncio.sleep(0)

        assert not pool.active_by_fence
        assert pool.available_approx == 0
        assert not pool._refill_tasks
        assert srv.connections == 1
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_cosyvoice_midflight_cancel_closes_ws_and_clears_binding() -> None:
    srv = MockCosyVoiceServer(scenario="slow")
    srv.start()
    try:
        cfg = CosyVoiceConfig(
            api_key="test",
            ws_url=srv.ws_url,
            pool_size=1,
            first_audio_timeout_s=1.0,
        )
        tts = CosyVoiceTTS(cfg)
        cancel = asyncio.Event()
        fence = GenerationFence("s", 1, 1, 0)
        task = asyncio.create_task(
            tts.synthesize_stream_text(["取消测试"], fence=fence, cancel_event=cancel)
        )
        await asyncio.sleep(0.05)
        cancel.set()
        result = await asyncio.wait_for(task, timeout=1)

        assert result.discarded
        assert not tts.pool.active_by_fence
        assert tts.pool.discarded_count == 1
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_pool_discard_refills_in_background_and_shutdown_waits_for_it() -> None:
    srv = MockCosyVoiceServer()
    srv.start()
    try:
        cfg = CosyVoiceConfig(api_key="test", ws_url=srv.ws_url, pool_size=1)
        pool = CosyVoicePool(cfg)
        await pool.warm(1)
        conn = await pool.acquire()
        await pool.discard(conn, reason="error")
        for _ in range(50):
            if pool.available_approx == 1:
                break
            await asyncio.sleep(0.01)
        assert pool.available_approx == 1
        assert srv.connections == 2
        await pool.aclose()
        assert not pool._refill_tasks
    finally:
        srv.stop()
