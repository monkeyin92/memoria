"""Authoritative, session-frozen interaction policy client.

The agent only consumes this small policy.  It never derives a mode from room
metadata or client packets, because mode decides whether private owner data may
enter the realtime process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

import httpx

from services.agent.src.runtime_profile import (
    VERIFY_KEY_ENV,
    VerifiedRuntimeProfile,
    parse_runtime_profile,
)
from services.common.companions import COMPANION_STYLE_VERSION, companion_definition
from services.tutor.domain import SESSION_FOCUSES, SessionFocus

InteractionMode = Literal[
    "companion",
    "self_preview",
    "legacy",
    "archive",
    "unknown_safe",
]
SpeakerClass = Literal["owner", "guest", "uncertain"]
PolicyReferenceValue = str | bool | None

_CONTROL_CAPABILITIES = frozenset(
    {
        "conversation",
        "private_memory",
        "persona",
        "persona_low_sensitivity",
        "tools",
        "history",
        "learning",
        "voice_profile",
    }
)

_INTERACTION_MODE_BY_SERVICE_MODE = {
    "student_minor": "companion",
    "adult_companion": "companion",
    "senior_companion": "companion",
    "family_shared": "companion",
    "adult_archive": "archive",
    "self_preview": "self_preview",
    "legacy_access": "legacy",
    "unknown_safe": "unknown_safe",
}

@dataclass(frozen=True, slots=True)
class CompanionStyle:
    display_name: str
    style_description: str
    warmth: str
    directness: str
    reply_length: str
    question_frequency: str
    interview_depth: str

    def prompt_fragment(self) -> str:
        return (
            "【冻结的陪伴身份与方式】\n"
            f"- 你对外只称自己为：{self.display_name}。不要无故、频繁或机械地讨论"
            "AI、语言模型、提供商或技术实现；用户直接询问你的本质时，如实说明你是"
            "由人工智能驱动的机器人伙伴。\n"
            f"- 角色说明：{self.style_description}。\n"
            f"- 温暖程度：{self.warmth}；直接程度：{self.directness}；回答长度：{self.reply_length}。\n"
            f"- 提问频率：{self.question_frequency}；访谈深度：{self.interview_depth}。\n"
            "这只约束陪伴节奏和表达方式，低于事实、安全和用户当前指令。"
            "不得把它当作账户主人的价值观、人格证据或建议立场。"
        )


@dataclass(frozen=True, slots=True)
class ModePolicy:
    """A policy parsed wholly from the internal Control API response."""

    mode: InteractionMode | None
    policy_version: str | None
    companion_style_id: str | None
    style_version: str | None
    references: tuple[tuple[str, PolicyReferenceValue], ...]
    capabilities: tuple[tuple[str, bool], ...]
    companion_style: CompanionStyle | None
    unavailable_reason: str | None = None
    session_focus: SessionFocus | None = "chat"
    runtime_profile: VerifiedRuntimeProfile | None = None
    runtime_profile_version: int = 0

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None and self.mode is not None

    @property
    def companion_style_prompt(self) -> str | None:
        if self.available and self.mode == "companion" and self.companion_style is not None:
            prompt = self.companion_style.prompt_fragment()
            definition = companion_definition(self.companion_style_id)
            if definition is not None:
                prompt += f"\n- 具体表达规则：{definition.conversation_instruction}"
            return prompt
        return None

    @property
    def owner_salutation(self) -> str | None:
        """The internal-only account-owner name; never use it for visitors."""
        value = dict(self.references).get("owner_display_name")
        if self.mode != "companion" or not _optional_bounded_string(value):
            return None
        return value if isinstance(value, str) else None

    def capability(self, name: str) -> bool:
        return self.available and dict(self.capabilities).get(name, False)

    def allows_private_context(self, speaker_class: SpeakerClass) -> bool:
        return speaker_class == "owner" and self.capability("private_memory")

    def allows_conversation(self, speaker_class: SpeakerClass | None = None) -> bool:
        if not self.capability("conversation"):
            return False
        return not (
            self.mode in {"self_preview", "legacy"}
            and speaker_class is not None
            and speaker_class != "owner"
        )

    def allows_anonymous_public_conversation(
        self,
        speaker_class: SpeakerClass | None = None,
    ) -> bool:
        """Allow only the identity-free, non-persistent unknown-safe surface."""

        sensitive = (
            "private_memory",
            "persona",
            "persona_low_sensitivity",
            "tools",
            "history",
            "learning",
            "voice_profile",
        )
        return (
            self.mode == "unknown_safe"
            and self.allows_conversation(speaker_class)
            and not any(self.capability(name) for name in sensitive)
        )

    def allows_private_persona(self, speaker_class: SpeakerClass) -> bool:
        return speaker_class == "owner" and self.capability("persona")

    def allows_tools(self, speaker_class: SpeakerClass) -> bool:
        return speaker_class == "owner" and self.capability("tools")

    def allows_voice_profile(self) -> bool:
        return self.capability("voice_profile")

    def allows_low_sensitivity_persona(self, *, is_shadow: bool) -> bool:
        return is_shadow and self.capability("persona_low_sensitivity")

    def history_eligible(
        self,
        speaker_class: SpeakerClass,
        *,
        reason_code: str | None = None,
    ) -> bool:
        del reason_code
        return speaker_class == "owner" and self.capability("history")

    def owner_projection_eligible(self, speaker_class: SpeakerClass) -> bool:
        return speaker_class == "owner" and self.capability("history")

    def allows_learning(
        self,
        speaker_class: SpeakerClass,
        *,
        reason_code: str | None = None,
    ) -> bool:
        del reason_code
        return speaker_class == "owner" and self.capability("learning")

    @classmethod
    def unavailable(cls, reason: str) -> ModePolicy:
        return cls(
            mode=None,
            policy_version=None,
            companion_style_id=None,
            style_version=None,
            references=(),
            capabilities=(),
            companion_style=None,
            unavailable_reason=reason,
            session_focus=None,
        )

    @classmethod
    def degraded_unknown_safe(
        cls,
    ) -> ModePolicy:
        """Local fail-closed surface after runtime-profile authority loss.

        Explicitly conversation-only (public chat + temporary English): no
        history / memory / learning / tools / private persona / personal
        voice.  The ``unknown_safe`` mode + ``degraded-unknown-safe-v1``
        version mark the surface; the signed RuntimeProfile stays the only
        authority for any sensitive capability and recovery requires a new
        profile at not less than the current epoch.
        """

        return cls(
            mode="unknown_safe",
            policy_version="degraded-unknown-safe-v1",
            companion_style_id=None,
            style_version=None,
            references=(),
            capabilities=(
                ("conversation", True),
                ("english_practice", True),
            ),
            companion_style=None,
            session_focus=None,
        )

    @classmethod
    def from_runtime_profile(
        cls,
        profile: VerifiedRuntimeProfile,
    ) -> ModePolicy:
        """Derive one safe, matching ModePolicy from a verified RuntimeProfile.

        After a subject switch the signed RuntimeProfile is the sole canonical
        authority: the service mode maps to the interaction mode and the
        canonical capabilities map to the conversation/history/learning
        surface.  Nothing is invented from the persona (style-only); the
        global ``tools`` surface stays False (tools are authorized per
        ``ToolSpec.required_capability`` at construction/dispatch, never by a
        coarse aggregate) and references are not synthesized — personal/legacy
        voice stays behind the profile's own ``voice_clone_use`` gate.
        """

        capabilities = set(profile.profile.capabilities)
        obligations = set(profile.profile.obligations)
        return cls(
            mode=cast(
                InteractionMode | None,
                _INTERACTION_MODE_BY_SERVICE_MODE.get(profile.profile.service_mode),
            ),
            policy_version=(
                profile.profile.policy_bundle_version or "derived"
            ),
            companion_style_id=None,
            style_version=None,
            references=(),
            capabilities=(
                ("conversation", "chat" in capabilities),
                (
                    "english_practice",
                    "english_practice" in capabilities,
                ),
                (
                    "tools",
                    False,
                ),
                (
                    "history",
                    "memory_recall_private" in capabilities,
                ),
                (
                    "private_memory",
                    "memory_recall_private" in capabilities,
                ),
                (
                    "learning",
                    ("tutor" in capabilities or "english_practice" in capabilities)
                    and "DO_NOT_WRITE_LEARNING_PROGRESS" not in obligations,
                ),
            ),
            companion_style=None,
            session_focus=None,
            runtime_profile=profile,
        )

    @classmethod
    def companion_for_test(
        cls,
        *,
        policy_version: str,
        private_context: bool,
        owner_evidence: bool,
        tools: bool,
        voice_profile: bool,
        shadow_low_sensitivity_persona: bool,
        session_focus: SessionFocus = "chat",
    ) -> ModePolicy:
        return cls(
            mode="companion",
            policy_version=policy_version,
            companion_style_id="starlight",
            style_version=COMPANION_STYLE_VERSION,
            references=(),
            capabilities=(
                ("conversation", True),
                ("private_memory", private_context),
                ("persona", private_context),
                ("persona_low_sensitivity", shadow_low_sensitivity_persona),
                ("tools", tools),
                ("history", owner_evidence),
                ("learning", owner_evidence),
                ("voice_profile", voice_profile),
            ),
            companion_style=_style_for("starlight", COMPANION_STYLE_VERSION),
            session_focus=session_focus,
        )


def mode_policy_after_identity_rotation(
    previous: ModePolicy,
    profile: VerifiedRuntimeProfile | None,
) -> ModePolicy:
    """Re-derive capabilities, but keep a valid companion style for TTS binding."""

    if profile is None:
        return ModePolicy.degraded_unknown_safe()
    derived = ModePolicy.from_runtime_profile(profile)
    if derived.mode != "companion":
        return derived
    style_id = previous.companion_style_id if previous.mode == "companion" else None
    style_version = previous.style_version if previous.mode == "companion" else None
    if style_id is None:
        style_id = profile.profile.persona_id
        style_version = COMPANION_STYLE_VERSION
    if not isinstance(style_id, str) or not isinstance(style_version, str):
        return derived
    style = _style_for(style_id, style_version)
    if style is None:
        return derived
    references = dict(derived.references)
    owner_name = dict(previous.references).get("owner_display_name")
    if previous.mode == "companion" and isinstance(owner_name, str) and owner_name:
        references["owner_display_name"] = owner_name
    return replace(
        derived,
        companion_style_id=style_id,
        style_version=style_version,
        companion_style=style,
        references=tuple(sorted(references.items())),
    )


@dataclass(frozen=True, slots=True)
class ModePolicyClientConfig:
    endpoint: str
    internal_token: str
    timeout_s: float = 0.4
    runtime_profile_verify_key: str | None = field(
        default_factory=lambda: os.getenv(VERIFY_KEY_ENV) or None
    )

    def __post_init__(self) -> None:
        url = httpx.URL(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("interaction policy endpoint must be HTTP(S)")
        if not self.internal_token.strip():
            raise ValueError("interaction policy internal token must not be blank")
        if self.timeout_s <= 0:
            raise ValueError("interaction policy timeout must be positive")


class ModePolicyClient:
    """Fetch once at session start; failures intentionally return no authority."""

    def __init__(
        self,
        config: ModePolicyClientConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_s)
        self._owns_client = client is None

    async def fetch(self, *, session_id: str) -> ModePolicy:
        if not session_id.strip():
            return ModePolicy.unavailable("missing_session_id")
        try:
            response = await self._client.post(
                self._config.endpoint,
                headers={"X-Memoria-Internal-Token": self._config.internal_token},
                json={"session_id": session_id},
                timeout=self._config.timeout_s,
            )
            if response.status_code != 200:
                return ModePolicy.unavailable(f"http_{response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict) or "runtime_profile" not in payload:
                return ModePolicy.unavailable("runtime_profile_missing")
            policy = self._parse(payload, self._config.runtime_profile_verify_key)
            if policy.runtime_profile is None:
                return ModePolicy.unavailable("runtime_profile_invalid")
            if policy.runtime_profile.profile.session_id != session_id:
                return ModePolicy.unavailable("runtime_profile_session_mismatch")
            return policy
        except (httpx.HTTPError, TypeError, ValueError):
            return ModePolicy.unavailable("request_or_payload_invalid")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _parse(payload: Any, runtime_profile_verify_key: str | None = None) -> ModePolicy:
        if not isinstance(payload, dict):
            return ModePolicy.unavailable("payload_not_object")
        if "runtime_profile" in payload:
            return ModePolicyClient._parse_signed_profile_envelope(
                payload,
                runtime_profile_verify_key,
            )
        required_voice_fields = {
            "voice_speaker_sha256",
            "fallback_voice_profile_id",
            "fallback_voice_provider",
            "fallback_voice_model",
            "fallback_voice_resource_id",
        }
        required_legacy_fields = {
            "actor_account_id",
            "resource_owner_account_id",
            "relationship_profile_version",
            "legacy_actor_role",
            "legacy_grantee_account_id",
            "legacy_shell_id",
            "legacy_grant_snapshot_sha256",
            "legacy_scope_sha256",
            "legacy_voice_allowed",
            "legacy_expires_at",
        }
        if not (required_voice_fields | required_legacy_fields).issubset(payload):
            return ModePolicy.unavailable("payload_invalid")
        mode = payload.get("interaction_mode")
        session_focus = payload.get("session_focus")
        policy_version = payload.get("mode_policy_version")
        style_id = payload.get("companion_style_id")
        style_version = payload.get("companion_style_version")
        policy_scope = payload.get("policy_scope")
        raw_voice_profile_version = payload.get("voice_profile_version")
        voice_profile_version_invalid = False
        if raw_voice_profile_version is None:
            voice_profile_version: str | None = None
        elif (
            isinstance(raw_voice_profile_version, int)
            and not isinstance(raw_voice_profile_version, bool)
            and raw_voice_profile_version >= 1
        ):
            voice_profile_version = str(raw_voice_profile_version)
        else:
            voice_profile_version = None
            voice_profile_version_invalid = True
        raw_relationship_profile_version = payload.get("relationship_profile_version")
        relationship_profile_version_invalid = False
        if raw_relationship_profile_version is None:
            relationship_profile_version: str | None = None
        elif (
            isinstance(raw_relationship_profile_version, int)
            and not isinstance(raw_relationship_profile_version, bool)
            and raw_relationship_profile_version >= 1
        ):
            relationship_profile_version = str(raw_relationship_profile_version)
        else:
            relationship_profile_version = None
            relationship_profile_version_invalid = True
        references = {
            key: payload.get(key)
            for key in (
                "actor_account_id",
                "resource_owner_account_id",
                "digital_self_version_id",
                "manifest_sha256",
                "preview_grant_id",
                "perspective",
                "relationship_profile_id",
                "legacy_actor_role",
                "legacy_grantee_account_id",
                "legacy_grant_id",
                "legacy_shell_id",
                "legacy_grant_snapshot_sha256",
                "legacy_scope_sha256",
                "legacy_voice_allowed",
                "legacy_expires_at",
                "voice_profile_id",
                "voice_provider",
                "voice_model",
                "voice_resource_id",
                "voice_provider_expires_at",
                "voice_speaker_sha256",
                "fallback_voice_profile_id",
                "fallback_voice_provider",
                "fallback_voice_model",
                "fallback_voice_resource_id",
                "owner_display_name",
            )
        }
        references["voice_profile_version"] = voice_profile_version
        references["relationship_profile_version"] = relationship_profile_version
        capabilities = payload.get("capabilities")
        voice_values = tuple(
            references[key]
            for key in (
                "voice_profile_id",
                "voice_profile_version",
                "voice_provider",
                "voice_model",
                "voice_resource_id",
                "voice_provider_expires_at",
                "voice_speaker_sha256",
            )
        )
        fallback_voice_values = tuple(
            references[key]
            for key in (
                "fallback_voice_profile_id",
                "fallback_voice_provider",
                "fallback_voice_model",
                "fallback_voice_resource_id",
            )
        )
        voice_complete = all(value is not None for value in voice_values)
        voice_absent = all(value is None for value in voice_values)
        voice_contract_valid = voice_absent or (
            voice_complete
            and references["voice_provider"] == "volcengine_doubao"
            and references["voice_model"] == "seed-icl-2.0"
            and references["voice_resource_id"] == "seed-icl-2.0"
            and _valid_utc_timestamp(references["voice_provider_expires_at"])
            and _valid_sha256(references["voice_speaker_sha256"])
        )
        fallback_voice_complete = all(value is not None for value in fallback_voice_values)
        fallback_voice_contract_valid = (
            fallback_voice_complete
            and _bounded_string(references["fallback_voice_profile_id"])
            and references["fallback_voice_provider"] == "volcengine_doubao"
            and references["fallback_voice_model"] == "seed-tts-2.0"
            and references["fallback_voice_resource_id"] == "seed-tts-2.0"
        )
        text_references = tuple(
            value
            for key, value in references.items()
            if key != "legacy_voice_allowed"
        )
        legacy_capabilities = {
            "conversation": True,
            "private_memory": False,
            "persona": False,
            "persona_low_sensitivity": False,
            "tools": False,
            "history": False,
            "learning": False,
            "voice_profile": references["legacy_voice_allowed"] is True and voice_complete,
        }
        legacy_required = (
            "actor_account_id",
            "resource_owner_account_id",
            "digital_self_version_id",
            "manifest_sha256",
            "relationship_profile_id",
            "relationship_profile_version",
            "legacy_actor_role",
            "legacy_grantee_account_id",
            "legacy_grant_id",
            "legacy_grant_snapshot_sha256",
            "legacy_scope_sha256",
            "legacy_expires_at",
        )
        legacy_common_valid = (
            all(_bounded_string(references[key]) for key in legacy_required)
            and _valid_sha256(references["manifest_sha256"])
            and _valid_sha256(references["legacy_grant_snapshot_sha256"])
            and _valid_sha256(references["legacy_scope_sha256"])
            and _valid_utc_timestamp(references["legacy_expires_at"])
            and isinstance(references["legacy_voice_allowed"], bool)
            and references["resource_owner_account_id"]
            != references["legacy_grantee_account_id"]
            and fallback_voice_contract_valid
            and (references["legacy_voice_allowed"] is True or voice_absent)
        )
        legacy_role_valid = (
            references["legacy_actor_role"] == "owner_preview"
            and references["actor_account_id"] == references["resource_owner_account_id"]
            and references["legacy_shell_id"] is None
        ) or (
            references["legacy_actor_role"] == "grantee"
            and references["actor_account_id"] == references["legacy_grantee_account_id"]
            and _bounded_string(references["legacy_shell_id"])
        )
        legacy_contract_valid = (
            legacy_common_valid
            and legacy_role_valid
            and references["preview_grant_id"] is None
            and references["perspective"] is None
            and (voice_absent or voice_complete)
            and capabilities == legacy_capabilities
        )
        if (
            mode not in {"companion", "self_preview", "legacy", "archive"}
            or session_focus not in SESSION_FOCUSES
            or (mode != "companion" and session_focus != "chat")
            or policy_scope != "session"
            or not _bounded_string(policy_version)
            or not _optional_bounded_string(style_id)
            or not _optional_bounded_string(style_version)
            or not isinstance(references, dict)
            or set(capabilities or ()) != _CONTROL_CAPABILITIES
            or not isinstance(capabilities, dict)
            or not all(isinstance(value, bool) for value in capabilities.values())
            or not all(_optional_bounded_string(value) for value in text_references)
            or (
                references["legacy_voice_allowed"] is not None
                and not isinstance(references["legacy_voice_allowed"], bool)
            )
            or voice_profile_version_invalid
            or relationship_profile_version_invalid
            or (mode != "companion" and not voice_contract_valid)
            or (mode == "companion" and not _companion_voice_contract_valid(references))
            or (
                mode == "self_preview"
                and (
                    not _bounded_string(references["digital_self_version_id"])
                    or not _bounded_string(references["manifest_sha256"])
                    or not _bounded_string(references["preview_grant_id"])
                    or references["perspective"] not in {"owner", "child", "friend"}
                    or references["relationship_profile_id"] is not None
                    or references["relationship_profile_version"] is not None
                    or references["legacy_grant_id"] is not None
                    or not fallback_voice_contract_valid
                )
            )
            or (mode == "legacy" and not legacy_contract_valid)
            or (
                mode != "legacy"
                and any(
                    references[key] is not None
                    for key in (
                        "actor_account_id",
                        "resource_owner_account_id",
                        "relationship_profile_version",
                        "legacy_actor_role",
                        "legacy_grantee_account_id",
                        "legacy_grant_snapshot_sha256",
                        "legacy_scope_sha256",
                        "legacy_shell_id",
                        "legacy_voice_allowed",
                        "legacy_expires_at",
                    )
                )
            )
            or (mode != "companion" and references["owner_display_name"] is not None)
            or (mode != "companion" and (style_id is not None or style_version is not None))
        ):
            return ModePolicy.unavailable("payload_invalid")
        style = None
        if mode == "companion":
            style = _style_for(style_id, style_version)
            if style is None:
                return ModePolicy.unavailable("companion_style_invalid")
        runtime_profile = parse_runtime_profile(
            payload.get("runtime_profile"),
            verify_key=runtime_profile_verify_key,
        )
        return ModePolicy(
            mode=mode,
            policy_version=policy_version,
            companion_style_id=style_id,
            style_version=style_version,
            references=tuple(sorted(references.items())),
            capabilities=tuple(sorted(capabilities.items())),
            companion_style=style,
            session_focus=cast(SessionFocus, session_focus),
            runtime_profile=runtime_profile,
        )

    @staticmethod
    def _parse_signed_profile_envelope(
        payload: dict[str, Any],
        runtime_profile_verify_key: str | None,
    ) -> ModePolicy:
        """Parse the signed profile first, then retain only display metadata.

        The legacy fields remain on the HTTP envelope for Tutor/Persona
        compatibility, but the signed RuntimeProfile is the only source for
        mode capabilities and subject-sensitive authority.
        """

        runtime_profile = parse_runtime_profile(
            payload.get("runtime_profile"),
            verify_key=runtime_profile_verify_key,
        )
        if runtime_profile is None:
            return ModePolicy.unavailable("runtime_profile_invalid")
        runtime_profile_version = payload.get("runtime_profile_version", 0)
        if (
            isinstance(runtime_profile_version, bool)
            or not isinstance(runtime_profile_version, int)
            or runtime_profile_version < 0
        ):
            return ModePolicy.unavailable("runtime_profile_version_invalid")

        derived = ModePolicy.from_runtime_profile(runtime_profile)
        expected_mode = derived.mode
        supplied_mode = payload.get("interaction_mode")
        if supplied_mode is not None and supplied_mode != expected_mode:
            return ModePolicy.unavailable("payload_invalid")

        session_focus = payload.get("session_focus", "chat")
        if (
            session_focus not in SESSION_FOCUSES
            or (expected_mode != "companion" and session_focus != "chat")
        ):
            return ModePolicy.unavailable("payload_invalid")
        policy_scope = payload.get("policy_scope", "session")
        if policy_scope != "session":
            return ModePolicy.unavailable("payload_invalid")

        style_id = payload.get("companion_style_id")
        style_version = payload.get("companion_style_version")
        if expected_mode == "companion":
            if style_id is None:
                style_id = runtime_profile.profile.persona_id
            if style_version is None:
                style_version = COMPANION_STYLE_VERSION
            style = _style_for(style_id, style_version)
            if style is None:
                return ModePolicy.unavailable("companion_style_invalid")
        else:
            if style_id is not None or style_version is not None:
                return ModePolicy.unavailable("payload_invalid")
            style = None

        owner_display_name = payload.get("owner_display_name")
        if not _optional_bounded_string(owner_display_name):
            return ModePolicy.unavailable("payload_invalid")

        raw_voice_profile_version = payload.get("voice_profile_version")
        if raw_voice_profile_version is None:
            voice_profile_version: str | None = None
        elif (
            isinstance(raw_voice_profile_version, int)
            and not isinstance(raw_voice_profile_version, bool)
            and raw_voice_profile_version >= 1
        ):
            voice_profile_version = str(raw_voice_profile_version)
        else:
            return ModePolicy.unavailable("payload_invalid")
        voice_references = {
            "voice_profile_id": payload.get("voice_profile_id"),
            "voice_profile_version": voice_profile_version,
            "voice_provider": payload.get("voice_provider"),
            "voice_model": payload.get("voice_model"),
            "voice_resource_id": payload.get("voice_resource_id"),
            "voice_provider_expires_at": payload.get("voice_provider_expires_at"),
            "voice_speaker_sha256": payload.get("voice_speaker_sha256"),
            "fallback_voice_profile_id": payload.get("fallback_voice_profile_id"),
            "fallback_voice_provider": payload.get("fallback_voice_provider"),
            "fallback_voice_model": payload.get("fallback_voice_model"),
            "fallback_voice_resource_id": payload.get("fallback_voice_resource_id"),
        }
        if expected_mode == "companion" and not _companion_voice_contract_valid(voice_references):
            return ModePolicy.unavailable("payload_invalid")
        if expected_mode != "companion" and any(
            value is not None for value in voice_references.values()
        ):
            # Signed companion envelopes may carry a frozen clone; other modes
            # keep voice contracts on the non-envelope parser path.
            voice_references = {key: None for key in voice_references}

        references: tuple[tuple[str, PolicyReferenceValue], ...] = tuple(
            sorted(
                (
                    *((("owner_display_name", owner_display_name),) if owner_display_name else ()),
                    *(
                        (key, value)
                        for key, value in voice_references.items()
                        if value is not None
                    ),
                )
            )
        )
        return replace(
            derived,
            companion_style_id=cast(str | None, style_id),
            style_version=cast(str | None, style_version),
            companion_style=style,
            references=references,
            session_focus=cast(SessionFocus, session_focus),
            runtime_profile_version=runtime_profile_version,
        )


def _companion_voice_contract_valid(references: dict[str, Any]) -> bool:
    """Companion may omit voice refs or freeze one complete personal clone."""

    personal_keys = (
        "voice_profile_id",
        "voice_profile_version",
        "voice_provider",
        "voice_model",
        "voice_resource_id",
        "voice_provider_expires_at",
        "voice_speaker_sha256",
    )
    fallback_keys = (
        "fallback_voice_profile_id",
        "fallback_voice_provider",
        "fallback_voice_model",
        "fallback_voice_resource_id",
    )
    personal_values = tuple(references.get(key) for key in personal_keys)
    fallback_values = tuple(references.get(key) for key in fallback_keys)
    if all(value is None for value in (*personal_values, *fallback_values)):
        return True
    fallback_ok = (
        all(value is not None for value in fallback_values)
        and _bounded_string(references.get("fallback_voice_profile_id"))
        and references.get("fallback_voice_provider") == "volcengine_doubao"
        and references.get("fallback_voice_model") == "seed-tts-2.0"
        and references.get("fallback_voice_resource_id") == "seed-tts-2.0"
    )
    speaker = references.get("voice_speaker_sha256")
    version = references.get("voice_profile_version")
    version_ok = (isinstance(version, str) and version.isdigit() and int(version) >= 1) or (
        isinstance(version, int) and not isinstance(version, bool) and version >= 1
    )
    if not (
        fallback_ok
        and _bounded_string(references.get("voice_profile_id"))
        and version_ok
        and isinstance(speaker, str)
        and _valid_sha256(speaker)
    ):
        return False
    provider = references.get("voice_provider")
    model = references.get("voice_model")
    resource = references.get("voice_resource_id")
    if provider == "volcengine_doubao" and model == "seed-icl-2.0" and resource == "seed-icl-2.0":
        return _valid_utc_timestamp(references.get("voice_provider_expires_at"))
    if (
        provider == "alibaba_model_studio"
        and isinstance(model, str)
        and model.startswith("cosyvoice-v3.5-")
        and resource == model
    ):
        expires = references.get("voice_provider_expires_at")
        return expires is None or _valid_utc_timestamp(expires)
    return False


def _bounded_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 128


def _optional_bounded_string(value: Any) -> bool:
    return value is None or _bounded_string(value)


def _valid_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() is not None
        and parsed.utcoffset() == UTC.utcoffset(None)
    )


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _style_for(style_id: object, style_version: object) -> CompanionStyle | None:
    if not _bounded_string(style_id) or style_version != COMPANION_STYLE_VERSION:
        return None
    definition = companion_definition(style_id)
    if definition is None:
        return None
    return CompanionStyle(
        display_name=definition.display_name,
        style_description=definition.style_description,
        warmth=definition.warmth,
        directness=definition.directness,
        reply_length=definition.response_length,
        question_frequency=definition.question_frequency,
        interview_depth=definition.interview_depth,
    )
