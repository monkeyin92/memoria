"""Media session: playback echo windows, overlap and retained-onset turns."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from services.agent.src.voice_core import media_session_input
from services.agent.src.voice_core.media_session_commit import _resolve_media_turn_text
from services.agent.src.voice_core.speech_timeline import (
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.media_session_support import (
    _ABANDONED_WINDOW_END,
    _accept_media_asr_decision,
    _accept_media_asr_final,
    _commit_pending_turn_from_device_endpoint,
    _commit_timing_fields,
    _complete_previous_device_playback,
    _device_vad,
    _feed_playback_window_finals,
    _open_retained_window,
    _start_retained_utterance,
    _user_turn_texts,
    _wait_until,
)
from services.agent.tests.unit.runtime_state_helpers import set_floor


@pytest.mark.asyncio
async def test_media_playback_overlap_finals_stay_out_of_the_next_commit(
    device_media_session: Any,
) -> None:
    """Bridge evidence 2026-09-15 epoch 1955: a played-back reply on the uplink.

    Five finals were accepted while the previous reply still owned playback
    (text_len 3, 4, 11, 4 and 7), the device reported no VAD edge at all after
    sample 113600, and the real question then arrived as text_len=10 on samples
    484480-508800.  The turn committed as text_len=42: ``turn_start_sample`` had
    been dragged back to the earliest of those finals, so the commit joined
    every pending timeline segment inside the resulting range, and that
    transcript is what the location lookup then searched.  Per-segment provider
    text was not captured, so the placeholders keep the logged lengths.
    """

    window = await device_media_session("playback-overlap-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _feed_playback_window_finals(window)
    assert context.pending.pending_turn_playback_overlap is True

    # Before the real question arrives the pending candidate window already
    # resolves those five finals as one 29-character transcript: that is the
    # polluted shape the field session committed as 42 characters.  The turn
    # boundary below is what has to keep it out of the committed range.
    pre_split = _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=158_560,
        end_sample=_ABANDONED_WINDOW_END,
    )
    assert pre_split is not None and "AAA" in pre_split
    # The live projection already carries the same polluted text, so the commit
    # has to reconcile an in-flight projection against the new boundary.
    assert context.projection.provisional is not None
    assert "AAA" in context.projection.provisional.text

    _start_retained_utterance(window)  # the previous reply ended here
    question = "今天南京的天气怎么样"
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="question-13",
        start_sample=484_480,
        end_sample=508_800,
        text=question,
    )

    # 350080 -> 484480 is 8.4 s, far beyond the conservative candidate gap: the
    # window closes and the question owns the turn on its own samples.
    assert context.pending.turn_start_sample == 484_480
    assert context.pending.turn_end_sample == 508_800

    text = _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=context.pending.turn_start_sample,
        end_sample=context.pending.turn_end_sample,
    )
    assert text == question

    # The abandoned window is retired, not merely out of range: resolving it
    # again (and resolving the wide pre-fix range) can no longer reach its text.
    abandoned = _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=158_560,
        end_sample=_ABANDONED_WINDOW_END,
    )
    assert not abandoned
    wide = _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=158_560,
        end_sample=508_800,
    )
    assert wide == question

    # The device chain then prepares exactly one turn, and the provider is
    # handed only the new question.
    await _wait_until(lambda: bool(window.provider.prepared), timeout=2.0)
    assert window.provider.prepared == [question]
    assert _user_turn_texts(context) == [question]


@pytest.mark.asyncio
async def test_media_out_of_order_final_cannot_rejoin_a_split_pending_turn(
    device_media_session: Any,
) -> None:
    """A late non-overlapping final for the abandoned window must not flow back.

    The ASR supervisor deliberately keeps out-of-order non-overlapping finals
    (``accept_result``), so the boundary cannot rely on arrival order: the split
    has to be a lifecycle decision that survives a later final for the audio it
    already closed.
    """

    window = await device_media_session("split-out-of-order-session")
    registry, identity, context = window.registry, window.identity, window.context
    from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

    for sentence_id, start_sample, end_sample, text, revision in (
        ("playback-2", 158_560, 163_040, "AAA", 1),
        ("playback-3", 185_280, 204_480, "BBBB", 2),
    ):
        await _accept_media_asr_final(
            registry,
            identity,
            sentence_id=sentence_id,
            start_sample=start_sample,
            end_sample=end_sample,
            text=text,
            revision=revision,
        )
    _start_retained_utterance(window)
    question = "下午一起出发吗"
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="question-13",
        start_sample=484_480,
        end_sample=508_800,
        text=question,
    )

    # Non-overlapping, so the supervisor would accept it on interval grounds;
    # the pending-turn boundary is what has to fail it closed.
    late = await _accept_media_asr_decision(
        registry,
        identity,
        sentence_id="late-playback-9",
        start_sample=317_280,
        end_sample=332_640,
        text="DDDD",
        revision=9,
    )
    assert late.accepted is None
    assert late.reason is ASRDecisionReason.INTERVAL_CONFLICT

    text = _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=context.pending.turn_start_sample,
        end_sample=context.pending.turn_end_sample,
    )
    assert text == question
    assert context.pending.turn_start_sample == 484_480


@pytest.mark.asyncio
async def test_media_long_pause_without_playback_overlap_still_merges(
    device_media_session: Any,
) -> None:
    """An ordinary utterance that pauses longer than the tail keeps both clauses."""

    window = await device_media_session("long-pause-session", during_playback=False)
    registry, identity, context = window.registry, window.identity, window.context

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="clause-1",
        start_sample=0,
        end_sample=8_000,
        text="未来三天南京天气",
    )
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="clause-2",
        start_sample=90_000,
        end_sample=100_000,
        text="还有明天呢",
        revision=2,
    )

    assert context.pending.turn_start_sample == 0
    assert context.pending.turn_end_sample == 100_000
    text = _resolve_media_turn_text(
        context, stream_epoch=1, start_sample=0, end_sample=100_000
    )
    assert text is not None
    assert "未来三天南京天气" in text and "还有明天呢" in text


async def _echo_window_candidate(window: Any) -> None:
    """The reply ended (boundary 100000, 0.8 s past its evidence) and no VAD
    edge came; a final began inside that echo window and is the pending turn."""

    window.context.last_playback_end_sample = 100_000
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="echo-window-1",
        start_sample=95_000,
        end_sample=99_000,
        text="停",
    )
    assert window.context.pending.turn_start_sample == 95_000
    assert window.context.pending.pending_turn_playback_overlap is False
    assert window.context.pending.turn_endpoint_sample is None


@pytest.mark.asyncio
async def test_media_echo_window_candidate_gives_way_after_an_unvoiced_gap(
    device_media_session: Any,
) -> None:
    """Field 2026-10-02 round 10 t015: the stale candidate deadlocked the next
    question; after a 2.5 s unvoiced gap it is split off and the question,
    wholly past the boundary, is endpointed as a post-playback follow-up."""

    window = await device_media_session("echo-window-gap", during_playback=False)
    await _echo_window_candidate(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="question-2",
        start_sample=145_000,
        end_sample=160_000,
        text="给我讲一个故事吧",
        revision=2,
    )
    context = window.context
    assert context.pending.turn_start_sample == 145_000
    assert context.pending.playback_followup_endpoint_sample == 160_000
    assert _resolve_media_turn_text(
        context, stream_epoch=1, start_sample=95_000, end_sample=160_000
    ) == "给我讲一个故事吧"


@pytest.mark.asyncio
async def test_media_echo_window_candidate_keeps_a_short_pause(
    device_media_session: Any,
) -> None:
    """Without a 2.5 s unvoiced gap the fallback never cuts an utterance."""

    window = await device_media_session("echo-window-short-pause", during_playback=False)
    await _echo_window_candidate(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="clause-2",
        start_sample=120_000,
        end_sample=130_000,
        text="还有呢",
        revision=2,
    )
    context = window.context
    assert context.pending.turn_start_sample == 95_000
    text = _resolve_media_turn_text(
        context, stream_epoch=1, start_sample=95_000, end_sample=130_000
    )
    assert text is not None and "停" in text and "还有呢" in text


# The reply's last words, and what the realtime ASR made of its own voice leaking into the microphone
# (2026-10-03 round 11: 27-53 character finals, seen 0.7-0.94 s after the playback ended).
_ECHOED_REPLY = "因为天上的云里装满了小水滴，水滴越来越重，就会变成雨落下来啦。"
_ECHOED_REPLY_ASR = "因为天上的云里装满了小水滴水滴越来越重就会变成雨落下来拉"
_QUESTION = "为什么会下雨呀"


async def _post_playback_echo_candidate(window: Any) -> None:
    """The reply ended (boundary 300000 = evidence 287200 + the 0.8 s echo-tail margin) and its own
    voice, finalized after the playback ended, is the pending turn: no endpoint, no overlap flag."""

    set_floor(
        window.context.runtime,
        played_assistant_text=_ECHOED_REPLY,
        last_playback_completed_ns=time.monotonic_ns(),
    )
    window.context.last_playback_end_sample = 300_000
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="reply-echo",
        start_sample=185_600,
        end_sample=295_040,
        text=_ECHOED_REPLY_ASR,
    )
    pending = window.context.pending
    assert pending.turn_start_sample == 185_600
    assert pending.turn_endpoint_sample is None
    assert pending.pending_turn_playback_overlap is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vad_start", "question_start", "question_end", "voiced_end"),
    [
        # The field case (bridge log of round 11 t002): 3.2 s after the echo ended.
        (348_160, 346_560, 364_480, 361_920),
        # A quick child, 1.1 s after it: the 2.5 s unvoiced-gap rule alone would never cut this.
        (313_600, 312_000, 325_000, 324_000),
    ],
)
async def test_media_post_playback_echo_does_not_swallow_the_next_question(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
    vad_start: int,
    question_start: int,
    question_end: int,
    voiced_end: int,
) -> None:
    """Round 11 (2026-10-03): 4 of 24 questions got silence with ``reason=assistant_echo``.

    The reply's own voice was finalized after its playback ended and became the pending turn.  The
    child's question came 1-3 s later; its final is accepted while the device VAD end is still being
    finalized, so the VAD latch is live and no endpoint is pinned yet.  The split of a stale
    echo-window candidate refused on that latch, the echo and the question merged, and the whole turn
    was dropped at commit as ``assistant_echo``: the question vanished with it.
    """

    monkeypatch.setenv("MEDIA_PLAYBACK_FOLLOWUP_GRACE_S", "0.3")
    window = await device_media_session("echo-then-question", during_playback=False)
    context = window.context
    await _post_playback_echo_candidate(window)

    await _device_vad(window, "question-vad", vad_start, final=False)
    assert context.pending.active_vad_start_sample == vad_start
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="question",
        start_sample=question_start,
        end_sample=question_end,
        text=_QUESTION,
        revision=2,
    )

    assert context.pending.turn_start_sample == question_start
    assert _resolve_media_turn_text(
        context, stream_epoch=1, start_sample=185_600, end_sample=question_end
    ) == _QUESTION
    await _device_vad(window, "question-vad-end", voiced_end, final=True)
    await _wait_until(lambda: window.provider.prepared == [_QUESTION], timeout=3.0)
    assert _QUESTION in _user_turn_texts(context)


@pytest.mark.asyncio
async def test_media_child_repeating_the_reply_after_its_echo_tail_is_a_turn(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Device soak 2026-11-01 t037: the robot offered an idiom and the child said it back 1 s after the
    reply ended.  The words are in the reply, so the post-playback echo guard dropped the turn as
    ``assistant_echo`` and the child got no answer.  Audio recorded only after the echo tail cannot hold
    the reply's own voice."""

    monkeypatch.setenv("MEDIA_PLAYBACK_FOLLOWUP_GRACE_S", "0.3")
    window = await device_media_session("repeat-after-echo-tail", during_playback=False)
    context = window.context
    set_floor(
        context.runtime,
        played_assistant_text=_ECHOED_REPLY,
        last_playback_completed_ns=time.monotonic_ns(),
    )
    context.last_playback_end_sample = 300_000
    repeated = "水滴越来越重"

    await _device_vad(window, "repeat-vad", 348_160, final=False)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="repeat",
        start_sample=346_560,
        end_sample=364_480,
        text=repeated,
    )
    await _device_vad(window, "repeat-vad-end", 361_920, final=True)

    await _wait_until(lambda: window.provider.prepared == [repeated], timeout=3.0)
    assert repeated in _user_turn_texts(context)


