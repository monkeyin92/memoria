"""Shared helpers for the split agent production wiring tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.response_planner_client import (
    ResponseGroundedItem,
    ResponsePlan,
    ResponseProvenance,
    ResponseVoiceTarget,
)


async def _text_source(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


def _plan_for_fence(
    fence: GenerationFence,
    *,
    instructions: str,
    direct_text: str | None = None,
    grounded_items: tuple[ResponseGroundedItem, ...] = (),
    speaker_class: str = "owner",
) -> ResponsePlan:
    return ResponsePlan(
        fence=fence,
        instructions=instructions,
        direct_text=direct_text,
        epistemic_status="fact",
        epistemic_reason_codes=("grounded_manifest",),
        grounded_items=grounded_items,
        disclosures=(),
        voice_target=ResponseVoiceTarget(
            kind="companion",
            profile_id="warm_companion",
            model="seed-tts-2.0",
        ),
        provenance=ResponseProvenance(
            planner_policy_version="digital-self-response-planner-v2",
            interaction_mode="companion",
            mode_policy_version="test-policy",
            digital_self_version_id=None,
            manifest_sha256=None,
            persona_version_id=None,
            persona_version_number=None,
            persona_style_only=False,
            relationship_profile_id=None,
            relationship_profile_version=None,
            speaker_class=speaker_class,  # type: ignore[arg-type]
            speaker_reason_code=("owner_match" if speaker_class == "owner" else "owner_mismatch"),
            speaker_profile_id=("profile-owner-001" if speaker_class == "owner" else None),
            speaker_model_version="campplus-test",
            speaker_template_version=1,
            source_refs=(),
            epistemic_status="fact",
            epistemic_reason_codes=("grounded_manifest",),
            disclosures=(),
        ),
    )
