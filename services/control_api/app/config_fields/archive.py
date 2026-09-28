"""Archive object storage, memory compilation and embeddings."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr


class ArchiveFields(BaseModel):
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
    archive_compile_lease_s: float = Field(
        default=300.0,
        ge=5.0,
        le=3600.0,
        alias="MEMORIA_ARCHIVE_COMPILE_LEASE_S",
    )
    archive_compile_max_attempts: int = Field(
        default=8,
        ge=1,
        le=100,
        alias="MEMORIA_ARCHIVE_COMPILE_MAX_ATTEMPTS",
    )
    archive_compile_retry_base_s: float = Field(
        default=2.0,
        ge=0.1,
        le=300.0,
        alias="MEMORIA_ARCHIVE_COMPILE_RETRY_BASE_S",
    )
    archive_compile_retry_max_s: float = Field(
        default=300.0,
        ge=0.1,
        le=3600.0,
        alias="MEMORIA_ARCHIVE_COMPILE_RETRY_MAX_S",
    )
    corpus_retention_interval_s: float = Field(
        default=300.0,
        ge=10.0,
        le=3600.0,
        alias="MEMORIA_CORPUS_RETENTION_INTERVAL_S",
    )
    memory_embedding_url: str = Field(default="", alias="MEMORIA_MEMORY_EMBEDDING_URL")
    memory_embedding_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_EMBEDDING_API_KEY",
    )
    memory_embedding_model: str = Field(default="", alias="MEMORIA_MEMORY_EMBEDDING_MODEL")
    memory_embedding_dimensions: int = Field(
        default=1024,
        ge=1,
        le=2000,
        alias="MEMORIA_MEMORY_EMBEDDING_DIMENSIONS",
    )
    memory_embedding_timeout_s: float = Field(
        default=5.0,
        ge=0.1,
        le=30.0,
        alias="MEMORIA_MEMORY_EMBEDDING_TIMEOUT_S",
    )
