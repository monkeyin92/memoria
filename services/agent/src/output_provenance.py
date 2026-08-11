"""Bounded output/evidence provenance construction (deep module).

DuplexRuntime delegates persona/mode-policy/acoustic evidence building here so
its call sites stay thin and the shared archive/UI fields are derived in one
place.  Nothing here grants permission: callers must already have checked the
signed RuntimeProfile gate (``profile_permits``) for the side effect.
"""

from __future__ import annotations

import math
from typing import Literal, cast

from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.orchestration.speaker_verify import voiced_stats_from_pcm
from services.speaker.domain import SpeakerDecision

SpeakerClass = Literal["owner", "guest", "uncertain"]


def speaker_persona_provenance(
    decision: SpeakerDecision | None,
) -> dict[str, object]:
    """Bind Persona eligibility to the decision for this exact speech epoch."""

    if decision is None:
        return {}
    return {
        "speaker_reason_code": decision.reason_code,
        "speaker_profile_id": decision.profile_id,
        "speaker_quality_score": decision.quality_score,
        "speaker_model_version": decision.model_version,
        "speaker_template_version": decision.template_version,
    }


def mode_policy_provenance(
    policy: ModePolicy,
    speaker_class: str,
    *,
    history_eligible: bool | None = None,
    owner_projection_eligible: bool | None = None,
    reason_code: str | None = None,
) -> dict[str, object]:
    """Project the frozen mode policy onto archive evidence."""

    supported_speaker = cast(
        SpeakerClass,
        speaker_class if speaker_class in {"owner", "guest", "uncertain"} else "uncertain",
    )
    return {
        "interaction_mode": policy.mode or "unavailable",
        "mode_policy_version": policy.policy_version or "unavailable",
        "simulated_output": policy.mode in {"self_preview", "legacy"},
        "history_eligible": (
            history_eligible
            if history_eligible is not None
            else policy.history_eligible(supported_speaker, reason_code=reason_code)
        ),
        "owner_projection_eligible": (
            owner_projection_eligible
            if owner_projection_eligible is not None
            else policy.owner_projection_eligible(supported_speaker)
        ),
    }


def owner_acoustic_evidence(
    *,
    speaker_class: str,
    decision: SpeakerDecision | None,
    owner_projection_eligible: bool,
    profile_permits_memory_capture: bool,
    pcm: bytearray | bytes,
    sample_rate: int,
) -> dict[str, int | float]:
    """Acoustic capture evidence for one verified owner turn.

    Acoustic projection writes capture evidence: it needs the capture
    authority (``memory_capture``), never the read authority (audit 5).
    """

    if (
        speaker_class != "owner"
        or decision is None
        or not owner_projection_eligible
        or not profile_permits_memory_capture
        or decision.classification != "owner"
        or not decision.profile_id
        or decision.template_version is None
        or decision.template_version < 1
        or not pcm
    ):
        return {}
    stats = voiced_stats_from_pcm(bytes(pcm), sample_rate=sample_rate)
    speech_ms = int(stats["speech_ms"])
    duty = float(stats["duty"])
    quality_score = float(decision.quality_score)
    if (
        speech_ms <= 0
        or speech_ms > 600_000
        or not math.isfinite(duty)
        or not 0 <= duty <= 1
        or not math.isfinite(quality_score)
        or not 0 <= quality_score <= 1
    ):
        return {}
    return {
        "speech_ms": speech_ms,
        "pause_ratio": min(1.0, max(0.0, 1.0 - duty)),
        "quality_score": quality_score,
    }


__all__ = [
    "mode_policy_provenance",
    "owner_acoustic_evidence",
    "speaker_persona_provenance",
]
