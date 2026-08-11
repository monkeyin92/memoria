"""Complete §11.5 event envelopes and archive-evidence construction.

Deep module: DuplexRuntime delegates envelope/evidence building here so the
runtime call sites stay thin.  The authoritative fence fields are always
overwritten from the event's own ``GenerationFence`` (forged/pre-filled
fields cannot survive); ``fence=None`` means a lifecycle-only epoch-0
envelope, never the current generation fence.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.events import UI_EVENT_TYPES
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.obligation_executor import PersistenceDecision

if TYPE_CHECKING:
    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.src.runtime_profile_gate import RuntimeProfileGate


def publish_ui_event(
    runtime: DuplexRuntime,
    event: dict[str, object],
    fence: GenerationFence | None,
) -> asyncio.Task[Any] | None:
    """Publish one UI event with the complete §11.5 envelope (deep module).

    Identity is unconditionally overwritten from the event's own fence so a
    forged/pre-filled envelope can never survive; ``fence=None`` means a
    lifecycle-only epoch-0 envelope, never the current generation fence.
    """

    if event.get("type") not in UI_EVENT_TYPES:
        raise ValueError("unsupported voice-agent.ui event type")
    event["session_id"] = runtime.session_id
    event.update(
        event_envelope(
            fence, gate=runtime.orchestrator.runtime_profiles, current_fence=runtime.fence
        )
    )
    runtime._event_sequence += 1
    event["event_sequence"] = runtime._event_sequence
    if runtime._event_publisher is not None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Teardown: the loop is already closed.  Never create an
            # orphaned publisher coroutine (a late finally must not spawn a
            # UI event outside a running loop).
            return None
        return runtime._spawn(
            runtime._event_publisher(event),
            name=f"duplex-ui-{event['type']}",
        )
    return None


EPOCH_ZERO_ENVELOPE: dict[str, object] = {
    "session_epoch": 0,
    "turn_id": 0,
    "generation_id": 0,
    "tool_epoch": 0,
    "active_subject_id": None,
    "runtime_profile_id": None,
    "actor_id": None,
    "binding_id": None,
    "binding_version": None,
    "device_id": None,
    "subject_revision": None,
}


def event_envelope(
    fence: GenerationFence | None,
    *,
    gate: RuntimeProfileGate,
    current_fence: GenerationFence,
) -> dict[str, object]:
    """Authoritative §11.5 envelope; None means an epoch-0 lifecycle event."""

    if fence is None:
        return dict(EPOCH_ZERO_ENVELOPE)
    envelope: dict[str, object] = {
        "session_epoch": fence.session_epoch,
        "turn_id": fence.turn_id,
        "generation_id": fence.generation_id,
        "tool_epoch": fence.tool_epoch,
    }
    envelope.update(gate.event_identity(fence, current_fence=current_fence))
    return envelope


def evidence_fingerprint(
    *,
    session_id: str,
    event_type: str,
    speaker: str,
    fence: GenerationFence,
    text: str,
) -> str:
    """Stable content fingerprint for one archive evidence record."""

    material = (
        f"{session_id}\0{event_type}\0{speaker}\0"
        f"{fence.turn_id}\0{fence.generation_id}\0"
        f"{fence.tool_epoch}\0{fence.session_epoch}\0{text}"
    )
    return hashlib.sha256(material.encode()).hexdigest()


def archive_evidence(
    *,
    event_type: str,
    session_id: str,
    speaker_class: str,
    payload: dict[str, Any],
    source: str,
    fence: GenerationFence,
    decision: PersistenceDecision,
    fingerprint: str,
    event_sequence: int,
) -> dict[str, object]:
    """One archive record with the full re-verifiable MemoryWriteFence."""

    evidence: dict[str, object] = {
        "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:evidence:{fingerprint}")),
        "session_id": session_id,
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "speaker_class": speaker_class,
        "source": source,
        "turn_id": fence.turn_id,
        "generation_id": fence.generation_id,
        "tool_epoch": fence.tool_epoch,
        "session_epoch": decision.session_epoch,
        "device_id": decision.device_id,
        "subject_revision": decision.subject_revision,
        "active_subject_id": decision.subject_id,
        "runtime_profile_id": decision.runtime_profile_id,
        "actor_id": decision.actor_id,
        "binding_id": decision.binding_id,
        "binding_version": decision.binding_version,
        "memory_scope": decision.memory_scope,
        "policy_receipt_id": decision.memory_capture_receipt_id,
        "raw_audio_receipt_id": decision.raw_audio_receipt_id,
        "training_receipt_id": decision.training_receipt_id,
        "no_model_training": decision.no_model_training,
        "event_sequence": event_sequence,
        "payload": (
            {
                "aggregate_only": True,
                "session_epoch": decision.session_epoch,
                "active_subject_id": decision.subject_id,
                "runtime_profile_id": decision.runtime_profile_id,
            }
            if decision.aggregate_only
            else payload
        ),
    }
    return evidence


def preview_provenance(
    provenance: object,
    fence: GenerationFence,
) -> dict[str, object] | None:
    """Bounded digital-self disclosure for one archived assistant turn."""

    if not (
        isinstance(provenance, dict)
        and isinstance(provenance.get("digital_self_version_id"), str)
        and isinstance(provenance.get("manifest_sha256"), str)
    ):
        return None
    refs = [
        {
            "kind": str(raw.get("kind"))[:64],
            "item_id": str(raw.get("item_id"))[:128],
            "source_event_ids": [str(value)[:128] for value in raw.get("source_event_ids", [])[:8]],
        }
        for raw in provenance.get("source_refs", [])[:12]
        if isinstance(raw, dict)
        and isinstance(raw.get("kind"), str)
        and isinstance(raw.get("item_id"), str)
        and isinstance(raw.get("source_event_ids"), list)
    ]
    disclosures = provenance.get("disclosures")
    return {
        "digital_self_version_id": provenance["digital_self_version_id"][:128],
        "manifest_sha256": provenance["manifest_sha256"][:64],
        "turn_id": fence.turn_id,
        "generation_id": fence.generation_id,
        "tool_epoch": fence.tool_epoch,
        "epistemic_status": str(provenance.get("epistemic_status") or "unknown")[:32],
        "disclosures": (
            [str(value)[:64] for value in disclosures[:4]] if isinstance(disclosures, list) else []
        ),
        "source_refs": refs,
    }


def transcript_delta_event(
    *,
    session_id: str,
    speaker: str,
    text: str,
    final: bool,
    fence: GenerationFence,
    revision: int,
    history_eligible: bool,
    heard: bool | None,
    text_delivered: bool,
    preview: dict[str, object] | None,
) -> dict[str, object]:
    """One UI transcript delta with the event's own fence fields."""

    event: dict[str, object] = {
        "type": "transcript_delta",
        "session_id": session_id,
        "speaker": speaker,
        "text": text,
        "final": final,
        "turn_id": fence.turn_id,
        "generation_id": fence.generation_id,
        "turn_revision": revision,
        "tool_epoch": fence.tool_epoch,
        "history_eligible": history_eligible,
    }
    if heard is not None:
        event["heard"] = heard
    if text_delivered:
        event["text_delivered"] = True
    if preview is not None:
        event["preview_provenance"] = preview
    return event


