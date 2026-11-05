"""Media session: playback follow-up endpoints and post-playback finals."""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.voice_core.asr_stream_supervisor import ASRAcceptDecision, ASRDecisionReason
from services.agent.src.voice_core.media_protocol import (
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.media_session_commit import _resolve_media_turn_text
from services.agent.src.voice_core.media_session_pending_turn import PendingTurn
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.media_session_support import (
    _accept_media_asr_decision,
    _accept_media_asr_final,
    _commit_timing_fields,
    _complete_previous_device_playback,
    _device_identity,
    _device_vad,
    _user_turn_texts,
    _wait_until,
)
from services.agent.tests.unit.runtime_state_helpers import set_floor


@pytest.mark.asyncio
async def test_media_playback_followup_endpoints_without_vad_edge(
    device_media_session: Any,
) -> None:
    """Run 20260921 window-a: a follow-up after playback must not wait ~20 s.

    The realtime chain recognized the follow-up, but the device VAD stayed
    active across the playback echo and the offline paragraph was rejected
    for straddling the committed boundary, so nothing endpointed the turn
    until the next vad.start.  A final that begins wholly after the playback
    boundary now owns the turn and pins the endpoint itself, with no vad.end
    and no offline paragraph ever arriving.
    """

    window = await device_media_session("followup-early-endpoint-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    # Boundary = last accepted evidence end (190_000) + echo-tail margin.
    assert context.last_playback_end_sample == 202_800

    # The echo-holdover VAD stays active across the boundary; it must not
    # keep suppressing the split or the follow-up endpoint.
    context.pending.active_vad_start_sample = 195_000
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=230_000,
        text="后天呢",
    )

    assert context.pending.turn_start_sample == 215_000
    assert context.pending.turn_endpoint_sample == 230_000
    await _wait_until(lambda: window.provider.prepared == ["后天呢"], timeout=3.0)
    assert "后天呢" in _user_turn_texts(context)
    # The abandoned echo transcript is evicted, not merely out of range.
    assert not _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=160_000,
        end_sample=190_000,
    )


class _CloseVerdict:
    """The conversation-close classifier of a test: it answers when the test says so."""

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


@pytest.mark.asyncio
async def test_media_playback_followup_starts_the_close_verdict_before_the_grace_runs_out(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 12 (2026-10-03): the conversation-close verdict took p50 403 ms of the commit, after the grace.

    The ASR final schedules an evaluation task for it, but the same call goes on to pin the follow-up
    endpoint, and by the time the task ran it saw the pinned endpoint and returned without asking the
    classifier: the commit asked, 1.2 s later, and the child waited for the answer.  The verdict now starts
    when the endpoint is pinned, so it is ready when the grace is over.
    """

    monkeypatch.setenv("MEDIA_PLAYBACK_FOLLOWUP_GRACE_S", "0.6")
    window = await device_media_session("followup-close-verdict-warm")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    verdict = _CloseVerdict(False)
    context.runtime.set_conversation_close_semantic_resolver(verdict)

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=230_000,
        text="后天呢",
    )
    assert context.pending.turn_endpoint_sample == 230_000
    await _let_tasks_run()

    assert verdict.calls == ["后天呢"]  # asked while the grace still runs
    assert window.provider.prepared == []  # the commit has not begun
    verdict.release.set()
    await _wait_until(lambda: window.provider.prepared == ["后天呢"], timeout=3.0)
    assert verdict.calls == ["后天呢"]  # and the commit used that call instead of asking again


@pytest.mark.asyncio
async def test_media_playback_followup_that_grows_asks_about_the_whole_sentence(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second final that extends the endpoint changes the sentence the commit will ask about."""

    monkeypatch.setenv("MEDIA_PLAYBACK_FOLLOWUP_GRACE_S", "0.6")
    window = await device_media_session("followup-close-verdict-grows")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    verdict = _CloseVerdict(False)
    context.runtime.set_conversation_close_semantic_resolver(verdict)

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=225_000,
        text="后天",
    )
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-2",
        start_sample=226_000,
        end_sample=232_000,
        text="天气呢",
        revision=2,
    )
    await _let_tasks_run()
    assert context.pending.playback_followup_endpoint_sample == 232_000
    committed_text = _resolve_media_turn_text(
        context, stream_epoch=1, start_sample=215_000, end_sample=232_000
    )
    assert committed_text is not None and "后天" in committed_text and "天气呢" in committed_text

    assert committed_text in verdict.calls  # the grown sentence is asked about before the commit
    assert window.provider.warmed[-1] == committed_text  # and announced to the provider
    verdict.release.set()
    await _wait_until(lambda: window.provider.prepared == [committed_text], timeout=3.0)
    assert verdict.calls.count(committed_text) == 1


