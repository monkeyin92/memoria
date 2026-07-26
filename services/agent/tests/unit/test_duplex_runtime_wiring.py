"""Agent entry wires Orchestrator / GenerationFence / atomic interrupt."""

from __future__ import annotations

import asyncio
import gc
import logging
import warnings
from types import SimpleNamespace
from typing import Any

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
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.orchestration.speaker_verify import (
    SpeakerGateState,
    SpeakerVerifier,
    voiced_stats_from_pcm,
)
from services.agent.src.orchestration.state_machine import ConversationState
from services.agent.src.orchestration.utterance_router import UtteranceIntent
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSTT
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


def _speaker_decision(*, profile_id: str = "profile-owner-001") -> SpeakerDecision:
    return SpeakerDecision(
        classification="owner",
        score=0.95,
        quality_score=0.9,
        reason_code="owner_match",
        model_version="campplus-test",
        template_version=1,
        profile_id=profile_id,
        permissions=permissions_for_speaker("owner"),
    )


def _shadow_speaker_decision(*, profile_id: str) -> SpeakerDecision:
    return SpeakerDecision(
        classification="uncertain",
        score=0.8,
        quality_score=0.9,
        reason_code="shadow_owner_candidate",
        model_version="campplus-test",
        template_version=1,
        profile_id=profile_id,
        permissions=permissions_for_speaker("uncertain"),
    )


def _guest_speaker_decision(*, profile_id: str = "profile-guest-001") -> SpeakerDecision:
    return SpeakerDecision(
        classification="guest",
        score=0.15,
        quality_score=0.9,
        reason_code="owner_mismatch",
        model_version="campplus-test",
        template_version=1,
        profile_id=profile_id,
        permissions=permissions_for_speaker("guest"),
    )


async def _classify_speaker(runtime: DuplexRuntime, decision: SpeakerDecision) -> None:
    async def _classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return decision

    runtime.set_speaker_classifier(_classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    assert await runtime.await_speaker_classification() is decision


class _SessionEmitter:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}
        self.options = SimpleNamespace(
            interruption={"min_words": 0, "false_interruption_timeout": 1.2}
        )

    def on(self, name: str, handler: Any) -> None:
        self.handlers.setdefault(name, []).append(handler)

    def off(self, name: str, handler: Any) -> None:
        self.handlers[name].remove(handler)

    def emit(self, name: str, event: Any) -> None:
        for handler in tuple(self.handlers.get(name, ())):
            handler(event)


@pytest.mark.asyncio
async def test_uncertain_user_evidence_carries_shadow_owner_provenance() -> None:
    evidence: list[dict[str, object]] = []

    async def _publish(event: dict[str, object]) -> None:
        evidence.append(event)

    runtime = DuplexRuntime.create(session_id="shadow-persona-session")
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
    runtime.set_evidence_publisher(_publish)
    await _classify_speaker(runtime, _shadow_speaker_decision(profile_id="shadow-profile-1"))

    text = "我觉得先听完对方，再认真回答这个问题。"
    assert runtime.accept_user_turn(text) == (True, None)
    fence = await runtime.on_turn_committed(text)
    runtime.publish_transcript(speaker="user", text=text, final=True, fence=fence)
    await asyncio.sleep(0)

    utterance = next(
        event for event in evidence if event.get("event_type") == "speech.utterance_finalized"
    )
    assert utterance["speaker_class"] == "uncertain"
    assert utterance["payload"] == {
        "text": text,
        "persona_eligible": True,
        "prompt_kind": "spontaneous",
        "speaker_reason_code": "shadow_owner_candidate",
        "speaker_profile_id": "shadow-profile-1",
        "speaker_quality_score": 0.9,
        "speaker_model_version": "campplus-test",
        "speaker_template_version": 1,
        "interaction_mode": "companion",
        "mode_policy_version": "test-policy",
        "simulated_output": False,
        "history_eligible": False,
        "owner_projection_eligible": False,
    }
    await runtime.close()


@pytest.mark.asyncio
async def test_playback_end_clears_unanchored_echo_before_the_next_vad() -> None:
    cleared: list[str] = []
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    runtime.set_user_turn_clearer(lambda: cleared.append("clear"))
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("你好")
    runtime.update_pending_assistant_text("你好呀，很高兴见到你。")
    await runtime.on_playback_started()

    assert runtime.observe_user_transcript("你好呀我告现你", final=False) == "wait"
    await runtime.on_assistant_reply_completed("你好呀，很高兴见到你。")

    assert cleared == ["clear"]
    assert runtime._user_transcript_contaminated is False
    assert runtime._suspected_playback_prefixes == []
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transcript", ["等一下", "等下。"])
async def test_trusted_aec_pure_interrupt_can_stop_without_a_vad_start(
    transcript: str,
) -> None:
    interrupted: list[str] = []
    interrupted_event = asyncio.Event()

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        assert _pcm
        return _speaker_decision()

    async def interrupt() -> None:
        interrupted.append("interrupt")
        interrupted_event.set()

    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=True,
    )
    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.set_target_speaker_focus(True)
    runtime.set_target_speaker_interrupt(interrupt)
    runtime._was_speaking = True
    runtime.update_pending_assistant_text("我正在讲一个很长的故事。")
    runtime.feed_speaker_pcm(b"\x00\x20" * 16_000)
    session = _SessionEmitter()
    runtime.attach_session_events(session)

    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript=transcript, is_final=False),
    )
    await asyncio.wait_for(interrupted_event.wait(), timeout=1)

    assert interrupted == ["interrupt"]
    assert runtime.input_guard.candidate_decision.value == "accept"
    assert runtime._trusted_unanchored_control_epoch == runtime._speaker_epoch
    await runtime.close()


