from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.duplex_runtime import (
    PLAYBACK_INPUT_BLOCK_MIN_WORDS,
    DuplexRuntime,
)
from services.speaker.domain import SpeakerDecision, permissions_for_speaker

SAMPLE_RATE = 16_000
FOCUS_PCM = b"\x01\x00" * int(SAMPLE_RATE * 0.8)


def _decision(
    classification: str,
    *,
    reason_code: str,
    profile_id: str = "profile-target-001",
) -> SpeakerDecision:
    return SpeakerDecision(
        classification=classification,  # type: ignore[arg-type]
        score=0.95 if classification == "owner" else 0.1,
        quality_score=0.9,
        reason_code=reason_code,
        model_version="campplus-runtime-test",
        template_version=1,
        profile_id=profile_id,
        permissions=permissions_for_speaker(classification),  # type: ignore[arg-type]
    )


async def _classify_as(
    decision: SpeakerDecision, _pcm: bytes, _sample_rate: int
) -> SpeakerDecision:
    return decision


async def _classify_turn(runtime: DuplexRuntime, decision: SpeakerDecision) -> SpeakerDecision:
    runtime.set_target_speaker_focus(True)
    runtime.set_speaker_classifier(
        lambda pcm, sample_rate: _classify_as(decision, pcm, sample_rate),
        sample_rate=SAMPLE_RATE,
    )
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(FOCUS_PCM)
    runtime.on_user_voice_stopped()
    return await runtime.await_speaker_classification()


@pytest.mark.asyncio
async def test_target_focus_rejects_formal_guest_from_chat() -> None:
    runtime = DuplexRuntime.create()
    decision = _decision("guest", reason_code="owner_mismatch")

    try:
        observed = await _classify_turn(runtime, decision)
        accepted, reason = runtime.accept_user_turn("这是旁边的人在说话")

        assert observed is decision
        assert accepted is False
        assert reason == "target_non_owner"
        assert runtime.speaker_permissions.normal_conversation is True
        assert runtime.speaker_permissions.read_private_memory is False
        assert runtime.speaker_permissions.write_long_term_memory is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason_code", "pcm"),
    [
        ("shadow_guest_candidate", FOCUS_PCM),
        ("shadow_guest_candidate", b"\x01\x00" * int(SAMPLE_RATE * 0.3)),
    ],
)
async def test_strict_policy_rejects_shadow_non_owner_from_normal_conversation(
    reason_code: str,
    pcm: bytes,
) -> None:
    runtime = DuplexRuntime.create()
    shadow_result = _decision("uncertain", reason_code=reason_code)

    try:
        runtime.set_target_speaker_focus(True)
        runtime.set_speaker_classifier(
            lambda candidate, sample_rate: _classify_as(
                shadow_result,
                candidate,
                sample_rate,
            ),
            sample_rate=SAMPLE_RATE,
        )
        runtime.on_user_voice_started()
        runtime.feed_speaker_pcm(pcm)
        runtime.on_user_voice_stopped()
        observed = await runtime.await_speaker_classification()
        accepted, reason = runtime.accept_user_turn("这是正常的一句话")

        assert observed is shadow_result
        assert accepted is False
        assert reason == "target_non_owner"
        assert runtime.speaker_permissions.normal_conversation is True
        assert runtime.speaker_permissions.read_private_memory is False
        assert runtime.speaker_permissions.write_long_term_memory is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_strict_policy_allows_shadow_ambiguous_chat_without_private_authority() -> None:
    runtime = DuplexRuntime.create()
    shadow_result = _decision("uncertain", reason_code="shadow_ambiguous_candidate")

    try:
        observed = await _classify_turn(runtime, shadow_result)
        accepted, reason = runtime.accept_user_turn("这是正常的一句话")

        assert observed is shadow_result
        assert accepted is True
        assert reason is None
        assert runtime.speaker_permissions.read_private_memory is False
        assert runtime.speaker_permissions.write_long_term_memory is False
        assert runtime._current_history_eligible() is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_shadow_owner_candidate_can_focus_chat_without_private_authority() -> None:
    runtime = DuplexRuntime.create()
    shadow_owner = _decision("uncertain", reason_code="shadow_owner_candidate")

    try:
        observed = await _classify_turn(runtime, shadow_owner)
        accepted, reason = runtime.accept_user_turn("这是账户主人在说话")

        assert observed is shadow_owner
        assert accepted is True
        assert reason is None
        assert runtime.speaker_permissions.normal_conversation is True
        assert runtime.speaker_permissions.read_private_memory is False
        assert runtime.speaker_permissions.write_long_term_memory is False
        assert runtime.speaker_permissions.sensitive_actions is False
    finally:
        await runtime.close()