@pytest.mark.asyncio
async def test_media_vad_inside_the_echo_tail_margin_is_not_new_speech(
    device_media_session: Any,
) -> None:
    """Round 11, second drop in the same bridge log: a VAD edge 0.9 s after the playback ended (0.08 s
    past the boundary) is the echo's own tail, so it cannot cut the candidate it belongs to."""

    window = await device_media_session("echo-tail-vad", during_playback=False)
    context = window.context
    await _post_playback_echo_candidate(window)
    await _device_vad(window, "tail-vad", 301_280, final=False)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="later-final",
        start_sample=346_560,
        end_sample=364_480,
        text=_QUESTION,
        revision=2,
    )

    assert context.pending.turn_start_sample == 185_600


@pytest.mark.asyncio
async def test_media_vad_that_began_before_the_candidate_ended_does_not_cut_it(
    device_media_session: Any,
) -> None:
    """A VAD that began while the candidate's own audio still ran is the same sound, not new speech."""

    window = await device_media_session("echo-overlapping-vad", during_playback=False)
    context = window.context
    set_floor(context.runtime, played_assistant_text=_ECHOED_REPLY)
    context.last_playback_end_sample = 300_000
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="reply-echo",
        start_sample=185_600,
        end_sample=330_000,
        text=_ECHOED_REPLY_ASR,
    )
    await _device_vad(window, "overlapping-vad", 320_000, final=False)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="later-final",
        start_sample=346_560,
        end_sample=364_480,
        text=_QUESTION,
        revision=2,
    )

    assert context.pending.turn_start_sample == 185_600


