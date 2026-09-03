from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.delegation_coordinator import (
    DelegationCoordinator,
    DelegationEventKind,
    DelegationRequest,
    OutputIntentAdmission,
    SideEffectPolicy,
)
from services.agent.src.orchestration.interaction_plane import (
    InteractionEvent,
    InteractionPlane,
    InteractionSnapshot,
)
from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec
from services.agent.src.prompts import (
    BRIDGE_PHRASES,
    DEVICE_WAKE_PHRASES,
    SPEAKER_ENROLLMENT_SAMPLE_PROMPTS,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2


def _fence() -> GenerationFence:
    return GenerationFence("session", 1, 2, 3)


def _request(*, now_ms: int = 1_000, committed: bool = True) -> DelegationRequest:
    return DelegationRequest(
        tool_name="search",
        arguments={"query": "天气"},
        fence=_fence(),
        task_epoch=4,
        context_version=5,
        expires_at_ms=9_999_999_999_999,
        side_effect_policy=SideEffectPolicy.READ_ONLY,
        committed=committed,
    )


async def _coordinator_with_result(result: object) -> DelegationCoordinator:
    manager = TaskManager()

    async def search(
        _args: dict[str, object],
        _cancel: asyncio.Event,
    ) -> object:
        return result

    manager.register(
        ToolSpec(
            "search",
            "",
            {},
            True,
            True,
            1,
            side_effect_policy=SideEffectPolicy.READ_ONLY.value,
        ),
        search,
    )
    return DelegationCoordinator(manager)


@pytest.mark.asyncio
async def test_delegation_events_and_output_intent_use_every_gate() -> None:
    coordinator = await _coordinator_with_result({"summary": "今天晴。"})
    request = _request()
    handle = await coordinator.delegate(request)
    events = [event async for event in coordinator.events(handle)]

    assert [event.kind for event in events] == [
        DelegationEventKind.STARTED,
        DelegationEventKind.RESULT_CANDIDATE,
    ]
    intent = coordinator.output_intent(
        handle,
        current_fence=request.fence,
        current_task_epoch=request.task_epoch,
        current_context_version=request.context_version,
        relevant=True,
        now_ms=1_001,
    )
    assert intent is not None
    assert intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT
    assert intent.tts_source == "今天晴。"
    assert intent.context_version == request.context_version
    assert (
        coordinator.output_intent(
            handle,
            current_fence=request.fence,
            current_task_epoch=request.task_epoch,
            current_context_version=request.context_version,
            relevant=True,
            now_ms=1_002,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_gate", ["fence", "task", "context", "expiry", "relevance"])
async def test_stale_deep_result_cannot_form_output_intent(stale_gate: str) -> None:
    coordinator = await _coordinator_with_result({"summary": "候选结果。"})
    request = _request()
    handle = await coordinator.delegate(request)
    await handle.record.task
    kwargs = {
        "current_fence": request.fence,
        "current_task_epoch": request.task_epoch,
        "current_context_version": request.context_version,
        "relevant": True,
        "now_ms": 1_001,
    }
    if stale_gate == "fence":
        kwargs["current_fence"] = request.fence.bump_generation()
    elif stale_gate == "task":
        kwargs["current_task_epoch"] = request.task_epoch + 1
    elif stale_gate == "context":
        kwargs["current_context_version"] = request.context_version + 1
    elif stale_gate == "expiry":
        kwargs["now_ms"] = request.expires_at_ms + 1
    else:
        kwargs["relevant"] = False

    assert coordinator.output_intent(handle, **kwargs) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_deep_result_delivery_outlives_request_expiry() -> None:
    """A spoken answer must not expire while it is still being delivered."""

    coordinator = await _coordinator_with_result({"summary": "最快的是 G7001 次。"})
    base_ms = int(time.time() * 1_000)
    request = replace(_request(), expires_at_ms=base_ms + 20_000)
    handle = await coordinator.delegate(request)
    await handle.record.task
    intent = coordinator.output_intent(
        handle,
        current_fence=request.fence,
        current_task_epoch=request.task_epoch,
        current_context_version=request.context_version,
        relevant=True,
        now_ms=base_ms + 5_000,
    )
    assert intent is not None
    assert intent.expires_at_ms >= base_ms + 5_000 + 120_000

    spoken = coordinator.admit_output_intent(
        intent,
        current_fence=request.fence,
        current_context_version=request.context_version,
        floor_allows_output=True,
        now_ms=base_ms + 5_100,
    )
    assert spoken == "最快的是 G7001 次。"
    assert (
        coordinator.output_intent_is_selected(
            intent,
            current_fence=request.fence,
            current_context_version=request.context_version,
            floor_allows_output=True,
            now_ms=base_ms + 25_000,
        )
        is True
    )
    assert (
        coordinator.output_intent_is_selected(
            intent,
            current_fence=request.fence,
            current_context_version=request.context_version,
            floor_allows_output=True,
            now_ms=intent.expires_at_ms + 1,
        )
        is False
    )


@pytest.mark.asyncio
async def test_cancelled_late_tool_result_cannot_form_new_generation_output() -> None:
    manager = TaskManager()

    async def late_search(
        _args: dict[str, object],
        cancel_event: asyncio.Event,
    ) -> dict[str, str]:
        # Model a cooperative provider that finishes after the cancellation
        # signal, which is still a late result and must not be spoken.
        await cancel_event.wait()
        return {"summary": "旧 generation 的迟到结果"}

    manager.register(
        ToolSpec(
            "search",
            "",
            {},
            True,
            True,
            1,
            side_effect_policy=SideEffectPolicy.READ_ONLY.value,
        ),
        late_search,
    )
    coordinator = DelegationCoordinator(manager)
    request = _request()
    handle = await coordinator.delegate(request)

    await manager.cancel_cancellable(request.fence)
    await handle.record.task

    assert handle.record.cancelled is True
    assert handle.record.result == {"summary": "旧 generation 的迟到结果"}
    assert (
        coordinator.output_intent(
            handle,
            current_fence=request.fence.bump_generation(),
            current_task_epoch=request.task_epoch,
            current_context_version=request.context_version,
            relevant=True,
            now_ms=1_001,
        )
        is None
    )


@pytest.mark.asyncio
async def test_tool_timeout_does_not_block_realtime_interaction_plane() -> None:
    manager = TaskManager()

    async def slow(_args: dict[str, object], _cancel: asyncio.Event) -> None:
        await asyncio.sleep(1)

    manager.register(
        ToolSpec(
            "search",
            "",
            {},
            True,
            True,
            0.01,
            side_effect_policy=SideEffectPolicy.READ_ONLY.value,
        ),
        slow,
    )
    coordinator = DelegationCoordinator(manager)
    handle = await coordinator.delegate(_request())

    decision = InteractionPlane().decide(
        InteractionSnapshot(
            event=InteractionEvent.VAD_START,
            assistant_speaking=True,
        )
    )
    assert decision.duck_output
    events = [event async for event in coordinator.events(handle)]
    assert events[-1].kind is DelegationEventKind.RESULT_CANDIDATE
    assert events[-1].candidate == {"error": "tool_timeout", "tool": "search"}


@pytest.mark.asyncio
async def test_high_risk_delegation_requires_committed_turn() -> None:
    coordinator = await _coordinator_with_result("ok")
    request = replace(
        _request(committed=False),
        side_effect_policy=SideEffectPolicy.HIGH_RISK,
    )
    with pytest.raises(PermissionError):
        await coordinator.delegate(request)


@pytest.mark.asyncio
async def test_high_risk_delegation_requires_explicit_user_confirmation() -> None:
    coordinator = await _coordinator_with_result("ok")
    # ToolSpec is frozen: replace the registered spec with an immutable
    # HIGH_RISK spec (never mutate the original's permission boundary).
    coordinator.task_manager.specs["search"] = replace(
        coordinator.task_manager.specs["search"],
        side_effect_policy=SideEffectPolicy.HIGH_RISK.value,
    )
    request = replace(
        _request(committed=True),
        side_effect_policy=SideEffectPolicy.HIGH_RISK,
    )
    with pytest.raises(PermissionError, match="explicit user confirmation"):
        await coordinator.delegate(request)


def test_bridge_acknowledgement_is_allowlist_only() -> None:
    intent = DelegationCoordinator.bridge_acknowledgement(
        BRIDGE_PHRASES[0],
        fence=_fence(),
        context_version=5,
        expires_at_ms=2_000,
        now_ms=1_000,
    )
    assert intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT
    wake = DelegationCoordinator.bridge_acknowledgement(
        DEVICE_WAKE_PHRASES[0],
        fence=_fence(),
        context_version=5,
        expires_at_ms=2_000,
        now_ms=1_000,
    )
    assert wake.tts_source == DEVICE_WAKE_PHRASES[0]
    enroll = DelegationCoordinator.bridge_acknowledgement(
        SPEAKER_ENROLLMENT_SAMPLE_PROMPTS[0],
        fence=_fence(),
        context_version=5,
        expires_at_ms=2_000,
        now_ms=1_000,
    )
    assert enroll.tts_source == SPEAKER_ENROLLMENT_SAMPLE_PROMPTS[0]
    with pytest.raises(ValueError):
        DelegationCoordinator.bridge_acknowledgement(
            "查询已经成功，结果一定正确。",
            fence=_fence(),
            context_version=5,
            expires_at_ms=2_000,
            now_ms=1_000,
        )


def test_conversation_reply_intent_is_admitted_and_consumed() -> None:
    coordinator = DelegationCoordinator(TaskManager())
    observations: list[OutputIntentAdmission] = []
    coordinator.set_output_intent_observer(observations.append)
    fence = _fence()
    intent = DelegationCoordinator.conversation_reply(
        fence=fence,
        context_version=5,
        expires_at_ms=2_000,
        now_ms=1_000,
    )

    assert intent.kind == media_pb2.OUTPUT_INTENT_KIND_CONVERSATION_REPLY
    assert (
        coordinator.admit_output_intent(
            intent,
            current_fence=fence,
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_001,
        )
        == ""
    )
    assert observations[-1].accepted is True
    assert observations[-1].consumed is False
    assert observations[-1].selected is True
    assert observations[-1].authoritative_candidate.intent_id == intent.intent_id

    assert coordinator.complete_output_intent(
        intent,
        current_fence=fence,
        current_context_version=5,
        floor_allows_output=True,
        now_ms=1_002,
    )
    assert observations[-1].accepted is False
    assert observations[-1].consumed is True
    assert observations[-1].reason == "completed"
    assert observations[-1].authoritative_candidate is None
    assert observations[-1].authoritative_candidates == ()
    assert not coordinator.complete_output_intent(
        intent,
        current_fence=fence,
        current_context_version=5,
        floor_allows_output=True,
        now_ms=1_003,
    )


@pytest.mark.asyncio
async def test_newer_task_epoch_invalidates_an_older_completed_result() -> None:
    coordinator = await _coordinator_with_result("旧结果")
    old_request = _request()
    old = await coordinator.delegate(old_request)
    await old.record.task
    new_request = replace(old_request, task_epoch=old_request.task_epoch + 1)
    new = await coordinator.delegate(new_request)
    await new.record.task

    assert (
        coordinator.output_intent(
            old,
            current_fence=old_request.fence,
            current_task_epoch=old_request.task_epoch,
            current_context_version=old_request.context_version,
            relevant=True,
            now_ms=1_001,
        )
        is None
    )


@pytest.mark.asyncio
async def test_dropped_output_is_one_shot_and_cannot_revive() -> None:
    coordinator = await _coordinator_with_result("候选结果")
    request = _request()
    handle = await coordinator.delegate(request)
    await handle.record.task

    assert (
        coordinator.output_intent(
            handle,
            current_fence=request.fence,
            current_task_epoch=request.task_epoch,
            current_context_version=request.context_version,
            relevant=False,
            now_ms=1_001,
        )
        is None
    )
    assert (
        coordinator.output_intent(
            handle,
            current_fence=request.fence,
            current_task_epoch=request.task_epoch,
            current_context_version=request.context_version,
            relevant=True,
            now_ms=1_002,
        )
        is None
    )


@pytest.mark.asyncio
async def test_uncommitted_or_sensitive_result_never_becomes_output() -> None:
    coordinator = await _coordinator_with_result("未提交结果")
    request = _request(committed=False)
    handle = await coordinator.delegate(request)
    await handle.record.task
    assert (
        coordinator.output_intent(
            handle,
            current_fence=request.fence,
            current_task_epoch=request.task_epoch,
            current_context_version=request.context_version,
            relevant=True,
            now_ms=1_001,
        )
        is None
    )

    manager = TaskManager()

    async def sensitive(_args: dict[str, object], _cancel: asyncio.Event) -> str:
        return "敏感结果"

    manager.register(
        ToolSpec(
            "sensitive",
            "",
            {},
            True,
            True,
            1,
            contains_sensitive_data=True,
            side_effect_policy=SideEffectPolicy.READ_ONLY.value,
        ),
        sensitive,
    )
    sensitive_coordinator = DelegationCoordinator(manager)
    sensitive_request = replace(request, tool_name="sensitive", committed=True)
    sensitive_handle = await sensitive_coordinator.delegate(sensitive_request)
    await sensitive_handle.record.task
    assert (
        sensitive_coordinator.output_intent(
            sensitive_handle,
            current_fence=sensitive_request.fence,
            current_task_epoch=sensitive_request.task_epoch,
            current_context_version=sensitive_request.context_version,
            relevant=True,
            now_ms=1_001,
        )
        is None
    )


@pytest.mark.asyncio
async def test_failed_task_emits_failed_once() -> None:
    manager = TaskManager()

    async def fail(_args: dict[str, object], _cancel: asyncio.Event) -> None:
        raise RuntimeError("boom")

    manager.register(
        ToolSpec(
            "search",
            "",
            {},
            True,
            True,
            1,
            side_effect_policy=SideEffectPolicy.READ_ONLY.value,
        ),
        fail,
    )
    coordinator = DelegationCoordinator(manager)
    handle = await coordinator.delegate(_request())
    events = [event async for event in coordinator.events(handle)]
    assert [event.kind for event in events] == [
        DelegationEventKind.STARTED,
        DelegationEventKind.FAILED,
    ]
    with pytest.raises(RuntimeError, match="one-shot"):
        _ = [event async for event in coordinator.events(handle)]


@pytest.mark.asyncio
async def test_side_effect_policy_cannot_be_self_reported() -> None:
    coordinator = await _coordinator_with_result("ok")
    request = replace(
        _request(),
        side_effect_policy=SideEffectPolicy.HIGH_RISK,
    )
    with pytest.raises(PermissionError, match="does not match"):
        await coordinator.delegate(request)


@pytest.mark.asyncio
async def test_output_intent_consumer_rechecks_floor_fence_context_and_expiry() -> None:
    coordinator = await _coordinator_with_result("当前结果")
    observations: list[OutputIntentAdmission] = []
    coordinator.set_output_intent_observer(observations.append)
    request = _request()
    handle = await coordinator.delegate(request)
    await handle.record.task
    intent = coordinator.output_intent(
        handle,
        current_fence=request.fence,
        current_task_epoch=request.task_epoch,
        current_context_version=request.context_version,
        relevant=True,
        now_ms=1_001,
    )
    assert intent is not None
    assert (
        coordinator.admit_output_intent(
            intent,
            current_fence=request.fence,
            current_context_version=request.context_version,
            floor_allows_output=False,
            now_ms=1_002,
        )
        is None
    )
    assert (
        coordinator.admit_output_intent(
            intent,
            current_fence=request.fence,
            current_context_version=request.context_version,
            floor_allows_output=True,
            now_ms=1_003,
        )
        is None
    )
    assert [item.reason for item in observations] == [
        "floor_blocked",
        "duplicate_intent",
    ]
    assert all(not item.accepted for item in observations)
    with pytest.raises(ValueError):
        DelegationCoordinator.bridge_acknowledgement(
            BRIDGE_PHRASES[0],
            fence=_fence(),
            context_version=5,
            expires_at_ms=1_000,
            now_ms=1_000,
        )


def test_output_intent_rejects_a_future_creation_timestamp() -> None:
    coordinator = DelegationCoordinator(TaskManager())
    observations: list[OutputIntentAdmission] = []
    coordinator.set_output_intent_observer(observations.append)
    intent = DelegationCoordinator.bridge_acknowledgement(
        BRIDGE_PHRASES[0],
        fence=_fence(),
        context_version=5,
        expires_at_ms=2_000,
        now_ms=1_000,
    )
    intent.created_at_ms = 1_501

    assert (
        coordinator.admit_output_intent(
            intent,
            current_fence=_fence(),
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_500,
        )
        is None
    )
    assert observations[-1].reason == "invalid_created_at"


def test_output_intent_observer_receives_multi_source_authoritative_winner() -> None:
    coordinator = DelegationCoordinator(TaskManager())
    observations: list[OutputIntentAdmission] = []
    coordinator.set_output_intent_observer(observations.append)

    def intent(intent_id: str, kind: int, priority: int, created_at_ms: int) -> Any:
        return media_pb2.OutputIntent(
            intent_id=intent_id,
            session_id="session",
            turn_id=1,
            generation_id=2,
            tool_epoch=3,
            kind=kind,
            priority=priority,
            created_at_ms=created_at_ms,
            expires_at_ms=2_000,
            floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
            context_version=5,
            tts_source=intent_id,
        )

    current_fence = _fence()
    assert (
        coordinator.admit_output_intent(
            intent("deep", media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT, 50, 900),
            current_fence=current_fence,
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_000,
        )
        == "deep"
    )
    assert observations[-1].authoritative_candidate.intent_id == "deep"
    assert [candidate.intent_id for candidate in observations[-1].authoritative_candidates] == [
        "deep"
    ]

    assert (
        coordinator.admit_output_intent(
            intent("lower-tool", media_pb2.OUTPUT_INTENT_KIND_TOOL_RESULT, 1, 901),
            current_fence=current_fence,
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_000,
        )
        is None
    )
    assert observations[-1].authoritative_candidate.intent_id == "deep"
    assert observations[-1].accepted is True
    assert observations[-1].selected is False
    assert observations[-1].reason == "queued"
    assert [candidate.intent_id for candidate in observations[-1].authoritative_candidates] == [
        "deep",
        "lower-tool",
    ]

    assert (
        coordinator.admit_output_intent(
            intent("notification", media_pb2.OUTPUT_INTENT_KIND_NOTIFICATION, 1000, 902),
            current_fence=current_fence,
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_000,
        )
        is None
    )
    assert observations[-1].authoritative_candidate.intent_id == "deep"

    assert (
        coordinator.admit_output_intent(
            intent("ack", media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT, 1, 903),
            current_fence=current_fence,
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_000,
        )
        == "ack"
    )
    assert observations[-1].authoritative_candidate.intent_id == "ack"
    assert observations[-1].selected is True
    assert [candidate.intent_id for candidate in observations[-1].authoritative_candidates] == [
        "ack",
        "deep",
        "lower-tool",
        "notification",
    ]


def test_output_intent_authoritative_state_restores_same_domain_fallback() -> None:
    coordinator = DelegationCoordinator(TaskManager())
    observations: list[OutputIntentAdmission] = []
    coordinator.set_output_intent_observer(observations.append)
    fence = _fence()

    def intent(intent_id: str, priority: int, expires_at_ms: int) -> Any:
        return media_pb2.OutputIntent(
            intent_id=intent_id,
            session_id=fence.session_id,
            turn_id=fence.turn_id,
            generation_id=fence.generation_id,
            tool_epoch=fence.tool_epoch,
            kind=media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT,
            priority=priority,
            created_at_ms=900,
            expires_at_ms=expires_at_ms,
            floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
            context_version=5,
            tts_source=intent_id,
        )

    winner = intent("winner", 100, 1_100)
    fallback = intent("fallback", 1, 2_000)
    assert (
        coordinator.admit_output_intent(
            winner,
            current_fence=fence,
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_000,
        )
        == "winner"
    )
    assert (
        coordinator.admit_output_intent(
            fallback,
            current_fence=fence,
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_001,
        )
        is None
    )
    assert observations[-1].authoritative_candidate.intent_id == "winner"

    assert (
        coordinator.admit_output_intent(
            winner,
            current_fence=fence,
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_200,
        )
        is None
    )
    assert observations[-1].reason == "duplicate_intent"
    assert observations[-1].authoritative_candidate.intent_id == "fallback"
    assert [candidate.intent_id for candidate in observations[-1].authoritative_candidates] == [
        "fallback"
    ]


def test_output_intent_authoritative_state_is_bounded_and_context_scoped() -> None:
    coordinator = DelegationCoordinator(TaskManager())
    observations: list[OutputIntentAdmission] = []
    coordinator.set_output_intent_observer(observations.append)
    fence = _fence()
    coordinator.activate_context_version(fence.session_id, 5)

    for priority in range(5):
        intent = media_pb2.OutputIntent(
            intent_id=f"candidate-{priority}",
            session_id=fence.session_id,
            turn_id=fence.turn_id,
            generation_id=fence.generation_id,
            tool_epoch=fence.tool_epoch,
            kind=media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT,
            priority=priority,
            created_at_ms=900,
            expires_at_ms=2_000,
            floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
            context_version=5,
            tts_source=str(priority),
        )
        assert coordinator.admit_output_intent(
            intent,
            current_fence=fence,
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_000,
        ) == str(priority)

    assert [candidate.intent_id for candidate in observations[-1].authoritative_candidates] == [
        "candidate-4",
        "candidate-3",
        "candidate-2",
        "candidate-1",
    ]

    coordinator.activate_context_version(fence.session_id, 6)
    stale = media_pb2.OutputIntent()
    stale.CopyFrom(observations[-1].intent)
    stale.intent_id = "new-context-rejection"
    stale.context_version = 6
    stale.created_at_ms = 1_100
    assert (
        coordinator.admit_output_intent(
            stale,
            current_fence=fence,
            current_context_version=6,
            floor_allows_output=True,
            now_ms=1_000,
        )
        is None
    )
    assert observations[-1].authoritative_candidate is None
    assert observations[-1].authoritative_candidates == ()


def test_output_intent_rejects_unspecified_kind() -> None:
    coordinator = DelegationCoordinator(TaskManager())
    observations: list[OutputIntentAdmission] = []
    coordinator.set_output_intent_observer(observations.append)
    intent = DelegationCoordinator.bridge_acknowledgement(
        BRIDGE_PHRASES[0],
        fence=_fence(),
        context_version=5,
        expires_at_ms=2_000,
        now_ms=1_000,
    )
    intent.kind = media_pb2.OUTPUT_INTENT_KIND_UNSPECIFIED

    assert (
        coordinator.admit_output_intent(
            intent,
            current_fence=_fence(),
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_001,
        )
        is None
    )
    assert observations[-1].reason == "invalid_kind"


def test_output_intent_floor_loss_clears_active_candidates() -> None:
    coordinator = DelegationCoordinator(TaskManager())
    observations: list[OutputIntentAdmission] = []
    coordinator.set_output_intent_observer(observations.append)
    fence = _fence()
    intent = DelegationCoordinator.bridge_acknowledgement(
        BRIDGE_PHRASES[0],
        fence=fence,
        context_version=5,
        expires_at_ms=2_000,
        now_ms=1_000,
    )
    assert (
        coordinator.admit_output_intent(
            intent,
            current_fence=fence,
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_001,
        )
        == BRIDGE_PHRASES[0]
    )

    blocked = media_pb2.OutputIntent()
    blocked.CopyFrom(intent)
    blocked.intent_id = "floor-blocked"
    assert (
        coordinator.admit_output_intent(
            blocked,
            current_fence=fence,
            current_context_version=5,
            floor_allows_output=False,
            now_ms=1_002,
        )
        is None
    )
    assert observations[-1].reason == "floor_blocked"
    assert observations[-1].authoritative_candidate is None
    assert observations[-1].authoritative_candidates == ()


def test_output_intent_shadow_state_resets_on_transport_epoch_change() -> None:
    coordinator = DelegationCoordinator(TaskManager())
    observations: list[OutputIntentAdmission] = []
    coordinator.set_output_intent_observer(observations.append)
    fence = _fence()
    intent = media_pb2.OutputIntent(
        intent_id="old-epoch",
        session_id="session",
        turn_id=fence.turn_id,
        generation_id=fence.generation_id,
        tool_epoch=fence.tool_epoch,
        kind=media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT,
        priority=50,
        created_at_ms=900,
        expires_at_ms=2_000,
        floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
        context_version=5,
        tts_source="old",
    )
    assert (
        coordinator.admit_output_intent(
            intent,
            current_fence=fence,
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_000,
        )
        == "old"
    )

    coordinator.reset_output_intent_state("session")
    replacement = media_pb2.OutputIntent()
    replacement.CopyFrom(intent)
    replacement.intent_id = "new-epoch"
    replacement.tts_source = "new"
    assert (
        coordinator.admit_output_intent(
            replacement,
            current_fence=fence,
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_000,
        )
        == "new"
    )
    assert observations[-1].authoritative_candidate.intent_id == "new-epoch"


def test_output_intent_observer_failure_does_not_change_admission() -> None:
    coordinator = DelegationCoordinator(TaskManager())

    def fail_observation(_admission: OutputIntentAdmission) -> None:
        raise RuntimeError("shadow observer unavailable")

    coordinator.set_output_intent_observer(fail_observation)
    intent = DelegationCoordinator.bridge_acknowledgement(
        BRIDGE_PHRASES[0],
        fence=_fence(),
        context_version=5,
        expires_at_ms=2_000,
        now_ms=1_000,
    )

    assert (
        coordinator.admit_output_intent(
            intent,
            current_fence=_fence(),
            current_context_version=5,
            floor_allows_output=True,
            now_ms=1_500,
        )
        == BRIDGE_PHRASES[0]
    )
