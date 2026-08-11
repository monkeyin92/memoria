"""PR-13 production configuration: dedicated guardian governance roles."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from services.control_api.app.config import ControlSettings
from services.guardian.postgres_store import PostgresGuardianStore


@pytest.mark.asyncio
async def test_governance_dsn_settings_and_fail_closed_without_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL",
        "postgresql://memoria_guardian_maintenance:pw@localhost:55439/postgres",
    )
    monkeypatch.setenv(
        "MEMORIA_GUARDIAN_WORKER_DATABASE_URL",
        "postgresql://memoria_guardian_worker:pw@localhost:55439/postgres",
    )
    settings = ControlSettings()
    assert (
        settings.guardian_maintenance_database_url.get_secret_value()
        == "postgresql://memoria_guardian_maintenance:pw@localhost:55439/postgres"
    )
    assert (
        settings.guardian_worker_database_url.get_secret_value()
        == "postgresql://memoria_guardian_worker:pw@localhost:55439/postgres"
    )
    assert settings.guardian_database_url.get_secret_value() == ""

    # Without the dedicated role DSNs the governance/worker operations fail
    # closed instead of falling back to the API connection.
    store = PostgresGuardianStore("postgresql://memoria_guardian:pw@localhost:55439/postgres")
    with pytest.raises(RuntimeError, match="account export requires"):
        await store._ready_role_pool(
            dsn=None,
            pool_attr="_maintenance_pool",
            expected_role="memoria_guardian_maintenance",
            operation="account export",
        )
    with pytest.raises(RuntimeError, match="outbox worker requires"):
        await store._ready_role_pool(
            dsn=None,
            pool_attr="_worker_pool",
            expected_role="memoria_guardian_worker",
            operation="outbox worker",
        )


def _production_settings(**overrides: str) -> ControlSettings:
    values: dict[str, str] = {
        "ENVIRONMENT": "production",
        "PUBLIC_BASE_URL": "https://voice.example.com",
        "ALLOWED_ORIGINS": "https://voice.example.com",
        "LIVEKIT_URL": "wss://livekit.example.com",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "test-livekit-material-long-enough",
        "MEMORIA_AUTH_SECRET": "test-auth-material-that-is-long-enough",
        "MEMORIA_MESSAGE_IDEMPOTENCY_SECRET": (
            "test-message-idempotency-material-that-is-long-enough"
        ),
        "MEMORIA_ARCHIVE_WRITE_TOKEN": "test-archive-write-material-long-enough",
        "MEMORIA_AGENT_HEARTBEAT_TOKEN": "test-heartbeat-material-that-is-long-enough",
        "MEMORIA_MEMORY_READ_TOKEN": "test-memory-read-material-long-enough",
        "MEMORIA_PERSONA_READ_TOKEN": "test-persona-read-material-long-enough",
        "MEMORIA_VOICE_RESOLUTION_TOKEN": "test-voice-resolve-material-long-enough",
        "MEMORIA_VOICE_CLEANUP_TOKEN": "test-voice-cleanup-material-long-enough",
        "MEMORIA_INTERACTION_POLICY_TOKEN": "test-interaction-policy-material-long-enough",
        "MEMORIA_RESPONSE_PLAN_TOKEN": "test-response-plan-material-long-enough",
        "MEMORIA_EVOLUTION_CONTROL_TOKEN": "test-evolution-control-material-long-enough",
        "MEMORIA_EVOLUTION_VALIDATOR_TOKEN": "test-evolution-validator-material-long-enough",
        "MEMORIA_ARCHIVE_DATABASE_URL": "postgresql://archive:test@db/memoria",
        "MEMORIA_EVOLUTION_DATABASE_URL": "postgresql://memoria_evolution:test@db/memoria",
        "MEMORIA_GUARDIAN_DATABASE_URL": "postgresql://memoria_guardian:test@db/memoria",
        "MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL": (
            "postgresql://memoria_guardian_maintenance:test@db/memoria"
        ),
        "MEMORIA_GUARDIAN_WORKER_DATABASE_URL": (
            "postgresql://memoria_guardian_worker:test@db/memoria"
        ),
        "MEMORIA_IDENTITY_DATABASE_URL": "postgresql://memoria_identity:test@db/memoria",
        "MEMORIA_CONSENT_DATABASE_URL": "postgresql://memoria_consent:test@db/memoria",
        "MEMORIA_IDENTITY_REGISTRATION_DATABASE_URL": (
            "postgresql://memoria_identity_registration:test@db/memoria"
        ),
        "MEMORIA_SESSION_RUNTIME_DATABASE_URL": (
            "postgresql://memoria_session_api:test@db/memoria"
        ),
        "MEMORIA_ACTION_EXECUTOR_DATABASE_URL": (
            "postgresql://memoria_action_executor:test@db/memoria"
        ),
        "MEMORIA_MEMORY_API_DATABASE_URL": ("postgresql://memoria_memory_api:test@db/memoria"),
        "MEMORIA_MEMORY_WORKER_DATABASE_URL": (
            "postgresql://memoria_memory_worker:test@db/memoria"
        ),
        "MEMORIA_MEMORY_SCHEMA_MANAGED_EXTERNALLY": "true",
        "MEMORIA_SESSION_RUNTIME_BOOTSTRAP_DATABASE_URL": (
            "postgresql://postgres_admin:test@db/memoria"
        ),
        "MEMORIA_SESSION_RUNTIME_PROJECTOR_DATABASE_URL": (
            "postgresql://memoria_session_projector:test@db/memoria"
        ),
        "MEMORIA_SESSION_RUNTIME_WORKER_DATABASE_URL": (
            "postgresql://memoria_session_worker:test@db/memoria"
        ),
        "MEMORIA_SESSION_RUNTIME_MAINTENANCE_DATABASE_URL": (
            "postgresql://memoria_session_maintenance:test@db/memoria"
        ),
        "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET": ("test-runtime-profile-signing-secret-32-bytes"),
        "MEMORIA_DEVICE_BINDING_TOKEN_SECRET": ("test-device-binding-token-secret-32-bytes"),
        "MEMORIA_TRANSFER_EVIDENCE_SECRET": ("test-transfer-evidence-secret-32-bytes"),
        "MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256": "a" * 64,
        "MEMORIA_ARCHIVE_COMPILER_DATABASE_URL": ("postgresql://memoria-compiler:test@db/memoria"),
        "MEMORIA_ARCHIVE_COMPILER_ROLE": "memoria-compiler",
        "MEMORIA_MEMORY_EMBEDDING_URL": "http://embedding:8000/v1/embeddings",
        "MEMORIA_MEMORY_EMBEDDING_API_KEY": "embedding-api-key",
        "MEMORIA_MEMORY_EMBEDDING_MODEL": "embedding-test",
        "MEMORIA_MEMORY_EMBEDDING_DIMENSIONS": "3",
        "MEMORIA_SPEAKER_INTERNAL_TOKEN": "test-speaker-material-that-is-long-enough",
        "MEMORIA_SPEAKER_EMBEDDING_TOKEN": "test-embedding-material-that-is-long-enough",
        "MEMORIA_SPEAKER_TEMPLATE_KEY": Fernet.generate_key().decode("ascii"),
        "MEMORIA_SPEAKER_EMBEDDING_URL": "http://speaker-model:8001/v1/embeddings/speaker",
        "MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "MEMORIA_VOICE_SAMPLE_URL_SECRET": "test-voice-url-material-that-is-long-enough",
        "MEMORIA_VOICE_OBJECT_BUCKET": "test-voice-samples",
        "MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "MEMORIA_ARCHIVE_OBJECT_BUCKET": "test-archive-objects",
        "MEMORIA_RELEASE_TAG": "release-tutor-config-test",
    }
    values.update(overrides)
    return ControlSettings(_env_file=None, **values)


def test_production_requires_independent_guardian_governance_roles() -> None:
    # The happy path passes with the three dedicated roles.
    _production_settings().validate_production()

    cases: list[tuple[dict[str, str], str]] = [
        (
            {"MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL": ""},
            "MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL",
        ),
        (
            {"MEMORIA_GUARDIAN_WORKER_DATABASE_URL": ""},
            "MEMORIA_GUARDIAN_WORKER_DATABASE_URL",
        ),
        (
            {
                "MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL": (
                    "postgresql://memoria_guardian:test@db/memoria"
                )
            },
            "independent",
        ),
        (
            {
                "MEMORIA_GUARDIAN_WORKER_DATABASE_URL": (
                    "postgresql://memoria_guardian:test@db/memoria"
                )
            },
            "independent",
        ),
        (
            {
                "MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL": (
                    "postgresql://memoria_guardian_worker:test@db/memoria"
                )
            },
            "independent",
        ),
        (
            {"MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL": ("sqlite:///tmp/memoria.sqlite3")},
            "MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL",
        ),
    ]
    for overrides, match in cases:
        settings = _production_settings(**overrides)
        with pytest.raises(ValueError, match=match):
            settings.validate_production()

    # The same URL for both roles is rejected as well.
    settings = _production_settings(
        MEMORIA_GUARDIAN_WORKER_DATABASE_URL=(
            "postgresql://memoria_guardian_maintenance:test@db/memoria"
        )
    )
    with pytest.raises(ValueError, match="distinct from each other"):
        settings.validate_production()


def test_production_requires_independent_consent_authority_role() -> None:
    _production_settings().validate_production()

    for value in (
        "",
        "postgresql://memoria_identity:test@db/memoria",
    ):
        settings = _production_settings(MEMORIA_CONSENT_DATABASE_URL=value)
        with pytest.raises(ValueError, match="MEMORIA_CONSENT_DATABASE_URL"):
            settings.validate_production()
