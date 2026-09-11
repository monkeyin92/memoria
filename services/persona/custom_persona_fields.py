"""Controlled structured fields for a custom persona.

Single source of truth for the eleven-field allowlist.  The Control API
validates the LLM / hand-filled payload with the strict ``StructuredPersona``
pydantic model (``extra="forbid"``, ``Literal`` enums, bounded lengths), the
identity service persists it as a ``CustomPersonaRecord``, and both the Control
API (session-policy envelope) and the Agent (ModePolicy parse) share ONE
adapter -- ``custom_persona_definition`` / ``companion_definition_from_envelope``
-- so a custom persona renders through exactly the same ``CompanionDefinition``
surface as the built-in catalogue.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.common.companions import (
    COMPANION_VOICE_PROFILES,
    DEFAULT_COMPANION_ID,
    CompanionDefinition,
    companion_definition,
)
from services.identity.domain import (
    CustomPersonaRecord,
    StructuredPersonaFields,
    is_custom_persona_id,
)

STRUCTURED_VERSION: Final = "persona-structuring-v1"

MAX_STYLE_DESCRIPTION: Final = 60
MAX_WELCOME_TEXT: Final = 60
MAX_CONVERSATION_INSTRUCTION: Final = 200
MAX_VOICE_INSTRUCTION: Final = 120
MIN_DEFAULT_VOICE_RATE: Final = 0.90
MAX_DEFAULT_VOICE_RATE: Final = 1.10
MAX_DISPLAY_NAME: Final = 16

STRUCTURED_ENUMS: Final[dict[str, tuple[str, ...]]] = {
    "warmth": ("warm", "bright", "soft", "calm", "reserved"),
    "directness": ("gentle", "direct"),
    "response_length": ("brief", "balanced"),
    "question_frequency": ("rare", "occasional", "frequent"),
    "interview_depth": ("light", "structured", "on_explicit_invitation"),
    "default_voice_emotion": ("neutral", "happy"),
}

_ENVELOPE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "persona_id",
        "persona_version",
        "display_name",
        "style_description",
        "warmth",
        "directness",
        "response_length",
        "question_frequency",
        "interview_depth",
        "designed_voice_profile",
        "welcome_text",
        "conversation_instruction",
        "voice_instruction",
        "default_voice_emotion",
        "default_voice_rate",
    }
)

_CUSTOM_PERSONA_ID_PATTERN = re.compile(r"^cu_[0-9a-f]{16,29}$")


class StructuredPersona(BaseModel):
    """Strict, fail-closed eleven-field payload.

    An out-of-domain value (e.g. ``warmth="甜"``) is a validation error, never
    silently clamped: a broken LLM response must not be written as user data.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    style_description: str = Field(max_length=MAX_STYLE_DESCRIPTION)
    warmth: Literal["warm", "bright", "soft", "calm", "reserved"]
    directness: Literal["gentle", "direct"]
    response_length: Literal["brief", "balanced"]
    question_frequency: Literal["rare", "occasional", "frequent"]
    interview_depth: Literal["light", "structured", "on_explicit_invitation"]
    welcome_text: str = Field(max_length=MAX_WELCOME_TEXT)
    conversation_instruction: str = Field(max_length=MAX_CONVERSATION_INSTRUCTION)
    voice_instruction: str = Field(max_length=MAX_VOICE_INSTRUCTION)
    default_voice_emotion: Literal["neutral", "happy"]
    default_voice_rate: float = Field(
        ge=MIN_DEFAULT_VOICE_RATE, le=MAX_DEFAULT_VOICE_RATE
    )


def invalid_structured_field(exc: ValidationError) -> str | None:
    """Name the first offending field of a ``StructuredPersona`` failure.

    Shared by the LLM structurer and the persistence endpoint so both surface
    ``422 {"code": "persona_structuring_invalid", "field": ...}`` identically.
    """
    errors = exc.errors()
    if not errors:
        return None
    location = errors[0].get("loc")
    if isinstance(location, (list, tuple)) and location:
        head = location[0]
        if isinstance(head, str):
            return head
    return None