@pytest.mark.asyncio
async def test_media_playback_followup_names_the_sentence_to_the_provider_before_the_commit(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider starts what its preparation waits for (the bound person's memory) inside the grace."""

    monkeypatch.setenv("MEDIA_PLAYBACK_FOLLOWUP_GRACE_S", "0.6")
    window = await device_media_session("followup-warm-provider")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=230_000,
        text="后天呢",
    )

    assert window.provider.warmed == ["后天呢"]
    assert window.provider.prepared == []  # the commit has not begun
    await _wait_until(lambda: window.provider.prepared == ["后天呢"], timeout=3.0)
    assert window.provider.warmed == ["后天呢"]
    await _wait_until(lambda: context.pending.turn_endpoint_sample is None, timeout=3.0)


@pytest.mark.asyncio
async def test_media_incomplete_followup_waits_for_second_half_before_commit(
    device_media_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Demo blocker: one child sentence must not become two server input turns.

    Field path: post-playback FunASR finals 「我想听」then 「恐龙的故事呀」, with the
    echo-holdover VAD still active.  The first final used to commit alone after grace;
    the second half then overlapped the reply, hit ``pending turn split after reply``,
    and died on ASR tail timeout (5 spoken sentences → 7 input turns).  Incomplete
    follow-ups now hold until the next final (or the incomplete-hold budget).
    """

    monkeypatch.setenv("MEDIA_PLAYBACK_FOLLOWUP_GRACE_S", "0.3")
    window = await device_media_session("followup-incomplete-hold")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.pending.active_vad_start_sample = 195_000

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="half-1",
        start_sample=215_000,
        end_sample=225_000,
        text="我想听",
    )
    assert context.pending.playback_followup_endpoint_sample == 225_000
    await asyncio.sleep(0.45)
    assert window.provider.prepared == []
    assert context.pending.turn_endpoint_sample == 225_000

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="half-2",
        start_sample=226_000,
        end_sample=245_000,
        text="恐龙的故事呀",
        revision=2,
    )
    await _wait_until(
        lambda: any("我想听" in text and "恐龙" in text for text in window.provider.prepared),
        timeout=3.0,
    )
    followups = [text for text in _user_turn_texts(context) if "我想听" in text or "恐龙" in text]
    assert followups == ["我想听 恐龙的故事呀"]
    assert window.provider.prepared.count(followups[0]) == 1


@pytest.mark.asyncio
async def test_media_playback_echo_tail_is_not_announced_to_the_provider(
    device_media_session: Any,
) -> None:
    """Only speech that pins the follow-up endpoint is warmed: the reply's own echo costs no call."""

    window = await device_media_session("followup-warm-echo-tail")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.pending.active_vad_start_sample = 195_000

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="tail-1",
        start_sample=195_000,
        end_sample=208_000,
        text="多云转晴",
    )
    assert context.pending.turn_endpoint_sample is None
    assert window.provider.warmed == []

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=230_000,
        text="后天呢",
    )
    assert window.provider.warmed == ["后天呢"]
    await _wait_until(lambda: window.provider.prepared == ["后天呢"], timeout=3.0)


