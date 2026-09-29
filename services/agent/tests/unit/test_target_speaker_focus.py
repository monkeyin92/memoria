from __future__ import annotations

from typing import Any

import pytest
from services.agent.src.duplex_runtime import (
    DuplexRuntime,
)
from services.agent.tests.unit.runtime_state_helpers import (
    set_floor,
    speaker_permissions,
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
        assert speaker_permissions(runtime).normal_conversation is True
        assert speaker_permissions(runtime).read_private_memory is False
        assert speaker_permissions(runtime).write_long_term_memory is False
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
        assert speaker_permissions(runtime).normal_conversation is True
        assert speaker_permissions(runtime).read_private_memory is False
        assert speaker_permissions(runtime).write_long_term_memory is False
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
        assert speaker_permissions(runtime).read_private_memory is False
        assert speaker_permissions(runtime).write_long_term_memory is False
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
        assert speaker_permissions(runtime).normal_conversation is True
        assert speaker_permissions(runtime).read_private_memory is False
        assert speaker_permissions(runtime).write_long_term_memory is False
        assert speaker_permissions(runtime).sensitive_actions is False
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
async def test_playback_unconfirmed_farewell_still_takes_the_floor() -> None:
    """A device farewell closes an unconfirmed turn; a bare stop must not.

    epoch 1895: a shadow score blocked「好的，再见」while playback was running, so
    the screen stayed in 聆听中 until owner_silence_timeout.  The barge-in gate
    has not classified this utterance yet, so only END_SESSION may pass it; a
    bare「停一下」must keep waiting for the endpointed transcript.
    """

    runtime = DuplexRuntime.create()
    try:
        runtime.set_device_conversation_controls(True)
        await runtime.orchestrator.ready()
        await runtime.on_turn_committed("开始播放")
        await runtime.on_assistant_speaking("机器人正在播放回复")
        await _classify_turn(
            runtime,
            _decision("uncertain", reason_code="shadow_guest_candidate"),
        )
        runtime.input_guard.candidate_text = "好的，再见"
        before = runtime.fence

        returned = await runtime.on_real_interrupt(
            cause="livekit_playback_interrupted",
        )

        assert returned != before
        assert runtime.fence == returned
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
        set_floor(runtime, assistant_speaking=True)
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


