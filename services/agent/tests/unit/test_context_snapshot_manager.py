from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.orchestration.context_snapshot_manager import (
    ContextConflict,
    ContextSnapshot,
    ContextSnapshotDraft,
    ContextSnapshotManager,
    ContextTurn,
    MemoryCapsule,
    MemoryCapsuleEntry,
    PendingSnapshot,
    PersonaCapsule,
)
from services.agent.src.orchestration.interaction_plane import (
    InteractionEvent,
    InteractionSnapshot,
)


def _policy(*, tools: bool) -> ModePolicy:
    return ModePolicy.companion_for_test(
        policy_version="policy-v1",
        private_context=True,
        owner_evidence=True,
        tools=tools,
        voice_profile=False,
        shadow_low_sensitivity_persona=False,
    )


@pytest.mark.asyncio
async def test_vad_prefetch_prepares_snapshot_without_activating_it() -> None:
    runtime = DuplexRuntime.create(session_id="vad-context-prefetch")
    initial = runtime.orchestrator.context_snapshots.current(runtime.session_id)

    decision = runtime.decide_interaction(
        InteractionSnapshot(
            event=InteractionEvent.VAD_START,
            assistant_speaking=False,
            has_speech_energy=True,
        )
    )
    runtime.apply_interaction_decision(decision)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert runtime._pending_context_snapshot is not None
    assert runtime.orchestrator.context_snapshots.current(runtime.session_id) == initial
    await runtime.close()


@pytest.mark.asyncio
async def test_superseded_or_timed_out_snapshot_builder_cannot_accumulate() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    starts = 0

    async def blocked_builder(
        base: ContextSnapshot,
        _turns: tuple[ContextTurn, ...],
    ) -> ContextSnapshotDraft:
        nonlocal starts
        starts += 1
        build_number = starts
        if build_number == 1:
            started.set()
        try:
            await asyncio.Future()
        finally:
            if build_number == 1:
                cancelled.set()
        return ContextSnapshotDraft(memory_capsule=base.memory_capsule)

    runtime = DuplexRuntime.create(session_id="bounded-context-prefetch")
    runtime.context_snapshot_prepare_timeout_s = 10
    runtime.orchestrator.context_snapshots.builder = blocked_builder
    runtime._schedule_context_snapshot_prepare()
    await asyncio.wait_for(started.wait(), timeout=1)
    first_task = runtime._context_snapshot_prepare_task

    runtime._schedule_context_snapshot_prepare()
    await asyncio.wait_for(cancelled.wait(), timeout=0.1)
    await asyncio.sleep(0)

    assert first_task is not None and first_task.cancelled()
    assert starts == 2
    assert runtime._pending_context_snapshot is None
    await runtime.close()


@pytest.mark.asyncio
async def test_non_owner_snapshot_cannot_inherit_owner_private_context() -> None:
    manager = ContextSnapshotManager()
    owner = manager.seed_initial(
        "privacy-session",
        ContextSnapshotDraft(
            recent_committed_turns=(
                ContextTurn("user", "主人私密话题", "owner"),
                ContextTurn("assistant", "房间公开话题", "public"),
            ),
            memory_capsule=MemoryCapsule(
                (MemoryCapsuleEntry("private-memory", "fact", "主人住址"),)
            ),
            persona_capsule=PersonaCapsule("private-persona", 1, "主人专属陪伴方式"),
            relationship_policy=_policy(tools=True),
            tool_permission=True,
            speaker_class="owner",
            summary="主人私密摘要",
        ),
    )

    pending = await manager.prepare_next(
        "privacy-session",
        base_version=owner.version,
        committed_events=owner.recent_committed_turns,
        draft=ContextSnapshotDraft(
            recent_committed_turns=owner.recent_committed_turns,
            memory_capsule=owner.memory_capsule,
            persona_capsule=owner.persona_capsule,
            relationship_policy=owner.relationship_policy,
            tool_permission=True,
            speaker_class="guest",
            summary=owner.summary,
        ),
    )
    assert isinstance(pending, PendingSnapshot)
    guest = manager.activate(pending, expected_current_version=owner.version)

    assert isinstance(guest, ContextSnapshot)
    assert guest.recent_committed_turns == (ContextTurn("assistant", "房间公开话题"),)
    assert guest.memory_capsule.entries == ()
    assert guest.persona_capsule == PersonaCapsule()
    assert guest.summary == ""
    assert guest.tool_permission is False


