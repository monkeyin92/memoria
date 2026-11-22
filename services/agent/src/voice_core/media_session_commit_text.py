"""Text and timing helpers for the committed media turn.

Pure helpers used by ``MediaSessionCommitMixin`` before and during a commit:
stage timing, preferred clock/live-query text, media turn text resolution and
empty-turn logging.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from services.agent.src.clock_fact_queries import is_clock_fact_query
from services.agent.src.voice_core.playback_text import resolve_playback_stop_text

if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

_COMMIT_LOGGER = logging.getLogger("services.agent.src.voice_core.media_session_commit")


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _mark_stage(timing: dict[str, int] | None, stage: str, started: float) -> None:
    """Record how long one awaited step of the commit took, for the commit timing log line."""

    if timing is not None:
        timing[stage] = _elapsed_ms(started)


def _preferred_clock_fact_text(
    context: _MediaVoiceSession,
    *,
    stream_epoch: int,
    start_sample: int,
    end_sample: int,
) -> str | None:
    forced = context.pending.clock_fact_forced_text
    if forced and is_clock_fact_query(forced):
        return forced
    candidates = [
        segment.text.strip()
        for segment in context.runtime.speech_timeline.segments_in_range(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
        )
        if segment.text.strip() and is_clock_fact_query(segment.text)
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


def _preferred_live_query_text(context: _MediaVoiceSession) -> str | None:
    forced = context.pending.live_query_forced_text
    if forced and context.runtime.live_lookup_needed(forced):
        return forced
    return None


def _resolve_media_turn_text(
    context: _MediaVoiceSession,
    *,
    stream_epoch: int,
    start_sample: int,
    end_sample: int,
) -> str | None:
    """Resolve authoritative media commit text for one sample range."""

    text = context.runtime.project_media_user_turn(
        stream_epoch=stream_epoch,
        start_sample=start_sample,
        end_sample=end_sample,
    )
    latest_final = context.pending.last_accepted_final
    if (
        text
        and latest_final is not None
        and latest_final.stream_epoch == stream_epoch
        and latest_final.capture_start_sample >= start_sample
        and latest_final.capture_end_sample <= end_sample
    ):
        text = context.runtime.speech_timeline.projected_text(
            stream_epoch=stream_epoch,
            start_sample=start_sample,
            end_sample=end_sample,
            exclude_cross_boundary=True,
        )
    preferred_clock = _preferred_clock_fact_text(
        context,
        stream_epoch=stream_epoch,
        start_sample=start_sample,
        end_sample=end_sample,
    )
    preferred_live = _preferred_live_query_text(context)
    if preferred_live:
        # An authoritative forced text means the in-range timeline text comes
        # from a blocking interval (e.g. playback echo that overlapped the
        # user final), so length comparison against it is meaningless.
        if context.pending.live_query_forced_authoritative:
            return preferred_live
        if not text or len(preferred_live.strip()) > len(text.strip()):
            return preferred_live
    if (
        text
        and context.identity.client_type == "device"
        and context.pending.playback_stop_text
    ):
        # Re-validate against the current projected text so a newer ASR revision
        # on the same range (e.g. 「停一下」 -> 「停一下我想问明天的天气」) is
        # never overwritten by the older pinned stop string, while punctuation-only
        # revisions (e.g. 「……安静」 -> 「……安静。」) still commit as a stop.
        resolved_stop = resolve_playback_stop_text(
            text, playback_text=str(getattr(context.output, "assistant_text", ""))
        )
        if resolved_stop is not None:
            context.pending.playback_stop_from_echo_strip = resolved_stop != text.strip()
            return resolved_stop
        context.pending.playback_stop_from_echo_strip = False
    if not preferred_clock:
        return text
    if not text or not is_clock_fact_query(text):
        return preferred_clock
    if text.strip() != preferred_clock:
        return preferred_clock
    return text


def _resolve_commit_playback_guard(
    context: _MediaVoiceSession,
    text: str,
    *,
    elapsed_ms: int,
    was_assistant_speaking: bool,
    stop_is_echo: Callable[[_MediaVoiceSession, str], bool],
) -> str | None:
    """Evaluate playback echo guards while honoring validated echo-stripped stops."""

    guarded_reason = context.runtime.playback_guarded_reason(text, duration_ms=elapsed_ms)
    if not was_assistant_speaking:
        return guarded_reason
    if guarded_reason == "assistant_echo" and getattr(
        context.pending, "playback_stop_from_echo_strip", False
    ):
        return None
    if guarded_reason is None and stop_is_echo(context, text):
        return "assistant_echo"
    return guarded_reason


def _log_empty_media_turn(
    context: _MediaVoiceSession,
    stream_epoch: int,
    start_sample: int,
    end_sample: int,
    stage: str,
) -> None:
    """Say why a turn commits no text: its end is otherwise silent.

    ``_commit_pending_turn`` reports every other reason as "did not start
    reply"; an empty turn only surfaced later as the tail-timeout discard, so
    an accepted final that had vanished from the timeline left no trace.
    """

    text_segments = [
        segment
        for segment in context.runtime.speech_timeline.pending
        if segment.stream_epoch == stream_epoch and segment.text.strip()
    ]
    _COMMIT_LOGGER.info(
        "media turn has no text session=%s stream_epoch=%s samples=%s-%s stage=%s "
        "pending_text_segments=%s pending_text_ranges=%s text_after_endpoint=%s "
        "pending_end=%s active_vad_start=%s last_playback_end=%s committed_sample=%s",
        context.identity.session_id,
        stream_epoch,
        start_sample,
        end_sample,
        stage,
        len(text_segments),
        [
            (segment.capture_start_sample, segment.capture_end_sample)
            for segment in text_segments[:4]
        ],
        sum(segment.capture_start_sample >= end_sample for segment in text_segments),
        context.pending.turn_end_sample,
        context.pending.active_vad_start_sample,
        context.last_playback_end_sample,
        context.asr.last_committed_sample,
    )
