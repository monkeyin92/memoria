"""Control production configuration for the MemoryScope authority."""

from __future__ import annotations

import pytest
from services.control_api.tests.test_tutor_config import _production_settings


@pytest.mark.parametrize(
    "field_name",
    [
        "MEMORIA_MEMORY_API_DATABASE_URL",
        "MEMORIA_MEMORY_WORKER_DATABASE_URL",
    ],
)
def test_production_requires_all_memory_scope_runtime_dsns(
    field_name: str,
) -> None:
    settings = _production_settings(**{field_name: ""})

    with pytest.raises(ValueError, match=field_name):
        settings.validate_production()


def test_production_requires_exactly_one_memory_schema_management_mode() -> None:
    externally_managed = _production_settings(
        MEMORIA_MEMORY_BOOTSTRAP_DATABASE_URL="",
        MEMORIA_MEMORY_SCHEMA_MANAGED_EXTERNALLY="true",
    )
    externally_managed.validate_production()

    unmanaged = _production_settings(
        MEMORIA_MEMORY_BOOTSTRAP_DATABASE_URL="",
        MEMORIA_MEMORY_SCHEMA_MANAGED_EXTERNALLY="false",
    )
    with pytest.raises(
        ValueError,
        match="exactly one MemoryScope schema management",
    ):
        unmanaged.validate_production()

    ambiguous = _production_settings(
        MEMORIA_MEMORY_BOOTSTRAP_DATABASE_URL=("postgresql://memory_owner:test@db/memoria"),
        MEMORIA_MEMORY_SCHEMA_MANAGED_EXTERNALLY="true",
    )
    with pytest.raises(
        ValueError,
        match="exactly one MemoryScope schema management",
    ):
        ambiguous.validate_production()


@pytest.mark.parametrize(
    ("field_name", "value", "expected_role"),
    [
        (
            "MEMORIA_MEMORY_API_DATABASE_URL",
            "postgresql://prefix_memoria_memory_api:test@db/memoria",
            "memoria_memory_api",
        ),
        (
            "MEMORIA_MEMORY_WORKER_DATABASE_URL",
            "postgresql://memoria_memory_worker_backup:test@db/memoria",
            "memoria_memory_worker",
        ),
    ],
)
def test_production_requires_exact_memory_scope_database_usernames(
    field_name: str,
    value: str,
    expected_role: str,
) -> None:
    settings = _production_settings(**{field_name: value})

    with pytest.raises(ValueError, match=expected_role):
        settings.validate_production()


def test_production_rejects_memory_role_reuse_by_other_domains() -> None:
    settings = _production_settings(
        MEMORIA_MEMORY_API_DATABASE_URL=("postgresql://memoria_session_worker:test@db/memoria")
    )
    with pytest.raises(ValueError, match="memoria_memory_api"):
        settings.validate_production()

    settings = _production_settings(
        MEMORIA_SESSION_RUNTIME_WORKER_DATABASE_URL=(
            "postgresql://memoria_memory_worker:test@db/memoria"
        )
    )
    with pytest.raises(ValueError, match="independent"):
        settings.validate_production()


def test_production_rejects_memory_bootstrap_owner_role_reuse() -> None:
    settings = _production_settings(
        MEMORIA_MEMORY_BOOTSTRAP_DATABASE_URL=("postgresql://memoria_memory_api:test@db/memoria"),
        MEMORIA_MEMORY_SCHEMA_MANAGED_EXTERNALLY="false",
    )

    with pytest.raises(ValueError, match="independent MemoryScope bootstrap"):
        settings.validate_production()