@pytest.mark.asyncio
async def test_media_playback_followup_keeps_its_endpoint_when_the_warm_up_fails(
    device_media_session: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    window = await device_media_session("followup-warm-failure")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)

    def refuse(_identity: SessionIdentity, _text: str) -> None:
        raise RuntimeError("control plane down")

    window.provider.warm_committed_turn = refuse
    with caplog.at_level(logging.WARNING):
        await _accept_media_asr_final(
            registry,
            identity,
            sentence_id="followup-1",
            start_sample=215_000,
            end_sample=230_000,
            text="后天呢",
        )

    assert context.pending.turn_endpoint_sample == 230_000
    await _wait_until(lambda: window.provider.prepared == ["后天呢"], timeout=3.0)
    assert any("media commit warm-up failed" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_media_playback_followup_echo_tail_cannot_endpoint(
    device_media_session: Any,
) -> None:
    """A final that begins before the playback boundary may still be echo.

    The supervisor accepts interior finals on interval grounds, so the echo
    guard has to fail them closed on its own: no early endpoint, and the
    later real follow-up still owns the turn on exclusively its samples.
    """

    window = await device_media_session("followup-echo-guard-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.pending.active_vad_start_sample = 195_000

    tail = await _accept_media_asr_decision(
        registry,
        identity,
        sentence_id="tail-1",
        start_sample=195_000,
        end_sample=208_000,
        text="多云转晴",
    )
    assert tail.accepted is not None, tail.reason
    assert context.pending.turn_endpoint_sample is None
    assert window.provider.prepared == []

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=230_000,
        text="后天呢",
    )
    assert context.pending.turn_start_sample == 215_000
    await _wait_until(lambda: window.provider.prepared == ["后天呢"], timeout=3.0)
    assert "多云转晴" not in window.provider.prepared[0]


_PLAYED_WEATHER_REPLY = "明天上海晴转多云，最高气温二十六度。"


@pytest.mark.asyncio
async def test_media_playback_followup_begun_before_playback_end_endpoints(
    device_media_session: Any,
) -> None:
    """Field 2026-09-29 session 6b38ba46: a question begun as the reply ended.

    The owner started the next question ~0.5 s before playback ended and
    finished ~4 s later.  Its 11-character final began before the playback
    boundary, so the echo guard refused to endpoint it, and the device VAD,
    suppressed during playback, never fired: nothing happened for ~9 s until
    the owner repeated it, and the two finals then committed as one
    23-character turn.  Speech that is not the reply's own text and runs well
    past the boundary owns a follow-up turn on its own samples.
    """

    window = await device_media_session("followup-straddles-boundary-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.output.assistant_text = _PLAYED_WEATHER_REPLY
    assert context.last_playback_end_sample == 202_800

    question = "那我明天出门要带伞吗"
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=190_400,
        end_sample=254_400,
        text=question,
    )

    assert context.pending.turn_start_sample == 190_400
    assert context.pending.turn_endpoint_sample == 254_400
    assert context.pending.playback_followup_endpoint_sample == 254_400
    await _wait_until(lambda: window.provider.prepared == [question], timeout=3.0)
    assert _user_turn_texts(context)[-1] == question
    # The echo candidate held from the playback window is evicted, not merged.
    assert not _resolve_media_turn_text(
        context,
        stream_epoch=1,
        start_sample=160_000,
        end_sample=190_000,
    )


@pytest.mark.asyncio
async def test_media_playback_followup_straddling_reply_echo_cannot_endpoint(
    device_media_session: Any,
) -> None:
    """The reply's own words past the boundary stay behind the echo guard."""

    window = await device_media_session("followup-straddling-echo-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.output.assistant_text = _PLAYED_WEATHER_REPLY

    tail = await _accept_media_asr_decision(
        registry,
        identity,
        sentence_id="tail-1",
        start_sample=190_400,
        end_sample=225_000,
        text="最高气温二十六度",
    )
    assert tail.accepted is not None, tail.reason
    assert context.pending.turn_endpoint_sample is None
    assert window.provider.prepared == []

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=230_000,
        end_sample=245_000,
        text="后天呢",
    )
    assert context.pending.turn_start_sample == 230_000
    await _wait_until(lambda: window.provider.prepared == ["后天呢"], timeout=3.0)


@pytest.mark.asyncio
async def test_media_playback_followup_survives_late_rejected_result(
    device_media_session: Any,
) -> None:
    """A rejection between arming and commit must not cancel the endpoint.

    Window-a lost the follow-ups partly because the only authoritative
    confirmation (the offline paragraph) was rejected and nothing re-armed
    the endpoint.  The armed follow-up endpoint now stands on the accepted
    realtime final alone.
    """

    window = await device_media_session("followup-rejection-tolerant-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.pending.active_vad_start_sample = 195_000
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=230_000,
        text="后天呢",
    )
    assert context.pending.turn_endpoint_sample == 230_000

    # Interior cross-sentence overlap: rejected by the supervisor, the same
    # class of rejection the offline paragraphs died from in the field run.
    late = await _accept_media_asr_decision(
        registry,
        identity,
        sentence_id="offline-9",
        start_sample=220_000,
        end_sample=228_000,
        text="南京呢",
        revision=9,
    )
    assert late.accepted is None

    await _wait_until(lambda: window.provider.prepared == ["后天呢"], timeout=3.0)


@pytest.mark.asyncio
async def test_media_playback_followup_advances_with_continued_speech(
    device_media_session: Any,
) -> None:
    """Continued post-boundary finals extend one follow-up turn, not two.

    A stuck VAD must not truncate the utterance at the first final either:
    each later guarded final advances the pinned endpoint and resets the
    grace, so both clauses commit together as one turn.
    """

    window = await device_media_session("followup-continued-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.pending.active_vad_start_sample = 195_000
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=225_000,
        text="那后天呢",
    )
    assert context.pending.turn_endpoint_sample == 225_000
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-2",
        start_sample=230_000,
        end_sample=240_000,
        text="天气怎么样",
        revision=2,
    )
    assert context.pending.turn_endpoint_sample == 240_000

    await _wait_until(lambda: len(window.provider.prepared) == 1, timeout=3.0)
    prepared = window.provider.prepared[0]
    assert "那后天呢" in prepared and "天气怎么样" in prepared


