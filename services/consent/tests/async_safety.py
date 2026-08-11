"""Small bounded primitives for deterministic async integration tests."""

from __future__ import annotations

import asyncio


async def wait_for_event_or_task[T](
    event: asyncio.Event,
    task: asyncio.Task[T],
    *,
    timeout_seconds: float,
) -> None:
    """Wait for ``event`` while surfacing early task completion or failure."""
    event_waiter = asyncio.create_task(event.wait())
    try:
        done, _pending = await asyncio.wait(
            {event_waiter, task},
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise TimeoutError("awaited event was not reached before timeout")
        if task in done:
            await task
            raise AssertionError("worker task completed before awaited event")
        await event_waiter
    finally:
        if not event_waiter.done():
            event_waiter.cancel()
        await asyncio.gather(event_waiter, return_exceptions=True)


async def await_task_bounded[T](
    task: asyncio.Task[T], *, timeout_seconds: float
) -> T:
    """Await a task without letting timeout cancellation hide it from cleanup."""
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout_seconds)
    except TimeoutError as exc:
        raise TimeoutError("task did not finish before timeout") from exc


async def cancel_and_monitor(*tasks: asyncio.Task[object] | None) -> None:
    """Cancel pending tasks and retrieve every task result or exception."""
    present = tuple(task for task in tasks if task is not None)
    for task in present:
        if not task.done():
            task.cancel()
    if present:
        await asyncio.gather(*present, return_exceptions=True)


__all__ = ["await_task_bounded", "cancel_and_monitor", "wait_for_event_or_task"]