@pytest.mark.asyncio
async def test_trusted_aec_wait_alias_interim_and_final_interrupt_only_once() -> None:
    interrupted: list[str] = []
    interrupted_event = asyncio.Event()

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return _speaker_decision()

    async def interrupt() -> None:
        interrupted.append("interrupt")
        interrupted_event.set()

    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=True,
    )
    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.set_target_speaker_focus(True)
    runtime.set_target_speaker_interrupt(interrupt)
    runtime._was_speaking = True
    runtime.update_pending_assistant_text("我正在讲一个很长的故事。")
    runtime.feed_speaker_pcm(b"\x00\x20" * 16_000)
    session = _SessionEmitter()
    runtime.attach_session_events(session)

    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript="等下", is_final=False),
    )
    await asyncio.wait_for(interrupted_event.wait(), timeout=1)
    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript="等下。", is_final=True),
    )
    await asyncio.sleep(0)

    assert interrupted == ["interrupt"]
    assert runtime.accept_user_turn("等下。") == (False, "interrupt_command_only")
    await runtime.close()


@pytest.mark.asyncio
async def test_sticky_interrupt_drops_final_that_replays_previous_user_turn() -> None:
    said: list[str] = []
    cleared: list[str] = []
    published: list[dict[str, object]] = []

    async def _yield(phrase: str) -> None:
        said.append(phrase)

    async def _publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime = DuplexRuntime.create(input_guard_enabled=True)
    runtime.set_interrupt_yield(_yield)
    runtime.set_event_publisher(_publish)
    runtime.set_user_turn_clearer(lambda: cleared.append("cleared"))
    runtime._speaker_class = "owner"
    runtime._speaker_decision = _speaker_decision()
    previous_fence = await runtime.on_turn_committed("你叫什么名字？")
    await runtime.on_assistant_speaking("我是你的记忆助手。")
    runtime.on_user_voice_started()

    assert (
        runtime.observe_user_transcript(
            "停一下，你叫什么名字？",
            final=False,
        ).value
        == "accept"
    )
    assert (
        runtime.observe_user_transcript(
            "你叫什么名字？",
            final=True,
        ).value
        == "accept"
    )
    interrupted_fence = await runtime.on_real_interrupt(
        cause="livekit_playback_interrupted"
    )

    accepted, reason = runtime.accept_user_turn(
        "你叫什么名字？",
        speech_anchored=True,
        canonical_speech_epoch=runtime._speaker_epoch,
    )
    await asyncio.sleep(0.01)

    assert accepted is False
    assert reason == "interrupt_replayed_previous_turn"
    assert interrupted_fence.turn_id == previous_fence.turn_id
    assert interrupted_fence.generation_id == previous_fence.generation_id + 1
    assert runtime.fence.matches(interrupted_fence)
    assert said == ["嗯，你说。"]
    assert cleared == ["cleared"]
    assert runtime._paused_reply_available is True
    suppressed = next(
        event
        for event in published
        if event.get("type") == "audio_trace"
        and event.get("name") == "interrupt_command_turn_suppressed"
    )
    detail = suppressed["detail"]
    assert isinstance(detail, dict)
    assert detail["intent"] == UtteranceIntent.INTERRUPT_REPLAY
    assert detail["reason"] == "interrupt_replayed_previous_turn"
    assert detail["ack_len"] == len("嗯，你说。")
    yield_started = next(
        event
        for event in published
        if event.get("type") == "audio_trace"
        and event.get("name") == "interrupt_yield_started"
    )
    yield_detail = yield_started["detail"]
    assert isinstance(yield_detail, dict)
    assert yield_detail["intent"] == UtteranceIntent.INTERRUPT_REPLAY
    assert yield_detail["reason"] == "interrupt_replayed_previous_turn"
    assert yield_detail["ack_len"] == len("嗯，你说。")
    await runtime.close()


@pytest.mark.asyncio
async def test_repeated_previous_user_turn_without_sticky_interrupt_remains_chat() -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    await runtime.on_turn_committed("你叫什么名字？")

    accepted, reason = runtime.accept_user_turn("你叫什么名字？")

    assert accepted is True
    assert reason is None
    await runtime.close()


@pytest.mark.asyncio
async def test_sticky_interrupt_keeps_final_with_different_content_as_chat() -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    previous_fence = await runtime.on_turn_committed("你叫什么名字？")
    await runtime.on_assistant_speaking("我是你的记忆助手。")
    runtime.on_user_voice_started()
    assert (
        runtime.observe_user_transcript(
            "停一下，你叫什么名字？",
            final=False,
        ).value
        == "accept"
    )
    assert (
        runtime.observe_user_transcript(
            "你今天过得怎么样？",
            final=True,
        ).value
        == "accept"
    )

    accepted, reason = runtime.accept_user_turn(
        "你今天过得怎么样？",
        speech_anchored=True,
        canonical_speech_epoch=runtime._speaker_epoch,
    )
    assert accepted is True
    assert reason is None
    new_fence = await runtime.on_turn_committed("你今天过得怎么样？")
    assert new_fence.turn_id == previous_fence.turn_id + 1
    await runtime.close()


@pytest.mark.asyncio
async def test_sticky_repeat_outside_playback_remains_chat() -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    await runtime.on_turn_committed("你叫什么名字？")
    runtime.on_user_voice_started()
    assert (
        runtime.observe_user_transcript(
            "停一下，你叫什么名字？",
            final=False,
        ).value
        == "accept"
    )
    assert (
        runtime.observe_user_transcript(
            "你叫什么名字？",
            final=True,
        ).value
        == "accept"
    )

    accepted, reason = runtime.accept_user_turn(
        "你叫什么名字？",
        speech_anchored=True,
        canonical_speech_epoch=runtime._speaker_epoch,
    )

    assert accepted is True
    assert reason is None
    await runtime.close()


@pytest.mark.asyncio
async def test_trusted_aec_pure_interrupt_keeps_voiced_anchor_for_late_asr() -> None:
    now_ns = 1_000_000_000
    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=True,
    )
    runtime._was_speaking = True
    runtime.update_pending_assistant_text("我正在讲一个很长的故事。")
    for _ in range(3):
        runtime.feed_speaker_pcm(b"\x00\x20" * 1_280, now_ns=now_ns)
        now_ns += 80_000_000
    for _ in range(15):
        runtime.feed_speaker_pcm(b"\x00\x00" * 1_280, now_ns=now_ns)
        now_ns += 80_000_000

    assert runtime.observe_user_transcript(
        "等一下",
        final=False,
        now_ns=now_ns,
    ).value == "accept"
    assert runtime._trusted_unanchored_control_epoch == runtime._speaker_epoch
    assert (
        voiced_stats_from_pcm(
            bytes(runtime._speaker_pcm),
            sample_rate=16_000,
        )["speech_ms"]
        >= 160
    )
    runtime.on_user_voice_started(now_ns=now_ns + 100_000_000)
    assert runtime._trusted_playback_witness_pcm == b""
    assert runtime._trusted_playback_witness_ns is None
    await runtime.close()


