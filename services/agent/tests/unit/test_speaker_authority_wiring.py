from __future__ import annotations

import asyncio

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


def _decision(classification: str) -> SpeakerDecision:
    return SpeakerDecision(
        classification=classification,  # type: ignore[arg-type]
        score=0.95 if classification == "owner" else 0.1,
        quality_score=0.9,
        reason_code="owner_match" if classification == "owner" else "owner_mismatch",
        model_version="campplus-runtime-test",
        template_version=1,
        profile_id="profile-001",
        permissions=permissions_for_speaker(classification),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_guest_classification_is_rejected_by_target_focus_but_closes_private_permissions() -> None:
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
        "speech_ms": 100,
        "pause_ratio": pytest.approx(0.5),
        "quality_score": 0.9,
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