def persona_draft_id(structured: StructuredPersona, free_text: str) -> str:
    """Deterministic, stateless draft id: ``sha256(canonical(structured)+text)``.

    The draft is held by the client; the server keeps no state and writes no
    row (design increment-02 §1.1 難点 4①②).  Re-running structuring over the
    same description and object yields the identical id.
    """
    canonical = json.dumps(
        structured.model_dump(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256()
    digest.update(canonical.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(free_text.encode("utf-8"))
    return digest.hexdigest()


def to_structured_fields(structured: StructuredPersona) -> StructuredPersonaFields:
    """Adapt the validated wire model into the identity-domain value object."""
    return StructuredPersonaFields(
        style_description=structured.style_description,
        warmth=structured.warmth,
        directness=structured.directness,
        response_length=structured.response_length,
        question_frequency=structured.question_frequency,
        interview_depth=structured.interview_depth,
        welcome_text=structured.welcome_text,
        conversation_instruction=structured.conversation_instruction,
        voice_instruction=structured.voice_instruction,
        default_voice_emotion=structured.default_voice_emotion,
        default_voice_rate=structured.default_voice_rate,
    )


def _designed_voice_profile(fallback_designed_voice: str) -> str:
    definition = companion_definition(fallback_designed_voice)
    if definition is None:
        definition = companion_definition(DEFAULT_COMPANION_ID)
    assert definition is not None  # DEFAULT_COMPANION_ID is always in the catalogue
    return definition.designed_voice_profile


def custom_persona_definition(record: CustomPersonaRecord) -> CompanionDefinition:
    """Adapt a persisted custom persona into the approved companion surface.

    The persona id becomes the ``companion_id`` and the designed-voice profile
    is resolved from the stored ``fallback_designed_voice`` built-in, so any
    custom persona can be rendered by the SAME function used for the built-in
    catalogue.
    """
    return CompanionDefinition(
        companion_id=record.persona_id,
        display_name=record.display_name,
        style_description=record.style_description,
        warmth=record.warmth,
        directness=record.directness,
        response_length=record.response_length,
        question_frequency=record.question_frequency,
        interview_depth=record.interview_depth,
        designed_voice_profile=_designed_voice_profile(
            record.fallback_designed_voice
        ),
        welcome_text=record.welcome_text,
        conversation_instruction=record.conversation_instruction,
        voice_instruction=record.voice_instruction,
        default_voice_emotion=cast(
            Literal["neutral", "happy"], record.default_voice_emotion
        ),
        default_voice_rate=record.default_voice_rate,
    )


def custom_persona_envelope(record: CustomPersonaRecord) -> dict[str, object]:
    """Serialize the internal ``custom_persona`` control->agent envelope key."""
    definition = custom_persona_definition(record)
    return {
        "persona_id": definition.companion_id,
        "persona_version": record.persona_version,
        "display_name": definition.display_name,
        "style_description": definition.style_description,
        "warmth": definition.warmth,
        "directness": definition.directness,
        "response_length": definition.response_length,
        "question_frequency": definition.question_frequency,
        "interview_depth": definition.interview_depth,
        "designed_voice_profile": definition.designed_voice_profile,
        "welcome_text": definition.welcome_text,
        "conversation_instruction": definition.conversation_instruction,
        "voice_instruction": definition.voice_instruction,
        "default_voice_emotion": definition.default_voice_emotion,
        "default_voice_rate": definition.default_voice_rate,
    }


def _envelope_text(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str) or len(value) > maximum:
        return None
    return value


def _envelope_enum(value: object, *, field: str) -> str | None:
    if isinstance(value, str) and value in STRUCTURED_ENUMS[field]:
        return value
    return None


def companion_definition_from_envelope(payload: object) -> CompanionDefinition | None:
    """Parse the internal ``custom_persona`` envelope, or ``None`` when invalid.

    This is the control->agent internal contract: the key set must match
    exactly (extra fields are rejected), every enum must be in its domain,
    every length bounded, and the rate inside ``[0.90, 1.10]``.  Any deviation
    fails closed (the caller degrades to the default persona rather than
    rendering a half-parsed definition).
    """
    if not isinstance(payload, Mapping) or set(payload) != _ENVELOPE_FIELDS:
        return None
    persona_id = payload["persona_id"]
    if not isinstance(persona_id, str) or not _CUSTOM_PERSONA_ID_PATTERN.match(
        persona_id
    ):
        return None
    if not is_custom_persona_id(persona_id):
        return None
    persona_version = payload["persona_version"]
    if isinstance(persona_version, bool) or persona_version != 1:
        return None
    display_name = _envelope_text(payload["display_name"], maximum=MAX_DISPLAY_NAME)
    style_description = _envelope_text(
        payload["style_description"], maximum=MAX_STYLE_DESCRIPTION
    )
    welcome_text = _envelope_text(
        payload["welcome_text"], maximum=MAX_WELCOME_TEXT
    )
    conversation_instruction = _envelope_text(
        payload["conversation_instruction"], maximum=MAX_CONVERSATION_INSTRUCTION
    )
    voice_instruction = _envelope_text(
        payload["voice_instruction"], maximum=MAX_VOICE_INSTRUCTION
    )
    if (
        not display_name
        or style_description is None
        or welcome_text is None
        or conversation_instruction is None
        or voice_instruction is None
    ):
        return None
    warmth = _envelope_enum(payload["warmth"], field="warmth")
    directness = _envelope_enum(payload["directness"], field="directness")
    response_length = _envelope_enum(payload["response_length"], field="response_length")
    question_frequency = _envelope_enum(
        payload["question_frequency"], field="question_frequency"
    )
    interview_depth = _envelope_enum(payload["interview_depth"], field="interview_depth")
    default_voice_emotion = _envelope_enum(
        payload["default_voice_emotion"], field="default_voice_emotion"
    )
    if (
        warmth is None
        or directness is None
        or response_length is None
        or question_frequency is None
        or interview_depth is None
        or default_voice_emotion is None
    ):
        return None
    designed_voice_profile = payload["designed_voice_profile"]
    if (
        not isinstance(designed_voice_profile, str)
        or designed_voice_profile not in COMPANION_VOICE_PROFILES.values()
    ):
        return None
    default_voice_rate = payload["default_voice_rate"]
    if isinstance(default_voice_rate, bool) or not isinstance(
        default_voice_rate, (int, float)
    ):
        return None
    rate = float(default_voice_rate)
    if not (MIN_DEFAULT_VOICE_RATE <= rate <= MAX_DEFAULT_VOICE_RATE):
        return None
    return CompanionDefinition(
        companion_id=persona_id,
        display_name=display_name,
        style_description=style_description,
        warmth=warmth,
        directness=directness,
        response_length=response_length,
        question_frequency=question_frequency,
        interview_depth=interview_depth,
        designed_voice_profile=designed_voice_profile,
        welcome_text=welcome_text,
        conversation_instruction=conversation_instruction,
        voice_instruction=voice_instruction,
        default_voice_emotion=cast(
            Literal["neutral", "happy"], default_voice_emotion
        ),
        default_voice_rate=rate,
    )


__all__ = [
    "MAX_CONVERSATION_INSTRUCTION",
    "MAX_DISPLAY_NAME",
    "MAX_STYLE_DESCRIPTION",
    "MAX_VOICE_INSTRUCTION",
    "MAX_WELCOME_TEXT",
    "MAX_DEFAULT_VOICE_RATE",
    "MIN_DEFAULT_VOICE_RATE",
    "STRUCTURED_ENUMS",
    "STRUCTURED_VERSION",
    "StructuredPersona",
    "companion_definition_from_envelope",
    "custom_persona_definition",
    "custom_persona_envelope",
    "invalid_structured_field",
    "persona_draft_id",
    "to_structured_fields",
]