@pytest.mark.asyncio
async def test_trusted_aec_voiced_anchor_expires_before_a_late_control() -> None:
    now_ns = 1_000_000_000
    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=True,
    )
    runtime._was_speaking = True
    runtime.update_pending_assistant_text("我正在讲一个很长的故事。")
    for _ in range(3):
        runtime.feed_speaker_pcm(b"\x00\x20" * 1_280, now_ns=now_ns)
        now_ns += 80_000_000
    for _ in range(33):
        runtime.feed_speaker_pcm(b"\x00\x00" * 1_280, now_ns=now_ns)
        now_ns += 80_000_000

    assert runtime.observe_user_transcript(
        "等一下",
        final=False,
        now_ns=now_ns,
    ).value == "wait"
    assert runtime._trusted_unanchored_control_epoch is None
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transcript", ["等一下", "等下。"])
async def test_trusted_unanchored_interrupt_reclassifies_the_current_pcm_epoch(
    transcript: str,
) -> None:
    interrupted: list[str] = []
    decisions = iter((_speaker_decision(), _guest_speaker_decision()))
    guest_classified = asyncio.Event()

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        assert _pcm
        decision = next(decisions)
        if decision.classification == "guest":
            guest_classified.set()
        return decision

    async def interrupt() -> None:
        interrupted.append("interrupt")

    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=True,
    )
    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.set_target_speaker_focus(True)
    runtime.set_target_speaker_interrupt(interrupt)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 16_000)
    runtime.on_user_voice_stopped()
    assert await runtime.await_speaker_classification() == _speaker_decision()
    assert runtime.accept_user_turn("上一轮真实用户话轮") == (True, None)

    runtime._was_speaking = True
    runtime.update_pending_assistant_text("我正在讲一个很长的故事。")
    runtime.feed_speaker_pcm(b"\x00\x20" * 16_000)
    session = _SessionEmitter()
    runtime.attach_session_events(session)

    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript=transcript, is_final=False),
    )
    await asyncio.wait_for(guest_classified.wait(), timeout=1)
    current_classification = runtime._speaker_classification_task
    assert current_classification is not None
    await current_classification

    assert interrupted == []
    assert runtime._speaker_decision == _guest_speaker_decision()
    assert runtime.input_guard.candidate_decision.value == "ignore"
    await runtime.close()


@pytest.mark.parametrize(
    "pcm",
    [
        b"\x00\x00" * 16_000,
        b"\x01\x00" * 16_000,
        b"\x00\x00" * (16_000 - 2_240) + b"\x00\x20" * 2_240,
    ],
)
def test_trusted_unanchored_control_requires_recent_voiced_pcm(pcm: bytes) -> None:
    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=True,
    )
    runtime._was_speaking = True
    runtime.update_pending_assistant_text("我正在讲一个很长的故事。")
    runtime.feed_speaker_pcm(pcm)

    assert runtime.observe_user_transcript("等一下", final=False).value == "wait"
    assert runtime._trusted_unanchored_control_epoch is None


@pytest.mark.asyncio
async def test_trusted_aec_single_stop_can_use_a_short_voiced_anchor() -> None:
    interrupted: list[str] = []
    interrupted_event = asyncio.Event()

    async def interrupt() -> None:
        interrupted.append("interrupt")
        interrupted_event.set()

    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=True,
    )
    runtime.set_target_speaker_interrupt(interrupt)
    runtime._was_speaking = True
    runtime.update_pending_assistant_text("我正在讲一个很长的故事。")
    runtime.feed_speaker_pcm(
        b"\x00\x00" * 11_840 + b"\x00\x20" * 2_560
    )
    session = _SessionEmitter()
    runtime.attach_session_events(session)

    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript="停", is_final=False),
    )
    await asyncio.wait_for(interrupted_event.wait(), timeout=1)

    assert interrupted == ["interrupt"]
    await runtime.close()


@pytest.mark.asyncio
async def test_revoking_aec_trust_cancels_an_inflight_unanchored_interrupt() -> None:
    classification_started = asyncio.Event()
    release_classification = asyncio.Event()
    interrupted: list[str] = []

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        classification_started.set()
        await release_classification.wait()
        return _speaker_decision()

    async def interrupt() -> None:
        interrupted.append("interrupt")

    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=True,
    )
    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.set_target_speaker_focus(True)
    runtime.set_target_speaker_interrupt(interrupt)
    runtime._was_speaking = True
    runtime.update_pending_assistant_text("我正在讲一个很长的故事。")
    runtime.feed_speaker_pcm(b"\x00\x20" * 16_000)
    session = _SessionEmitter()
    runtime.attach_session_events(session)
    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript="等一下", is_final=False),
    )
    await asyncio.wait_for(classification_started.wait(), timeout=1)
    trusted_epoch = runtime._speaker_epoch

    assert runtime.revoke_trusted_aec_playback_control(cause="gateway_apm_failed") is True
    release_classification.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert interrupted == []
    assert runtime.trusted_aec_playback_control is False
    assert runtime._speaker_epoch == trusted_epoch + 1
    assert runtime._trusted_playback_pcm == bytearray()
    assert runtime._trusted_playback_witness_pcm == b""
    assert runtime._trusted_playback_witness_ns is None
    assert runtime.input_guard.candidate_decision.value == "ignore"
    assert runtime.revoke_trusted_aec_playback_control(cause="duplicate") is False
    await runtime.close()


