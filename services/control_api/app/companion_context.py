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
    try:
        completed = await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=COMPANION_CONTEXT_BUDGET_S,
        )
    except TimeoutError:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        logger.info(
            "recall_timeout hit=false category=companion_context latency_ms=%.1f",
            (time.perf_counter() - started) * 1000.0,
        )
        return None
    return list(completed)
