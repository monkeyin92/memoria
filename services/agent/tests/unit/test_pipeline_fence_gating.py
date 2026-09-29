"""Production path: LLM/TTS nodes gate on GenerationFence; active tasks cancel on interrupt."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from services.agent.src.agent import _chunk_text
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy


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