def speaker_classification_evidence(
    *,
    session_id: str,
    epoch: int,
    decision: Any,
    turn_id: int,
    generation_id: int,
    provenance: dict[str, object],
) -> dict[str, object]:
    """One durable speaker-classification evidence record."""

    return {
        "event_id": str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"memoria:speaker-classification:{session_id}:{epoch}",
            )
        ),
        "session_id": session_id,
        "event_type": "speaker.classified",
        "occurred_at": datetime.now(UTC).isoformat(),
        "speaker_class": decision.classification,
        "source": "speaker_authority.formal_embedding",
        "turn_id": turn_id,
        "generation_id": generation_id,
        "payload": {
            "score": decision.score,
            "quality_score": decision.quality_score,
            "reason_code": decision.reason_code,
            "model_version": decision.model_version,
            "template_version": decision.template_version,
            "profile_id": decision.profile_id,
            **provenance,
        },
    }


def phase_for_published_state(state: str) -> object | None:
    """InteractionPhase for one published assistant state (UI mapping)."""

    from services.agent.src.orchestration.state_machine import InteractionPhase

    return {
        "connecting": InteractionPhase.CONNECTING,
        "ready": InteractionPhase.LISTENING,
        "speaker_enroll": InteractionPhase.LISTENING,
        "listening": InteractionPhase.LISTENING,
        "user_speaking": InteractionPhase.USER_SPEAKING,
        "backchannel": InteractionPhase.BACKCHANNEL,
        "thinking": InteractionPhase.THINKING_SILENT,
        "thinking_silent": InteractionPhase.THINKING_SILENT,
        "speaking": InteractionPhase.SPEAKING,
        "interrupted": InteractionPhase.INTERRUPTED,
        "tool_waiting": InteractionPhase.TOOL_WAITING,
        "recovering": InteractionPhase.RECOVERING,
        "closed": InteractionPhase.CLOSED,
    }.get(state)


def input_policy_for_state(state: str) -> tuple[bool, str] | None:
    """Capture policy for one published assistant state (UI mapping)."""

    if state in {
        "ready",
        "speaker_enroll",
        "listening",
        "user_speaking",
        "eot_pending",
    }:
        return True, f"assistant_{state}"
    if state == "backchannel":
        return True, "user_holds_floor"
    if state in {"thinking", "thinking_silent"}:
        return False, "assistant_thinking"
    if state == "tool_waiting":
        return False, "assistant_tool_waiting"
    if state == "speaking":
        return False, "assistant_speaking"
    if state in {"interrupted", "interruption_pending"}:
        return False, "assistant_stopping"
    if state == "recovering":
        return False, "transport_recovering"
    if state in {"connecting", "closed"}:
        return False, f"session_{state}"
    return None


__all__ = [
    "EPOCH_ZERO_ENVELOPE",
    "archive_evidence",
    "event_envelope",
    "evidence_fingerprint",
    "input_policy_for_state",
    "phase_for_published_state",
    "preview_provenance",
    "speaker_classification_evidence",
    "transcript_delta_event",
]
