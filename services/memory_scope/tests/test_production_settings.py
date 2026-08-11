"""MemoryScope production DSN and schema-management invariants."""

from __future__ import annotations

import pytest
from services.memory_scope.production import MemoryProductionSettings


def _settings(**overrides: object) -> MemoryProductionSettings:
    values: dict[str, object] = {
        "api_dsn": "postgresql://memoria_memory_api:test@db/memoria",
        "worker_dsn": "postgresql://memoria_memory_worker:test@db/memoria",
        "action_executor_dsn": ("postgresql://memoria_action_executor:test@db/memoria"),
        "schema_managed_externally": True,
    }
    values.update(overrides)
    return MemoryProductionSettings(**values)  # type: ignore[arg-type]


def test_external_schema_mode_accepts_the_three_exact_runtime_roles() -> None:
    settings = _settings()

    assert settings.schema_managed_externally is True


@pytest.mark.parametrize(
    ("field_name", "value", "expected_role"),
    [
        (
            "api_dsn",
            "postgresql://prefix_memoria_memory_api:test@db/memoria",
            "memoria_memory_api",
        ),
        (
            "worker_dsn",
            "postgresql://memoria_memory_worker_backup:test@db/memoria",
            "memoria_memory_worker",
        ),
        (
            "action_executor_dsn",
            "postgresql://backup_memoria_action_executor:test@db/memoria",
            "memoria_action_executor",
        ),
    ],
)
def test_role_name_substrings_do_not_satisfy_exact_username_validation(
    field_name: str,
    value: str,
    expected_role: str,
) -> None:
    with pytest.raises(ValueError, match=f"exactly as the {expected_role} role"):
        _settings(**{field_name: value})


def test_api_worker_and_action_executor_roles_must_be_independent() -> None:
    with pytest.raises(ValueError, match="independent roles"):
        _settings(
            worker_dsn="postgresql://memoria_memory_api:test@db/memoria",
            worker_role="memoria_memory_api",
        )


def test_bootstrap_owner_must_not_reuse_a_runtime_role() -> None:
    with pytest.raises(ValueError, match="owner distinct from runtime roles"):
        _settings(
            schema_managed_externally=False,
            bootstrap_dsn="postgresql://memoria_memory_api:test@db/memoria",
            app_role_password="test",
        )


def test_schema_management_mode_rejects_both_bootstrap_and_external() -> None:
    with pytest.raises(ValueError, match="cannot also configure bootstrap"):
        _settings(
            bootstrap_dsn="postgresql://memory_owner:test@db/memoria",
            app_role_password="test",
        )


def test_schema_management_mode_rejects_neither_bootstrap_nor_external() -> None:
    with pytest.raises(ValueError, match="memory schema requires"):
        _settings(schema_managed_externally=False)


def test_bootstrap_mode_requires_both_owner_dsn_and_role_password() -> None:
    with pytest.raises(ValueError, match="memory schema requires"):
        _settings(
            schema_managed_externally=False,
            bootstrap_dsn="postgresql://memory_owner:test@db/memoria",
        )

    with pytest.raises(ValueError, match="memory schema requires"):
        _settings(
            schema_managed_externally=False,
            app_role_password="test",
        )
