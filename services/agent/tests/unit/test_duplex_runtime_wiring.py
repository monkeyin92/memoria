"""Agent entry wires Orchestrator / GenerationFence / atomic interrupt."""

from __future__ import annotations

import asyncio
import logging

import pytest
from livekit.agents import llm
from services.agent.src.agent import (
    DuplexVoiceAgent,
    _heard_only_chat_context,
    build_session_kwargs,
    build_turn_handling_config,
    create_runtime_for_tests,
)
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.state_machine import ConversationState
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSTT


def test_build_session_kwargs_includes_stt_tts() -> None:
    stt = FunASRSTT(FunASRConfig(api_key="t", ws_url="ws://x"))
    tts = CosyVoiceTTS(CosyVoiceConfig(api_key="t", ws_url="ws://x", pool_size=1))
    kwargs = build_session_kwargs(
        vad=None,
        stt=stt,
        llm=object(),
        tts=tts,
        profile="livekit_cloud",
        offline=True,
    )
    assert kwargs["stt"] is stt
    assert kwargs["tts"] is tts
    # turn_handling present or typed fallback recorded without silent pass
    assert "turn_handling" in kwargs or "turn_handling_config" in kwargs


@pytest.mark.asyncio
async def test_runtime_commit_and_interrupt_bumps_fence() -> None:
    runtime = create_runtime_for_tests()
    await runtime.orchestrator.ready()
    fence = await runtime.on_turn_committed("你好")
    assert fence.turn_id == 1
    assert fence.generation_id >= 1
    assert runtime.orchestrator.state is ConversationState.THINKING
    await runtime.on_assistant_speaking("完整回答内容")
    assert runtime.orchestrator.state is ConversationState.SPEAKING
    assert runtime.heard_tracker.full_text == "完整回答内容"
    new_fence = await runtime.on_real_interrupt(cause="test")
    assert new_fence.generation_id == fence.generation_id + 1
    # Stale audio from old fence dropped
    assert runtime.gate_tts_audio(fence, b"\x01\x02") is None
    assert runtime.gate_tts_audio(new_fence, b"\x03\x04") == b"\x03\x04"


@pytest.mark.asyncio
async def test_runtime_publishes_and_correlates_first_audio_trace() -> None:
    runtime = DuplexRuntime.create(session_id="trace-session")
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    runtime.mark_audio_event("last_user_audio", mono_ns=1_000_000_000)
    runtime.mark_audio_event("turn_committed", mono_ns=1_200_000_000)
    runtime.mark_audio_event("llm_request_started", mono_ns=1_300_000_000)
    runtime.mark_audio_event("llm_first_content_token", mono_ns=1_450_000_000)
    runtime.observe_client_audio_trace(
        {
            "type": "audio_trace",
            "session_id": "trace-session",
            "name": "first_playback",
            "status": "ok",
            "turn_id": 1,
            "generation_id": 1,
        },
        mono_ns=1_900_000_000,
    )
    await asyncio.sleep(0.01)

    assert runtime.latency_trace.derived()["endpointing_latency"] == pytest.approx(0.2)
    assert runtime.latency_trace.derived()["llm_ttft"] == pytest.approx(0.15)
    assert runtime.latency_trace.marks["client_first_playback"] == 1_900_000_000
    assert any(
        event["type"] == "audio_trace"
        and event["name"] == "llm_first_content_token"
        and event["status"] == "ok"
        for event in published
    )


