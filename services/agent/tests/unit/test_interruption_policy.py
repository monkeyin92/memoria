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
    owner_authority_verified: bool = False,
    speaker_reason_code: str = "authority_unavailable",
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
        owner_authority_verified=owner_authority_verified,
        speaker_reason_code=speaker_reason_code,
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
        (
            InterruptionSource.CLOUD_ASR,
            "天气怎么样",
            False,
            InterruptionVerdict.UNCERTAIN,
            False,
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


def test_device_farewell_during_playback_is_a_true_interrupt() -> None:
    decision = InterruptionPolicy().evaluate(
        evidence(InterruptionSource.CLOUD_ASR, duration_ms=400),
        asr_text="好的，再见",
        device_conversation=True,
    )

    assert decision.verdict is InterruptionVerdict.TRUE_INTERRUPT
    assert decision.reason == "conversation_end_explicit"
    assert decision.cancel_generation


def test_verified_owner_authority_allows_ordinary_cloud_barge_in() -> None:
    decision = InterruptionPolicy().evaluate(
        evidence(
            InterruptionSource.CLOUD_ASR,
            speaker_class="owner",
            owner_authority_verified=True,
            speaker_reason_code="owner_match",
        ),
        asr_text="天气怎么样",
    )

    assert decision.verdict is InterruptionVerdict.TRUE_INTERRUPT
    assert decision.reason == "cloud_asr_user_content"
    assert decision.cancel_generation


def test_verified_aec_allows_ordinary_cloud_barge_in_without_owner_authority() -> None:
    decision = InterruptionPolicy().evaluate(
        evidence(InterruptionSource.CLOUD_ASR, aec_verified=True),
        asr_text="天气怎么样",
    )

    assert decision.verdict is InterruptionVerdict.TRUE_INTERRUPT
    assert decision.cancel_generation


def test_speaker_class_alone_does_not_prove_owner_authority() -> None:
    decision = InterruptionPolicy().evaluate(
        evidence(InterruptionSource.CLOUD_ASR, speaker_class="owner"),
        asr_text="天气怎么样",
    )

    assert decision.verdict is InterruptionVerdict.UNCERTAIN
    assert decision.reason == "speaker_authority_unverified"
    assert not decision.cancel_generation


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


def test_known_bystander_speech_never_cancels_the_playing_generation() -> None:
    """A nearby voice is not this account's owner and must not cut playback."""

    for source, duration_ms in (
        (InterruptionSource.CLOUD_ASR, 1_200),
        (InterruptionSource.LOCAL_VAD, 900),
    ):
        decision = InterruptionPolicy().evaluate(
            evidence(source, duration_ms=duration_ms, speaker_class="guest"),
            asr_text="我们下午几点出发",
        )

        assert decision.verdict is InterruptionVerdict.FALSE_POSITIVE
        assert decision.reason == "known_non_owner_speech"
        assert not decision.cancel_generation
        assert decision.continue_output


def test_aec_verification_does_not_admit_a_known_bystander() -> None:
    """AEC only removes the assistant's own echo, never proves who is talking."""

    decision = InterruptionPolicy().evaluate(
        evidence(
            InterruptionSource.CLOUD_ASR,
            duration_ms=1_200,
            aec_verified=True,
            residual_echo_score=0.05,
            speaker_class="guest",
            speaker_reason_code="owner_mismatch",
        ),
        asr_text="我们下午几点出发",
    )

    assert not decision.cancel_generation
    assert decision.continue_output


def test_unknown_speaker_farewell_still_stops_and_ends_playback() -> None:
    """Explicit farewell wording yields before any speaker-class rule."""

    for speaker_class in ("uncertain", "guest"):
        decision = InterruptionPolicy().evaluate(
            evidence(InterruptionSource.CLOUD_ASR, duration_ms=400, speaker_class=speaker_class),
            asr_text="好的，再见",
            device_conversation=True,
        )

        assert decision.verdict is InterruptionVerdict.TRUE_INTERRUPT
        assert decision.reason == "conversation_end_explicit"
        assert decision.cancel_generation


def test_physical_hard_stop_is_never_blocked_by_speaker_class() -> None:
    """The physical mute keeps working even for a classified non-owner."""

    decision = InterruptionPolicy().evaluate(
        evidence(InterruptionSource.BUTTON, speaker_class="guest")
    )

    assert decision.verdict is InterruptionVerdict.HARD_STOP
    assert decision.cancel_generation


def test_known_non_owner_cannot_carry_owner_authority() -> None:
    """The evidence model refuses authority that contradicts its own class."""

    with pytest.raises(ValueError, match="owner authority"):
        evidence(
            InterruptionSource.CLOUD_ASR,
            speaker_class="guest",
            owner_authority_verified=True,
        )


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
    assert projected.owner_authority_verified is False
    assert projected.speaker_reason_code == "authority_unavailable"


def test_segment_projection_accepts_runtime_speaker_authority() -> None:
    segment = SpeechSegment(
        session_id="session-1",
        stream_epoch=2,
        provider_task_epoch=1,
        segment_id="asr-1",
        revision=1,
        kind=SegmentKind.ASR_FINAL,
        capture_start_sample=1_600,
        capture_end_sample=8_000,
        text="天气怎么样",
    )

    projected = evidence_from_speech_segment(
        segment,
        active_generation_id=3,
        speaker_class="owner",
        owner_authority_verified=True,
        speaker_reason_code="owner_match",
    )

    assert projected.speaker_class == "owner"
    assert projected.owner_authority_verified is True
    assert projected.speaker_reason_code == "owner_match"
