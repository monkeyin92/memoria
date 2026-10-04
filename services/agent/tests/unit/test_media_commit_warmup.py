"""A turn commit that waits for the end-of-speech grace has what it needs ready when the grace is over.

Round 14 (2026-10-04): a commit that follows a device VAD end took 2.9 s from the ASR final to the end of the
commit (p50, 7 turns), against 1.3 s for the playback follow-up flow (50 turns), because the follow-up flow
alone started what the commit waits for during its grace (TODOLIST N-14 7 and 8): the conversation-close
verdict (0.5-0.9 s), the live-lookup verdict (0.6 s, and again when the commit's sentence is the merge of
several finals) and the bound person's memory (0.4 s).  In the VAD flow the ASR final arrives while the VAD end
is still being finalized, so the endpoint is set a few milliseconds later, in the same step, and the evaluation
task the final scheduled finds it set and returns without asking anything.  The head start now begins where the
commit is scheduled, whoever scheduled it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session_commit import _resolve_media_turn_text
from services.agent.src.voice_core.speech_timeline import ASRResult
from services.agent.tests.unit.media_session_support import (
    _accept_media_asr_decision,
    _accept_media_asr_final,
    _device_vad,
    _finish_previous_reply,
    _wait_until,
)
from services.agent.tests.unit.runtime_state_helpers import set_floor

_GRACE_S = 0.5
_QUESTION = "明天我们去动物园好不好"
_STRAY = "嗯嗯"
_VOICED_END = 60_000


class _Verdict:
    """A semantic classifier of a test: it records what it is asked and answers when the test says so."""

    def __init__(self, answer: bool = False) -> None:
        self.answer = answer
        self.calls: list[str] = []
        self.release = asyncio.Event()

    async def __call__(self, text: str) -> bool:
        self.calls.append(text)
        await self.release.wait()
        return self.answer


async def _let_tasks_run() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def _final(
    sentence_id: str, start: int, end: int, text: str, revision: int = 1, task_epoch: int = 3
) -> ASRResult:
    return ASRResult(
        task_epoch=task_epoch,
        sentence_id=sentence_id,
        revision=revision,
        capture_start_sample=start,
        capture_end_sample=end,
        text=text,
        is_final=True,
    )


async def _open(device_media_session: Any, session_id: str, grace_s: float = _GRACE_S) -> Any:
    window = await device_media_session(session_id, during_playback=False)
    window.close_verdict = _Verdict()
    window.live_verdict = _Verdict()
    runtime = window.context.runtime
    runtime.set_conversation_close_semantic_resolver(window.close_verdict)
    runtime.set_live_lookup_semantic_resolver(window.live_verdict)
    registry = window.registry
    registry.turn_endpoint_grace_s = grace_s
    registry.turn_endpoint_min_grace_s = 0.0
    registry.turn_endpoint_max_grace_s = grace_s
    return window


def _release(window: SimpleNamespace) -> None:
    window.close_verdict.release.set()
    window.live_verdict.release.set()


async def _end_the_utterance(
    window: SimpleNamespace,
    *finals: ASRResult,
    vad_start: int,
    voiced_end: int = _VOICED_END,
) -> None:
    """The device VAD ends and the ASR returns its finals while that end is being finalized.

    This is the order of every VAD-flow commit in the round-14 logs: the finals reach the session before the
    endpoint exists, and the same handler sets the endpoint right after them.
    """

    async def finalize(_identity: SessionIdentity) -> Sequence[ASRResult]:
        return finals

    window.provider.finalize_speech_segment = finalize
    await _device_vad(window, f"vad-{vad_start}", vad_start, final=False)
    window.context.asr.last_sent_sample = voiced_end
    await _device_vad(window, f"vad-end-{vad_start}", voiced_end, final=True)


def _turn_text(window: SimpleNamespace, start: int, end: int) -> str:
    text = _resolve_media_turn_text(
        window.context, stream_epoch=window.identity.stream_epoch, start_sample=start, end_sample=end
    )
    assert text
    return text


@pytest.mark.asyncio
async def test_a_vad_flow_commit_asks_the_close_verdict_before_the_grace_runs_out(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "vad-warm-close")

    await _end_the_utterance(window, _final("q-1", 40_000, 58_000, _QUESTION), vad_start=40_000)
    await _let_tasks_run()

    assert window.context.pending.turn_endpoint_sample == _VOICED_END
    assert window.close_verdict.calls == [_QUESTION]  # asked while the grace still runs
    assert window.provider.prepared == []  # the commit has not begun
    _release(window)
    await _wait_until(lambda: window.provider.prepared == [_QUESTION], timeout=3.0)
    assert window.close_verdict.calls == [_QUESTION]  # and the commit used that call


@pytest.mark.asyncio
async def test_a_vad_flow_commit_names_its_sentence_to_the_provider_before_the_grace_runs_out(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "vad-warm-provider")

    await _end_the_utterance(window, _final("q-1", 40_000, 58_000, _QUESTION), vad_start=40_000)

    assert window.provider.warmed == [_QUESTION]
    assert window.provider.prepared == []
    _release(window)
    await _wait_until(lambda: window.provider.prepared == [_QUESTION], timeout=3.0)
    assert window.provider.warmed == [_QUESTION]  # once, however often the commit was rescheduled


@pytest.mark.asyncio
async def test_a_stray_final_in_the_turn_makes_the_verdicts_ask_about_the_sentence_the_commit_reads(
    device_media_session: Any,
) -> None:
    """Round 14, t002: a 2-character final earlier in the turn made the commit's sentence 11 characters.

    The live-lookup verdict started at the last final was keyed on its 8 characters, so the commit asked
    the cloud classifier a second time (0.65 s) and then fetched the memory (0.43 s) after it.
    """

    window = await _open(device_media_session, "vad-warm-merged")
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="stray-1",
        start_sample=20_000,
        end_sample=22_000,
        text=_STRAY,
    )

    await _end_the_utterance(window, _final("q-1", 40_000, 58_000, _QUESTION), vad_start=40_000)
    await _let_tasks_run()

    committed = _turn_text(window, 20_000, _VOICED_END)
    assert _STRAY in committed and _QUESTION in committed and committed != _QUESTION
    assert committed in window.close_verdict.calls
    assert committed in window.live_verdict.calls
    assert window.provider.warmed == [committed]
    _release(window)
    await _wait_until(lambda: window.provider.prepared == [committed], timeout=3.0)
    assert window.close_verdict.calls.count(committed) == 1
    assert window.live_verdict.calls.count(committed) == 1  # the commit joined it


@pytest.mark.asyncio
async def test_a_partial_after_the_last_final_is_part_of_the_sentence_that_is_warmed(
    device_media_session: Any,
) -> None:
    """The commit reads the timeline up to the endpoint, so a partial that follows the last final counts."""

    window = await _open(device_media_session, "vad-warm-partial")
    await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="p-1",
        start_sample=50_000,
        end_sample=58_000,
        text="动物园好不好",
        is_final=False,
    )

    await _end_the_utterance(window, _final("q-1", 40_000, 50_000, "明天我们去"), vad_start=40_000)
    await _let_tasks_run()

    committed = _turn_text(window, 40_000, _VOICED_END)
    assert committed == "明天我们去 动物园好不好"
    assert window.provider.warmed == [committed]
    assert committed in window.close_verdict.calls


@pytest.mark.asyncio
async def test_a_final_that_extends_the_turn_inside_the_grace_warms_the_longer_sentence(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "vad-warm-grows")
    await _end_the_utterance(window, _final("q-1", 40_000, 58_000, "明天我们去"), vad_start=40_000)
    await _let_tasks_run()
    assert window.provider.warmed == ["明天我们去"]

    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="q-2",
        start_sample=58_000,
        end_sample=_VOICED_END,
        text="动物园好不好",
        revision=2,
    )
    await _let_tasks_run()

    committed = _turn_text(window, 40_000, _VOICED_END)
    assert committed != "明天我们去" and "动物园好不好" in committed
    assert window.provider.warmed == ["明天我们去", committed]
    assert committed in window.close_verdict.calls
    _release(window)
    await _wait_until(lambda: window.provider.prepared == [committed], timeout=3.0)


@pytest.mark.asyncio
async def test_a_late_final_after_the_vad_end_starts_the_verdicts_with_the_commit(
    device_media_session: Any,
) -> None:
    """The VAD end came first and its grace is spent: the commit starts the moment the final lands.

    There is no grace left to hide the waits in, but the three of them (close verdict, live-lookup verdict,
    memory) can overlap instead of following one another.  Nothing is warmed before there is text.
    """

    window = await _open(device_media_session, "vad-warm-late-final", grace_s=0.1)
    await _end_the_utterance(window, vad_start=40_000)
    await asyncio.sleep(0.25)  # the grace is over; the commit is waiting for the ASR to cover the endpoint
    assert window.context.pending.turn_endpoint_sample == _VOICED_END
    assert window.provider.warmed == []
    assert window.close_verdict.calls == [] and window.live_verdict.calls == []

    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="late-1",
        start_sample=40_000,
        end_sample=_VOICED_END,
        text=_QUESTION,
    )
    await _let_tasks_run()

    assert window.provider.warmed == [_QUESTION]
    assert _QUESTION in window.close_verdict.calls
    assert _QUESTION in window.live_verdict.calls
    _release(window)
    await _wait_until(lambda: window.provider.prepared == [_QUESTION], timeout=3.0)
    assert window.close_verdict.calls.count(_QUESTION) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("spoken", ["再见", "今天星期几", "今天上海天气怎么样"])
async def test_a_turn_that_pins_its_own_endpoint_is_not_warmed(
    device_media_session: Any, spoken: str
) -> None:
    """A goodbye ends the session, a clock fact is answered locally and a live query has its own route."""

    window = await _open(device_media_session, "vad-warm-pinned")

    await _end_the_utterance(window, _final("pinned-1", 40_000, 52_000, spoken), vad_start=40_000)
    await _let_tasks_run()

    assert window.provider.warmed == []


async def _seed_endpoint(window: SimpleNamespace, endpoint: int = 58_000) -> None:
    """A pending turn whose final is on the timeline and whose endpoint stands, with nothing scheduled."""

    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="q-1",
        start_sample=40_000,
        end_sample=58_000,
        text=_QUESTION,
    )
    pending = window.context.pending
    pending.turn_endpoint_sample = endpoint
    pending.turn_retire_sample = endpoint


@pytest.mark.asyncio
async def test_an_endpoint_with_no_text_in_its_range_warms_nothing(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "vad-warm-no-text")
    pending = window.context.pending
    pending.turn_start_sample = 40_000
    pending.turn_end_sample = _VOICED_END
    pending.turn_endpoint_sample = _VOICED_END
    pending.turn_retire_sample = _VOICED_END

    window.registry._warm_endpoint_commit(window.context)

    assert window.provider.warmed == []
    assert window.close_verdict.calls == [] and window.live_verdict.calls == []


@pytest.mark.asyncio
async def test_a_pending_turn_without_a_start_or_an_endpoint_warms_nothing(
    device_media_session: Any,
) -> None:
    window = await _open(device_media_session, "vad-warm-incomplete")
    await _seed_endpoint(window)
    pending = window.context.pending

    pending.turn_start_sample = None
    window.registry._warm_endpoint_commit(window.context)
    pending.turn_start_sample = 40_000
    pending.turn_endpoint_sample = None
    window.registry._warm_endpoint_commit(window.context)

    assert window.provider.warmed == []


@pytest.mark.asyncio
async def test_a_range_the_commit_would_refuse_warms_nothing_and_says_nothing(
    device_media_session: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The commit answers an inverted range with ``invalid_pending_media_turn``; the timeline raises on it."""

    window = await _open(device_media_session, "vad-warm-inverted")
    await _seed_endpoint(window)
    window.context.pending.turn_start_sample = 58_000  # the endpoint is the start

    with caplog.at_level(logging.WARNING):
        window.registry._warm_endpoint_commit(window.context)

    assert window.provider.warmed == []
    assert not [record for record in caplog.records if "warm-up failed" in record.message]


