"""Project only a fresh Python phase observed by the device's live Edge connection."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class AssistantPhase(StrEnum):
    CONNECTING = "connecting"
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    BACKCHANNEL = "backchannel"
    THINKING_SILENT = "thinking_silent"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    TOOL_WAITING = "tool_waiting"
    RECOVERING = "recovering"
    CLOSED = "closed"


def project_assistant_snapshot(
    runtime: dict[str, object] | None, *, now: datetime
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "unknown",
        "phase": None,
        "authority": "edge_python_assistant_state",
    }
    if runtime is None:
        return result
    if runtime.get("connected") is False:
        return {**result, "status": "offline"}
    snapshot = runtime.get("assistant_state")
    if runtime.get("connected") is not True or not isinstance(snapshot, dict):
        return result
    if not isinstance(runtime.get("session_id"), str) or not runtime["session_id"]:
        return result
    if snapshot.get("session_id") != runtime.get("session_id") or snapshot.get(
        "stream_epoch"
    ) != runtime.get("stream_epoch"):
        return result
    fence = snapshot.get("fence")
    if not isinstance(fence, dict) or any(
        type(fence.get(key)) is not int or fence[key] < minimum
        for key, minimum in (
            ("session_epoch", 1),
            ("generation_id", 0),
            ("turn_id", 0),
            ("tool_epoch", 0),
        )
    ):
        return result
    if any(
        type(snapshot.get(key)) is not int or snapshot[key] < 1
        for key in ("stream_epoch", "sequence")
    ):
        return result
    try:
        observed = datetime.fromisoformat(str(snapshot["observed_at"]).replace("Z", "+00:00"))
        if observed.tzinfo is None or observed.utcoffset() is None:
            return result
        age = (now.astimezone(UTC) - observed).total_seconds()
        phase = AssistantPhase(snapshot["phase"])
    except (KeyError, TypeError, ValueError):
        return result
    if not 0 <= age <= 15:
        return {**result, "stale": True}
    return {
        **result,
        "status": "online",
        "phase": phase.value,
        "snapshot": snapshot,
        "freshness_ms": int(age * 1000),
    }
