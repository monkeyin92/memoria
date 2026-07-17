"""100 consecutive simulated barge-ins: zero stale generation audio."""

from __future__ import annotations

import asyncio

import pytest
from services.agent.src.contracts.events import TimedWord
from services.agent.src.orchestration.orchestrator import Orchestrator
from services.agent.src.orchestration.state_machine import ConversationState, TransitionEvent


@pytest.mark.asyncio
async def test_100_interrupt_zero_stale_audio() -> None:
    orch = Orchestrator()
    await orch.ready()
    stale = 0
    published_after_cancel = 0

    for i in range(100):
        await orch.on_vad_start()
        fence = await orch.commit_turn(f"问题{i}")
        words = [TimedWord(text="答", begin_ms=0, end_ms=100)]
        await orch.begin_speaking(words, "答案内容比较长需要被打断")
        # simulate speaking path to interruption_pending
        assert orch.state_machine is not None
        if orch.state is ConversationState.SPEAKING:
            orch.state_machine.apply(TransitionEvent.USER_VOICE_WHILE_SPEAKING)

        old_fence = fence
        new_fence = await orch.confirm_interruption(cause="test")
        assert new_fence.generation_id == old_fence.generation_id + 1

        # Old generation audio must not publish
        ok_old = orch.publish_audio_if_current(old_fence, b"\x01\x02")
        if ok_old:
            published_after_cancel += 1
        else:
            stale += 0  # dropped correctly
        # New fence may publish
        assert orch.publish_audio_if_current(new_fence, b"\x03\x04") is True

        # Cosy pool discard called with old fence
        assert any(f.generation_id == old_fence.generation_id for f in orch.cosyvoice_pool.discarded)

    assert published_after_cancel == 0
    assert orch.stale_audio_outputs == 100  # each old publish attempt counted
    # mic never muted
    assert orch.mic_open is True
    assert orch.asr_active is True


@pytest.mark.asyncio
async def test_heard_text_on_interrupt() -> None:
    orch = Orchestrator()
    await orch.ready()
    await orch.on_vad_start()
    await orch.commit_turn("天气如何")
    text = "明天下午可能有雨，建议你带一把伞。"
    words = []
    t = 0
    for ch in text:
        words.append(TimedWord(text=ch, begin_ms=t, end_ms=t + 100))
        t += 100
    await orch.begin_speaking(words, text)
    # stop after ~1.2s of audio progress
    await asyncio.sleep(0)
    # Manually set playback window before interrupt
    start = orch.playback.started_mono_ns or 0
    orch.heard_tracker.mark_playback_started(start)
    orch.playback.stopped_mono_ns = start + 1_200_000_000
    orch.heard_tracker.mark_playback_stopped(start + 1_200_000_000)
    await orch.confirm_interruption()
    heard = ""
    for m in orch.context.turns:
        if m.role == "assistant":
            heard = m.content
    assert "建议" not in heard
