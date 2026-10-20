"""Shared minor memory-retention ceiling for runtime and archive consumers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

TEXT_WITHHELD_SUBJECT_CATEGORIES: Final[frozenset[str]] = frozenset({"minor", "unknown"})


def payload_text_allowed(payload: Mapping[str, Any]) -> bool:
    return (
        payload.get("memory_retention") != "ephemeral_only"
        and payload.get("subject_category") not in TEXT_WITHHELD_SUBJECT_CATEGORIES
    )


def memory_retention_allowed(
    *,
    subject_category: object,
    active_consent: bool,
) -> bool:
    if subject_category == "adult":
        return True
    if subject_category == "minor":
        return active_consent
    return False


def apply_memory_retention_ceiling(
    context: Mapping[str, Any],
    *,
    allowed: bool,
) -> dict[str, Any]:
    result = dict(context)
    if allowed:
        return result
    capabilities = dict(result.get("capabilities") or {})
    for key in (
        "private_memory",
        "persona",
        "persona_low_sensitivity",
        "history",
        "learning",
    ):
        capabilities[key] = False
    result.update(
        {
            "history_eligible": False,
            "owner_projection_eligible": False,
            "capabilities": capabilities,
            "memory_retention": "ephemeral_only",
        }
    )
    return result




def conversation_turn_eligible(
    *,
    event_type: str,
    speaker_class: str,
    payload: Mapping[str, Any],
) -> bool:
    """Whether one archived event is a conversation turn the owner may review.

    The paired history items also need the stored text; the session list only
    needs the session and its turns. #282 stops storing turn text, so a list
    built from the history items alone would hide every real session.
    """

    if not (
        payload.get("history_eligible") is True
        and payload.get("owner_projection_eligible") is True
        and payload_text_allowed(payload)
    ):
        return False
    if event_type == "speech.utterance_finalized":
        return speaker_class == "owner"
    return event_type == "assistant.playout_stopped" and (
        speaker_class == "assistant" and payload.get("actual_heard") is True
    )
__all__ = [
    "TEXT_WITHHELD_SUBJECT_CATEGORIES",
    "apply_memory_retention_ceiling",
    "conversation_turn_eligible",
    "memory_retention_allowed",
    "payload_text_allowed",
]