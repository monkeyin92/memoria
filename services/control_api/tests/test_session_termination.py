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
        self.closed_sessions: list[set[str]] = []

    async def close_account(self, account_id: str) -> int:
        self.closed.append(account_id)
        return 1

    async def close_sessions(self, session_ids: set[str]) -> int:
        self.closed_sessions.append(session_ids)
        return len(session_ids)


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
async def test_realtime_registry_can_close_one_related_legacy_session_only() -> None:
    registry = RealtimeConnectionRegistry()
    legacy = SocketStub()
    unrelated = SocketStub()
    await registry.register(
        account_id="legacy-grantee",
        session_id="legacy-owner-session",
        connection=legacy,
    )
    await registry.register(
        account_id="legacy-grantee",
        session_id="other-owner-session",
        connection=unrelated,
    )

    count = await registry.close_sessions({"legacy-owner-session"})

    assert count == 1
    assert legacy.closed == [(4401, "account deleting")]
    assert unrelated.closed == []


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
    frozen_companion = {
        "interaction_mode": "companion",
        "mode_policy_version": "s2-v1",
        "digital_self_version_id": None,
        "relationship_profile_id": None,
        "legacy_grant_id": None,
        "companion_style_id": "starlight",
        "companion_style_version": "companion-v1",
    }
    store.add_voice_session(
        session_id="cascade-session",
        user_id="account-delete",
        room_name="cascade-room",
        voice_backend="cascade",
        created_at=now,
        **frozen_companion,
    )
    store.add_voice_session(
        session_id="omni-session",
        user_id="account-delete",
        room_name="omni-room",
        voice_backend="qwen_omni",
        created_at=now,
        **frozen_companion,
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
    assert registry.closed_sessions == [{"cascade-session", "omni-session"}]
    assert closed_rooms == ["cascade-room"]
    assert store.get_voice_session_by_id(session_id="cascade-session") is None
    assert store.get_voice_session_by_id(session_id="audio-session") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deleted_account", "closed_session_ids", "remaining_session_id"),
    (
        (
            "legacy-owner-a",
            {"legacy-owner-a-grantee-a", "legacy-owner-a-grantee-b"},
            "legacy-owner-b-grantee-a",
        ),
        (
            "legacy-grantee-a",
            {"legacy-owner-a-grantee-a", "legacy-owner-b-grantee-a"},
            "legacy-owner-a-grantee-b",
        ),
    ),
)
async def test_account_session_termination_closes_exact_legacy_sessions_for_either_role(
    tmp_path: Path,
    deleted_account: str,
    closed_session_ids: set[str],
    remaining_session_id: str,
) -> None:
    store = MemoryStore(str(tmp_path / "memoria.sqlite3"))
    now = datetime.now(UTC).isoformat()
    for account_id in (
        "legacy-owner-a",
        "legacy-owner-b",
        "legacy-grantee-a",
        "legacy-grantee-b",
    ):
        store.register_account(
            user_id=account_id,
            username=account_id,
            username_normalized=account_id,
            password_hash=hash_password("safe-passphrase"),
            now=now,
        )
    session_owners = {
        "legacy-owner-a-grantee-a": ("legacy-owner-a", "legacy-grantee-a"),
        "legacy-owner-b-grantee-a": ("legacy-owner-b", "legacy-grantee-a"),
        "legacy-owner-a-grantee-b": ("legacy-owner-a", "legacy-grantee-b"),
    }
    registry = RealtimeConnectionRegistry()
    sockets: dict[str, SocketStub] = {}
    for session_id, (owner_account_id, grantee_account_id) in session_owners.items():
        store.add_voice_session(
            session_id=session_id,
            user_id=grantee_account_id,
            resource_owner_account_id=owner_account_id,
            room_name=f"{session_id}-room",
            voice_backend="cascade",
            created_at=now,
            interaction_mode="legacy",
            mode_policy_version="legacy-v1",
            digital_self_version_id=f"{owner_account_id}-version",
            digital_self_manifest_sha256="a" * 64,
            relationship_profile_id=f"{owner_account_id}-relationship",
            relationship_profile_version=1,
            legacy_grant_id=f"{session_id}-grant",
            legacy_actor_role="grantee",
            legacy_grantee_account_id=grantee_account_id,
            legacy_shell_id=f"{session_id}-shell",
            legacy_grant_snapshot_sha256="b" * 64,
            legacy_scope_sha256="c" * 64,
            legacy_voice_allowed=False,
            legacy_expires_at="2099-01-01T00:00:00+00:00",
        )
        socket = SocketStub()
        sockets[session_id] = socket
        await registry.register(
            account_id=grantee_account_id,
            session_id=session_id,
            connection=socket,
        )
    closed_rooms: list[str] = []

    async def close_room(room_name: str) -> None:
        closed_rooms.append(room_name)

    terminator = AccountSessionTerminator(
        store=store,
        connections=registry,
        close_room=close_room,
    )

    count = await terminator.terminate_account(deleted_account)

    assert count == 2
    assert {
        session_id for session_id, socket in sockets.items() if socket.closed
    } == closed_session_ids
    assert closed_rooms == [f"{session_id}-room" for session_id in sorted(closed_session_ids)]
    assert all(
        store.get_voice_session_by_id(session_id=session_id) is None
        for session_id in closed_session_ids
    )
    assert store.get_voice_session_by_id(session_id=remaining_session_id) is not None


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
