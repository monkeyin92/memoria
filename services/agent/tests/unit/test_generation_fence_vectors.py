"""Generation-fence semantics shared with the Go Media Edge.

``packages/contracts/generation-fence-vectors.json`` is read by this test and
by ``services/media_edge/generation_fence_vectors_test.go``; a rule changes in
that file and in both languages together.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.generation_controller import GenerationController
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter

VECTORS: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[4]
        / "packages"
        / "contracts"
        / "generation-fence-vectors.json"
    ).read_text(encoding="utf-8")
)
SESSION_ID = str(VECTORS["session_id"])


def _fence(fields: list[int]) -> GenerationFence:
    session_epoch, turn_id, generation_id, tool_epoch = fields
    return GenerationFence(
        session_id=SESSION_ID,
        turn_id=turn_id,
        generation_id=generation_id,
        tool_epoch=tool_epoch,
        session_epoch=session_epoch,
    )


def _controller_at(fence: GenerationFence) -> GenerationController:
    controller = GenerationController(SESSION_ID)
    controller.advance(fence)
    return controller


@pytest.mark.parametrize("vector", VECTORS["order_cases"], ids=lambda case: case["name"])
def test_fence_equality_and_order_match_the_shared_vectors(vector: dict[str, Any]) -> None:
    current = _fence(vector["current"])
    candidate = _fence(vector["candidate"])

    assert current.matches(candidate) is vector["equal"]

    order = ExistingVoiceProviderAdapter._generation_order
    expected = {"before": -1, "same": 0, "after": 1}[vector["order"]]
    actual = (order(candidate) > order(current)) - (order(candidate) < order(current))
    assert actual == expected

    controller = _controller_at(current)
    if vector["order"] == "before":
        with pytest.raises(ValueError):
            controller.advance(candidate)
    else:
        assert controller.advance(candidate) == candidate


@pytest.mark.parametrize("vector", VECTORS["cancel_cases"], ids=lambda case: case["name"])
def test_cancel_fence_matches_the_shared_vectors(vector: dict[str, Any]) -> None:
    current = _fence(vector["current"])
    cancelled = _fence(vector["cancelled"])

    # Voice Core derives a cancel by bumping only the generation; Media Edge
    # accepts exactly that fence and nothing else.
    assert current.bump_generation().matches(cancelled) is vector["accepted"]
