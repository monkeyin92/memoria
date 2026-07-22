"""One server-side policy for S2 interaction modes and frozen sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final, Literal, cast

from services.common.companions import COMPANION_STYLE_VERSION, CompanionDefinition

InteractionMode = Literal["companion", "self_preview", "legacy", "archive"]
SpeakerClass = Literal["owner", "guest", "uncertain"]
MODE_POLICY_VERSION: Final = "s2-v1"


@dataclass(frozen=True, slots=True)
class FrozenMode:
    interaction_mode: InteractionMode
    mode_policy_version: str
    digital_self_version_id: str | None
    relationship_profile_id: str | None
    legacy_grant_id: str | None
    companion_style_id: str | None
    companion_style_version: str | None

    def payload(self) -> dict[str, str | None]:
        return {
            "interaction_mode": self.interaction_mode,
            "mode_policy_version": self.mode_policy_version,
            "digital_self_version_id": self.digital_self_version_id,
            "relationship_profile_id": self.relationship_profile_id,
            "legacy_grant_id": self.legacy_grant_id,
            "companion_style_id": self.companion_style_id,
            "companion_style_version": self.companion_style_version,
        }

    @classmethod
    def from_session(cls, session: dict[str, Any]) -> FrozenMode:
        return cls(
            interaction_mode=cast(InteractionMode, str(session["interaction_mode"])),
            mode_policy_version=str(session["mode_policy_version"]),
            digital_self_version_id=_optional(session.get("digital_self_version_id")),
            relationship_profile_id=_optional(session.get("relationship_profile_id")),
            legacy_grant_id=_optional(session.get("legacy_grant_id")),
            companion_style_id=_optional(session.get("companion_style_id")),
            companion_style_version=_optional(session.get("companion_style_version")),
        )


def _optional(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


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
            return ModeAvailability("blocked", True, ("self_preview_runtime",))
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
            relationship_profile_id=None,
            legacy_grant_id=None,
            companion_style_id=definition.companion_id,
            companion_style_version=COMPANION_STYLE_VERSION,
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
            available
            and speaker_class == "uncertain"
            and reason_code == "shadow_owner_candidate"
        )
        companion = available and frozen.interaction_mode == "companion"
        # `uncertain` may retain S1's explicitly curated low-sensitivity style
        # path, but is never a private-owner authority.
        return EffectiveCapabilities(
            conversation=account_active and companion,
            private_memory=account_active and companion and owner_private,
            persona=account_active and companion and owner_private,
            persona_low_sensitivity=account_active and companion and shadow_owner,
            tools=account_active and companion and owner_private,
            history=account_active and companion and (owner_private or shadow_owner),
            learning=account_active and companion and (owner_private or shadow_owner),
            voice_profile=account_active and companion,
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
            else bool(
                history_eligible
                and frozen.interaction_mode == "companion"
            )
        )
        resolved_owner_projection_eligible = (
            frozen.interaction_mode == "companion" and speaker_class == "owner"
            if owner_projection_eligible is None
            else bool(
                owner_projection_eligible
                and frozen.interaction_mode == "companion"
            )
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
        return {
            **frozen.payload(),
            "simulated_output": frozen.interaction_mode != "companion",
            "history_eligible": companion,
            "owner_projection_eligible": companion,
            "policy_scope": "session",
            "capabilities": EffectiveCapabilities(
                conversation=companion,
                private_memory=companion,
                persona=companion,
                persona_low_sensitivity=companion,
                tools=companion,
                history=companion,
                learning=companion,
                voice_profile=companion,
            ).payload(),
        }