@pytest.mark.asyncio
async def test_snapshot_is_deeply_immutable_and_activates_with_cas() -> None:
    manager = ContextSnapshotManager()
    initial = manager.initialize("session")
    draft = ContextSnapshotDraft(
        recent_committed_turns=(ContextTurn("user", "你好", "owner"),),
        memory_capsule=MemoryCapsule((MemoryCapsuleEntry("m1", "fact", "喜欢桂花", ("event-1",)),)),
        persona_capsule=PersonaCapsule("persona-v1", 1, "表达温和"),
        relationship_policy=_policy(tools=True),
        tool_permission=True,
        speaker_class="owner",
        summary="已完成问候",
    )

    pending = await manager.prepare_next(
        "session",
        base_version=initial.version,
        committed_events=draft.recent_committed_turns,
        draft=draft,
    )
    assert isinstance(pending, PendingSnapshot)
    activated = manager.activate(pending, expected_current_version=0)

    assert isinstance(activated, ContextSnapshot)
    assert activated.version == 1
    assert activated.tool_permission is True
    assert activated.memory_capsule.entries[0].content == "喜欢桂花"
    with pytest.raises(FrozenInstanceError):
        activated.version = 2  # type: ignore[misc]


@pytest.mark.asyncio
async def test_slow_or_failed_prepare_never_changes_current_snapshot() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_builder(
        base: ContextSnapshot,
        events: tuple[ContextTurn, ...],
    ) -> ContextSnapshotDraft:
        started.set()
        await release.wait()
        return ContextSnapshotDraft(
            recent_committed_turns=events,
            relationship_policy=base.relationship_policy,
        )

    manager = ContextSnapshotManager(builder=slow_builder)
    initial = manager.initialize("session")
    task = asyncio.create_task(
        manager.prepare_next(
            "session",
            base_version=0,
            committed_events=(ContextTurn("user", "慢构建"),),
        )
    )
    await started.wait()

    assert manager.current("session") is initial
    release.set()
    pending = await task
    assert isinstance(pending, PendingSnapshot)
    assert manager.current("session") is initial

    async def failed_builder(
        _base: ContextSnapshot,
        _events: tuple[ContextTurn, ...],
    ) -> ContextSnapshotDraft:
        raise RuntimeError("summary failed")

    manager.builder = failed_builder
    with pytest.raises(RuntimeError, match="summary failed"):
        await manager.prepare_next(
            "session",
            base_version=0,
            committed_events=(),
        )
    assert manager.current("session") is initial
    assert (
        manager.metrics.get(
            "context_snapshot_build_failed_total",
            {"reason": "builder"},
        )
        == 1
    )


@pytest.mark.asyncio
async def test_stale_pending_snapshot_cannot_overwrite_newer_policy() -> None:
    manager = ContextSnapshotManager()
    manager.initialize("session")
    allow = await manager.prepare_next(
        "session",
        base_version=0,
        committed_events=(),
        draft=ContextSnapshotDraft(
            relationship_policy=_policy(tools=True),
            tool_permission=True,
            speaker_class="owner",
        ),
    )
    deny = await manager.prepare_next(
        "session",
        base_version=0,
        committed_events=(),
        draft=ContextSnapshotDraft(
            relationship_policy=_policy(tools=False),
            tool_permission=False,
            speaker_class="owner",
        ),
    )
    assert isinstance(allow, PendingSnapshot)
    assert isinstance(deny, PendingSnapshot)
    assert isinstance(manager.activate(allow, expected_current_version=0), ContextSnapshot)

    conflict = manager.activate(deny, expected_current_version=0)

    assert isinstance(conflict, ContextConflict)
    assert manager.current("session").tool_permission is True


@pytest.mark.asyncio
async def test_runtime_freezes_context_version_per_generation_boundary() -> None:
    runtime = DuplexRuntime.create(session_id="context-version-runtime")
    first = await runtime.on_turn_committed("第一轮")
    first_version = runtime.orchestrator.context_version_for_fence(first)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    second = await runtime.on_turn_committed("第二轮")
    second_version = runtime.orchestrator.context_version_for_fence(second)

    assert first_version == 0
    assert second_version == 1
    assert runtime.orchestrator.context_version_for_fence(first) == first_version
    assert runtime.orchestrator.delegation.current_context_version(runtime.session_id) == 1

    unknown = GenerationFence(runtime.session_id, 999, 999, 999)
    with pytest.raises(ValueError, match="not bound"):
        runtime.orchestrator.context_version_for_fence(unknown)
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_turn_commit_does_not_wait_for_slow_snapshot_builder() -> None:
    runtime = DuplexRuntime.create(session_id="slow-context-runtime")
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_builder(
        base: ContextSnapshot,
        events: tuple[ContextTurn, ...],
    ) -> ContextSnapshotDraft:
        started.set()
        await release.wait()
        return ContextSnapshotDraft(
            recent_committed_turns=events,
            relationship_policy=base.relationship_policy,
            speaker_class=base.speaker_class,
        )

    runtime.orchestrator.context_snapshots.builder = slow_builder
    first = await runtime.on_turn_committed("第一轮")
    await asyncio.wait_for(started.wait(), timeout=1)

    second = await asyncio.wait_for(runtime.on_turn_committed("第二轮"), timeout=0.1)

    assert runtime.orchestrator.context_version_for_fence(first) == 0
    assert runtime.orchestrator.context_version_for_fence(second) == 0
    release.set()
    await runtime.close()
