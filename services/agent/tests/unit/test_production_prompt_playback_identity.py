"""Playback seam and identity task capture contract tests.

Covers the playback barrier, commit authority, identity task capture and
cancellation on subject switch, and the task drain."""

from __future__ import annotations

import asyncio
from datetime import (
    UTC,
    datetime,
)

import pytest
from services.agent.src.orchestration.task_manager import ToolSpec
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.transactional_effect_commit import (
    CommitReceipt,
    CommitReconcileResult,
    CommitReconcileState,
    EffectAuthorization,
    PreparedToolEffect,
    fence_fingerprint,
)
from services.agent.tests.unit.production_wiring_support import (
    _policy,
    _prepared_intent,
    _profile,
    _runtime,
    _speaker_decision,
)
from services.speaker.domain import SpeakerDecision


async def test_slow_playback_seam_keeps_barrier_visible_until_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-1: the drain barrier stays visible while the physical owners are
    still draining — concurrent output callbacks keep failing closed."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    seam_release = asyncio.Event()
    seam_started = asyncio.Event()

    async def _slow_seam() -> None:
        seam_started.set()
        await seam_release.wait()

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 第一轮。")
    runtime.set_playback_stop_seam(_slow_seam)
    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    commit_task = asyncio.create_task(_commit("B 第一轮。"))
    await asyncio.wait_for(seam_started.wait(), timeout=2)
    await asyncio.sleep(0)
    # Barrier still visible during the drain: outputs fail closed.
    assert runtime._pending_epoch_drain is not None
    assert runtime.gate_llm_token(runtime.fence, "blocked") is None
    assert runtime.gate_tts_audio(runtime.fence, b"blocked") is None
    assert runtime.gate_tool_result(runtime.fence, {"ok": True}) is None
    seam_release.set()
    await asyncio.wait_for(commit_task, timeout=2)
    assert runtime._pending_epoch_drain is None
    assert runtime.gate_llm_token(runtime.fence, "B 输出") == "B 输出"
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("awaitable_kind", ["future", "task"])
async def test_playback_seam_accepts_non_coroutine_awaitables(
    awaitable_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production LiveKit interrupt seam may return Future/Task, not a coroutine."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    await runtime.on_turn_committed("A 第一轮。", input_modality="text")

    def _future_seam() -> asyncio.Future[None]:
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    async def _finished() -> None:
        return None

    def _task_seam() -> asyncio.Task[None]:
        return asyncio.create_task(_finished())

    runtime.set_playback_stop_seam(
        _future_seam if awaitable_kind == "future" else _task_seam
    )
    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None

    await runtime.on_turn_committed("B 第一轮。", input_modality="text")

    assert runtime._pending_epoch_drain is None
    assert [turn.content for turn in runtime.orchestrator.context.turns] == [
        "B 第一轮。"
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_hanging_playback_seam_times_out_and_aborts_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-2/P0-3: a permanently hanging playback seam hits the bounded
    deadline; the barrier stays, metrics/log fire and the current turn is
    aborted — no generation, playback, side effect or committed state."""

    runtime = _runtime(monkeypatch)
    runtime.orchestrator.epoch_drain_timeout_s = 0.05
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    orch = runtime.orchestrator

    async def _hanging_seam() -> None:
        await asyncio.Event().wait()

    await runtime.on_turn_committed("A 第一轮。", input_modality="text")
    runtime.set_playback_stop_seam(_hanging_seam)
    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    fence_after_apply = runtime.fence
    with pytest.raises(RuntimeError):
        await runtime.on_turn_committed("B 第一轮。", input_modality="text")
    # The turn was aborted: no committed state, no fence advance, no output.
    assert runtime._pending_epoch_drain is not None
    assert runtime.fence.matches(fence_after_apply)
    # Old-subject context was cleared by the rotation; B never committed.
    assert [turn.content for turn in runtime.orchestrator.context.turns] == []
    assert runtime.gate_llm_token(runtime.fence, "x") is None
    assert runtime.gate_tts_audio(runtime.fence, b"x") is None
    assert runtime.gate_tool_result(runtime.fence, {"ok": True}) is None
    assert (
        orch.metrics.get("runtime_profile_refresh_failures_total", {"reason": "drain_timeout"}) >= 1
    )
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        "session_id",
        "turn_id",
        "generation_id",
        "tool_epoch",
        "session_epoch",
    ],
)
async def test_commit_authority_requires_full_fence_match(
    mutate: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the exact-fence CAS compares EVERY fence field — a same-epoch old
    turn/generation or a forged session can never pass the side-effect gate."""

    from services.agent.src.identity_state import exact_fence_commit_authority

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    current = runtime.fence
    assert (
        exact_fence_commit_authority(runtime, current, "memory_recall_private", {"receipt": "r"})
        is True
    )

    from dataclasses import replace

    altered = replace(
        current,
        **(
            {"session_id": "ses_other"}
            if mutate == "session_id"
            else {mutate: getattr(current, mutate) + 1}
        ),
    )
    assert (
        exact_fence_commit_authority(runtime, altered, "memory_recall_private", {"receipt": "r"})
        is False
    )


@pytest.mark.asyncio
async def test_commit_port_blocks_then_real_bump_denies_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-TOCTOU: the port blocks after prepare; the runtime REALLY advances
    via the real APIs (generation bump / turn commit / higher-epoch profile);
    each round awaits the task and resets independent events."""

    import time
    from types import SimpleNamespace

    from services.agent.src.identity_state import exact_fence_commit_authority
    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("第一轮。", input_modality="text")
    evidence = SimpleNamespace(expires_at=datetime(2099, 1, 1, tzinfo=UTC))

    for scenario in ("generation", "turn", "epoch"):
        commits = 0
        blocked = asyncio.Event()
        release = asyncio.Event()

        class Port:
            async def commit_prepared(
                self,
                *,
                spec: object,
                authorization: EffectAuthorization,
                prepared: PreparedToolEffect,
                idempotency_key: str,
                now: object,
                _blocked: asyncio.Event = blocked,
                _release: asyncio.Event = release,
            ) -> CommitReceipt | None:
                nonlocal commits
                _blocked.set()
                await _release.wait()
                if not exact_fence_commit_authority(
                    runtime,
                    authorization.fence,
                    authorization.capability,
                    evidence,
                ):
                    return None
                commits += 1
                return CommitReceipt(
                    intent_id=f"intent-{idempotency_key}",
                    idempotency_key=idempotency_key,
                    committed_at=datetime.now(UTC),
                    fence_fingerprint=fence_fingerprint(authorization.fence),
                    authority_revision=1,
                    capability=authorization.capability,
                    purpose=authorization.purpose,
                    resource_id=authorization.resource_id,
                    intent_sha256=prepared.payload_sha256,
                    evidence_refs=authorization.evidence_refs,
                )

            async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
                del idempotency_key
                return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

        tm = TaskManager()
        tm.set_effect_commit_port(Port())
        tm.register_side_effect(
            ToolSpec(
                name="fenced_write",
                description="t",
                input_schema={},
                cancellable=True,
                idempotent=True,
                timeout_s=5,
                contains_sensitive_data=True,
                side_effect_policy="idempotent",
                required_capability="memory_capture",
                required_purpose="memory_capture",
            ),
            _prepared_intent,
        )
        effect = EffectAuthorization(
            fence=runtime.fence,
            capability="memory_capture",
            purpose="memory_capture",
            resource_id="memory:subject-scoped",
            evidence_refs=("receipt_memory_1",),
            action_id=f"fenced-write-{scenario}",
        )
        rec = await tm.start(
            "fenced_write",
            {},
            runtime.fence,
            task_epoch=0,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 60_000,
            side_effect_policy="idempotent",
            committed=True,
            effect=effect,
        )
        await asyncio.wait_for(blocked.wait(), timeout=2)
        assert not rec.task.done()
        before = runtime.fence
        if scenario == "generation":
            await runtime.preempt_media_output(
                cause="test-generation-bump",
                synchronized_transcript=None,
            )
            after = runtime.fence
            assert after.generation_id == before.generation_id + 1
            assert after.turn_id == before.turn_id
            assert after.session_epoch == before.session_epoch
            assert after.tool_epoch == before.tool_epoch
        elif scenario == "turn":
            await runtime.orchestrator.commit_turn(
                "下一轮。",
                speaker_scope="public",
            )
            after = runtime.fence
            assert after.turn_id == before.turn_id + 1
            assert after.generation_id == before.generation_id + 1
            assert after.session_epoch == before.session_epoch
            assert after.tool_epoch == before.tool_epoch
        else:
            applied = runtime.apply_runtime_profile(_profile(epoch=3))
            assert applied is not None
            after = runtime.fence
            assert after.session_epoch == 3
            assert after.turn_id == before.turn_id
            assert after.generation_id == before.generation_id
            assert after.tool_epoch == before.tool_epoch
        release.set()
        await asyncio.wait_for(rec.task, timeout=2)
        assert rec.finished
        assert commits == 0
        assert rec.cancelled is True
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,epoch_field",
    [
        ("_context_snapshot_prepare_task", "_context_snapshot_prepare_epoch"),
        ("_speaker_classification_task", "_speaker_epoch"),
        ("_voice_profile_refresh_task", "_speaker_epoch"),
    ],
)
async def test_identity_tasks_are_captured_cancelled_and_void_after_switch(
    field: str,
    epoch_field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: identity-owned background tasks are captured before the reset and
    bounded-cancelled by the drain; after the switch their late bodies cannot
    run (cancelled) and the bumped epochs void any late completion."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 第一轮。")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _late_writer() -> None:
        started.set()
        await release.wait()
        # Would write an identity-private field on the new subject.
        setattr(runtime, f"{field}_late_write", True)

    task = asyncio.create_task(_late_writer())
    setattr(runtime, field, task)
    await asyncio.sleep(0)
    assert started.is_set() and not task.done()

    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    # The fake task was captured (other real background tasks may be too);
    # the drain (inside the next commit) bounded-cancels them.
    assert runtime._rotation_captured_tasks is not None
    assert task in runtime._rotation_captured_tasks
    await _commit("B 第一轮。")
    assert runtime._rotation_captured_tasks is None
    assert task.cancelled()
    release.set()
    await asyncio.sleep(0)
    # The late body never ran: no private field was written back.  (The
    # runtime may already have spawned a fresh task for the new subject.)
    assert not hasattr(runtime, f"{field}_late_write")
    await runtime.close()


@pytest.mark.asyncio
async def test_cancel_swallowing_identity_task_stays_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: a cancel-swallowing identity task makes the drain time out; the
    barrier stays fail-closed, and even after the task late-completes the
    bumped identity epochs void the write (no pending snapshot or speaker
    decision lands on the new subject)."""

    runtime = _runtime(monkeypatch)
    runtime.orchestrator.epoch_drain_timeout_s = 0.05
    runtime.set_mode_policy(_policy(_profile(epoch=1)))

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 第一轮。")
    old_prepare_epoch = runtime._context_snapshot_prepare_epoch
    old_speaker_epoch = runtime._speaker_epoch
    started = asyncio.Event()
    release = asyncio.Event()

    async def _swallow_and_late_complete() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass  # swallow the cancellation, then late-complete
        await release.wait()
        # Late writes replicate the production epoch checks.
        if runtime._context_snapshot_prepare_epoch == old_prepare_epoch:
            runtime._pending_context_snapshot = object()
        if runtime._speaker_epoch == old_speaker_epoch:
            runtime._speaker_decision = object()  # type: ignore[assignment]

    stubborn = asyncio.create_task(_swallow_and_late_complete())
    runtime._context_snapshot_prepare_task = stubborn
    await asyncio.sleep(0)
    assert started.is_set()

    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    assert stubborn in (runtime._rotation_captured_tasks or [])
    with pytest.raises(RuntimeError):
        await _commit("B 第一轮。")  # drain times out -> turn aborted
    # Barrier stays fail-closed; the late complete cannot write back.
    assert runtime._pending_epoch_drain is not None
    release.set()
    await asyncio.wait_for(stubborn, timeout=2)
    assert runtime._pending_context_snapshot is None
    assert runtime._speaker_decision is None
    assert (
        runtime.orchestrator.metrics.get(
            "runtime_profile_refresh_failures_total", {"reason": "drain_timeout"}
        )
        >= 1
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_real_context_snapshot_prepare_late_draft_is_void_after_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the REAL ``_schedule_context_snapshot_prepare`` producer with a
    controllable ``prepare_next`` adapter; after the switch the bumped
    prepare epoch voids the late draft (it can never land on the new
    subject's pending snapshot)."""

    from services.agent.src.orchestration.context_snapshot_manager import (
        PendingSnapshot,
    )

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("A 第一轮。", input_modality="text")

    started = asyncio.Event()
    release = asyncio.Event()
    manager = runtime.orchestrator.context_snapshots

    async def _blocking_prepare(*_args: object, **_kwargs: object) -> object:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass  # late complete after the switch
        base = manager.current(runtime.session_id)
        return PendingSnapshot(runtime.session_id, base.version, base, 0.0)

    # The manager is a frozen dataclass; patch the class method instead.
    monkeypatch.setattr(type(manager), "prepare_next", _blocking_prepare)
    runtime._schedule_context_snapshot_prepare()
    await asyncio.wait_for(started.wait(), timeout=2)
    task = runtime._context_snapshot_prepare_task
    assert task is not None and not task.done()

    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    assert task in (runtime._rotation_captured_tasks or [])
    release.set()
    await asyncio.wait_for(task, timeout=2)
    # The prepare epoch was bumped during the rotation: the late draft cannot
    # be written into the new subject's pending snapshot.
    assert runtime._pending_context_snapshot is None
    assert runtime._pending_context_snapshot_epoch is None
    await runtime.close()


@pytest.mark.asyncio
async def test_real_speaker_classification_late_decision_is_void_after_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the REAL ``_classify_speaker`` producer with a controllable
    classifier; after the switch the bumped speaker epoch voids the late
    owner decision (private speaker state never lands on the new subject)."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("A 第一轮。", input_modality="text")

    started = asyncio.Event()
    release = asyncio.Event()

    async def _classifier(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass  # late complete after the switch
        return _speaker_decision()  # owner decision

    runtime._speaker_classifier = _classifier
    runtime._speaker_pcm = bytearray(b"\x00\x01")
    runtime._start_speaker_classification()
    await asyncio.wait_for(started.wait(), timeout=2)
    task = runtime._speaker_classification_task
    assert task is not None and not task.done()

    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    release.set()
    await asyncio.wait_for(task, timeout=2)
    # The speaker epoch was bumped: the late owner decision cannot land.
    assert runtime._speaker_decision is None
    assert runtime._speaker_class == "uncertain"
    await runtime.close()


@pytest.mark.asyncio
async def test_real_voice_profile_refresh_is_captured_and_dropped_on_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the REAL ``refresh_voice_profile`` task is captured by the
    rotation and its reference is dropped, so a late personal-voice result
    can never be applied for the new subject."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    await runtime.orchestrator.ready()

    started = asyncio.Event()
    release = asyncio.Event()

    async def _refresher() -> dict[str, object]:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass  # late complete after the switch
        return {
            "profile_id": "vo_personal_new",
            "voice_profile_id": "vo_personal_new",
            "profile_version": 1,
        }

    runtime.set_voice_profile_refresher(_refresher)
    task = runtime.refresh_voice_profile()
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=2)
    assert not task.done()

    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    assert task in (runtime._rotation_captured_tasks or [])
    release.set()
    await asyncio.wait_for(task, timeout=2)
    # The refresher reference was dropped during the rotation: nothing can
    # consume the late personal-voice result for the new subject.
    assert runtime._voice_profile_refresh_task is None
    await runtime.close()


async def test_production_wiring_has_no_commit_port_and_side_effects_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the real agent wiring installs NO transactional effect commit port
    yet, so every side-effect registration fails closed while the real
    read-only planner tool registers and runs normally."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))

    class _PlannerClient:
        async def fetch(self, **_kwargs: object) -> object:
            from services.agent.src.response_planner_client import ResponsePlanFetch

            return ResponsePlanFetch(None, "no_profile")

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=_PlannerClient(),  # type: ignore[arg-type]
    )
    tm = runtime.orchestrator.task_manager
    # Production default: no persistent commit port -> side effects denied.
    assert tm._effect_commit_port is None
    with pytest.raises(PermissionError):
        tm.register(
            ToolSpec(
                name="w",
                description="t",
                input_schema={},
                cancellable=True,
                idempotent=True,
                timeout_s=5,
                contains_sensitive_data=True,
                side_effect_policy="idempotent",
                required_capability="memory_capture",
                required_purpose="memory_capture",
            ),
            lambda *_: None,
        )
    assert "w" not in tm.specs
    # The real agent registration path installs the read-only planner tool.
    assert agent._register_response_planner_tool() is True
    assert tm.specs["response_planner"]
    await runtime.close()


async def test_cancel_swallowing_task_drain_timeout_stays_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-1: a task that swallows CancelledError must not hang the drain:
    the bounded timeout is observable, metrics/log fire, and the new epoch
    stays fail-closed (no sensitive output until a successful drain)."""

    runtime = _runtime(monkeypatch)
    runtime.orchestrator.epoch_drain_timeout_s = 0.05
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    orch = runtime.orchestrator

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 第一轮。")
    started = asyncio.Event()
    release = asyncio.Event()

    async def _swallow_cancel() -> None:
        started.set()
        while True:
            try:
                await release.wait()
                break
            except asyncio.CancelledError:
                # Malicious/faulty handler: swallows EVERY cancellation.
                continue

    stubborn_llm = asyncio.create_task(_swallow_cancel())
    orch.set_active_llm_task(stubborn_llm)
    await asyncio.sleep(0)
    assert started.is_set() and not stubborn_llm.done()

    applied = runtime.apply_runtime_profile(
        _profile(epoch=2, subject="person_parent", mode="adult_companion")
    )
    assert applied is not None
    assert stubborn_llm.cancelling() or stubborn_llm.cancelled()
    with pytest.raises(RuntimeError):
        await _commit("B 第一轮。")  # drain fails -> the turn is aborted

    # Drain failed observably: barrier remains, fail-closed, metric recorded
    # and no committed state advanced.
    assert runtime._pending_epoch_drain is not None
    assert runtime.fence.session_epoch == 2
    assert runtime.gate_llm_token(runtime.fence, "敏感输出") is None
    assert runtime.gate_tts_audio(runtime.fence, b"pcm") is None
    assert runtime.gate_tool_result(runtime.fence, {"ok": True}) is None
    assert (
        runtime.orchestrator.metrics.get(
            "runtime_profile_refresh_failures_total", {"reason": "drain_timeout"}
        )
        == 1
    )
    release.set()
    await asyncio.sleep(0)
    await runtime.close()


@pytest.mark.asyncio
async def test_drain_stops_production_playback_owner_and_clears_queued_pcm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-1: drain calls the production playback owner (LiveKit stop seam)
    and clears queued PCM, not just the in-process controller flag."""

    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(epoch=1)))
    orch = runtime.orchestrator
    stopped_owners: list[str] = []

    async def _production_stop() -> None:
        stopped_owners.append("livekit_handle")
        orch.playback.pcm_played.clear()
        orch.playback.playing = False

    runtime.set_playback_stop_seam(_production_stop)
    orch.playback.start()
    orch.playback.push_pcm(b"\x01\x00" * 64)
    assert orch.playback.playing is True
    assert bytes(orch.playback.pcm_played) == b"\x01\x00" * 64

    async def _commit(text: str) -> None:
        await runtime.on_turn_committed(text, input_modality="text")

    await _commit("A 第一轮。")
    assert stopped_owners == ["livekit_handle"]
    assert orch.playback.playing is False
    assert not orch.playback.pcm_played
    assert runtime._pending_epoch_drain is None
    await runtime.close()


def test_non_cancellable_side_effect_tool_registration_is_rejected() -> None:
    """P0-2: non-cancellable tools must be NONE/READ_ONLY and non-sensitive,
    otherwise they are rejected at registration (never exposed to runtime)."""

    from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec

    tm = TaskManager()
    for side_effect, sensitive in (
        ("idempotent", False),
        ("high_risk", False),
        ("read_only", True),
        ("none", True),
    ):
        with pytest.raises(PermissionError):
            tm.register(
                ToolSpec(
                    name=f"bad-{side_effect}-{sensitive}",
                    description="must be rejected",
                    input_schema={},
                    cancellable=False,
                    idempotent=False,
                    timeout_s=5,
                    contains_sensitive_data=sensitive,
                    side_effect_policy=side_effect,
                ),
                lambda *_: None,
            )
    # Without a commit authority, even a cancellable side-effect tool is
    # rejected: the runtime never carries unguarded external writes.
    with pytest.raises(PermissionError):
        tm.register(
            ToolSpec(
                name="bad-cancellable-write",
                description="must be rejected without authority",
                input_schema={},
                cancellable=True,
                idempotent=True,
                timeout_s=5,
                contains_sensitive_data=True,
                side_effect_policy="idempotent",
            ),
            lambda *_: None,
        )
    # A compliant non-cancellable read-only tool registers fine.
    tm.register(
        ToolSpec(
            name="safe-probe",
            description="read-only probe",
            input_schema={},
            cancellable=False,
            idempotent=False,
            timeout_s=5,
            contains_sensitive_data=False,
            side_effect_policy="read_only",
        ),
        lambda *_: None,
    )
    assert "safe-probe" in tm.specs