@pytest.mark.asyncio
async def test_a_timeline_that_fails_cannot_break_the_scheduling_of_the_commit(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    window = await _open(device_media_session, "vad-warm-timeline-failure")
    await _seed_endpoint(window)

    def fail(_timeline: object, **_range: int) -> str:
        raise RuntimeError("timeline unavailable")

    monkeypatch.setattr(type(window.context.runtime.speech_timeline), "projected_text", fail)
    with caplog.at_level(logging.WARNING):
        window.registry._schedule_turn_commit(window.context)

    assert window.context.turn_endpoint_task is not None  # the commit is scheduled all the same
    assert window.provider.warmed == []
    assert len([record for record in caplog.records if "media commit warm-up failed" in record.message]) == 1


@pytest.mark.asyncio
async def test_nothing_is_warmed_while_a_reply_is_in_flight(device_media_session: Any) -> None:
    """The reply's own echo is on the timeline then: it must not start a verdict or a memory fetch."""

    window = await _open(device_media_session, "vad-warm-reply-in-flight")
    await _seed_endpoint(window)

    set_floor(window.context.runtime, assistant_speaking=True)
    window.registry._warm_endpoint_commit(window.context)
    assert window.provider.warmed == []

    set_floor(window.context.runtime, assistant_speaking=False)
    window.registry._warm_endpoint_commit(window.context)
    assert window.provider.warmed == [_QUESTION]


@pytest.mark.asyncio
async def test_nothing_is_warmed_before_the_asr_covers_the_endpoint(
    device_media_session: Any,
) -> None:
    """The commit waits for a late final then, and the text it will read is not there yet."""

    window = await _open(device_media_session, "vad-warm-uncovered")
    await _seed_endpoint(window, endpoint=95_000)  # 37 000 samples (2.3 s) past the last final

    window.registry._warm_endpoint_commit(window.context)
    assert window.provider.warmed == []

    window.context.pending.turn_endpoint_sample = 58_000
    window.registry._warm_endpoint_commit(window.context)
    assert window.provider.warmed == [_QUESTION]


@pytest.mark.asyncio
async def test_a_commit_that_is_rescheduled_is_warmed_once(device_media_session: Any) -> None:
    window = await _open(device_media_session, "vad-warm-once")
    await _seed_endpoint(window)

    for _ in range(3):
        window.registry._warm_endpoint_commit(window.context)
    await _let_tasks_run()

    assert window.provider.warmed == [_QUESTION]
    assert window.close_verdict.calls.count(_QUESTION) == 1


@pytest.mark.asyncio
async def test_the_next_turn_with_the_same_words_is_warmed_again(device_media_session: Any) -> None:
    window = await _open(device_media_session, "vad-warm-repeat")
    await _end_the_utterance(window, _final("q-1", 40_000, 58_000, _QUESTION), vad_start=40_000)
    _release(window)
    await _wait_until(lambda: window.provider.prepared == [_QUESTION], timeout=3.0)
    # The commit is over once its task is gone (that is when the pending turn is cleared), and the reply has played out.
    await _wait_until(lambda: window.context.turn_endpoint_task is None, timeout=3.0)
    _finish_previous_reply(window.context)
    await _wait_until(lambda: not window.registry._reply_in_flight(window.context), timeout=3.0)
    assert window.provider.warmed == [_QUESTION]

    await _end_the_utterance(  # the provider's next ASR task
        window,
        _final("q-2", 80_000, 98_000, _QUESTION, task_epoch=4),
        vad_start=80_000,
        voiced_end=100_000,
    )

    assert window.provider.warmed == [_QUESTION, _QUESTION]


@pytest.mark.asyncio
async def test_a_failed_warm_up_does_not_stop_the_commit(
    device_media_session: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    window = await _open(device_media_session, "vad-warm-failure")

    def refuse(_identity: SessionIdentity, _text: str) -> None:
        raise RuntimeError("control plane down")

    window.provider.warm_committed_turn = refuse
    with caplog.at_level(logging.WARNING):
        await _end_the_utterance(window, _final("q-1", 40_000, 58_000, _QUESTION), vad_start=40_000)

    assert window.context.pending.turn_endpoint_sample == _VOICED_END
    window.registry._warm_endpoint_commit(window.context)  # a reschedule does not try again
    _release(window)
    await _wait_until(lambda: window.provider.prepared == [_QUESTION], timeout=3.0)
    failures = [record for record in caplog.records if "media commit warm-up failed" in record.message]
    assert len(failures) == 1  # once, not at every reschedule
    assert _QUESTION not in failures[0].getMessage()  # the child's words stay out of the log
