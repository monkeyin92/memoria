"""GenerationFence drop semantics."""

from __future__ import annotations

import pytest
from services.agent.src.contracts.ids import CancellationContext, GenerationFence
from services.agent.src.orchestration.generation_fence import FenceGate, StaleFenceError
from services.agent.src.voice_core.generation_controller import GenerationController


def _fence(g: int = 1, e: int = 0) -> GenerationFence:
    return GenerationFence(session_id="s", turn_id=1, generation_id=g, tool_epoch=e)


def test_cancellation_context_reuses_the_complete_generation_fence() -> None:
    fence = _fence(2, e=3)
    context = CancellationContext.capture(fence)

    assert context.fence is fence
    assert context.session_id == "s"
    assert context.turn_id == 1
    assert context.generation_id == 2
    assert context.tool_epoch == 3
    assert context.is_current(fence)
    assert context.is_stale(fence.bump_generation())


def test_accept_matching_fence() -> None:
    gate = FenceGate(current=_fence(1))
    assert gate.accept(_fence(1), source="tts") is True
    assert gate.dropped_count == 0


def test_drop_stale_generation() -> None:
    gate = FenceGate(current=_fence(2))
    assert gate.gate(_fence(1), b"audio", source="tts") is None
    assert gate.dropped_count == 1
    assert gate.metrics.get("stale_result_dropped_total", {"source": "tts"}) == 1


def test_drop_stale_tool_epoch() -> None:
    gate = FenceGate(current=_fence(2, e=1))
    assert gate.gate(_fence(2, e=0), {"ok": True}, source="tool") is None
    assert gate.metrics.get("stale_result_dropped_total", {"source": "tool"}) == 1


def test_require_raises() -> None:
    gate = FenceGate(current=_fence(3))
    with pytest.raises(StaleFenceError):
        gate.require(_fence(1), source="llm")


def test_generation_controller_orders_session_epoch_before_turn() -> None:
    controller = GenerationController(session_id="s")
    controller.advance(GenerationFence("s", 1, 1, 0, 3))

    with pytest.raises(ValueError, match="monotonically"):
        controller.advance(GenerationFence("s", 99, 99, 0, 2))

    assert controller.current == GenerationFence("s", 1, 1, 0, 3)
