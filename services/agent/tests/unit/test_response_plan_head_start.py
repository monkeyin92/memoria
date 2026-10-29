"""The response-plan request of a turn goes out before the turn is committed, and the commit takes it.

2026-10-28 bench, turn 2: the request (0.77 s) left only after the close verdict (0.26 s), the live-lookup
verdict (0.19 s) and two profile refreshes (0.14 s), all on the child's wait (``prepare_ms`` p50 983).  The
head start runs it beside them.  These pin that it does, and that the commit uses an answer only to the
request it would have sent itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from services.agent.src import reply_pipeline
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.llm_types import StopResponse
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import (
    ResponsePlan,
    ResponsePlanFetch,
    ResponseProvenance,
    ResponseVoiceTarget,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import bind_owner_speaker

QUESTION = "你还记得我今天画的什么吗"
EARLIER = "我今天画了一只黄色的小狗"


def _plan(fence: GenerationFence, query: str) -> ResponsePlan:
    return ResponsePlan(
        fence=fence,
        instructions=f"回答：{query}",
        direct_text=None,
        epistemic_status="not_applicable",
        epistemic_reason_codes=("test",),
        grounded_items=(),
        disclosures=(),
        voice_target=ResponseVoiceTarget(
            kind="companion", profile_id="warm_companion", model="seed-tts-2.0"
        ),
        provenance=ResponseProvenance(
            planner_policy_version="digital-self-response-planner-v2",
            interaction_mode="companion",
            mode_policy_version="test-policy",
            digital_self_version_id=None,
            manifest_sha256=None,
            persona_version_id=None,
            persona_version_number=None,
            persona_style_only=False,
            relationship_profile_id=None,
            relationship_profile_version=None,
            speaker_class="owner",
            speaker_reason_code="authenticated_text_input",
            speaker_profile_id=None,
            speaker_model_version="account-auth-v1",
            speaker_template_version=None,
            source_refs=(),
            epistemic_status="not_applicable",
            epistemic_reason_codes=("test",),
            disclosures=(),
        ),
    )


class _Planner:
    """Answers every request with a plan for the fence and sentence it was asked about."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.release: asyncio.Event | None = None

    async def fetch(self, **kwargs: Any) -> ResponsePlanFetch:
        task = asyncio.current_task()
        self.requests.append({**kwargs, "task": task.get_name() if task else ""})
        if self.release is not None:
            await self.release.wait()
        fence = kwargs["fence"]
        assert isinstance(fence, GenerationFence)
        return ResponsePlanFetch(_plan(fence, str(kwargs["query"])), "ok")


def _runtime(name: str) -> DuplexRuntime:
    runtime = DuplexRuntime.create(session_id=name)
    bind_owner_policy(runtime, owner_evidence=False, tools=False, voice_profile=False)
    bind_owner_speaker(runtime)
    return runtime


def _pipeline(runtime: DuplexRuntime, planner: _Planner) -> ReplyPipeline:
    return ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=planner,  # type: ignore[arg-type]
    )


async def _let_tasks_run() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[str]:
    prefix = "response plan head start session_id="
    return [
        record.getMessage().rsplit("outcome=", 1)[1]
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]


@pytest.mark.asyncio
async def test_the_request_sent_before_the_commit_is_the_one_the_commit_uses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.reply_pipeline")
    runtime = _runtime("plan-head-start-used")
    planner = _Planner()
    pipeline = _pipeline(runtime, planner)
    await pipeline.prepare_committed_turn(EARLIER)
    await _let_tasks_run()
    caplog.clear()

    pipeline.start_committed_turn_plan(QUESTION)
    await _let_tasks_run()
    assert [r["query"] for r in planner.requests[1:]] == [QUESTION]  # out before the commit began
    fence = await pipeline.prepare_committed_turn(QUESTION)

    asked = planner.requests[1:]
    assert len(asked) == 1, "one request, sent before the commit and used by it"
    assert asked[0]["task"] == reply_pipeline.EARLY_RESPONSE_PLAN_TASK_NAME
    assert asked[0]["fence"] == fence
    assert asked[0]["recall_context"] == (EARLIER,)
    plan = pipeline.response_plans.get(fence)
    assert plan is not None and plan.instructions == f"回答：{QUESTION}"
    assert _outcomes(caplog) == ["used"]
    assert QUESTION not in caplog.text, "the sentence is never logged"
    await runtime.close()


