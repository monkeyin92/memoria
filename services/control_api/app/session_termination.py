"""Server-side invalidation for every realtime session owned by an account."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore


class RealtimeConnections(Protocol):
    async def close_account(self, account_id: str) -> int: ...


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


class AccountSessionTerminator:
    def __init__(
        self,
        *,
        store: MemoryStore,
        connections: RealtimeConnections,
        close_room: Callable[[str], Awaitable[None]],
    ) -> None:
        self._store = store
        self._connections = connections
        self._close_room = close_room

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
        for session in sessions:
            if session["voice_backend"] == "cascade":
                await self._close_room(str(session["room_name"]))
        await asyncio.to_thread(
            self._store.delete_voice_sessions,
            user_id=account_id,
        )
        return len(sessions)


class LiveKitRoomCloser:
    def __init__(self, settings: ControlSettings) -> None:
        self._settings = settings

    async def __call__(self, room_name: str) -> None:
        if self._settings.offline_mock:
            return
        from livekit import api

        async with api.LiveKitAPI(
            self._settings.livekit_url,
            self._settings.livekit_api_key,
            self._settings.livekit_api_secret,
        ) as livekit:
            await livekit.room.delete_room(api.DeleteRoomRequest(room=room_name))
