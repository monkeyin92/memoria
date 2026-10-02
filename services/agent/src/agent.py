"""Response-plan admission policy for the reply pipeline.

``plan_matches_mode_policy`` binds a fetched plan to the fence's frozen
interaction policy, and ``build_local_safe_plan`` is the fail-closed plan used
whenever no authorized plan is available. ``ReplyPipeline``
(``reply_pipeline.py``) is the only production caller.
"""

from __future__ import annotations

import os
from typing import Literal, cast

from services.agent.src import generation_output_policy as output_policy
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.orchestration.interruption_guard import is_primarily_non_chinese_script
from services.agent.src.prompts import BRIDGE_PHRASES
from services.agent.src.response_planner_client import (
    CANONICAL_PLANNER_POLICY_VERSION,
    Disclosure,
    ResponsePlan,
    ResponseProvenance,
    ResponseVoiceTarget,
)
from services.common.companion_response_safety import (
    CRISIS_SUPPORT_REPLY,
    SAFE_UNKNOWN_REPLY,
    companion_identity_replies,
    fixed_companion_reply,
)
from services.common.companion_turn_policy import (
    COMPANION_TURN_POLICY_INSTRUCTIONS,
    companion_scope_instructions,
)
from services.common.companions import DESIGNED_VOICE_MODEL, companion_definition
from services.common.realtime_information import (
    current_local_time,
    fixed_realtime_reply,
    is_fuzzy_weekday_query,
    is_safe_realtime_reply,
    realtime_instruction,
)
from services.speaker.domain import DEVICE_BOUND_SUBJECT_REASON

_LOCAL_SAFE_REFUSAL_INSTRUCTIONS = "禁止生成普通回答；仅返回固定安全拒答。"
_LOCAL_SAFE_REFUSAL_TEXT = "当前模式暂时无法安全生成回答。"


def plan_is_local_safe(plan: ResponsePlan) -> bool:
    return plan.provenance.planner_policy_version == "local-safe-fallback-v1"


