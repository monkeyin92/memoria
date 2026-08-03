from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from services.control_api.app.session_directory import (
    InMemorySessionDirectory,
    RedisSessionDirectory,
    SessionDirectoryUnavailable,
    SessionDraining,
    SessionEpochConflict,
    SessionNotFound,
)


@pytest.mark.asyncio
async def test_session_directory_reconnect_bumps_epoch_and_expiry_is_fail_closed() -> None:
    clock = [datetime(2026, 8, 2, tzinfo=UTC)]
    directory = InMemorySessionDirectory(ttl_s=10, now=lambda: clock[0])

    claimed = await directory.claim(
        "session-1",
        media_edge_id="edge-a",
        voice_core_id="core-a",
        device_id="device-1",
        account_id="account-1",
        generation=4,
    )
    assert claimed.stream_epoch == 1
    assert claimed.generation_id == 4

    reconnected = await directory.reconnect("session-1", media_edge_id="edge-b")
    assert reconnected.stream_epoch == 2
    assert reconnected.media_edge_id == "edge-b"
    assert reconnected.account_id == "account-1"
    with pytest.raises(SessionEpochConflict):
        await directory.reconnect("session-1", expected_stream_epoch=1)
    renewed = await directory.renew("session-1", expected_stream_epoch=2, ttl_s=20)
    assert renewed.stream_epoch == 2
    assert renewed.expires_at == clock[0] + timedelta(seconds=20)

    clock[0] += timedelta(seconds=21)
    assert await directory.lookup("session-1") is None
    with pytest.raises(SessionNotFound):
        await directory.reconnect("session-1")


@pytest.mark.asyncio
async def test_session_directory_advance_generation_is_cas_fenced() -> None:
    directory = InMemorySessionDirectory()
    claimed = await directory.claim(
        "cancel-session",
        media_edge_id="edge-a",
        voice_core_id="core-a",
        device_id="device-1",
        account_id="account-1",
        generation=2,
    )
    cancelled = await directory.advance_generation(
        "cancel-session",
        expected_stream_epoch=claimed.stream_epoch,
    )
    assert cancelled.generation_id == 3
    with pytest.raises(SessionEpochConflict):
        await directory.advance_generation(
            "cancel-session",
            expected_stream_epoch=claimed.stream_epoch - 1,
        )


@pytest.mark.asyncio
async def test_session_directory_fallback_to_livekit_preserves_epoch_and_is_cas_fenced() -> None:
    directory = InMemorySessionDirectory()
    claimed = await directory.claim(
        "fallback-session",
        media_edge_id="edge-a",
        voice_core_id="core-a",
        device_id="h5",
        account_id="account-1",
        media_runtime="streamcore",
    )
    fallback = await directory.fallback_to_livekit(
        "fallback-session",
        expected_stream_epoch=claimed.stream_epoch,
    )
    assert fallback.media_runtime == "livekit"
    assert fallback.stream_epoch == claimed.stream_epoch
    assert fallback.generation_id == claimed.generation_id
    with pytest.raises(SessionEpochConflict):
        await directory.fallback_to_livekit(
            "fallback-session",
            expected_stream_epoch=claimed.stream_epoch + 1,
        )


@pytest.mark.asyncio
async def test_session_directory_concurrent_generation_bumps_do_not_collapse() -> None:
    directory = InMemorySessionDirectory()
    await directory.claim(
        "concurrent-cancel",
        media_edge_id="edge-a",
        voice_core_id="core-a",
        device_id="device-1",
        account_id="account-1",
    )
    outcomes = await asyncio.gather(
        directory.advance_generation("concurrent-cancel"),
        directory.advance_generation("concurrent-cancel"),
        return_exceptions=True,
    )
    assert sorted(
        getattr(value, "generation_id", -1)
        for value in outcomes
        if not isinstance(value, BaseException)
    ) == [1, 2]
    route = await directory.lookup("concurrent-cancel")
    assert route is not None and route.generation_id == 2


@pytest.mark.asyncio
async def test_drain_is_visible_and_blocks_reconnect() -> None:
    directory = InMemorySessionDirectory()
    await directory.claim(
        "session-1",
        media_edge_id="edge-a",
        voice_core_id="core-a",
        device_id="device-1",
        account_id="account-1",
    )
    route = await directory.drain("session-1")
    assert route is not None and route.state == "draining"
    assert (await directory.lookup("session-1")).state == "draining"  # type: ignore[union-attr]
    with pytest.raises(SessionDraining):
        await directory.reconnect("session-1")
    assert await directory.expire("session-1") is True


class _UnavailableRedis:
    async def get(self, _key: str) -> None:
        raise ConnectionError("redis down")


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int, nx: bool = False) -> bool:
        _ = ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(
        self,
        _script: str,
        _numkeys: int,
        key: str,
        expected_epoch: int,
        replacement: str,
        _seconds: int,
    ) -> int:
        current_raw = self.values.get(key)
        if current_raw is None:
            return -1
        current = json.loads(current_raw)
        if current.get("state") == "draining":
            return -3
        if int(current.get("stream_epoch", 0)) != int(expected_epoch):
            return -2
        self.values[key] = replacement
        return 1

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)


@pytest.mark.asyncio
async def test_redis_backend_never_falls_back_to_memory() -> None:
    directory = RedisSessionDirectory("redis://unused", redis_client=_UnavailableRedis())
    with pytest.raises(SessionDirectoryUnavailable):
        await directory.lookup("session-1")


@pytest.mark.asyncio
async def test_redis_backend_uses_epoch_compare_and_set_for_reconnect_and_renew() -> None:
    directory = RedisSessionDirectory("redis://unused", redis_client=_FakeRedis())
    claimed = await directory.claim(
        "session-1",
        media_edge_id="edge-a",
        voice_core_id="core-a",
        device_id="device-1",
        account_id="account-1",
    )
    assert claimed.stream_epoch == 1
    reconnected = await directory.reconnect("session-1", media_edge_id="edge-b")
    assert reconnected.stream_epoch == 2
    with pytest.raises(SessionEpochConflict):
        await directory.reconnect("session-1", expected_stream_epoch=1)
    renewed = await directory.renew("session-1", expected_stream_epoch=2)
    assert renewed.stream_epoch == 2
