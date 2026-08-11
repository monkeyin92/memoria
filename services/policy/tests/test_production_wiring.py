from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any, cast

import asyncpg
import pytest
from services.policy.action_authorizer import (
    ActionAuthorizationError,
    ActionExecutionRequest,
    PostgresActionAuthorizer,
)
from services.policy.action_fence import build_action_resource_fence
from services.policy.context import canonical_purpose_for_capability as context_resolver
from services.policy.engine import PolicyEngine
from services.policy.production_wiring import (
    PostgresCurrentConsentAuthorityAdapter,
    ProductionAuthorityAdapters,
    SensitiveWriteService,
    build_composite_action_authority,
    canonical_purpose_for_capability,
)
from services.policy.tests.fakes import (
    make_binding,
    make_consent,
    make_consent_snapshot_ref,
    make_context,
)

NOW = datetime(2026, 8, 10, 17, 0, tzinfo=UTC)


class _Connection:
    def is_in_transaction(self) -> bool:
        return True


class _ConsentTransaction:
    def __init__(self) -> None:
        self.expected: tuple[object, ...] = ()
        self.connection: object | None = None

    async def execute_with_authority(
        self,
        conn: asyncpg.Connection,
        *,
        expected: tuple[object, ...],
        now: datetime,
        operation: Any,
    ) -> None:
        assert now == NOW
        self.connection = conn
        self.expected = expected
        await operation(conn)


def _current_snapshot_case() -> tuple[object, object, object]:
    consent = make_consent(
        snapshot_id="historical-source-1",
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    current = make_consent_snapshot_ref(
        snapshot_id="current-global-head-7",
        revision=7,
        canonical_hash="7" * 64,
        consents=(consent,),
        now=NOW,
    )
    action_fence = build_action_resource_fence(
        capability="voice_clone_use",
        purpose="voice_clone",
        action_resource_id="voice-clone-write-1",
        action_revision=1,
        consent_snapshot_id=current.snapshot_id,
        consent_snapshot_revision=current.revision,
        consent_snapshot_hash=current.canonical_hash,
        generation_id=0,
        turn_id=0,
        tool_epoch=0,
        issued_at=NOW,
        valid_until=NOW.replace(minute=NOW.minute + 5),
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        data_classification="biometric",
        consent_evidence=(consent,),
        consent_snapshot_evidence=(current,),
        binding_evidence=make_binding(now=NOW),
        action_resource_fence=action_fence,
        evaluated_at=NOW,
    )
    engine = PolicyEngine()
    receipt = engine.receipt_for(context, engine.decide(context))
    return consent, context, receipt


def test_production_wiring_reexports_the_single_context_purpose_resolver() -> None:
    assert canonical_purpose_for_capability is context_resolver


def test_public_sensitive_write_api_is_callback_only_and_typed_connection() -> None:
    signature = inspect.signature(SensitiveWriteService.execute)
    assert list(signature.parameters) == [
        "self",
        "connection",
        "context",
        "write_callback",
    ]
    assert "asyncpg.Connection" in str(signature.parameters["connection"].annotation)
    assert "PolicyContext" not in str(signature.return_annotation)


@pytest.mark.asyncio
async def test_missing_composite_adapters_fail_before_callback() -> None:
    _consent, context, receipt = _current_snapshot_case()
    callback_called = False

    async def callback(*_args: object) -> None:
        nonlocal callback_called
        callback_called = True

    class _Repository:
        def __init__(self) -> None:
            self.receipt: object | None = None

        async def insert_many(self, connection: object, receipts: tuple[object, ...]) -> None:
            del connection
            self.receipt = receipts[0]

    repository = _Repository()
    service = SensitiveWriteService(
        authorizer=PostgresActionAuthorizer(
            authority=build_composite_action_authority(
                ProductionAuthorityAdapters()
            ),
            receipt_locker=lambda _connection, _receipt_id: repository.receipt,
        ),
        repository=cast(Any, repository),
        engine=PolicyEngine(receipt_id_factory=lambda: receipt.receipt_id),  # type: ignore[union-attr]
    )
    with pytest.raises(ActionAuthorizationError, match="principal adapter"):
        await service.execute(
            cast(asyncpg.Connection, cast(Any, _Connection())),
            context,  # type: ignore[arg-type]
            callback,
        )
    assert not callback_called


@pytest.mark.asyncio
async def test_consent_adapter_locks_current_global_head_not_historical_source() -> None:
    consent, context, receipt = _current_snapshot_case()
    transaction = _ConsentTransaction()
    adapter = PostgresCurrentConsentAuthorityAdapter(
        authorizer=cast(Any, transaction)
    )
    connection = cast(asyncpg.Connection, cast(Any, _Connection()))

    locked = await adapter.lock_current(
        connection,
        receipt,  # type: ignore[arg-type]
        ActionExecutionRequest(receipt.receipt_id, context, NOW),  # type: ignore[arg-type]
    )

    assert locked == (consent,)
    assert transaction.connection is connection
    expected = transaction.expected[0]
    assert expected.current_snapshot_id == "current-global-head-7"
    assert expected.current_snapshot_revision == 7
    assert expected.current_snapshot_hash == "7" * 64
    assert consent.snapshot_id == "historical-source-1"


def test_composite_has_no_public_verified_context_api() -> None:
    composite = build_composite_action_authority(ProductionAuthorityAdapters())
    assert not hasattr(composite, "lock_current")
    assert hasattr(composite, "_lock_current")