def plan_matches_mode_policy(
    plan: ResponsePlan,
    policy: ModePolicy,
    *,
    tts_model: str,
) -> bool:
    """Allow a plan only when it is bound to this fence's frozen policy."""

    provenance = plan.provenance
    references = dict(policy.references)
    if (
        policy.mode is None
        or provenance.interaction_mode != policy.mode
        or provenance.mode_policy_version != policy.policy_version
    ):
        return False
    if plan_is_local_safe(plan):
        if output_policy.anonymous_public_plan_allowed(plan, policy, tts_model=tts_model):
            return True
        unknown_safe_direct = (
            policy.mode == "unknown_safe"
            and plan.direct_text is not None
            and (is_safe_realtime_reply(plan.direct_text) or plan.direct_text in BRIDGE_PHRASES)
            and plan.instructions == output_policy.ANONYMOUS_PUBLIC_CHAT_INSTRUCTIONS
            and not plan.grounded_items
            and not provenance.source_refs
            and plan.disclosures == provenance.disclosures == ("privacy_refusal", "unknown")
            and provenance.planner_policy_version == "local-safe-fallback-v1"
            and _fallback_voice_target_matches(plan.voice_target, policy, tts_model)
        )
        if unknown_safe_direct:
            return True
        companion = companion_definition(policy.companion_style_id)
        allowed_companion_direct_text = {None, SAFE_UNKNOWN_REPLY, CRISIS_SUPPORT_REPLY}
        if companion is not None:
            allowed_companion_direct_text |= companion_identity_replies(
                companion.display_name, companion.style_description
            )
        if is_safe_realtime_reply(plan.direct_text):
            allowed_companion_direct_text.add(plan.direct_text)
        companion_safe = (
            policy.mode == "companion"
            and companion is not None
            and plan.direct_text in allowed_companion_direct_text
            and not plan.grounded_items
            and not provenance.source_refs
            and plan.disclosures == ("privacy_refusal", "unknown")
            and provenance.disclosures == plan.disclosures
        )
        refusal_safe = (
            policy.mode in {"self_preview", "legacy"}
            and plan.instructions == _LOCAL_SAFE_REFUSAL_INSTRUCTIONS
            and (plan.direct_text in {_LOCAL_SAFE_REFUSAL_TEXT, CRISIS_SUPPORT_REPLY} or is_safe_realtime_reply(plan.direct_text))
            and not plan.grounded_items
            and not plan.provenance.source_refs
            and plan.disclosures
            == (
                ("digital_identity", "privacy_refusal", "unknown")
                if policy.mode == "legacy"
                else ("privacy_refusal", "unknown")
            )
            and plan.provenance.disclosures == plan.disclosures
        )
        relationship_version = provenance.relationship_profile_version
        relationship_safe = provenance.relationship_profile_id == references.get(
            "relationship_profile_id"
        ) and (
            str(relationship_version) if relationship_version is not None else None
        ) == references.get("relationship_profile_version")
        frozen_snapshot_safe = (
            policy.mode == "companion"
            and provenance.digital_self_version_id is None
            and provenance.manifest_sha256 is None
            and provenance.relationship_profile_id is None
            and provenance.relationship_profile_version is None
            and _legacy_provenance_absent(provenance)
        ) or (
            policy.mode in {"self_preview", "legacy"}
            and provenance.digital_self_version_id == references.get("digital_self_version_id")
            and provenance.manifest_sha256 == references.get("manifest_sha256")
            and relationship_safe
            and (
                _legacy_provenance_absent(provenance)
                if policy.mode == "self_preview"
                else _legacy_provenance_matches(provenance, references)
            )
        )
        return (
            (companion_safe or refusal_safe)
            and frozen_snapshot_safe
            and _fallback_voice_target_matches(plan.voice_target, policy, tts_model)
        )
    if provenance.planner_policy_version != CANONICAL_PLANNER_POLICY_VERSION:
        return False
    if policy.mode == "companion":
        companion = companion_definition(policy.companion_style_id)
        return (
            companion is not None
            and plan.voice_target.kind == "companion"
            and plan.voice_target.profile_id == companion.designed_voice_profile
            and plan.voice_target.model == DESIGNED_VOICE_MODEL
            and provenance.digital_self_version_id is None
            and provenance.manifest_sha256 is None
            and _persona_snapshot_matches(provenance)
            and provenance.relationship_profile_id is None
            and provenance.relationship_profile_version is None
            and _legacy_provenance_absent(provenance)
        )
    if policy.mode not in {"self_preview", "legacy"}:
        return False
    if (
        provenance.digital_self_version_id is None
        or provenance.manifest_sha256 is None
        or references.get("digital_self_version_id") != provenance.digital_self_version_id
        or references.get("manifest_sha256") != provenance.manifest_sha256
    ):
        return False
    if policy.mode == "self_preview" and not _legacy_provenance_absent(provenance):
        return False
    if provenance.relationship_profile_id is None:
        if provenance.relationship_profile_version is not None:
            return False
    elif (
        provenance.relationship_profile_version is None
        or references.get("relationship_profile_id") != provenance.relationship_profile_id
        or references.get("relationship_profile_version")
        != str(provenance.relationship_profile_version)
    ):
        return False
    if policy.mode == "legacy":
        if (
            "digital_identity" not in plan.disclosures
            or "digital_identity" not in provenance.disclosures
            or provenance.speaker_class != "owner"
            or not _legacy_provenance_matches(provenance, references)
        ):
            return False
    if plan.voice_target.kind == "fallback":
        return _fallback_voice_target_matches(plan.voice_target, policy, tts_model)
    return (
        plan.voice_target.kind == "approved_personal"
        and plan.voice_target.profile_id is not None
        and (policy.mode != "legacy" or references.get("legacy_voice_allowed") is True)
        and references.get("voice_profile_id") == plan.voice_target.profile_id
        and references.get("voice_model") == plan.voice_target.model
    )


def _legacy_provenance_absent(provenance: ResponseProvenance) -> bool:
    return all(value is None for value in (provenance.actor_account_id, provenance.resource_owner_account_id, provenance.legacy_actor_role, provenance.legacy_grantee_account_id, provenance.legacy_grant_id, provenance.legacy_grant_snapshot_sha256, provenance.legacy_scope_sha256, provenance.legacy_shell_id, provenance.legacy_voice_allowed, provenance.legacy_expires_at))


def _legacy_provenance_matches(
    provenance: ResponseProvenance,
    references: dict[str, str | bool | None],
) -> bool:
    return (
        provenance.actor_account_id == references.get("actor_account_id")
        and provenance.resource_owner_account_id == references.get("resource_owner_account_id")
        and provenance.legacy_actor_role == references.get("legacy_actor_role")
        and provenance.legacy_grantee_account_id == references.get("legacy_grantee_account_id")
        and provenance.legacy_grant_id == references.get("legacy_grant_id")
        and provenance.legacy_grant_snapshot_sha256 == references.get("legacy_grant_snapshot_sha256")
        and provenance.legacy_scope_sha256 == references.get("legacy_scope_sha256")
        and provenance.legacy_shell_id == references.get("legacy_shell_id")
        and provenance.legacy_voice_allowed == references.get("legacy_voice_allowed")
        and provenance.legacy_expires_at == references.get("legacy_expires_at")
    )