@pytest.mark.asyncio
async def test_media_echo_candidate_ignores_a_vad_latch_of_another_stream_epoch(
    device_media_session: Any,
) -> None:
    """A VAD latch left over from a rotated stream says nothing about this stream's speech."""

    window = await device_media_session("echo-stale-latch", during_playback=False)
    context = window.context
    await _post_playback_echo_candidate(window)
    await _device_vad(window, "question-vad", 348_160, final=False)
    context.pending.active_vad_stream_epoch = 7
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="question",
        start_sample=346_560,
        end_sample=364_480,
        text=_QUESTION,
        revision=2,
    )

    assert context.pending.turn_start_sample == 185_600


@pytest.mark.asyncio
async def test_media_user_speech_candidate_still_merges_under_a_live_vad(
    device_media_session: Any,
) -> None:
    """Only the reply's own voice gives way: a candidate that is not an echo of the reply (the child
    began inside the echo window) keeps merging with the speech that follows it."""

    window = await device_media_session("candidate-is-user-speech", during_playback=False)
    context = window.context
    set_floor(context.runtime, played_assistant_text=_ECHOED_REPLY)
    context.last_playback_end_sample = 300_000
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="user-first",
        start_sample=290_000,
        end_sample=296_000,
        text="不对不对",
    )
    await _device_vad(window, "question-vad", 340_000, final=False)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="user-second",
        start_sample=341_000,
        end_sample=352_000,
        text=_QUESTION,
        revision=2,
    )

    assert context.pending.turn_start_sample == 290_000
    text = _resolve_media_turn_text(context, stream_epoch=1, start_sample=290_000, end_sample=352_000)
    assert text is not None and "不对不对" in text and _QUESTION in text


