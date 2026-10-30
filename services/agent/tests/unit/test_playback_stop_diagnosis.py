"""Every final heard during device playback says why it did or did not stop the reply.

2026-10-01 voice soak on DeepSeek: the owner's 「别说了」 reached the ASR clearly and a final
followed, yet no stop ran and nothing was logged, because each guard of
``_maybe_pin_playback_stop`` returned silently. These pin the diagnostics (never the text)
and that the guards decide as before, except that a stop word takes the endpoint of an
earlier turn that holds no words (soak10: a text-less VAD edge held the stop out for 3-6 s).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason
from services.agent.src.voice_core.media_session_commit import MediaSessionCommitMixin
from services.agent.src.voice_core.media_session_pending_turn import PendingTurn
from services.agent.src.voice_core.media_session_playback_stop import MediaPlaybackStopMixin
from services.agent.src.voice_core.speech_timeline import ASRResult

LOGGER = "services.agent.src.voice_core.media_session_playback_stop"
COMMIT_LOGGER = "services.agent.src.voice_core.media_session_commit"
STORY = "小刺猬回到家，把最后一点果子递给妈妈。妈妈咬了一小口，笑着说，真甜。"


class _Session(MediaPlaybackStopMixin):
    def __init__(self, *, in_flight: bool = True) -> None:
        self.in_flight = in_flight
        self.scoped: list[ASRResult] = []
        self.commits = 0

    def _reply_in_flight(self, context: Any) -> bool:
        return self.in_flight

    def _scope_pending_turn_to(self, context: Any, result: ASRResult) -> None:  # type: ignore[override]
        self.scoped.append(result)

    def _schedule_turn_commit(self, context: Any) -> None:
        self.commits += 1

    def _mark_empty_turn_endpoint_pin(self, context: Any, *, source: str) -> None:
        MediaSessionCommitMixin._mark_empty_turn_endpoint_pin(context, source=source)

    def _pending_turn_holds_no_words(self, context: Any) -> bool:
        return MediaSessionCommitMixin._pending_turn_holds_no_words(context)


class _CommitSession(MediaSessionCommitMixin, _Session):
    def __init__(self, context: Any) -> None:
        _Session.__init__(self)
        self._sessions = {"s1": context}


def _context(
    *,
    endpoint: int | None = None,
    start: int | None = None,
    words: str = "",
    spoken: str = STORY,
    playing: str | None = None,
    **pending: Any,
) -> Any:
    """A device session mid-story; ``words`` is the text the timeline holds in the pinned range."""

    fence = GenerationFence("s1", 1, 1, 0)
    playback = SimpleNamespace(playing_text=lambda _fence: playing) if playing is not None else None
    return SimpleNamespace(
        identity=SimpleNamespace(client_type="device", session_id="s1", stream_epoch=1),
        stream_epoch=1,
        closed=False,
        standby_requested=False,
        pending=PendingTurn(turn_endpoint_sample=endpoint, turn_start_sample=start, **pending),
        output=SimpleNamespace(
            assistant_text=spoken,
            output_owner=GenerationFence("s1", 1, 4, 0),
            playback=playback,
        ),
        runtime=SimpleNamespace(
            assistant_speaking=True,
            fence=fence,
            speech_timeline=SimpleNamespace(projected_text=lambda **_: words),
            playback_guarded_reason=lambda _text, *, duration_ms: None,
        ),
    )


def _final(
    text: str, *, start: int = 160_000, end: int = 176_000, is_final: bool = True
) -> ASRResult:
    return ASRResult(
        task_epoch=1,
        sentence_id="sentence-1",
        revision=1,
        capture_start_sample=start,
        capture_end_sample=end,
        text=text,
        is_final=is_final,
    )


def _lines(caplog: pytest.LogCaptureFixture, prefix: str) -> list[str]:
    return [
        record.getMessage() for record in caplog.records if record.getMessage().startswith(prefix)
    ]


def test_a_stop_word_after_an_earlier_pin_says_the_endpoint_was_taken(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The 2026-09-29 note: a misheard 「停」 pinned the endpoint, the real one was refused.
    session, context = _Session(), _context(endpoint=150_000, start=140_000, words="行。")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("别说了。"), source="final")

    [line] = _lines(caplog, "media playback-stop not taken")
    assert "reason=endpoint_already_pinned" in line and "stop_word=True" in line
    assert "endpoint_ms=-625" in line and "别说了" not in line
    assert context.pending.turn_endpoint_sample == 150_000 and session.commits == 0


class _Timer:
    """The tail-timeout handle of the pin that is replaced."""

    def __init__(self) -> None:
        self.is_cancelled = False

    def cancel(self) -> None:
        self.is_cancelled = True

    def cancelled(self) -> bool:
        return self.is_cancelled


def test_a_stop_word_replaces_the_endpoint_of_a_turn_with_no_words(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # soak10 2026-10-02: a VAD edge on room noise pinned an empty turn and the 「别说了」
    # after it was refused as endpoint_already_pinned until the ASR tail timeout.
    session, context = _Session(), _context(endpoint=150_000, start=140_000)
    timer = _Timer()
    context.pending.turn_endpoint_timeout_handle = timer
    context.pending.turn_endpoint_tail_deadline = 1.0
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("别说了。"), source="final")

    assert context.pending.turn_endpoint_sample == 176_000 and session.commits == 1
    assert context.pending.turn_retire_sample == 176_000
    # The replaced pin's tail timer belongs to its endpoint: the new one gets its own bound.
    assert timer.is_cancelled and context.pending.turn_endpoint_timeout_handle is None
    assert context.pending.turn_endpoint_tail_deadline is None
    assert context.pending.turn_endpoint_grace_deadline is not None
    [line] = _lines(caplog, "media early playback-stop endpoint")
    assert "replaced_empty_endpoint=150000" in line and "stop_word=True" in line
    assert "别说了" not in line and not _lines(caplog, "media playback-stop not taken")


@pytest.mark.parametrize(
    ("pin", "start", "words", "final_start"),
    [
        pytest.param({}, 140_000, "行。", 160_000, id="words-in-the-pinned-range"),
        pytest.param({}, 140_000, "", 145_000, id="pin-still-covers-the-final"),
        pytest.param({}, None, "", 160_000, id="no-turn-start"),
        pytest.param({"clock_fact_endpoint_pinned": 150_000}, 140_000, "", 160_000, id="clock-pin"),
        pytest.param(
            {"conversation_close_endpoint_pinned": 150_000}, 140_000, "", 160_000, id="close-pin"
        ),
        pytest.param(
            {"live_query_endpoint_pinned": 150_000}, 140_000, "", 160_000, id="live-query-pin"
        ),
        pytest.param(
            {"clock_fact_forced_text": "现在几点了"}, 140_000, "", 160_000, id="forced-clock-text"
        ),
        pytest.param(
            {"live_query_forced_text": "明天天气怎么样"}, 140_000, "", 160_000, id="forced-lookup"
        ),
    ],
)
def test_a_pin_that_is_a_real_turn_still_refuses_the_stop_word(
    caplog: pytest.LogCaptureFixture,
    pin: dict[str, Any],
    start: int | None,
    words: str,
    final_start: int,
) -> None:
    session = _Session()
    context = _context(endpoint=150_000, start=start, words=words, **pin)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(
            context, _final("别说了。", start=final_start, end=final_start + 16_000), source="final"
        )

    [line] = _lines(caplog, "media playback-stop not taken")
    assert "reason=endpoint_already_pinned" in line
    assert context.pending.turn_endpoint_sample == 150_000 and session.commits == 0


@pytest.mark.parametrize(
    ("start", "endpoint"),
    [
        pytest.param(140_000, 150_000, id="wordless-range"),
        pytest.param(150_000, 150_000, id="pin-at-the-turn-start"),
    ],
)
def test_a_content_question_takes_over_an_empty_pin(
    caplog: pytest.LogCaptureFixture,
    start: int,
    endpoint: int,
) -> None:
    # soak 2026-10-30 t019: a wordless VAD pinned its own start, and the owner's
    # question after it was refused as endpoint_already_pinned until the tail timeout.
    session, context = _Session(), _context(endpoint=endpoint, start=start)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("我们下午几点出发"), source="final")

    assert context.pending.turn_endpoint_sample == 176_000 and session.commits == 1
    assert context.pending.content_barge_in is True
    [line] = _lines(caplog, "media early playback-stop endpoint")
    assert "content_barge_in=True" in line and f"replaced_empty_endpoint={endpoint}" in line
    assert not _lines(caplog, "media playback-stop not taken")


def test_speech_with_no_content_still_leaves_an_empty_pin_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A backchannel has no question to answer; the pin stays and the log names it.
    session, context = _Session(), _context(endpoint=150_000, start=140_000)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("嗯。"), source="final")

    [line] = _lines(caplog, "media playback-stop not taken")
    assert "reason=endpoint_already_pinned" in line and "stop_word=False" in line
    assert context.pending.turn_endpoint_sample == 150_000 and session.commits == 0


def test_a_stop_word_the_later_guards_refuse_leaves_the_empty_pin_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, context = _Session(), _context(endpoint=150_000, start=140_000, spoken="大喊：停！")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("停"), source="final")
    assert _lines(caplog, "media playback stop ignored as reply echo")
    assert context.pending.turn_endpoint_sample == 150_000 and session.commits == 0

    caplog.clear()
    session, context = _Session(in_flight=False), _context(endpoint=150_000, start=140_000)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("停"), source="final")
    [line] = _lines(caplog, "media playback-stop not taken")
    assert "reason=no_reply_in_flight" in line
    assert context.pending.turn_endpoint_sample == 150_000 and session.commits == 0


def test_the_replys_own_voice_heard_as_speech_is_logged_without_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, context = _Session(), _context()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("现在可。"), source="final")

    [line] = _lines(caplog, "media playback-stop not taken")
    assert "reason=not_stop_word" in line and "stop_word=False" in line and "in_flight=True" in line
    assert "现在可" not in line and "text_len=4" in line


def test_a_clear_stop_word_still_pins_the_stop(caplog: pytest.LogCaptureFixture) -> None:
    session, context = _Session(), _context()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("别说了。"), source="final")

    assert context.pending.turn_endpoint_sample == 176_000 and session.commits == 1
    assert context.pending.playback_stop_text == "别说了。"
    assert context.pending.content_barge_in is False
    [line] = _lines(caplog, "media early playback-stop endpoint")
    assert "stop_word=True" in line and "echo=False" in line
    assert not _lines(caplog, "media playback-stop not taken")


def test_a_stop_word_the_reply_itself_just_said_is_still_echo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, context = (
        _Session(),
        _context(
            spoken="小兔子大喊：停！",
            playing="小兔子大喊：停！",
        ),
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("停"), source="final")

    assert context.pending.turn_endpoint_sample is None and session.commits == 0
    [line] = _lines(caplog, "media playback stop ignored as reply echo")
    assert "echo=True" in line


def test_a_stop_word_is_allowed_when_the_current_line_has_no_stop_character(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An earlier 「停」 in the reply must not swallow a later real stop."""

    session, context = (
        _Session(),
        _context(
            spoken="小兔子大喊：停！然后继续跑。",
            playing="然后继续跑。",
        ),
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("停"), source="final")

    assert context.pending.turn_endpoint_sample == 176_000 and session.commits == 1
    [line] = _lines(caplog, "media early playback-stop endpoint")
    assert "stop_word=True" in line and "echo=False" in line
    assert not _lines(caplog, "media playback stop ignored as reply echo")