def _fallback_voice_target_matches(
    voice_target: ResponseVoiceTarget,
    policy: ModePolicy,
    tts_model: str,
) -> bool:
    references = dict(policy.references)
    if policy.mode in {"self_preview", "legacy"}:
        return (
            voice_target.kind == "fallback"
            and voice_target.profile_id == references.get("fallback_voice_profile_id")
            and voice_target.model == references.get("fallback_voice_model")
        )
    return (
        voice_target.kind == "fallback"
        and voice_target.profile_id is None
        and voice_target.model == tts_model
    )


def _persona_snapshot_matches(provenance: ResponseProvenance) -> bool:
    absent = provenance.persona_version_id is None and provenance.persona_version_number is None
    present = (
        provenance.persona_version_id is not None
        and provenance.persona_version_number is not None
    )
    if not absent and not present:
        return False
    if absent:
        return not provenance.persona_style_only
    if not provenance.persona_style_only:
        return provenance.speaker_class == "owner"
    return (
        provenance.speaker_class == "uncertain"
        and provenance.speaker_reason_code == "shadow_owner_candidate"
        and provenance.persona_version_id is not None
        and not provenance.source_refs
    )



def build_local_safe_plan(
    *,
    policy: ModePolicy,
    fence: GenerationFence,
    speaker: object,
    reason: str,
    tts_model: str,
    query: str = "",
) -> ResponsePlan:
    """The fail-closed plan for ``fence`` when no authorized plan is usable."""

    mode = policy.mode or "legacy"
    raw_speaker_class = getattr(speaker, "classification", "uncertain")
    speaker_class = cast(
        Literal["owner", "guest", "uncertain"],
        raw_speaker_class
        if raw_speaker_class in {"owner", "guest", "uncertain"}
        else "uncertain",
    )
    anonymous_public = policy.allows_anonymous_public_conversation(speaker_class)
    speaker_reason = getattr(speaker, "reason_code", "speaker_unavailable")
    speaker_model = getattr(speaker, "model_version", "unknown")
    speaker_profile = None if anonymous_public else getattr(speaker, "profile_id", None)
    speaker_template = None if anonymous_public else getattr(speaker, "template_version", None)
    companion = mode == "companion"
    companion_definition_for_policy = companion_definition(policy.companion_style_id)
    runtime_profile = policy.runtime_profile
    fixed_reply = fixed_companion_reply(
        query=query,
        is_companion=companion,
        audience=runtime_profile.profile.service_mode if runtime_profile is not None else None,
        display_name=(
            companion_definition_for_policy.display_name
            if companion and companion_definition_for_policy is not None
            else None
        ),
        style_description=(
            companion_definition_for_policy.style_description
            if companion and companion_definition_for_policy is not None
            else None
        ),
    )
    live_now = current_local_time(os.getenv("MEMORIA_TIMEZONE", "Asia/Shanghai"))
    if fixed_reply is None:
        lookup_query = "今天星期几" if is_fuzzy_weekday_query(query) else query
        fixed_reply = fixed_realtime_reply(query=lookup_query, now=live_now)
    if fixed_reply is None and is_primarily_non_chinese_script(query):
        fixed_reply = BRIDGE_PHRASES[2]
    if policy.mode == "unknown_safe" and fixed_reply is not None and (
        is_safe_realtime_reply(fixed_reply) or fixed_reply in BRIDGE_PHRASES
    ):
        # Clock/date facts and bridge nudges use the anonymous public surface
        # even when the degraded profile still carries non-conversation caps.
        anonymous_public = True
    references = dict(policy.references)
    relationship_version_raw = references.get("relationship_profile_version")
    relationship_version = (
        int(relationship_version_raw)
        if isinstance(relationship_version_raw, str) and relationship_version_raw.isdigit()
        else None
    )
    refusal_disclosures: tuple[Disclosure, ...] = (
        ("digital_identity", "privacy_refusal", "unknown")
        if mode == "legacy"
        else ("privacy_refusal", "unknown")
    )
    instructions = (
        output_policy.ANONYMOUS_PUBLIC_CHAT_INSTRUCTIONS
        if anonymous_public
        else _LOCAL_SAFE_REFUSAL_INSTRUCTIONS
    )
    if companion:
        instructions = companion_scope_instructions(
            owner=speaker_class == "owner",
            device_bound=speaker_reason == DEVICE_BOUND_SUBJECT_REASON,
            private_memory=policy.allows_private_context(speaker_class),
        )
        instructions += "\n" + COMPANION_TURN_POLICY_INSTRUCTIONS
    if companion and policy.companion_style_prompt is not None:
        instructions += "\n" + policy.companion_style_prompt
    live_instruction = realtime_instruction(query=query, now=live_now)
    if companion and live_instruction is not None:
        instructions += "\n" + live_instruction
    return ResponsePlan(
        fence=fence,
        instructions=instructions,
        direct_text=(
            fixed_reply
            if companion
            or anonymous_public
            or fixed_reply == CRISIS_SUPPORT_REPLY
            or fixed_reply in BRIDGE_PHRASES
            or is_safe_realtime_reply(fixed_reply)
            else _LOCAL_SAFE_REFUSAL_TEXT
        ),
        epistemic_status="not_applicable",
        epistemic_reason_codes=("local_safe_fallback", reason),
        grounded_items=(),
        disclosures=("privacy_refusal", "unknown") if companion else refusal_disclosures,
        voice_target=ResponseVoiceTarget(
            kind="fallback",
            profile_id=(
                None
                if companion or anonymous_public
                else cast(str | None, references.get("fallback_voice_profile_id"))
            ),
            model=(
                tts_model
                if companion or anonymous_public
                else cast(str, references.get("fallback_voice_model"))
            ),
        ),
        provenance=ResponseProvenance(
            planner_policy_version="local-safe-fallback-v1",
            interaction_mode=mode,
            mode_policy_version=policy.policy_version or "unavailable",
            digital_self_version_id=(
                None
                if companion
                else cast(str | None, references.get("digital_self_version_id"))
            ),
            manifest_sha256=(
                None if companion else cast(str | None, references.get("manifest_sha256"))
            ),
            persona_version_id=None,
            persona_version_number=None,
            persona_style_only=False,
            relationship_profile_id=(
                None
                if companion
                else cast(str | None, references.get("relationship_profile_id"))
            ),
            relationship_profile_version=(None if companion else relationship_version),
            speaker_class=speaker_class,
            speaker_reason_code=speaker_reason,
            speaker_profile_id=speaker_profile,
            speaker_model_version=speaker_model,
            speaker_template_version=speaker_template,
            source_refs=(),
            epistemic_status="not_applicable",
            epistemic_reason_codes=("local_safe_fallback", reason),
            disclosures=(("privacy_refusal", "unknown") if companion else refusal_disclosures),
            actor_account_id=(
                cast(str | None, references.get("actor_account_id"))
                if mode == "legacy"
                else None
            ),
            resource_owner_account_id=(
                cast(str | None, references.get("resource_owner_account_id"))
                if mode == "legacy"
                else None
            ),
            legacy_actor_role=(
                cast(
                    Literal["owner_preview", "grantee"] | None,
                    references.get("legacy_actor_role"),
                )
                if mode == "legacy"
                else None
            ),
            legacy_grantee_account_id=(
                cast(str | None, references.get("legacy_grantee_account_id"))
                if mode == "legacy"
                else None
            ),
            legacy_grant_id=(
                cast(str | None, references.get("legacy_grant_id"))
                if mode == "legacy"
                else None
            ),
            legacy_grant_snapshot_sha256=(
                cast(str | None, references.get("legacy_grant_snapshot_sha256"))
                if mode == "legacy"
                else None
            ),
            legacy_scope_sha256=(
                cast(str | None, references.get("legacy_scope_sha256"))
                if mode == "legacy"
                else None
            ),
            legacy_shell_id=(
                cast(str | None, references.get("legacy_shell_id"))
                if mode == "legacy"
                else None
            ),
            legacy_voice_allowed=(
                cast(bool | None, references.get("legacy_voice_allowed"))
                if mode == "legacy"
                else None
            ),
            legacy_expires_at=(
                cast(str | None, references.get("legacy_expires_at"))
                if mode == "legacy"
                else None
            ),
        ),
    )


__all__ = ["build_local_safe_plan", "plan_is_local_safe", "plan_matches_mode_policy"]
