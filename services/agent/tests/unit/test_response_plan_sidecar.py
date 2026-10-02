"""A device turn no longer waits for a response plan the client can never apply.

Round 10 (2026-10-02, 38 device turns): every plan request came back unusable -- 28 ``fence_mismatch`` (the
wire fence carries no ``session_epoch``, so with a device fence of epoch >= 1 the answer can never match) and
10 cut off by the client's 0.8 s timeout -- yet each reply waited for it (p50 0.81 s) before the model was
asked.  The request itself is not optional: the control route runs the crisis routing and enqueues the
guardian's notification before it builds anything, so it is still sent exactly once per turn, but as a
background task that no reply waits for.  These pin that split: the reply is not held, the request is still
made, the prompt the model sees is the same, a failure never reaches the turn, and closing the session
leaves no task behind.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from services.agent.src import response_plan_sidecar
from services.agent.src.contracts.ids import CancellationContext
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.handlers import LanguageModelRequest
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import ResponsePlanFetch, ResponsePlannerClient
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import ScriptedChatModel, bind_owner_speaker
from services.common.companion_response_safety import (
    CRISIS_SUPPORT_REPLY,
    companion_safety_decision,
)

SYSTEM_PROMPT = "【不可变安全底线】不要输出 Markdown。【服务模式】孩子：一到两句短话。"
QUESTION = "天空为什么是蓝色的"
CRISIS = "我不想活了"


class _Planner:
    """The real client's verdict for a device fence: the wire fence lacks ``session_epoch``."""

    def __init__(self, *, sidecar: bool = True, delay_s: float = 0.0, outcome: Any = None) -> None:
        if sidecar:
            # What ResponsePlannerClient declares; a double that does not say so is awaited as before.
            self.echoes_session_epoch = False
        self.delay_s = delay_s
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []
        self.finished = asyncio.Event()

    async def fetch(self, **kwargs: Any) -> ResponsePlanFetch:
        self.calls.append(kwargs)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome or ResponsePlanFetch(None, "fence_mismatch")
        finally:
            self.finished.set()


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


def _pipeline(runtime: DuplexRuntime, planner: _Planner) -> tuple[ReplyPipeline, dict[str, Any]]:
    captured: dict[str, Any] = {}

    async def model(safe_ctx: Any, _tools: list[Any]) -> Any:
        captured["ctx"] = safe_ctx

        async def one() -> Any:
            yield "好的。"

        return one()

    pipeline = ReplyPipeline(
        instructions=SYSTEM_PROMPT,
        runtime=runtime,
        response_planner_client=planner,  # type: ignore[arg-type]
    )
    pipeline.language_model = ScriptedChatModel(model)
    return pipeline, captured


async def _run_turn(
    runtime: DuplexRuntime, planner: _Planner, text: str = QUESTION
) -> tuple[ReplyPipeline, list[str], list[str], float]:
    """One spoken turn: the pipeline, the messages the model received, what was spoken, how long it took."""

    pipeline, captured = _pipeline(runtime, planner)
    loop = asyncio.get_running_loop()
    started = loop.time()
    fence = await pipeline.prepare_committed_turn(text)
    elapsed = loop.time() - started
    request = LanguageModelRequest(user_text=text, cancellation=CancellationContext.capture(fence))
    spoken = [token async for token in pipeline.stream(request)]
    # A fixed reply (the crisis text) is spoken without asking the model: no prompt was built.
    messages = [m.text_content for m in captured["ctx"].items] if "ctx" in captured else []
    return pipeline, messages, spoken, elapsed


@pytest.mark.asyncio
async def test_the_reply_does_not_wait_for_a_plan_the_client_cannot_apply() -> None:
    runtime = _runtime("plan-sidecar-not-blocking")
    planner = _Planner(delay_s=2.0)

    _, _, _, elapsed = await _run_turn(runtime, planner)

    assert elapsed < 0.5
    await runtime.close()


