"""CosyVoice mock integration: PCM, timestamps, cancel discards connection."""

from __future__ import annotations

import asyncio

import pytest
from livekit.agents import APIConnectionError
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoicePool, CosyVoiceTTS
from services.agent.tests.integration.mock_servers import MockCosyVoiceServer
from services.common.companions import DESIGNED_VOICE_SPEAKERS
from services.common.voice_identity import TTS_MODEL

BASELINE_VOICE = DESIGNED_VOICE_SPEAKERS["warm_companion"]
CLONE_VOICE = "qwen-audio-3.1-tts-flash-owner001-abc123"


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
async def test_clone_failure_before_audio_falls_back_to_designed_baseline_once() -> None:
    srv = MockCosyVoiceServer(scenario="slow_once")
    srv.start()
    try:
        cfg = CosyVoiceConfig(
            api_key="test",
            ws_url=srv.ws_url,
            voice=BASELINE_VOICE,
            pool_size=1,
            first_audio_timeout_s=0.05,
        )
        tts = CosyVoiceTTS(cfg)
        tts.apply_voice_profile(
            model=TTS_MODEL,
            voice=CLONE_VOICE,
            profile_id="pv_owner001",
            voice_kind="personal",
        )

        result = await tts.synthesize_stream_text(
            ["复刻音色失败后继续回答。"],
            fence=GenerationFence("clone-fallback", 1, 1, 0),
        )

        assert result.pcm
        assert [request["payload"]["parameters"]["voice"] for request in srv.run_requests] == [
            CLONE_VOICE,
            BASELINE_VOICE,
        ]
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_clone_task_failure_before_audio_also_falls_back_to_baseline() -> None:
    srv = MockCosyVoiceServer(scenario="fail_once")
    srv.start()
    try:
        cfg = CosyVoiceConfig(
            api_key="test",
            ws_url=srv.ws_url,
            voice=BASELINE_VOICE,
            pool_size=1,
        )
        tts = CosyVoiceTTS(cfg)
        tts.apply_voice_profile(
            model=TTS_MODEL,
            voice=CLONE_VOICE,
            profile_id="pv_owner001",
            voice_kind="personal",
        )

        result = await tts.synthesize_stream_text(
            ["任务启动失败也要降级。"],
            fence=GenerationFence("clone-task-fallback", 1, 1, 0),
        )

        assert result.pcm
        assert [request["payload"]["parameters"]["voice"] for request in srv.run_requests] == [
            CLONE_VOICE,
            BASELINE_VOICE,
        ]
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_direct_synthesis_keeps_the_voice_selected_before_pool_wait() -> None:
    srv = MockCosyVoiceServer(scenario="happy")
    srv.start()
    try:
        cfg = CosyVoiceConfig(
            api_key="test",
            ws_url=srv.ws_url,
            voice="qwen-audio-3.1-tts-flash-owner001-before-wait",
            pool_size=1,
        )
        pool = CosyVoicePool(cfg)
        tts = CosyVoiceTTS(cfg, pool)
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
            model=TTS_MODEL,
            voice="qwen-audio-3.1-tts-flash-owner001-after-wait",
            profile_id="pv_owner001",
            voice_kind="personal",
        )
        await pool.release(held)

        result = await synthesis

        assert result.pcm
        assert srv.run_requests[-1]["payload"]["parameters"]["voice"] == (
            "qwen-audio-3.1-tts-flash-owner001-before-wait"
        )
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
        await tts.pool.warm(1)
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
        for _ in range(100):
            if tts.pool.available_approx == 1:
                break
            await asyncio.sleep(0.01)
        assert tts.pool.available_approx == 1
        assert srv.connections == 2

        srv.scenario = "happy"
        resumed = await tts.synthesize_stream_text(
            ["补池连接复用"],
            fence=GenerationFence("s", 2, 2, 0),
        )
        assert resumed.pcm and resumed.words
        assert srv.connections == 2
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
        for _ in range(100):
            if pool.available_approx == 1:
                break
            await asyncio.sleep(0.01)
        assert pool.available_approx == 1
        assert srv.connections == 2
        await pool.aclose()
        assert not pool._refill_tasks
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_cosyvoice_batch_renews_the_stall_watchdog_across_delayed_chunks() -> None:
    """The batch path must not keep an absolute wall clock.

    ``total_timeout_s`` is 0.08s while the second transport chunk arrives after
    0.05s: each gap is inside the stall budget, but the elapsed wall clock
    exceeds the initial total budget. An absolute wall clock fails here even
    though the provider keeps producing audio.
    """
    srv = MockCosyVoiceServer(scenario="split_pcm", chunk_delay_s=0.06)
    srv.start()
    try:
        cfg = CosyVoiceConfig(
            api_key="test",
            ws_url=srv.ws_url,
            pool_size=1,
            first_audio_timeout_s=0.2,
            total_timeout_s=0.08,
        )
        tts = CosyVoiceTTS(cfg)
        await tts.pool.warm(1)
        result = await tts.synthesize_stream_text(
            ["今天天气很好，温度二十度。"],
            fence=GenerationFence("cosy-renewal", 1, 1, 0),
        )
        assert result.pcm
        assert result.words
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_clone_missing_timestamps_retries_without_changing_voice() -> None:
    """A post-audio retry keeps the requested voice.

    ``empty_ts_once`` fails only AFTER the sentence audio exists (the word
    timestamps are missing).  Re-synthesizing may recover the alignment, but it
    must not fall back to the designed voice: the caller asked for the clone, and
    switching speaker after audio would deliver another voice for that sentence.
    """

    srv = MockCosyVoiceServer(scenario="empty_ts_once")
    srv.start()
    try:
        cfg = CosyVoiceConfig(
            api_key="test",
            ws_url=srv.ws_url,
            voice=BASELINE_VOICE,
            pool_size=1,
        )
        tts = CosyVoiceTTS(cfg)
        tts.apply_voice_profile(
            model=TTS_MODEL,
            voice=CLONE_VOICE,
            profile_id="pv_owner001",
            voice_kind="personal",
        )

        result = await tts.synthesize_stream_text(
            ["缺时间戳也要保持音色。"],
            fence=GenerationFence("clone-ts-retry", 1, 1, 0),
        )

        assert result.pcm
        assert result.words
        assert srv.connections == 2
        assert [request["payload"]["parameters"]["voice"] for request in srv.run_requests] == [
            CLONE_VOICE,
            CLONE_VOICE,
        ]
        await tts.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_stall_after_audio_ends_the_attempt_without_replaying_the_sentence() -> None:
    """Audio exists, the provider goes silent: bounded failure, no second attempt."""

    srv = MockCosyVoiceServer(scenario="stall_after_pcm")
    srv.start()
    try:
        cfg = CosyVoiceConfig(
            api_key="test",
            ws_url=srv.ws_url,
            pool_size=1,
            first_audio_timeout_s=0.5,
            total_timeout_s=0.15,
        )
        tts = CosyVoiceTTS(cfg)
        await tts.pool.warm(1)
        with pytest.raises(APIConnectionError, match="total-timeout"):
            await tts.synthesize_stream_text(
                ["只发一半就停下的句子"],
                fence=GenerationFence("cosy-stall", 1, 1, 0),
            )
        assert srv.connections == 1
        await tts.aclose()
    finally:
        srv.stop()

