from __future__ import annotations

from services.agent.src.orchestration.interruption_guard import (
    ChineseInterruptionGuard,
    InterruptDecision,
    PlaybackInputDecision,
    PlaybackInputGuard,
    is_backchannel,
    is_explicit_interrupt,
)


def test_backchannel_whitelist() -> None:
    assert is_backchannel("嗯嗯", duration_ms=400)
    assert is_backchannel("好的", duration_ms=500)
    assert not is_backchannel("好的，那我们下周见", duration_ms=1200)


def test_explicit_interrupt_prefixes() -> None:
    assert is_explicit_interrupt("等等，不是这个意思")
    assert is_explicit_interrupt("停一下")
    assert is_explicit_interrupt("我问的是下周三")


def test_guard_decisions() -> None:
    g = ChineseInterruptionGuard()
    assert g.on_user_voice_while_speaking() is InterruptDecision.DUCK
    assert (
        g.evaluate(elapsed_ms=100, asr_text="停一下", has_speech_energy=True)
        is InterruptDecision.CONFIRM_INTERRUPT
    )
    assert (
        g.evaluate(elapsed_ms=200, asr_text="嗯嗯", has_speech_energy=True)
        is InterruptDecision.RESUME
    )
    assert (
        g.evaluate(
            elapsed_ms=150,
            asr_text="",
            has_speech_energy=False,
            looks_like_noise_only=True,
        )
        is InterruptDecision.RESUME
    )


def test_playback_input_guard_opens_feedback_circuit_on_third_rapid_turn() -> None:
    guard = PlaybackInputGuard(enabled=True, max_feedback_turns=2, feedback_window_s=15)

    for index in range(2):
        now = (index + 1) * 1_000_000_000
        guard.start(during_playback=True, now_ns=now)
        assert (
            guard.observe(
                "这是完整的新问题？",
                final=True,
                assistant_text="当前回答。",
                now_ns=now + 100_000_000,
            )
            is PlaybackInputDecision.ACCEPT
        )
        assert guard.accept_turn("这是完整的新问题？", now_ns=now)[0] is True

    guard.start(during_playback=True, now_ns=3_000_000_000)
    guard.observe(
        "这是第三次新问题？",
        final=True,
        assistant_text="当前回答。",
        now_ns=3_100_000_000,
    )
    assert guard.accept_turn("这是第三次新问题？", now_ns=3_000_000_000) == (
        False,
        "feedback_circuit_open",
    )