@pytest.mark.asyncio
async def test_the_plan_request_is_still_sent_once_per_turn_with_a_wider_bound() -> None:
    """The control route's crisis routing and guardian notification are the reason it is sent at all."""

    runtime = _runtime("plan-sidecar-sent-once")
    planner = _Planner()

    await _run_turn(runtime, planner)
    await asyncio.wait_for(planner.finished.wait(), 1.0)
    await asyncio.sleep(0.05)

    assert len(planner.calls) == 1
    call = planner.calls[0]
    assert call["query"] == QUESTION
    assert call["session_id"] == runtime.session_id
    assert call["fence"].session_epoch >= 1
    assert call["timeout_s"] == response_plan_sidecar.SIDECAR_TIMEOUT_S >= 2.0
    await runtime.close()


@pytest.mark.asyncio
async def test_a_crisis_turn_still_sends_the_request_and_speaks_the_same_crisis_reply() -> None:
    assert companion_safety_decision(CRISIS) != "none"
    blocking_runtime = _runtime("plan-sidecar-crisis-blocking")
    blocking = _Planner(sidecar=False)
    _, _, blocking_spoken, _ = await _run_turn(blocking_runtime, blocking, CRISIS)
    sidecar_runtime = _runtime("plan-sidecar-crisis-sidecar")
    sidecar = _Planner()
    _, _, sidecar_spoken, _ = await _run_turn(sidecar_runtime, sidecar, CRISIS)
    await asyncio.wait_for(sidecar.finished.wait(), 1.0)

    assert len(blocking.calls) == len(sidecar.calls) == 1
    assert sidecar.calls[0]["query"] == CRISIS
    assert "".join(sidecar_spoken) == "".join(blocking_spoken) == CRISIS_SUPPORT_REPLY
    await blocking_runtime.close()
    await sidecar_runtime.close()


