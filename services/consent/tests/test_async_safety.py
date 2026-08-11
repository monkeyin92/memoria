"""Deterministic bounded orchestration for live PostgreSQL concurrency tests."""

from __future__ import annotations

import asyncio

import pytest
from services.consent.tests.async_safety import (
    await_task_bounded,
    cancel_and_monitor,
    wait_for_event_or_task,
)


@pytest.mark.asyncio
async def test_event_wait_propagates_worker_exception() -> None:
    event = asyncio.Event()

    async def fail() -> None:
        raise RuntimeError("worker failed")

    task = asyncio.create_task(fail())
    with pytest.raises(RuntimeError, match="worker failed"):
        await wait_for_event_or_task(event, task, timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_event_wait_rejects_worker_success_before_barrier() -> None:
    event = asyncio.Event()

    async def finish() -> str:
        return "too-early"

    task = asyncio.create_task(finish())
    with pytest.raises(AssertionError, match="before awaited event"):
        await wait_for_event_or_task(event, task, timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_bounded_task_wait_times_out_without_orphaning_task() -> None:
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def wait_forever() -> None:
        try:
            await release.wait()
        finally:
            cleaned.set()

    task = asyncio.create_task(wait_forever())
    try:
        with pytest.raises(TimeoutError, match="task did not finish"):
            await await_task_bounded(task, timeout_seconds=0.01)
    finally:
        await cancel_and_monitor(task)
    assert cleaned.is_set()
