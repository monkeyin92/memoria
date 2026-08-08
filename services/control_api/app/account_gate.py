"""Single-worker write drain used before irreversible account deletion."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from threading import Condition
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request

from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.evolution.account_fence import AccountWriteBlockedError


class AccountDeletingError(AccountWriteBlockedError):
    pass


class AccountOperationGate:
    def __init__(self) -> None:
        # The deletion worker and evolution sleep worker may run in different
        # threads.  A threading condition keeps the lease valid across both
        # async request tasks and synchronous worker calls.
        self._condition = Condition()
        self._blocked: set[str] = set()
        self._active_writes: dict[str, int] = {}
        self._active_reads: dict[str, int] = {}

    @contextmanager
    def sync_write(self, account_id: str) -> Iterator[None]:
        """Acquire an in-process write lease for a sync worker."""

        with self._condition:
            if account_id in self._blocked:
                raise AccountDeletingError("account deletion is in progress")
            self._active_writes[account_id] = self._active_writes.get(account_id, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                remaining = self._active_writes.get(account_id, 1) - 1
                if remaining > 0:
                    self._active_writes[account_id] = remaining
                else:
                    self._active_writes.pop(account_id, None)
                self._condition.notify_all()

    @asynccontextmanager
    async def write(self, account_id: str) -> AsyncIterator[None]:
        with self.sync_write(account_id):
            yield

    @contextmanager
    def sync_read(self, account_id: str) -> Iterator[None]:
        """Acquire a short read lease that deletion waits to drain.

        This is intentionally opt-in for privacy-sensitive projections such
        as evolution resolution; ordinary account reads keep their existing
        behavior. A durable tombstone remains the cross-process backstop.
        """

        with self._condition:
            if account_id in self._blocked:
                raise AccountDeletingError("account deletion is in progress")
            self._active_reads[account_id] = self._active_reads.get(account_id, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                remaining = self._active_reads.get(account_id, 1) - 1
                if remaining > 0:
                    self._active_reads[account_id] = remaining
                else:
                    self._active_reads.pop(account_id, None)
                self._condition.notify_all()

    @asynccontextmanager
    async def read(self, account_id: str) -> AsyncIterator[None]:
        with self.sync_read(account_id):
            yield

    async def block_account(self, account_id: str) -> None:
        await asyncio.to_thread(self._block_account, account_id)

    def _block_account(self, account_id: str) -> None:
        with self._condition:
            self._blocked.add(account_id)
            while (
                self._active_writes.get(account_id, 0) > 0
                or self._active_reads.get(account_id, 0) > 0
            ):
                self._condition.wait()


async def require_writable_account(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> AsyncIterator[AuthenticatedUser]:
    gate = cast(AccountOperationGate, request.app.state.account_operations)
    try:
        async with gate.write(user.user_id):
            yield user
    except AccountDeletingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