@pytest.mark.asyncio
async def test_media_turn_commit_logs_where_the_time_after_the_final_went(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TODOLIST N-14 4: round 11 could not say what the 2.7 s between the ASR final and the commit consisted
    of; one line per commit now splits it (never the text)."""

    caplog.set_level(logging.INFO)
    monkeypatch.setenv("MEDIA_PLAYBACK_FOLLOWUP_GRACE_S", "0.3")
    window = await device_media_session("commit-timing", during_playback=False)
    context = window.context
    context.last_playback_end_sample = 10_000
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="question",
        start_sample=20_000,
        end_sample=40_000,
        text=_QUESTION,
    )
    await _device_vad(window, "question-vad-end", 40_000, final=True)
    await _wait_until(lambda: window.provider.prepared == [_QUESTION], timeout=3.0)
    # The line is written at the end of the commit, after the provider already recorded the text.
    await _wait_until(lambda: bool(_commit_timing_fields(caplog)), timeout=3.0)

    [fields] = _commit_timing_fields(caplog)
    assert fields["result"] == "committed"
    assert fields["session"] == "commit-timing"
    for name in (
        "final_to_start_ms",
        "final_to_done_ms",
        "lock_ms",
        "speaker_ms",
        "close_ms",
        "prepare_ms",
        "commit_ms",
    ):
        assert fields[name].isdigit(), (name, fields)
    # No PCM went up in this scenario, so the uplink position (and with it the endpoint distance) is meaningless
    # here; the field must still be a plain integer.
    assert int(fields["since_endpoint_ms"]) <= 0
    # The commit began after the follow-up grace, so the final was already older than the grace when it started.
    assert int(fields["final_to_start_ms"]) >= 250
    assert int(fields["final_to_done_ms"]) >= int(fields["final_to_start_ms"])
    assert int(fields["commit_ms"]) >= int(fields["prepare_ms"])
    assert _QUESTION not in caplog.text.split("media turn commit timing", 1)[1].splitlines()[0]


@pytest.mark.asyncio
async def test_media_turn_commit_timing_names_the_reason_a_turn_did_not_start_a_reply(
    device_media_session: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A turn dropped as the reply's own echo is a commit too: its line carries the drop reason."""

    caplog.set_level(logging.INFO)
    window = await device_media_session("commit-timing-drop", during_playback=False)
    context = window.context
    set_floor(
        context.runtime,
        played_assistant_text=_ECHOED_REPLY,
        last_playback_completed_ns=time.monotonic_ns(),
    )
    context.last_playback_end_sample = 10_000
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="reply-echo",
        start_sample=5_000,
        end_sample=40_000,
        text=_ECHOED_REPLY_ASR,
    )
    await _device_vad(window, "echo-vad-end", 40_000, final=True)
    await _wait_until(lambda: bool(_commit_timing_fields(caplog)), timeout=3.0)

    [fields] = _commit_timing_fields(caplog)
    assert fields["result"] == "assistant_echo"
    assert window.provider.prepared == []


