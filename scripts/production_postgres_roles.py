"""Single source of truth for production PostgreSQL runtime credentials."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class ProductionPostgresRole:
    """One login role and the service DSN that consumes it."""

    password_env: str
    role: str
    control_dsn_env: str | None


PRODUCTION_POSTGRES_ROLES = (
    ProductionPostgresRole(
        password_env="MEMORIA_DB_APP_PASSWORD",
        role="memoria_app",
        control_dsn_env="MEMORIA_ARCHIVE_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_COMPILER_PASSWORD",
        role="memoria_compiler",
        control_dsn_env="MEMORIA_ARCHIVE_COMPILER_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_EVOLUTION_PASSWORD",
        role="memoria_evolution",
        control_dsn_env="MEMORIA_EVOLUTION_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_GUARDIAN_PASSWORD",
        role="memoria_guardian",
        control_dsn_env="MEMORIA_GUARDIAN_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD",
        role="memoria_guardian_maintenance",
        control_dsn_env="MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_GUARDIAN_WORKER_PASSWORD",
        role="memoria_guardian_worker",
        control_dsn_env="MEMORIA_GUARDIAN_WORKER_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_IDENTITY_PASSWORD",
        role="memoria_identity",
        control_dsn_env="MEMORIA_IDENTITY_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD",
        role="memoria_identity_registration",
        control_dsn_env="MEMORIA_IDENTITY_REGISTRATION_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_CONSENT_PASSWORD",
        role="memoria_consent",
        control_dsn_env="MEMORIA_CONSENT_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_DEVICE_ONBOARDING_API_PASSWORD",
        role="memoria_device_onboarding_api",
        control_dsn_env="MEMORIA_DEVICE_ONBOARDING_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_DEVICE_ONBOARDING_MAINTENANCE_PASSWORD",
        role="memoria_device_onboarding_maintenance",
        control_dsn_env=None,
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_SESSION_API_PASSWORD",
        role="memoria_session_api",
        control_dsn_env="MEMORIA_SESSION_RUNTIME_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_ACTION_EXECUTOR_PASSWORD",
        role="memoria_action_executor",
        control_dsn_env="MEMORIA_ACTION_EXECUTOR_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_SESSION_PROJECTOR_PASSWORD",
        role="memoria_session_projector",
        control_dsn_env="MEMORIA_SESSION_RUNTIME_PROJECTOR_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_SESSION_WORKER_PASSWORD",
        role="memoria_session_worker",
        control_dsn_env="MEMORIA_SESSION_RUNTIME_WORKER_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD",
        role="memoria_session_maintenance",
        control_dsn_env="MEMORIA_SESSION_RUNTIME_MAINTENANCE_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_MEMORY_API_PASSWORD",
        role="memoria_memory_api",
        control_dsn_env="MEMORIA_MEMORY_API_DATABASE_URL",
    ),
    ProductionPostgresRole(
        password_env="MEMORIA_DB_MEMORY_WORKER_PASSWORD",
        role="memoria_memory_worker",
        control_dsn_env="MEMORIA_MEMORY_WORKER_DATABASE_URL",
    ),
)


def required_role_passwords(values: dict[str, str]) -> dict[str, str]:
    """Return role passwords after fail-closed bootstrap validation."""

    passwords: dict[str, str] = {}
    for role in PRODUCTION_POSTGRES_ROLES:
        password = values.get(role.password_env, "").strip()
        if not password:
            raise ValueError(f"missing required bootstrap value: {role.password_env}")
        passwords[role.password_env] = password
    return passwords


def production_control_database_urls(
    values: dict[str, str],
    *,
    host: str = "memoria-postgres",
    port: int = 5432,
    database: str = "memoria",
) -> dict[str, str]:
    """Build every long-lived Control API DSN from the canonical role list."""

    passwords = required_role_passwords(values)
    urls: dict[str, str] = {}
    for role in PRODUCTION_POSTGRES_ROLES:
        if role.control_dsn_env is None:
            continue
        password = quote(passwords[role.password_env], safe="")
        urls[role.control_dsn_env] = f"postgresql://{role.role}:{password}@{host}:{port}/{database}"
    return urls


__all__ = [
    "PRODUCTION_POSTGRES_ROLES",
    "ProductionPostgresRole",
    "production_control_database_urls",
    "required_role_passwords",
]
