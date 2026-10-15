"""Defense in depth: fence_mismatch fallback still speaks fixed crisis/support replies."""

from __future__ import annotations

from typing import Any

import pytest
from services.agent.src import response_plan_sidecar
from services.agent.src.contracts.ids import CancellationContext
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.llm_types import StopResponse
from services.agent.src.orchestration.handlers import LanguageModelRequest
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import ResponsePlanFetch
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import ScriptedChatModel, bind_owner_speaker
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    FixedSafetyScript,
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
        ("我不想活了", CRISIS_SELF_SCRIPT),
        ("有人正在打我", SUPPORT_VICTIM_SCRIPT),
        ("有人在打我", SUPPORT_VICTIM_SCRIPT),
        ("我朋友说他不想活了", SUPPORT_RELAY_SCRIPT),
        ("我不想活了。", CRISIS_SELF_SCRIPT),
        ("我想伤害自己。", CRISIS_SELF_SCRIPT),
        ("我不想或了。", CRISIS_SELF_SCRIPT),
        ("有个大人老是打我，还不让我告诉妈妈。", SUPPORT_VICTIM_SCRIPT),
        ("有人摸我身体，我不喜欢。", SUPPORT_VICTIM_SCRIPT),
        ("有个叔叔老是大我。", SUPPORT_VICTIM_SCRIPT),
        ("我同桌说她爸爸打她，我该怎么帮她？", SUPPORT_RELAY_SCRIPT),
        ("我朋友说有人欺负她。", SUPPORT_RELAY_SCRIPT),
        ("我同学被人打了。", SUPPORT_RELAY_SCRIPT),
    ],
)
async def test_fence_mismatch_fallback_still_speaks_fixed_safety_replies(
    query: str, expected: FixedSafetyScript
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
    assert spoken == [expected]
    await runtime.close()


class _InvalidPayloadPlanner:
    echoes_session_epoch = True

    async def fetch(self, **kwargs: Any) -> ResponsePlanFetch:
        return ResponsePlanFetch(None, "request_or_payload_invalid")


@pytest.mark.asyncio
async def test_after_fixed_safety_local_safe_fallback_does_not_speak_chat(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Product rule: after a fixed safety script the server must not speak again
    on its own. An echo/invalid follow-up that hits local-safe LLM fallback is
    suppressed. The child's speech is still accepted into prepare; only the
    proactive invented reply is stopped.
    """

    import logging

    runtime = _runtime("post-safety-suppress")
    pipeline = _pipeline(runtime, _InvalidPayloadPlanner())
    first = await pipeline.prepare_committed_turn("我想伤害自己。")
    spoken_safety = [
        token
        async for token in pipeline.stream(
            LanguageModelRequest(
                user_text="我想伤害自己。",
                cancellation=CancellationContext.capture(first),
            )
        )
    ]
    assert spoken_safety == [CRISIS_SELF_SCRIPT]
    assert runtime.fixed_safety_heard is True

    with caplog.at_level(logging.INFO):
        with pytest.raises(StopResponse):
            await pipeline.prepare_committed_turn("嗯嗯好的大人陪着你")
    assert any(
        "post_fixed_safety_fallback_suppressed" in record.getMessage()
        for record in caplog.records
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_after_fixed_safety_a_new_safety_hit_still_speaks_fixed_script() -> None:
    runtime = _runtime("post-safety-retrigger")
    pipeline = _pipeline(runtime, _InvalidPayloadPlanner())
    first = await pipeline.prepare_committed_turn("我想伤害自己。")
    async for _ in pipeline.stream(
        LanguageModelRequest(
            user_text="我想伤害自己。",
            cancellation=CancellationContext.capture(first),
        )
    ):
        pass
    assert runtime.fixed_safety_heard is True

    second = await pipeline.prepare_committed_turn("有个叔叔老是大我。")
    spoken = [
        token
        async for token in pipeline.stream(
            LanguageModelRequest(
                user_text="有个叔叔老是大我。",
                cancellation=CancellationContext.capture(second),
            )
        )
    ]
    assert spoken == [SUPPORT_VICTIM_SCRIPT]
    await runtime.close()