def test_a_stop_word_is_allowed_when_the_playing_line_is_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, context = _Session(), _context(spoken="小兔子大喊：停！", playing="")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("停"), source="final")

    assert context.pending.turn_endpoint_sample == 176_000 and session.commits == 1
    assert not _lines(caplog, "media playback stop ignored as reply echo")


def test_ordinary_speech_outside_playback_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    session, context = _Session(in_flight=False), _context()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("恐龙都吃什么东西？"), source="final")

    assert not caplog.records
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("停"), source="final")
    [line] = _lines(caplog, "media playback-stop not taken")
    assert "reason=no_reply_in_flight" in line


def test_a_question_heard_during_playback_pins_and_is_not_a_control_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, context = _Session(), _context()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(
            context, _final("等一下，我想问另一个问题，云是怎么变出来的？"), source="final"
        )

    assert context.pending.turn_endpoint_sample == 176_000 and session.commits == 1
    assert context.pending.content_barge_in is True
    assert context.pending.playback_stop_text is None
    assert not _lines(caplog, "media playback-stop not taken")


def test_a_question_the_playing_line_already_says_is_echo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, context = _Session(), _context()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("小刺猬回到家，"), source="final")

    [line] = _lines(caplog, "media playback-stop not taken")
    assert "reason=not_stop_word" in line and "echo=True" in line
    assert context.pending.turn_endpoint_sample is None and session.commits == 0
    assert context.pending.content_barge_in is False


def test_a_backchannel_alone_does_not_pin_the_endpoint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, context = _Session(), _context()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        session._maybe_pin_playback_stop(context, _final("嗯。"), source="final")

    [line] = _lines(caplog, "media playback-stop not taken")
    assert "reason=not_stop_word" in line
    assert context.pending.turn_endpoint_sample is None and session.commits == 0
    assert context.pending.content_barge_in is False


def test_a_rejected_final_says_whether_it_was_a_stop_word(caplog: pytest.LogCaptureFixture) -> None:
    session = _CommitSession(_context())
    with caplog.at_level(logging.INFO, logger=COMMIT_LOGGER):
        decision = session._reject_accepted_final(
            "s1", _final("别说了。"), ASRDecisionReason.INTERVAL_CONFLICT, stage="transcript"
        )
        session._reject_accepted_final(
            "s1",
            _final("别说", is_final=False),
            ASRDecisionReason.INTERVAL_CONFLICT,
            stage="transcript",
        )

    assert decision.accepted is None and decision.reason is ASRDecisionReason.INTERVAL_CONFLICT
    [line] = _lines(caplog, "media ASR result rejected")
    assert "stage=transcript" in line and "stop_word=True" in line and "别说了" not in line
