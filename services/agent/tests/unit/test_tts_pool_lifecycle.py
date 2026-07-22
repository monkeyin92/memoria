from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from services.agent.src.providers import doubao_tts
from services.agent.src.providers.cosyvoice_tts import (
    CosyVoiceConfig,
    CosyVoicePool,
)
from services.agent.src.providers.cosyvoice_tts import (
    PooledConnection as CosyVoiceConnection,
)
from services.agent.src.providers.doubao_tts import (
    DoubaoTTSConfig,
    DoubaoTTSPool,
)
from services.agent.src.providers.doubao_tts import (
    PooledConnection as DoubaoConnection,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def send(self, _payload: object) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def _doubao_pool() -> tuple[DoubaoTTSPool, DoubaoConnection]:
    ws = FakeWebSocket()
    pool = DoubaoTTSPool(
        DoubaoTTSConfig(
            api_key="test",
            speaker="zh_male_yangguangqingnian_uranus_bigtts",
            pool_size=1,
        )
    )
    return pool, DoubaoConnection(ws=ws, conn_id="doubao-test")  # type: ignore[arg-type]


def _cosyvoice_pool() -> tuple[CosyVoicePool, CosyVoiceConnection]:
    ws = FakeWebSocket()
    pool = CosyVoicePool(CosyVoiceConfig(api_key="test", ws_url="wss://example.test", pool_size=1))
    return pool, CosyVoiceConnection(ws=ws, conn_id="cosyvoice-test")  # type: ignore[arg-type]


_POOL_FACTORIES: tuple[Callable[[], tuple[Any, Any]], ...] = (
    _doubao_pool,
    _cosyvoice_pool,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_factory", _POOL_FACTORIES)
async def test_cancel_discard_does_not_wait_for_replacement_handshake(
    pool_factory: Callable[[], tuple[Any, Any]],
) -> None:
    pool, conn = pool_factory()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def refill() -> None:
        entered.set()
        await release.wait()

    pool._refill_one = refill
    pool._all[conn.conn_id] = conn
    if hasattr(conn, "session_id"):
        conn.session_id = "session-cancel"

    discard = asyncio.create_task(pool.discard(conn, reason="cancel"))
    await entered.wait()
    await asyncio.sleep(0)
    finished_without_refill = discard.done()
    release.set()
    await discard
    await pool.aclose()

    assert finished_without_refill


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_factory", _POOL_FACTORIES)
async def test_pool_close_waits_for_inflight_acquire_open(
    pool_factory: Callable[[], tuple[Any, Any]],
) -> None:
    pool, conn = pool_factory()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_open() -> Any:
        entered.set()
        await release.wait()
        pool._all[conn.conn_id] = conn
        return conn

    pool._open = slow_open
    acquire = asyncio.create_task(pool.acquire(wait_s=0))
    await entered.wait()
    close = asyncio.create_task(pool.aclose())
    await asyncio.sleep(0)
    waited_for_open = not close.done()
    release.set()
    await asyncio.gather(acquire, close)

    assert waited_for_open
    assert not pool._all
    assert pool.available_approx == 0
    assert conn.ws.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_factory", _POOL_FACTORIES)
async def test_timed_out_acquire_rechecks_queue_before_opening(
    pool_factory: Callable[[], tuple[Any, Any]],
) -> None:
    pool, conn = pool_factory()
    replacement = replace(conn, ws=FakeWebSocket(), conn_id=f"{conn.conn_id}-replacement")

    async def unexpected_open() -> Any:
        raise AssertionError("acquire opened a duplicate connection")

    pool._open = unexpected_open
    pool._all[replacement.conn_id] = replacement
    async with pool._lock:
        acquire = asyncio.create_task(pool.acquire(wait_s=0.001))
        await asyncio.sleep(0.01)
        await pool._available.put(replacement)

    acquired = await acquire
    await pool.release(acquired)
    await pool.aclose()

    assert acquired is replacement


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_factory", _POOL_FACTORIES)
async def test_pool_close_waits_for_inflight_warm_open(
    pool_factory: Callable[[], tuple[Any, Any]],
) -> None:
    pool, conn = pool_factory()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_open() -> Any:
        entered.set()
        await release.wait()
        pool._all[conn.conn_id] = conn
        return conn

    pool._open = slow_open
    warm = asyncio.create_task(pool.warm(1))
    await entered.wait()
    close = asyncio.create_task(pool.aclose())
    await asyncio.sleep(0)
    waited_for_open = not close.done()
    release.set()
    await asyncio.gather(warm, close)

    assert waited_for_open
    assert not pool._all
    assert pool.available_approx == 0
    assert conn.ws.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_factory", _POOL_FACTORIES)
async def test_pool_close_cancels_and_awaits_background_refill(
    pool_factory: Callable[[], tuple[Any, Any]],
) -> None:
    pool, conn = pool_factory()
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def refill() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    pool._refill_one = refill
    pool._all[conn.conn_id] = conn
    await pool.discard(conn, reason="cancel")
    await entered.wait()

    await pool.aclose()

    assert finished.is_set()
    assert not pool._refill_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_factory", _POOL_FACTORIES)
async def test_burst_connection_is_closed_instead_of_becoming_idle(
    pool_factory: Callable[[], tuple[Any, Any]],
) -> None:
    pool, pooled = pool_factory()
    burst = replace(pooled, ws=FakeWebSocket(), conn_id=f"{pooled.conn_id}-burst")
    pool._all[pooled.conn_id] = pooled
    await pool._available.put(pooled)
    held = await pool.acquire(wait_s=0)

    async def open_burst() -> Any:
        pool._all[burst.conn_id] = burst
        return burst

    pool._open = open_burst
    acquired_burst = await pool.acquire(wait_s=0)
    await pool.release(acquired_burst)

    assert acquired_burst is burst
    assert burst.ws.closed
    assert burst.conn_id not in pool._all
    assert pool.available_approx == 0

    await pool.release(held)
    assert pool.available_approx == 1
    await pool.aclose()


@pytest.mark.asyncio
async def test_doubao_cancelled_open_closes_connected_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    class HangingWebSocket(FakeWebSocket):
        async def recv(self) -> bytes:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    ws = HangingWebSocket()

    async def connect(*_args: object, **_kwargs: object) -> HangingWebSocket:
        return ws

    monkeypatch.setattr(doubao_tts.websockets, "connect", connect)
    pool, _ = _doubao_pool()
    warm = asyncio.create_task(pool.warm(1))
    await entered.wait()
    warm.cancel()

    with pytest.raises(asyncio.CancelledError):
        await warm

    assert ws.closed
    await pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_factory", _POOL_FACTORIES)
async def test_pool_close_serializes_with_release_enqueue(
    pool_factory: Callable[[], tuple[Any, Any]],
) -> None:
    pool, conn = pool_factory()
    entered = asyncio.Event()
    allow_put = asyncio.Event()
    original_put = pool._available.put

    async def delayed_put(item: Any) -> None:
        entered.set()
        await allow_put.wait()
        await original_put(item)

    pool._available.put = delayed_put
    pool._all[conn.conn_id] = conn
    conn.in_use = True
    release = asyncio.create_task(pool.release(conn))
    await entered.wait()
    close = asyncio.create_task(pool.aclose())
    await asyncio.sleep(0)
    close_waited_for_release = not close.done()
    allow_put.set()
    await asyncio.gather(release, close)

    assert close_waited_for_release
    assert pool.available_approx == 0
    assert conn.ws.closed