@pytest.mark.asyncio
async def test_late_trusted_control_cannot_interrupt_the_next_playback() -> None:
    classification_started = asyncio.Event()
    release_classification = asyncio.Event()
    interrupted: list[str] = []

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        classification_started.set()
        await release_classification.wait()
        return _speaker_decision()

    async def interrupt() -> None:
        interrupted.append("interrupt")
        await runtime.on_real_interrupt(cause="late_trusted_control")

    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=True,
    )
    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.set_target_speaker_focus(True)
    runtime.set_target_speaker_interrupt(interrupt)
    runtime.update_pending_assistant_text("播放 A 的很长内容。")
    await runtime.on_playback_started()
    runtime.feed_speaker_pcm(b"\x00\x20" * 16_000)
    session = _SessionEmitter()
    runtime.attach_session_events(session)
    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript="等一下", is_final=False),
    )
    await asyncio.wait_for(classification_started.wait(), timeout=1)

    await runtime.on_playback_finished(
        playback_position_s=1.0,
        interrupted=False,
        synchronized_transcript="播放 A 的很长内容。",
    )
    runtime.update_pending_assistant_text("播放 B 的很长内容。")
    await runtime.on_playback_started()
    fence_before_release = runtime.fence
    release_classification.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert interrupted == []
    assert runtime.fence.matches(fence_before_release)
    await runtime.close()


@pytest.mark.asyncio
async def test_trusted_control_rechecks_playback_epoch_inside_interruption_lock() -> None:
    interrupt_requested = asyncio.Event()
    interrupt_completed = asyncio.Event()
    stopped_playback_epochs: list[int] = []

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return _speaker_decision()

    async def interrupt() -> None:
        interrupt_requested.set()

        async def stop_playback() -> str | None:
            stopped_playback_epochs.append(runtime._playback_epoch)
            return None

        await runtime.on_real_interrupt(
            cause="target_speaker_confirmed",
            stop_playback=stop_playback,
        )
        interrupt_completed.set()

    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=True,
    )
    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.set_target_speaker_focus(True)
    runtime.set_target_speaker_interrupt(interrupt)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("请讲一个故事")
    runtime.update_pending_assistant_text("播放 A 的很长内容。")
    await runtime.on_playback_started()
    playback_a_epoch = runtime._playback_epoch
    runtime.feed_speaker_pcm(b"\x00\x20" * 16_000)
    session = _SessionEmitter()
    runtime.attach_session_events(session)

    state_lock = runtime.orchestrator._state_lock
    await state_lock.acquire()
    try:
        session.emit(
            "user_input_transcribed",
            SimpleNamespace(transcript="等一下", is_final=False),
        )
        await asyncio.wait_for(interrupt_requested.wait(), timeout=1)
        await asyncio.sleep(0)

        runtime.update_pending_assistant_text("播放 B 的很长内容。")
        start_playback_b = asyncio.create_task(runtime.on_playback_started())
        await asyncio.sleep(0)
        assert start_playback_b.done() is False
    finally:
        state_lock.release()

    await asyncio.wait_for(interrupt_completed.wait(), timeout=1)
    await asyncio.wait_for(start_playback_b, timeout=1)

    assert stopped_playback_epochs == [playback_a_epoch]
    assert runtime._playback_epoch == playback_a_epoch + 1
    assert runtime._was_speaking is True
    assert runtime._pending_assistant_text == "播放 B 的很长内容。"
    await runtime.close()


@pytest.mark.asyncio
async def test_trusted_interrupt_serializes_stop_with_the_next_playback() -> None:
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()
    interrupt_completed = asyncio.Event()
    stopped_playback_epochs: list[int] = []

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return _speaker_decision()

    async def interrupt() -> None:
        async def stop_playback() -> str | None:
            stop_entered.set()
            await release_stop.wait()
            stopped_playback_epochs.append(runtime._playback_epoch)
            return None

        await runtime.on_real_interrupt(
            cause="target_speaker_confirmed",
            stop_playback=stop_playback,
        )
        interrupt_completed.set()

    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=True,
    )
    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.set_target_speaker_focus(True)
    runtime.set_target_speaker_interrupt(interrupt)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("请讲一个故事")
    runtime.update_pending_assistant_text("播放 A 的很长内容。")
    await runtime.on_playback_started()
    playback_a_epoch = runtime._playback_epoch
    runtime.feed_speaker_pcm(b"\x00\x20" * 16_000)
    session = _SessionEmitter()
    runtime.attach_session_events(session)

    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript="等一下", is_final=False),
    )
    await asyncio.wait_for(stop_entered.wait(), timeout=1)

    runtime.update_pending_assistant_text("播放 B 的很长内容。")
    start_playback_b = asyncio.create_task(runtime.on_playback_started())
    await asyncio.sleep(0)
    assert start_playback_b.done() is False

    release_stop.set()
    await asyncio.wait_for(interrupt_completed.wait(), timeout=1)
    await asyncio.wait_for(start_playback_b, timeout=1)

    assert stopped_playback_epochs == [playback_a_epoch]
    assert runtime._playback_epoch == playback_a_epoch + 1
    assert runtime._was_speaking is True
    assert runtime._pending_assistant_text == "播放 B 的很长内容。"
    await runtime.close()


@pytest.mark.parametrize(
    ("trusted_aec", "text", "assistant_text"),
    [
        (False, "等一下", "我正在讲一个很长的故事。"),
        (True, "我想问个问题", "我正在讲一个很长的故事。"),
        (True, "等一下，我想问个问题", "我正在讲一个很长的故事。"),
        (True, "等一下", "你先等一下，我马上说完。"),
        (True, "等下。", "你先等一下，我马上说完。"),
    ],
)
def test_unanchored_playback_text_stays_guarded_without_trusted_pure_control(
    trusted_aec: bool,
    text: str,
    assistant_text: str,
) -> None:
    runtime = DuplexRuntime.create(
        input_guard_enabled=True,
        trusted_aec_playback_control=trusted_aec,
    )
    runtime._was_speaking = True
    runtime.update_pending_assistant_text(assistant_text)
    runtime.feed_speaker_pcm(b"\x00\x20" * 16_000)

    assert runtime.observe_user_transcript(text, final=True).value == "ignore"
    assert runtime._trusted_unanchored_control_epoch is None


