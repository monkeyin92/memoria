from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

# The Agent abandons the whole request at 0.8 s.  This wait is only one stage
# of that request, so 0.45 s left too little room for session, scope, recall
# and payload once a long session slowed them down; the Agent then recorded a
# timeout as an invalid payload.  Keep this stage small enough to finish the
# rest of the route inside the same client budget.
COMPANION_CONTEXT_BUDGET_S = 0.25

logger = logging.getLogger(__name__)


async def gather_companion_context(
    memory_task: asyncio.Task[Any] | None,
    persona_task: asyncio.Task[Any] | None,
) -> list[Any] | None:
    pending = [task for task in (memory_task, persona_task) if task is not None]
    if not pending:
        return []
    started = time.perf_counter()
    done, late = await asyncio.wait(set(pending), timeout=COMPANION_CONTEXT_BUDGET_S)
    if late:
        for task in late:
            task.cancel()
        await asyncio.gather(*late, return_exceptions=True)
        logger.info(
            "recall_timeout hit=false category=companion_context latency_ms=%.1f",
            (time.perf_counter() - started) * 1000.0,
        )

    def ready(task: asyncio.Task[Any]) -> bool:
        return task in done and not task.cancelled()

    # A late persona read must not throw away memory that already finished.
    # Memory missing the deadline still drops the stage, persona included.
    if memory_task is not None and not ready(memory_task):
        return None
    if not any(ready(task) for task in pending):
        return None
    return [_task_outcome(task) if ready(task) else TimeoutError() for task in pending]


def _task_outcome(task: asyncio.Task[Any]) -> Any:
    exc = task.exception()
    return exc if exc is not None else task.result()
