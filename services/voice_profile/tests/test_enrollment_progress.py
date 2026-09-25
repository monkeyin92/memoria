"""Durable enrollment progress: the stages a client can actually render."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.voice_profile.domain import VoiceProfile
from services.voice_profile.enrollment_progress import (
    PROMISED_TOTAL_MS,
    enrollment_progress,
)
from services.voice_profile.sample_copy import sample_rejection_message

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _profile(
    *,
    status: str,
    provider_voice_id: str | None = None,
    created_seconds_ago: float = 0.0,
    **overrides: object,
) -> VoiceProfile:
    fields: dict[str, object] = {
        "profile_id": "profile-1",
        "sample_id": "sample-1",
        "version_number": 1,
        "provider": "alibaba_model_studio",
        "provider_region": "cn-beijing",
        "target_model": "cosyvoice-v3.5-flash",
        "provider_voice_id": provider_voice_id,
        "status": status,
        "evaluation_status": "pending",
        "quality_status": "pending",
        "deletion_status": "not_requested",
        "provider_expires_at": None,
        "created_at": _NOW - timedelta(seconds=created_seconds_ago),
    }
    fields.update(overrides)
    return VoiceProfile(**fields)  # type: ignore[arg-type]


def test_every_stage_is_derived_from_durable_status() -> None:
    queued = enrollment_progress(_profile(status="enrolling"), now=_NOW)
    assert (queued.stage, queued.stage_label) == ("queued", "正在准备你的声音")
    assert queued.stage_count == 3
    assert queued.stage_index == 1
    assert queued.progress == 0.0

    cloning = enrollment_progress(
        _profile(status="enrolling", provider_voice_id="voice-1"), now=_NOW
    )
    assert cloning.stage == "cloning"
    assert cloning.stage_index == 2
    assert cloning.progress == 0.15

    activating = enrollment_progress(
        _profile(status="candidate", provider_voice_id="voice-1"), now=_NOW
    )
    assert activating.stage == "activating"
    assert activating.stage_index == 3
    assert activating.progress == 0.9

    done = enrollment_progress(
        _profile(status="active", provider_voice_id="voice-1"), now=_NOW
    )
    assert done.stage == "done"
    assert done.stage_label == "自定义声音已就绪"
    assert done.progress is None
    assert done.retryable is False

    failed = enrollment_progress(_profile(status="failed"), now=_NOW)
    assert failed.stage == "failed"
    assert failed.progress is None
    assert failed.retryable is True
    assert failed.error_code == "failed"

    revoked = enrollment_progress(_profile(status="revoked"), now=_NOW)
    assert revoked.stage == "failed"
    # A revoked profile is not something the user retries by pressing again.
    assert revoked.retryable is False


def test_progress_never_invents_a_percentage_for_terminal_stages() -> None:
    for status in ("active", "failed", "revoked"):
        assert enrollment_progress(_profile(status=status), now=_NOW).progress is None


def test_the_minute_promise_is_dropped_once_it_is_exceeded() -> None:
    inside = enrollment_progress(
        _profile(status="enrolling", created_seconds_ago=30), now=_NOW
    )
    assert inside.elapsed_ms == 30_000
    assert inside.expected_total_ms == PROMISED_TOTAL_MS
    assert inside.over_budget is False

    past = enrollment_progress(
        _profile(status="enrolling", created_seconds_ago=61), now=_NOW
    )
    assert past.over_budget is True
    # Stage progress is unaffected: the clone really is still running.
    assert past.progress == 0.0

    # A finished profile can never be "over budget" -- there is nothing left
    # to wait for, so the copy must stop implying that there is.
    finished = enrollment_progress(
        _profile(status="active", provider_voice_id="voice-1", created_seconds_ago=600),
        now=_NOW,
    )
    assert finished.over_budget is False


def test_progress_survives_a_reload_because_it_is_derived_not_tracked() -> None:
    """The same row must produce the same progress in a later process."""
    profile = _profile(status="enrolling", provider_voice_id="voice-1")
    first = enrollment_progress(profile, now=_NOW)
    later = enrollment_progress(
        profile, now=_NOW + timedelta(seconds=10)
    )
    assert first.stage == later.stage
    assert first.progress == later.progress
    assert later.elapsed_ms == first.elapsed_ms + 10_000


def test_every_rejection_reason_has_actionable_copy() -> None:
    reasons = (
        "audio_decode_failed",
        "audio_too_short",
        "audio_too_long",
        "audio_low_sample_rate",
        "audio_silent",
        "speech_too_short",
        "audio_clipped",
    )
    for reason in reasons:
        message = sample_rejection_message((reason,))
        assert message.endswith("。"), reason
        assert len(message) >= 8, reason
        assert "audio_" not in message, reason
    assert "安静" in sample_rejection_message(("audio_silent",))
    assert "短" in sample_rejection_message(("audio_too_short", "audio_silent"))
    # An unknown reason still produces something a user can act on.
    assert sample_rejection_message(()) != ""
