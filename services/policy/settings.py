"""Fail-closed production bootstrap for PostgreSQL Policy decisions.

Control currently owns an in-memory writer and is outside this package's write
boundary.  ``build_postgres_decision_lifecycle`` is the concrete replacement
seam: it creates a generated-only ``DecisionService`` backed by the append-only
PostgreSQL repository.  Until Control calls this seam, production persistence
is an explicit unmet integration dependency and is not end-to-end enabled.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from packages.contracts.generated.python.multi_subject_contracts import PolicyDecision

from services.policy.context import PolicyContext
from services.policy.decision_service import (
    DecisionService,
    PolicyReceiptRepositoryPort,
)
from services.policy.engine import PolicyEngine
from services.policy.postgres_receipt_repository import (
    PostgresPolicyReceiptRepository,
)

_DSN_ENV = "MEMORIA_POLICY_POSTGRES_DSN"


def _positive_number(value: float, field: str, *, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) <= maximum
    ):
        raise ValueError(f"{field} must be a finite positive number <= {maximum}")
    return float(value)


@dataclass(frozen=True, slots=True)
class PostgresPolicySettings:
    """Validated runtime settings for the API decision producer."""

    dsn: str
    connect_timeout_seconds: float = 5.0
    command_timeout_seconds: float = 10.0
    application_name: str = "memoria-policy-api"

    def __post_init__(self) -> None:
        if not isinstance(self.dsn, str) or not self.dsn.strip():
            raise ValueError("dsn must be a non-empty PostgreSQL URL")
        parsed = urlsplit(self.dsn)
        if parsed.scheme not in {"postgres", "postgresql"}:
            raise ValueError("dsn must use postgres or postgresql")
        if parsed.hostname is None or not parsed.hostname.strip():
            raise ValueError("dsn requires a hostname")
        if parsed.username != "memoria_policy_api":
            raise ValueError("dsn must authenticate as memoria_policy_api")
        if parsed.password is None or not parsed.password:
            raise ValueError("dsn requires an explicit password")
        if not parsed.path.removeprefix("/").strip():
            raise ValueError("dsn requires a database name")
        if parsed.fragment:
            raise ValueError("dsn fragments are forbidden")
        object.__setattr__(
            self,
            "connect_timeout_seconds",
            _positive_number(
                self.connect_timeout_seconds,
                "connect_timeout_seconds",
                maximum=60.0,
            ),
        )
        object.__setattr__(
            self,
            "command_timeout_seconds",
            _positive_number(
                self.command_timeout_seconds,
                "command_timeout_seconds",
                maximum=300.0,
            ),
        )
        if (
            not isinstance(self.application_name, str)
            or not self.application_name.strip()
            or len(self.application_name) > 64
        ):
            raise ValueError("application_name must be a bounded non-empty string")

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str]
    ) -> PostgresPolicySettings:
        dsn = environment.get(_DSN_ENV)
        if dsn is None:
            raise ValueError(f"{_DSN_ENV} is required")
        try:
            connect_timeout = float(
                environment.get("MEMORIA_POLICY_CONNECT_TIMEOUT_SECONDS", "5")
            )
            command_timeout = float(
                environment.get("MEMORIA_POLICY_COMMAND_TIMEOUT_SECONDS", "10")
            )
        except ValueError as exc:
            raise ValueError("policy PostgreSQL timeouts must be numeric") from exc
        return cls(
            dsn=dsn,
            connect_timeout_seconds=connect_timeout,
            command_timeout_seconds=command_timeout,
            application_name=environment.get(
                "MEMORIA_POLICY_APPLICATION_NAME", "memoria-policy-api"
            ),
        )


@dataclass(frozen=True, slots=True)
class PostgresDecisionLifecycle:
    """Generated decision producer that returns only after receipt persistence."""

    settings: PostgresPolicySettings
    _service: DecisionService

    @property
    def output_type(self) -> type[PolicyDecision]:
        return PolicyDecision

    async def decide_and_persist(self, context: PolicyContext) -> PolicyDecision:
        return await self._service.decide_and_persist(context)


def _build_postgres_decision_lifecycle(
    settings: PostgresPolicySettings,
    *,
    engine: PolicyEngine | None = None,
    repository: PolicyReceiptRepositoryPort | None = None,
) -> PostgresDecisionLifecycle:
    """Build the production Postgres decision lifecycle; no in-memory fallback."""
    selected_repository = repository or PostgresPolicyReceiptRepository(
        dsn=settings.dsn,
        connect_timeout_seconds=settings.connect_timeout_seconds,
        command_timeout_seconds=settings.command_timeout_seconds,
        application_name=settings.application_name,
    )
    return PostgresDecisionLifecycle(
        settings=settings,
        _service=DecisionService(
            engine=engine or PolicyEngine(),
            repository=selected_repository,
        ),
    )
