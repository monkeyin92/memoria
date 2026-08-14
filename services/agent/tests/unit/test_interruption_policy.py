"""Contract tests for the transport-neutral interruption policy."""

from __future__ import annotations

import pytest
from services.agent.src.orchestration.speech_timeline import SegmentKind, SpeechSegment
from services.agent.src.voice_core.interruption import (
    InterruptionEvidence,
    InterruptionPolicy,
    InterruptionSource,
    InterruptionVerdict,
    evidence_from_speech_segment,
)


def evidence(
    source: InterruptionSource,
    *,
    duration_ms: int = 0,
    aec_verified: bool = False,
    residual_echo_score: float | None = None,
    speaker_class: str = "uncertain",
) -> InterruptionEvidence:
    return InterruptionEvidence(
        session_id="session-1",
        stream_epoch=1,
        active_generation_id=4,
        detected_sample=320,
        source=source,
        duration_ms=duration_ms,
        aec_verified=aec_verified,
        residual_echo_score=residual_echo_score,
        speaker_class=speaker_class,
    )


@pytest.mark.parametrize(
    ("source", "text", "local_hard_stop", "verdict", "cancel"),
    [
        (InterruptionSource.BUTTON, "", False, InterruptionVerdict.HARD_STOP, True),
        (
            InterruptionSource.LOCAL_KWS,
            "opaque-model-label",
            True,
            InterruptionVerdict.HARD_STOP,
            True,
        ),
        (
            InterruptionSource.LOCAL_KWS,
            "停一下",
            False,
            InterruptionVerdict.FALSE_POSITIVE,
            False,
        ),
        (
            InterruptionSource.CLOUD_ASR,
            "停一下",
            False,
            InterruptionVerdict.TRUE_INTERRUPT,
            True,
        ),
        (
            InterruptionSource.CLOUD_ASR,
            "等一下我想问天气",
            False,
            InterruptionVerdict.TRUE_INTERRUPT,
            True,
        ),
    ],
)
def test_hard_stop_and_router_text_semantics(
    source: InterruptionSource,
    text: str,
    local_hard_stop: bool,
    verdict: InterruptionVerdict,
    cancel: bool,
) -> None:
    decision = InterruptionPolicy().evaluate(
        evidence(source),
        asr_text=text,
        local_hard_stop=local_hard_stop,
    )

    assert decision.verdict is verdict
    assert decision.cancel_generation is cancel


def test_short_backchannel_continues_playback() -> None:
    decision = InterruptionPolicy().evaluate(
        evidence(InterruptionSource.CLOUD_ASR, duration_ms=300),
        asr_text="嗯",
    )

    assert decision.verdict is InterruptionVerdict.BACKCHANNEL
    assert decision.backchannel
    assert decision.continue_output
    assert not decision.cancel_generation


def test_verified_residual_echo_beats_ordinary_cloud_text() -> None:
    decision = InterruptionPolicy().evaluate(
        evidence(
            InterruptionSource.CLOUD_ASR,
            duration_ms=800,
            aec_verified=True,
            residual_echo_score=0.9,
        ),
        asr_text="天气怎么样",
    )

    assert decision.verdict is InterruptionVerdict.FALSE_POSITIVE
    assert decision.reason == "residual_echo"


def test_safety_reply_keeps_physical_stop_but_rejects_semantic_interrupt() -> None:
    policy = InterruptionPolicy()

    assert policy.evaluate(
        evidence(InterruptionSource.BUTTON),
        safety_reply=True,
    ).cancel_generation
    semantic = policy.evaluate(
        evidence(InterruptionSource.CLOUD_ASR, duration_ms=900),
        asr_text="我想说话",
        safety_reply=True,
    )
    assert semantic.verdict is InterruptionVerdict.FALSE_POSITIVE
    assert not semantic.cancel_generation


def test_bystander_can_stop_public_output_without_granting_authority() -> None:
    decision = InterruptionPolicy().evaluate(
        evidence(
            InterruptionSource.LOCAL_VAD,
            duration_ms=650,
            speaker_class="guest",
        )
    )

    assert decision.verdict is InterruptionVerdict.TRUE_INTERRUPT
    assert decision.reason == "bystander_speech"


def test_child_threshold_is_longer_and_unverified_aec_stays_conservative() -> None:
    policy = InterruptionPolicy(
        adult_sustained_speech_ms=400,
        child_sustained_speech_ms=700,
        unverified_aec_sustained_speech_ms=600,
    )
    candidate = evidence(InterruptionSource.LOCAL_VAD, duration_ms=650)

    assert policy.evaluate(candidate, speaker_profile="adult").cancel_generation
    assert (
        policy.evaluate(candidate, speaker_profile="child").verdict
        is InterruptionVerdict.UNCERTAIN
    )


def test_segment_projection_preserves_acoustic_and_keyword_text() -> None:
    segment = SpeechSegment(
        session_id="session-1",
        stream_epoch=2,
        provider_task_epoch=0,
        segment_id="kws-1",
        revision=1,
        kind=SegmentKind.KWS,
        capture_start_sample=1_600,
        capture_end_sample=1_920,
        text="停一下",
        confidence=0.93,
        near_end_rms=0.4,
        far_end_rms=0.2,
        residual_echo_score=0.1,
    )

    projected = evidence_from_speech_segment(segment, active_generation_id=3)

    assert projected.source is InterruptionSource.LOCAL_KWS
    assert projected.asr_prefix == "停一下"
    assert projected.duration_ms == 20
    assert projected.near_end_rms == 0.4
    assert projected.far_end_rms == 0.2
    assert projected.residual_echo_score == 0.1