class _Emitter:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}

    def on(self, name: str, handler: Any) -> None:
        self.handlers.setdefault(name, []).append(handler)

    def off(self, name: str, handler: Any) -> None:
        self.handlers[name].remove(handler)

    def emit(self, name: str, event: Any) -> None:
        for handler in tuple(self.handlers.get(name, ())):
            handler(event)


class _PlaybackSession(_Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.options = SimpleNamespace(interruption={"min_words": 0})


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", ["这是旁边的人在说话", "停一下"])
async def test_playback_shadow_guest_cannot_lower_interrupt_gate_or_stop_playout(
    candidate: str,
) -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    session = _PlaybackSession()
    callback_calls = 0

    async def _target_interrupt() -> None:
        nonlocal callback_calls
        callback_calls += 1

    try:
        runtime.set_speaker_classifier(
            lambda pcm, sample_rate: _classify_as(
                _decision("uncertain", reason_code="shadow_guest_candidate"),
                pcm,
                sample_rate,
            ),
            sample_rate=SAMPLE_RATE,
        )
        runtime.set_target_speaker_focus(True)
        runtime.set_target_speaker_interrupt(_target_interrupt)
        runtime.attach_session_events(session)
        await runtime.orchestrator.ready()
        await runtime.on_turn_committed("开始播放")
        await runtime.on_playback_started()
        before = runtime.fence

        session.emit("user_state_changed", SimpleNamespace(new_state="speaking"))
        runtime.feed_speaker_pcm(FOCUS_PCM)
        session.emit("user_state_changed", SimpleNamespace(new_state="listening"))
        session.emit(
            "user_input_transcribed",
            SimpleNamespace(transcript=candidate, is_final=True),
        )
        await asyncio.sleep(0)

        assert session.options.interruption["min_words"] == PLAYBACK_INPUT_BLOCK_MIN_WORDS
        assert callback_calls == 0
        assert runtime.fence == before
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", ["这是旁边的人在说话", "停一下"])
async def test_playback_shadow_guest_fallback_cannot_bump_fence_or_stop_playout(
    candidate: str,
) -> None:
    runtime = DuplexRuntime.create()
    stop_calls = 0

    async def _stop_playback() -> str | None:
        nonlocal stop_calls
        stop_calls += 1
        return None

    try:
        await runtime.orchestrator.ready()
        await runtime.on_turn_committed("开始播放")
        await runtime.on_assistant_speaking("机器人正在播放回复")
        await _classify_turn(
            runtime,
            _decision("uncertain", reason_code="shadow_guest_candidate"),
        )
        runtime.input_guard.candidate_text = candidate
        before = runtime.fence

        returned = await runtime.on_real_interrupt(
            cause="livekit_playback_interrupted",
            stop_playback=_stop_playback,
        )

        assert returned == before
        assert runtime.fence == before
        assert stop_calls == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "expected_ack"),
    [
        ("等一下", ["嗯，你说。"]),
        ("等一下我想问个事", []),
    ],
)
async def test_explicit_interrupt_ack_matches_command_only_policy(
    candidate: str,
    expected_ack: list[str],
) -> None:
    """A control-only command acks; command plus chat goes straight to the reply."""
    runtime = DuplexRuntime.create()
    stopped = 0
    said: list[str] = []

    async def _stop_playback() -> str | None:
        nonlocal stopped
        stopped += 1
        return None

    async def _yield(phrase: str) -> None:
        said.append(phrase)

    try:
        await runtime.orchestrator.ready()
        await runtime.on_turn_committed("开始播放")
        await runtime.on_assistant_speaking("机器人正在播放回复")
        runtime._was_speaking = True
        await _classify_turn(
            runtime,
            _decision("uncertain", reason_code="shadow_guest_candidate"),
        )
        runtime.set_reject_non_owner_voice(False)
        runtime.input_guard.candidate_text = candidate
        runtime.set_interrupt_yield(_yield)
        before = runtime.fence

        returned = await runtime.on_real_interrupt(
            cause="livekit_playback_interrupted",
            stop_playback=_stop_playback,
        )
        await asyncio.sleep(0.05)

        assert not returned.matches(before)
        assert stopped == 1
        assert said == expected_ack
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_permissive_policy_allows_formal_guest_to_interrupt() -> None:
    runtime = DuplexRuntime.create()
    stopped = 0

    async def _stop_playback() -> str | None:
        nonlocal stopped
        stopped += 1
        return None

    try:
        await runtime.orchestrator.ready()
        await runtime.on_turn_committed("开始播放")
        await runtime.on_assistant_speaking("机器人正在播放回复")
        runtime._was_speaking = True
        await _classify_turn(runtime, _decision("guest", reason_code="owner_mismatch"))
        runtime.set_reject_non_owner_voice(False)
        runtime.input_guard.candidate_text = "停一下"
        before = runtime.fence

        returned = await runtime.on_real_interrupt(
            cause="livekit_playback_interrupted",
            stop_playback=_stop_playback,
        )

        assert not returned.matches(before)
        assert stopped == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_playback_focus_waits_for_endpoint_before_classifying_interrupt() -> None:
    """An early LiveKit interrupt must not snapshot a partial command as a guest."""
    runtime = DuplexRuntime.create()
    session = _PlaybackSession()
    classified_lengths: list[int] = []
    callback_calls = 0

    async def _classify(pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        classified_lengths.append(len(pcm))
        if len(pcm) < len(FOCUS_PCM):
            return _decision("guest", reason_code="owner_mismatch")
        return _decision("owner", reason_code="owner_match")

    async def _target_interrupt() -> None:
        nonlocal callback_calls
        callback_calls += 1

    try:
        runtime.set_target_speaker_focus(True)
        runtime.set_speaker_classifier(_classify, sample_rate=SAMPLE_RATE)
        runtime.set_target_speaker_interrupt(_target_interrupt)
        runtime.attach_session_events(session)
        await runtime.orchestrator.ready()
        await runtime.on_turn_committed("开始播放")
        await runtime.on_assistant_speaking("机器人正在播放回复")
        runtime._was_speaking = True

        session.emit("user_state_changed", SimpleNamespace(new_state="speaking"))
        runtime.feed_speaker_pcm(b"\x01\x00" * int(SAMPLE_RATE * 0.025))
        before = runtime.fence
        early = await runtime.on_real_interrupt(
            cause="session.interrupt",
            stop_playback=lambda: asyncio.sleep(0),
        )

        assert early == before
        assert classified_lengths == []

        runtime.feed_speaker_pcm(FOCUS_PCM)
        session.emit("user_state_changed", SimpleNamespace(new_state="listening"))
        session.emit(
            "user_input_transcribed",
            SimpleNamespace(transcript="等一下", is_final=True),
        )
        await asyncio.sleep(0.05)

        assert runtime.input_guard.candidate_during_playback is False
        expected_pcm_bytes = len(FOCUS_PCM) + int(SAMPLE_RATE * 0.025 * 2)
        assert classified_lengths
        assert set(classified_lengths) == {expected_pcm_bytes}
        assert callback_calls == 1
        assert runtime._target_focus_pending_epoch is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_explicit_partial_interrupt_survives_final_asr_revision() -> None:
    """A clear interim 「等一下」 must not be lost when endpoint ASR revises it."""
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    session = _PlaybackSession()
    stop_calls = 0

    async def _stop_playback() -> str | None:
        nonlocal stop_calls
        stop_calls += 1
        return None

    async def _target_interrupt() -> None:
        await runtime.on_real_interrupt(
            cause="target_speaker_confirmed",
            stop_playback=_stop_playback,
        )

    try:
        runtime.set_target_speaker_focus(True)
        runtime.set_speaker_classifier(
            lambda pcm, sample_rate: _classify_as(
                _decision("uncertain", reason_code="shadow_owner_candidate"),
                pcm,
                sample_rate,
            ),
            sample_rate=SAMPLE_RATE,
        )
        runtime.set_target_speaker_interrupt(_target_interrupt)
        runtime.attach_session_events(session)
        await runtime.orchestrator.ready()
        await runtime.on_turn_committed("开始播放")
        await runtime.on_assistant_speaking("机器人正在播放回复")
        runtime._was_speaking = True
        before = runtime.fence

        session.emit("user_state_changed", SimpleNamespace(new_state="speaking"))
        runtime.feed_speaker_pcm(FOCUS_PCM)
        session.emit(
            "user_input_transcribed",
            SimpleNamespace(transcript="等一下", is_final=False),
        )
        session.emit("user_state_changed", SimpleNamespace(new_state="listening"))
        session.emit(
            "user_input_transcribed",
            SimpleNamespace(transcript="等一项", is_final=True),
        )
        await asyncio.sleep(0.05)

        assert stop_calls == 1
        assert not runtime.fence.matches(before)
        assert runtime.accept_user_turn("等一项") == (False, "interrupt_command_only")
    finally:
        await runtime.close()
