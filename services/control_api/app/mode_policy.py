"""One server-side policy for S2 interaction modes and frozen sessions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Final, Literal, cast

from services.common.companions import COMPANION_STYLE_VERSION, CompanionDefinition

InteractionMode = Literal["companion", "self_preview", "legacy", "archive"]
SpeakerClass = Literal["owner", "guest", "uncertain"]
MODE_POLICY_VERSION: Final = "s2-v1"
SELF_PREVIEW_POLICY_VERSION: Final = "s8-v1"


@dataclass(frozen=True, slots=True)
class FrozenMode:
    interaction_mode: InteractionMode
    mode_policy_version: str
    digital_self_version_id: str | None
    manifest_sha256: str | None
    preview_grant_id: str | None
    perspective: str | None
    relationship_profile_id: str | None
    legacy_grant_id: str | None
    companion_style_id: str | None
    companion_style_version: str | None
    voice_profile_id: str | None = None
    voice_profile_version: int | None = None
    voice_provider: str | None = None
    voice_model: str | None = None
    voice_resource_id: str | None = None
    voice_provider_expires_at: str | None = None
    voice_speaker_sha256: str | None = None
    fallback_voice_profile_id: str | None = None
    fallback_voice_provider: str | None = None
    fallback_voice_model: str | None = None
    fallback_voice_resource_id: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "interaction_mode": self.interaction_mode,
            "mode_policy_version": self.mode_policy_version,
            "digital_self_version_id": self.digital_self_version_id,
            "manifest_sha256": self.manifest_sha256,
            "preview_grant_id": self.preview_grant_id,
            "perspective": self.perspective,
            "relationship_profile_id": self.relationship_profile_id,
            "legacy_grant_id": self.legacy_grant_id,
            "companion_style_id": self.companion_style_id,
            "companion_style_version": self.companion_style_version,
            "voice_profile_id": self.voice_profile_id,
            "voice_profile_version": self.voice_profile_version,
            "voice_provider": self.voice_provider,
            "voice_model": self.voice_model,
            "voice_resource_id": self.voice_resource_id,
            "voice_provider_expires_at": self.voice_provider_expires_at,
            "voice_speaker_sha256": self.voice_speaker_sha256,
            "fallback_voice_profile_id": self.fallback_voice_profile_id,
            "fallback_voice_provider": self.fallback_voice_provider,
            "fallback_voice_model": self.fallback_voice_model,
            "fallback_voice_resource_id": self.fallback_voice_resource_id,
        }

    @classmethod
    def from_session(cls, session: Mapping[str, Any]) -> FrozenMode:
        return cls(
            interaction_mode=cast(InteractionMode, str(session["interaction_mode"])),
            mode_policy_version=str(session["mode_policy_version"]),
            digital_self_version_id=_optional(session.get("digital_self_version_id")),
            manifest_sha256=_optional(session.get("digital_self_manifest_sha256")),
            preview_grant_id=_optional(session.get("preview_grant_id")),
            perspective=_optional(session.get("self_preview_perspective")),
            relationship_profile_id=_optional(session.get("relationship_profile_id")),
            legacy_grant_id=_optional(session.get("legacy_grant_id")),
            companion_style_id=_optional(session.get("companion_style_id")),
            companion_style_version=_optional(session.get("companion_style_version")),
            voice_profile_id=_optional(session.get("voice_profile_id")),
            voice_profile_version=_optional_int(session.get("voice_profile_version")),
            voice_provider=_optional(session.get("voice_provider")),
            voice_model=_optional(session.get("voice_model")),
            voice_resource_id=_optional(session.get("voice_resource_id")),
            voice_provider_expires_at=_optional(session.get("voice_provider_expires_at")),
            voice_speaker_sha256=_optional(session.get("voice_speaker_sha256")),
            fallback_voice_profile_id=_optional(session.get("fallback_voice_profile_id")),
            fallback_voice_provider=_optional(session.get("fallback_voice_provider")),
            fallback_voice_model=_optional(session.get("fallback_voice_model")),
            fallback_voice_resource_id=_optional(session.get("fallback_voice_resource_id")),
        )


def _optional(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


@dataclass(frozen=True, slots=True)
class EffectiveCapabilities:
    conversation: bool
    private_memory: bool
    persona: bool
    persona_low_sensitivity: bool
    tools: bool
    history: bool
    learning: bool
    voice_profile: bool

    def payload(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ModeAvailability:
    status: Literal["available", "blocked"]
    conversational: bool
    missing: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status, "conversational": self.conversational}
        if self.missing:
            result["missing"] = list(self.missing)
        return result


class ModePolicy:
    """Small policy seam; S3/S7/S9 will supply currently missing dependencies."""

    @staticmethod
    def availability(mode: InteractionMode) -> ModeAvailability:
        if mode == "companion":
            return ModeAvailability("available", True)
        if mode == "archive":
            return ModeAvailability("available", False)
        if mode == "self_preview":
            return ModeAvailability("available", True)
        return ModeAvailability(
            "blocked",
            True,
            ("frozen_digital_self_version", "relationship_profile", "legacy_grant"),
        )

    @staticmethod
    def freeze_companion(definition: CompanionDefinition) -> FrozenMode:
        return FrozenMode(
            interaction_mode="companion",
            mode_policy_version=MODE_POLICY_VERSION,
            digital_self_version_id=None,
            manifest_sha256=None,
            preview_grant_id=None,
            perspective=None,
            relationship_profile_id=None,
            legacy_grant_id=None,
            companion_style_id=definition.companion_id,
            companion_style_version=COMPANION_STYLE_VERSION,
            voice_profile_id=None,
            voice_profile_version=None,
            voice_provider=None,
            voice_model=None,
            voice_resource_id=None,
            voice_provider_expires_at=None,
            voice_speaker_sha256=None,
            fallback_voice_profile_id=None,
            fallback_voice_provider=None,
            fallback_voice_model=None,
            fallback_voice_resource_id=None,
        )

    @staticmethod
    def freeze_self_preview(
        *,
        version_id: str,
        manifest_sha256: str,
        preview_grant_id: str,
        perspective: str,
        voice_profile_id: str | None = None,
        voice_profile_version: int | None = None,
        voice_provider: str | None = None,
        voice_model: str | None = None,
        voice_resource_id: str | None = None,
        voice_provider_expires_at: str | None = None,
        voice_speaker_sha256: str | None = None,
        fallback_voice_profile_id: str,
        fallback_voice_provider: str,
        fallback_voice_model: str,
        fallback_voice_resource_id: str,
    ) -> FrozenMode:
        return FrozenMode(
            interaction_mode="self_preview",
            mode_policy_version=SELF_PREVIEW_POLICY_VERSION,
            digital_self_version_id=version_id,
            manifest_sha256=manifest_sha256,
            preview_grant_id=preview_grant_id,
            perspective=perspective,
            relationship_profile_id=None,
            legacy_grant_id=None,
            companion_style_id=None,
            companion_style_version=None,
            voice_profile_id=voice_profile_id,
            voice_profile_version=voice_profile_version,
            voice_provider=voice_provider,
            voice_model=voice_model,
            voice_resource_id=voice_resource_id,
            voice_provider_expires_at=voice_provider_expires_at,
            voice_speaker_sha256=voice_speaker_sha256,
            fallback_voice_profile_id=fallback_voice_profile_id,
            fallback_voice_provider=fallback_voice_provider,
            fallback_voice_model=fallback_voice_model,
            fallback_voice_resource_id=fallback_voice_resource_id,
        )

    @classmethod
    def effective_capabilities(
        cls,
        frozen: FrozenMode,
        *,
        speaker_class: SpeakerClass,
        reason_code: str | None = None,
        account_active: bool = True,
    ) -> EffectiveCapabilities:
        available = cls.availability(frozen.interaction_mode).status == "available"
        owner_private = available and speaker_class == "owner"
        shadow_owner = (
            available and speaker_class == "uncertain" and reason_code == "shadow_owner_candidate"
        )
        companion = available and frozen.interaction_mode == "companion"
        self_preview = available and frozen.interaction_mode == "self_preview"
        # `uncertain` may retain S1's explicitly curated low-sensitivity style
        # path, but is never a private-owner authority.
        return EffectiveCapabilities(
            conversation=account_active
            and (companion or (self_preview and speaker_class == "owner")),
            private_memory=account_active and companion and owner_private,
            persona=account_active and companion and owner_private,
            persona_low_sensitivity=account_active and companion and shadow_owner,
            tools=account_active and companion and owner_private,
            history=account_active and companion and owner_private,
            learning=account_active and companion and owner_private,
            voice_profile=account_active
            and (companion or (self_preview and speaker_class == "owner")),
        )

    @classmethod
    def trusted_context(
        cls,
        frozen: FrozenMode,
        *,
        speaker_class: SpeakerClass,
        reason_code: str | None = None,
        history_eligible: bool | None = None,
        owner_projection_eligible: bool | None = None,
    ) -> dict[str, Any]:
        capabilities = cls.effective_capabilities(
            frozen,
            speaker_class=speaker_class,
            reason_code=reason_code,
        )
        resolved_history_eligible = (
            capabilities.history
            if history_eligible is None
            else bool(history_eligible and frozen.interaction_mode == "companion")
        )
        resolved_owner_projection_eligible = (
            capabilities.history and speaker_class == "owner"
            if owner_projection_eligible is None
            else bool(owner_projection_eligible and frozen.interaction_mode == "companion")
        )
        return {
            **frozen.payload(),
            "mode_policy_version": frozen.mode_policy_version,
            "simulated_output": frozen.interaction_mode != "companion",
            "history_eligible": resolved_history_eligible,
            "owner_projection_eligible": resolved_owner_projection_eligible,
            "capabilities": capabilities.payload(),
        }

    @classmethod
    def session_context(cls, frozen: FrozenMode) -> dict[str, Any]:
        """Return the frozen mode ceiling, not a stale per-turn speaker result."""
        available = cls.availability(frozen.interaction_mode).status == "available"
        companion = available and frozen.interaction_mode == "companion"
        self_preview = available and frozen.interaction_mode == "self_preview"
        return {
            **frozen.payload(),
            "simulated_output": frozen.interaction_mode != "companion",
            "history_eligible": companion,
            "owner_projection_eligible": companion,
            "policy_scope": "session",
            "capabilities": EffectiveCapabilities(
                conversation=companion or self_preview,
                private_memory=companion,
                persona=companion,
                persona_low_sensitivity=companion,
                tools=companion,
                history=companion,
                learning=companion,
                voice_profile=companion or self_preview,
            ).payload(),
        }