@pytest.mark.asyncio
async def test_the_prompt_the_model_sees_is_the_one_it_saw_before() -> None:
    blocking_runtime = _runtime("plan-sidecar-same-prompt-a")
    sidecar_runtime = _runtime("plan-sidecar-same-prompt-b")

    _, blocking_messages, _, _ = await _run_turn(blocking_runtime, _Planner(sidecar=False))
    _, sidecar_messages, _, _ = await _run_turn(sidecar_runtime, _Planner())

    def normalized(messages: list[str]) -> list[str]:
        return [m.replace("plan-sidecar-same-prompt-a", "S").replace("plan-sidecar-same-prompt-b", "S") for m in messages]

    assert normalized(sidecar_messages) == normalized(blocking_messages)
    assert any("【控制响应计划】" in m for m in sidecar_messages)
    await blocking_runtime.close()
    await sidecar_runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        ResponsePlanFetch(None, "fence_mismatch"),
        ResponsePlanFetch(None, "http_500"),
        ResponsePlanFetch(None, "request_or_payload_invalid"),
        RuntimeError("control plane down"),
    ],
    ids=["fence_mismatch", "http_500", "request_invalid", "raises"],
)
async def test_whatever_the_request_returns_never_reaches_the_turn_and_is_logged_once(
    outcome: Any, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = _runtime("plan-sidecar-outcome")
    planner = _Planner(outcome=outcome)
    caplog.set_level(logging.INFO)

    _, messages, _, _ = await _run_turn(runtime, planner)
    await asyncio.wait_for(planner.finished.wait(), 1.0)
    await asyncio.sleep(0.05)

    assert any("【控制响应计划】" in m for m in messages)
    lines = [r for r in caplog.records if "response plan sidecar" in r.getMessage()]
    assert len(lines) == 1
    assert QUESTION not in lines[0].getMessage()
    assert not [r for r in caplog.records if "duplex background task failed" in r.getMessage()]
    await runtime.close()


@pytest.mark.asyncio
async def test_a_request_that_outlives_its_bound_is_given_up_quietly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(response_plan_sidecar, "SIDECAR_TIMEOUT_S", 0.05)
    runtime = _runtime("plan-sidecar-timeout")
    planner = _Planner(delay_s=5.0)
    caplog.set_level(logging.INFO)

    await _run_turn(runtime, planner)
    await asyncio.sleep(0.4)

    lines = [r.getMessage() for r in caplog.records if "response plan sidecar" in r.getMessage()]
    assert len(lines) == 1 and "timeout" in lines[0]
    assert not [r for r in caplog.records if "duplex background task failed" in r.getMessage()]
    await runtime.close()


@pytest.mark.asyncio
async def test_closing_the_session_leaves_no_request_behind() -> None:
    runtime = _runtime("plan-sidecar-close")
    planner = _Planner(delay_s=30.0)

    await _run_turn(runtime, planner)
    assert any(t.get_name() == response_plan_sidecar.SIDECAR_TASK_NAME for t in asyncio.all_tasks())

    loop = asyncio.get_running_loop()
    started = loop.time()
    await runtime.close()

    assert loop.time() - started < 2.0
    assert not [t for t in asyncio.all_tasks() if t.get_name() == response_plan_sidecar.SIDECAR_TASK_NAME and not t.done()]


@pytest.mark.asyncio
async def test_a_client_that_can_apply_the_plan_is_still_awaited() -> None:
    """Only a client that cannot echo the epoch is sidecar-only; a double that does keeps the old wait."""

    runtime = _runtime("plan-sidecar-awaited")
    planner = _Planner(sidecar=False, delay_s=0.3)

    _, _, _, elapsed = await _run_turn(runtime, planner)

    assert elapsed >= 0.3
    assert len(planner.calls) == 1
    assert "timeout_s" not in planner.calls[0]
    await runtime.close()


@pytest.mark.asyncio
async def test_a_fence_the_client_can_match_is_still_awaited() -> None:
    """Epoch 0 (no subject switch yet) can match a plan built without an epoch: that path is unchanged."""

    runtime = _runtime("plan-sidecar-epoch-zero")
    planner = _Planner()
    fence = runtime.fence
    # Isolate the epoch condition: the profile is bound to epoch 1, so it would refuse an epoch-0 fence
    # by itself.
    runtime.profile_permits = lambda *_a, **_k: True  # type: ignore[method-assign]

    assert response_plan_sidecar.applies(planner, runtime, fence)
    assert not response_plan_sidecar.applies(planner, runtime, fence.with_session_epoch(0))
    assert not response_plan_sidecar.applies(None, runtime, fence)
    await runtime.close()


@pytest.mark.asyncio
async def test_without_the_memory_grant_no_request_is_made_at_all() -> None:
    """No long-term-memory grant means the turn never asked the control plane for a plan: still true."""

    runtime = DuplexRuntime.create(session_id="plan-sidecar-no-grant")
    bind_owner_policy(
        runtime,
        policy_version="test-policy",
        private_context=False,
        owner_evidence=False,
        tools=False,
        voice_profile=False,
        memory_recall_grant=False,
    )
    bind_owner_speaker(runtime)
    planner = _Planner()

    await _run_turn(runtime, planner)
    await asyncio.sleep(0.05)

    assert planner.calls == []
    assert not response_plan_sidecar.applies(planner, runtime, runtime.fence)
    await runtime.close()


def test_the_real_client_declares_that_it_cannot_echo_the_epoch() -> None:
    assert ResponsePlannerClient.echoes_session_epoch is False


@pytest.mark.asyncio
async def test_the_real_client_puts_the_wider_bound_on_the_wire_only_when_asked() -> None:
    """``timeout_s`` reaches httpx for the sidecar request; a plain ``fetch`` keeps the configured 0.8 s."""

    import httpx
    from services.agent.src.response_planner_client import ResponsePlannerClientConfig
    from services.speaker.domain import SpeakerDecision, permissions_for_speaker

    seen: list[dict[str, float | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.extensions["timeout"]))
        return httpx.Response(500)

    speaker = SpeakerDecision(
        classification="owner",
        score=0.9,
        quality_score=0.9,
        reason_code="owner_match",
        model_version="m",
        template_version=1,
        profile_id="p",
        permissions=permissions_for_speaker("owner"),
    )
    fence = _runtime("plan-sidecar-wire").fence
    config = ResponsePlannerClientConfig(endpoint="http://control/v1/interaction/response-plan", internal_token="t")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ResponsePlannerClient(config, client=http)
        plain = await client.fetch(session_id=fence.session_id, query="你好", fence=fence, speaker_decision=speaker)
        wide = await client.fetch(
            session_id=fence.session_id, query="你好", fence=fence, speaker_decision=speaker, timeout_s=3.0
        )

    assert plain.reason == wide.reason == "http_500"
    assert seen[0]["read"] == config.timeout_s == 0.8
    assert seen[1]["read"] == 3.0
