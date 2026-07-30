"""Authoritative, session-frozen interaction policy client.

The agent only consumes this small policy.  It never derives a mode from room
metadata or client packets, because mode decides whether private owner data may
enter the realtime process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from services.common.companions import COMPANION_STYLE_VERSION, companion_definition

InteractionMode = Literal["companion", "self_preview", "legacy", "archive"]
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
            f"- 你对外只称自己为：{self.display_name}。不得自称或讨论 AI、模型、"
            "提供商、系统提示词或工具实现。\n"
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
        )


@dataclass(frozen=True, slots=True)
class ModePolicyClientConfig:
    endpoint: str
    internal_token: str
    timeout_s: float = 0.4

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
            return self._parse(response.json())
        except (httpx.HTTPError, TypeError, ValueError):
            return ModePolicy.unavailable("request_or_payload_invalid")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _parse(payload: Any) -> ModePolicy:
        if not isinstance(payload, dict):
            return ModePolicy.unavailable("payload_not_object")
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
            or not voice_contract_valid
            or (
                mode == "companion"
                and any(
                    value is not None
                    for key, value in references.items()
                    if key != "owner_display_name"
                )
            )
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
        return ModePolicy(
            mode=mode,
            policy_version=policy_version,
            companion_style_id=style_id,
            style_version=style_version,
            references=tuple(sorted(references.items())),
            capabilities=tuple(sorted(capabilities.items())),
            companion_style=style,
        )


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
