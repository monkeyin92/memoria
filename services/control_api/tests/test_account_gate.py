from __future__ import annotations

import asyncio

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
