"""Single decision point for generation-bound response and TTS evidence."""

from __future__ import annotations

from typing import Literal

from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.response_planner_client import ResponsePlan
from services.common.companion_response_safety import CRISIS_SUPPORT_REPLY, SAFE_UNKNOWN_REPLY
from services.common.companions import (
    DESIGNED_VOICE_MODEL,
    DESIGNED_VOICE_SPEAKERS,
    companion_definition,
    designed_voice_speaker_sha256,
)

VoiceKind = Literal["designed", "personal"]
PERSONAL_VOICE_MODEL = "seed-icl-2.0"
ANONYMOUS_PUBLIC_CHAT_INSTRUCTIONS = (
    "仅依据当前用户这一轮内容回答普通聊天、通用知识或临时练习。"
    "不得读取、引用或推断历史对话、账户主人的私人记忆、人格、关系或工具结果；"
    "不得写入长期记忆；不确定时明确说明。"
)
_PUBLIC_DESIGNED_SPEAKER_SHA256S = frozenset(
    digest
    for profile_id in DESIGNED_VOICE_SPEAKERS
    if (digest := designed_voice_speaker_sha256(profile_id)) is not None
)
_PLAN_VOICE_BOUND_MODES = frozenset({"self_preview", "legacy", "unknown_safe"})


def generation_voice_allowed(
    policy: ModePolicy,
    *,
    personal_voice_permitted: bool,
    profile_id: str | None,
    resource_id: str,
    speaker_sha256: str,
    voice_kind: VoiceKind,
) -> bool:
    """Validate the actual voice against one mode-frozen contract."""

    if (
        (profile_id is not None and not _bounded_profile_id(profile_id))
        or not _valid_sha256(speaker_sha256)
        or voice_kind not in {"designed", "personal"}
    ):
        return False
    references = dict(policy.references)
    if voice_kind == "personal":
        return (
            resource_id == PERSONAL_VOICE_MODEL
            and personal_voice_permitted
            and policy.mode in {"self_preview", "legacy"}
            and (policy.mode != "legacy" or references.get("legacy_voice_allowed") is True)
            and profile_id is not None
            and profile_id == references.get("voice_profile_id")
            and references.get("voice_profile_version") is not None
            and references.get("voice_provider") == "volcengine_doubao"
            and references.get("voice_model") == PERSONAL_VOICE_MODEL
            and references.get("voice_resource_id") == PERSONAL_VOICE_MODEL
            and references.get("voice_provider_expires_at") is not None
            and speaker_sha256 == references.get("voice_speaker_sha256")
        )
    if resource_id != DESIGNED_VOICE_MODEL:
        return False
    if policy.mode == "companion":
        companion = companion_definition(policy.companion_style_id)
        expected = companion.designed_voice_profile if companion is not None else None
        return (
            expected is not None
            and profile_id == expected
            and speaker_sha256 == designed_voice_speaker_sha256(expected)
        )
    if policy.mode in {"self_preview", "legacy"}:
        fallback_profile = references.get("fallback_voice_profile_id")
        return (
            isinstance(fallback_profile, str)
            and profile_id == fallback_profile
            and references.get("fallback_voice_provider") == "volcengine_doubao"
            and references.get("fallback_voice_model") == DESIGNED_VOICE_MODEL
            and references.get("fallback_voice_resource_id") == DESIGNED_VOICE_MODEL
            and speaker_sha256 == designed_voice_speaker_sha256(fallback_profile)
        )
    return (
        policy.allows_anonymous_public_conversation()
        and profile_id is None
        and speaker_sha256 in _PUBLIC_DESIGNED_SPEAKER_SHA256S
    )


def generation_voice_profile_id(
    policy: ModePolicy,
    *,
    profile_id: str,
    voice_kind: str,
) -> str | None:
    """Strip product identity from anonymous public voice evidence."""

    if voice_kind == "personal":
        return profile_id
    return profile_id if policy.mode in {"companion", "self_preview", "legacy"} else None


def generation_voice_must_match_plan(policy: ModePolicy) -> bool:
    return policy.mode in _PLAN_VOICE_BOUND_MODES


def anonymous_public_plan_allowed(
    plan: ResponsePlan,
    policy: ModePolicy,
    *,
    tts_model: str,
) -> bool:
    """Accept only a current-turn, identity-free unknown-safe response plan."""

    provenance = plan.provenance
    identity_references = (
        provenance.digital_self_version_id,
        provenance.manifest_sha256,
        provenance.persona_version_id,
        provenance.persona_version_number,
        provenance.relationship_profile_id,
        provenance.relationship_profile_version,
        provenance.actor_account_id,
        provenance.resource_owner_account_id,
        provenance.legacy_actor_role,
        provenance.legacy_grantee_account_id,
        provenance.legacy_grant_id,
        provenance.legacy_grant_snapshot_sha256,
        provenance.legacy_scope_sha256,
        provenance.legacy_shell_id,
        provenance.legacy_voice_allowed,
        provenance.legacy_expires_at,
    )
    return (
        policy.allows_anonymous_public_conversation(provenance.speaker_class)
        and provenance.interaction_mode == policy.mode
        and provenance.mode_policy_version == policy.policy_version
        and provenance.planner_policy_version == "local-safe-fallback-v1"
        and plan.instructions == ANONYMOUS_PUBLIC_CHAT_INSTRUCTIONS
        and plan.direct_text in {None, SAFE_UNKNOWN_REPLY, CRISIS_SUPPORT_REPLY}
        and plan.epistemic_status == provenance.epistemic_status == "not_applicable"
        and plan.epistemic_reason_codes == provenance.epistemic_reason_codes
        and bool(plan.epistemic_reason_codes)
        and plan.epistemic_reason_codes[0] == "local_safe_fallback"
        and not plan.grounded_items
        and not provenance.source_refs
        and plan.disclosures == provenance.disclosures == ("privacy_refusal", "unknown")
        and plan.voice_target.kind == "fallback"
        and plan.voice_target.profile_id is None
        and plan.voice_target.model == tts_model
        and provenance.speaker_profile_id is None
        and provenance.speaker_template_version is None
        and not provenance.persona_style_only
        and all(value is None for value in identity_references)
        and provenance.evolution_contract_version is None
        and not provenance.evolution_artifacts
        and provenance.evolution_receipt is None
    )


def _bounded_profile_id(value: str) -> bool:
    return bool(value) and len(value) <= 128 and value == value.strip()


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


__all__ = [
    "ANONYMOUS_PUBLIC_CHAT_INSTRUCTIONS",
    "VoiceKind",
    "anonymous_public_plan_allowed",
    "generation_voice_allowed",
    "generation_voice_must_match_plan",
    "generation_voice_profile_id",
]