@pytest.mark.asyncio
async def test_interrupt_says_friendly_yield_when_was_speaking() -> None:
    """Mid-reply cancel should not leave dead silence."""
    said: list[str] = []

    async def _yield(phrase: str) -> None:
        said.append(phrase)

    runtime = DuplexRuntime.create(session_id="yield-session")
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("讲个故事")
    await runtime.on_assistant_speaking("很长的故事内容")
    runtime._was_speaking = True
    runtime.input_guard.candidate_text = "停一下"
    runtime.set_interrupt_yield(_yield)
    fence_before = runtime.fence
    new_fence = await runtime.on_real_interrupt(cause="livekit_playback_interrupted")
    assert not new_fence.matches(fence_before)
    # yield is spawned; allow it to run
    await asyncio.sleep(0.05)
    assert said == ["嗯，你说。"]
    await runtime.close()


@pytest.mark.asyncio
async def test_pause_clears_livekit_turn_before_ack_and_marks_the_next_resume() -> None:
    """Production trace must not merge yield TTS,「等一下」and「继续」into one chat turn."""

    events: list[str] = []

    def _clear_user_turn() -> None:
        events.append("clear")

    async def _yield(_phrase: str) -> None:
        events.append("ack")

    runtime = DuplexRuntime.create(session_id="pause-then-resume")
    runtime.set_user_turn_clearer(_clear_user_turn)
    runtime.set_interrupt_yield(_yield)
    await runtime.orchestrator.ready()
    owner = _speaker_decision()
    await _classify_speaker(runtime, owner)
    await runtime.on_turn_committed("介绍一下南京")
    await runtime.on_assistant_speaking("南京是江苏省省会，也是中国四大古都之一。")
    runtime._was_speaking = True
    runtime.input_guard.candidate_text = "等一下"

    first = await runtime.on_real_interrupt(
        cause="target_speaker_confirmed",
        synchronized_transcript="南京是江苏省省会，",
    )
    await runtime.on_real_interrupt(
        cause="livekit_playback_interrupted",
        synchronized_transcript="南京是江苏省省会，",
    )
    await asyncio.sleep(0.05)

    assert events == ["clear", "ack"]
    await _classify_speaker(runtime, owner)
    accepted, reason = runtime.accept_user_turn(
        "好的，好的。 等一下。 继续。",
        speech_anchored=True,
    )
    assert accepted is True
    assert reason is None
    resumed = await runtime.on_turn_committed("好的，好的。 等一下。 继续。")
    assert resumed.turn_id == first.turn_id + 1
    assert runtime.is_resume_generation(resumed)
    await runtime.close()


@pytest.mark.asyncio
async def test_different_owner_profile_cannot_resume_the_interrupted_reply() -> None:
    runtime = DuplexRuntime.create(session_id="resume-speaker-mismatch")
    await runtime.orchestrator.ready()
    await _classify_speaker(runtime, _speaker_decision(profile_id="profile-owner-a"))
    await runtime.on_turn_committed("说说我的私人安排")
    await runtime.on_assistant_speaking("你的私人安排是周末回家。")
    runtime._was_speaking = True
    runtime.input_guard.candidate_text = "等一下"
    await runtime.on_real_interrupt(cause="livekit_playback_interrupted")

    await _classify_speaker(runtime, _speaker_decision(profile_id="profile-owner-b"))
    accepted, reason = runtime.accept_user_turn("继续", speech_anchored=True)
    resumed = await runtime.on_turn_committed("继续")

    assert accepted is True
    assert reason is None
    assert not runtime.is_resume_generation(resumed)
    await runtime.close()


@pytest.mark.asyncio
async def test_different_shadow_profile_cannot_resume_the_interrupted_reply() -> None:
    runtime = DuplexRuntime.create(session_id="resume-shadow-mismatch")
    await runtime.orchestrator.ready()
    await _classify_speaker(
        runtime,
        _shadow_speaker_decision(profile_id="profile-shadow-a"),
    )
    await runtime.on_turn_committed("介绍一下南京")
    await runtime.on_assistant_speaking("南京是江苏省省会。")
    runtime._was_speaking = True
    runtime.input_guard.candidate_text = "等一下"
    await runtime.on_real_interrupt(cause="livekit_playback_interrupted")

    await _classify_speaker(
        runtime,
        _shadow_speaker_decision(profile_id="profile-shadow-b"),
    )
    accepted, reason = runtime.accept_user_turn("继续", speech_anchored=True)
    resumed = await runtime.on_turn_committed("继续")

    assert accepted is True
    assert reason is None
    assert not runtime.is_resume_generation(resumed)
    await runtime.close()


@pytest.mark.asyncio
async def test_interrupt_with_content_does_not_play_a_control_ack() -> None:
    """「等一下我想问…」interrupts, then leaves the actual question to chat."""
    said: list[str] = []
    cleared: list[str] = []

    async def _yield(phrase: str) -> None:
        said.append(phrase)

    runtime = DuplexRuntime.create(session_id="interrupt-then-chat")
    runtime.set_user_turn_clearer(lambda: cleared.append("clear"))
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("讲个故事")
    await runtime.on_assistant_speaking("很长的故事内容")
    runtime._was_speaking = True
    runtime.input_guard.candidate_text = "等一下我想问个事"
    runtime.set_interrupt_yield(_yield)
    before = runtime.fence

    returned = await runtime.on_real_interrupt(cause="livekit_playback_interrupted")
    await asyncio.sleep(0.05)

    assert not returned.matches(before)
    assert said == []
    assert cleared == []
    await runtime.close()


@pytest.mark.asyncio
async def test_plain_chat_interrupt_does_not_play_a_control_ack() -> None:
    """A plain new question must not overlap its reply with an interrupt yield."""
    said: list[str] = []

    async def _yield(phrase: str) -> None:
        said.append(phrase)

    runtime = DuplexRuntime.create(session_id="plain-chat-no-ack")
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("讲个故事")
    await runtime.on_assistant_speaking("很长的故事内容")
    await runtime.on_playback_started()
    playback_fence = runtime.fence
    runtime._was_speaking = True
    runtime.input_guard.candidate_text = "再见"
    runtime.set_interrupt_yield(_yield)

    await runtime.on_real_interrupt(cause="livekit_playback_interrupted")
    await asyncio.sleep(0.05)

    assert said == []
    assert runtime._playback_fence is not None
    assert runtime._playback_fence.matches(playback_fence)
    await runtime.close()


