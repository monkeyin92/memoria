"""Single-worker write drain used before irreversible account deletion."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request

from services.control_api.app.security import AuthenticatedUser, require_authenticated_user


class AccountDeletingError(RuntimeError):
    pass


class AccountOperationGate:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._blocked: set[str] = set()
        self._active_writes: dict[str, int] = {}

    @asynccontextmanager
    async def write(self, account_id: str) -> AsyncIterator[None]:
        async with self._condition:
            if account_id in self._blocked:
                raise AccountDeletingError("account deletion is in progress")
            self._active_writes[account_id] = self._active_writes.get(account_id, 0) + 1
        try:
            yield
        finally:
            async with self._condition:
                remaining = self._active_writes.get(account_id, 1) - 1
                if remaining > 0:
                    self._active_writes[account_id] = remaining
                else:
                    self._active_writes.pop(account_id, None)
                self._condition.notify_all()

    async def block_account(self, account_id: str) -> None:
        async with self._condition:
            self._blocked.add(account_id)
            while self._active_writes.get(account_id, 0) > 0:
                await self._condition.wait()


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
