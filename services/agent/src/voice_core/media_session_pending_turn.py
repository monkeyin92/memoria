"""Mutable state of the one candidate user turn a Media Voice session holds."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.speech_timeline import ASRResult


@dataclass(slots=True)
class PendingTurn:
    """The pending turn's range, endpoint timers, pins and forced texts.

    ``reset`` clears the turn when it commits, is discarded or the stream
    rotates. The reopen-evidence and evidence-less-hold fields belong to the
    same turn but are cleared by their own owners, so ``reset`` leaves them.
    """

    admitted_input_stream_epoch: int | None = None
    # Only accepted VAD owns this latch; ASR admission/turn ranges do not prove
    # that speech is still in progress.
    active_vad_stream_epoch: int | None = None
    active_vad_start_sample: int | None = None
    # Independent wall-clock bound for one accepted user utterance.  This is
    # deliberately separate from owner-silence timing: a stuck VAD stream
    # must eventually fail closed even while the owner is still speaking.
    max_user_speech_task: asyncio.Task[None] | None = None
    max_user_speech_deadline: float | None = None
    committed_asr_keys: OrderedDict[tuple[int, str, int, int], None] = field(
        default_factory=OrderedDict
    )
    turn_start_sample: int | None = None
    turn_input_fence: GenerationFence | None = None
    turn_end_sample: int | None = None
    turn_endpoint_sample: int | None = None
    turn_retire_sample: int | None = None
    turn_endpoint_grace_deadline: float | None = None
    turn_endpoint_tail_deadline: float | None = None
    turn_endpoint_timeout_handle: asyncio.TimerHandle | None = None
    # A turn that already has text, reopened by a later vad.start: the
    # text-covered endpoint to fall back to, the transcript end it had, and
    # the bounded window for the new speech to produce any text of its own.
    reopen_evidence_endpoint: int | None = None
    reopen_evidence_turn_end: int | None = None
    reopen_evidence_turn_start: int | None = None
    reopen_evidence_handle: asyncio.TimerHandle | None = None
    # When admitted output first waited behind the floor; bounds how long a
    # pending turn with no text evidence (room-noise VAD) may keep holding it.
    evidence_less_hold_since: float | None = None
    evidence_less_hold_handle: asyncio.TimerHandle | None = None
    # Observed while a reply owned output; not proof of acoustic echo.
    pending_turn_playback_overlap: bool = False
    # Closed candidate input may not re-enter through ASR, rescue, or VAD.
    pending_turn_onset_floor: int | None = None
    # Endpoint pinned by the playback-followup path; a later guarded final may
    # advance it while the utterance keeps producing post-boundary finals.
    playback_followup_endpoint_sample: int | None = None
    # The sentence the commit's head start was last started for: a reschedule of the same commit must
    # not start it again (``_warm_endpoint_commit``).
    warmed_commit_text: str | None = None
    turn_commit_retry_task: asyncio.Task[None] | None = None
    turn_commit_retry_attempt: int = 0
    turn_commit_retry_stream_epoch: int | None = None
    turn_commit_retry_endpoint_sample: int | None = None
    pending_partial: ASRResult | None = None
    clock_fact_partial_text: str | None = None
    clock_fact_partial_stable_since: float | None = None
    # When set, a clock/date final already chose the turn endpoint; later VAD
    # tails must not extend the range or cancel the pending commit task.
    clock_fact_endpoint_pinned: int | None = None
    conversation_close_partial_text: str | None = None
    conversation_close_partial_stable_since: float | None = None
    conversation_close_endpoint_pinned: int | None = None
    conversation_close_semantic_text: str | None = None
    conversation_close_semantic_task: asyncio.Task[None] | None = None
    clock_fact_forced_text: str | None = None
    live_query_forced_text: str | None = None
    live_query_partial_text: str | None = None
    live_query_partial_stable_since: float | None = None
    live_query_endpoint_pinned: int | None = None
    # Set when the forced live-query text was recovered from a
    # CROSS_SENTENCE_OVERLAP rejection: in-range timeline text then belongs
    # to the blocking interval and the forced text must win unconditionally.
    live_query_forced_authoritative: bool = False

    def restart_endpoint_bounds(self, grace_s: float) -> None:
        """Start a moved endpoint's grace and absolute tail bound over.

        A playback follow-up extends its pinned endpoint while the user keeps
        talking. Kept from the first pin, the absolute bound expired
        mid-sentence and put the device on standby with the question
        unanswered (turn_prepare_timeout, 2026-09-28).
        """

        self.turn_endpoint_grace_deadline = time.monotonic() + grace_s
        if self.turn_endpoint_timeout_handle is not None:
            self.turn_endpoint_timeout_handle.cancel()
        self.turn_endpoint_timeout_handle = None
        self.turn_endpoint_tail_deadline = None

    def clear_commit_retry(self) -> None:
        """Drop the retry state; never cancel the retry task that is running."""

        task = self.turn_commit_retry_task
        current_task = asyncio.current_task()
        if task is not None and task is not current_task and not task.done():
            task.cancel()
        self.turn_commit_retry_task = None
        self.turn_commit_retry_attempt = 0
        self.turn_commit_retry_stream_epoch = None
        self.turn_commit_retry_endpoint_sample = None

    def cancel_close_semantic(self) -> None:
        task = self.conversation_close_semantic_task
        if task is not None and not task.done():
            task.cancel()
        self.conversation_close_semantic_task = None
        self.conversation_close_semantic_text = None

    def reset(self) -> None:
        """Clear the turn in place, cancelling its timers and owned tasks.

        The running task (a max-speech or retry task that is itself resetting
        the turn) is never cancelled; the close-semantic task always is.
        """

        self.admitted_input_stream_epoch = None
        self.active_vad_stream_epoch = None
        self.active_vad_start_sample = None
        max_speech_task = self.max_user_speech_task
        self.max_user_speech_task = None
        self.max_user_speech_deadline = None
        if (
            max_speech_task is not None
            and max_speech_task is not asyncio.current_task()
            and not max_speech_task.done()
        ):
            max_speech_task.cancel()
        if self.turn_endpoint_timeout_handle is not None:
            self.turn_endpoint_timeout_handle.cancel()
            self.turn_endpoint_timeout_handle = None
        self.clear_commit_retry()
        self.turn_start_sample = None
        self.turn_input_fence = None
        self.turn_end_sample = None
        self.turn_endpoint_sample = None
        self.turn_retire_sample = None
        self.turn_endpoint_grace_deadline = None
        self.turn_endpoint_tail_deadline = None
        self.pending_turn_playback_overlap = False
        self.pending_turn_onset_floor = None
        self.playback_followup_endpoint_sample = None
        self.warmed_commit_text = None
        self.committed_asr_keys.clear()
        self.pending_partial = None
        self.clock_fact_partial_text = None
        self.clock_fact_partial_stable_since = None
        self.clock_fact_endpoint_pinned = None
        self.conversation_close_partial_text = None
        self.conversation_close_partial_stable_since = None
        self.conversation_close_endpoint_pinned = None
        self.cancel_close_semantic()
        self.clock_fact_forced_text = None
        self.live_query_forced_text = None
        self.live_query_partial_text = None
        self.live_query_partial_stable_since = None
        self.live_query_endpoint_pinned = None
        self.live_query_forced_authoritative = False
