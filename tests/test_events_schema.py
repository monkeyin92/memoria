"""Minimal UI event schema contract without a runtime JSON-schema dependency."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from services.agent.src.contracts.events import UI_EVENT_TYPES
from services.agent.src.duplex_runtime import DuplexRuntime


def test_runtime_ui_event_schema_covers_all_published_event_shapes() -> None:
    schema_path = Path(__file__).parents[1] / "packages" / "contracts" / "events.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    variants = {
        variant["properties"]["type"]["const"]: variant for variant in schema["oneOf"]
    }

    assert set(variants) == UI_EVENT_TYPES
    assert {"phase", "tool_epoch"} <= set(variants["assistant_state"]["properties"])
    assert {"session_id", "heard", "history_eligible", "tool_epoch"} <= set(
        variants["transcript_delta"]["properties"]
    )
    assert "session_id" in variants["transcript_delta"]["required"]
    assert variants["transcript_delta"]["properties"]["session_id"]["minLength"] == 1
    for event_type in ("audio_trace", "assistant_audio", "emotion_observation"):
        assert "tool_epoch" in variants[event_type]["properties"]
    assert set(variants["speaker_enroll_progress"]["required"]) >= {
        "speech_ms",
        "target_ms",
        "elapsed_ms",
    }
    assert set(variants["speaker_enroll_result"]["required"]) >= {
        "accepted",
        "reason",
        "score",
    }
    assert set(variants["speaker_reject"]["required"]) >= {
        "context",
        "reason",
        "score",
    }
    assert set(variants["listener_cue"]["required"]) >= {
        "cue_id",
        "cue_epoch",
        "user_turn_id",
        "phase",
    }


def test_runtime_rejects_ui_event_types_missing_from_the_contract() -> None:
    runtime = DuplexRuntime.create(session_id="ui-contract-test")

    with pytest.raises(ValueError, match="unsupported voice-agent.ui event type"):
        runtime._publish({"type": "undeclared_event"})


@pytest.mark.asyncio
async def test_runtime_transcript_event_carries_the_session_contract() -> None:
    runtime = DuplexRuntime.create(session_id="ui-contract-test")
    published: list[dict[str, object]] = []

    async def capture(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(capture)
    runtime.publish_transcript(speaker="user", text="当前问题", final=False)
    await asyncio.sleep(0)

    assert published == [
        {
            "type": "transcript_delta",
            "session_id": "ui-contract-test",
            "speaker": "user",
            "text": "当前问题",
            "final": False,
            "turn_id": 0,
            "generation_id": 0,
            "tool_epoch": 0,
            "history_eligible": False,
        }
    ]
    await runtime.close()
