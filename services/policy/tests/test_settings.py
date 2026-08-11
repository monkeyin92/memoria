from __future__ import annotations

from datetime import UTC, datetime

import pytest
from packages.contracts.generated.python import multi_subject_contracts as generated
from services.policy.production_wiring import (
    PostgresPolicySettings,
    build_postgres_decision_lifecycle,
)
from services.policy.tests.fakes import make_context


def test_postgres_policy_settings_fail_closed_without_dedicated_dsn() -> None:
    with pytest.raises(ValueError, match="MEMORIA_POLICY_POSTGRES_DSN"):
        PostgresPolicySettings.from_environment({})


@pytest.mark.parametrize(
    "dsn",
    [
        "",
        "sqlite:///tmp/policy.db",
        "postgresql://memoria_policy_api@/missing-host",
        "postgresql://other:secret@localhost/policy",
        "postgresql://memoria_policy_api:secret@localhost/",
    ],
)
def test_postgres_policy_settings_reject_malformed_or_wrong_role_dsn(dsn: str) -> None:
    with pytest.raises(ValueError):
        PostgresPolicySettings(dsn=dsn)


def test_postgres_decision_lifecycle_is_generated_and_postgres_backed() -> None:
    settings = PostgresPolicySettings(
        dsn="postgresql://memoria_policy_api:secret@localhost/policy"
    )
    lifecycle = build_postgres_decision_lifecycle(settings)

    assert lifecycle.settings is settings
    assert lifecycle.output_type is generated.PolicyDecision
    assert not hasattr(lifecycle, "in_memory_writer")
    assert callable(lifecycle.decide_and_persist)


def test_settings_reject_bool_timeout() -> None:
    with pytest.raises(ValueError, match="connect_timeout_seconds"):
        PostgresPolicySettings(
            dsn="postgresql://memoria_policy_api:secret@localhost/policy",
            connect_timeout_seconds=True,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_lifecycle_does_not_return_before_repository_success() -> None:
    class _FailingRepository:
        async def get(self, receipt_id: str) -> None:
            del receipt_id
            return None

        async def insert(self, receipt: generated.PolicyReceiptV2) -> None:
            del receipt
            raise RuntimeError("postgres unavailable")

    settings = PostgresPolicySettings(
        dsn="postgresql://memoria_policy_api:secret@localhost/policy"
    )
    lifecycle = build_postgres_decision_lifecycle(
        settings, repository=_FailingRepository()
    )

    with pytest.raises(RuntimeError, match="postgres unavailable"):
        await lifecycle.decide_and_persist(
            make_context(
                evaluated_at=datetime(2026, 8, 10, 16, 0, tzinfo=UTC)
            )
        )