@pytest.mark.asyncio
async def test_media_out_of_order_final_inside_the_retained_window_still_merges(
    device_media_session: Any,
) -> None:
    """The pending-turn boundary must not cost same-turn out-of-order support.

    The retained window is one utterance with a hole in it (two clauses closer
    than the tail).  A final that arrives last but belongs inside that hole is
    still part of the same turn and has to merge.
    """

    window = await device_media_session("split-retained-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _accept_media_asr_final(
       registry,
       identity,
       sentence_id="playback-2",
       start_sample=158_560,
       end_sample=163_040,
       text="AAA",
    )
    _start_retained_utterance(window)
    await _accept_media_asr_final(
       registry,
       identity,
       sentence_id="clause-1",
       start_sample=484_480,
       end_sample=490_000,
       text="今天南京",
    )
    await _accept_media_asr_final(
       registry,
       identity,
       sentence_id="clause-2",
       start_sample=500_000,
       end_sample=508_000,
       text="还有明天呢",
       revision=2,
    )

    # Non-overlapping, inside the retained window's hole: same turn, same onset.
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="clause-1b",
        start_sample=493_000,
        end_sample=498_000,
        text="的",
        revision=3,
    )

    assert context.pending.turn_start_sample == 484_480
    text = _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=context.pending.turn_start_sample,
        end_sample=context.pending.turn_end_sample,
    )
    assert text is not None
    assert "AAA" not in text
    assert "今天南京" in text and "的" in text and "还有明天呢" in text


