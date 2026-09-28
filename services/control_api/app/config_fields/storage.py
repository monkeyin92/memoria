"""Store backends: SQLite paths for development and PostgreSQL DSNs for production."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr


class StorageFields(BaseModel):
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
    evolution_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_EVOLUTION_DATABASE_URL",
    )
    guardian_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_GUARDIAN_DATABASE_URL",
    )
    # PR-13 governance roles: separate LOGIN/NOBYPASSRLS credentials for the
    # account export/delete path and the outbox worker.  Never reuse the API
    # guardian DSN; unset means those operations fail closed (503/error).
    guardian_maintenance_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL",
    )
    guardian_worker_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_GUARDIAN_WORKER_DATABASE_URL",
    )
    identity_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_IDENTITY_DATABASE_URL",
    )
    consent_database_url: SecretStr = Field(default=SecretStr(""), alias="MEMORIA_CONSENT_DATABASE_URL")
    control_database_url: SecretStr = Field(default=SecretStr(""), alias="MEMORIA_CONTROL_DATABASE_URL")
    eager_postgres: bool = Field(default=False, alias="MEMORIA_EAGER_POSTGRES")  # tests only
    device_onboarding_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_DEVICE_ONBOARDING_DATABASE_URL",
    )
    device_activation_signing_seed_b64: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_DEVICE_ACTIVATION_SIGNING_SEED_B64",
    )
    identity_registration_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_IDENTITY_REGISTRATION_DATABASE_URL",
    )
    session_runtime_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SESSION_RUNTIME_DATABASE_URL",
    )
    action_executor_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ACTION_EXECUTOR_DATABASE_URL",
    )
    memory_api_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_API_DATABASE_URL",
    )
    memory_worker_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_WORKER_DATABASE_URL",
    )
    memory_maintenance_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL",
    )
    memory_bootstrap_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_BOOTSTRAP_DATABASE_URL",
    )
    memory_schema_managed_externally: bool = Field(
        default=False,
        alias="MEMORIA_MEMORY_SCHEMA_MANAGED_EXTERNALLY",
    )
    session_runtime_bootstrap_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SESSION_RUNTIME_BOOTSTRAP_DATABASE_URL",
    )
    session_runtime_schema_managed_externally: bool = Field(
        default=False,
        alias="MEMORIA_SESSION_RUNTIME_SCHEMA_MANAGED_EXTERNALLY",
    )
    session_runtime_projector_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SESSION_RUNTIME_PROJECTOR_DATABASE_URL",
    )
    session_runtime_worker_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SESSION_RUNTIME_WORKER_DATABASE_URL",
    )
    session_runtime_maintenance_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SESSION_RUNTIME_MAINTENANCE_DATABASE_URL",
    )
    identity_db_path: str = Field(
        default="",
        alias="MEMORIA_IDENTITY_DB_PATH",
    )
    consent_db_path: str = Field(
        default="",
        alias="MEMORIA_CONSENT_DB_PATH",
    )
