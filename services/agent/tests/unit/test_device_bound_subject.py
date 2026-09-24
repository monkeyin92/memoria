"""A device serves its one bound person: owner data authority without a voiceprint."""

from __future__ import annotations

import time

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.runtime_speaker import POST_PLAYBACK_CLOSE_ECHO_GUARD_MS
from services.agent.tests.unit.runtime_profile_test_helpers import (
    bind_owner_policy,
    canonical_wire_payload,
)
from services.speaker.domain import (
    DEVICE_BOUND_SUBJECT_REASON,
    SpeakerDecision,
    permissions_for_speaker,
)


def _device_runtime(session_id: str) -> DuplexRuntime:
    runtime = DuplexRuntime.create(session_id=session_id, device_id="dev_01J_test")
    runtime.set_device_conversation_controls(True)
    return runtime


@pytest.mark.asyncio
async def test_bound_subject_carries_owner_data_authority_but_not_a_verified_voice() -> None:
    runtime = _device_runtime("device-bound-owner")
    bind_owner_policy(runtime)

    decision = await runtime.await_speaker_classification()

    assert decision.classification == "owner"
    assert decision.reason_code == DEVICE_BOUND_SUBJECT_REASON
    assert runtime.current_speaker_class == "owner"
    # History and memory follow the signed profile ...
    assert runtime.current_history_eligible is True
    # ... but the robot's own echo must never count as an owner voice.
    assert runtime.current_speaker_authority_verified is False
    await runtime.close()


@pytest.mark.asyncio
async def test_bound_subject_needs_private_recall_in_the_signed_profile() -> None:
    runtime = _device_runtime("device-bound-no-recall")
    bind_owner_policy(runtime)
    verified = runtime.mode_policy.runtime_profile
    assert verified is not None
    from dataclasses import replace

    from services.agent.src.runtime_profile import parse_runtime_profile
    from services.agent.tests.unit.runtime_profile_test_helpers import TEST_VERIFY_KEY

    chat_only = parse_runtime_profile(
        canonical_wire_payload(
            session_id=runtime.session_id,
            runtime_profile_id=f"rp_chat_{runtime.session_id}",
            active_subject_id="person_child",
            speaker_state="confirmed",
            service_mode="student_minor",
            session_epoch=1,
            capabilities=["chat"],
        ),
        verify_key=TEST_VERIFY_KEY,
    )
    assert chat_only is not None
    runtime.set_mode_policy(replace(runtime.mode_policy, runtime_profile=chat_only))

    decision = await runtime.await_speaker_classification()

    assert decision.classification == "uncertain"
    assert decision.reason_code == "authority_unconfigured"
    assert runtime.current_history_eligible is False
    await runtime.close()


@pytest.mark.asyncio
async def test_bound_subject_is_device_only() -> None:
    runtime = DuplexRuntime.create(session_id="app-no-binding", device_id="dev_01J_test")
    bind_owner_policy(runtime)

    decision = await runtime.await_speaker_classification()

    assert decision.classification == "uncertain"
    await runtime.close()


@pytest.mark.asyncio
async def test_a_running_voiceprint_keeps_its_own_decision() -> None:
    runtime = _device_runtime("device-voiceprint-still-on")
    bind_owner_policy(runtime)

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="uncertain",
            score=0.5,
            quality_score=0.9,
            reason_code="ambiguous_score",
            model_version="campplus-test",
            template_version=1,
            profile_id="profile",
            permissions=permissions_for_speaker("uncertain"),
        )

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime._speaker_pcm.extend(b"\x00\x20" * 8_000)  # noqa: SLF001 - classifier input seam

    decision = await runtime.await_speaker_classification()

    assert decision.reason_code == "ambiguous_score"
    assert runtime.current_speaker_class == "uncertain"
    await runtime.close()


def _just_played(runtime: DuplexRuntime, text: str, *, ago_ms: int) -> int:
    now_ns = time.monotonic_ns()
    runtime._played_assistant_text = text  # noqa: SLF001 - playback tail seam
    runtime._last_playback_completed_ns = now_ns - ago_ms * 1_000_000  # noqa: SLF001
    return now_ns


@pytest.mark.asyncio
async def test_farewell_echo_right_after_playback_cannot_close_the_device() -> None:
    runtime = _device_runtime("device-farewell-echo")
    now_ns = _just_played(runtime, "好的，那我们明天见，再见！", ago_ms=1_200)

    assert runtime._close_phrase_is_playback_echo("再见", now_ns=now_ns) is True  # noqa: SLF001
    # A farewell the robot did not just say is the user's own.
    assert runtime._close_phrase_is_playback_echo("拜拜", now_ns=now_ns) is False  # noqa: SLF001
    await runtime.close()


@pytest.mark.asyncio
async def test_farewell_after_the_echo_window_closes_normally() -> None:
    runtime = _device_runtime("device-farewell-late")
    now_ns = _just_played(
        runtime, "再见！", ago_ms=POST_PLAYBACK_CLOSE_ECHO_GUARD_MS + 500
    )

    assert runtime._close_phrase_is_playback_echo("再见", now_ns=now_ns) is False  # noqa: SLF001
    await runtime.close()


@pytest.mark.asyncio
async def test_farewell_echo_guard_is_device_only() -> None:
    runtime = DuplexRuntime.create(session_id="app-farewell", device_id="dev_01J_test")
    now_ns = _just_played(runtime, "再见！", ago_ms=500)

    assert runtime._close_phrase_is_playback_echo("再见", now_ns=now_ns) is False  # noqa: SLF001
    await runtime.close()


@pytest.mark.asyncio
async def test_accept_user_turn_refuses_a_farewell_echo_on_the_device() -> None:
    runtime = _device_runtime("device-farewell-accept")
    bind_owner_policy(runtime)
    _just_played(runtime, "明天见，再见！", ago_ms=800)

    accepted, reason = runtime.accept_user_turn("再见")

    assert accepted is False
    assert reason == "conversation_end_playback_echo"
    await runtime.close()