@pytest.mark.asyncio
async def test_textless_vad_cannot_hold_an_answered_question_open(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-28 field: background sound deferred a weather answer 17 s.

    The question was recognized and endpointed, then vad.start/vad.end kept
    cycling on sound that never produced any text; each start reopened the
    turn, so it committed only when the room went quiet. The reopened turn
    now commits the question once a bounded window passes with no new text,
    however many times it is reopened meanwhile.
    """

    monkeypatch.setattr(media_session_input, "_REOPEN_EVIDENCE_WINDOW_S", 0.2)
    window = await device_media_session("reopen-noise-session")
    context = window.context
    await _complete_previous_device_playback(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="question",
        start_sample=215_000,
        end_sample=230_000,
        text="给我讲个小故事吧",
    )
    assert context.pending.turn_endpoint_sample == 230_000
    # Background sound: starts and ends that never bring text.
    await _device_vad(window, "noise-1", 235_000, final=False)
    assert context.pending.turn_endpoint_sample is None
    await _device_vad(window, "noise-1-end", 250_000, final=True)
    await asyncio.sleep(0.05)
    await _device_vad(window, "noise-2", 252_000, final=False)
    await asyncio.sleep(0.05)
    await _device_vad(window, "noise-3", 260_000, final=False)

    await _wait_until(lambda: window.provider.prepared == ["给我讲个小故事吧"], timeout=2.0)


@pytest.mark.asyncio
async def test_reopened_turn_keeps_waiting_when_the_new_speech_has_text(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window only ends a reopen that brought no words of its own."""

    monkeypatch.setattr(media_session_input, "_REOPEN_EVIDENCE_WINDOW_S", 0.2)
    window = await device_media_session("reopen-continued-session")
    context = window.context
    await _complete_previous_device_playback(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="question-1",
        start_sample=215_000,
        end_sample=225_000,
        text="我今天有点累",
    )
    await _device_vad(window, "continued", 228_000, final=False)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="question-2",
        start_sample=229_000,
        end_sample=240_000,
        text="想早点休息",
        revision=2,
    )
    await asyncio.sleep(0.35)
    # Past the window, the still-open speech that did bring text has not
    # been cut at the first endpoint.
    assert window.provider.prepared == []
    assert context.pending.turn_end_sample == 240_000
    await _device_vad(window, "continued-end", 240_000, final=True)
    await _wait_until(lambda: len(window.provider.prepared) == 1, timeout=3.0)
    prepared = window.provider.prepared[0]
    assert "我今天有点累" in prepared and "想早点休息" in prepared


@pytest.mark.asyncio
async def test_reopen_window_never_commits_a_newer_turn_at_the_old_endpoint(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_session_input, "_REOPEN_EVIDENCE_WINDOW_S", 60.0)
    window = await device_media_session("reopen-newer-turn-session")
    context = window.context
    await _complete_previous_device_playback(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="question",
        start_sample=215_000,
        end_sample=230_000,
        text="给我讲个小故事吧",
    )
    await _device_vad(window, "noise", 235_000, final=False)
    handle = context.pending.reopen_evidence_handle
    assert handle is not None and context.pending.reopen_evidence_endpoint == 230_000
    handle.cancel()
    # The logical turn changed meanwhile (committed, then a new one opened).
    context.pending.turn_start_sample = 300_000
    window.registry._expire_reopen_evidence_window(
        window.identity.session_id, window.identity.stream_epoch, 230_000
    )
    assert context.pending.turn_endpoint_sample is None
    assert context.pending.reopen_evidence_endpoint is None
    await asyncio.sleep(0.05)
    assert window.provider.prepared == []