@pytest.mark.asyncio
async def test_media_playback_followup_empty_final_does_not_pin_the_endpoint(
    device_media_session: Any,
) -> None:
    """2026-09-28 field: an empty post-playback final pinned the endpoint.

    Pinned before a single word was recognized, the turn's absolute tail
    bound ran out while the user was still asking about the weather.
    """

    window = await device_media_session("followup-empty-final-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.pending.active_vad_start_sample = 195_000
    await _accept_media_asr_decision(
        registry,
        identity,
        sentence_id="followup-empty",
        start_sample=215_000,
        end_sample=225_000,
        text="",
    )
    assert context.pending.turn_endpoint_sample is None
    assert context.pending.turn_endpoint_tail_deadline is None

    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=226_000,
        end_sample=236_000,
        text="今天天气怎么样",
    )
    assert context.pending.turn_endpoint_sample == 236_000
    await _wait_until(lambda: window.provider.prepared == ["今天天气怎么样"], timeout=3.0)


@pytest.mark.asyncio
async def test_media_playback_followup_advance_restarts_the_absolute_tail_bound(
    device_media_session: Any,
) -> None:
    """Continued speech moves the absolute tail bound with the endpoint.

    Kept from the first pin, the bound expired mid-sentence and the device
    was put on standby (turn_prepare_timeout) with the question unanswered.
    """

    window = await device_media_session("followup-tail-bound-session")
    registry, identity, context = window.registry, window.identity, window.context
    await _complete_previous_device_playback(window)
    context.pending.active_vad_start_sample = 195_000
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-1",
        start_sample=215_000,
        end_sample=225_000,
        text="今天",
    )
    first_deadline = context.pending.turn_endpoint_tail_deadline
    first_handle = context.pending.turn_endpoint_timeout_handle
    assert first_deadline is not None and first_handle is not None

    await asyncio.sleep(0.05)
    await _accept_media_asr_final(
        registry,
        identity,
        sentence_id="followup-2",
        start_sample=230_000,
        end_sample=240_000,
        text="天气怎么样",
        revision=2,
    )
    assert context.pending.turn_endpoint_sample == 240_000
    assert context.pending.turn_endpoint_tail_deadline is not None
    assert context.pending.turn_endpoint_tail_deadline > first_deadline
    assert first_handle.cancelled()
    assert context.pending.turn_endpoint_timeout_handle is not None
    assert not context.standby_requested


