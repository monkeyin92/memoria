"""Defense in depth: fence_mismatch fallback still speaks fixed crisis/support replies."""

from __future__ import annotations

from typing import Any

import pytest
from services.agent.src import response_plan_sidecar
from services.agent.src.contracts.ids import CancellationContext
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.handlers import LanguageModelRequest
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import ResponsePlanFetch
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import ScriptedChatModel, bind_owner_speaker
from services.common.companion_response_safety import (
    CRISIS_SUPPORT_REPLY,
    SUPPORT_RELAY_REPLY,
    SUPPORT_VICTIM_REPLY,
)


class _MismatchPlanner:
    echoes_session_epoch = False

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def fetch(self, **kwargs: Any) -> ResponsePlanFetch:
        self.calls.append(kwargs)
        return ResponsePlanFetch(None, "fence_mismatch")


def _runtime(name: str) -> DuplexRuntime:
    runtime = DuplexRuntime.create(session_id=name)
    bind_owner_policy(
        runtime,
        policy_version="test-policy",
        private_context=True,
        owner_evidence=False,
        tools=False,
        voice_profile=False,
        memory_recall_grant=True,
    )
    bind_owner_speaker(runtime)
    return runtime


def _pipeline(runtime: DuplexRuntime, planner: _MismatchPlanner) -> ReplyPipeline:
    async def model(_safe_ctx: Any, _tools: list[Any]) -> Any:
        async def one() -> Any:
            yield "模型不该开口。"

        return one()

    pipeline = ReplyPipeline(
        instructions="系统提示",
        runtime=runtime,
        response_planner_client=planner,  # type: ignore[arg-type]
    )
    pipeline.language_model = ScriptedChatModel(model)
    return pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("我不想活了", CRISIS_SUPPORT_REPLY),
        ("有人正在打我", SUPPORT_VICTIM_REPLY),
        ("有人在打我", SUPPORT_VICTIM_REPLY),
        ("我朋友说他不想活了", SUPPORT_RELAY_REPLY),
    ],
)
async def test_fence_mismatch_fallback_still_speaks_fixed_safety_replies(
    query: str, expected: str
) -> None:
    runtime = _runtime("safety-fallback")
    planner = _MismatchPlanner()
    # Force the awaited mismatch path (not sidecar) so prepare_turn builds a local plan.
    assert response_plan_sidecar.applies(planner, runtime, runtime.fence)
    pipeline = _pipeline(runtime, planner)
    fence = await pipeline.prepare_committed_turn(query)
    spoken = [
        token
        async for token in pipeline.stream(
            LanguageModelRequest(
                user_text=query,
                cancellation=CancellationContext.capture(fence),
            )
        )
    ]
    assert "".join(spoken) == expected
    assert "模型不该开口" not in "".join(spoken)
    await runtime.close()
