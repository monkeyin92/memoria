"""Response item builders for the archive read endpoints.

Pure mapping from evidence rows and consent records to the JSON items the
archive routes return; the routes keep the request scope and store accessors.
"""

from __future__ import annotations

from typing import Any

from services.archive.domain import EvidenceEvent, RawVoiceConsent
from services.guardian.retention import payload_text_allowed


def _consent_payload(consent: RawVoiceConsent) -> dict[str, Any]:
    return {
        "consent_grant_id": consent.consent_grant_id,
        "policy_version": consent.policy_version,
        "retention_policy": consent.retention_policy,
        "granted_at": consent.granted_at.isoformat(),
        "revoked_at": consent.revoked_at.isoformat() if consent.revoked_at else None,
    }


def _actual_heard_item(event: EvidenceEvent) -> dict[str, Any] | None:
    payload = event.payload
    text = payload.get("text")
    if not (
        event.event_type == "assistant.playout_stopped"
        and event.speaker_class == "assistant"
        and payload.get("actual_heard") is True
        and payload.get("history_eligible") is True
        and payload.get("owner_projection_eligible") is True
        and payload_text_allowed(payload)
        and isinstance(text, str)
        and text.strip()
        and event.session_id is not None
        and event.turn_id is not None
        and event.generation_id is not None
    ):
        return None
    return {
        "event_id": event.event_id,
        "occurred_at": event.occurred_at.isoformat(),
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "generation_id": event.generation_id,
        "text": text.strip(),
        # Exact DAC evidence must opt in explicitly. Missing or malformed
        # telemetry remains an approximate playback record.
        "approximate": payload.get("approximate") is not False,
    }


def _owner_turn_item(event: EvidenceEvent) -> dict[str, Any] | None:
    payload = event.payload
    text = payload.get("text")
    if not (
        event.event_type == "speech.utterance_finalized"
        and event.speaker_class == "owner"
        and payload.get("history_eligible") is True
        and payload.get("owner_projection_eligible") is True
        and payload_text_allowed(payload)
        and isinstance(text, str)
        and text.strip()
        and event.session_id is not None
        and event.turn_id is not None
        and event.generation_id is not None
    ):
        return None
    return {
        "event_id": event.event_id,
        "occurred_at": event.occurred_at.isoformat(),
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "generation_id": event.generation_id,
        "speaker_class": "owner",
        "text": text.strip(),
    }


def _search_item(item: Any) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "title": item.title,
        "snippet": item.snippet,
        "category": item.category,
        "domain_category": item.domain_category,
        "memory_kind": item.memory_kind,
        "entity_ids": list(item.entity_ids),
        "status": item.status,
        "source_event_id": item.source_event_id,
        "source_event_ids": list(item.source_event_ids),
        "occurred_at": item.occurred_at.isoformat(),
        "valid_from": item.valid_from.isoformat() if item.valid_from else None,
        "valid_to": item.valid_to.isoformat() if item.valid_to else None,
        "observed_at": item.observed_at.isoformat() if item.observed_at else None,
        "stability": item.stability,
        "salience": item.salience,
        "sensitivity": item.sensitivity,
        "conflict_state": item.conflict_state,
        "score": item.score,
    }
