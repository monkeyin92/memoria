from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from services.policy.action_authorizer import (
    ActionAuthorizationError,
    ActionExecutionRequest,
    CompositeActionAuthority,
    PostgresActionAuthorizer,
    RejectingActionAuthority,
)
from services.policy.engine import PolicyEngine
from services.policy.tests.fakes import make_binding, make_consent, make_context

NOW = datetime(2026, 8, 10, 11, 0, tzinfo=UTC)


class _Connection:
    def __init__(self, *, in_transaction: bool = True) -> None:
        self._in_transaction = in_transaction

    def is_in_transaction(self) -> bool:
        return self._in_transaction


class _Authority:
    def __init__(self, context: object) -> None:
        self.context = context
        self.calls = 0

    async def _lock_current(self, connection: object, receipt: object, request: object):
        self.calls += 1
        return self.context


class _Stage:
    def __init__(self, name: str, value: object, calls: list[str]) -> None:
        self.name = name
        self.value = value
        self.calls = calls

    async def lock_current(
        self, connection: object, receipt: object, request: object
    ) -> object:
        del connection, receipt, request
        self.calls.append(self.name)
        return self.value


def _exact_case():
    consent = make_consent(
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        data_classification="biometric",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    engine = PolicyEngine()
    decision = engine.decide(context)
    return context, engine.receipt_for(context, decision)


@pytest.mark.asyncio
async def test_execute_authorized_runs_callback_only_while_transaction_is_open() -> None:
    context, receipt = _exact_case()
    connection = _Connection()
    authority = _Authority(context)
    callback_calls = 0

    async def callback(active_connection: object, locked_receipt: object) -> str:
        nonlocal callback_calls
        callback_calls += 1
        assert active_connection is connection
        assert locked_receipt is receipt
        assert connection.is_in_transaction()
        return "written"

    authorizer = PostgresActionAuthorizer(
        authority=authority,
        receipt_locker=lambda _connection, _receipt_id: receipt,
    )
    result = await authorizer.execute_authorized(
        connection,
        ActionExecutionRequest(receipt.receipt_id, context, NOW),
        callback,
    )

    assert result == "written"
    assert callback_calls == 1
    assert authority.calls == 1


@pytest.mark.asyncio
async def test_execute_authorized_fails_closed_without_open_transaction() -> None:
    context, receipt = _exact_case()
    authorizer = PostgresActionAuthorizer(
        authority=_Authority(context),
        receipt_locker=lambda _connection, _receipt_id: receipt,
    )

    with pytest.raises(ActionAuthorizationError, match="transaction"):
        await authorizer.execute_authorized(
            _Connection(in_transaction=False),
            ActionExecutionRequest(receipt.receipt_id, context, NOW),
            lambda *_args: None,
        )


@pytest.mark.asyncio
async def test_missing_authority_adapter_and_revoked_evidence_never_call_callback() -> None:
    context, receipt = _exact_case()
    callback_called = False

    async def callback(*_args: object) -> None:
        nonlocal callback_called
        callback_called = True

    rejecting = PostgresActionAuthorizer(
        authority=RejectingActionAuthority(),
        receipt_locker=lambda _connection, _receipt_id: receipt,
    )
    with pytest.raises(ActionAuthorizationError):
        await rejecting.execute_authorized(
            _Connection(),
            ActionExecutionRequest(receipt.receipt_id, context, NOW),
            callback,
        )

    revoked = replace(
        context,
        consent_evidence=(replace(context.consent_evidence[0], status="revoked"),),
    )
    invalid = PostgresActionAuthorizer(
        authority=_Authority(revoked),
        receipt_locker=lambda _connection, _receipt_id: receipt,
    )
    with pytest.raises(ActionAuthorizationError):
        await invalid.execute_authorized(
            _Connection(),
            ActionExecutionRequest(receipt.receipt_id, context, NOW),
            callback,
        )

    assert not callback_called


@pytest.mark.asyncio
async def test_composite_locks_required_current_heads_in_canonical_order() -> None:
    context, receipt = _exact_case()
    calls: list[str] = []
    composite = CompositeActionAuthority(
        principal=_Stage("principal", context.actor_id, calls),
        consent=_Stage("consent", context.consent_evidence, calls),
        binding=_Stage("binding", context.binding_evidence, calls),
        action=_Stage("action", receipt.action_resource_fence, calls),
    )
    authorizer = PostgresActionAuthorizer(
        authority=composite,
        receipt_locker=lambda _connection, _receipt_id: receipt,
    )

    result = await authorizer.execute_authorized(
        _Connection(),
        ActionExecutionRequest(receipt.receipt_id, context, NOW),
        lambda _connection, _receipt: "written",
    )

    assert result == "written"
    assert calls == ["principal", "consent", "binding", "action"]


@pytest.mark.asyncio
async def test_composite_fails_closed_when_required_action_adapter_is_missing() -> None:
    context, receipt = _exact_case()
    composite = CompositeActionAuthority(
        principal=_Stage("principal", context.actor_id, []),
        consent=_Stage("consent", context.consent_evidence, []),
        binding=_Stage("binding", context.binding_evidence, []),
    )

    with pytest.raises(ActionAuthorizationError, match="action adapter"):
        await PostgresActionAuthorizer(
            authority=composite,
            receipt_locker=lambda _connection, _receipt_id: receipt,
        ).execute_authorized(
            _Connection(),
            ActionExecutionRequest(receipt.receipt_id, context, NOW),
            lambda *_args: None,
        )
