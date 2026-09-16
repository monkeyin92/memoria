"""Shared progress and hard-deadline accounting for TTS generation attempts."""

from __future__ import annotations


class GenerationBudget:
    """One generation attempt's progress and hard-deadline state.

    Both the low-latency stream path and the batch path must answer the same
    three questions the same way:

    - before the first audio: is the first-packet budget spent?
    - after the first audio: has the provider stalled for ``total_timeout_s``?
    - has the absolute cap been reached even though audio keeps arriving?

    ``renew`` is called for every received message once audio has started, so
    an actively generating utterance is stopped by progress alone; the hard cap
    exists only to bound runaway generation. ``failure_reason`` keeps the
    classification identical on both paths so a caller cannot see two different
    meanings for the same timeout.
    """

    __slots__ = (
        "first_audio_deadline",
        "hard_deadline",
        "got_audio",
        "stall_budget_s",
        "total_deadline",
    )

    def __init__(
        self,
        *,
        started_at: float,
        first_audio_timeout_s: float,
        total_timeout_s: float,
        hard_deadline_s: float,
    ) -> None:
        self.first_audio_deadline = started_at + first_audio_timeout_s
        self.total_deadline = started_at + total_timeout_s
        self.hard_deadline = started_at + max(total_timeout_s * 5, hard_deadline_s)
        self.stall_budget_s = total_timeout_s
        self.got_audio = False

    def timeout_s(self, now: float) -> float:
        """Seconds left before this attempt must fail."""

        deadline = (
            min(self.total_deadline, self.hard_deadline)
            if self.got_audio
            else min(self.total_deadline, self.first_audio_deadline)
        )
        return max(0.0, deadline - now)

    def renew(self, now: float) -> None:
        """Record progress: the stream is alive until it stalls again."""

        self.got_audio = True
        self.total_deadline = now + self.stall_budget_s

    def failure_reason(self) -> str:
        return "total-timeout" if self.got_audio else "first-audio-timeout"


class BeforeAudioError:
    """Marker: the attempt failed before any audio existed for the utterance.

    The batch helpers (``synthesize_stream_text``) retry by re-synthesizing the
    **whole** utterance on a fresh connection, so the failed attempt's buffer is
    discarded: partial audio is never returned as if it were the sentence.

    Only this kind of failure may also change the voice (the personal/clone
    fallback to the designed voice), because no audio with the requested voice
    reached the caller yet.
    """

    audio_produced = False
    may_change_voice = True


class AlignmentRetryError:
    """Marker: audio exists but the word timestamps for it are missing.

    A retry may re-synthesize once to recover the alignment metadata, but it must
    keep the SAME voice: audio for this utterance already exists, and switching
    speaker on the retry would deliver another voice for a sentence the caller
    asked to hear in the requested one.
    """

    audio_produced = True
    may_change_voice = False


def retry_allowed(exc: BaseException) -> bool:
    """Whether one fresh attempt may re-synthesize this failure's utterance.

    Fail closed: an error that is not explicitly marked retryable ends the call
    instead of replaying the sentence.
    """

    return isinstance(exc, (BeforeAudioError, AlignmentRetryError))


def retry_may_change_voice(exc: BaseException) -> bool:
    """Whether the retry may fall back to another voice. Only before audio."""

    return bool(getattr(exc, "may_change_voice", False))
