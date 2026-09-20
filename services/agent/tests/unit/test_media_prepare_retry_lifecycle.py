"""Fault-injected preparation retries, using real commit and timeout lifecycles.

The short retry backoffs are not provider-await deadlines.  In particular, a
direct call to an expiry coroutine is not proof that its timer was ever armed:
every timeout driver below first checks the live production timer/deadline.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.voice_core import media_session_turns
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaSessionResources,
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.speech_timeline import ASRResult, SegmentKind, SpeechSegment
from services.agent.tests.unit.test_media_session import (
    FakeMediaProvider,
    _CapturingMediaBridge,
    _owner_silence_identity,
    _seed_pending_media_turn,
    _verified_owner_decision,
)

_TEXT = "请给我讲一个森林里的故事"
_ENDPOINT = 600
_RETIRE = 640
_BUDGET = 7.5


@dataclass
class _PrepareStep:
    outcome: Literal["commit", "error"] = "error"
    ignore_cancel: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


class _GatedProvider(FakeMediaProvider):
    def __init__(self, runtime: DuplexRuntime, steps: list[_PrepareStep]) -> None:
        super().__init__()
        self.runtime = runtime
        self.steps = steps
        self.prepare_calls = 0
        self.reply_calls = 0
        self.reply_started = asyncio.Event()
        self.close_finished = asyncio.Event()

    async def prepare_committed_turn(
        self, _identity: SessionIdentity, text: str,
    ) -> GenerationFence:
        index = self.prepare_calls
        self.prepare_calls += 1
        assert index < len(self.steps), "preparation exceeded the initial call plus two retries"
        step = self.steps[index]
        step.entered.set()
        try:
            await step.release.wait()
        except asyncio.CancelledError:
            step.cancelled.set()
            if not step.ignore_cancel:
                raise
            # Deliberately model a provider that finishes after cancellation.
            # Test teardown always releases this gate before finalizing.
            await step.release.wait()
        if step.outcome == "error":
            raise RuntimeError(f"prepare fault at call {index + 1}")
        return await self.runtime.on_turn_committed(text, input_modality="audio")

    def generate_reply(
        self, identity: SessionIdentity, user_text: str, fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        self.reply_calls += 1
        self.reply_started.set()
        return super().generate_reply(identity, user_text, fence)

    async def close(self, identity: SessionIdentity) -> None:
        await super().close(identity)
        self.close_finished.set()


@dataclass
class _Scenario:
    registry: MediaVoiceCoreRegistry
    identity: SessionIdentity
    context: Any
    provider: _GatedProvider
    bridge: _CapturingMediaBridge
    session: Any
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    clock_patch: pytest.MonkeyPatch = field(default_factory=pytest.MonkeyPatch)

    def track(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        self.tasks.append(task)
        return task

    def events(self, event_type: str) -> list[dict[str, object]]:
        return [payload for kind, payload in self.bridge.events if kind == event_type]

    async def entered(self, call: int) -> _PrepareStep:
        step = self.provider.steps[call - 1]
        await asyncio.wait_for(step.entered.wait(), 1)
        assert self.context.turn_commit_task is not None
        assert not self.context.turn_commit_task.done()
        assert self.context.turn_commit_lock.locked()
        if call > 1:
            retry = self.context.turn_commit_retry_task
            assert retry is not None and not retry.done()
            self.track(retry)
        return step


def _vad(identity: SessionIdentity, *, start: int = 0, final: bool = False) -> SpeechSegment:
    return SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        provider_task_epoch=0,
        segment_id=f"retry-vad-{identity.stream_epoch}-{start}-{final}",
        revision=1, kind=SegmentKind.VAD,
        capture_start_sample=start, capture_end_sample=start + 1,
        final=final, voiced_end_sample=_ENDPOINT if final else None,
        near_end_rms=900,
    )


@asynccontextmanager
async def _scenario(
    name: str, steps: list[_PrepareStep], *, direct_seed: bool = False,
    with_grace: bool = False,
) -> AsyncIterator[_Scenario]:
    identity = _owner_silence_identity(name)
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    provider = _GatedProvider(runtime, steps)
    bridge = _CapturingMediaBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _: MediaSessionResources(runtime, provider),
        metrics=MetricsRegistry(),
        owner_silence_timeout_s=100, max_user_speech_duration_s=60,
        turn_endpoint_grace_s=0, turn_endpoint_absolute_timeout_s=60,
    )
    session = bridge._open_connection(identity).session
    context = await registry._get_or_create(identity)
    case = _Scenario(registry, identity, context, provider, bridge, session)
    try:
        # A previously spent budget, not a new budget minted by this failure.
        registry._pause_owner_silence_timer(context)
        context.owner_silence_remaining_s = _BUDGET
        registry._sync_owner_silence_phase(context, "listening")
        if direct_seed:
            await _seed_pending_media_turn(
                registry, identity, text=_TEXT,
                endpoint_sample=_ENDPOINT, retire_sample=_RETIRE,
            )
        else:
            final = ASRResult(
                task_epoch=1, sentence_id="retry-original-final", revision=1,
                capture_start_sample=0, capture_end_sample=_ENDPOINT,
                text=_TEXT, is_final=True, stream_epoch=identity.stream_epoch,
            )
            if with_grace:
                assert await registry.accept_asr_result(identity.session_id, final)
                await _expire_real_owner_timer(case)
                assert context.owner_silence_grace_deadline is not None
                assert context.owner_silence_grace_used
            await registry.on_speech_segment(session, _vad(identity))
            assert context.max_user_speech_task is not None
            assert context.active_vad_stream_epoch == identity.stream_epoch
            if not with_grace:
                assert await registry.accept_asr_result(identity.session_id, final)
            else:
                assert context.owner_silence_grace_deadline is None
                assert context.owner_silence_remaining_s == 0
            await registry.on_speech_segment(
                session, _vad(identity, start=_RETIRE, final=True),
            )
            assert context.max_user_speech_task is None
            assert context.active_vad_stream_epoch is None
        assert not runtime.current_speaker_authority_verified
        yield case
    finally:
        # Release every provider before acquiring lifecycle locks in teardown.
        for step in steps:
            step.release.set()
        for attr in (
            "turn_commit_task", "turn_commit_retry_task", "turn_endpoint_task",
        ):
            task = getattr(context, attr)
            if task is not None:
                case.track(task)
        try:
            await asyncio.wait_for(registry._finalize_session(identity.session_id), 1)
            for task in case.tasks:
                if not task.done():
                    task.cancel()
            if case.tasks:
                await asyncio.wait_for(asyncio.gather(*case.tasks, return_exceptions=True), 1)
        finally:
            case.clock_patch.undo()


def _live_tail(case: _Scenario) -> tuple[asyncio.TimerHandle, float]:
    context = case.context
    handle = context.turn_endpoint_timeout_handle
    deadline = context.turn_endpoint_tail_deadline
    assert handle is not None, (
        "matching prepare retry has no armed endpoint-tail timer; a retry task/backoff "
        "does not bound provider.prepare_committed_turn"
    )
    assert not handle.cancelled(), "endpoint-tail timer was already cancelled"
    assert deadline is not None, "endpoint-tail timer has no absolute deadline"
    assert handle.when() == pytest.approx(deadline, abs=0.01)
    return handle, deadline


def _expire_real_tail(case: _Scenario) -> asyncio.Task[Any]:
    handle, deadline = _live_tail(case)
    assert case.context.turn_endpoint_sample == _ENDPOINT
    # Advance only this lifecycle module's clock to the OBSERVED deadline; do
    # not overwrite the stored deadline or fast-forward unrelated owner/output
    # timers on the shared event loop. Early manual expiry remains a separate
    # contract in test_media_session; this callback is genuinely due here.
    case.clock_patch.setattr(
        media_session_turns, "time",
        SimpleNamespace(monotonic=lambda: deadline, monotonic_ns=time.monotonic_ns),
    )
    # Dispatch the callback belonging to the observed production timer, without
    # sleeping for its configured 60 seconds or manufacturing a missing timer.
    handle.cancel()
    before = asyncio.all_tasks()
    case.registry._start_endpoint_tail_expiry(
        case.identity.session_id, case.context.stream_epoch, _ENDPOINT,
    )
    created = asyncio.all_tasks() - before
    tail = [task for task in created if task.get_name().startswith("media-turn-tail-")]
    assert len(tail) == 1
    return case.track(tail[0])


async def _expire_real_owner_timer(case: _Scenario) -> None:
    context = case.context
    previous = context.owner_silence_task
    assert previous is not None and not previous.done()
    assert context.owner_silence_deadline is not None
    previous.cancel()
    await asyncio.gather(previous, return_exceptions=True)
    watcher = case.track(asyncio.create_task(case.registry._owner_silence_watch(context, 0)))
    context.owner_silence_task = watcher
    await asyncio.wait_for(asyncio.shield(watcher), 1)


async def _must_settle(task: asyncio.Task[Any], message: str) -> None:
    # This is a test hang guard AFTER dispatching expiry, not a production
    # provider timeout and not a replacement for the live-timer assertion.
    done, _ = await asyncio.wait({task}, timeout=0.25)
    assert task in done, message
    task.result()


def _verify_owner(case: _Scenario) -> None:
    # Inject the same verified authority fixture as the existing owner-budget
    # tests, not a bare VAD or an assumed device identity.
    decision = _verified_owner_decision()
    case.context.runtime._speaker_decision = decision
    case.context.runtime._speaker_class = decision.classification
    assert case.context.runtime.current_speaker_authority_verified
    assert case.context.runtime.current_speaker_class == "owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_order", ["retry_first", "endpoint_first"])
async def test_matching_retry_keeps_an_armed_absolute_tail(schedule_order: str) -> None:
    steps = [_PrepareStep(), _PrepareStep("commit")]
    async with _scenario(
        f"retry-tail-{schedule_order}", steps, direct_seed=schedule_order == "retry_first",
    ) as case:
        if schedule_order == "retry_first":
            steps[0].release.set()
            assert await case.registry._commit_pending_turn(case.context) == "provider_prepare_failed"
        else:
            await case.entered(1)
            first_handle, first_deadline = _live_tail(case)
            steps[0].release.set()
        await case.entered(2)
        assert case.registry._turn_commit_retry_matches(
            case.context, case.identity.stream_epoch, _ENDPOINT,
        )
        case.registry._schedule_turn_commit(case.context)
        handle, deadline = _live_tail(case)
        if schedule_order == "endpoint_first":
            assert handle is first_handle
            assert deadline == first_deadline
        case.registry._schedule_turn_commit(case.context)
        assert _live_tail(case) == (handle, deadline)


@pytest.mark.asyncio
@pytest.mark.parametrize("success_on_call", [2, 3])
@pytest.mark.parametrize("verified_owner", [False, True])
async def test_prepare_retries_succeed_once_without_extending_tail(
    success_on_call: int, verified_owner: bool,
) -> None:
    steps = [_PrepareStep() for _ in range(success_on_call - 1)] + [_PrepareStep("commit")]
    async with _scenario(f"retry-success-{success_on_call}", steps) as case:
        await case.entered(1)
        handle, deadline = _live_tail(case)
        for call in range(1, success_on_call + 1):
            await case.entered(call)
            assert _live_tail(case) == (handle, deadline)
            assert 0 <= case.context.owner_silence_remaining_s <= _BUDGET
            if verified_owner:
                _verify_owner(case)
            steps[call - 1].release.set()
        retry = case.context.turn_commit_retry_task
        assert retry is not None
        await asyncio.wait_for(asyncio.shield(retry), 1)
        await asyncio.wait_for(case.provider.reply_started.wait(), 1)
        assert case.provider.prepare_calls == success_on_call
        assert case.provider.reply_calls == 1
        assert len(case.events("turn.committed")) == 1
        assert [
            turn.content for turn in case.context.runtime.orchestrator.context.turns
            if turn.role == "user"
        ] == [_TEXT]
        assert case.context.asr.last_committed_sample == _RETIRE
        assert case.context.turn_commit_retry_task is None
        assert case.context.turn_endpoint_tail_deadline is None
        assert handle.cancelled()
        # The accepted turn refills the follow-up window with or without
        # speaker authority (2026-09-20 product contract); the retries before it
        # are not owner activity and are still bounded by the seeded budget
        # inside the loop above.
        assert (
            case.context.owner_silence_remaining_s
            == case.registry.owner_silence_timeout_s
        )
        assert not case.context.owner_silence_grace_used
        assert case.registry.metrics.get(
            "voice_turn_prepare_retry_total", {"status": "attempt"},
        ) == success_on_call - 1


@pytest.mark.asyncio
@pytest.mark.parametrize("verified_owner", [False, True])
async def test_prepare_retry_exhaustion_retires_once_without_refilling_owner_budget(
    verified_owner: bool,
) -> None:
    steps = [_PrepareStep(), _PrepareStep(), _PrepareStep()]
    async with _scenario("retry-exhaustion-budget", steps) as case:
        await case.entered(1)
        handle, deadline = _live_tail(case)
        for call in range(1, 4):
            await case.entered(call)
            assert _live_tail(case) == (handle, deadline)
            assert 0 <= case.context.owner_silence_remaining_s <= _BUDGET
            if verified_owner:
                _verify_owner(case)
            steps[call - 1].release.set()
        retry = case.context.turn_commit_retry_task
        assert retry is not None
        await asyncio.wait_for(asyncio.shield(retry), 1)
        discarded = case.events("turn.provisional.discarded")
        assert len(discarded) == 1
        assert discarded[0]["reason"] == "provider_prepare_retries_exhausted"
        assert case.provider.prepare_calls == 3
        assert case.provider.reply_calls == 0
        assert not case.events("turn.committed")
        assert case.context.projection.provisional is None
        assert case.context.turn_start_sample is None
        assert case.context.turn_commit_retry_task is None
        assert case.context.asr.last_committed_sample == _RETIRE
        assert case.context.runtime.speech_timeline.committed_sample == _RETIRE
        assert handle.cancelled()
        assert 0 <= case.context.owner_silence_remaining_s <= _BUDGET
        assert case.context.owner_silence_task is not None
        assert not case.context.owner_silence_task.done()
        assert case.registry.metrics.get(
            "voice_turn_prepare_retry_total", {"status": "attempt"},
        ) == 2
        assert case.registry.metrics.get(
            "voice_turn_prepare_retry_total", {"status": "exhausted"},
        ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("blocked_call", "with_grace"),
    [(1, False), (2, False), (3, False), (1, True)],
)
async def test_hung_prepare_is_stopped_by_the_armed_absolute_tail(
    blocked_call: int, with_grace: bool,
) -> None:
    steps = [_PrepareStep() for _ in range(3)]
    async with _scenario(
        f"retry-hung-{blocked_call}-{with_grace}", steps, with_grace=with_grace,
    ) as case:
        await case.entered(1)
        original_tail = _live_tail(case)
        for call in range(1, blocked_call):
            steps[call - 1].release.set()
            await case.entered(call + 1)
        assert _live_tail(case) == original_tail
        if with_grace:
            assert case.context.owner_silence_grace_used
            assert case.context.owner_silence_remaining_s == 0
            assert case.context.owner_silence_task is None
            assert case.context.max_user_speech_task is None
        expiring = _expire_real_tail(case)
        await _must_settle(
            expiring,
            f"armed endpoint deadline cannot stop blocked prepare call {blocked_call}; "
            "expiry is waiting for the provider/retry or its turn_commit_lock",
        )
        assert steps[blocked_call - 1].cancelled.is_set()
        assert not steps[blocked_call - 1].release.is_set()
        assert case.context.projection.provisional is None
        assert case.context.turn_commit_task is None
        assert case.context.turn_commit_retry_task is None
        assert not case.events("turn.committed")
        assert case.provider.reply_calls == 0
        assert case.context.closed or (
            case.context.owner_silence_task is not None
            and not case.context.owner_silence_task.done()
            and 0 <= case.context.owner_silence_remaining_s <= _BUDGET
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("verified_owner", [False, True])
async def test_prepare_failure_after_retracted_grace_cannot_buy_another_budget(
    verified_owner: bool,
) -> None:
    steps = [_PrepareStep(), _PrepareStep(), _PrepareStep()]
    async with _scenario(
        "retry-failure-spent-grace", steps, with_grace=True,
    ) as case:
        await case.entered(1)
        handle, _ = _live_tail(case)
        assert case.context.owner_silence_remaining_s == 0
        assert case.context.owner_silence_grace_used
        assert case.context.owner_silence_grace_deadline is None
        if verified_owner:
            _verify_owner(case)
        steps[0].release.set()
        await asyncio.wait_for(case.provider.close_finished.wait(), 1)
        assert case.context.closed
        assert case.context.standby_reason == "owner_silence_timeout"
        assert case.provider.prepare_calls == 1
        assert case.provider.reply_calls == 0
        assert case.context.owner_silence_grace_used
        assert case.context.owner_silence_task is None
        assert case.context.turn_commit_retry_task is None
        assert case.context.projection.provisional is None
        assert handle.cancelled()
        assert not case.events("turn.committed")


@pytest.mark.asyncio
@pytest.mark.parametrize("late_outcome", ["commit", "error"])
async def test_owner_grace_terminal_fences_late_retry_result(
    late_outcome: Literal["commit", "error"],
) -> None:
    steps = [_PrepareStep(), _PrepareStep(late_outcome, ignore_cancel=True)]
    async with _scenario(f"retry-terminal-{late_outcome}", steps) as case:
        await case.entered(1)
        steps[0].release.set()
        await case.entered(2)
        _live_tail(case)
        await _expire_real_owner_timer(case)
        assert case.context.owner_silence_grace_used
        assert case.context.owner_silence_grace_deadline is not None
        assert case.context.owner_silence_remaining_s == 0
        closing = case.track(asyncio.create_task(_expire_real_owner_timer(case)))
        await asyncio.wait_for(steps[1].cancelled.wait(), 1)
        assert case.context.standby_requested
        assert case.context.standby_reason == "owner_silence_timeout"
        steps[1].release.set()
        await asyncio.wait_for(asyncio.shield(closing), 1)
        assert case.context.closed
        assert case.registry.context(case.identity.session_id) is None
        assert case.provider.closed
        assert case.context.owner_silence_grace_used
        assert case.context.owner_silence_task is None
        assert case.context.turn_commit_retry_task is None
        assert case.context.max_user_speech_task is None
        assert case.context.active_vad_stream_epoch is None
        assert case.context.projection.provisional is None
        assert not case.events("turn.committed")
        assert case.provider.reply_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("late_outcome", ["commit", "error"])
@pytest.mark.parametrize("verified_owner", [False, True])
async def test_reconnect_fences_old_retry_result_before_new_input(
    late_outcome: Literal["commit", "error"], verified_owner: bool,
) -> None:
    # Preserve a genuinely late result even when reconnect actively retires
    # the old preparation instead of waiting for it cooperatively.
    steps = [_PrepareStep(), _PrepareStep(late_outcome, ignore_cancel=True)]
    async with _scenario(f"retry-reconnect-{late_outcome}", steps) as case:
        await case.entered(1)
        steps[0].release.set()
        await case.entered(2)
        old_handle, _ = _live_tail(case)
        old_retry = case.context.turn_commit_retry_task
        if verified_owner:
            _verify_owner(case)
        assert 0 <= case.context.owner_silence_remaining_s <= _BUDGET
        replacement = replace(case.identity, stream_epoch=case.identity.stream_epoch + 1)
        new_session = case.bridge._open_connection(replacement).session
        reconnecting = case.track(asyncio.create_task(case.registry.on_session_connected(new_session)))
        await asyncio.wait_for(steps[1].cancelled.wait(), 1)
        assert not reconnecting.done(), "the old prepare still owns turn_commit_lock"
        steps[1].release.set()
        await asyncio.wait_for(asyncio.shield(reconnecting), 1)
        assert old_retry is not None
        await asyncio.wait_for(asyncio.shield(old_retry), 1)
        assert case.context.stream_epoch == replacement.stream_epoch
        assert case.context.projection.provisional is None
        assert case.context.turn_commit_retry_task is None
        assert old_handle.cancelled()
        assert not case.events("turn.committed")
        assert case.provider.reply_calls == 0
        # Isolate stale-completion cleanup from the subsequently accepted VAD:
        # reconnect itself must resume the old budget, not replenish it.
        assert 0 <= case.context.owner_silence_remaining_s <= _BUDGET
        assert case.context.owner_silence_task is not None
        assert not case.context.owner_silence_task.done()
        await case.registry.on_speech_segment(new_session, _vad(replacement, start=960))
        assert case.context.turn_start_sample == 960
        assert case.context.active_vad_stream_epoch == replacement.stream_epoch
        assert case.context.active_vad_start_sample == 960
        assert case.context.max_user_speech_task is not None
        assert not case.context.max_user_speech_task.done()
        assert 0 <= case.context.owner_silence_remaining_s <= _BUDGET


@pytest.mark.asyncio
@pytest.mark.parametrize("late_outcome", ["commit", "error"])
async def test_terminal_while_reconnect_waits_cannot_install_the_new_epoch(
    late_outcome: Literal["commit", "error"],
) -> None:
    steps = [_PrepareStep(), _PrepareStep(late_outcome, ignore_cancel=True)]
    async with _scenario(f"retry-terminal-during-reconnect-{late_outcome}", steps) as case:
        await case.entered(1)
        steps[0].release.set()
        await case.entered(2)
        _live_tail(case)
        prepare = case.context.turn_commit_task
        assert prepare is not None
        identity = replace(case.identity, stream_epoch=case.identity.stream_epoch + 1)
        session = case.bridge._open_connection(identity).session
        reconnecting = case.track(asyncio.create_task(case.registry.on_session_connected(session)))
        await asyncio.wait_for(steps[1].cancelled.wait(), 1)
        assert not reconnecting.done()
        assert prepare.cancelling() == 1
        closing = case.track(asyncio.create_task(case.registry._finalize_session(identity.session_id)))
        await asyncio.sleep(0)
        assert case.context.standby_requested
        assert not case.context.closed
        # Terminal cleanup must not repeatedly cancel a task whose provider
        # is already unwinding the epoch-retirement cancellation.
        assert prepare.cancelling() == 1
        steps[1].release.set()
        with pytest.raises(ValueError, match="media conversation is already closed"):
            await asyncio.wait_for(asyncio.shield(reconnecting), 1)
        await asyncio.wait_for(asyncio.shield(closing), 1)
        assert case.context.closed
        assert case.context.stream_epoch == case.identity.stream_epoch
        assert case.registry.context(identity.session_id) is None
        assert case.bridge.bridge.get(identity.session_id) is None
        assert case.context.turn_commit_task is None
        assert case.context.turn_commit_retry_task is None
        assert case.provider.closed
        assert not case.events("turn.committed")
        assert case.provider.reply_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("late_outcome", ["commit", "error"])
async def test_new_vad_serializes_retry_result_without_merging_turns(
    late_outcome: Literal["commit", "error"],
) -> None:
    steps = [_PrepareStep(), _PrepareStep(late_outcome)]
    async with _scenario(f"retry-new-vad-{late_outcome}", steps) as case:
        await case.entered(1)
        steps[0].release.set()
        await case.entered(2)
        old_handle, _ = _live_tail(case)
        arriving = case.track(asyncio.create_task(case.registry.on_speech_segment(
            case.session, _vad(case.identity, start=960),
        )))
        await asyncio.sleep(0)
        assert not arriving.done()
        assert case.context.active_vad_start_sample is None
        # The candidate is not yet admitted: the old successful result may
        # commit, but cannot be merged into the subsequently admitted input.
        steps[1].release.set()
        await asyncio.wait_for(asyncio.shield(arriving), 1)
        assert case.context.turn_start_sample == 960
        assert case.context.turn_endpoint_sample is None
        assert case.context.active_vad_start_sample == 960
        assert case.context.turn_commit_retry_task is None
        assert case.context.projection.provisional is not None
        assert case.context.projection.provisional.capture_start_sample == 960
        assert case.context.max_user_speech_task is not None
        assert old_handle.cancelled()
        assert case.context.asr.last_committed_sample == _RETIRE
        assert len(case.events("turn.committed")) == (late_outcome == "commit")


@pytest.mark.asyncio
async def test_new_vad_waiting_on_hung_retry_is_released_by_real_tail_expiry() -> None:
    steps = [_PrepareStep(), _PrepareStep()]
    async with _scenario("retry-blocks-new-vad", steps) as case:
        await case.entered(1)
        steps[0].release.set()
        await case.entered(2)
        _live_tail(case)
        arriving = case.track(asyncio.create_task(case.registry.on_speech_segment(
            case.session, _vad(case.identity, start=960),
        )))
        await asyncio.sleep(0)
        assert not arriving.done()
        assert case.context.active_vad_start_sample is None
        expiring = _expire_real_tail(case)
        await _must_settle(
            expiring, "real tail expiry is shielded behind hung retry; the new VAD cannot be admitted",
        )
        await _must_settle(arriving, "new VAD still waits on retired prepare work")
        assert steps[1].cancelled.is_set()
        assert not steps[1].release.is_set()
        assert not case.events("turn.committed")
        assert case.context.closed or (
            case.context.turn_start_sample == 960
            and case.context.active_vad_start_sample == 960
            and case.context.max_user_speech_task is not None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_kind", ["new_vad", "reconnect"])
async def test_old_tail_callback_cannot_detach_the_replacement_live_timer(
    replacement_kind: str,
) -> None:
    steps = [
        _PrepareStep(),
        _PrepareStep(ignore_cancel=replacement_kind == "reconnect"),
        _PrepareStep(),
    ]
    async with _scenario(f"retry-old-tail-{replacement_kind}", steps) as case:
        await case.entered(1)
        steps[0].release.set()
        await case.entered(2)
        old_handle, _ = _live_tail(case)
        identity, session = case.identity, case.session
        if replacement_kind == "reconnect":
            identity = replace(identity, stream_epoch=identity.stream_epoch + 1)
            session = case.bridge._open_connection(identity).session
            replacement = case.track(asyncio.create_task(case.registry.on_session_connected(session)))
            start, endpoint, retire = 0, _ENDPOINT, _RETIRE
        else:
            start, endpoint, retire = 960, 1560, 1600
            replacement = case.track(asyncio.create_task(case.registry.on_speech_segment(
                session, _vad(identity, start=start),
            )))
        await asyncio.sleep(0)
        assert not replacement.done()
        steps[1].release.set()
        await asyncio.wait_for(asyncio.shield(replacement), 1)
        assert old_handle.cancelled()
        if replacement_kind == "reconnect":
            await case.registry.on_speech_segment(session, _vad(identity, start=start))
        assert await case.registry.accept_asr_result(identity.session_id, ASRResult(
            task_epoch=2, sentence_id="replacement-final", revision=1,
            capture_start_sample=start, capture_end_sample=endpoint,
            text=_TEXT, is_final=True, stream_epoch=identity.stream_epoch,
        ))
        await case.registry.on_speech_segment(session, replace(
            _vad(identity, start=retire, final=True), voiced_end_sample=endpoint,
        ))
        await asyncio.wait_for(steps[2].entered.wait(), 1)
        new_tail = _live_tail(case)
        assert new_tail[0] is not old_handle
        before = asyncio.all_tasks()
        case.registry._start_endpoint_tail_expiry(
            case.identity.session_id, case.identity.stream_epoch, _ENDPOINT,
        )
        assert _live_tail(case) == new_tail
        assert asyncio.all_tasks() == before
        assert not case.context.closed and not case.context.standby_requested


@pytest.mark.asyncio
async def test_slightly_early_real_tail_callback_rearms_the_same_absolute_deadline() -> None:
    steps = [_PrepareStep(), _PrepareStep()]
    async with _scenario("retry-early-tail", steps) as case:
        await case.entered(1)
        steps[0].release.set()
        await case.entered(2)
        handle, deadline = _live_tail(case)
        # Advance both clocks only for this synchronous callback: no unrelated
        # loop task runs in the synthetic time window. The stored deadline and
        # resulting handle.when() stay in their original absolute clock domain.
        before = asyncio.all_tasks()
        handle.cancel()
        early_now = deadline - 0.001
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(media_session_turns, "time", SimpleNamespace(
                monotonic=lambda: early_now, monotonic_ns=time.monotonic_ns,
            ))
            patch.setattr(asyncio.get_running_loop(), "time", lambda: early_now)
            case.registry._start_endpoint_tail_expiry(
                case.identity.session_id, case.identity.stream_epoch, _ENDPOINT,
            )
            replacement, same_deadline = _live_tail(case)
        assert replacement is not handle
        assert same_deadline == deadline
        assert asyncio.all_tasks() == before
        assert not steps[1].cancelled.is_set()
        expiring = _expire_real_tail(case)
        await _must_settle(expiring, "the rearmed absolute deadline did not stop preparation")
        assert steps[1].cancelled.is_set()
        assert case.context.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_kind", ["prepare_complete", "new_vad", "bridge_epoch"])
async def test_due_tail_rechecks_its_owner_after_waiting_for_standby_lock(
    replacement_kind: str,
) -> None:
    outcome: Literal["commit", "error"] = (
        "commit" if replacement_kind == "prepare_complete" else "error"
    )
    steps = [_PrepareStep(), _PrepareStep(outcome)]
    async with _scenario(f"retry-tail-lock-{replacement_kind}", steps) as case:
        await case.entered(1)
        steps[0].release.set()
        await case.entered(2)
        retry = case.context.turn_commit_retry_task
        assert retry is not None
        await case.context.standby_lock.acquire()
        try:
            expiring = _expire_real_tail(case)
            await asyncio.sleep(0)
            assert not expiring.done()
            assert not case.context.standby_requested
            case.clock_patch.undo()
            if replacement_kind == "prepare_complete":
                steps[1].release.set()
                await asyncio.wait_for(asyncio.shield(retry), 1)
                await asyncio.wait_for(case.provider.reply_started.wait(), 1)
            elif replacement_kind == "new_vad":
                arriving = case.track(asyncio.create_task(case.registry.on_speech_segment(
                    case.session, _vad(case.identity, start=960),
                )))
                await asyncio.sleep(0)
                assert not arriving.done()
                steps[1].release.set()
                await asyncio.wait_for(asyncio.shield(arriving), 1)
                assert case.context.active_vad_start_sample == 960
            else:
                identity = replace(case.identity, stream_epoch=case.identity.stream_epoch + 1)
                session = case.bridge._open_connection(identity).session
                assert case.context.stream_epoch == case.identity.stream_epoch
                assert not steps[1].release.is_set()
        finally:
            case.context.standby_lock.release()
        await _must_settle(expiring, "obsolete tail close is still waiting after lock release")
        assert not case.context.standby_requested and not case.context.closed
        assert case.registry.metrics.get(
            "voice_conversation_standby_total", {"reason": "turn_prepare_timeout"},
        ) == 0
        if replacement_kind == "bridge_epoch":
            # Here the provider eventually returns. A separate test below
            # proves whether a permanently blocked old epoch is also retired.
            reconnecting = case.track(asyncio.create_task(case.registry.on_session_connected(session)))
            steps[1].release.set()
            await asyncio.wait_for(asyncio.shield(reconnecting), 1)
            assert case.context.stream_epoch == identity.stream_epoch
        assert len(case.events("turn.committed")) == (replacement_kind == "prepare_complete")


@pytest.mark.asyncio
@pytest.mark.parametrize(("blocked_call", "with_grace"), [(1, True), (2, False)])
async def test_reconnect_retires_hung_old_prepare_without_stale_tail_close(
    blocked_call: int, with_grace: bool,
) -> None:
    steps = [_PrepareStep(), _PrepareStep()]
    async with _scenario(
        f"retry-hung-during-reconnect-{blocked_call}", steps, with_grace=with_grace,
    ) as case:
        await case.entered(1)
        if blocked_call == 2:
            steps[0].release.set()
            await case.entered(2)
        _live_tail(case)
        if with_grace:
            assert case.context.owner_silence_task is None
            assert case.context.max_user_speech_task is None
            assert case.context.owner_silence_remaining_s == 0
            assert case.context.owner_silence_grace_used
        # Start the already-wired old timeout while a new transport is being
        # accepted, then exercise its lock-time fence with the provider blocked.
        await case.context.standby_lock.acquire()
        try:
            expiring = _expire_real_tail(case)
            await asyncio.sleep(0)
            assert not expiring.done()
            case.clock_patch.undo()
            identity = replace(case.identity, stream_epoch=case.identity.stream_epoch + 1)
            session = case.bridge._open_connection(identity).session
            reconnecting = case.track(asyncio.create_task(case.registry.on_session_connected(session)))
            await asyncio.sleep(0)
        finally:
            case.context.standby_lock.release()
        await _must_settle(expiring, "old tail close did not settle after epoch changed")
        await _must_settle(
            reconnecting,
            "tail veto left the old prepare holding turn_commit_lock after its absolute deadline",
        )
        assert steps[blocked_call - 1].cancelled.is_set()
        assert not steps[blocked_call - 1].release.is_set()
        assert case.context.stream_epoch == identity.stream_epoch
        assert case.context.turn_commit_retry_task is None
        assert case.context.turn_commit_task is None
        assert not case.events("turn.committed")
        assert case.provider.reply_calls == 0
        assert case.registry.metrics.get(
            "voice_conversation_standby_total", {"reason": "turn_prepare_timeout"},
        ) == 0
        if with_grace:
            # Epoch retirement must not buy another owner budget. Resuming
            # an already-spent one-shot grace legitimately closes for owner
            # silence, not for the obsolete preparation's tail deadline.
            await asyncio.wait_for(case.provider.close_finished.wait(), 1)
            assert case.context.closed
            assert case.context.standby_reason == "owner_silence_timeout"
            assert case.context.owner_silence_grace_used
        else:
            assert not case.context.standby_requested and not case.context.closed
            assert 0 <= case.context.owner_silence_remaining_s <= _BUDGET
