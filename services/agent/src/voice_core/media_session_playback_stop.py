"""Spoken playback stop (「停」「等等」「好了」) while a Media Voice reply plays.

Field 2026-09-29 (story on a real ESP32, ``audio_mode=interrupt_assist``): the
signed ``allowed_barge_in`` excludes voice, so firmware and Edge suppress every
playback-window vad.start and the pending turn never gets a VAD endpoint.  The
cloud-ASR 「停」 finals therefore buffered forever, and a committed stop command
only flipped the runtime floor to listening while the reply kept streaming.
This mixin pins the endpoint of a lexical stop heard during playback and then
carries out the Router's INTERRUPT_COMMAND the way a keyword stop does.

It also owns which other finals around the playback window may endpoint a
turn: while the reply plays, any other early endpoint blocks this stop and
swallows later finals, so only a farewell that is not the reply's own echo may
pin one, and never a rescue; after playback, a follow-up that began inside the
echo window may, but a farewell whose audio ends before the boundary never does.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.conversation_close_router import lexical_playback_control_only
from services.agent.src.voice_core.media_protocol import should_pause_asr_for_playback
from services.agent.src.voice_core.speech_timeline import ASRResult

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

logger = logging.getLogger(__name__)

# UtteranceRouter rule 4 (pure stop / completion acknowledgement) reports this
# reason when ``accept_user_turn`` suppresses the control turn.
PLAYBACK_STOP_ROUTE_REASON = "interrupt_command_only"
_PLAYBACK_STOP_CAUSE = "media_voice_stop"
# The assistant text announced most recently is what the uplink can still be
# echoing: the phrase being played plus the one queued behind it (phrases are
# at most ~28 characters) and the ASR finalization lag.  A stop phrase found
# there is treated as the reply's own voice, never as the owner's command.
_PLAYBACK_STOP_ECHO_TAIL_CHARS = 64
# A follow-up final that begins before the playback boundary (itself 0.8 s past
# the reply's last uplink evidence) must run at least this far past it: the
# reply's echo cannot, only speech that went on after the speaker stopped.
_PLAYBACK_FOLLOWUP_STRADDLE_SAMPLES = 16_000  # 1.0 s at 16 kHz
# A follow-up final that starts wholly after the playback boundary is user
# speech, not echo: endpoint it with a short grace instead of waiting for a
# VAD edge that a stuck post-playback VAD may never emit (run 20260921).
_PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S = 1.2
# Latency experiment (TODOLIST N-14 tier 3): the grace may only be shortened, never lengthened, so
# the turn budget that is sized for the default (test_the_default_turn_budget_covers...) still holds.
PLAYBACK_FOLLOWUP_GRACE_ENV = "MEDIA_PLAYBACK_FOLLOWUP_GRACE_S"
_PLAYBACK_FOLLOWUP_MIN_GRACE_S = 0.3
# Conservative sample-gap policy for unanchored device candidates observed
# during a previous reply, not a VAD silence measurement or endpoint timeout.
_PLAYBACK_CANDIDATE_SPLIT_GAP_SAMPLES = 40_000  # 2.5 s at 16 kHz
# A device VAD that began this far past the playback boundary (which already carries 0.8 s over the
# last uplink evidence) is new speech; one closer to it is still the echo's own tail (2026-10-03 round 11:
# a VAD edge 0.08 s past the boundary was the echo, 3 s later the child's question followed).
_PLAYBACK_NEW_SPEECH_VAD_SAMPLES = 12_800  # 0.8 s at 16 kHz


def playback_followup_grace_s() -> float:
    """The grace a post-playback follow-up final waits for the next one before it commits.

    1.2 s unless MEDIA_PLAYBACK_FOLLOWUP_GRACE_S asks for a shorter one (0.3-1.2 s).  Anything else
    (unreadable, longer, shorter than 0.3 s) keeps the default: a typo must not silently shorten the
    wait for a child's next word.
    """

    raw = os.environ.get(PLAYBACK_FOLLOWUP_GRACE_ENV, "").strip()
    if not raw:
        return _PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if _PLAYBACK_FOLLOWUP_MIN_GRACE_S <= value <= _PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S:
        return value
    logger.warning("%s=%r is not within 0.3-1.2 s; keeping the default", PLAYBACK_FOLLOWUP_GRACE_ENV, raw)
    return _PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S


def _compact(text: str) -> str:
    return "".join(character for character in text if character.isalnum())


_SAMPLES_PER_MS = 16  # the media clock runs at 16 kHz


def _ms_after(sample: int | None, origin: int) -> str:
    """``sample - origin`` in ms for a diagnostics line, ``-`` when unset."""

    return "-" if sample is None else str((sample - origin) // _SAMPLES_PER_MS)


class MediaPlaybackStopMixin:
    """Endpoint and execute a spoken stop command during assistant playback.

    Also decides which other finals around the playback window may endpoint
    a device turn early.
    """

    if TYPE_CHECKING:
        metrics: MetricsRegistry

        @staticmethod
        def _reply_in_flight(context: _MediaVoiceSession) -> bool: ...

        def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None: ...

        async def _record_interrupted_timed_spans(
            self, context: _MediaVoiceSession, fence: GenerationFence
        ) -> None: ...

        async def _cancel_reply_task(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
            *,
            reason: str = "cancelled",
            cancel_timeout_s: float = 5.0,
        ) -> None: ...

        @staticmethod
        def _device_playback_flush_required(
            context: _MediaVoiceSession, fence: GenerationFence
        ) -> bool: ...

        async def _emit_cancel_generation(
            self,
            context: _MediaVoiceSession,
            cancelled: GenerationFence,
            *,
            heard_fence: GenerationFence,
            source_event_id: str,
            payload: dict[str, Any],
            playback_flush_required: bool | None = None,
        ) -> bool: ...

        async def _end_replacement_generation(
            self,
            context: _MediaVoiceSession,
            replacement: GenerationFence,
            *,
            reason: str,
        ) -> bool: ...

        @staticmethod
        def _record_playback_boundary(context: _MediaVoiceSession) -> None: ...

    @staticmethod
    def _playback_text_is_echo(context: _MediaVoiceSession, text: str) -> bool:
        """True when ``text`` repeats what the reply announced most recently."""

        heard = _compact(text)
        spoken = _compact(context.output.assistant_text)[-_PLAYBACK_STOP_ECHO_TAIL_CHARS:]
        return bool(heard) and heard in spoken

    @staticmethod
    def _playback_stop_is_echo(context: _MediaVoiceSession, text: str) -> bool:
        """True when a stop phrase repeats what the reply itself just said.

        A single character such as 「停」 is below the generic assistant-echo
        guard, so a story line like 「小兔子大喊：停！」 would otherwise stop
        its own reply through an open microphone.
        """

        return lexical_playback_control_only(text) and (
            MediaPlaybackStopMixin._playback_text_is_echo(context, text)
        )

    def _playback_stop_diagnosis(self, context: _MediaVoiceSession, result: ASRResult) -> str:
        """Why a final could or could not be a spoken stop, without its text.

        2026-10-01 (voice soak on DeepSeek): the owner's 「别说了」 reached the ASR
        26–31 dB above the reply's echo and a 3–4 character final followed, yet
        no stop ran and nothing was logged; every guard returned silently.
        """

        try:
            text = result.text.strip()
            pending = context.pending
            start = result.capture_start_sample
            return (
                f"stop_word={lexical_playback_control_only(text)} "
                f"echo={self._playback_text_is_echo(context, text)} "
                f"in_flight={self._reply_in_flight(context)} "
                f"speaking={context.runtime.assistant_speaking} "
                f"owner={context.output.output_owner is not None} "
                f"endpoint_ms={_ms_after(pending.turn_endpoint_sample, start)} "
                f"followup_pin={pending.playback_followup_endpoint_sample is not None} "
                f"onset_floor_ms={_ms_after(pending.pending_turn_onset_floor, start)} "
                f"turn_start_ms={_ms_after(pending.turn_start_sample, start)} "
                f"reply_text_len={len(context.output.assistant_text)} "
                f"text_len={len(text)} samples={start}-{result.capture_end_sample}"
            )
        except Exception:  # diagnostics sit on the audio path and must never break it
            return "diagnosis=unavailable"

    @staticmethod
    def _log_unpinned_final(
        context: _MediaVoiceSession, *, reason: str, diagnosis: str, source: str
    ) -> None:
        logger.info(
            "media playback-stop not taken session=%s reason=%s source=%s %s",
            context.identity.session_id,
            reason,
            source,
            diagnosis,
        )

    def _playback_holds_early_endpoint(
        self,
        context: _MediaVoiceSession,
        text: str,
        *,
        kind: str,
        echo_only: bool = False,
    ) -> bool:
        """True when an audible device reply keeps this candidate unendpointed.

        Simulated trials 2026-09-29: a misheard 「停」 (「行」) went to the
        semantic close classifier during a story and pinned an endpoint.  With
        playback VAD suppressed nothing else moves a pinned endpoint, and
        while it stood ``_maybe_pin_playback_stop`` refused the owner's clear
        「停」; the commit then held the turn anyway (a semantic-only verdict
        never routes END_SESSION past the playback policy, and a device-bound
        owner is no voice authority) and cleared the stop with it.  A lexical
        farewell still ends the session, unless it is the reply's own echo.
        """

        if context.identity.client_type != "device" or not context.runtime.assistant_speaking:
            return False
        echo = self._playback_text_is_echo(context, text)
        if echo_only and not echo:
            return False
        logger.info(
            "media early %s endpoint held during playback session=%s text_len=%s echo=%s",
            kind,
            context.identity.session_id,
            len(text.strip()),
            echo,
        )
        return True

    @staticmethod
    def _playback_window_holds_close(
        context: _MediaVoiceSession,
        capture_end_sample: int,
        *,
        text: str,
        source: str,
        result: ASRResult | None,
    ) -> bool:
        """True when a device farewell candidate is audio of the reply's window.

        Field 2026-09-29 session addf5e00: SenseVoice rescued the wake
        greeting's echo (samples 17280-81280) as a farewell.  Its overlap
        recovery resolved just after the playback ack, when the audible hold
        no longer applied, and the session closed with the greeting.  Audio
        that ends before the last playback boundary is the reply's time on
        the uplink, whatever its text reads.  A rescue result spans its whole
        provider task, echo included, so one heard while the reply plays is no
        owner farewell either (``_playback_followup_straddles`` refuses it the
        same way); one that runs past the boundary keeps its trailing farewell.
        """

        boundary = context.last_playback_end_sample
        rescue = result is not None and result.rescue_synthesized
        if context.identity.client_type != "device" or not (
            (boundary is not None and capture_end_sample <= boundary)
            or (rescue and context.runtime.assistant_speaking)
        ):
            return False
        start = None if result is None else result.capture_start_sample
        logger.info(
            "media early conversation-close %s endpoint held from playback window "
            "session=%s boundary=%s start=%s end=%s rescue=%s text_len=%s",
            source,
            context.identity.session_id,
            boundary,
            start,
            capture_end_sample,
            rescue,
            len(text.strip()),
        )
        return True

    def _playback_followup_straddles(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
    ) -> bool:
        """True for owner speech that began just before the playback boundary.

        Field 2026-09-29 session 6b38ba46: the owner asked the next question
        ~0.5 s before the reply finished.  Its final began inside the echo
        window, so the follow-up endpoint refused it, and with playback VAD
        suppressed no vad.start ever came: the question waited ~9 s for the
        owner to repeat it.  A final that runs well past the boundary and is
        not the reply's own text is speech that went on after the speaker
        stopped; it opens the follow-up turn on its own samples.  A rescue
        result never does: it spans its whole provider task, echo included.
        """

        boundary = context.last_playback_end_sample
        text = result.text.strip()
        if (
            context.identity.client_type != "device"
            or not text
            or result.rescue_synthesized
            or boundary is None
            or result.capture_start_sample >= boundary
            or result.capture_end_sample - boundary < _PLAYBACK_FOLLOWUP_STRADDLE_SAMPLES
            or self._reply_in_flight(context)
        ):
            return False
        duration_ms = (result.capture_end_sample - result.capture_start_sample) // 16
        return not (
            self._playback_text_is_echo(context, text)
            or context.runtime.playback_guarded_reason(text, duration_ms=duration_ms)
            == "assistant_echo"
        )

    def _maybe_endpoint_playback_followup(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
    ) -> None:
        """Endpoint a post-playback follow-up without waiting for a VAD edge.

        Run 20260921 window-a: both follow-ups were recognized in realtime,
        but the device VAD stayed active across the playback echo, the pending
        turn merged echo and follow-up text, and the offline paragraphs were
        rejected for straddling the committed boundary -- so the endpoint
        waited ~20 s for the next vad.start.  When playback is over and an
        accepted final lies wholly after the playback boundary, it is user
        speech, not echo: pin the endpoint with a short grace instead of
        waiting for a VAD edge that may never arrive.  Owner speech that began
        just before the boundary and ran well past it is endpointed the same
        way (``_playback_followup_straddles``).
        """

        if context.identity.client_type != "device" or not result.text.strip():
            return  # An empty final is not speech and must not pin the endpoint.
        playback_end = context.last_playback_end_sample
        if playback_end is None or (
            result.capture_start_sample < playback_end
            and not self._playback_followup_straddles(context, result)
        ):
            # Echo guard: a final that begins before the playback boundary may
            # be the reply's tail; only non-echo speech well past it may pin.
            return
        if self._reply_in_flight(context):
            return
        if context.pending.turn_endpoint_sample is not None:
            # Only an endpoint this path pinned may be extended while the
            # utterance keeps producing post-boundary finals; a VAD end or a
            # clock-fact/live-query pin already owns the tail.
            if (
                context.pending.playback_followup_endpoint_sample
                == context.pending.turn_endpoint_sample
                and result.capture_start_sample >= context.pending.turn_endpoint_sample
            ):
                endpoint = max(result.capture_end_sample, context.pending.turn_end_sample or 0)
                context.pending.turn_endpoint_sample = endpoint
                context.pending.turn_retire_sample = max(
                    context.pending.turn_retire_sample or 0, endpoint
                )
                context.pending.playback_followup_endpoint_sample = endpoint
                context.pending.restart_endpoint_bounds(playback_followup_grace_s())
                logger.info(
                    "media playback-followup endpoint advanced session=%s "
                    "boundary=%s endpoint=%s text_len=%s",
                    context.identity.session_id,
                    playback_end,
                    endpoint,
                    len(result.text.strip()),
                )
                self._schedule_turn_commit(context)
            return
        if (
            context.pending.turn_start_sample is not None
            and context.pending.turn_start_sample < min(playback_end, result.capture_start_sample)
        ):
            # The pending window still reaches back into the echo window; the
            # playback-boundary split owns resetting it before this may fire.
            return
        endpoint = max(result.capture_end_sample, context.pending.turn_end_sample or 0)
        context.pending.turn_endpoint_sample = endpoint
        context.pending.turn_retire_sample = max(context.pending.turn_retire_sample or 0, endpoint)
        context.pending.playback_followup_endpoint_sample = endpoint
        context.pending.turn_endpoint_grace_deadline = (
            time.monotonic() + playback_followup_grace_s()
        )
        logger.info(
            "media playback-followup endpoint session=%s boundary=%s "
            "endpoint=%s text_len=%s",
            context.identity.session_id,
            playback_end,
            endpoint,
            len(result.text.strip()),
        )
        self._schedule_turn_commit(context)

    @staticmethod
    def _echo_candidate_yields_to_live_vad(
        context: _MediaVoiceSession,
        result: ASRResult,
        playback_end: int | None,
    ) -> bool:
        """True when the pending turn is the reply's own echo and live speech began after it.

        The candidate must read like an echo of the reply (the guard that would drop it at commit
        as ``assistant_echo`` agrees), so speech the child began inside the echo window keeps
        merging with what follows.  The live VAD must have started past the echo window and after
        the candidate ended: a VAD that began while the candidate's audio still ran is the same sound.
        """

        pending = context.pending
        vad_start = pending.active_vad_start_sample
        start, end = pending.turn_start_sample, pending.turn_end_sample
        if (
            playback_end is None
            or vad_start is None
            or start is None
            or end is None
            or end <= start
            or pending.active_vad_stream_epoch != result.stream_epoch
            or vad_start < playback_end + _PLAYBACK_NEW_SPEECH_VAD_SAMPLES
            or vad_start <= end
        ):
            return False
        text = context.runtime.speech_timeline.projected_text(
            stream_epoch=result.stream_epoch, start_sample=start, end_sample=end
        )
        return bool(text.strip()) and (
            context.runtime.playback_guarded_reason(
                text, duration_ms=(end - start) // _SAMPLES_PER_MS
            )
            == "assistant_echo"
        )

    def _split_pending_turn_at_unvoiced_gap(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
    ) -> None:
        """Bound an unanchored candidate after the reply ended.

        Two policies close the abandoned echo window: a completed playback
        boundary (a final that starts wholly after it, or owner speech that
        straddles it, begins a new turn even while the echo-holdover VAD is
        still marked active), and the conservative unvoiced-gap fallback.
        Neither may segment ordinary long pauses or an endpointed turn; the
        resulting sample fence also applies before ASR/rescue/VAD ingest.

        The fallback also bounds a candidate that began inside the echo window
        after the reply ended (2026-10-02 round 10 t015: a 「停」 as the story
        finished by itself).  No playback overlapped it, yet the follow-up
        endpoint leaves any pending turn begun before the boundary to this
        split: without the fallback each left the reset to the other and the
        next question waited for a VAD edge that never came.

        The reply's own voice finalized after its playback ended is such a
        candidate.  Field 2026-10-03 (round 11, 3 of 24 questions lost): the next
        question's final is accepted while its device VAD end is still being
        finalized, so the VAD latch is live and the gap guard below refused;
        the echo and the question merged and the whole turn was dropped at
        commit as ``assistant_echo``.  A live VAD that began past the echo
        window, after the candidate ended, is new speech: then an echo
        candidate gives way at once.
        """

        if context.identity.client_type != "device":
            return
        if self._reply_in_flight(context):
            if context.pending.turn_endpoint_sample is None:
                context.pending.pending_turn_playback_overlap = True
            return
        # Everything before a straddling follow-up lies inside the echo window;
        # a recovered final overlapping the pending range is no new sentence.
        straddle = self._playback_followup_straddles(context, result) and (
            context.pending.turn_end_sample or 0
        ) <= result.capture_start_sample
        overlap = context.pending.pending_turn_playback_overlap
        playback_end = context.last_playback_end_sample
        stale_echo_window = (
            playback_end is not None
            and context.pending.turn_start_sample is not None
            and context.pending.turn_start_sample < playback_end <= result.capture_start_sample
        )
        if not (overlap or straddle or stale_echo_window):
            return
        if context.pending.turn_end_sample is None:
            return
        if (
            context.pending.turn_endpoint_sample is not None
            or context.pending.live_query_forced_text
            or context.pending.clock_fact_forced_text
        ):
            return
        # Only a candidate that overlapped the playback may be cut at the
        # boundary at once; one that merely began inside the echo window must
        # also pass the unvoiced-gap fallback (no live VAD, a 2.5 s gap).
        boundary_split = straddle or (
            overlap and playback_end is not None and result.capture_start_sample >= playback_end
        )
        echo_gives_way = (
            not boundary_split
            and stale_echo_window
            and self._echo_candidate_yields_to_live_vad(context, result, playback_end)
        )
        if not (boundary_split or echo_gives_way):
            if context.pending.active_vad_start_sample is not None:
                return
            if (
                result.capture_start_sample - context.pending.turn_end_sample
                <= _PLAYBACK_CANDIDATE_SPLIT_GAP_SAMPLES
            ):
                return
        if straddle:
            boundary = "playback_straddle"
        elif boundary_split:
            boundary = "playback_end"
        elif echo_gives_way:
            boundary = "echo_candidate_live_vad"
        else:
            boundary = "unvoiced_gap" if overlap else "stale_echo_window"
        logger.warning(
            "media pending turn split after reply session=%s "
            "stream_epoch=%s boundary=%s gap_samples=%s pending=%s-%s final=%s-%s",
            context.identity.session_id,
            result.stream_epoch,
            boundary,
            result.capture_start_sample - context.pending.turn_end_sample,
            context.pending.turn_start_sample,
            context.pending.turn_end_sample,
            result.capture_start_sample,
            result.capture_end_sample,
        )
        # Drop even a cached partial crossing the boundary: its text cannot
        # safely be sliced without word timing. Do not advance commit history.
        context.runtime.speech_timeline.evict_before(
            stream_epoch=result.stream_epoch, sample=result.capture_start_sample
        )
        context.pending.turn_start_sample = None
        context.pending.turn_end_sample = None
        context.pending.pending_partial = None
        context.pending.clock_fact_partial_text = None
        context.pending.clock_fact_partial_stable_since = None
        context.pending.live_query_partial_text = None
        context.pending.live_query_partial_stable_since = None
        context.pending.conversation_close_partial_text = None
        context.pending.conversation_close_partial_stable_since = None
        context.pending.cancel_close_semantic()
        context.pending.pending_turn_playback_overlap = False
        context.pending.pending_turn_onset_floor = result.capture_start_sample

    @staticmethod
    def _scope_pending_turn_to(context: _MediaVoiceSession, result: ASRResult) -> None:
        """Make a playback command's own interval the whole pending turn.

        Earlier playback-window candidates (echo, or held speech without owner
        authority) would merge into the command, route as chat and stay held.
        The fence mirrors the playback-boundary split in
        ``MediaTurnEndpointMixin``; by interval, since a command from a later
        provider task may reuse the sentence id of a candidate it retires.
        """

        start = result.capture_start_sample
        context.runtime.speech_timeline.evict_before(
            stream_epoch=result.stream_epoch, sample=start
        )
        pending = context.pending
        pending.clock_fact_partial_text = None
        pending.clock_fact_partial_stable_since = None
        pending.live_query_partial_text = None
        pending.live_query_partial_stable_since = None
        pending.conversation_close_partial_text = None
        pending.conversation_close_partial_stable_since = None
        pending.cancel_close_semantic()
        pending.pending_turn_playback_overlap = False
        pending.pending_turn_onset_floor = start
        pending.pending_partial = None if result.is_final else result
        pending.turn_start_sample = start
        pending.turn_end_sample = result.capture_end_sample

    def _pin_conversation_close_endpoint(
        self,
        context: _MediaVoiceSession,
        capture_end_sample: int,
        *,
        text: str,
        source: str,
        result: ASRResult | None = None,
    ) -> None:
        """Pin a farewell's endpoint without waiting for a device VAD end.

        While a device reply is audible a lexical farewell commits only its own
        ``result`` interval, as a spoken stop does: after any held candidate a
        story-time 「再见」 merged into 「小猫咪去哪儿了 再见」, routed as chat
        and was held for missing owner authority, so the story played on.
        """

        if self._playback_window_holds_close(
            context, capture_end_sample, text=text, source=source, result=result
        ) or self._playback_holds_early_endpoint(
            context, text, kind=f"conversation-close {source}",
            echo_only=not source.startswith("semantic"),
        ):
            return
        if (
            result is not None
            and context.identity.client_type == "device"
            and context.runtime.assistant_speaking
        ):
            self._scope_pending_turn_to(context, result)
        endpoint = max(capture_end_sample, context.pending.turn_end_sample or 0)
        context.pending.turn_endpoint_sample = endpoint
        context.pending.turn_end_sample = max(context.pending.turn_end_sample or 0, endpoint)
        context.pending.turn_retire_sample = endpoint
        context.pending.conversation_close_endpoint_pinned = endpoint
        context.pending.turn_endpoint_grace_deadline = time.monotonic()
        logger.info(
            "media early conversation-close endpoint session=%s endpoint=%s "
            "text_len=%s source=%s",
            context.identity.session_id,
            endpoint,
            len(text),
            source,
        )

    @staticmethod
    def _pinned_turn_holds_no_words(context: _MediaVoiceSession, result: ASRResult) -> bool:
        """True when the standing endpoint ends a turn with no words, before ``result``.

        Field 2026-10-02 (soak10): the device VAD ended on room noise while the
        story played.  The ASR heard nothing in that range, so the turn could
        never commit (``empty_media_turn`` leaves it pinned) and a final that
        begins after its endpoint can never join it; yet the pin held every
        final out as ``endpoint_already_pinned`` until the ASR tail timeout
        discarded the turn 3-6 s later, a 「停」 included.  A pin that a
        recognized farewell, clock or live-query text set, or whose range
        still reaches ``result``, is a real turn and keeps the refusal.
        """

        pending = context.pending
        endpoint, start = pending.turn_endpoint_sample, pending.turn_start_sample
        if (
            endpoint is None
            or start is None
            or not 0 <= start < endpoint <= result.capture_start_sample
            or pending.clock_fact_endpoint_pinned is not None
            or pending.conversation_close_endpoint_pinned is not None
            or pending.live_query_endpoint_pinned is not None
            or pending.clock_fact_forced_text
            or pending.live_query_forced_text
        ):
            return False
        return not context.runtime.speech_timeline.projected_text(
            stream_epoch=result.stream_epoch, start_sample=start, end_sample=endpoint
        )

    def _maybe_pin_playback_stop(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
        *,
        source: str,
    ) -> None:
        """Endpoint a device stop phrase heard while a reply holds the floor.

        Only the stop phrase's own interval is committed
        (``_scope_pending_turn_to``): earlier playback-window candidates would
        otherwise merge into 「……停」, route as chat and stay held.  A stop
        phrase also replaces the endpoint of an earlier turn that holds no
        words (``_pinned_turn_holds_no_words``), which has nothing to lose.
        """

        if context.identity.client_type != "device":
            return
        text = result.text.strip()
        stop_word = bool(text) and lexical_playback_control_only(text)
        in_flight = self._reply_in_flight(context)
        diagnosis = self._playback_stop_diagnosis(context, result)
        replaced_endpoint = context.pending.turn_endpoint_sample
        # The same guards in the same order as before; each one now says why it held.
        reason = ""
        if context.closed or context.standby_requested:
            reason = "session_closing"
        elif replaced_endpoint is not None and not (
            stop_word and self._pinned_turn_holds_no_words(context, result)
        ):
            reason = "endpoint_already_pinned"
        elif not stop_word:
            reason = "not_stop_word"
        elif not in_flight:
            reason = "no_reply_in_flight"
        else:
            floor = context.pending.pending_turn_onset_floor
            if floor is not None and result.capture_start_sample < floor:
                reason = "before_onset_floor"
        if reason:
            if in_flight or stop_word:
                self._log_unpinned_final(
                    context, reason=reason, diagnosis=diagnosis, source=source
                )
            return
        if self._playback_stop_is_echo(context, text):
            logger.info(
                "media playback stop ignored as reply echo session=%s text_len=%s "
                "source=%s %s",
                context.identity.session_id,
                len(text),
                source,
                diagnosis,
            )
            return
        self._scope_pending_turn_to(context, result)
        pending = context.pending
        start = result.capture_start_sample
        endpoint = result.capture_end_sample
        pending.turn_endpoint_sample = endpoint
        pending.turn_retire_sample = endpoint
        # Also drops the replaced pin's tail bound: its timer belongs to the old endpoint.
        pending.restart_endpoint_bounds(0.0)
        logger.info(
            "media early playback-stop endpoint session=%s start=%s endpoint=%s "
            "text_len=%s source=%s replaced_empty_endpoint=%s %s",
            context.identity.session_id,
            start,
            endpoint,
            len(text),
            source,
            replaced_endpoint,
            diagnosis,
        )
        self._schedule_turn_commit(context)

    async def _stop_reply_for_voice_command(self, context: _MediaVoiceSession) -> bool:
        """Carry out a Router-accepted spoken stop like a keyword stop.

        ``accept_user_turn`` only suppresses the control turn; the reply task,
        the generation and the device playback must be stopped here.  The
        session stays open: this is never the END_SESSION path.
        """

        if (
            context.closed
            or context.standby_requested
            or not context.runtime.floor.interruptible
        ):
            return False
        previous_fence = context.output.playback.current_fence or context.runtime.fence
        if not previous_fence.matches(context.runtime.fence):
            # Another control already replaced this generation.
            return False
        stop_started_ns = time.monotonic_ns()
        flush_required = self._device_playback_flush_required(context, previous_fence)
        await self._record_interrupted_timed_spans(context, previous_fence)
        heard = context.output.playback.actual_heard_text(previous_fence)
        cancelled = await context.runtime.on_real_interrupt(
            cause=_PLAYBACK_STOP_CAUSE,
            create_user_turn=False,
            synchronized_transcript=heard,
            force_generation_bump=True,
        )
        if cancelled.matches(previous_fence):
            return False
        await context.runtime.on_media_playback_interrupted(
            interrupted_from=previous_fence,
            synchronized_transcript=heard,
        )
        context.output.playback.start(cancelled)
        context.output.provider_complete = False
        context.output.output_complete_emitted = False
        if should_pause_asr_for_playback(context.identity):
            await context.provider.pause_asr_for_playback(context.identity)
        await self._cancel_reply_task(context, previous_fence, reason=_PLAYBACK_STOP_CAUSE)
        flushed = await self._emit_cancel_generation(
            context,
            cancelled,
            heard_fence=previous_fence,
            source_event_id="voice_stop_command",
            payload={"reason": "voice_stop_command"},
            playback_flush_required=flush_required,
        )
        if flushed and flush_required and context.identity.client_type == "device":
            # No reply follows a stop: end the replacement generation the flush installed on the device.
            await self._end_replacement_generation(context, cancelled, reason="voice_stop_command")
        # A stop ends the speaker window like a finished or failed playback.
        # Without this boundary a session whose replies were all stopped had
        # none, so the post-playback follow-up endpoint skipped the owner's
        # next question and, with no device VAD edge, nothing endpointed it
        # (2026-10-02 round 10 t005: the session closed 30 s later).
        self._record_playback_boundary(context)
        self.metrics.observe_voice_latency(
            "interrupt_core_stop",
            (time.monotonic_ns() - stop_started_ns) / 1_000_000_000,
        )
        logger.info(
            "media spoken stop interrupted reply session=%s turn=%s generation=%s "
            "replacement_generation=%s flush=%s",
            context.identity.session_id,
            previous_fence.turn_id,
            previous_fence.generation_id,
            cancelled.generation_id,
            flush_required,
        )
        return True


__all__ = ["PLAYBACK_STOP_ROUTE_REASON", "MediaPlaybackStopMixin"]
