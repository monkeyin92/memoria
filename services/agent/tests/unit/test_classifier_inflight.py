"""Semantic turn classifiers: one call per sentence, started at the ASR final.

Round 10 (2026-10-02): a fresh sentence took 2.54 s from the ASR final to the turn commit, a repeated one
1.34 s.  The difference is two cloud classifier calls (live lookup, conversation close) that the commit path
started after the end-of-speech grace, and a second identical call whenever the early one was still running.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.src.voice_core.speech_timeline import ASRResult, SegmentKind, SpeechSegment
from services.agent.tests.unit.media_session_support import (
    _AckCapturingProvider,
    _CapturingGenerationBridge,
    _device_identity,
    _finish_output_owner_playback,
)

CLOSE_TEXT = "那先不聊了"
LIVE_TEXT = "明天从南京去上海，哪种交通方式最快"


def _runtime() -> DuplexRuntime:
    return DuplexRuntime.create(session_id="classifier-inflight", barge_in_enabled=False)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class _Gate:
    """A classifier whose answer is released by the test."""

    def __init__(self, verdict: bool | Exception = True) -> None:
        self.calls: list[str] = []
        self.cancelled = 0
        self.release = asyncio.Event()
        self.verdict = verdict

    async def __call__(self, text: str) -> bool:
        self.calls.append(text)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


# -- the shared in-flight call, through the runtime's public resolve methods --------------------


@pytest.mark.asyncio
async def test_close_resolve_joins_the_call_already_in_flight() -> None:
    runtime, gate = _runtime(), _Gate(True)
    runtime.set_conversation_close_semantic_resolver(gate)
    early = asyncio.ensure_future(runtime.resolve_conversation_close_needed(CLOSE_TEXT))
    await _settle()
    commit = asyncio.ensure_future(runtime.resolve_conversation_close_needed(CLOSE_TEXT))
    await _settle()
    gate.release.set()
    assert await early is True
    assert await commit is True
    assert gate.calls == [CLOSE_TEXT]


@pytest.mark.asyncio
async def test_live_lookup_resolve_joins_the_call_already_in_flight() -> None:
    runtime, gate = _runtime(), _Gate(True)
    runtime.set_live_lookup_semantic_resolver(gate)
    early = asyncio.ensure_future(runtime.resolve_live_lookup_needed(LIVE_TEXT))
    await _settle()
    commit = asyncio.ensure_future(runtime.resolve_live_lookup_needed(LIVE_TEXT))
    await _settle()
    gate.release.set()
    assert await early is True
    assert await commit is True
    assert gate.calls == [LIVE_TEXT]


@pytest.mark.asyncio
async def test_a_finished_verdict_is_still_cached() -> None:
    runtime, gate = _runtime(), _Gate(True)
    gate.release.set()
    runtime.set_conversation_close_semantic_resolver(gate)
    assert await runtime.resolve_conversation_close_needed(CLOSE_TEXT) is True
    assert await runtime.resolve_conversation_close_needed(CLOSE_TEXT) is True
    assert runtime.conversation_close_needed(CLOSE_TEXT) is True
    assert gate.calls == [CLOSE_TEXT]


@pytest.mark.asyncio
async def test_another_sentence_is_another_call() -> None:
    runtime, gate = _runtime(), _Gate(False)
    runtime.set_live_lookup_semantic_resolver(gate)
    first = asyncio.ensure_future(runtime.resolve_live_lookup_needed(LIVE_TEXT))
    second = asyncio.ensure_future(runtime.resolve_live_lookup_needed(LIVE_TEXT + "呢"))
    await _settle()
    gate.release.set()
    await first
    await second
    assert sorted(gate.calls) == sorted([LIVE_TEXT, LIVE_TEXT + "呢"])


@pytest.mark.asyncio
async def test_a_cancelled_joiner_does_not_cancel_the_shared_call() -> None:
    runtime, gate = _runtime(), _Gate(True)
    runtime.set_conversation_close_semantic_resolver(gate)
    driver = asyncio.ensure_future(runtime.resolve_conversation_close_needed(CLOSE_TEXT))
    await _settle()
    joiner = asyncio.ensure_future(runtime.resolve_conversation_close_needed(CLOSE_TEXT))
    await _settle()
    joiner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await joiner
    gate.release.set()
    assert await driver is True
    assert gate.calls == [CLOSE_TEXT]
    assert gate.cancelled == 0
    assert runtime.conversation_close_needed(CLOSE_TEXT) is True


@pytest.mark.asyncio
async def test_when_the_caller_running_the_call_is_cancelled_the_joiner_asks_again() -> None:
    """Without sharing the joiner would have made its own call; it must not inherit a cancelled one."""
    runtime, gate = _runtime(), _Gate(True)
    runtime.set_conversation_close_semantic_resolver(gate)
    driver = asyncio.ensure_future(runtime.resolve_conversation_close_needed(CLOSE_TEXT))
    await _settle()
    joiner = asyncio.ensure_future(runtime.resolve_conversation_close_needed(CLOSE_TEXT))
    await _settle()
    driver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await driver
    await _settle()
    gate.release.set()
    assert await joiner is True
    assert gate.calls == [CLOSE_TEXT, CLOSE_TEXT]
    assert gate.cancelled == 1


@pytest.mark.asyncio
async def test_a_call_whose_caller_swallowed_the_cancellation_is_not_joined() -> None:
    runtime = _runtime()
    release, started = asyncio.Event(), []

    async def swallowing(text: str) -> bool:
        started.append(text)
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return True

    runtime.set_conversation_close_semantic_resolver(swallowing)
    abandoned = asyncio.ensure_future(runtime.resolve_conversation_close_needed(CLOSE_TEXT))
    await _settle()
    abandoned.cancel()
    await _settle()
    assert not abandoned.done()  # swallowed: still waiting, exactly like awaiting the resolver directly
    newcomer = asyncio.ensure_future(runtime.resolve_conversation_close_needed(CLOSE_TEXT))
    await _settle()
    assert started == [CLOSE_TEXT, CLOSE_TEXT]  # the newcomer did not join the abandoned call
    release.set()
    assert await abandoned is True
    assert await newcomer is True


@pytest.mark.asyncio
async def test_cancelling_the_only_awaiter_cancels_the_call_as_it_always_did() -> None:
    """Nobody else needs the verdict: the resolver is cancelled, nothing is cached, the next caller asks again."""
    runtime, gate = _runtime(), _Gate(True)
    runtime.set_conversation_close_semantic_resolver(gate)
    only = asyncio.ensure_future(runtime.resolve_conversation_close_needed(CLOSE_TEXT))
    await _settle()
    only.cancel()
    with pytest.raises(asyncio.CancelledError):
        await only
    assert gate.cancelled == 1
    assert runtime.conversation_close_needed(CLOSE_TEXT) is False
    gate.release.set()
    assert await runtime.resolve_conversation_close_needed(CLOSE_TEXT) is True
    assert gate.calls == [CLOSE_TEXT, CLOSE_TEXT]


@pytest.mark.asyncio
async def test_a_verdict_started_in_the_background_survives_a_cancelled_awaiter() -> None:
    """The live-lookup verdict started at the final is for whoever commits next, not for one awaiter."""
    runtime, gate = _runtime(), _Gate(True)
    runtime.set_live_lookup_semantic_resolver(gate)
    runtime.live_lookup_needed(LIVE_TEXT, start_verdict=True)
    await _settle()
    assert gate.calls == [LIVE_TEXT]
    early = asyncio.ensure_future(runtime.resolve_live_lookup_needed(LIVE_TEXT))
    await _settle()
    early.cancel()
    with pytest.raises(asyncio.CancelledError):
        await early
    assert gate.cancelled == 0
    commit = asyncio.ensure_future(runtime.resolve_live_lookup_needed(LIVE_TEXT))
    await _settle()
    gate.release.set()
    assert await commit is True
    assert gate.calls == [LIVE_TEXT]


@pytest.mark.asyncio
async def test_closing_the_runtime_cancels_the_verdict_calls_still_in_flight() -> None:
    runtime, gate = _runtime(), _Gate(True)
    runtime.set_live_lookup_semantic_resolver(gate)
    runtime.live_lookup_needed(LIVE_TEXT, start_verdict=True)
    await _settle()
    assert gate.calls == [LIVE_TEXT] and gate.cancelled == 0
    await runtime.close()
    assert gate.cancelled == 1
    assert runtime._live_lookup_cache.inflight == {}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_failed_call_is_shared_by_its_awaiters_not_cached_and_retried() -> None:
    runtime, gate = _runtime(), _Gate(RuntimeError("classifier down"))
    runtime.set_live_lookup_semantic_resolver(gate)
    first = asyncio.ensure_future(runtime.resolve_live_lookup_needed(LIVE_TEXT))
    joined = asyncio.ensure_future(runtime.resolve_live_lookup_needed(LIVE_TEXT))
    await _settle()
    gate.release.set()
    for awaiter in (first, joined):
        with pytest.raises(RuntimeError, match="classifier down"):
            await awaiter
    assert gate.calls == [LIVE_TEXT]
    assert runtime.live_lookup_needed(LIVE_TEXT) is False  # nothing cached

    gate.verdict = True
    assert await runtime.resolve_live_lookup_needed(LIVE_TEXT) is True
    assert gate.calls == [LIVE_TEXT, LIVE_TEXT]


# -- timing log lines (TODOLIST N-14 4): how long the cloud call took, whether the turn waited for it ----


def _classifier_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, str]]:
    """The ``classifier call`` / ``classifier verdict wait`` lines as key=value dicts, line kind under ``line``."""

    lines = []
    for record in caplog.records:
        message = record.getMessage()
        for prefix in ("classifier call ", "classifier verdict wait "):
            if message.startswith(prefix):
                fields = dict(part.split("=", 1) for part in message[len(prefix):].split())
                lines.append({"line": prefix.strip(), **fields})
    return lines


@pytest.mark.asyncio
async def test_a_call_and_the_wait_for_it_are_logged_without_the_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.classifier_inflight")
    runtime, gate = _runtime(), _Gate(True)
    gate.release.set()
    runtime.set_conversation_close_semantic_resolver(gate)
    assert await runtime.resolve_conversation_close_needed(CLOSE_TEXT) is True

    call, wait = _classifier_lines(caplog)
    assert call["line"] == "classifier call"
    assert (call["kind"], call["outcome"], call["verdict"]) == ("conversation_close", "ok", "True")
    assert call["duration_ms"].isdigit()
    assert (wait["kind"], wait["source"]) == ("conversation_close", "called")
    assert wait["waited_ms"].isdigit()
    assert CLOSE_TEXT not in caplog.text


@pytest.mark.asyncio
async def test_a_joiner_logs_the_wait_for_the_call_in_flight_and_a_cached_verdict_logs_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.classifier_inflight")
    runtime, gate = _runtime(), _Gate(False)
    runtime.set_live_lookup_semantic_resolver(gate)
    runtime.live_lookup_needed(LIVE_TEXT, start_verdict=True)  # the call started at the ASR final
    await _settle()
    commit = asyncio.ensure_future(runtime.resolve_live_lookup_needed(LIVE_TEXT))
    await _settle()
    gate.release.set()
    assert await commit is False
    await _settle()

    lines = _classifier_lines(caplog)
    assert [(line["line"], line["kind"]) for line in lines] == [
        ("classifier call", "live_lookup"),
        ("classifier verdict wait", "live_lookup"),
    ]
    assert lines[1]["source"] == "joined"

    assert await runtime.resolve_live_lookup_needed(LIVE_TEXT) is False  # cached: nothing waited for
    assert len(_classifier_lines(caplog)) == 2


@pytest.mark.asyncio
async def test_a_failed_and_a_cancelled_call_are_logged_with_their_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.classifier_inflight")
    runtime, gate = _runtime(), _Gate(RuntimeError("classifier down"))
    gate.release.set()
    runtime.set_live_lookup_semantic_resolver(gate)
    with pytest.raises(RuntimeError, match="classifier down"):
        await runtime.resolve_live_lookup_needed(LIVE_TEXT)

    slow = _Gate(True)
    runtime.set_conversation_close_semantic_resolver(slow)
    awaiter = asyncio.ensure_future(runtime.resolve_conversation_close_needed(CLOSE_TEXT))
    await _settle()
    awaiter.cancel()
    await asyncio.gather(awaiter, return_exceptions=True)

    outcomes = [
        (line["kind"], line["outcome"], line["verdict"])
        for line in _classifier_lines(caplog)
        if line["line"] == "classifier call"
    ]
    assert outcomes == [
        ("live_lookup", "error", "None"),
        ("conversation_close", "cancelled", "None"),
    ]
    assert not [line for line in _classifier_lines(caplog) if line["line"].endswith("wait")]


@pytest.mark.asyncio
async def test_a_wait_nobody_is_on_the_commit_path_for_is_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.classifier_inflight")
    runtime, gate = _runtime(), _Gate(True)
    gate.release.set()
    runtime.set_conversation_close_semantic_resolver(gate)

    assert await runtime.resolve_conversation_close_needed(CLOSE_TEXT, log_wait=False) is True

    assert [line["line"] for line in _classifier_lines(caplog)] == ["classifier call"]


@pytest.mark.asyncio
async def test_a_quiet_joiner_of_a_call_in_flight_logs_no_wait_either(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.classifier_inflight")
    runtime, gate = _runtime(), _Gate(True)
    runtime.set_conversation_close_semantic_resolver(gate)
    runtime.start_conversation_close_verdict(CLOSE_TEXT)
    await _settle()
    quiet = asyncio.ensure_future(runtime.resolve_conversation_close_needed(CLOSE_TEXT, log_wait=False))
    await _settle()
    gate.release.set()
    assert await quiet is True
    await _settle()

    assert [line["line"] for line in _classifier_lines(caplog)] == ["classifier call"]
    await runtime.close()


@pytest.mark.asyncio
async def test_the_close_verdict_can_be_started_in_the_background_and_is_joined_by_the_commit() -> None:
    runtime, gate = _runtime(), _Gate(False)
    runtime.set_conversation_close_semantic_resolver(gate)

    runtime.start_conversation_close_verdict(CLOSE_TEXT)
    runtime.start_conversation_close_verdict(CLOSE_TEXT)  # the same sentence again: nothing new starts
    await _settle()
    assert gate.calls == [CLOSE_TEXT]

    commit = asyncio.ensure_future(runtime.resolve_conversation_close_needed(CLOSE_TEXT))
    await _settle()
    assert not commit.done()
    gate.release.set()
    assert await commit is False
    assert gate.calls == [CLOSE_TEXT]
    runtime.start_conversation_close_verdict(CLOSE_TEXT)  # cached now
    await _settle()
    assert gate.calls == [CLOSE_TEXT]
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["再见", "停", "好了", "", "  "])
async def test_no_close_verdict_is_started_where_the_commit_would_not_call_the_classifier(
    text: str,
) -> None:
    runtime, gate = _runtime(), _Gate(True)
    runtime.set_conversation_close_semantic_resolver(gate)

    runtime.start_conversation_close_verdict(text)
    await _settle()

    assert gate.calls == []
    gate.release.set()
    await runtime.close()


@pytest.mark.asyncio
async def test_no_close_verdict_is_started_without_a_resolver(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.classifier_inflight")
    runtime = _runtime()
    runtime.start_conversation_close_verdict(CLOSE_TEXT)  # nothing installed: nothing to start
    await _settle()
    assert runtime._conversation_close_cache.inflight == {}  # type: ignore[attr-defined]
    assert _classifier_lines(caplog) == [], "not even a failed call"
    await runtime.close()


@pytest.mark.asyncio
async def test_no_call_is_left_in_flight_after_it_finishes() -> None:
    runtime, gate = _runtime(), _Gate(True)
    gate.release.set()
    runtime.set_live_lookup_semantic_resolver(gate)
    runtime.set_conversation_close_semantic_resolver(gate)
    await runtime.resolve_live_lookup_needed(LIVE_TEXT)
    await runtime.resolve_conversation_close_needed(CLOSE_TEXT)
    await _settle()
    assert runtime._live_lookup_cache.inflight == {}  # type: ignore[attr-defined]
    assert runtime._conversation_close_cache.inflight == {}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_rule_and_keyword_short_circuits_never_reach_the_classifier() -> None:
    runtime, gate = _runtime(), _Gate(True)
    gate.release.set()
    runtime.set_live_lookup_semantic_resolver(gate)
    runtime.set_conversation_close_semantic_resolver(gate)
    assert await runtime.resolve_conversation_close_needed("再见") is True  # rule farewell
    assert await runtime.resolve_conversation_close_needed("停") is False  # lexical stop word
    assert await runtime.resolve_live_lookup_needed("今天天气怎么样") is True  # keyword
    assert await runtime.resolve_live_lookup_needed("现在几点了") is False  # local clock fact
    assert gate.calls == []


# -- the live-lookup verdict starts at the ASR final, as the conversation-close one already does --


async def _idle_device_session(name: str) -> tuple[MediaVoiceCoreRegistry, object, object]:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id, barge_in_enabled=False
        ),
    )
    registry.install()
    identity = _device_identity(name)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    await asyncio.wait_for(provider.completed.wait(), timeout=1)
    await _finish_output_owner_playback(registry, identity, bridge, session)
    await registry.on_speech_segment(
        session,
        SpeechSegment(
            session_id=identity.session_id,
            stream_epoch=identity.stream_epoch,
            provider_task_epoch=1,
            segment_id="vad-start",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=0,
            capture_end_sample=1,
        ),
    )
    return registry, identity, context


async def _accept_final(registry: MediaVoiceCoreRegistry, identity: object, text: str) -> None:
    accepted = ASRResult(
        stream_epoch=identity.stream_epoch,  # type: ignore[attr-defined]
        task_epoch=1,
        sentence_id=f"final-{text}",
        revision=1,
        capture_start_sample=0,
        capture_end_sample=16_000,
        text=text,
        is_final=True,
        confidence=0.9,
    )
    assert await registry.accept_asr_result(identity.session_id, accepted)  # type: ignore[attr-defined]
    await _settle()


@pytest.mark.asyncio
async def test_live_lookup_verdict_starts_at_the_final_and_does_not_move_the_endpoint() -> None:
    registry, identity, context = await _idle_device_session("inflight-live-at-final")
    gate = _Gate(True)
    try:
        context.runtime.set_live_lookup_semantic_resolver(gate)  # type: ignore[attr-defined]
        await _accept_final(registry, identity, LIVE_TEXT)
        assert gate.calls == [LIVE_TEXT]
        gate.release.set()
        await _settle()
        # Starting the classifier early is all that changes: a positive verdict does not pin the endpoint.
        assert context.pending.turn_endpoint_sample is None  # type: ignore[attr-defined]
        assert context.runtime.live_lookup_needed(LIVE_TEXT)  # type: ignore[attr-defined]
    finally:
        gate.release.set()
        await registry.finalize_session(identity.session_id)  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["停", "别说了", "再见"])
async def test_a_stop_word_or_a_farewell_final_does_not_start_the_live_lookup_verdict(
    text: str,
) -> None:
    registry, identity, context = await _idle_device_session(f"inflight-live-stop-{len(text)}")
    gate = _Gate(True)
    try:
        context.runtime.set_live_lookup_semantic_resolver(gate)  # type: ignore[attr-defined]
        await _accept_final(registry, identity, text)
        assert gate.calls == []
    finally:
        gate.release.set()
        await registry.finalize_session(identity.session_id)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_keyword_final_starts_no_semantic_call() -> None:
    registry, identity, context = await _idle_device_session("inflight-live-keyword")
    gate = _Gate(True)
    try:
        context.runtime.set_live_lookup_semantic_resolver(gate)  # type: ignore[attr-defined]
        await _accept_final(registry, identity, "今天天气怎么样")
        assert gate.calls == []
    finally:
        gate.release.set()
        await registry.finalize_session(identity.session_id)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_final_heard_while_a_reply_is_audible_starts_no_live_lookup_verdict() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id, barge_in_enabled=False
        ),
    )
    registry.install()
    identity = _device_identity("inflight-live-reply-audible")
    session = bridge.bridge.open(identity)
    gate = _Gate(True)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        # The wake acknowledgement is still the audible reply: its echo must not cost a classifier call.
        assert registry._reply_in_flight(context)
        context.runtime.set_live_lookup_semantic_resolver(gate)
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="vad-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        accepted = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="echo-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=16_000,
            text=LIVE_TEXT,
            is_final=True,
            confidence=0.9,
        )
        await registry.accept_asr_result(identity.session_id, accepted)
        await _settle()
        assert gate.calls == []
    finally:
        gate.release.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_the_commit_joins_the_close_verdict_started_at_the_final(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real sequence: the final starts the close verdict, the commit path asks for it again mid-call."""
    caplog.set_level(logging.INFO, logger="services.agent.src.classifier_inflight")
    registry, identity, context = await _idle_device_session("inflight-close-commit-joins")
    gate = _Gate(False)
    try:
        context.runtime.set_conversation_close_semantic_resolver(gate)  # type: ignore[attr-defined]
        await _accept_final(registry, identity, "我想听一个新的故事")
        assert gate.calls == ["我想听一个新的故事"]
        # What _commit_user_turn_locked does after the end-of-speech grace, while the early call still runs.
        joined = asyncio.ensure_future(
            context.runtime.resolve_conversation_close_needed("我想听一个新的故事")  # type: ignore[attr-defined]
        )
        await _settle()
        assert not joined.done()
        gate.release.set()
        assert await joined is False
        assert gate.calls == ["我想听一个新的故事"]
        await _settle()
        # The commit's wait is the only wait on record: the early evaluation made the call, nobody waited on it.
        waits = [line for line in _classifier_lines(caplog) if line["line"].endswith("wait")]
        assert [(line["kind"], line["source"]) for line in waits] == [("conversation_close", "joined")]
    finally:
        gate.release.set()
        await registry.finalize_session(identity.session_id)  # type: ignore[attr-defined]
