"""Spoken playback stop (「停」「等等」「好了」) while a Media Voice reply plays.

Field 2026-09-29 (story on a real ESP32, ``audio_mode=interrupt_assist``): the
signed ``allowed_barge_in`` excludes voice, so firmware and Edge suppress every
playback-window vad.start and the pending turn never gets a VAD endpoint.  The
cloud-ASR 「停」 finals therefore buffered forever, and a committed stop command
only flipped the runtime floor to listening while the reply kept streaming.
This mixin pins the endpoint of a lexical stop heard during playback and then
carries out the Router's INTERRUPT_COMMAND the way a keyword stop does.

It also endpoints a question heard over the reply (content barge-in): the commit
then stops the reply and answers the question, so a new question replaces the
story instead of waiting for it to finish (soak 2026-10-29 t017).

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

from services.agent.src.asr_text_debug_log import (
    asr_text_debug_line,
    excerpt,
    session_asr_text_debug_subjects,
    subject_tag,
)
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.conversation_close_router import (
    lexical_playback_control_only,
    rule_conversation_close_only,
)
from services.agent.src.orchestration.interruption_guard import (
    count_cjk_chars,
    is_stop_command_only,
)
from services.agent.src.voice_core.media_protocol import should_pause_asr_for_playback
from services.agent.src.voice_core.media_session_pending_turn import EndpointPinSource
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
# Field 2026-10-29 soak (t017/t018): the owner asked a whole new question and said 「嗯。」
# while a story played.  Only a lexical stop was accepted, so both played through and the
# question was dropped.  Content endpoints the reply when it is not a backchannel, not the
# reply's own echo, and either four characters long or carrying a question/command word.
CONTENT_BARGE_IN_CAUSE = "media_content_barge_in"
_CONTENT_MIN_CJK = 4
_BACKCHANNEL_FILLERS = tuple(
    sorted(
        (
            "嗯", "啊", "哦", "噢", "喔", "对", "好", "行",
            "嗯嗯", "对对", "好的", "对啊", "对呀", "好啊", "行的",
        ),
        key=len,
        reverse=True,
    )
)
_CONTENT_MARKERS = (
    "等一下",
    "等等",
    "我想问",
    "我想知道",
    "我问你",
    "问你",
    "问一下",
    "为什么",
    "怎么回事",
    "怎么",
    "什么",
    "哪儿",
    "哪里",
    "谁",
    "多少",
    "能不能",
    "可不可以",
    "不要",
    "别说了",
    "别看",
    "别动",
    "别讲",
    "别吵",
    "帮我",
    "告诉我",
)
_SPEECH_MARKUP = str.maketrans("", "", "，。！？、,.!?;；:：…~～\"'“”‘’（）() \t\r\n　")
# A request to keep talking is the opposite of a barge-in; 「不要停」 must never stop the reply.
_KEEP_PLAYING = ("不要停", "别停", "不用停", "继续", "接着讲", "接着说")


def content_barge_in_wanted(text: str) -> bool:
    """True when words heard over playback are the owner's next question.

    A backchannel alone (「嗯」「对」「好」) is agreement, never a stop.  What is left after
    the backchannel needs four characters or a question/command word to endpoint the reply.
    """

    if any(phrase in text for phrase in _KEEP_PLAYING):
        return False
    remainder = text.translate(_SPEECH_MARKUP)
    stripping = True
    while stripping and remainder:
        stripping = False
        for filler in _BACKCHANNEL_FILLERS:
            if remainder.startswith(filler):
                remainder = remainder[len(filler) :]
                stripping = True
            elif remainder.endswith(filler):
                remainder = remainder[: -len(filler)]
                stripping = True
    if not remainder:
        return False
    if count_cjk_chars(remainder) >= _CONTENT_MIN_CJK:
        return True
    return any(marker in remainder for marker in _CONTENT_MARKERS)


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
# How long an incomplete follow-up (「我想听」before 「恐龙的故事」) may keep the commit open while
# later finals arrive.  Matches the unvoiced-gap split threshold so mid-sentence pauses do not mint
# a second turn that later dies on ASR tail timeout (demo: 5 sentences → 7 input turns).
_PLAYBACK_FOLLOWUP_INCOMPLETE_HOLD_S = 2.5
# Conservative sample-gap policy for unanchored device candidates observed
# during a previous reply, not a VAD silence measurement or endpoint timeout.
_PLAYBACK_CANDIDATE_SPLIT_GAP_SAMPLES = 40_000  # 2.5 s at 16 kHz
# A device VAD that began this far past the playback boundary (which already carries 0.8 s over the
# last uplink evidence) is new speech; one closer to it is still the echo's own tail (2026-10-03 round 11:
# a VAD edge 0.08 s past the boundary was the echo, 3 s later the child's question followed).
_PLAYBACK_NEW_SPEECH_VAD_SAMPLES = 12_800  # 0.8 s at 16 kHz
_FIXED_SAFETY_COMMIT_RECHECK_S = 0.5

# Sentence-final particles / punctuation: a follow-up that ends here may commit after one grace.
_FOLLOWUP_COMPLETE_SUFFIXES = (
    "吗",
    "呢",
    "吧",
    "呀",
    "啊",
    "哦",
    "啦",
    "嘛",
    "哈",
    "嗯",
    "谢谢",
    "再见",
    "拜拜",
    "。",
    "！",
    "？",
    "!",
    "?",
)
# Clause-internal endings: FunASR often finalizes here mid-utterance; hold the commit.
_FOLLOWUP_INCOMPLETE_SUFFIXES = (
    "的",
    "了",
    "着",
    "和",
    "跟",
    "在",
    "把",
    "被",
    "是",
    "有",
    "想",
    "要",
    "会",
    "能",
    "可以",
    "因为",
    "所以",
    "但是",
    "然后",
    "如果",
    "虽然",
    "听",
)


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
    logger.warning(
        "%s=%r is not within 0.3-1.2 s; keeping the default", PLAYBACK_FOLLOWUP_GRACE_ENV, raw
    )
    return _PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S


def followup_utterance_looks_complete(text: str) -> bool:
    """True when a post-playback follow-up may commit after one grace window.

    Incomplete mid-clause finals (「我想听」) must wait for the next final or the
    incomplete-hold budget; complete short follow-ups (「后天呢」) commit as today.
    """

    compact = "".join(character for character in text.strip() if not character.isspace())
    if not compact:
        return False
    if any(compact.endswith(suffix) for suffix in _FOLLOWUP_COMPLETE_SUFFIXES):
        return True
    if any(compact.endswith(suffix) for suffix in _FOLLOWUP_INCOMPLETE_SUFFIXES):
        return False
    return len(compact) >= 6


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

        def _asr_covers_endpoint(
            self, context: _MediaVoiceSession, result_end_sample: int | None, endpoint_sample: int
        ) -> bool: ...

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
            finalize_audible: bool = True,
        ) -> None: ...

        @staticmethod
        def _device_playback_flush_required(
            context: _MediaVoiceSession, fence: GenerationFence
        ) -> bool: ...

        def _schedule_aigc_round_closing(
            self, context: _MediaVoiceSession, *, close_reason: str
        ) -> None: ...

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
        def _mark_empty_turn_endpoint_pin(
            context: _MediaVoiceSession, *, source: EndpointPinSource
        ) -> None: ...

    @staticmethod
    def _playback_current_line(context: _MediaVoiceSession) -> str:
        """The line the device is playing now, or the announced tail as a fallback.

        The ledger has per-phrase spans, so the current line is exact. A test
        double or an older path without spans keeps the previous tail window.
        """

        playback = getattr(context.output, "playback", None)
        fence = getattr(context.runtime, "fence", None)
        if playback is not None and fence is not None:
            line = playback.playing_text(fence)
            if line is not None:
                return str(line)
        return context.output.assistant_text[-_PLAYBACK_STOP_ECHO_TAIL_CHARS:]

    @staticmethod
    def _reply_can_still_be_stopped(context: _MediaVoiceSession) -> bool:
        """True while the reply can still be heard and stopped.

        A running reply task is live.  So is a finished provider whose audio the
        device is still draining (``provider_complete``): a question there must
        still stop the tail.  A task that died mid-stream with no completion is
        the speaking watchdog's stuck window (compliance #345), where speech must
        stay provisional and never become the turn that answers it.
        """

        if not hasattr(context.output, "reply_task"):
            # A partial double: nothing to prove the reply died.
            return True
        task = context.output.reply_task
        if task is not None and not task.done():
            return True
        return bool(getattr(context.output, "provider_complete", False))

    @staticmethod
    def _playback_input_guard_reason(
        context: _MediaVoiceSession, result: ASRResult
    ) -> str | None:
        """The commit's own guard verdict, so a garbled echo never pins an endpoint.

        ``_playback_text_is_echo`` catches an exact repeat; a fuzzy 12+ character
        echo (``assistant_echo``) also carries a ``low_information_fragment`` or
        ``non_target_language`` verdict here, and none of them may stop the reply.
        """

        duration_ms = (
            max(0, result.capture_end_sample - result.capture_start_sample) * 1_000 // 16_000
        )
        guard = getattr(context.runtime, "playback_guarded_reason", None)
        if not callable(guard):
            return None
        reason: str | None = guard(result.text.strip(), duration_ms=duration_ms)
        return reason

    @staticmethod
    def _playback_text_is_echo(context: _MediaVoiceSession, text: str) -> bool:
        """True when ``text`` repeats what the reply announced most recently."""

        heard = _compact(text)
        spoken = _compact(getattr(context.output, "assistant_text", ""))[
            -_PLAYBACK_STOP_ECHO_TAIL_CHARS:
        ]
        return bool(heard) and heard in spoken

    @staticmethod
    def _playback_stop_is_echo(context: _MediaVoiceSession, text: str) -> bool:
        """True when the line now playing itself contains the stop character.

        A single character such as 「停」 is below the generic assistant-echo
        guard. Suppress it only while the current line carries 「停」 (a story
        line like 「小兔子大喊：停！」); a stop heard against any other line is
        the owner's command, even if an earlier sentence said 「停」.
        """

        if not lexical_playback_control_only(text):
            return False
        return "停" in MediaPlaybackStopMixin._playback_current_line(context)

    @staticmethod
    def _playback_echo_for_guard(context: _MediaVoiceSession, text: str) -> bool:
        if lexical_playback_control_only(text):
            return MediaPlaybackStopMixin._playback_stop_is_echo(context, text)
        return MediaPlaybackStopMixin._playback_text_is_echo(context, text)

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
                f"content_barge_in={content_barge_in_wanted(text)} "
                f"guard={self._playback_input_guard_reason(context, result)} "
                f"echo={self._playback_echo_for_guard(context, text)} "
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
        vad_start = context.pending.active_vad_start_sample
        if vad_start is not None and vad_start >= playback_end:
            return  # A VAD that opened after playback sends its own end.
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
                if context.pending.followup_pin_monotonic is None:
                    context.pending.followup_pin_monotonic = time.monotonic()
                # The follow-up grew: its sentence changed, so verdicts ask again.
                context.runtime.begin_turn_verdicts()
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
        pending = context.pending
        endpoint = max(result.capture_end_sample, pending.turn_end_sample or 0)
        pending.turn_start_sample = (
            result.capture_start_sample
            if pending.turn_start_sample is None
            else min(pending.turn_start_sample, result.capture_start_sample)
        )
        pending.turn_end_sample = endpoint
        pending.turn_endpoint_sample = endpoint
        pending.turn_retire_sample = max(pending.turn_retire_sample or 0, endpoint)
        pending.playback_followup_endpoint_sample = endpoint
        if context.pending.followup_pin_monotonic is None:
            context.pending.followup_pin_monotonic = time.monotonic()
        # This final's own text is already on the timeline under the pinned
        # range, so the pin assumed words: only a VAD end over a range with no
        # projected text is a pin a later final may take back.
        context.pending.empty_turn_endpoint_pin = None
        context.pending.turn_endpoint_grace_deadline = (
            time.monotonic() + playback_followup_grace_s()
        )
        logger.info(
            "media playback-followup endpoint session=%s boundary=%s endpoint=%s text_len=%s",
            context.identity.session_id,
            playback_end,
            endpoint,
            len(result.text.strip()),
        )
        self._schedule_turn_commit(context)

    def defer_commit(self, context: _MediaVoiceSession, endpoint_sample: int) -> bool:
        """True when the commit due at ``endpoint_sample`` must wait and has been rescheduled.

        Committing during a fixed safety script would supersede it mid-sentence,
        so the turn waits for the script's end with a fresh tail bound on every
        recheck. Otherwise an incomplete follow-up may keep waiting.
        """

        if context.runtime.floor.fixed_safety_audible:
            logger.info(
                "media turn commit held for fixed safety playback session=%s endpoint=%s",
                context.identity.session_id,
                endpoint_sample,
            )
            context.pending.restart_endpoint_bounds(_FIXED_SAFETY_COMMIT_RECHECK_S)
            self._schedule_turn_commit(context)
            return True
        return self.defer_incomplete_followup_commit(context, endpoint_sample)

    def defer_incomplete_followup_commit(
        self, context: _MediaVoiceSession, endpoint_sample: int
    ) -> bool:
        """True when grace ran out on an incomplete follow-up that should keep waiting.

        Restarts the grace when the projected sentence still looks mid-clause and the
        incomplete-hold budget remains.  Returns False once the text looks complete or
        the hold expires, so the caller may commit.
        """

        pending = context.pending
        if pending.playback_followup_endpoint_sample != endpoint_sample:
            return False
        start = pending.turn_start_sample
        if start is None or endpoint_sample <= start:
            return False
        text = context.runtime.speech_timeline.projected_text(
            stream_epoch=context.stream_epoch, start_sample=start, end_sample=endpoint_sample
        )
        if followup_utterance_looks_complete(text):
            return False
        pinned_at = pending.followup_pin_monotonic
        if pinned_at is None:
            pending.followup_pin_monotonic = time.monotonic()
            pinned_at = pending.followup_pin_monotonic
        if time.monotonic() - pinned_at >= _PLAYBACK_FOLLOWUP_INCOMPLETE_HOLD_S:
            logger.info(
                "media playback-followup incomplete hold exhausted session=%s "
                "endpoint=%s text_len=%s",
                context.identity.session_id,
                endpoint_sample,
                len(text.strip()),
            )
            return False
        pending.restart_endpoint_bounds(playback_followup_grace_s())
        logger.info(
            "media playback-followup incomplete deferred session=%s endpoint=%s text_len=%s",
            context.identity.session_id,
            endpoint_sample,
            len(text.strip()),
        )
        self._schedule_turn_commit(context)
        return True

    def endpoint_overlapped_pending_after_playback(self, context: _MediaVoiceSession) -> None:
        """Pin a follow-up endpoint for speech that overlapped the reply just ended.

        Without this, an incomplete first half that slipped through commits a reply, the
        second half sits as ``pending_turn_playback_overlap``, and a stuck post-playback
        VAD never ends the turn: ASR tail timeout discards it (field demo blocker).
        """

        if context.identity.client_type != "device":
            return
        if self._reply_in_flight(context):
            return
        pending = context.pending
        start, end = pending.turn_start_sample, pending.turn_end_sample
        if (
            not pending.pending_turn_playback_overlap
            or pending.turn_endpoint_sample is not None
            or start is None
            or end is None
            or end <= start
        ):
            return
        text = context.runtime.speech_timeline.projected_text(
            stream_epoch=context.stream_epoch, start_sample=start, end_sample=end
        )
        if not text.strip():
            return
        duration_ms = (end - start) // _SAMPLES_PER_MS
        if (
            context.runtime.playback_guarded_reason(text, duration_ms=duration_ms)
            == "assistant_echo"
        ):
            return
        pending.turn_endpoint_sample = end
        pending.turn_retire_sample = max(pending.turn_retire_sample or 0, end)
        pending.playback_followup_endpoint_sample = end
        if pending.followup_pin_monotonic is None:
            pending.followup_pin_monotonic = time.monotonic()
        pending.pending_turn_playback_overlap = False
        pending.restart_endpoint_bounds(playback_followup_grace_s())
        logger.info(
            "media playback-overlap followup endpoint session=%s endpoint=%s text_len=%s",
            context.identity.session_id,
            end,
            len(text.strip()),
        )
        self._schedule_turn_commit(context)

    def _warm_endpoint_commit(self, context: _MediaVoiceSession) -> None:
        """A commit is scheduled and its grace runs: start what the commit would wait for after it.

        Round 12 (2026-10-03): the conversation-close verdict (p50 403 ms) was asked only after the playback
        follow-up grace, because the task the final scheduled found the endpoint already pinned and returned
        without asking, and the bound person's memory (about 0.4 s) was fetched after it too (TODOLIST N-14
        7 and 8).  Round 14 (2026-10-04): every commit that follows a device VAD end waits the same way
        (final to end of commit p50 2.9 s, against 1.3 s for the follow-up flow): the final arrives while the
        VAD end is being finalized, the endpoint is set a few milliseconds later in the same step, and the
        task the final scheduled returns at once; and when earlier finals are merged into the turn, the live
        verdict started at the last final asks about a shorter sentence than the commit does (N-14 9).

        So this runs wherever a commit is scheduled and reads the sentence the way the commit will (the pending
        turn up to the endpoint): a final that extends the turn starts the longer text, and whatever nobody
        uses costs one read-only call.  It starts nothing unless the commit will go ahead with that sentence:
        not while a reply is in flight (its echo must not start a verdict), not before the ASR covers the
        endpoint (the commit waits for a late final) and not for a turn a pin owns (a farewell, a clock fact
        and a live query have their own routes).  Never at the price of the commit: a failure is logged and
        the commit goes on as before.
        """

        pending = context.pending
        start, endpoint = pending.turn_start_sample, pending.turn_endpoint_sample
        if (
            start is None
            or endpoint is None
            or endpoint <= start  # the commit refuses such a range, and the timeline raises on it
            or self._reply_in_flight(context)
            or not self._asr_covers_endpoint(context, pending.turn_end_sample, endpoint)
            or pending.clock_fact_endpoint_pinned is not None
            or pending.conversation_close_endpoint_pinned is not None
            or pending.live_query_endpoint_pinned is not None
        ):
            return
        runtime = context.runtime
        text = ""
        try:
            text = runtime.speech_timeline.projected_text(
                stream_epoch=context.stream_epoch, start_sample=start, end_sample=endpoint
            )
            if not text or text == pending.warmed_commit_text:
                return
            pending.warmed_commit_text = text
            runtime.start_conversation_close_verdict(text)
            runtime.live_lookup_needed(text, start_verdict=True)  # also starts the semantic verdict
            warm = getattr(context.provider, "warm_committed_turn", None)
            if callable(warm):
                warm(context.identity, text)
        except Exception:
            logger.warning(
                "media commit warm-up failed session=%s text_len=%s",
                context.identity.session_id,
                len(text),
                exc_info=True,
            )

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
        straddle = (
            self._playback_followup_straddles(context, result)
            and (context.pending.turn_end_sample or 0) <= result.capture_start_sample
        )
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
        context.runtime.speech_timeline.evict_before(stream_epoch=result.stream_epoch, sample=start)
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
            context,
            text,
            kind=f"conversation-close {source}",
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
            "text_len=%s source=%s rule_match=%s asr_samples=%s-%s "
            "asr_task_epoch=%s asr_rescue=%s",
            context.identity.session_id,
            endpoint,
            len(text),
            source,
            rule_conversation_close_only(text),
            result.capture_start_sample if result is not None else None,
            result.capture_end_sample if result is not None else None,
            result.task_epoch if result is not None else None,
            result.rescue_synthesized if result is not None else None,
        )

    @staticmethod
    def _final_overlaps_handled_turn(
        context: _MediaVoiceSession,
        result: ASRResult,
        handled_range: tuple[int | None, int | None] | None,
    ) -> bool:
        """True when this final overlaps the range the turn already handled.

        ``_observe_final_asr_result`` merges the final into the pending turn
        before this decision, so the range from before it is passed in.  A
        final that starts inside a range with an end re-finalizes audio the
        reply already answers.

        A range with no end is bounded only by its start.  A final may begin a
        little before the VAD edge that detects live speech (soak turn 18:
        0.15 s), so only a turn whose floor was already released without words
        (``fresh_user_speech`` false) owns a straddling final (t017).
        """

        if handled_range is None:
            return False
        start, end = handled_range
        if start is None:
            return False
        if end is not None:
            return result.capture_start_sample < end and result.capture_end_sample > start
        floor = getattr(context.runtime, "floor", None)
        if bool(getattr(floor, "fresh_user_speech", True)):
            return False
        return result.capture_start_sample < start < result.capture_end_sample

    @staticmethod
    def _pinned_turn_holds_no_words(
        context: _MediaVoiceSession,
        result: ASRResult,
        *,
        content_barge_in: bool = False,
    ) -> bool:
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
            or endpoint > result.capture_start_sample
            or pending.clock_fact_endpoint_pinned is not None
            or pending.conversation_close_endpoint_pinned is not None
            or pending.live_query_endpoint_pinned is not None
            or pending.clock_fact_forced_text
            or pending.live_query_forced_text
        ):
            return False
        # Field 2026-10-21 (session 74cdaf88 S1): a VAD end pinned 236480, then
        # the turn reopened at 250880.  ``start < endpoint`` was false, so the
        # range was never asked whether it held words and the stop final
        # (stop_word=True, text_len=2) was refused as endpoint_already_pinned
        # until the ASR tail timeout threw the turn away.  An inverted range
        # cannot hold a word, but only a final that brings words of its own may
        # take it: a wait or completion turn (「等等」) belongs to the empty
        # tail being retired, not to a new turn.  So does a content question
        # (soak 2026-10-30 t019): the pin it replaces holds no words either.
        if start >= endpoint:
            return is_stop_command_only(result.text) or content_barge_in
        return not context.runtime.speech_timeline.projected_text(
            stream_epoch=result.stream_epoch, start_sample=start, end_sample=endpoint
        )

    def _log_asr_text_debug(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
        *,
        source: str,
    ) -> None:
        """The final this playback-stop decision read, for a whitelisted session.

        The line also covers an empty final (``text_len=0``): a 「停」 that
        FunASR dropped reaches every guard below as an empty string, and only
        its text and interval say whether the segment was heard at all.
        """

        subjects = session_asr_text_debug_subjects(context)
        if not subjects.enabled:
            return
        identity = context.identity
        text = result.text.strip()
        line = asr_text_debug_line(
            subjects,
            identity.subject_id,
            identity.device_id,
            source=source,
            session_id=identity.session_id,
            subject_sha256_8=subject_tag(identity.subject_id),
            device_sha256_8=subject_tag(identity.device_id),
            stream_epoch=result.stream_epoch,
            task_epoch=result.task_epoch,
            sentence_id=result.sentence_id,
            final=result.is_final,
            rescue=result.rescue_synthesized,
            empty=not result.text.strip(),
            text_len=len(result.text),
            begin_ms=result.provider_begin_ms,
            end_ms=result.provider_end_ms,
            begin_sample=result.capture_start_sample,
            end_sample=result.capture_end_sample,
            playback_boundary_sample=context.last_playback_end_sample,
            in_flight=self._reply_in_flight(context),
            stop_word=bool(text) and lexical_playback_control_only(text),
            text=excerpt(result.text) if result.text else None,
        )
        if line is not None:
            logger.info(line)

    def _maybe_pin_playback_stop(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
        *,
        source: str,
        handled_range: tuple[int | None, int | None] | None = None,
    ) -> None:
        """Endpoint a device stop phrase or question heard while a reply holds the floor.

        Only the stop phrase's own interval is committed
        (``_scope_pending_turn_to``): earlier playback-window candidates would
        otherwise merge into 「……停」, route as chat and stay held.  A stop
        phrase or a content question also replaces the endpoint of an earlier
        turn that holds no words (``_pinned_turn_holds_no_words``), which has
        nothing to lose.

        ``handled_range`` is the turn's range from before ``result`` was merged
        into it.  A final that overlaps that range re-finalizes the turn the
        reply already answers, so it never counts as a content barge
        (``_final_overlaps_handled_turn``, soak 2026-10-30 t017).

        The committed turn is marked (``pending.playback_stop_text``) so its
        route cannot depend on the same text peeling twice: a stop stays the
        control turn even after it already muted the reply.  A question instead
        sets ``pending.content_barge_in``: its commit stops the reply and answers it.
        """

        if context.identity.client_type != "device":
            return
        self._log_asr_text_debug(context, result, source=source)
        text = result.text.strip()
        stop_word = bool(text) and lexical_playback_control_only(text)
        content_barge_in = (
            not stop_word
            and not result.rescue_synthesized
            and self._reply_can_still_be_stopped(context)
            and content_barge_in_wanted(text)
            and not self._playback_text_is_echo(context, text)
            and self._playback_input_guard_reason(context, result) is None
        )
        tail_final = content_barge_in and self._final_overlaps_handled_turn(
            context, result, handled_range
        )
        if tail_final:
            content_barge_in = False
        in_flight = self._reply_in_flight(context)
        diagnosis = self._playback_stop_diagnosis(context, result)
        replaced_endpoint = context.pending.turn_endpoint_sample
        # The same guards in the same order as before; each one now says why it held.
        reason = ""
        if context.closed or context.standby_requested:
            reason = "session_closing"
        elif tail_final:
            reason = "overlapping_turn_final"
        elif replaced_endpoint is not None and not (
            (stop_word or content_barge_in)
            and self._pinned_turn_holds_no_words(
                context, result, content_barge_in=content_barge_in
            )
        ):
            reason = "endpoint_already_pinned"
        elif not stop_word and not content_barge_in:
            reason = "not_stop_word"
        elif not in_flight:
            reason = "no_reply_in_flight"
        else:
            floor = context.pending.pending_turn_onset_floor
            if floor is not None and result.capture_start_sample < floor:
                reason = "before_onset_floor"
        if reason:
            if in_flight or stop_word:
                self._log_unpinned_final(context, reason=reason, diagnosis=diagnosis, source=source)
            return
        if self._playback_stop_is_echo(context, text):
            logger.info(
                "media playback stop ignored as reply echo session=%s text_len=%s source=%s %s",
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
        self._mark_empty_turn_endpoint_pin(context, source="vad_end")
        pending.playback_stop_text = text if stop_word else None
        pending.content_barge_in = content_barge_in
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

    async def _stop_reply_for_voice_command(
        self, context: _MediaVoiceSession, text: str, *, cause: str = _PLAYBACK_STOP_CAUSE
    ) -> bool:
        """Carry out a Router-accepted spoken stop like a keyword stop.

        ``accept_user_turn`` only suppresses the control turn; the reply task,
        the generation and the device playback must be stopped here.  The
        session stays open: this is never the END_SESSION path.
        """

        if context.closed or context.standby_requested or not context.runtime.floor.interruptible:
            return False
        if context.runtime.floor.fixed_safety_audible:
            logger.info(
                "media spoken stop ignored during fixed safety playback session=%s",
                context.identity.session_id,
            )
            return False
        previous_fence = context.output.playback.current_fence or context.runtime.fence
        if not previous_fence.matches(context.runtime.fence):
            # Another control already replaced this generation.
            return False
        marker_fence = context.aigc_opening_marker_fence or context.aigc_closing_marker_fence
        if marker_fence is not None and previous_fence.matches(marker_fence):
            logger.info(
                "media spoken stop ignored during AIGC notice session=%s",
                context.identity.session_id,
            )
            return False
        stop_started_ns = time.monotonic_ns()
        flush_required = self._device_playback_flush_required(context, previous_fence)
        await self._record_interrupted_timed_spans(context, previous_fence)
        heard = context.output.playback.actual_heard_text(previous_fence)
        cancelled = await context.runtime.on_real_interrupt(
            cause=cause,
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
        context.output.playback.discard(previous_fence)
        context.output.provider_complete = False
        context.output.output_complete_emitted = False
        # The old reply is gone: its announced text must not leak into the next
        # turn's echo guard or be mistaken for the reply that is speaking.
        context.output.assistant_text = ""
        if should_pause_asr_for_playback(context.identity):
            await context.provider.pause_asr_for_playback(context.identity)
        await self._cancel_reply_task(
            context,
            previous_fence,
            reason=cause,
            finalize_audible=False,
        )
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
            "replacement_generation=%s flush=%s cause=%s",
            context.identity.session_id,
            previous_fence.turn_id,
            previous_fence.generation_id,
            cancelled.generation_id,
            flush_required,
            cause,
        )
        self._schedule_aigc_round_closing(context, close_reason=cause)
        return True


__all__ = [
    "CONTENT_BARGE_IN_CAUSE",
    "PLAYBACK_STOP_ROUTE_REASON",
    "MediaPlaybackStopMixin",
    "content_barge_in_wanted",
]
