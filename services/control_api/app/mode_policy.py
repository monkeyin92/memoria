"""One server-side policy for S2 interaction modes and frozen sessions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast

from services.common.companions import COMPANION_STYLE_VERSION, CompanionDefinition

InteractionMode = Literal["companion", "self_preview", "legacy", "archive"]
SpeakerClass = Literal["owner", "guest", "uncertain"]
LegacyActorRole = Literal["owner_preview", "grantee"]
MODE_POLICY_VERSION: Final = "s2-v1"
SELF_PREVIEW_POLICY_VERSION: Final = "s8-v1"
LEGACY_POLICY_VERSION: Final = "s9-v1"


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
    actor_account_id: str | None = None
    resource_owner_account_id: str | None = None
    relationship_profile_version: int | None = None
    legacy_actor_role: LegacyActorRole | None = None
    legacy_grantee_account_id: str | None = None
    legacy_shell_id: str | None = None
    legacy_grant_snapshot_sha256: str | None = None
    legacy_scope_sha256: str | None = None
    legacy_voice_allowed: bool | None = None
    legacy_expires_at: str | None = None
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
            "relationship_profile_version": self.relationship_profile_version,
            "legacy_grant_id": self.legacy_grant_id,
            "actor_account_id": self.actor_account_id,
            "resource_owner_account_id": self.resource_owner_account_id,
            "legacy_actor_role": self.legacy_actor_role,
            "legacy_grantee_account_id": self.legacy_grantee_account_id,
            "legacy_shell_id": self.legacy_shell_id,
            "legacy_grant_snapshot_sha256": self.legacy_grant_snapshot_sha256,
            "legacy_scope_sha256": self.legacy_scope_sha256,
            "legacy_voice_allowed": self.legacy_voice_allowed,
            "legacy_expires_at": self.legacy_expires_at,
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
        interaction_mode = cast(InteractionMode, str(session["interaction_mode"]))
        legacy = interaction_mode == "legacy"
        return cls(
            interaction_mode=interaction_mode,
            mode_policy_version=str(session["mode_policy_version"]),
            digital_self_version_id=_optional(session.get("digital_self_version_id")),
            manifest_sha256=_optional(session.get("digital_self_manifest_sha256")),
            preview_grant_id=_optional(session.get("preview_grant_id")),
            perspective=_optional(session.get("self_preview_perspective")),
            relationship_profile_id=_optional(session.get("relationship_profile_id")),
            relationship_profile_version=(
                _optional_int(session.get("relationship_profile_version")) if legacy else None
            ),
            legacy_grant_id=_optional(session.get("legacy_grant_id")),
            companion_style_id=_optional(session.get("companion_style_id")),
            companion_style_version=_optional(session.get("companion_style_version")),
            actor_account_id=_optional(session.get("user_id")) if legacy else None,
            resource_owner_account_id=(
                _optional(session.get("resource_owner_account_id")) if legacy else None
            ),
            legacy_actor_role=(
                _legacy_actor_role(session.get("legacy_actor_role")) if legacy else None
            ),
            legacy_grantee_account_id=(
                _optional(session.get("legacy_grantee_account_id")) if legacy else None
            ),
            legacy_shell_id=_optional(session.get("legacy_shell_id")) if legacy else None,
            legacy_grant_snapshot_sha256=(
                _optional(session.get("legacy_grant_snapshot_sha256")) if legacy else None
            ),
            legacy_scope_sha256=(
                _optional(session.get("legacy_scope_sha256")) if legacy else None
            ),
            legacy_voice_allowed=(
                _optional_bool(session.get("legacy_voice_allowed")) if legacy else None
            ),
            legacy_expires_at=(
                _optional(session.get("legacy_expires_at")) if legacy else None
            ),
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


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return None


def _legacy_actor_role(value: object) -> LegacyActorRole | None:
    return cast(LegacyActorRole, value) if value in {"owner_preview", "grantee"} else None


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
    def availability(mode: InteractionMode | FrozenMode) -> ModeAvailability:
        frozen = mode if isinstance(mode, FrozenMode) else None
        interaction_mode = frozen.interaction_mode if frozen is not None else mode
        if interaction_mode == "companion":
            return ModeAvailability("available", True)
        if interaction_mode == "archive":
            return ModeAvailability("available", False)
        if interaction_mode == "self_preview":
            return ModeAvailability("available", True)
        if frozen is not None and _legacy_contract_valid(frozen):
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

    @staticmethod
    def freeze_legacy(
        *,
        actor_account_id: str,
        resource_owner_account_id: str,
        actor_role: LegacyActorRole,
        grantee_account_id: str,
        grant_id: str,
        grant_snapshot_sha256: str,
        version_id: str,
        manifest_sha256: str,
        relationship_profile_id: str,
        relationship_profile_version: int,
        scope_sha256: str,
        shell_id: str | None,
        voice_allowed: bool,
        expires_at: str,
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
        frozen = FrozenMode(
            interaction_mode="legacy",
            mode_policy_version=LEGACY_POLICY_VERSION,
            digital_self_version_id=version_id,
            manifest_sha256=manifest_sha256,
            preview_grant_id=None,
            perspective=None,
            relationship_profile_id=relationship_profile_id,
            legacy_grant_id=grant_id,
            companion_style_id=None,
            companion_style_version=None,
            actor_account_id=actor_account_id,
            resource_owner_account_id=resource_owner_account_id,
            relationship_profile_version=relationship_profile_version,
            legacy_actor_role=actor_role,
            legacy_grantee_account_id=grantee_account_id,
            legacy_shell_id=shell_id,
            legacy_grant_snapshot_sha256=grant_snapshot_sha256,
            legacy_scope_sha256=scope_sha256,
            legacy_voice_allowed=voice_allowed,
            legacy_expires_at=expires_at,
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
        if not _legacy_contract_valid(frozen):
            raise ValueError("legacy frozen policy is invalid")
        return frozen

    @classmethod
    def effective_capabilities(
        cls,
        frozen: FrozenMode,
        *,
        speaker_class: SpeakerClass,
        reason_code: str | None = None,
        account_active: bool = True,
    ) -> EffectiveCapabilities:
        available = cls.availability(frozen).status == "available"
        owner_private = available and speaker_class == "owner"
        shadow_owner = (
            available and speaker_class == "uncertain" and reason_code == "shadow_owner_candidate"
        )
        companion = available and frozen.interaction_mode == "companion"
        self_preview = available and frozen.interaction_mode == "self_preview"
        legacy = available and frozen.interaction_mode == "legacy"
        legacy_actor = legacy and (
            (
                frozen.legacy_actor_role == "owner_preview"
                and frozen.actor_account_id == frozen.resource_owner_account_id
            )
            or (
                frozen.legacy_actor_role == "grantee"
                and frozen.actor_account_id == frozen.legacy_grantee_account_id
            )
        )
        legacy_conversation = legacy_actor and speaker_class == "owner"
        legacy_personal_voice = (
            legacy_conversation
            and frozen.legacy_voice_allowed is True
            and _personal_voice_contract_valid(frozen)
        )
        # `uncertain` may retain S1's explicitly curated low-sensitivity style
        # path, but is never a private-owner authority.
        return EffectiveCapabilities(
            conversation=account_active
            and (
                companion
                or (self_preview and speaker_class == "owner")
                or legacy_conversation
            ),
            private_memory=account_active and companion and owner_private,
            persona=account_active and companion and owner_private,
            persona_low_sensitivity=account_active and companion and shadow_owner,
            tools=account_active and companion and owner_private,
            history=account_active and companion and owner_private,
            learning=account_active and companion and owner_private,
            voice_profile=account_active
            and (
                companion
                or (self_preview and speaker_class == "owner")
                or legacy_personal_voice
            ),
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
        available = cls.availability(frozen).status == "available"
        companion = available and frozen.interaction_mode == "companion"
        self_preview = available and frozen.interaction_mode == "self_preview"
        legacy = available and frozen.interaction_mode == "legacy"
        legacy_personal_voice = (
            legacy
            and frozen.legacy_voice_allowed is True
            and _personal_voice_contract_valid(frozen)
        )
        return {
            **frozen.payload(),
            "simulated_output": frozen.interaction_mode != "companion",
            "history_eligible": companion,
            "owner_projection_eligible": companion,
            "policy_scope": "session",
            "capabilities": EffectiveCapabilities(
                conversation=companion or self_preview or legacy,
                private_memory=companion,
                persona=companion,
                persona_low_sensitivity=companion,
                tools=companion,
                history=companion,
                learning=companion,
                voice_profile=companion or self_preview or legacy_personal_voice,
            ).payload(),
        }


def _sha256(value: str | None) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _present(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 128


def _utc_timestamp(value: str | None) -> bool:
    if not _present(value):
        return False
    try:
        parsed = datetime.fromisoformat(cast(str, value))
    except ValueError:
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() is not None
        and parsed.utcoffset() == UTC.utcoffset(None)
    )


def _personal_voice_contract_valid(frozen: FrozenMode) -> bool:
    return bool(
        _present(frozen.voice_profile_id)
        and frozen.voice_profile_version is not None
        and frozen.voice_profile_version > 0
        and frozen.voice_provider == "volcengine_doubao"
        and frozen.voice_model == "seed-icl-2.0"
        and frozen.voice_resource_id == "seed-icl-2.0"
        and _utc_timestamp(frozen.voice_provider_expires_at)
        and _sha256(frozen.voice_speaker_sha256)
    )


def _voice_contract_valid(frozen: FrozenMode) -> bool:
    personal = (
        frozen.voice_profile_id,
        frozen.voice_profile_version,
        frozen.voice_provider,
        frozen.voice_model,
        frozen.voice_resource_id,
        frozen.voice_provider_expires_at,
        frozen.voice_speaker_sha256,
    )
    personal_absent = all(value is None for value in personal)
    personal_complete = _personal_voice_contract_valid(frozen)
    fallback_complete = (
        _present(frozen.fallback_voice_profile_id)
        and frozen.fallback_voice_provider == "volcengine_doubao"
        and frozen.fallback_voice_model == "seed-tts-2.0"
        and frozen.fallback_voice_resource_id == "seed-tts-2.0"
    )
    return bool(
        fallback_complete
        and (personal_absent or personal_complete)
        and (frozen.legacy_voice_allowed is True or personal_absent)
    )


def _legacy_contract_valid(frozen: FrozenMode) -> bool:
    if (
        frozen.interaction_mode != "legacy"
        or frozen.mode_policy_version != LEGACY_POLICY_VERSION
        or any(
            value is not None
            for value in (
                frozen.preview_grant_id,
                frozen.perspective,
                frozen.companion_style_id,
                frozen.companion_style_version,
            )
        )
        or not all(
            _present(value)
            for value in (
                frozen.actor_account_id,
                frozen.resource_owner_account_id,
                frozen.legacy_grantee_account_id,
                frozen.legacy_grant_id,
                frozen.digital_self_version_id,
                frozen.relationship_profile_id,
            )
        )
        or frozen.relationship_profile_version is None
        or frozen.relationship_profile_version < 1
        or not _sha256(frozen.manifest_sha256)
        or not _sha256(frozen.legacy_grant_snapshot_sha256)
        or not _sha256(frozen.legacy_scope_sha256)
        or not _utc_timestamp(frozen.legacy_expires_at)
        or frozen.legacy_voice_allowed is None
        or frozen.actor_account_id == frozen.legacy_grantee_account_id
        == frozen.resource_owner_account_id
        or not _voice_contract_valid(frozen)
    ):
        return False
    if frozen.legacy_actor_role == "owner_preview":
        return (
            frozen.actor_account_id == frozen.resource_owner_account_id
            and frozen.actor_account_id != frozen.legacy_grantee_account_id
            and frozen.legacy_shell_id is None
        )
    return bool(
        frozen.legacy_actor_role == "grantee"
        and frozen.actor_account_id == frozen.legacy_grantee_account_id
        and frozen.actor_account_id != frozen.resource_owner_account_id
        and _present(frozen.legacy_shell_id)
    )
