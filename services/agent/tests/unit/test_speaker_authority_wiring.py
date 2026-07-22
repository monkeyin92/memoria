from __future__ import annotations

import asyncio

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


def _decision(
    classification: str,
    *,
    reason_code: str | None = None,
) -> SpeakerDecision:
    return SpeakerDecision(
        classification=classification,  # type: ignore[arg-type]
        score=0.95 if classification == "owner" else 0.1,
        quality_score=0.9,
        reason_code=reason_code
        or ("owner_match" if classification == "owner" else "owner_mismatch"),
        model_version="campplus-runtime-test",
        template_version=1,
        profile_id="profile-001",
        permissions=permissions_for_speaker(classification),  # type: ignore[arg-type]
    )


def _enable_companion_policy(runtime: DuplexRuntime) -> None:
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


@pytest.mark.asyncio
async def test_guest_classification_is_rejected_by_target_focus_but_closes_private_permissions() -> (
    None
):
    observed: dict[str, object] = {}
    focus_pcm = b"\x00\x01" * 12_800

    async def classify(pcm: bytes, sample_rate: int) -> SpeakerDecision:
        observed.update(pcm=pcm, sample_rate=sample_rate)
        return _decision("guest")

    runtime = DuplexRuntime.create()
    runtime.set_target_speaker_focus(True)
    runtime.set_speaker_classifier(classify, sample_rate=16000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(focus_pcm)
    runtime.on_user_voice_stopped()

    decision = await runtime.await_speaker_classification()
    accepted, reason = runtime.accept_user_turn("你好，我是他的朋友")

    assert observed == {"pcm": focus_pcm, "sample_rate": 16000}
    assert decision.classification == "guest"
    assert accepted is False
    assert reason == "target_non_owner"
    assert runtime.speaker_permissions.normal_conversation is True
    assert runtime.speaker_permissions.read_private_memory is False
    assert runtime.speaker_permissions.write_long_term_memory is False
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (_decision("owner"), True),
        (_decision("uncertain", reason_code="shadow_owner_candidate"), True),
        (_decision("guest"), False),
        (_decision("uncertain", reason_code="shadow_guest_candidate"), False),
    ],
)
async def test_transcript_history_eligibility_is_frozen_for_user_and_assistant(
    decision: SpeakerDecision,
    expected: bool,
) -> None:
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime = DuplexRuntime.create(session_id="history-binding")
    _enable_companion_policy(runtime)
    runtime.set_event_publisher(publish)
    runtime.set_reject_non_owner_voice(False)
    await runtime.orchestrator.ready()

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return decision

    runtime.set_speaker_classifier(classify, sample_rate=16000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x00\x01" * 800)
    runtime.on_user_voice_stopped()
    await runtime.await_speaker_classification()
    fence = await runtime.on_turn_committed("当前问题")
    runtime.publish_transcript(speaker="user", text="当前问题", final=True, fence=fence)
    await runtime.on_assistant_speaking("当前回答")
    await runtime.on_playback_started()
    await runtime.on_assistant_reply_completed("当前回答")
    await asyncio.sleep(0)

    finals = [
        event
        for event in published
        if event.get("type") == "transcript_delta" and event.get("final") is True
    ]
    assert [(event["speaker"], event["history_eligible"]) for event in finals] == [
        ("user", expected),
        ("assistant", expected),
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_interrupted_assistant_uses_the_original_generation_history_binding() -> None:
    published: list[dict[str, object]] = []
    archived: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    async def archive(event: dict[str, object]) -> None:
        archived.append(event)

    runtime = DuplexRuntime.create(session_id="interrupted-history-binding")
    _enable_companion_policy(runtime)
    runtime.set_event_publisher(publish)
    runtime.set_evidence_publisher(archive)
    await runtime.orchestrator.ready()
    runtime._speaker_decision = _decision("owner")
    runtime._speaker_class = "owner"
    old = await runtime.on_turn_committed("主人问题")
    runtime.publish_transcript(speaker="user", text="主人问题", final=True, fence=old)
    runtime.update_pending_assistant_text("主人回答还有未播放内容")
    await runtime.on_playback_started()

    await runtime.on_real_interrupt(
        cause="session.interrupt",
        synchronized_transcript="主人回答已听部分",
    )
    runtime._speaker_decision = _decision("guest")
    runtime._speaker_class = "guest"
    await runtime.on_playback_finished(
        playback_position_s=0.5,
        interrupted=True,
        synchronized_transcript="主人回答已听部分",
    )
    await asyncio.sleep(0)

    assistant = next(
        event
        for event in published
        if event.get("type") == "transcript_delta"
        and event.get("speaker") == "assistant"
        and event.get("final") is True
    )
    assert assistant["turn_id"] == old.turn_id
    assert assistant["generation_id"] == old.generation_id + 1
    assert assistant["history_eligible"] is True
    archived_assistant = next(
        event
        for event in archived
        if event.get("event_type") == "assistant.playout_stopped"
    )
    assert archived_assistant["turn_id"] == old.turn_id
    assert archived_assistant["generation_id"] == old.generation_id
    await runtime.close()


@pytest.mark.asyncio
async def test_non_owner_turn_cannot_start_the_background_deep_tool() -> None:
    class DeepStub:
        called = False

        async def stream_deep(self, *_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
            self.called = True
            if False:
                yield None

        async def aclose(self) -> None:
            return None

    deep = DeepStub()
    runtime = DuplexRuntime.create(session_id="guest-deep-tool-gate")
    runtime.configure_deep_path(deep)
    await runtime.orchestrator.ready()
    runtime._speaker_decision = _decision("guest")
    runtime._speaker_class = "guest"

    await runtime.on_turn_committed("请深入分析" + "这个问题" * 20)

    assert deep.called is False
    assert runtime.orchestrator.task_manager.active_count() == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_model_timeout_is_uncertain_and_cannot_late_promote_the_next_turn() -> None:
    release = asyncio.Event()

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        await release.wait()
        return _decision("owner")

    runtime = DuplexRuntime.create()
    runtime.set_speaker_classifier(classify, sample_rate=16000, timeout_s=0.001)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x00\x01")
    runtime.on_user_voice_stopped()

    decision = await runtime.await_speaker_classification()
    runtime.on_user_voice_started()
    release.set()
    await asyncio.sleep(0)

    assert decision.classification == "uncertain"
    assert decision.reason_code == "authority_timeout"
    assert runtime.speaker_permissions.read_private_memory is False
    assert runtime.speaker_permissions.write_long_term_memory is False
    await runtime.close()


@pytest.mark.asyncio
async def test_classification_and_user_final_use_the_same_speaker_class() -> None:
    evidence: list[dict[str, object]] = []
    voiced_pcm = b"\x00\x40" * 1600
    silent_pcm = b"\x00\x00" * 1600

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return _decision("owner")

    async def publish(event: dict[str, object]) -> None:
        evidence.append(event)

    runtime = DuplexRuntime.create(session_id="session-speaker-evidence")
    _enable_companion_policy(runtime)
    runtime.set_speaker_classifier(classify, sample_rate=16000)
    runtime.set_evidence_publisher(publish)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(voiced_pcm + silent_pcm)
    runtime.on_user_voice_stopped()
    await runtime.await_speaker_classification()
    fence = await runtime.on_turn_committed("这是我的经历")
    runtime.publish_transcript(
        speaker="user",
        text="这是我的经历",
        final=True,
        fence=fence,
    )
    await asyncio.sleep(0)

    assert [(item["event_type"], item["speaker_class"]) for item in evidence] == [
        ("speaker.classified", "owner"),
        ("speech.utterance_finalized", "owner"),
    ]
    assert evidence[1]["payload"] == {
        "text": "这是我的经历",
        "persona_eligible": False,
        "speaker_reason_code": "owner_match",
        "speaker_profile_id": "profile-001",
        "speaker_quality_score": 0.9,
        "speaker_model_version": "campplus-runtime-test",
        "speaker_template_version": 1,
        "speech_ms": 100,
        "pause_ratio": pytest.approx(0.5),
        "quality_score": 0.9,
        "interaction_mode": "companion",
        "mode_policy_version": "test-policy",
        "simulated_output": False,
        "history_eligible": True,
        "owner_projection_eligible": True,
    }
    await runtime.close()


@pytest.mark.asyncio
async def test_speaker_authority_wait_is_visible_in_endpointing_latency() -> None:
    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        await asyncio.sleep(0.01)
        return _decision("owner")

    runtime = DuplexRuntime.create(session_id="session-speaker-latency")
    runtime.set_speaker_classifier(classify, sample_rate=16000, timeout_s=0.1)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x00\x01")
    runtime.on_user_voice_stopped()
    stopped_at = runtime.latency_trace.marks.get("last_user_audio")

    await runtime.await_speaker_classification()
    await runtime.on_turn_committed("这是我的经历")

    assert stopped_at is not None
    assert runtime.latency_trace.marks["last_user_audio"] == stopped_at
    endpointing = runtime.latency_trace.derived()["endpointing_latency"]
    assert endpointing is not None and endpointing >= 0.005
    await runtime.close()
