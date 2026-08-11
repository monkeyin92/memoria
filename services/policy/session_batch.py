"""Caller-transaction Session receipt batch + profile persistence seam.

``SessionPolicyBatchAuthorizer.execute_profile_persist`` is the only public
operation.  It appends a complete generated receipt batch, locks and validates
every current action authority on the same caller-owned connection, locks the
current sensitive runtime-profile authority, and invokes one immediate profile
write callback while every lock is retained.  This module never acquires a
connection or manages a transaction.  Downstream Session wiring is still an
explicit integration dependency; this seam alone is not end-to-end activation.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar, cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyReceiptV2,
)

from services.policy.action_authorizer import (
    ActionAuthorizationError,
    ActionExecutionRequest,
    PostgresActionAuthorizer,
)
from services.policy.context import is_resource_scoped_action

T = TypeVar("T")


class ConnectionBoundReceiptBatchPort(Protocol):
    async def insert_many(
        self,
        connection: asyncpg.Connection,
        receipts: tuple[PolicyReceiptV2, ...],
    ) -> None: ...


class RuntimeProfileAuthorityAdapter(Protocol):
    """Lock the current Session profile head on the supplied connection."""

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipts: tuple[PolicyReceiptV2, ...],
    ) -> None: ...


type ProfileWriteCallback[T] = Callable[
    [asyncpg.Connection, tuple[PolicyReceiptV2, ...]], T | Awaitable[T]
]


class SessionPolicyBatchAuthorizer:
    """Append and consume an all-or-nothing generated receipt batch."""

    def __init__(
        self,
        *,
        repository: ConnectionBoundReceiptBatchPort,
        action_authorizer: PostgresActionAuthorizer,
        profile_authority: RuntimeProfileAuthorityAdapter | None = None,
    ) -> None:
        self._repository = repository
        self._action_authorizer = action_authorizer
        self._profile_authority = profile_authority

    async def execute_profile_persist(
        self,
        connection: asyncpg.Connection,
        *,
        receipts: tuple[PolicyReceiptV2, ...],
        requests: tuple[ActionExecutionRequest, ...],
        write_callback: ProfileWriteCallback[T],
    ) -> T:
        in_transaction = getattr(connection, "is_in_transaction", None)
        if not callable(in_transaction) or not in_transaction():
            raise ActionAuthorizationError(
                "profile batch requires an already-open caller transaction"
            )
        if not receipts or len(receipts) != len(requests):
            raise ActionAuthorizationError("profile receipt batch is incomplete")
        if any(is_resource_scoped_action(item.capability.value) for item in receipts):
            raise ActionAuthorizationError(
                "resource-scoped actions cannot be pre-signed in a Session profile"
            )
        receipt_ids = tuple(item.receipt_id for item in receipts)
        request_ids = tuple(item.receipt_id for item in requests)
        if len(set(receipt_ids)) != len(receipt_ids) or set(receipt_ids) != set(
            request_ids
        ):
            raise ActionAuthorizationError("profile receipt/request identities mismatch")
        if self._profile_authority is None:
            raise ActionAuthorizationError("profile authority adapter is not configured")

        ordered_receipts = tuple(sorted(receipts, key=lambda item: item.receipt_id))
        requests_by_id = {item.receipt_id: item for item in requests}
        await self._repository.insert_many(connection, ordered_receipts)
        locked = tuple(
            [
                await self._action_authorizer._lock_and_validate(
                    connection, requests_by_id[receipt.receipt_id]
                )
                for receipt in ordered_receipts
            ]
        )
        await self._profile_authority.lock_current(connection, locked)
        result = write_callback(connection, locked)
        if inspect.isawaitable(result):
            return await cast(Awaitable[T], result)
        return result
