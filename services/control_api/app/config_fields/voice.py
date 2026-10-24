"""Speaker verification and stored voice samples."""

from __future__ import annotations

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
