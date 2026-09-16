"""The shared retry rule for one TTS synthesis attempt.

The batch helpers (``synthesize_stream_text`` on both providers) retry by
re-synthesizing the whole utterance on a fresh connection.  These cases pin the
rule that decides whether that is allowed:

- a pre-audio failure may retry and may fall back to another voice;
- a missing-timestamp failure may retry, but never with another voice, because
  audio for the sentence already exists;
- anything else fails closed, so a failure after audio can never replay the
  sentence as a whole.
"""

from __future__ import annotations

from livekit.agents import APIConnectionError
from services.agent.src.providers.generation_budget import (
    AlignmentRetryError,
    BeforeAudioError,
    retry_allowed,
    retry_may_change_voice,
)


class _ProviderPreAudioError(BeforeAudioError, RuntimeError):
    pass


class _ProviderFirstAudioTimeout(BeforeAudioError, TimeoutError):
    pass


class _ProviderAlignmentError(AlignmentRetryError, RuntimeError):
    pass


def test_pre_audio_failure_may_retry_and_may_change_voice() -> None:
    exc = _ProviderPreAudioError("session start failed")

    assert exc.audio_produced is False
    assert retry_allowed(exc) is True
    assert retry_may_change_voice(exc) is True


def test_alignment_failure_may_retry_but_never_changes_voice() -> None:
    exc = _ProviderAlignmentError("no word timestamps")

    assert exc.audio_produced is True
    assert retry_allowed(exc) is True
    # The same speaker must be requested again: audio for the sentence exists.
    assert retry_may_change_voice(exc) is False


def test_first_audio_timeout_stays_a_timeout_for_existing_callers() -> None:
    exc = _ProviderFirstAudioTimeout()

    assert isinstance(exc, TimeoutError)
    assert retry_allowed(exc) is True
    assert retry_may_change_voice(exc) is True


def test_unknown_failure_is_not_retryable() -> None:
    assert retry_allowed(RuntimeError("provider error after audio")) is False
    assert retry_allowed(APIConnectionError("total-timeout")) is False
    assert retry_allowed(ValueError("pcm continuity")) is False
    assert retry_may_change_voice(RuntimeError("provider error")) is False