class _UnlockedReplyLock:
    def locked(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_question_final_after_a_pinned_noise_turn_commits_the_question() -> None:
    """Field 2026-10-21 A6 t003 / N2 C8 / N2 C-stop4: 20-30 s replies.

    A sub-second noise edge pinned its own endpoint with no words (the ASR
    answered it from an unrelated task, ``empty+vendor_silent``), and the
    child's question then arrived as a final whose audio began after that
    pinned endpoint.  Committing the pinned turn carried nothing, and the
    question was only answered once the next sentence opened a fresh VAD turn.
    """

    identity = _device_identity("device-noise-turn-before-question")
    context = SimpleNamespace(
        identity=identity,
        runtime=DuplexRuntime.create(session_id=identity.session_id, barge_in_enabled=False),
        stream_epoch=identity.stream_epoch,
        closed=False,
        standby_requested=False,
        pending=PendingTurn(
            turn_start_sample=425_920,
            turn_end_sample=425_921,
            turn_endpoint_sample=425_921,
            empty_turn_endpoint_pin="vad_end",
        ),
        asr=SimpleNamespace(
            last_committed_sample=0,
            preview_result=lambda result: ASRAcceptDecision(
                result, ASRDecisionReason.ACCEPTED
            ),
            accept_result=lambda result, session_id: ASRAcceptDecision(
                result, ASRDecisionReason.ACCEPTED
            ),
        ),
        projection=SimpleNamespace(provisional=None),
        output=SimpleNamespace(
            reply_task=None,
            output_owner=None,
            reply_lock=_UnlockedReplyLock(),
            delegation_output_claims={},
        ),
        owner_silence_grace_deadline=None,
        last_asr_evidence_end_sample=0,
        last_playback_end_sample=12_800,
    )

    async def never_standby(ctx: Any, *, reason: str) -> bool:
        raise AssertionError(f"unexpected standby {reason}")

    async def noop_patch(ctx: Any, patch: Any) -> None:
        return None

    class _NoSessionBridge:
        def get(self, session_id: str) -> None:
            return None

    class _BridgeHarness:
        bridge = _NoSessionBridge()

        async def emit_transcript(self, *args: Any, **kwargs: Any) -> None:
            return None

    registry = object.__new__(MediaVoiceCoreRegistry)
    registry._sessions = {identity.session_id: context}  # noqa: SLF001
    registry.bridge = _BridgeHarness()  # noqa: SLF001
    registry.metrics = MetricsRegistry()  # noqa: SLF001
    registry.turn_endpoint_grace_s = 0.0  # noqa: SLF001
    registry._schedule_turn_commit = (  # noqa: SLF001
        lambda ctx: None
    )
    registry._request_device_standby = never_standby  # type: ignore[method-assign]  # noqa: SLF001
    registry._split_pending_turn_at_unvoiced_gap = (  # noqa: SLF001
        lambda ctx, result: None
    )
    registry._emit_projection_patch = noop_patch  # type: ignore[method-assign]  # noqa: SLF001

    async def noop_apply(ctx: Any, segment: Any) -> None:
        return None

    registry._apply_projection_segment = noop_apply  # type: ignore[method-assign]  # noqa: SLF001
    context.runtime.live_lookup_needed = lambda text, start_verdict=True: False  # type: ignore[method-assign]

    vad_end = ASRResult(
        stream_epoch=identity.stream_epoch,
        task_epoch=1,
        sentence_id="vad-end",
        revision=1,
        capture_start_sample=425_920,
        capture_end_sample=425_921,
        text="",
        is_final=True,
    )
    assert (
        await registry._accept_asr_result_decision(identity.session_id, vad_end)  # noqa: SLF001
    ).accepted is not None
    assert context.pending.turn_endpoint_sample == 425_921
    assert context.pending.empty_turn_endpoint_pin == "vad_end"

    question = ASRResult(
        stream_epoch=identity.stream_epoch,
        task_epoch=3,
        sentence_id="question-final",
        revision=1,
        capture_start_sample=523_520,
        capture_end_sample=529_280,
        text="它一天要吃多少",
        is_final=True,
    )
    assert (
        await registry._accept_asr_result_decision(identity.session_id, question)  # noqa: SLF001
    ).accepted is not None
    assert context.pending.empty_turn_endpoint_pin is None
    assert context.pending.turn_start_sample == 523_520
    assert context.pending.turn_end_sample == 529_280
    assert context.pending.turn_endpoint_sample == 529_280


async def _one_char_final_after_playback(
    window: Any, *, voiced_end: int
) -> None:
    """2026-10-31 16:09 soak t023: 0.2 s after the reply ended the device VAD fired on the room,
    ended 1.0 s later with no voiced sample (``voiced_end_sample == vad start``), and ASR made one
    acknowledgement-shaped character of it.  The commit runs about 3 s after the playback ended,
    past the 0.8 s backchannel guard."""

    set_floor(
        window.context.runtime,
        played_assistant_text="小兔子最后找到了回家的路，你喜欢这个故事吗？",
        last_playback_completed_ns=time.monotonic_ns() - 3_000_000_000,
    )
    await _device_vad(window, "room-vad", 320_000, final=False)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="room",
        start_sample=318_400,
        end_sample=324_000,
        text="嗯",
    )
    await window.registry.on_speech_segment(
        window.session,
        SpeechSegment(
            session_id=window.identity.session_id,
            stream_epoch=window.identity.stream_epoch,
            provider_task_epoch=0,
            segment_id="room-vad-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=336_000,
            capture_end_sample=336_001,
            final=True,
            voiced_end_sample=voiced_end,
            near_end_rms=0.0035,
        ),
    )


@pytest.mark.asyncio
async def test_media_unvoiced_one_char_final_after_playback_is_not_a_turn(
    device_media_session: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A one-character final over a device segment with no voiced sample is echo or noise: it must
    not commit as a turn and take the floor from the child's real question."""

    caplog.set_level(logging.INFO)
    window = await device_media_session("unvoiced-one-char", during_playback=False)
    await _one_char_final_after_playback(window, voiced_end=320_000)
    await _wait_until(lambda: bool(_commit_timing_fields(caplog)), timeout=3.0)

    [fields] = _commit_timing_fields(caplog)
    assert fields["result"] != "committed"
    assert window.provider.prepared == []
    assert "嗯" not in _user_turn_texts(window.context)


@pytest.mark.asyncio
async def test_media_voiced_one_char_final_after_playback_still_commits(
    device_media_session: Any,
) -> None:
    """The same 「嗯」 with voiced device samples is the child answering the robot's question."""

    window = await device_media_session("voiced-one-char", during_playback=False)
    await _one_char_final_after_playback(window, voiced_end=324_000)
    await _wait_until(lambda: window.provider.prepared == ["嗯"], timeout=3.0)
    assert "嗯" in _user_turn_texts(window.context)