@pytest.mark.asyncio
async def test_media_playback_overlap_split_is_blocked_by_a_vad_anchored_turn(
    device_media_session: Any,
) -> None:
    """A VAD-admitted candidate keeps its onset; dropping the anchor splits it.

    The positive control is the point: with the anchor removed the very same
    accepted final and gap must split the window, so the blocked assertion
    cannot be passing because an unrelated gate rejected the final first.
    """

    window = await device_media_session("split-vad-anchor")
    await _feed_playback_window_finals(window)
    _start_retained_utterance(window)
    context = window.context
    assert context.pending.pending_turn_playback_overlap is True
    assert context.pending.turn_start_sample == 158_560
    assert context.pending.turn_end_sample == _ABANDONED_WINDOW_END

    context.pending.active_vad_start_sample = _ABANDONED_WINDOW_END
    anchored = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="far-1",
        start_sample=400_000,
        end_sample=410_000,
        text="下午一起出发吗",
    )
    assert anchored.accepted is not None
    assert context.pending.turn_start_sample == 158_560

    context.pending.active_vad_start_sample = None
    split = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="far-2",
        start_sample=470_000,
        end_sample=480_000,
        text="下午一起出发吗",
        revision=2,
    )
    assert split.accepted is not None
    assert context.pending.turn_start_sample == 470_000


@pytest.mark.parametrize(
    ("late_text", "pin_attribute"),
    (
        ("今天几号", "clock_fact_endpoint_pinned"),
        ("明天南京天气怎么样", "live_query_endpoint_pinned"),
        ("再见", "conversation_close_endpoint_pinned"),
    ),
)
@pytest.mark.asyncio
async def test_device_abandoned_window_final_cannot_pin_pollute_or_close(
    device_media_session: Any,
    late_text: str,
    pin_attribute: str,
) -> None:
    """Audio for the closed candidate window may not pin, pollute or close."""

    from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

    window = await _open_retained_window(
        device_media_session,
        f"device-abandoned-{pin_attribute}",
    )
    late = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="late-old-1",
        start_sample=352_000,
        end_sample=360_000,
        text=late_text,
    )
    assert late.accepted is None
    assert late.reason is ASRDecisionReason.INTERVAL_CONFLICT
    assert getattr(window.context.pending, pin_attribute) is None

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=508_800)
    assert window.provider.prepared == ["下午一起出发吗"]
    assert _user_turn_texts(window.context) == ["下午一起出发吗"]
    assert window.context.standby_requested is False


@pytest.mark.asyncio
async def test_device_abandoned_window_partial_cannot_pin_or_pollute(
    device_media_session: Any,
) -> None:
    """A late partial for the closed window may not seed a semantic endpoint."""

    window = await _open_retained_window(device_media_session, "device-abandoned-partial")
    late = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="late-partial-1",
        start_sample=352_000,
        end_sample=360_000,
        text="再见",
        is_final=False,
    )
    assert late.accepted is None
    assert window.context.pending.conversation_close_partial_text is None
    assert window.context.pending.conversation_close_endpoint_pinned is None

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=508_800)
    assert window.provider.prepared == ["下午一起出发吗"]


@pytest.mark.asyncio
async def test_device_old_vad_edge_cannot_pull_the_retained_onset_back(
    device_media_session: Any,
) -> None:
    """A late VAD edge for the closed window is not admitted and cannot re-anchor."""

    window = await _open_retained_window(device_media_session, "device-old-vad")
    context = window.context
    for segment_id, revision, final, sample, voiced_end in (
        ("stale-vad-start", 1, False, 317_280, None),
        ("stale-vad-end", 2, True, 332_640, 332_640),
    ):
        await window.registry.on_speech_segment(
            window.session,
            SpeechSegment(
                session_id=window.identity.session_id,
                stream_epoch=window.identity.stream_epoch,
                provider_task_epoch=8,
                segment_id=segment_id,
                revision=revision,
                kind=SegmentKind.VAD,
                capture_start_sample=sample,
                capture_end_sample=sample + 1,
                final=final,
                voiced_end_sample=voiced_end,
            ),
        )

    assert context.pending.active_vad_start_sample is None
    assert context.pending.turn_start_sample == 484_480
    assert context.pending.turn_endpoint_sample is None

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=508_800)
    assert window.provider.prepared == ["下午一起出发吗"]


