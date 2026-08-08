from __future__ import annotations

import asyncio
from threading import Event, Thread

import pytest
from services.control_api.app.account_gate import AccountDeletingError, AccountOperationGate


@pytest.mark.asyncio
async def test_deletion_gate_drains_existing_writes_and_rejects_new_ones() -> None:
    gate = AccountOperationGate()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def existing_write() -> None:
        async with gate.write("account-a"):
            entered.set()
            await release.wait()

    writer = asyncio.create_task(existing_write())
    await entered.wait()
    blocker = asyncio.create_task(gate.block_account("account-a"))
    await asyncio.sleep(0)
    assert blocker.done() is False

    release.set()
    await writer
    await blocker

    with pytest.raises(AccountDeletingError):
        async with gate.write("account-a"):
            pass


@pytest.mark.asyncio
async def test_deletion_gate_drains_sync_worker_writes() -> None:
    gate = AccountOperationGate()
    entered = Event()
    release = Event()

    def existing_worker_write() -> None:
        with gate.sync_write("account-sync"):
            entered.set()
            release.wait(timeout=5)

    worker = Thread(target=existing_worker_write)
    worker.start()
    assert entered.wait(timeout=5)
    blocker = asyncio.create_task(gate.block_account("account-sync"))
    await asyncio.sleep(0)
    assert blocker.done() is False

    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    await blocker

    with pytest.raises(AccountDeletingError):
        with gate.sync_write("account-sync"):
            pass


@pytest.mark.asyncio
async def test_deletion_gate_drains_privacy_sensitive_reads() -> None:
    gate = AccountOperationGate()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def existing_read() -> None:
        async with gate.read("account-read"):
            entered.set()
            await release.wait()

    reader = asyncio.create_task(existing_read())
    await entered.wait()
    blocker = asyncio.create_task(gate.block_account("account-read"))
    await asyncio.sleep(0)
    assert blocker.done() is False

    release.set()
    await reader
    await blocker

    with pytest.raises(AccountDeletingError):
        with gate.sync_read("account-read"):
            pass
