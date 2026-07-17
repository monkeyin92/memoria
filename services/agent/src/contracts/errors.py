"""Typed errors for the voice agent pipeline."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    CONFIG_INVALID = "config_invalid"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_TASK_FAILED = "provider_task_failed"
    STALE_RESULT = "stale_result"
    ILLEGAL_STATE_TRANSITION = "illegal_state_transition"
    QUEUE_OVERLOAD = "queue_overload"
    SESSION_CLOSED = "session_closed"
    TOOL_CANCELLED = "tool_cancelled"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_INVALID_JSON = "tool_invalid_json"
    ALIGNMENT_DEGRADED = "alignment_degraded"
    CIRCUIT_OPEN = "circuit_open"


class VoiceAgentError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class IllegalStateTransition(VoiceAgentError):
    def __init__(self, from_state: str, event: str) -> None:
        super().__init__(
            ErrorCode.ILLEGAL_STATE_TRANSITION,
            f"illegal transition from {from_state} on event {event}",
        )
        self.from_state = from_state
        self.event = event


class StaleResultError(VoiceAgentError):
    def __init__(self, source: str) -> None:
        super().__init__(ErrorCode.STALE_RESULT, f"stale result dropped from {source}")
        self.source = source


class ConfigValidationError(VoiceAgentError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.CONFIG_INVALID, message)
