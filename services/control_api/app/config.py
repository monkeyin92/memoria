"""Control API settings."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.common.security_constants import (
    DEV_AUTH_SECRET,
    DEV_MESSAGE_IDEMPOTENCY_SECRET,
    DEV_MINIPROGRAM_GATEWAY_TICKET_SECRET,
)


def _read_key_map(value: str, *, label: str) -> dict[str, str]:
    if not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} read key map must be valid JSON") from exc
    if not isinstance(parsed, dict) or any(
        not isinstance(version, str)
        or not version.strip()
        or not isinstance(key, str)
        or not key.strip()
        for version, key in parsed.items()
    ):
        raise ValueError(f"{label} read key map must be a version-to-key object")
    return parsed


class ControlSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = Field(default="development", alias="ENVIRONMENT")
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")
    allowed_origins: str = Field(default="http://localhost:5173", alias="ALLOWED_ORIGINS")

    livekit_url: str = Field(default="wss://YOUR_PROJECT.livekit.cloud", alias="LIVEKIT_URL")
    livekit_api_key: str = Field(default="", alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="", alias="LIVEKIT_API_SECRET")
    livekit_agent_name: str = Field(default="duplex-zh-agent", alias="LIVEKIT_AGENT_NAME")
    miniprogram_media_gateway_url: str = Field(
        default="",
        alias="MINIPROGRAM_MEDIA_GATEWAY_URL",
    )
    miniprogram_gateway_ticket_ttl_s: int = Field(
        default=90,
        ge=30,
        le=300,
        alias="MINIPROGRAM_GATEWAY_TICKET_TTL_S",
    )
    memoria_miniprogram_gateway_ticket_secret: SecretStr = Field(
        default=SecretStr(DEV_MINIPROGRAM_GATEWAY_TICKET_SECRET),
        alias="MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET",
    )

    memoria_db_path: str = Field(default="data/memoria.sqlite3", alias="MEMORIA_DB_PATH")
    archive_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_DATABASE_URL",
    )
    archive_compiler_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_COMPILER_DATABASE_URL",
    )
    archive_compiler_role: str = Field(default="", alias="MEMORIA_ARCHIVE_COMPILER_ROLE")
    memoria_timezone: str = Field(default="Asia/Shanghai", alias="MEMORIA_TIMEZONE")
    memoria_archive_internal_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_INTERNAL_TOKEN",
    )
    memoria_archive_write_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_WRITE_TOKEN",
    )
    memoria_agent_heartbeat_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_AGENT_HEARTBEAT_TOKEN",
    )
    memoria_memory_read_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_READ_TOKEN",
    )
    memoria_persona_read_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_PERSONA_READ_TOKEN",
    )
    memoria_voice_resolution_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_RESOLUTION_TOKEN",
    )
    memoria_voice_cleanup_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_CLEANUP_TOKEN",
    )
    memoria_interaction_policy_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_INTERACTION_POLICY_TOKEN",
    )
    memoria_response_plan_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_RESPONSE_PLAN_TOKEN",
    )
    archive_object_store_path: str = Field(
        default="data/archive-objects",
        alias="MEMORIA_ARCHIVE_OBJECT_STORE_PATH",
    )
    archive_object_encryption_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY",
    )
    archive_object_key_version: str = Field(
        default="archive-object-v1",
        alias="MEMORIA_ARCHIVE_OBJECT_KEY_VERSION",
    )
    archive_object_read_keys: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_OBJECT_READ_KEYS",
    )
    archive_object_bucket: str = Field(default="", alias="MEMORIA_ARCHIVE_OBJECT_BUCKET")
    archive_object_endpoint: str = Field(default="", alias="MEMORIA_ARCHIVE_OBJECT_ENDPOINT")
    archive_object_access_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY",
    )
    archive_object_secret_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_OBJECT_SECRET_KEY",
    )
    archive_object_region: str = Field(
        default="cn-beijing",
        alias="MEMORIA_ARCHIVE_OBJECT_REGION",
    )
    archive_object_prefix: str = Field(
        default="archive",
        alias="MEMORIA_ARCHIVE_OBJECT_PREFIX",
    )
    archive_compile_interval_s: float = Field(
        default=1.0,
        ge=0.1,
        le=300,
        alias="MEMORIA_ARCHIVE_COMPILE_INTERVAL_S",
    )
    archive_compile_batch_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        alias="MEMORIA_ARCHIVE_COMPILE_BATCH_SIZE",
    )
    memory_embedding_url: str = Field(default="", alias="MEMORIA_MEMORY_EMBEDDING_URL")
    memory_embedding_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_EMBEDDING_API_KEY",
    )
    memory_embedding_model: str = Field(default="", alias="MEMORIA_MEMORY_EMBEDDING_MODEL")
    memory_embedding_timeout_s: float = Field(
        default=5.0,
        ge=0.1,
        le=30.0,
        alias="MEMORIA_MEMORY_EMBEDDING_TIMEOUT_S",
    )
    speaker_database_path: str = Field(
        default="data/speakers.sqlite3",
        alias="MEMORIA_SPEAKER_DB_PATH",
    )
    speaker_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SPEAKER_DATABASE_URL",
    )
    speaker_template_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SPEAKER_TEMPLATE_KEY",
    )
    speaker_internal_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SPEAKER_INTERNAL_TOKEN",
    )
    speaker_embedding_url: str = Field(
        default="",
        alias="MEMORIA_SPEAKER_EMBEDDING_URL",
    )
    speaker_embedding_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SPEAKER_EMBEDDING_TOKEN",
    )
    speaker_embedding_model: str = Field(
        default="campplus-unconfigured",
        alias="MEMORIA_SPEAKER_EMBEDDING_MODEL",
    )
    speaker_embedding_timeout_s: float = Field(
        default=1.0,
        ge=0.1,
        le=5.0,
        alias="MEMORIA_SPEAKER_EMBEDDING_TIMEOUT_S",
    )
    speaker_owner_threshold: float = Field(
        default=0.78,
        ge=0.5,
        le=0.99,
        alias="MEMORIA_SPEAKER_OWNER_THRESHOLD",
    )
    speaker_guest_threshold: float = Field(
        default=0.40,
        ge=0.0,
        le=0.8,
        alias="MEMORIA_SPEAKER_GUEST_THRESHOLD",
    )
    voice_sample_store_path: str = Field(
        default="data/voice-samples",
        alias="MEMORIA_VOICE_SAMPLE_STORE_PATH",
    )
    voice_sample_encryption_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY",
    )
    voice_sample_key_version: str = Field(
        default="voice-sample-v1",
        alias="MEMORIA_VOICE_SAMPLE_KEY_VERSION",
    )
    voice_sample_read_keys: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_SAMPLE_READ_KEYS",
    )
    voice_object_bucket: str = Field(default="", alias="MEMORIA_VOICE_OBJECT_BUCKET")
    voice_object_endpoint: str = Field(default="", alias="MEMORIA_VOICE_OBJECT_ENDPOINT")
    voice_object_access_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_OBJECT_ACCESS_KEY",
    )
    voice_object_secret_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_OBJECT_SECRET_KEY",
    )
    voice_object_region: str = Field(default="cn-beijing", alias="MEMORIA_VOICE_OBJECT_REGION")
    voice_object_prefix: str = Field(default="voice-clone", alias="MEMORIA_VOICE_OBJECT_PREFIX")
    voice_sample_url_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_SAMPLE_URL_SECRET",
    )
    voice_sample_url_ttl_s: int = Field(
        default=300,
        ge=30,
        le=1800,
        alias="MEMORIA_VOICE_SAMPLE_URL_TTL_S",
    )
    voice_provider_region: str = Field(
        default="cn-beijing",
        alias="MEMORIA_VOICE_PROVIDER_REGION",
    )
    voice_clone_provider: Literal["alibaba_model_studio", "volcengine_doubao"] = Field(
        default="alibaba_model_studio",
        alias="MEMORIA_VOICE_CLONE_PROVIDER",
    )
    voice_enrollment_url: str = Field(
        default="https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
        alias="MEMORIA_VOICE_ENROLLMENT_URL",
    )
    voice_enrollment_timeout_s: float = Field(
        default=120.0,
        ge=5.0,
        le=180.0,
        alias="MEMORIA_VOICE_ENROLLMENT_TIMEOUT_S",
    )
    voice_target_model: str = Field(
        default="cosyvoice-v3.5-flash",
        alias="MEMORIA_VOICE_TARGET_MODEL",
    )
    doubao_voice_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_DOUBAO_VOICE_API_KEY",
    )
    doubao_voice_clone_url: str = Field(
        default="https://openspeech.bytedance.com/api/v3/tts/voice_clone",
        alias="MEMORIA_DOUBAO_VOICE_CLONE_URL",
    )
    doubao_voice_query_url: str = Field(
        default="https://openspeech.bytedance.com/api/v3/tts/get_voice",
        alias="MEMORIA_DOUBAO_VOICE_QUERY_URL",
    )
    doubao_voice_clone_poll_interval_s: float = Field(
        default=1.0,
        ge=0.05,
        le=30.0,
        alias="MEMORIA_DOUBAO_VOICE_CLONE_POLL_INTERVAL_S",
    )
    doubao_voice_synth_ready_id_mode: Literal[
        "unverified", "custom_speaker_id", "response_field"
    ] = Field(
        default="unverified",
        alias="MEMORIA_DOUBAO_VOICE_SYNTH_READY_ID_MODE",
    )
    doubao_voice_synth_ready_id_field: str = Field(
        default="",
        alias="MEMORIA_DOUBAO_VOICE_SYNTH_READY_ID_FIELD",
    )
    doubao_voice_expires_at_field: str = Field(
        default="",
        alias="MEMORIA_DOUBAO_VOICE_EXPIRES_AT_FIELD",
    )
    doubao_voice_expires_at_format: Literal["epoch_ms", "rfc3339"] = Field(
        default="epoch_ms",
        alias="MEMORIA_DOUBAO_VOICE_EXPIRES_AT_FORMAT",
    )

    llm_provider: Literal["qwen", "deepseek"] = Field(default="qwen", alias="LLM_PROVIDER")
    tts_provider: Literal["cosyvoice", "doubao"] = Field(
        default="doubao",
        alias="TTS_PROVIDER",
    )
    dashscope_api_key: SecretStr = Field(default=SecretStr(""), alias="DASHSCOPE_API_KEY")
    dashscope_ws_url: str = Field(default="", alias="DASHSCOPE_WS_URL")
    dashscope_workspace_id: str = Field(default="", alias="DASHSCOPE_WORKSPACE_ID")
    # P1-8: fixed Memoria persona voice on Omni (DashScope preset; not free-form clone).
    # Qwen3.5-Omni Realtime voice (not Qwen-TTS names like Cherry).
    # Liora Mira = 清欢，温柔女声（官方 Omni 音色表）.
    qwen_omni_voice: str = Field(default="Liora Mira", alias="QWEN_OMNI_VOICE")
    # Optional custom/cloned voice id from DashScope voice-clone product; wins over preset.
    qwen_omni_voice_clone_id: str = Field(default="", alias="QWEN_OMNI_VOICE_CLONE_ID")
    qwen_omni_persona_label: str = Field(
        default="Memoria 人设声",
        alias="QWEN_OMNI_PERSONA_LABEL",
    )
    qwen_omni_sdp_timeout_s: float = Field(
        default=10.0,
        ge=1.0,
        le=30.0,
        alias="QWEN_OMNI_SDP_TIMEOUT_S",
    )
    # Shared semantic_vad knobs for Omni Flash A/B sweeps.
    qwen_omni_vad_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        alias="QWEN_OMNI_VAD_THRESHOLD",
    )
    qwen_omni_prefix_padding_ms: int = Field(
        default=500,
        ge=0,
        le=2000,
        alias="QWEN_OMNI_PREFIX_PADDING_MS",
    )
    qwen_omni_silence_duration_ms: int = Field(
        default=800,
        ge=200,
        le=2500,
        alias="QWEN_OMNI_SILENCE_DURATION_MS",
    )
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="DASHSCOPE_BASE_URL",
    )
    dashscope_summary_model: str = Field(default="qwen-plus", alias="DASHSCOPE_SUMMARY_MODEL")
    dashscope_summary_timeout_s: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        alias="DASHSCOPE_SUMMARY_TIMEOUT_S",
    )
    memory_extraction_model: str = Field(
        default="qwen-plus",
        alias="MEMORIA_MEMORY_EXTRACTION_MODEL",
    )
    memory_extraction_timeout_s: float = Field(
        default=20.0,
        ge=1.0,
        le=120.0,
        alias="MEMORIA_MEMORY_EXTRACTION_TIMEOUT_S",
    )

    deepseek_api_key: SecretStr = Field(default=SecretStr(""), alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        alias="DEEPSEEK_BASE_URL",
    )
    deepseek_summary_model: str = Field(
        default="deepseek-chat",
        alias="DEEPSEEK_SUMMARY_MODEL",
    )
    deepseek_summary_timeout_s: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        alias="DEEPSEEK_SUMMARY_TIMEOUT_S",
    )

    session_token_ttl_s: int = Field(default=300, alias="SESSION_TOKEN_TTL_S")
    jwt_issuer: str = Field(default="voice-agent", alias="JWT_ISSUER")

    memoria_auth_secret: SecretStr = Field(
        default=SecretStr(DEV_AUTH_SECRET),
        alias="MEMORIA_AUTH_SECRET",
    )
    memoria_message_idempotency_secret: SecretStr = Field(
        default=SecretStr(DEV_MESSAGE_IDEMPOTENCY_SECRET),
        alias="MEMORIA_MESSAGE_IDEMPOTENCY_SECRET",
    )
    memoria_auth_issuer: str = Field(
        default="memoria-control-api",
        alias="MEMORIA_AUTH_ISSUER",
    )
    memoria_auth_audience: str = Field(
        default="memoria-h5",
        alias="MEMORIA_AUTH_AUDIENCE",
    )
    memoria_auth_token_ttl_s: int = Field(
        default=900,
        ge=600,
        le=900,
        alias="MEMORIA_AUTH_TOKEN_TTL_S",
    )
    memoria_auth_refresh_ttl_s: int = Field(
        default=2_592_000,
        ge=86_400,
        le=2_592_000,
        alias="MEMORIA_AUTH_REFRESH_TTL_S",
    )
    memoria_refresh_cookie_name: str = Field(
        default="memoria_refresh",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        alias="MEMORIA_REFRESH_COOKIE_NAME",
    )
    legacy_auth_compat_until: datetime | None = Field(
        default=None,
        alias="MEMORIA_LEGACY_AUTH_COMPAT_UNTIL",
    )
    memoria_release_tag: str = Field(default="development", alias="MEMORIA_RELEASE_TAG")
    readiness_gate_ttl_s: int = Field(
        default=86_400,
        ge=60,
        le=86_400,
        alias="READINESS_GATE_TTL_S",
    )
    offline_mock: bool = Field(default=False, alias="OFFLINE_MOCK")

    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @field_validator("legacy_auth_compat_until", mode="before")
    @classmethod
    def require_absolute_utc_legacy_auth_cutoff(cls, value: object) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("MEMORIA_LEGACY_AUTH_COMPAT_UNTIL must be absolute UTC") from exc
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("MEMORIA_LEGACY_AUTH_COMPAT_UNTIL must be absolute UTC")
        cutoff = value.astimezone(UTC)
        remaining = cutoff - datetime.now(UTC)
        if remaining > timedelta(hours=24):
            raise ValueError("MEMORIA_LEGACY_AUTH_COMPAT_UNTIL must be within 24 hours")
        return cutoff

    def legacy_auth_compat_active(self, *, now: datetime | None = None) -> bool:
        cutoff = self.legacy_auth_compat_until
        current = now or datetime.now(UTC)
        return cutoff is not None and current < cutoff

    def archive_object_read_key_map(self) -> dict[str, str]:
        return _read_key_map(
            self.archive_object_read_keys.get_secret_value(),
            label="archive object",
        )

    def voice_sample_read_key_map(self) -> dict[str, str]:
        return _read_key_map(
            self.voice_sample_read_keys.get_secret_value(),
            label="voice sample",
        )

    def internal_token(
        self,
        capability: Literal[
            "archive_write",
            "agent_heartbeat",
            "memory_read",
            "persona_read",
            "voice_resolution",
            "voice_cleanup",
            "interaction_policy",
            "response_plan",
        ],
    ) -> str:
        configured = {
            "archive_write": self.memoria_archive_write_token,
            "agent_heartbeat": self.memoria_agent_heartbeat_token,
            "memory_read": self.memoria_memory_read_token,
            "persona_read": self.memoria_persona_read_token,
            "voice_resolution": self.memoria_voice_resolution_token,
            "voice_cleanup": self.memoria_voice_cleanup_token,
            "interaction_policy": self.memoria_interaction_policy_token,
            "response_plan": self.memoria_response_plan_token,
        }[capability].get_secret_value()
        if configured or self.environment == "production":
            return configured
        return self.memoria_archive_internal_token.get_secret_value()

    def validate_production(self) -> None:
        if self.environment != "production":
            return
        if "*" in self.origins_list():
            raise ValueError("production must not allow * CORS")
        if self.public_base_url.startswith("http://"):
            raise ValueError("production must not use plaintext PUBLIC_BASE_URL")
        if not self.livekit_url.startswith("wss://"):
            raise ValueError("production must use secure LIVEKIT_URL")
        if not self.livekit_api_key or not self.livekit_api_secret:
            raise ValueError("production requires LiveKit credentials")
        auth_secret = self.memoria_auth_secret.get_secret_value()
        if auth_secret == DEV_AUTH_SECRET or len(auth_secret) < 32:
            raise ValueError("production requires an independent MEMORIA_AUTH_SECRET (>=32 chars)")
        if auth_secret == self.livekit_api_secret:
            raise ValueError("MEMORIA_AUTH_SECRET must differ from LIVEKIT_API_SECRET")
        gateway_url = self.miniprogram_media_gateway_url.strip()
        gateway_ticket_secret = self.memoria_miniprogram_gateway_ticket_secret.get_secret_value()
        if gateway_url:
            if not gateway_url.startswith("wss://"):
                raise ValueError("production Mini Program gateway must use secure WSS")
            if (
                gateway_ticket_secret == DEV_MINIPROGRAM_GATEWAY_TICKET_SECRET
                or len(gateway_ticket_secret) < 32
                or gateway_ticket_secret in {auth_secret, self.livekit_api_secret}
            ):
                raise ValueError(
                    "production requires an independent Mini Program gateway ticket secret"
                )
        capability_tokens = {
            "MEMORIA_ARCHIVE_WRITE_TOKEN": self.internal_token("archive_write"),
            "MEMORIA_AGENT_HEARTBEAT_TOKEN": self.internal_token("agent_heartbeat"),
            "MEMORIA_MEMORY_READ_TOKEN": self.internal_token("memory_read"),
            "MEMORIA_PERSONA_READ_TOKEN": self.internal_token("persona_read"),
            "MEMORIA_VOICE_RESOLUTION_TOKEN": self.internal_token("voice_resolution"),
            "MEMORIA_VOICE_CLEANUP_TOKEN": self.internal_token("voice_cleanup"),
            "MEMORIA_INTERACTION_POLICY_TOKEN": self.internal_token("interaction_policy"),
            "MEMORIA_RESPONSE_PLAN_TOKEN": self.internal_token("response_plan"),
        }
        if any(len(token) < 32 for token in capability_tokens.values()):
            raise ValueError("production requires eight capability-scoped internal tokens")
        if len(set(capability_tokens.values())) != len(capability_tokens) or any(
            token in {auth_secret, self.livekit_api_secret} for token in capability_tokens.values()
        ):
            raise ValueError("production internal capability tokens must be independent")
        if gateway_url and gateway_ticket_secret in capability_tokens.values():
            raise ValueError(
                "Mini Program gateway ticket secret must differ from internal capability tokens"
            )
        message_idempotency_secret = self.memoria_message_idempotency_secret.get_secret_value()
        if (
            message_idempotency_secret == DEV_MESSAGE_IDEMPOTENCY_SECRET
            or len(message_idempotency_secret) < 32
            or message_idempotency_secret
            in {
                auth_secret,
                self.livekit_api_secret,
                *capability_tokens.values(),
            }
        ):
            raise ValueError(
                "production requires an independent MEMORIA_MESSAGE_IDEMPOTENCY_SECRET (>=32 chars)"
            )
        archive_url = self.archive_database_url.get_secret_value()
        if not archive_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("production requires MEMORIA_ARCHIVE_DATABASE_URL for PostgreSQL")
        speaker_token = self.speaker_internal_token.get_secret_value()
        embedding_token = self.speaker_embedding_token.get_secret_value()
        template_key = self.speaker_template_key.get_secret_value()
        if len(speaker_token) < 32 or speaker_token in {
            auth_secret,
            self.livekit_api_secret,
            *capability_tokens.values(),
        }:
            raise ValueError("production requires an independent speaker internal token")
        if len(embedding_token) < 32 or embedding_token in {
            auth_secret,
            self.livekit_api_secret,
            *capability_tokens.values(),
            speaker_token,
        }:
            raise ValueError("production requires an independent speaker model token")
        try:
            from cryptography.fernet import Fernet

            Fernet(template_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("production requires a valid speaker template key") from exc
        if not self.speaker_embedding_url.startswith(("http://", "https://")):
            raise ValueError("production requires a speaker embedding service URL")
        if self.speaker_guest_threshold >= self.speaker_owner_threshold:
            raise ValueError("speaker thresholds must satisfy guest < owner")
        speaker_database_url = self.speaker_database_url.get_secret_value() or archive_url
        if not speaker_database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("production requires PostgreSQL for speaker profiles")
        voice_key = self.voice_sample_encryption_key.get_secret_value()
        voice_url_secret = self.voice_sample_url_secret.get_secret_value()
        if not voice_key or not self.voice_sample_key_version.strip():
            raise ValueError("production requires encrypted voice sample storage")
        try:
            from cryptography.fernet import Fernet

            Fernet(voice_key.encode("ascii"))
            voice_read_keys = self.voice_sample_read_key_map()
            if self.voice_sample_key_version in voice_read_keys:
                raise ValueError("active version cannot be read-only")
            for read_key in voice_read_keys.values():
                Fernet(read_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("production requires valid voice sample object keys") from exc
        if voice_key == template_key:
            raise ValueError(
                "production requires independent voice sample and speaker template keys"
            )
        if len(voice_url_secret) < 32 or voice_url_secret in {
            auth_secret,
            self.livekit_api_secret,
            *capability_tokens.values(),
            speaker_token,
            embedding_token,
            template_key,
            voice_key,
        }:
            raise ValueError("production requires an independent voice sample URL secret")
        if not self.voice_object_bucket.strip():
            raise ValueError("production requires an S3-compatible voice object bucket")
        voice_access_key = self.voice_object_access_key.get_secret_value().strip()
        voice_secret_key = self.voice_object_secret_key.get_secret_value().strip()
        if bool(voice_access_key) != bool(voice_secret_key) or (
            self.voice_object_endpoint.strip() and not voice_access_key
        ):
            raise ValueError("production requires a complete voice object credential pair")
        if self.voice_clone_provider == "alibaba_model_studio":
            if not self.voice_target_model.startswith("cosyvoice-v3.5-"):
                raise ValueError("CosyVoice voice cloning requires a CosyVoice v3.5 target")
        else:
            if self.voice_target_model != "seed-icl-2.0":
                raise ValueError("Doubao voice cloning requires seed-icl-2.0")
            if not self.doubao_voice_api_key.get_secret_value().strip():
                raise ValueError("Doubao voice cloning requires an independent API key")
            if self.doubao_voice_synth_ready_id_mode == "unverified":
                raise ValueError("Doubao voice cloning requires a smoke-verified synth ID mapping")
            if (
                self.doubao_voice_synth_ready_id_mode == "response_field"
                and not self.doubao_voice_synth_ready_id_field.strip()
            ):
                raise ValueError("Doubao response-field synth ID mapping requires a field path")
            if not self.doubao_voice_expires_at_field.strip():
                raise ValueError("Doubao voice cloning requires a provider expiry field path")
        archive_object_key = self.archive_object_encryption_key.get_secret_value()
        if not archive_object_key or not self.archive_object_key_version.strip():
            raise ValueError("production requires encrypted archive object storage")
        try:
            from cryptography.fernet import Fernet

            Fernet(archive_object_key.encode("ascii"))
            archive_read_keys = self.archive_object_read_key_map()
            if self.archive_object_key_version in archive_read_keys:
                raise ValueError("active version cannot be read-only")
            for read_key in archive_read_keys.values():
                Fernet(read_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("production requires valid archive object keys") from exc
        if archive_object_key in {voice_key, template_key}:
            raise ValueError("production requires an independent archive object key")
        key_material = [
            template_key,
            voice_key,
            *voice_read_keys.values(),
            archive_object_key,
            *archive_read_keys.values(),
        ]
        decoded_key_material = {
            base64.urlsafe_b64decode(value.encode("ascii")) for value in key_material
        }
        if len(decoded_key_material) != len(key_material):
            raise ValueError(
                "production encryption key material must be unique across active and read-only keyrings"
            )
        if not self.archive_object_bucket.strip():
            raise ValueError("production requires an S3-compatible archive object bucket")
        archive_access_key = self.archive_object_access_key.get_secret_value().strip()
        archive_secret_key = self.archive_object_secret_key.get_secret_value().strip()
        if bool(archive_access_key) != bool(archive_secret_key) or (
            self.archive_object_endpoint.strip() and not archive_access_key
        ):
            raise ValueError("production requires a complete archive object credential pair")
        if self.archive_object_bucket.strip() == self.voice_object_bucket.strip():
            raise ValueError("production requires independent archive and voice object buckets")
        release_tag = self.memoria_release_tag.strip().lower()
        if release_tag in ("", "latest", "development"):
            raise ValueError("production requires an immutable MEMORIA_RELEASE_TAG")
        compiler_url = self.archive_compiler_database_url.get_secret_value()
        if not compiler_url.startswith(("postgresql://", "postgres://")):
            raise ValueError(
                "production requires MEMORIA_ARCHIVE_COMPILER_DATABASE_URL for PostgreSQL"
            )
        compiler_role = self.archive_compiler_role.strip()
        compiler_user = urlsplit(compiler_url).username or ""
        archive_user = urlsplit(archive_url).username or ""
        if (
            compiler_url == archive_url
            or not compiler_role
            or compiler_role != compiler_user
            or compiler_role == archive_user
        ):
            raise ValueError("production requires an independent archive compiler database role")
        if (
            not self.memory_embedding_url.startswith(("http://", "https://"))
            or not self.memory_embedding_api_key.get_secret_value()
            or not self.memory_embedding_model.strip()
        ):
            raise ValueError(
                "production requires MEMORIA_MEMORY_EMBEDDING_URL, "
                "MEMORIA_MEMORY_EMBEDDING_API_KEY and MEMORIA_MEMORY_EMBEDDING_MODEL"
            )