@pytest.mark.asyncio
async def test_clear_user_turn_failure_does_not_block_yield_or_restore_listening() -> None:
    """A LiveKit clear failure must not strand the conversation before the ack."""

    said: list[str] = []

    def _clear_user_turn() -> None:
        raise ValueError("injected clear failure")

    async def _yield(phrase: str) -> None:
        said.append(phrase)

    runtime = DuplexRuntime.create(session_id="clear-failure")
    runtime.set_user_turn_clearer(_clear_user_turn)
    runtime.set_interrupt_yield(_yield)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("介绍一下南京")
    await runtime.on_assistant_speaking("南京是江苏省省会。")
    runtime._was_speaking = True
    runtime.input_guard.candidate_text = "等一下"

    await runtime.on_real_interrupt(cause="livekit_playback_interrupted")
    await asyncio.sleep(0.05)

    assert said == ["嗯，你说。"]
    assert runtime.interaction_phase.value == "listening"
    await runtime.close()


@pytest.mark.asyncio
async def test_resume_after_control_grace_accepts_missing_speech_anchor() -> None:
    """LiveKit can omit speaking metrics for「继续」right after a control ack."""

    async def _yield(_phrase: str) -> None:
        return None

    runtime = DuplexRuntime.create(
        session_id="resume-missing-anchor",
        input_guard_enabled=True,
    )
    runtime.set_interrupt_yield(_yield)
    await runtime.orchestrator.ready()
    owner = _speaker_decision()
    await _classify_speaker(runtime, owner)
    await runtime.on_turn_committed("介绍一下南京")
    await runtime.on_assistant_speaking("南京是江苏省省会。")
    runtime._was_speaking = True
    runtime.input_guard.candidate_text = "等一下"
    await runtime.on_real_interrupt(cause="livekit_playback_interrupted")
    await asyncio.sleep(0.05)
    await _classify_speaker(runtime, owner)
    runtime._fresh_user_speech = False

    accepted, reason = runtime.accept_user_turn("继续", speech_anchored=False)

    assert accepted is True
    assert reason is None
    resumed = await runtime.on_turn_committed("继续")
    assert runtime.is_resume_generation(resumed)
    await runtime.close()


@pytest.mark.asyncio
async def test_explicit_stop_phrase_yields_not_continue() -> None:
    """「停一下」must say 嗯你说, not 我继续 (owner cmd overrides short speaker score)."""
    said: list[str] = []

    async def _yield(phrase: str) -> None:
        said.append(phrase)

    async def _recover() -> None:
        said.append("我继续。")

    from services.agent.tests.unit.test_speaker_verify import _signal_pcm

    verifier = SpeakerVerifier(
        enabled=True,
        enroll_speech_ms=1200,
        enroll_timeout_ms=5000,
        accept_threshold=0.70,
        min_verify_speech_ms=400,
    )
    verifier.begin_enrollment()
    verifier.feed_pcm(_signal_pcm(kind="owner", seconds=2.0, seed=41))
    assert verifier.try_finalize_enrollment() is not None
    runtime = DuplexRuntime.create(
        session_id="stop-cmd",
        speaker_verifier=verifier,
        input_guard_enabled=True,
    )
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("讲个故事")
    await runtime.on_assistant_speaking("很长的故事")
    runtime._was_speaking = True
    runtime._playback_started_ns = __import__("time").monotonic_ns()
    runtime.input_guard.candidate_text = "停一下"
    runtime.set_interrupt_yield(_yield)
    runtime.set_false_interrupt_recover(_recover)
    await runtime.on_real_interrupt(cause="livekit_playback_interrupted")
    await asyncio.sleep(0.05)
    assert said == ["嗯，你说。"]
    # After yield, must hand floor back so next user speech is not blackholed.
    assert runtime.interaction_phase.value == "listening"
    await runtime.close()


@pytest.mark.asyncio
async def test_interrupt_command_restores_listen_and_unlocks_min_words() -> None:
    """Regression: after「停一下」commit, do not leave min_words=1000 + interrupted."""
    min_words: list[int] = []

    runtime = DuplexRuntime.create(session_id="unlock-after-stop", input_guard_enabled=True)
    runtime._base_interruption_min_words = 0
    runtime._set_interruption_min_words = min_words.append  # type: ignore[method-assign]
    await runtime.orchestrator.ready()
    runtime.set_interaction_phase(
        __import__(
            "services.agent.src.orchestration.state_machine", fromlist=["InteractionPhase"]
        ).InteractionPhase.INTERRUPTED,
        cause="test",
        publish=False,
    )
    accepted, reason = runtime.accept_user_turn("啊，停一下，停一下！", speech_anchored=True)
    assert accepted is False
    assert reason == "interrupt_command_only"
    assert runtime.interaction_phase.value == "listening"
    assert 0 in min_words
    await runtime.close()


@pytest.mark.asyncio
async def test_interrupt_diagnostics_never_expose_transcript_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime = DuplexRuntime.create(session_id="private-interrupt")
    runtime.set_event_publisher(publish)
    await runtime.orchestrator.ready()
    private_text = "啊，停一下，停一下！"
    caplog.set_level(logging.INFO, logger="services.agent.src.duplex_runtime")

    accepted, reason = runtime.accept_user_turn(private_text, speech_anchored=True)
    await asyncio.sleep(0)

    assert accepted is False
    assert reason == "interrupt_command_only"
    assert private_text not in "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "services.agent.src.duplex_runtime"
    )
    traces = [event for event in published if event.get("type") == "audio_trace"]
    assert traces
    for trace in traces:
        detail = trace.get("detail")
        if isinstance(detail, dict):
            assert "text" not in detail
            assert "candidate" not in detail
    assert any(
        isinstance(trace.get("detail"), dict)
        and trace["detail"].get("text_len") == len(private_text)  # type: ignore[index,union-attr]
        for trace in traces
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_immediate_close_after_control_turn_does_not_leak_coroutine() -> None:
    runtime = DuplexRuntime.create(session_id="immediate-close")
    await runtime.orchestrator.ready()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert runtime.accept_user_turn("等等", speech_anchored=None) == (
            False,
            "interrupt_command_only",
        )
        await runtime.close()
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)

    assert [
        warning
        for warning in caught
        if issubclass(warning.category, RuntimeWarning)
        and "was never awaited" in str(warning.message)
    ] == []


