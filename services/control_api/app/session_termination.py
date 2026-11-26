"""Server-side invalidation for every realtime session owned by an account."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from services.control_api.app.database import MemoryStore


class RealtimeConnections(Protocol):
    async def close_account(self, account_id: str) -> int: ...

    async def close_sessions(self, session_ids: set[str]) -> int: ...


class RealtimeConnection(Protocol):
    async def close(self, *, code: int, reason: str) -> None: ...


class RealtimeConnectionRegistry:
    def __init__(self) -> None:
        self._connections: dict[str, dict[str, RealtimeConnection]] = {}
        self._revoked_accounts: set[str] = set()

    async def register(
        self,
        *,
        account_id: str,
        session_id: str,
        connection: RealtimeConnection,
    ) -> bool:
        if account_id in self._revoked_accounts:
            await connection.close(code=4401, reason="account deleting")
            return False
        self._connections.setdefault(account_id, {})[session_id] = connection
        return True

    def unregister(self, *, account_id: str, session_id: str) -> None:
        account_connections = self._connections.get(account_id)
        if account_connections is None:
            return
        account_connections.pop(session_id, None)
        if not account_connections:
            self._connections.pop(account_id, None)

    async def close_account(self, account_id: str) -> int:
        self._revoked_accounts.add(account_id)
        account_connections = self._connections.get(account_id, {})
        count = len(account_connections)
        for session_id, connection in tuple(account_connections.items()):
            await connection.close(code=4401, reason="account deleting")
            self.unregister(account_id=account_id, session_id=session_id)
        return count

    async def close_sessions(self, session_ids: set[str]) -> int:
        """Close exact sessions without revoking unrelated grantee sessions."""

        count = 0
        for account_id, account_connections in tuple(self._connections.items()):
            for session_id, connection in tuple(account_connections.items()):
                if session_id not in session_ids:
                    continue
                await connection.close(code=4401, reason="account deleting")
                self.unregister(account_id=account_id, session_id=session_id)
                count += 1
        return count


class AccountSessionTerminator:
    def __init__(
        self,
        *,
        store: MemoryStore,
        connections: RealtimeConnections,
        close_subject_session: Callable[..., Awaitable[bool]] | None = None,
    ) -> None:
        self._store = store
        self._connections = connections
        self._close_subject_session = close_subject_session

    async def terminate_account(self, account_id: str) -> int:
        sessions = await asyncio.to_thread(
            self._store.list_voice_sessions,
            user_id=account_id,
        )
        await asyncio.to_thread(
            self._store.mark_voice_sessions_deleting,
            user_id=account_id,
            deleted_at=datetime.now(UTC).isoformat(),
        )
        await self._connections.close_account(account_id)
        await self._connections.close_sessions(
            {str(session["session_id"]) for session in sessions}
        )
        await asyncio.to_thread(
            self._store.delete_voice_sessions,
            user_id=account_id,
        )
        return len(sessions)

    async def terminate_subject(self, subject_id: str, *, reason: str = "subject_data_deleted",
                                binding_ids: frozenset[str] | None = None) -> int:
        session_ids = await asyncio.to_thread(self._store.open_device_media_session_ids)
        if not session_ids:
            return 0
        close_subject_session = self._close_subject_session
        if close_subject_session is None:
            raise RuntimeError("device session close authority is not configured")
        closed = set()
        for session_id in session_ids:
            row = await asyncio.to_thread(self._store.get_device_media_session, session_id=session_id)
            if row is None:
                continue
            if await close_subject_session(device_id=str(row["device_id"]), session_id=session_id,
                    reason=reason, subject_id=subject_id, binding_ids=binding_ids):
                closed.add(session_id)
        await self._connections.close_sessions(closed)
        return len(closed)
