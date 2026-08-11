from __future__ import annotations

from datetime import UTC, datetime

import pytest
from services.policy.engine import PolicyEngine
from services.policy.postgres_receipt_repository import (
    ConnectionBoundPolicyReceiptRepository,
)
from services.policy.tests.fakes import make_context

NOW = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)


class _NoTransactionConnection:
    def is_in_transaction(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_connection_bound_repository_requires_caller_transaction() -> None:
    context = make_context(evaluated_at=NOW)
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))

    with pytest.raises(RuntimeError, match="caller-owned transaction"):
        await ConnectionBoundPolicyReceiptRepository().insert_many(
            _NoTransactionConnection(), (receipt,)
        )