@pytest.mark.asyncio
async def test_word_timestamps_disabled_returns_complete_audio_without_word_metadata() -> None:
    """P0-03 P2: with timestamps disabled the batch path degrades, not retries.

    ``COSYVOICE_WORD_TIMESTAMPS=false`` means the caller asked for plain audio:
    full PCM is still delivered with ``alignment_status="degraded"`` and empty
    words, on exactly one connection. Fails while the batch path still raises
    ``CosyVoiceTimestampError`` and re-synthesizes the whole sentence.
    """

    srv = MockCosyVoiceServer(scenario="empty_ts")
    srv.start()
    try:
        cfg = CosyVoiceConfig(
            api_key="test",
            ws_url=srv.ws_url,
            pool_size=1,
            word_timestamps=False,
        )
        tts = CosyVoiceTTS(cfg)
        result = await tts.synthesize_stream_text(
            ["无词时间戳也要完整播完。"],
            fence=GenerationFence("no-ts-degrade", 1, 1, 0),
        )
        assert result.pcm
        assert result.words == ()
        assert result.alignment_status == "degraded"
        assert srv.connections == 1
        assert len(srv.run_requests) == 1
        await tts.aclose()
    finally:
        srv.stop()

@pytest.mark.asyncio
async def test_word_timestamps_disabled_honors_cancel_before_returning_degraded_audio() -> None:
    """P0-03 P2: cancel wins over the no-timestamp degrade path.

    With ``word_timestamps=False`` and complete but wordless audio, a cancel
    arriving before the result is returned must discard (``discarded=True``),
    never release the connection for reuse, and never report success. Fails
    while the degrade early-return settles the watcher as not-cancelled and
    releases the connection.
    """
    import asyncio

    srv = MockCosyVoiceServer(scenario="empty_ts")
    srv.start()
    try:
        cfg = CosyVoiceConfig(
            api_key="test",
            ws_url=srv.ws_url,
            pool_size=1,
            word_timestamps=False,
        )
        tts = CosyVoiceTTS(cfg)
        cancel = asyncio.Event()
        tts.set_trace_callback(
            lambda name, status, detail: (
                cancel.set() if name == "cosyvoice_task_finished" else None
            )
        )
        result = await tts.synthesize_stream_text(
            ["取消优先于降级返回。"],
            fence=GenerationFence("no-ts-cancel", 1, 1, 0),
            cancel_event=cancel,
        )
        assert result.discarded is True
        assert tts.pool.discarded_count >= 1
        assert tts.pool.available_approx == 0
        await tts.aclose()
    finally:
        srv.stop()