def test_runtime_accepts_only_numeric_allowlisted_webrtc_metrics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = DuplexRuntime.create(session_id="stats-session")
    caplog.set_level("INFO", logger="services.agent.src.duplex_runtime")
    event = {
        "type": "audio_trace",
        "session_id": "stats-session",
        "name": "webrtc_inbound_audio",
        "status": "ok",
        "turn_id": 1,
        "generation_id": 1,
        "detail": {
            "jitter": 0.004,
            "packets_lost": 0,
            "packets_received": 100,
            "bytes_received": 12_000,
            "concealed_samples": 480,
            "silent_concealed_samples": 240,
            "total_samples_received": 48_000,
            "concealment_ratio": 0.01,
            "non_silent_concealment_ratio": 0.005,
            "jitter_buffer_delay": 0.12,
            "jitter_buffer_emitted_count": 4_800,
            "average_jitter_buffer_delay_ms": 0.025,
            "encoded_audio_bitrate_kbps": 96.0,
        },
    }

    assert runtime.observe_client_audio_trace(event) is True
    assert "total_samples_received" in caplog.text

    event["detail"] = {"transcript": "must-not-enter-logs"}
    assert runtime.observe_client_audio_trace(event) is False
    assert "must-not-enter-logs" not in caplog.text


@pytest.mark.asyncio
async def test_listener_cue_uses_an_isolated_cancel_domain_and_never_enters_history() -> None:
    runtime = DuplexRuntime.create(session_id="cue-session", listener_cues_enabled=True)
    runtime.cue_scheduler.min_speech_ms = 1_000
    runtime.cue_scheduler.pause_ms = 0
    runtime.cue_scheduler.cooldown_ms = 0
    runtime.set_listener_cue_aec_healthy(True)
    published: list[dict[str, object]] = []
    handles: list[object] = []

    class Handle:
        def __init__(self) -> None:
            self.stopped = False
            self.done = asyncio.Event()

        def stop(self) -> None:
            self.stopped = True
            self.done.set()

        async def wait_for_playout(self) -> None:
            await self.done.wait()

    def play(text: str) -> Handle:
        assert text == "嗯"
        handle = Handle()
        handles.append(handle)
        return handle

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    runtime.set_listener_cue_player(play)
    await runtime.orchestrator.ready()
    runtime.on_user_voice_started(now_ns=1_000_000_000)
    runtime.observe_user_transcript(
        "我还在继续讲这件事情",
        final=False,
        now_ns=2_100_000_000,
    )
    await asyncio.sleep(0.01)

    assert len(handles) == 1
    assert runtime.orchestrator.context.turns == []
    assert any(event.get("type") == "listener_cue" for event in published)
    assert any(
        event.get("type") == "assistant_state" and event.get("state") == "backchannel"
        for event in published
    )

    await runtime.on_turn_committed("我讲完了")
    await asyncio.sleep(0)
    assert handles[0].stopped is True  # type: ignore[attr-defined]
    assert [turn.role for turn in runtime.orchestrator.context.turns] == ["user"]
    assert runtime.interaction_phase.value == "thinking_silent"
    assert any(
        event.get("type") == "assistant_state"
        and event.get("state") == "thinking_silent"
        for event in published
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_listener_cue_waits_for_a_micro_pause_and_final_cancels_candidate() -> None:
    runtime = DuplexRuntime.create(
        session_id="cue-pause-session",
        listener_cues_enabled=True,
    )
    runtime.cue_scheduler.min_speech_ms = 0
    runtime.cue_scheduler.pause_ms = 30
    runtime.cue_scheduler.cooldown_ms = 0
    runtime.set_listener_cue_aec_healthy(True)
    played: list[str] = []
    runtime.set_listener_cue_player(played.append)
    await runtime.orchestrator.ready()

    runtime.on_user_voice_started(now_ns=1_000_000_000)
    runtime.observe_user_transcript("我还没说完", final=False, now_ns=1_100_000_000)
    await asyncio.sleep(0.005)
    assert played == []

    runtime.observe_user_transcript("我说完了", final=True, now_ns=1_110_000_000)
    await asyncio.sleep(0.04)
    assert played == []
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_applies_ephemeral_emotion_to_the_next_cosyvoice_generation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    tts = CosyVoiceTTS(
        CosyVoiceConfig(api_key="key", ws_url="wss://example", pool_size=0)
    )
    runtime = DuplexRuntime.create(session_id="emotion-session", tts=tts)
    await runtime.orchestrator.ready()

    first = runtime.observe_acoustic_emotion("sad", text="最近有点累", turn_id=1)
    await runtime.on_turn_committed("第一轮")
    second = runtime.observe_acoustic_emotion("sad", text="还是很低落", turn_id=2)
    await runtime.on_turn_committed("第二轮")

    assert first.label == "neutral"
    assert second.label == "sad"
    # Acoustic sad still observed, but TTS stays neutral @ rate 1.0 for stability.
    assert tts.current_instruction == "你正在进行闲聊互动，你说话的情感是neutral。"
    assert tts.current_rate == 1.0
    assert all(turn.role != "emotion" for turn in runtime.orchestrator.context.turns)
    assert "emotion_observation label=sad provider_label=sad" in caplog.text
    assert "speech_plan_selected emotion=neutral rate=1.00" in caplog.text
    assert "最近有点累" not in caplog.text
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_uses_happy_delivery_only_for_safe_laughter_context() -> None:
    tts = CosyVoiceTTS(
        CosyVoiceConfig(api_key="key", ws_url="wss://example", pool_size=0)
    )
    runtime = DuplexRuntime.create(session_id="laughter-session", tts=tts)
    await runtime.orchestrator.ready()

    runtime.observe_acoustic_emotion(
        "happy",
        text="哈哈，我把单词读错得太离谱了",
        turn_id=1,
    )
    await runtime.on_turn_committed("哈哈，我把单词读错得太离谱了")

    assert runtime.speech_plan.delivery_mode == "light_laughter"
    assert tts.current_instruction == "你正在进行闲聊互动，你说话的情感是happy。"

    runtime.observe_acoustic_emotion(
        "happy",
        text="哈哈，其实我刚刚出车祸了",
        turn_id=2,
    )
    await runtime.on_turn_committed("哈哈，其实我刚刚出车祸了")

    assert runtime.speech_plan.delivery_mode == "supportive"
    assert tts.current_instruction == "你正在进行闲聊互动，你说话的情感是neutral。"
    await runtime.close()


@pytest.mark.asyncio
async def test_late_emotion_result_cannot_style_the_next_turn() -> None:
    tts = CosyVoiceTTS(
        CosyVoiceConfig(api_key="key", ws_url="wss://example", pool_size=0)
    )
    runtime = DuplexRuntime.create(session_id="late-emotion-session", tts=tts)
    await runtime.orchestrator.ready()

    await runtime.on_turn_committed("第一轮")
    runtime.observe_acoustic_emotion("happy", text="第一轮", turn_id=1)
    runtime.observe_acoustic_emotion("happy", text="第一轮", turn_id=1)
    await runtime.on_turn_committed("第二轮没有情绪自述")

    assert tts.current_instruction == "你正在进行闲聊互动，你说话的情感是neutral。"
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_commits_new_user_turn_while_thinking() -> None:
    runtime = create_runtime_for_tests()
    await runtime.orchestrator.ready()
    first = await runtime.on_turn_committed("第一句话")

    second = await runtime.on_turn_committed("补充一句")

    assert second.turn_id == first.turn_id + 1
    assert second.generation_id == first.generation_id + 1
    assert runtime.orchestrator.state is ConversationState.THINKING
    assert [turn.content for turn in runtime.orchestrator.context.turns[-2:]] == [
        "第一句话",
        "补充一句",
    ]


@pytest.mark.asyncio
async def test_stop_response_bumps_before_playback_and_is_idempotent() -> None:
    runtime = create_runtime_for_tests()
    await runtime.orchestrator.ready()
    old = await runtime.on_turn_committed("请回答")
    await runtime.on_assistant_speaking("用户只听到这里，后面没有听到")
    saw_bumped_fence = False

    async def stop_playback() -> str:
        nonlocal saw_bumped_fence
        saw_bumped_fence = runtime.fence.generation_id == old.generation_id + 1
        return "用户只听到这里"

    stopped = await runtime.on_real_interrupt(
        cause="user_button",
        stop_playback=stop_playback,
        create_user_turn=False,
    )
    duplicate = await runtime.on_real_interrupt(
        cause="user_button",
        create_user_turn=False,
    )

    assert saw_bumped_fence is True
    assert duplicate == stopped
    assert runtime.orchestrator.state is ConversationState.LISTENING
    assert runtime.orchestrator.context.turns[-1].content == "用户只听到这里"


@pytest.mark.asyncio
async def test_rtc_recovery_forces_generation_bump_while_idle() -> None:
    runtime = create_runtime_for_tests()
    await runtime.orchestrator.ready()
    old = runtime.fence

    recovered = await runtime.on_real_interrupt(
        cause="rtc_recovered",
        create_user_turn=False,
        force_generation_bump=True,
    )

    assert recovered.generation_id == old.generation_id + 1
    assert runtime.orchestrator.metrics.get("interruptions_confirmed_total") == 0


def test_livekit_llm_history_uses_only_heard_assistant_text() -> None:
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="问题一")
    chat_ctx.add_message(role="assistant", content="完整生成但只听到一半")
    chat_ctx.add_message(role="user", content="问题二")

    safe = _heard_only_chat_context(chat_ctx, ["只听到一半"])

    assert [message.text_content for message in safe.messages()] == [
        "问题一",
        "只听到一半",
        "问题二",
    ]


