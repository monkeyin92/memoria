from __future__ import annotations

from typing import Any, cast

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.coordination_contracts import (
    AdmittedSpeech,
    CoordinationHarness,
    DeliverDecision,
    DeliverModule,
    DeliverRejectReason,
    FloorGate,
    HoldDecision,
    HoldModule,
    HoldState,
    HoldTimeout,
    IntakeModule,
    IntakeRejectDecision,
    IntakeRejectReason,
    MemoryCommitEvidence,
    MemoryCommittedTurn,
    MemoryFailure,
    MemoryModule,
    OutputIntentState,
    PolicyFailure,
    PolicyIntent,
    PolicyModule,
    PolicyRoute,
    ProjectionState,
    SpeechPacket,
    WorkerDelegation,
    WorkerFailure,
    WorkerModule,
    WorkerResult,
)


def _fence() -> GenerationFence:
    return GenerationFence("session-1", 1, 1, 0)


def test_policy_contracts() -> None:
    assert PolicyIntent.CHAT.value == "chat"
    assert PolicyIntent.INTERRUPT_COMMAND.value == "interrupt_command"
    assert PolicyFailure.INVALID_INTENT.value == "invalid_intent"

    route = PolicyRoute(
        intent=PolicyIntent.CHAT,
        reason="normal_chat",
        enter_chat=True,
        should_interrupt=False,
        speaker_gate_override=False,
        ack_phrase=None,
        normalized_text="你好",
    )
    assert route.intent == PolicyIntent.CHAT
    assert route.failure is None

    with pytest.raises(NotImplementedError):
        PolicyModule.route_utterance(cast(Any, None), "text")


def test_intake_contracts() -> None:
    assert IntakeRejectReason.LISTENING_HOLD.value == "listening_hold"
    packet = SpeechPacket(
        kind="VAD",
        session_id="s1",
        stream_epoch=1,
        capture_start_sample=0,
        capture_end_sample=1600,
        text="",
        final=False,
    )
    admitted = AdmittedSpeech(
        packet=packet,
        turn_opened=True,
        reopened_turn=False,
        pending_turn_floor=None,
    )
    assert admitted.packet.kind == "VAD"

    rejection = IntakeRejectDecision(IntakeRejectReason.SPURIOUS_VAD, "noise")
    assert rejection.reason == IntakeRejectReason.SPURIOUS_VAD
    assert rejection.detail == "noise"

    with pytest.raises(NotImplementedError):
        IntakeModule.admit_speech_packet(
            cast(Any, None), packet, session_id="s1", stream_epoch=1
        )


def test_hold_contracts() -> None:
    assert HoldDecision.COMMIT_NOW.value == "commit_now"
    assert HoldTimeout.TAIL_TIMEOUT.value == "tail_timeout"

    state = HoldState(
        turn_start_sample=0,
        turn_end_sample=1600,
        turn_endpoint_sample=1600,
        turn_retire_sample=None,
        has_text_evidence=True,
        endpoint_covered_by_asr=True,
        grace_deadline=1.0,
        tail_deadline=2.0,
        evidence_window_deadline=3.0,
    )
    assert state.turn_start_sample == 0

    packet = SpeechPacket("VAD", "s1", 1, 0, 1600, "", False)
    admitted = AdmittedSpeech(packet, True, False, None)
    with pytest.raises(NotImplementedError):
        HoldModule.decide_hold(
            cast(Any, None),
            session_id="s1",
            stream_epoch=1,
            admitted=admitted,
            state=state,
        )


@pytest.mark.asyncio
async def test_worker_contracts() -> None:
    assert WorkerFailure.TASK_TIMEOUT.value == "task_timeout"
    assert OutputIntentState.CANDIDATE.value == "candidate"

    delegation = WorkerDelegation(
        tool_name="weather",
        arguments={"city": "Beijing"},
        fence=_fence(),
        task_epoch=1,
        context_version=1,
        expires_at_ms=5000,
        side_effect_policy="read_only",
        committed=True,
    )
    assert delegation.tool_name == "weather"

    result = WorkerResult(
        task_id="t1",
        fence=_fence(),
        task_epoch=1,
        context_version=1,
        payload={"temp": 20},
    )
    assert result.failure is None

    with pytest.raises(NotImplementedError):
        await WorkerModule.delegate_task(cast(Any, None), delegation)

    with pytest.raises(NotImplementedError):
        WorkerModule.admit_output_intent(
            cast(Any, None),
            None,
            current_fence=_fence(),
            current_context_version=1,
            floor_allows_output=True,
        )


def test_deliver_contracts() -> None:
    assert DeliverDecision.SEND.value == "send"
    assert DeliverRejectReason.FLOOR_BLOCKED.value == "floor_blocked"

    gate = FloorGate(assistant_may_speak=True, reason="ok")
    assert gate.assistant_may_speak is True

    with pytest.raises(NotImplementedError):
        DeliverModule.decide_delivery(
            cast(Any, None),
            None,
            current_fence=_fence(),
            current_context_version=1,
            floor_gate=gate,
        )


def test_memory_contracts() -> None:
    assert MemoryFailure.PROJECTION_REJECTED.value == "projection_rejected"
    assert ProjectionState.COMMITTED.value == "committed"

    evidence = MemoryCommitEvidence(
        session_id="s1",
        stream_epoch=1,
        capture_start_sample=0,
        capture_end_sample=1600,
        text="hello",
        fence=_fence(),
        speaker_class="child",
        speaker_authority_verified=True,
        history_eligible=True,
    )
    committed = MemoryCommittedTurn(
        fence=_fence(),
        text="hello",
        capture_start_sample=0,
        capture_end_sample=1600,
        revision=1,
        speaker_class="child",
    )
    assert committed.failure is None

    with pytest.raises(NotImplementedError):
        MemoryModule.commit_turn(cast(Any, None), evidence)


@pytest.mark.asyncio
async def test_coordination_harness_contract() -> None:
    with pytest.raises(NotImplementedError):
        await CoordinationHarness.coordinate_user_input(
            cast(Any, None), "s1", cast(Any, None)
        )
