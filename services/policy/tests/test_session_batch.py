from __future__ import annotations

from datetime import UTC, datetime

import pytest
from services.policy.action_authorizer import (
    ActionExecutionRequest,
    PostgresActionAuthorizer,
)
from services.policy.engine import PolicyEngine
from services.policy.session_batch import (
    SessionPolicyBatchAuthorizer,
)
from services.policy.tests.fakes import make_context

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)


class _Connection:
    def is_in_transaction(self) -> bool:
        return True


class _Repository:
    def __init__(self) -> None:
        self.inserted: tuple[object, ...] = ()

    async def insert_many(
        self, connection: object, receipts: tuple[object, ...]
    ) -> None:
        assert connection.is_in_transaction()  # type: ignore[attr-defined]
        self.inserted = receipts


class _Authority:
    def __init__(self, context: object) -> None:
        self.context = context

    async def _lock_current(self, *_args: object) -> object:
        return self.context


class _ProfileAuthority:
    def __init__(self) -> None:
        self.calls = 0

    async def lock_current(self, connection: object, receipts: object) -> None:
        assert connection.is_in_transaction()  # type: ignore[attr-defined]
        assert receipts
        self.calls += 1


@pytest.mark.asyncio
async def test_session_batch_appends_and_persists_profile_on_same_connection() -> None:
    context = make_context(evaluated_at=NOW)
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))
    repository = _Repository()
    profile = _ProfileAuthority()
    connection = _Connection()
    authorizer = SessionPolicyBatchAuthorizer(
        repository=repository,  # type: ignore[arg-type]
        action_authorizer=PostgresActionAuthorizer(
            authority=_Authority(context),
            receipt_locker=lambda _connection, _receipt_id: receipt,
        ),
        profile_authority=profile,
    )

    result = await authorizer.execute_profile_persist(
        connection,
        receipts=(receipt,),
        requests=(ActionExecutionRequest(receipt.receipt_id, context, NOW),),
        write_callback=lambda active, locked: (
            "persisted"
            if active is connection and locked == (receipt,)
            else "wrong"
        ),
    )

    assert result == "persisted"
    assert repository.inserted == (receipt,)
    assert profile.calls == 1


@pytest.mark.asyncio
async def test_session_batch_fails_closed_without_profile_authority() -> None:
    context = make_context(evaluated_at=NOW)
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))
    callback_called = False

    async def callback(*_args: object) -> None:
        nonlocal callback_called
        callback_called = True

    with pytest.raises(PermissionError, match="profile authority"):
        await SessionPolicyBatchAuthorizer(
            repository=_Repository(),  # type: ignore[arg-type]
            action_authorizer=PostgresActionAuthorizer(
                authority=_Authority(context),
                receipt_locker=lambda _connection, _receipt_id: receipt,
            ),
        ).execute_profile_persist(
            _Connection(),
            receipts=(receipt,),
            requests=(ActionExecutionRequest(receipt.receipt_id, context, NOW),),
            write_callback=callback,
        )

    assert not callback_called