def test_livekit_llm_history_aligns_latest_heard_reply_when_counts_differ() -> None:
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="帮我安排口语训练")
    chat_ctx.add_message(role="assistant", content="模型生成的训练安排")
    chat_ctx.add_message(role="user", content="可以")

    safe = _heard_only_chat_context(
        chat_ctx,
        ["欢迎语", "实际听到的训练安排"],
    )

    assert [message.text_content for message in safe.messages()] == [
        "帮我安排口语训练",
        "实际听到的训练安排",
        "可以",
    ]


@pytest.mark.asyncio
async def test_confirm_interruption_cancels_registered_llm_task() -> None:
    runtime = create_runtime_for_tests()
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("x")
    cancelled = asyncio.Event()

    async def long_llm() -> None:
        runtime.orchestrator.set_active_llm_task(asyncio.current_task())  # type: ignore[arg-type]
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            runtime.orchestrator.clear_active_llm_task()

    task = asyncio.create_task(long_llm())
    await asyncio.sleep(0.02)
    assert runtime.orchestrator.active_llm_task is task
    await runtime.on_real_interrupt()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_duplex_voice_agent_commits_fence_on_user_turn() -> None:
    runtime = DuplexRuntime.create()
    await runtime.orchestrator.ready()
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    class Msg:
        def text_content(self) -> str:
            return "订下周三的票"

    await agent.on_user_turn_completed(None, Msg())
    assert runtime.fence.turn_id == 1
    assert runtime.orchestrator.context.turns[-1].content == "订下周三的票"