@pytest.mark.asyncio
async def test_the_request_runs_while_the_commit_still_waits_for_its_verdicts() -> None:
    runtime = _runtime("plan-head-start-overlaps")
    verdict = asyncio.Event()

    async def live_lookup(_text: str) -> bool:
        await verdict.wait()
        return False

    runtime.set_live_lookup_semantic_resolver(live_lookup)
    planner = _Planner()
    pipeline = _pipeline(runtime, planner)

    prepare = asyncio.create_task(pipeline.prepare_committed_turn(QUESTION))
    await _let_tasks_run()

    assert [r["query"] for r in planner.requests] == [QUESTION]
    assert not prepare.done()
    verdict.set()
    fence = await prepare
    assert len(planner.requests) == 1 and planner.requests[0]["fence"] == fence
    await runtime.close()


@pytest.mark.asyncio
async def test_a_head_start_for_another_sentence_is_not_used() -> None:
    runtime = _runtime("plan-head-start-other-sentence")
    planner = _Planner()
    pipeline = _pipeline(runtime, planner)

    pipeline.start_committed_turn_plan("你还记得我吗")
    await _let_tasks_run()
    fence = await pipeline.prepare_committed_turn(QUESTION)

    assert [r["query"] for r in planner.requests] == ["你还记得我吗", QUESTION]
    plan = pipeline.response_plans.get(fence)
    assert plan is not None and plan.instructions == f"回答：{QUESTION}"
    await runtime.close()


@pytest.mark.asyncio
async def test_a_fence_that_moves_before_the_commit_gets_its_own_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.reply_pipeline")
    runtime = _runtime("plan-head-start-fence-moved")

    async def live_lookup(_text: str) -> bool:
        # A tool-condition change while the commit waits gives it another fence than the one predicted.
        await runtime.orchestrator.bump_tool_epoch_on_condition_change()
        return False

    runtime.set_live_lookup_semantic_resolver(live_lookup)
    planner = _Planner()
    pipeline = _pipeline(runtime, planner)
    predicted = runtime.fence.bump_turn()

    committed = await pipeline.prepare_committed_turn(QUESTION)

    assert committed != predicted
    assert [r["fence"] for r in planner.requests] == [predicted, committed]
    assert planner.requests[1]["task"] != reply_pipeline.EARLY_RESPONSE_PLAN_TASK_NAME
    plan = pipeline.response_plans.get(committed)
    assert plan is not None and plan.fence == committed
    assert _outcomes(caplog) == ["other_fence"]
    await runtime.close()


@pytest.mark.asyncio
async def test_an_answer_that_lands_after_the_turn_moved_on_is_not_used() -> None:
    runtime = _runtime("plan-head-start-stale-answer")
    planner = _Planner()
    planner.release = asyncio.Event()
    pipeline = _pipeline(runtime, planner)

    predicted = runtime.fence.bump_turn()
    prepare = asyncio.create_task(pipeline.prepare_committed_turn(QUESTION))
    for _ in range(100):
        if runtime.fence == predicted:
            break
        await asyncio.sleep(0)
    await _let_tasks_run()
    assert runtime.fence == predicted and not prepare.done()  # committed, and waiting for the answer
    assert planner.requests[0]["fence"] == predicted
    await runtime.orchestrator.bump_tool_epoch_on_condition_change()
    planner.release.set()

    with pytest.raises(StopResponse):
        await prepare
    assert len(planner.requests) == 1
    assert len(pipeline.response_plans) == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_a_local_crisis_sentence_sends_no_head_start() -> None:
    runtime = _runtime("plan-head-start-crisis")
    planner = _Planner()
    pipeline = _pipeline(runtime, planner)

    pipeline.start_committed_turn_plan("我不想活了")
    await _let_tasks_run()

    assert planner.requests == []
    await runtime.close()


@pytest.mark.asyncio
async def test_no_head_start_without_the_profiles_memory_grant() -> None:
    runtime = DuplexRuntime.create(session_id="plan-head-start-no-grant")
    bind_owner_policy(
        runtime,
        owner_evidence=False,
        tools=False,
        voice_profile=False,
        memory_recall_grant=False,
    )
    bind_owner_speaker(runtime)
    planner = _Planner()
    pipeline = _pipeline(runtime, planner)

    pipeline.start_committed_turn_plan(QUESTION)
    await _let_tasks_run()

    assert planner.requests == []
    await runtime.close()
