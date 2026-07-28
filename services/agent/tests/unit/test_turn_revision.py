from __future__ import annotations

import asyncio
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.turn_revision import TurnRevisionTracker


def test_turn_revision_rejects_late_provider_updates() -> None:
    tracker = TurnRevisionTracker()
    fence = GenerationFence("session", 2, 3, 0)

    assert tracker.issue(speaker="user", fence=fence, requested=2) == 2
    assert tracker.issue(speaker="user", fence=fence, requested=1) is None
    assert tracker.issue(speaker="user", fence=fence) == 3
    assert tracker.issue(speaker="assistant", fence=fence) == 1


def test_turn_revision_closes_after_the_first_final() -> None:
    tracker = TurnRevisionTracker()
    fence = GenerationFence("session", 2, 3, 0)

    assert tracker.issue(speaker="user", fence=fence) == 1
    assert tracker.issue(speaker="user", fence=fence, final=True) == 2
    assert tracker.issue(speaker="user", fence=fence, requested=3, final=True) is None


@pytest.mark.asyncio
async def test_final_revision_cannot_diverge_from_durable_history() -> None:
    runtime = DuplexRuntime.create(session_id="revision-history")
    ui_events: list[dict[str, Any]] = []
    evidence_events: list[dict[str, Any]] = []

    async def publish_ui(event: dict[str, Any]) -> None:
        ui_events.append(event)

    async def publish_evidence(event: dict[str, Any]) -> None:
        evidence_events.append(event)

    runtime.set_event_publisher(publish_ui)
    runtime.set_evidence_publisher(publish_evidence)

    assert runtime.publish_transcript(
        speaker="user",
        text="第一份最终稿",
        final=True,
        turn_revision=1,
    )
    assert not runtime.publish_transcript(
        speaker="user",
        text="迟到的第二份最终稿",
        final=True,
        turn_revision=2,
    )
    await asyncio.sleep(0)

    transcript_events = [
        event for event in ui_events if event.get("type") == "transcript_delta"
    ]
    assert [event["text"] for event in transcript_events] == ["第一份最终稿"]
    assert [event["payload"]["text"] for event in evidence_events] == [
        "第一份最终稿"
    ]
