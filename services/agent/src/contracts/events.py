"""Typed pipeline events shared across orchestration and providers."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EventKind(StrEnum):
    USER_SPEECH_START = "user_speech_start"
    USER_SPEECH_END = "user_speech_end"
    ASR_INTERIM = "asr_interim"
    ASR_PREFLIGHT = "asr_preflight"
    ASR_FINAL = "asr_final"
    TURN_COMMITTED = "turn_committed"
    LLM_STARTED = "llm_started"
    LLM_TOKEN = "llm_token"
    LLM_COMPLETED = "llm_completed"
    TTS_STARTED = "tts_started"
    TTS_AUDIO = "tts_audio"
    TTS_ALIGNMENT = "tts_alignment"
    PLAYBACK_STARTED = "playback_started"
    PLAYBACK_PROGRESS = "playback_progress"
    PLAYBACK_STOPPED = "playback_stopped"
    INTERRUPTION_CANDIDATE = "interruption_candidate"
    INTERRUPTION_CONFIRMED = "interruption_confirmed"
    FALSE_INTERRUPTION = "false_interruption"
    TOOL_STARTED = "tool_started"
    TOOL_RESULT = "tool_result"
    TOOL_CANCELLED = "tool_cancelled"
    PROVIDER_ERROR = "provider_error"
    STALE_RESULT_DROPPED = "stale_result_dropped"


class BasePipelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EventKind
    session_id: str
    turn_id: int
    generation_id: int
    monotonic_ns: int
    wall_time_utc: str


class TimedWord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    begin_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    punctuation: str = ""


class TranscriptEvent(BasePipelineEvent):
    kind: Literal[
        EventKind.ASR_INTERIM,
        EventKind.ASR_PREFLIGHT,
        EventKind.ASR_FINAL,
    ]
    text: str
    sentence_id: int
    begin_ms: int
    end_ms: int | None
    is_final: bool
    words: tuple[TimedWord, ...] = ()
    confidence: float | None = None


class ToolResultEvent(BasePipelineEvent):
    kind: Literal[EventKind.TOOL_RESULT]
    tool_epoch: int
    tool_task_id: str
    tool_name: str
    payload: dict[str, Any]


class StaleResultDroppedEvent(BasePipelineEvent):
    kind: Literal[EventKind.STALE_RESULT_DROPPED] = EventKind.STALE_RESULT_DROPPED
    source: str
    expected_generation_id: int
    actual_generation_id: int
    tool_epoch_expected: int | None = None
    tool_epoch_actual: int | None = None


class StateTransitionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    from_state: str
    to_state: str
    event: str
    turn_id: int
    generation_id: int
    tool_epoch: int
    monotonic_ns: int
    cause: str = ""
