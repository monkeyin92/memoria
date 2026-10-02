"""The response-plan request of a turn whose plan the client can never apply, sent without waiting for it.

``ResponsePlannerClient`` rebuilds the plan's fence from the wire, and the wire carries turn, generation and
tool epoch only.  A device fence has ``session_epoch >= 1``, so ``plan.fence.matches(fence)`` cannot hold and
every answer ends as ``fence_mismatch`` (round 10, 2026-10-02: 28 of 38 device turns; the other 10 hit the
client's 0.8 s timeout).  The reply used to wait for that answer anyway, p50 0.81 s, and then answered with the
local safe plan.

The request is still made, once per turn, because of what the control route does with it before it builds
anything: crisis routing and the guardian's notification (``record_minor_crisis``), and the tutor turn
accounting.  Those run server side whenever the request arrives, whether or not this client stays to hear the
answer (a plain async route is not cancelled by the caller going away), so sending it from a task nobody
awaits keeps them and no longer holds the reply.  The task gets a wider bound than the old 0.8 s timeout, so
fewer requests are cut off by this client, and it never retries.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.response_planner_client import ResponsePlanFetch

logger = logging.getLogger(__name__)

SIDECAR_TIMEOUT_S = 3.0
SIDECAR_TASK_NAME = "response-plan-sidecar"


def applies(client: object, runtime: Any, fence: GenerationFence) -> bool:
    """True when the request is made but the reply need not wait for it.

    Only a client that says it cannot echo the epoch, a fence that carries one, and a profile that grants
    long-term memory recall (without it no request was ever made) qualify; everything else keeps the wait.
    """

    return (
        client is not None
        and getattr(client, "echoes_session_epoch", True) is False
        and fence.session_epoch >= 1
        and bool(runtime.profile_permits(fence, capability="memory_recall_private"))
    )


def spawn(
    runtime: Any,
    client: Any,
    *,
    session_id: str,
    text: str,
    fence: GenerationFence,
    speaker: Any,
    recall_context: Sequence[str],
    utterance_intent: str,
) -> ResponsePlanFetch:
    """Send the request from a tracked background task; return the outcome the reply proceeds with."""

    runtime._spawn(
        _send(
            client,
            session_id=session_id,
            text=text,
            fence=fence,
            speaker=speaker,
            recall_context=tuple(recall_context),
            utterance_intent=utterance_intent,
        ),
        name=SIDECAR_TASK_NAME,
    )
    # What the awaited request would have ended as, so the turn takes the same local safe plan.
    return ResponsePlanFetch(None, "fence_mismatch")


async def _send(
    client: Any,
    *,
    session_id: str,
    text: str,
    fence: GenerationFence,
    speaker: Any,
    recall_context: tuple[str, ...],
    utterance_intent: str,
) -> None:
    """One request, one log line, no retry; nothing it returns or raises reaches the turn."""

    try:
        async with asyncio.timeout(SIDECAR_TIMEOUT_S):
            fetched = await client.fetch(
                session_id=session_id,
                query=text,
                fence=fence,
                speaker_decision=speaker,
                recall_context=recall_context,
                utterance_intent=utterance_intent,
                timeout_s=SIDECAR_TIMEOUT_S,
            )
        outcome = str(getattr(fetched, "reason", "unknown"))
    except TimeoutError:
        outcome = "timeout"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        outcome = f"error:{type(exc).__name__}"
    logger.info(
        "response plan sidecar session_id=%s turn_id=%s generation_id=%s outcome=%s",
        session_id,
        fence.turn_id,
        fence.generation_id,
        outcome,
    )