@pytest.mark.asyncio
async def test_after_control_chat_fail_opens_missing_speech_epoch() -> None:
    """After 停一下, next real question without LiveKit anchors must still answer."""
    runtime = DuplexRuntime.create(
        session_id="epoch-fail-open",
        input_guard_enabled=True,
    )
    await runtime.orchestrator.ready()
    runtime._restore_listen_after_control(cause="test_yield")
    # Orphan FINAL: metrics present but empty anchors, fresh speech cleared.
    runtime._fresh_user_speech = False
    accepted, reason = runtime.accept_user_turn(
        "你叫什么名字",
        speech_anchored=False,
    )
    assert accepted is True
    assert reason is None or reason != "missing_speech_epoch"
    await runtime.close()


@pytest.mark.asyncio
async def test_current_vad_accepts_final_without_livekit_endpoint_metrics() -> None:
    """A real current VAD epoch must survive an endpoint callback with no metrics."""
    runtime = DuplexRuntime.create(
        session_id="current-vad-missing-metrics",
        input_guard_enabled=True,
    )
    await runtime.orchestrator.ready()
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()

    accepted, reason = runtime.accept_user_turn(
        "再说一遍",
        speech_anchored=False,
        canonical_speech_epoch=runtime._speaker_epoch,
    )

    assert accepted is True
    assert reason is None
    await runtime.close()


@pytest.mark.asyncio
async def test_orphan_final_without_current_vad_stays_rejected() -> None:
    """A metric-less final without current VAD PCM remains playback-echo-safe."""
    runtime = DuplexRuntime.create(
        session_id="orphan-final-stays-rejected",
        input_guard_enabled=True,
    )
    await runtime.orchestrator.ready()

    accepted, reason = runtime.accept_user_turn(
        "后到字幕",
        speech_anchored=False,
    )

    assert accepted is False
    assert reason == "missing_speech_epoch"
    await runtime.close()


@pytest.mark.asyncio
async def test_stop_talking_phrase_acks_quietly() -> None:
    """「别说了 / 暂停」must ack「好的。」not invite「嗯，你说。」"""
    said: list[str] = []

    async def _yield(phrase: str) -> None:
        said.append(phrase)

    runtime = DuplexRuntime.create(session_id="quiet-ack")
    await runtime.orchestrator.ready()
    runtime.set_interrupt_yield(_yield)

    await runtime.on_turn_committed("讲故事")
    await runtime.on_assistant_speaking("故事开始")
    runtime._was_speaking = True
    runtime.input_guard.candidate_text = "别说了"
    await runtime.on_real_interrupt(cause="livekit_playback_interrupted")
    await asyncio.sleep(0.1)
    assert said == ["好的。"]

    said.clear()
    runtime._last_interrupt_yield_ns = None
    await runtime.on_turn_committed("再讲讲")
    await runtime.on_assistant_speaking("第二段故事")
    runtime._was_speaking = True
    runtime.input_guard.candidate_text = "暂停"
    await runtime.on_real_interrupt(cause="livekit_playback_interrupted")
    await asyncio.sleep(0.1)
    assert said == ["好的。"]
    await runtime.close()


@pytest.mark.asyncio
async def test_guest_interrupt_is_not_muted_by_legacy_voice_mismatch() -> None:
    """A real guest may interrupt; log-mel mismatch is not identity authority."""
    recovered: list[str] = []

    async def _recover() -> None:
        recovered.append("我继续。")

    from services.agent.tests.unit.test_speaker_verify import _signal_pcm

    verifier = SpeakerVerifier(
        enabled=True,
        enroll_speech_ms=1200,
        enroll_timeout_ms=5000,
        accept_threshold=0.70,
        min_verify_speech_ms=400,
    )
    verifier.begin_enrollment()
    verifier.feed_pcm(_signal_pcm(kind="owner", seconds=2.0, seed=31))
    assert verifier.try_finalize_enrollment() is not None
    runtime = DuplexRuntime.create(
        session_id="recover-session",
        speaker_verifier=verifier,
        input_guard_enabled=True,
    )
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("你叫什么名字")
    await runtime.on_assistant_speaking("我叫记忆助手")
    runtime._was_speaking = True
    runtime._playback_started_ns = __import__("time").monotonic_ns()
    runtime.set_false_interrupt_recover(_recover)
    # Nearby talker audio in rolling window → guest interruption, not silence.
    runtime.feed_speaker_pcm(_signal_pcm(kind="bystander", seconds=4.0, seed=32))
    fence_before = runtime.fence
    new_fence = await runtime.on_real_interrupt(cause="livekit_playback_interrupted")
    assert not new_fence.matches(fence_before)
    await asyncio.sleep(0.05)
    assert recovered == []
    await runtime.close()


@pytest.mark.asyncio
async def test_enrolled_barge_in_allows_a_real_guest_to_take_the_floor() -> None:
    from services.agent.src.orchestration.interruption_guard import PlaybackInputDecision
    from services.agent.tests.unit.test_speaker_verify import _signal_pcm

    verifier = SpeakerVerifier(
        enabled=True,
        enroll_speech_ms=1200,
        enroll_timeout_ms=5000,
        accept_threshold=0.70,
        min_verify_speech_ms=400,
    )
    verifier.begin_enrollment()
    verifier.feed_pcm(_signal_pcm(kind="owner", seconds=2.0, seed=21))
    assert verifier.try_finalize_enrollment() is not None
    runtime = DuplexRuntime.create(
        session_id="barge-noise",
        speaker_verifier=verifier,
        input_guard_enabled=True,
    )
    await runtime.orchestrator.ready()
    runtime._was_speaking = True
    # Overwrite rolling window with nearby talker only (4s rolling).
    other = _signal_pcm(kind="bystander", seconds=4.0, seed=22)
    runtime.feed_speaker_pcm(other)
    score = verifier.score_pcm()
    assert score.accepted is False
    decision = runtime.on_user_voice_started()
    assert decision is PlaybackInputDecision.WAIT
    await runtime.close()


