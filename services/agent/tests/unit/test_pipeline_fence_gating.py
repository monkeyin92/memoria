"""Production path: LLM/TTS nodes gate on GenerationFence; active tasks cancel on interrupt."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from services.agent.src.agent import _chunk_text
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.orchestration.state_machine import ConversationState


def test_chunk_text_extracts_content() -> None:
    assert _chunk_text("你好") == "你好"

    class Delta:
        content = "世界"

    class Chunk:
        delta = Delta()

    assert _chunk_text(Chunk()) == "世界"
    assert _chunk_text(object()) == ""


@pytest.mark.asyncio
async def test_active_llm_task_set_and_cancelled_on_interrupt() -> None:
    runtime = DuplexRuntime.create()
    runtime.set_mode_policy(
        ModePolicy.companion_for_test(
            policy_version="test-policy",
            private_context=True,
            owner_evidence=True,
            tools=True,
            voice_profile=True,
            shadow_low_sensitivity_persona=True,
        )
    )
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("用户问题")
    fence = runtime.fence

    async def fake_llm_stream() -> AsyncIterator[str]:
        yield "第一"
        await asyncio.sleep(0.05)
        yield "第二"
        await asyncio.sleep(0.5)
        yield "不该出现"

    # Same registration path used by DuplexVoiceAgent.llm_node.
    async def llm_job() -> list[str]:
        runtime.orchestrator.set_active_llm_task(asyncio.current_task())
        out: list[str] = []
        try:
            async for tok in fake_llm_stream():
                g = runtime.gate_llm_token(fence, tok)
                if g is None:
                    break
                if runtime.orchestrator.tts_cancel_event().is_set():
                    break
                out.append(g)
                await asyncio.sleep(0.02)
        finally:
            runtime.orchestrator.clear_active_llm_task(asyncio.current_task())
        return out

    task = asyncio.create_task(llm_job())
    await asyncio.sleep(0.05)
    assert runtime.orchestrator.active_llm_task is not None
    await runtime.on_real_interrupt(cause="test")
    assert runtime.orchestrator.tts_cancel_event().is_set()
    assert runtime.gate_llm_token(fence, "旧") is None
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_tts_audio_gated_and_active_task_cleared() -> None:
    runtime = DuplexRuntime.create()
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("问")
    fence = runtime.fence
    await runtime.on_assistant_speaking("回答内容")

    async def tts_job() -> int:
        runtime.orchestrator.set_active_tts_task(asyncio.current_task())
        published = 0
        try:
            for _ in range(5):
                if runtime.orchestrator.tts_cancel_event().is_set():
                    break
                pcm = b"\x01\x02" * 10
                if runtime.gate_tts_audio(fence, pcm) is not None:
                    published += 1
                await asyncio.sleep(0.02)
        finally:
            runtime.orchestrator.clear_active_tts_task(asyncio.current_task())
        return published

    task = asyncio.create_task(tts_job())
    await asyncio.sleep(0.03)
    assert runtime.orchestrator.active_tts_task is not None
    await runtime.on_real_interrupt(cause="barge")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert runtime.gate_tts_audio(fence, b"\xff\xff") is None
    assert runtime.orchestrator.active_tts_task is None


@pytest.mark.asyncio
async def test_speaking_lifecycle_sets_heard_tracker() -> None:
    runtime = DuplexRuntime.create()
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("天气")
    assert runtime.orchestrator.state is ConversationState.THINKING
    await runtime.on_assistant_speaking("明天下午可能有雨，")
    assert runtime.orchestrator.state is ConversationState.SPEAKING
    assert runtime.heard_tracker.full_text.startswith("明天")
    await runtime.on_playback_done()
    assert runtime.orchestrator.state is ConversationState.LISTENING


@pytest.mark.asyncio
async def test_livekit_playback_fact_commits_and_publishes_only_heard_text() -> None:
    runtime = DuplexRuntime.create(session_id="session-public")
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("继续")
    runtime.update_pending_assistant_text("已经听到，后面未播放")
    await runtime.on_playback_started()
    await runtime.on_playback_finished(
        playback_position_s=0.5,
        interrupted=False,
        synchronized_transcript="已经听到，",
    )
    await runtime.on_assistant_reply_completed("已经听到，")
    await asyncio.sleep(0)

    assert runtime.orchestrator.context.turns[-1].content == "已经听到，"
    assistant_events = [
        event
        for event in published
        if event.get("type") == "transcript_delta" and event.get("speaker") == "assistant"
    ]
    assert assistant_events == [
        {
            "type": "transcript_delta",
            "speaker": "assistant",
            "text": "已经听到，",
            "final": False,
            "heard": True,
            "turn_id": 1,
            "generation_id": 1,
            "history_eligible": False,
        },
        {
            "type": "transcript_delta",
            "speaker": "assistant",
            "text": "已经听到，",
            "final": True,
            "heard": True,
            "turn_id": 1,
            "generation_id": 1,
            "history_eligible": False,
        },
    ]


@pytest.mark.asyncio
async def test_multi_segment_playback_commits_one_assistant_history_item() -> None:
    runtime = DuplexRuntime.create()
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("介绍一下")
    runtime.update_pending_assistant_text("第一句，第二句。")
    await runtime.on_playback_started()
    await runtime.on_playback_finished(
        playback_position_s=0.4,
        interrupted=False,
        synchronized_transcript="第一句，",
    )
    await runtime.on_playback_started()
    await runtime.on_playback_finished(
        playback_position_s=0.4,
        interrupted=False,
        synchronized_transcript="第二句。",
    )

    assert [turn.role for turn in runtime.orchestrator.context.turns] == ["user"]

    await runtime.on_assistant_reply_completed("第一句，第二句。")
    await asyncio.sleep(0)

    assistant_turns = [
        turn.content
        for turn in runtime.orchestrator.context.turns
        if turn.role == "assistant"
    ]
    assert assistant_turns == ["第一句，第二句。"]
    assistant_events = [
        event
        for event in published
        if event.get("type") == "transcript_delta"
        and event.get("speaker") == "assistant"
    ]
    assert [event["text"] for event in assistant_events] == [
        "第一句，",
        "第一句，第二句。",
        "第一句，第二句。",
    ]
    assert [event["final"] for event in assistant_events] == [False, False, True]


@pytest.mark.asyncio
async def test_session_interrupt_and_playback_event_confirm_once_and_refine_heard() -> None:
    runtime = DuplexRuntime.create()
    published: list[dict[str, object]] = []
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    async def stop_playback() -> None:
        stop_entered.set()
        await release_stop.wait()

    runtime.set_event_publisher(publish)
    await runtime.orchestrator.ready()
    old = await runtime.on_turn_committed("打断测试")
    runtime.update_pending_assistant_text("实际听到，后面没有播放。")
    await runtime.on_playback_started()

    first_confirm = asyncio.create_task(
        runtime.on_real_interrupt(
            cause="session.interrupt",
            stop_playback=stop_playback,
        )
    )
    await stop_entered.wait()
    assert runtime.fence.generation_id == old.generation_id + 1

    playback_fact = asyncio.create_task(
        runtime.on_playback_finished(
            playback_position_s=0.5,
            interrupted=True,
            synchronized_transcript="实际听到，",
        )
    )
    release_stop.set()
    await asyncio.gather(first_confirm, playback_fact)
    await asyncio.sleep(0)

    assert runtime.fence.generation_id == old.generation_id + 1
    assert runtime.orchestrator.metrics.get("interruptions_confirmed_total") == 1
    assert [
        turn.content
        for turn in runtime.orchestrator.context.turns
        if turn.role == "assistant"
    ] == ["实际听到，"]
    assistant_final = [
        event
        for event in published
        if event.get("type") == "transcript_delta"
        and event.get("speaker") == "assistant"
        and event.get("final") is True
    ]
    assert [event["text"] for event in assistant_final] == ["实际听到，"]


@pytest.mark.asyncio
async def test_interrupt_without_alignment_keeps_only_completed_segments() -> None:
    runtime = DuplexRuntime.create()
    await runtime.orchestrator.ready()
    old = await runtime.on_turn_committed("继续")
    runtime.update_pending_assistant_text("第一句。第二句未完整播放。")
    await runtime.on_playback_started()
    await runtime.on_playback_finished(
        playback_position_s=0.4,
        interrupted=False,
        synchronized_transcript="第一句。",
    )
    await runtime.on_playback_started()
    await runtime.on_playback_finished(
        playback_position_s=0.2,
        interrupted=True,
        synchronized_transcript=None,
    )

    assert runtime.fence.generation_id == old.generation_id + 1
    assert [
        turn.content
        for turn in runtime.orchestrator.context.turns
        if turn.role == "assistant"
    ] == ["第一句。"]
