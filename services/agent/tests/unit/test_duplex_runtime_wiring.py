"""Agent entry wires Orchestrator / GenerationFence / atomic interrupt."""

from __future__ import annotations

import asyncio
import gc
import logging
import warnings
from typing import Any

import pytest
from services.agent.src import llm_types as llm
from services.agent.src.agent_voice_profile import _heard_only_chat_context
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.interruption_guard import PlaybackInputDecision
from services.agent.src.orchestration.speaker_verify import (
    SpeakerGateState,
    SpeakerVerifier,
)
from services.agent.src.orchestration.state_machine import ConversationState
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig
from services.agent.src.providers.doubao_voice_catalog import catalog_by_id
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.tests.unit.runtime_profile_test_helpers import (
    bind_owner_policy,
    canonical_wire_payload,
)
from services.agent.tests.unit.runtime_state_helpers import (
    commit_media_turn,
    set_floor,
    set_pending_assistant_text,
)
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


async def _classify_speaker(runtime: DuplexRuntime, decision: SpeakerDecision) -> None:
    async def _classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return decision

    runtime.set_speaker_classifier(_classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    assert await runtime.await_speaker_classification() is decision


@pytest.mark.asyncio
async def test_uncertain_user_evidence_carries_shadow_owner_provenance() -> None:
    evidence: list[dict[str, object]] = []

    async def _publish(event: dict[str, object]) -> None:
        evidence.append(event)

    runtime = DuplexRuntime.create(session_id="shadow-persona-session")
    bind_owner_policy(
        runtime,
        private_context=True,
        owner_evidence=True,
        tools=True,
        voice_profile=True,
        shadow_low_sensitivity_persona=True,
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
async def test_media_playback_done_arms_post_playback_weekday_echo_guard() -> None:
    """media-v1 completion must stamp the same echo window as LiveKit."""

    runtime = DuplexRuntime.create(
        session_id="media-post-playback-weekday-echo",
        input_guard_enabled=True,
        barge_in_enabled=False,
    )
    await runtime.orchestrator.ready()
    fence = await runtime.on_turn_committed("今天星期几")
    answer = "今天是2026年8月6日，星期四。"
    set_pending_assistant_text(runtime, answer)
    await runtime.orchestrator.begin_speaking([], answer)
    assert await runtime.on_media_playback_done(fence, answer)
    assert runtime._voice_floor.last_playback_completed_ns is not None
    assert "星期四" in runtime._voice_floor.played_assistant_text

    accepted, reason = runtime.accept_user_turn(
        "星期四",
        input_modality="audio",
        speech_anchored=True,
        canonical_speech_epoch=runtime._speaker_epoch,
    )
    assert accepted is False
    assert reason == "assistant_echo"
    await runtime.close()


@pytest.mark.asyncio
async def test_media_playback_done_falls_back_to_pending_text_for_echo_guard() -> None:
    runtime = DuplexRuntime.create(
        session_id="media-post-playback-empty-heard",
        input_guard_enabled=True,
        barge_in_enabled=False,
    )
    await runtime.orchestrator.ready()
    fence = await runtime.on_turn_committed("今天星期几")
    answer = "今天是星期四。"
    set_pending_assistant_text(runtime, answer)
    await runtime.orchestrator.begin_speaking([], answer)
    # Empty exact heard text (e.g. ledger gap) still arms weekday echo match.
    assert await runtime.on_media_playback_done(fence, "")
    assert runtime._voice_floor.played_assistant_text == answer
    assert runtime._voice_floor.last_playback_completed_ns is not None

    accepted, reason = runtime.accept_user_turn(
        "星期四",
        input_modality="audio",
        speech_anchored=True,
        canonical_speech_epoch=runtime._speaker_epoch,
    )
    assert accepted is False
    assert reason == "assistant_echo"
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
async def test_different_owner_profile_cannot_resume_the_interrupted_reply() -> None:
    runtime = DuplexRuntime.create(session_id="resume-speaker-mismatch")
    await runtime.orchestrator.ready()
    await _classify_speaker(runtime, _speaker_decision(profile_id="profile-owner-a"))
    await runtime.on_turn_committed("说说我的私人安排")
    await runtime.on_assistant_speaking("你的私人安排是周末回家。")
    set_floor(runtime, assistant_speaking=True)
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
    set_floor(runtime, assistant_speaking=True)
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
async def test_device_farewell_interrupt_does_not_restore_listen_before_close() -> None:
    """Playback farewell must not publish listening before the committed close."""
    runtime = DuplexRuntime.create(session_id="device-farewell-no-listen")
    runtime.set_device_conversation_controls(True)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("今天天气怎么样")
    await runtime.on_assistant_speaking("南宁今天多云。")
    set_floor(runtime, assistant_speaking=True)
    runtime.input_guard.candidate_text = "好的，再见"

    await runtime.on_real_interrupt(cause="livekit_playback_interrupted")
    await asyncio.sleep(0.05)

    assert runtime.interaction_phase.value == "interrupted"
    await runtime.close()


@pytest.mark.asyncio
async def test_interrupt_command_restores_listen() -> None:
    """Regression: after「停一下」commit, hand the floor back instead of staying interrupted."""
    runtime = DuplexRuntime.create(session_id="unlock-after-stop", input_guard_enabled=True)
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
    await runtime.close()


@pytest.mark.asyncio
async def test_stale_control_final_is_rejected_over_new_speech() -> None:
    published: list[dict[str, object]] = []

    async def _publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime = DuplexRuntime.create(input_guard_enabled=True)
    runtime.set_event_publisher(_publish)
    await runtime.orchestrator.ready()
    runtime.on_user_voice_started()
    stale_epoch = runtime._speaker_epoch
    runtime.on_user_voice_started()

    accepted, reason = runtime.accept_user_turn(
        "停一下",
        speech_anchored=True,
        canonical_speech_epoch=stale_epoch,
        canonical_snapshot_bound=True,
    )
    await asyncio.sleep(0)

    assert accepted is False
    assert reason == "stale_control_epoch"
    assert any(
        event.get("type") == "audio_trace" and event.get("name") == "control_turn_stale"
        for event in published
    )
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
    set_floor(runtime, fresh_user_speech=False)
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
async def test_snapshot_bound_accepts_without_livekit_metrics() -> None:
    """Device LiveKit sessions may omit timing metrics while the assembler still binds a VAD epoch."""
    runtime = DuplexRuntime.create(
        session_id="snapshot-bound-fail-open",
        input_guard_enabled=True,
    )
    await runtime.orchestrator.ready()
    runtime.on_user_voice_started()
    runtime.on_user_voice_started()

    accepted, reason = runtime.accept_user_turn(
        "今天星期几",
        speech_anchored=False,
        canonical_speech_epoch=1,
        canonical_snapshot_bound=True,
    )

    assert accepted is True
    assert reason is None
    await runtime.close()


@pytest.mark.asyncio
async def test_guest_interrupt_is_not_muted_by_legacy_voice_mismatch() -> None:
    """A real guest may interrupt; log-mel mismatch is not identity authority."""
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
    set_floor(runtime, assistant_speaking=True)
    # Nearby talker audio in rolling window → guest interruption, not silence.
    runtime.feed_speaker_pcm(_signal_pcm(kind="bystander", seconds=4.0, seed=32))
    fence_before = runtime.fence
    new_fence = await runtime.on_real_interrupt(cause="livekit_playback_interrupted")
    assert not new_fence.matches(fence_before)
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
    set_floor(runtime, assistant_speaking=True)
    # Overwrite rolling window with nearby talker only (4s rolling).
    other = _signal_pcm(kind="bystander", seconds=4.0, seed=22)
    runtime.feed_speaker_pcm(other)
    score = verifier.score_pcm()
    assert score.accepted is False
    decision = runtime.on_user_voice_started()
    assert decision is PlaybackInputDecision.WAIT
    await runtime.close()


@pytest.mark.asyncio
async def test_enrolled_playback_vad_start_waits_when_utterance_is_empty() -> None:
    """Owner barge-in at VAD start has a cleared buffer; too_short must WAIT."""
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
        session_id="barge-empty",
        speaker_verifier=verifier,
        input_guard_enabled=True,
    )
    runtime.set_device_conversation_controls(True)
    await runtime.orchestrator.ready()
    set_floor(runtime, assistant_speaking=True)
    # Enrollment PCM would otherwise remain in the 4s rolling window and look
    # like an immediate owner match. Playback barge-in starts with a cleared
    # utterance and, after a long reply, no scorable uplink yet.
    runtime.speaker_verifier._rolling.clear()
    runtime.speaker_verifier._utterance.clear()
    decision = runtime.on_user_voice_started()
    assert decision is PlaybackInputDecision.WAIT
    # Empty buffer is pending evidence: barge-in start WAITs, interrupt still
    # withholds cancel so a micro-blip cannot bump the generation fence.
    assert runtime._speaker_allows_user_input(context="barge_in_start") is True
    assert runtime._speaker_allows_user_input(context="interrupt") is False
    accepted, reason = runtime.accept_user_turn("好的，再见", speech_anchored=None)
    assert accepted is False
    assert reason == "conversation_end_explicit"
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
    set_floor(runtime, assistant_speaking=True)
    runtime.begin_speaker_enrollment()
    assert runtime.assistant_speaking is False
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


@pytest.mark.asyncio
async def test_runtime_commit_and_interrupt_bumps_fence() -> None:
    runtime = DuplexRuntime.create()
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
async def test_runtime_publishes_one_fence_bound_assistant_expression() -> None:
    runtime = DuplexRuntime.create(session_id="expression-session")
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    await runtime.orchestrator.ready()
    fence = await runtime.on_turn_committed("今天有个好消息")
    await runtime.on_assistant_speaking("太好了，这真值得庆祝！")
    await runtime.on_assistant_speaking("太好了，这真值得庆祝！")
    await asyncio.sleep(0)

    expressions = [event for event in published if event.get("type") == "assistant_expression"]
    assert len(expressions) == 1
    assert expressions[0] | {"at": ""} == {
        "type": "assistant_expression",
        "session_id": "expression-session",
        "expression": "happy",
        "turn_id": fence.turn_id,
        "generation_id": fence.generation_id,
        "tool_epoch": fence.tool_epoch,
        "session_epoch": fence.session_epoch,
        "device_id": None,
        "subject_revision": None,
        "active_subject_id": None,
        "runtime_profile_id": None,
        "actor_id": None,
        "binding_id": None,
        "binding_version": None,
        "event_sequence": expressions[0]["event_sequence"],
        "at": "",
    }
    assert isinstance(expressions[0]["at"], str)
    await runtime.close()


@pytest.mark.asyncio
async def test_publish_forces_authoritative_envelope_over_forged_fields() -> None:
    """§11.5: pre-filled session/turn/generation/tool/identity fields in an
    upstream event are unconditionally overwritten from the event's own fence
    (or the epoch-0 lifecycle envelope), so a forged identity can never
    survive publication."""

    runtime = DuplexRuntime.create(session_id="envelope-session")
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    fence = runtime.fence.bump_generation()
    runtime._publish(
        {
            "type": "assistant_state",
            "session_id": "evil-session",
            "session_epoch": 999,
            "turn_id": 999,
            "generation_id": 999,
            "tool_epoch": 999,
            "active_subject_id": "person_evil",
            "runtime_profile_id": "rp_evil",
            "actor_id": "actor_evil",
            "binding_id": "bind_evil",
            "binding_version": 999,
            "device_id": "dev_evil",
            "subject_revision": 999,
        },
        fence=fence,
    )
    runtime._publish(
        {
            "type": "listener_cue",
            "session_id": "evil-session",
            "session_epoch": 7,
            "turn_id": 7,
            "generation_id": 7,
            "tool_epoch": 7,
            "active_subject_id": "person_evil",
            "runtime_profile_id": "rp_evil",
            "device_id": "dev_evil",
            "subject_revision": 999,
        }
    )
    await asyncio.sleep(0)

    assert len(published) == 2
    fenced, lifecycle = published
    assert fenced["session_id"] == "envelope-session"
    assert fenced["session_epoch"] == fence.session_epoch
    assert fenced["turn_id"] == fence.turn_id
    assert fenced["generation_id"] == fence.generation_id
    assert fenced["tool_epoch"] == fence.tool_epoch
    assert fenced["active_subject_id"] is None
    assert fenced["runtime_profile_id"] is None
    assert fenced["actor_id"] is None
    assert fenced["binding_id"] is None
    assert fenced["binding_version"] is None
    assert fenced["device_id"] is None
    assert fenced["subject_revision"] is None
    assert isinstance(fenced["event_sequence"], int)
    assert lifecycle["session_epoch"] == 0
    assert lifecycle["turn_id"] == 0
    assert lifecycle["generation_id"] == 0
    assert lifecycle["tool_epoch"] == 0
    assert lifecycle["active_subject_id"] is None
    assert lifecycle["runtime_profile_id"] is None
    assert lifecycle["device_id"] is None
    assert lifecycle["subject_revision"] is None
    assert lifecycle["session_id"] == "envelope-session"
    assert isinstance(lifecycle["event_sequence"], int)
    await runtime.close()


@pytest.mark.asyncio
async def test_publish_envelope_carries_profile_device_and_subject_revision() -> None:
    """§11.5: a fenced event carries device_id/subject_revision from the exact
    signed profile; an epoch-0 lifecycle event carries null identity even when
    a profile is currently applied (forged pre-filled values are overwritten)."""

    runtime = DuplexRuntime.create(session_id="envelope-profile-session")
    bind_owner_policy(runtime)
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    runtime._publish(
        {
            "type": "assistant_state",
            "device_id": "dev_evil",
            "subject_revision": 999,
        },
        fence=runtime.fence,
    )
    runtime._publish(
        {
            "type": "listener_cue",
            "device_id": "dev_evil",
            "subject_revision": 999,
        }
    )
    await asyncio.sleep(0)

    fenced, lifecycle = published
    assert fenced["session_epoch"] == runtime.fence.session_epoch
    assert fenced["device_id"] == "dev_01J_test"
    assert fenced["subject_revision"] == 1
    assert fenced["active_subject_id"] == "person_owner"
    assert lifecycle["session_epoch"] == 0
    assert lifecycle["device_id"] is None
    assert lifecycle["subject_revision"] is None
    assert lifecycle["active_subject_id"] is None
    await runtime.close()


@pytest.mark.asyncio
async def test_late_old_epoch_event_keeps_its_own_epoch_identity() -> None:
    """§11.5: a late event frozen under a pre-switch fence carries its own old
    epoch and never borrows the newest subject/profile identity."""

    from services.agent.tests.unit.runtime_profile_test_helpers import (
        TEST_VERIFY_KEY,
    )

    runtime = DuplexRuntime.create(session_id="late-epoch-session")
    bind_owner_policy(runtime)
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    old_fence = runtime.fence
    runtime.orchestrator.runtime_profiles.verify_key = TEST_VERIFY_KEY
    switched = canonical_wire_payload(
        session_id="late-epoch-session",
        runtime_profile_id="rp_switched",
        active_subject_id="person_parent",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        service_mode="adult_companion",
        session_epoch=2,
        capabilities=["chat", "tutor", "english_practice"],
    )
    applied = runtime.apply_runtime_profile(switched)
    assert applied is not None
    assert runtime.fence.session_epoch == 2
    runtime._publish(
        {"type": "assistant_state"},
        fence=old_fence,
    )
    await asyncio.sleep(0)

    late = published[0]
    # The old epoch's profile is void after the switch: no identity is
    # borrowed from the current subject.
    assert late["session_epoch"] == 1
    assert late["turn_id"] == old_fence.turn_id
    assert late["generation_id"] == old_fence.generation_id
    assert late["active_subject_id"] is None
    assert late["runtime_profile_id"] is None
    assert late["actor_id"] is None
    await runtime.close()


@pytest.mark.asyncio
async def test_stale_generation_cannot_commit_speaking_state_or_expression() -> None:
    runtime = DuplexRuntime.create(session_id="stale-speaking-session")
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    await runtime.orchestrator.ready()
    fence = await runtime.on_turn_committed("第一条问题")
    replacement = fence.bump_generation()
    assert await runtime.accept_media_generation(replacement, cause="test_superseded")
    state = runtime.orchestrator.state
    published.clear()

    assert not await runtime.on_assistant_speaking(
        "已经失效的回答",
        expected_fence=fence,
    )
    await asyncio.sleep(0)

    assert runtime.fence.matches(replacement)
    assert runtime.orchestrator.state is state
    assert runtime.orchestrator.state is not ConversationState.SPEAKING
    assert runtime._voice_floor.pending_assistant_text == ""
    assert runtime.assistant_speaking is False
    assert runtime._assistant_expression_fence is None
    assert not any(
        event.get("type") == "assistant_expression"
        or (event.get("type") == "assistant_state" and event.get("state") == "speaking")
        for event in published
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_unheard_output_restores_half_duplex_listen() -> None:
    runtime = DuplexRuntime.create(
        session_id="unheard-output-listen",
        barge_in_enabled=False,
    )
    await runtime.orchestrator.ready()
    fence = await runtime.on_turn_committed("今天天气怎么样")
    assert await runtime.on_assistant_speaking("南京今天晴。", expected_fence=fence)
    assert runtime.assistant_speaking is True
    assert runtime.on_user_voice_started() is PlaybackInputDecision.IGNORE

    await runtime.restore_listen_after_unheard_output(fence, cause="stale_generation")

    assert runtime.assistant_speaking is False
    assert runtime.orchestrator.state is ConversationState.LISTENING
    assert runtime.on_user_voice_started() is PlaybackInputDecision.ACCEPT
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_passes_only_same_scope_actual_heard_context_to_doubao() -> None:
    tts = DoubaoTTS(
        DoubaoTTSConfig(
            api_key="test",
            speaker=catalog_by_id()["warm_companion"].speaker_id,
            style_control_enabled=True,
            pool_size=0,
        )
    )
    runtime = DuplexRuntime.create(session_id="tts-context-session", tts=tts)
    context = runtime.orchestrator.context
    context.add_user("主人说了私密安排", speaker_scope="owner")
    context.commit_assistant_heard("主人专属回复", speaker_scope="owner")
    context.add_user("我们刚才在聊咖啡", speaker_scope="public")
    context.commit_assistant_heard("你想学点咖啡可以说哪一种", speaker_scope="public")
    await runtime.orchestrator.ready()

    await runtime.on_turn_committed("如何用英语点一杯拿铁")

    assert len(tts.current_context_texts) == 1
    reference = tts.current_context_texts[0]
    assert "语音要求：" in reference
    assert "用户：我们刚才在聊咖啡" in reference
    assert "助手：你想学点咖啡可以说哪一种" in reference
    assert "用户：如何用英语点一杯拿铁" in reference
    assert "主人说了私密安排" not in reference
    assert "主人专属回复" not in reference
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_commits_new_user_turn_while_thinking() -> None:
    runtime = DuplexRuntime.create()
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
    runtime = DuplexRuntime.create()
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
    runtime = DuplexRuntime.create()
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

    assert [message.text_content for message in safe.items] == [
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

    assert [message.text_content for message in safe.items] == [
        "帮我安排口语训练",
        "实际听到的训练安排",
        "可以",
    ]


@pytest.mark.asyncio
async def test_confirm_interruption_cancels_registered_llm_task() -> None:
    runtime = DuplexRuntime.create()
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
    agent = ReplyPipeline(instructions="test", runtime=runtime)

    class Msg:
        def text_content(self) -> str:
            return "订下周三的票"

    await commit_media_turn(agent, Msg())
    assert runtime.fence.turn_id == 1
    assert runtime.orchestrator.context.turns[-1].content == "订下周三的票"


@pytest.mark.asyncio
async def test_controlled_turn_runtime_publishes_monotonic_input_policy() -> None:
    runtime = DuplexRuntime.create(session_id="mini-input-policy", barge_in_enabled=False)
    published: list[dict[str, Any]] = []

    async def publish(event: dict[str, Any]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    for state in ("thinking", "speaking", "listening"):
        task = runtime.publish_assistant_state(state)
        assert task is not None
        await task

    policies = [event for event in published if event["type"] == "input_policy"]
    states = [event for event in published if event["type"] == "assistant_state"]
    assert [
        (event["capture_allowed"], event["policy_epoch"], event["reason"]) for event in policies
    ] == [
        (False, 1, "assistant_thinking"),
        (False, 2, "assistant_speaking"),
        (True, 3, "assistant_listening"),
    ]
    assert all(event["session_id"] == runtime.session_id for event in policies)
    assert all(event["generation_id"] == runtime.fence.generation_id for event in policies)
    assert all(event["tool_epoch"] == runtime.fence.tool_epoch for event in states)


@pytest.mark.asyncio
async def test_device_capture_release_is_held_after_speaking() -> None:
    runtime = DuplexRuntime.create(
        session_id="device-capture-holdoff",
        barge_in_enabled=False,
        capture_release_holdoff_s=0.05,
    )
    published: list[dict[str, Any]] = []

    async def publish(event: dict[str, Any]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    await runtime.publish_assistant_state("speaking")
    await runtime.publish_assistant_state("listening")

    policies = [event for event in published if event["type"] == "input_policy"]
    assert [(event["capture_allowed"], event["reason"]) for event in policies] == [
        (False, "assistant_speaking"),
    ]

    await asyncio.sleep(0.08)
    policies = [event for event in published if event["type"] == "input_policy"]
    assert [(event["capture_allowed"], event["reason"]) for event in policies] == [
        (False, "assistant_speaking"),
        (True, "assistant_listening"),
    ]


@pytest.mark.asyncio
async def test_controlled_state_completion_waits_for_state_and_input_policy() -> None:
    runtime = DuplexRuntime.create(session_id="mini-state-barrier", barge_in_enabled=False)
    state_release = asyncio.Event()
    state_started = asyncio.Event()
    policy_release = asyncio.Event()
    policy_started = asyncio.Event()

    async def publish(event: dict[str, Any]) -> None:
        if event["type"] == "assistant_state":
            state_started.set()
            await state_release.wait()
        if event["type"] == "input_policy":
            policy_started.set()
            await policy_release.wait()

    runtime.set_event_publisher(publish)
    completion = runtime.publish_assistant_state("speaking")
    assert completion is not None
    await asyncio.gather(state_started.wait(), policy_started.wait())

    state_release.set()
    await asyncio.sleep(0)
    assert not completion.done()
    policy_release.set()
    await completion


@pytest.mark.asyncio
async def test_interrupt_discards_cosy_pool_binding() -> None:
    tts = CosyVoiceTTS(CosyVoiceConfig(api_key="t", ws_url="ws://127.0.0.1:9", pool_size=0))
    runtime = DuplexRuntime.create(tts=tts)
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
