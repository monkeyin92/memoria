from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from livekit import api
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import hash_password
from services.control_api.app.session_termination import (
    AccountSessionTerminator,
    LiveKitRoomCloser,
    RealtimeConnectionRegistry,
)


class ConnectionRegistryStub:
    def __init__(self) -> None:
        self.closed: list[str] = []

    async def close_account(self, account_id: str) -> int:
        self.closed.append(account_id)
        return 1


class SocketStub:
    def __init__(self) -> None:
        self.closed: list[tuple[int, str]] = []

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append((code, reason))


@pytest.mark.asyncio
async def test_realtime_registry_closes_only_the_deleted_accounts_connections() -> None:
    registry = RealtimeConnectionRegistry()
    owned = SocketStub()
    other = SocketStub()
    await registry.register(account_id="account-delete", session_id="owned", connection=owned)
    await registry.register(account_id="account-other", session_id="other", connection=other)

    count = await registry.close_account("account-delete")

    assert count == 1
    assert owned.closed == [(4401, "account deleting")]
    assert other.closed == []


@pytest.mark.asyncio
async def test_account_session_termination_closes_live_connections_and_invalidates_rows(
    tmp_path: Path,
) -> None:
    store = MemoryStore(str(tmp_path / "memoria.sqlite3"))
    now = datetime.now(UTC).isoformat()
    store.register_account(
        user_id="account-delete",
        username="owner",
        username_normalized="owner",
        password_hash=hash_password("safe-passphrase"),
        now=now,
    )
    store.add_voice_session(
        session_id="cascade-session",
        user_id="account-delete",
        room_name="cascade-room",
        voice_backend="cascade",
        created_at=now,
    )
    store.add_voice_session(
        session_id="omni-session",
        user_id="account-delete",
        room_name="omni-room",
        voice_backend="qwen_omni",
        created_at=now,
    )
    registry = ConnectionRegistryStub()
    closed_rooms: list[str] = []

    async def close_room(room_name: str) -> None:
        closed_rooms.append(room_name)

    terminator = AccountSessionTerminator(
        store=store,
        connections=registry,
        close_room=close_room,
    )

    count = await terminator.terminate_account("account-delete")

    assert count == 2
    assert registry.closed == ["account-delete"]
    assert closed_rooms == ["cascade-room"]
    assert store.get_voice_session_by_id(session_id="cascade-session") is None
    assert store.get_voice_session_by_id(session_id="audio-session") is None


@pytest.mark.asyncio
async def test_livekit_room_closer_treats_missing_room_as_already_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingRoom:
        async def delete_room(self, request: object) -> None:
            del request
            raise api.ServerError(
                api.ServerErrorCode.NOT_FOUND,
                "requested room does not exist",
                status=404,
            )

    class FakeLiveKit:
        room = MissingRoom()

        async def __aenter__(self) -> FakeLiveKit:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(api, "LiveKitAPI", lambda *args: FakeLiveKit())
    closer = LiveKitRoomCloser(
        SimpleNamespace(
            offline_mock=False,
            livekit_url="wss://livekit.example.com",
            livekit_api_key="key",
            livekit_api_secret="secret",
        )  # type: ignore[arg-type]
    )

    await closer("stale-room")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [
        (api.ServerErrorCode.UNAVAILABLE, 503),
        (api.ServerErrorCode.NOT_FOUND, 500),
        (api.ServerErrorCode.UNAVAILABLE, 404),
    ],
)
async def test_livekit_room_closer_keeps_unexpected_errors_visible(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status: int,
) -> None:
    class BrokenRoom:
        async def delete_room(self, request: object) -> None:
            del request
            raise api.ServerError(
                code,
                "livekit unavailable",
                status=status,
            )

    class FakeLiveKit:
        room = BrokenRoom()

        async def __aenter__(self) -> FakeLiveKit:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(api, "LiveKitAPI", lambda *args: FakeLiveKit())
    closer = LiveKitRoomCloser(
        SimpleNamespace(
            offline_mock=False,
            livekit_url="wss://livekit.example.com",
            livekit_api_key="key",
            livekit_api_secret="secret",
        )  # type: ignore[arg-type]
    )

    with pytest.raises(api.ServerError):
        await closer("unavailable-room")
