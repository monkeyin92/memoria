"""Speaker verification, voice samples and voice cloning."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class VoiceFields(BaseModel):
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
