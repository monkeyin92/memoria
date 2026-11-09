"""Wire contract for the response-plan endpoints.

The agent sends one :class:`ResponsePlanRequest` per committed turn and the
Control API answers with the rendered plan payload. Keeping the model shapes
and their small bounds here lets the payload renderer and the router share one
definition without either owning the other.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.tutor.turn_policy import TutorUtteranceIntent

EVOLUTION_PROTOCOL_V1 = "v1"
_RECALL_CONTEXT_MAX_ITEMS = 4
_RECALL_CONTEXT_ITEM_MAX_CHARS = 240
_RECALL_CONTEXT_TOTAL_MAX_CHARS = 960


class ResponsePlanFence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    session_id: str = Field(min_length=1, max_length=128)
    turn_id: int = Field(ge=0, le=2**31 - 1)
    generation_id: int = Field(ge=0, le=2**31 - 1)
    tool_epoch: int = Field(ge=0, le=2**31 - 1)
    session_epoch: int = Field(default=0, ge=0, le=2**31 - 1)


class ResponsePlanSpeakerDecision(BaseModel):
    """The non-biometric routing snapshot; scores and embeddings never cross this seam."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    classification: Literal["owner", "guest", "uncertain"]
    reason_code: str = Field(default="trusted", min_length=1, max_length=128)
    model_version: str | None = Field(default=None, min_length=1, max_length=256)
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    template_version: int | None = Field(default=None, ge=1, le=10_000_000)


class ResponsePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=4000)
    utterance_intent: TutorUtteranceIntent = "chat"
    recall_context: list[str] = Field(default_factory=list, max_length=_RECALL_CONTEXT_MAX_ITEMS)
    fence: ResponsePlanFence
    speaker_decision: ResponsePlanSpeakerDecision

    @field_validator("recall_context")
    @classmethod
    def validate_recall_context(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        total_chars = 0
        for item in value:
            text = item.strip()
            if not text or len(text) > _RECALL_CONTEXT_ITEM_MAX_CHARS:
                raise ValueError("recall_context items must be non-empty and bounded")
            total_chars += len(text)
            if total_chars > _RECALL_CONTEXT_TOTAL_MAX_CHARS:
                raise ValueError("recall_context is too large")
            normalized.append(text)
        return normalized

    @model_validator(mode="after")
    def require_matching_fence_session(self) -> ResponsePlanRequest:
        if self.fence.session_id != self.session_id:
            raise ValueError("fence session_id must match session_id")
        return self


class ContextPrefetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=4000)
    speaker_decision: ResponsePlanSpeakerDecision