def test_cn_self_hosted_turn_config() -> None:
    cfg = build_turn_handling_config("cn_self_hosted")
    assert cfg["turn_detection"]["version"] == "v1-mini"
    assert cfg["interruption"]["mode"] == "vad"


@pytest.mark.asyncio
async def test_interrupt_discards_cosy_pool_binding() -> None:
    tts = CosyVoiceTTS(CosyVoiceConfig(api_key="t", ws_url="ws://127.0.0.1:9", pool_size=0))
    runtime = create_runtime_for_tests(tts=tts)
    await runtime.orchestrator.ready()
    fence = await runtime.on_turn_committed("hi")
    # Simulate bind without real WS by marking active_by_fence via fake conn id path
    from services.agent.src.providers.cosyvoice_tts import PooledConnection

    class FakeWS:
        async def close(self) -> None:
            return None

    conn = PooledConnection(ws=FakeWS())  # type: ignore[arg-type]
    tts.pool.bind_active(fence, conn)
    key = (
        f"{fence.session_id}:{fence.turn_id}:"
        f"{fence.generation_id}:{fence.tool_epoch}"
    )
    assert key in tts.pool.active_by_fence
    await runtime.on_real_interrupt()
    assert key not in tts.pool.active_by_fence
    assert tts.pool.discarded_count >= 1