@pytest.mark.asyncio
async def test_turn_commit_rejects_far_field_tablet_audio() -> None:
    """Quiet far-field media must not become a chat turn even if mel score is mid-band."""
    import numpy as np
    from services.agent.tests.unit.test_speaker_verify import _signal_pcm

    verifier = SpeakerVerifier(
        enabled=True,
        enroll_speech_ms=1200,
        enroll_timeout_ms=5000,
        accept_threshold=0.50,
        min_verify_speech_ms=400,
        far_field_rms_ratio=0.32,
    )
    verifier.begin_enrollment()
    verifier.feed_pcm(_signal_pcm(kind="owner", seconds=2.0, seed=61))
    assert verifier.try_finalize_enrollment() is not None
    runtime = DuplexRuntime.create(
        session_id="far-field",
        speaker_verifier=verifier,
        input_guard_enabled=True,
    )
    await runtime.orchestrator.ready()
    # Quiet bystander-like audio (tablet across room).
    far = _signal_pcm(kind="bystander", seconds=1.5, seed=62)
    samples = np.frombuffer(far, dtype="<i2").astype(np.float32) * 0.10
    quiet = np.clip(samples, -32767, 32767).astype(np.int16).tobytes()
    runtime.speaker_verifier.mark_utterance_start()
    runtime.feed_speaker_pcm(quiet)
    runtime.speaker_verifier.mark_utterance_end()
    allowed = runtime._speaker_allows_user_input(context="turn_commit")
    assert allowed is False
    await runtime.close()


@pytest.mark.asyncio
async def test_pure_wait_phrase_does_not_commit_chat_turn() -> None:
    """「等等」must not become LLM turn that answers 怎么了."""
    runtime = DuplexRuntime.create(session_id="no-zenmele")
    await runtime.orchestrator.ready()
    accepted, reason = runtime.accept_user_turn("嗯，等等，等等。", speech_anchored=None)
    assert accepted is False
    assert reason == "interrupt_command_only"
    accepted2, reason2 = runtime.accept_user_turn("等一下我想问个事", speech_anchored=None)
    # Has real content beyond command → normal turn
    assert accepted2 is True
    assert reason2 != "interrupt_command_only"
    await runtime.close()


@pytest.mark.asyncio
async def test_pending_enrollment_blocks_chat_turns() -> None:
    """Regression: enroll speech must not become generate_reply / skip-enroll race."""
    verifier = SpeakerVerifier(enabled=True, enroll_speech_ms=5000, enroll_timeout_ms=15000)
    assert verifier.state is SpeakerGateState.PENDING
    runtime = DuplexRuntime.create(session_id="enroll-gate", speaker_verifier=verifier)
    await runtime.orchestrator.ready()
    accepted, reason = runtime.accept_user_turn(
        "我是主人，请记住我的声音",
        speech_anchored=None,
    )
    assert accepted is False
    assert reason == "speaker_enrolling"
    await runtime.close()


@pytest.mark.asyncio
async def test_enroll_collects_pcm_even_if_was_speaking_stuck() -> None:
    """session.say can leave _was_speaking True; enroll must still capture mic PCM."""
    import math
    import struct

    verifier = SpeakerVerifier(enabled=True, enroll_speech_ms=800, enroll_timeout_ms=5000)
    runtime = DuplexRuntime.create(session_id="enroll-pcm", speaker_verifier=verifier)
    await runtime.orchestrator.ready()
    runtime._was_speaking = True
    runtime.begin_speaker_enrollment()
    assert runtime._was_speaking is False
    assert runtime._enroll_collecting is True
    # 1s of voiced-like tone @16k
    n = 16000
    samples = [int(12000 * math.sin(2 * math.pi * 180 * i / 16000)) for i in range(n)]
    pcm = struct.pack("<" + "h" * n, *samples)
    runtime.feed_speaker_pcm(pcm)
    result = runtime.poll_speaker_enrollment()
    assert result is not None
    assert result["reason"] == "enrolled"
    accepted, reason = runtime.accept_user_turn("你好", speech_anchored=None)
    assert accepted is True
    assert reason is None or reason == "ok" or accepted
    await runtime.close()


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
            "packets_lost_delta": 0,
            "packets_received_delta": 80,
            "packets_discarded_delta": 0,
            "bytes_received": 12_000,
            "concealed_samples": 480,
            "concealed_samples_delta": 0,
            "silent_concealed_samples": 240,
            "total_samples_received": 48_000,
            "total_samples_received_delta": 24_000,
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
        event.get("type") == "assistant_state" and event.get("state") == "thinking_silent"
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
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    tts = CosyVoiceTTS(CosyVoiceConfig(api_key="key", ws_url="wss://example", pool_size=0))
    runtime = DuplexRuntime.create(session_id="emotion-session", tts=tts)
    runtime.set_event_publisher(publish)
    await runtime.orchestrator.ready()

    first = runtime.observe_acoustic_emotion("sad", text="最近有点累", turn_id=1)
    await asyncio.sleep(0)
    first_event = next(event for event in published if event["type"] == "emotion_observation")
    assert first_event["turn_id"] == 1
    assert first_event["generation_id"] == 1
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
    tts = CosyVoiceTTS(CosyVoiceConfig(api_key="key", ws_url="wss://example", pool_size=0))
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
    tts = CosyVoiceTTS(CosyVoiceConfig(api_key="key", ws_url="wss://example", pool_size=0))
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
    key = f"{fence.session_id}:{fence.turn_id}:{fence.generation_id}:{fence.tool_epoch}"
    assert key in tts.pool.active_by_fence
    await runtime.on_real_interrupt()
    assert key not in tts.pool.active_by_fence
    assert tts.pool.discarded_count >= 1