@pytest.mark.asyncio
async def test_device_current_vad_still_extends_the_retained_window(
    device_media_session: Any,
) -> None:
    """The current utterance's VAD edges still open and close the retained window."""

    window = await _open_retained_window(
        device_media_session,
        "device-current-vad",
        retained_end=490_000,
    )
    context = window.context
    await window.registry.on_speech_segment(
        window.session,
        SpeechSegment(
            session_id=window.identity.session_id,
            stream_epoch=window.identity.stream_epoch,
            provider_task_epoch=8,
            segment_id="current-vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=500_000,
            capture_end_sample=500_001,
        ),
    )
    assert context.pending.active_vad_start_sample == 500_000
    await _accept_media_asr_final(
       window.registry,
       window.identity,
       sentence_id="clause-2",
       start_sample=500_000,
       end_sample=508_000,
       text="还有明天呢",
       revision=2,
    )
    assert context.pending.turn_start_sample == 484_480

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=508_000)
    text = window.provider.prepared[-1]
    assert "下午一起出发吗" in text and "还有明天呢" in text


@pytest.mark.asyncio
async def test_device_pinned_endpoint_is_never_split(device_media_session: Any) -> None:
    """An already endpointed turn keeps its range while it is committing."""

    window = await _open_retained_window(
        device_media_session,
        "device-pinned-endpoint",
        retained_end=490_000,
    )
    context = window.context
    context.pending.pending_turn_playback_overlap = True
    context.pending.turn_endpoint_sample = 490_000
    context.pending.turn_retire_sample = 490_000
    context.pending.turn_endpoint_grace_deadline = time.monotonic() + 60.0

    far = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="far-1",
        start_sample=532_000,
        end_sample=540_000,
        text="还有明天呢",
        revision=2,
    )
    assert far.accepted is not None
    assert context.pending.turn_start_sample == 484_480
    assert context.pending.turn_endpoint_sample == 490_000

    # Positive control: the accepted final and its 42 000-sample gap above the
    # candidate tail do split once the endpoint is gone, so the endpoint, not an
    # unrelated gate, was what blocked the earlier final.
    context.pending.turn_endpoint_sample = None
    context.pending.turn_retire_sample = None
    split = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="far-2",
        start_sample=600_000,
        end_sample=610_000,
        text="还有明天呢",
        revision=3,
    )
    assert split.accepted is not None
    assert context.pending.turn_start_sample == 600_000


@pytest.mark.asyncio
async def test_device_final_crossing_the_retained_onset_fails_closed(
    device_media_session: Any,
) -> None:
    """A revision that reaches back across the boundary may not carry old text."""

    from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

    window = await _open_retained_window(
        device_media_session,
        "device-boundary-revision",
        retained_end=490_000,
    )
    crossing = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="plain-1",
        start_sample=400_000,
        end_sample=495_000,
        text="AAA 下午一起出发吗",
        revision=2,
    )
    assert crossing.accepted is None
    assert crossing.reason is ASRDecisionReason.INTERVAL_CONFLICT

    text = _resolve_media_turn_text(
        window.context,
        stream_epoch=1,
        start_sample=window.context.pending.turn_start_sample,
        end_sample=window.context.pending.turn_end_sample,
    )
    assert text == "下午一起出发吗"

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=490_000)
    assert window.provider.prepared == ["下午一起出发吗"]


@pytest.mark.asyncio
async def test_device_partial_only_overlap_window_cannot_pollute_the_next_commit(
    device_media_session: Any,
) -> None:
    """A candidate window built only from partials still owns no committed text."""

    window = await device_media_session("device-partial-overlap")
    partial = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="playback-partial-6",
        start_sample=258_880,
        end_sample=264_640,
        text="CCCCCCCCCCC",
        is_final=False,
    )
    assert partial.accepted is not None
    _start_retained_utterance(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="plain-1",
        start_sample=484_480,
        end_sample=508_800,
        text="下午一起出发吗",
    )
    assert window.context.pending.turn_start_sample == 484_480

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=508_800)
    assert window.provider.prepared == ["下午一起出发吗"]
