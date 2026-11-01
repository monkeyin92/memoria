"""The reply request of a turn goes out before the turn is committed, and only that exact turn plays it.

2026-10-31 device soak (run-20261031-223809, turn 8): the reply model's first token came 0.75 s after the
request, and the request left only after the commit (ASR final to commit done p50 0.98 s, p90 1.24 s over 36
commits).  The speculative reply sends the request the committed turn is predicted to send while the commit
still runs.  These pin that an unchanged turn plays exactly what the old path would have asked for, that any
difference sends the old request instead, and that nothing of a cancelled request reaches any reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from services.agent.src import reply_pipeline
from services.agent.src.contracts.ids import CancellationContext
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.handlers import LanguageModelRequest
from services.agent.src.providers.llm_failover import LlmUnavailableError
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import (
    ContextPrefetchFetch,
    ResponseGroundedItem,
    ResponsePlanFetch,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import bind_owner_speaker
from services.common.fixed_safety_scripts import FixedSafetyScript
from services.speaker.domain import DEVICE_BOUND_SUBJECT_REASON

SYSTEM_PROMPT = "【不可变安全底线】不要输出 Markdown。【服务模式】孩子：一到两句短话。"
EARLIER = "我今天画了一只黄色的小狗"
EARLIER_REPLY = "哇，黄色的小狗一定很可爱。"
QUESTION = "你还记得我今天画的什么吗"
OTHER = "我们明天去公园好不好"
DRAWING = "上午画了一只黄色的小狗，还给它画了一条红色的围巾"


def _answer(index: int) -> list[str]:
    """What the reply model says to its ``index``-th request."""

    return [f"第{index}次", "的回答。"]


class _Model:
    """Reply model double: records every request and answers the ``n``-th with ``_answer(n)``.

    ``release`` holds the tokens back while it is clear; ``closed`` lists the requests whose stream was closed.
    """

    def __init__(self) -> None:
        self.requests: list[list[dict[str, str]]] = []
        self.closed: list[int] = []
        self.yielded: list[str] = []
        self.release = asyncio.Event()
        self.release.set()
        self.fail_after: int | None = None

    @contextlib.asynccontextmanager
    async def chat(self, *, chat_ctx: Any, tools: list[Any]) -> AsyncIterator[Any]:
        index = len(self.requests)
        self.requests.append(chat_ctx.to_openai_messages())

        async def tokens() -> AsyncGenerator[str, None]:
            for position, token in enumerate(_answer(index)):
                await self.release.wait()
                if position == self.fail_after:
                    raise LlmUnavailableError("primary and backup both stalled")
                self.yielded.append(token)
                yield token

        stream = tokens()
        try:
            yield stream
        finally:
            await stream.aclose()
            self.closed.append(index)


def _memory() -> ResponseGroundedItem:
    return ResponseGroundedItem(
        kind="memory_claim",
        item_id="claim-1",
        content=DRAWING,
        use_as="fact",
        source_event_ids=("evt-1",),
        confidence=None,
        sharing_scope=None,
    )


class _Planner:
    """A device fence's plan verdict (the local safe plan answers) plus the bound person's memory."""

    def __init__(self) -> None:
        self.hold: asyncio.Event | None = None

    async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
        return ResponsePlanFetch(None, "fence_mismatch")

    async def prefetch_context(
        self, *, session_id: str, query: str, speaker_decision: object
    ) -> ContextPrefetchFetch:
        task = asyncio.current_task()
        mine = task is not None and task.get_name() == reply_pipeline.DEVICE_MEMORY_TASK_NAME
        if mine and self.hold is not None:
            await self.hold.wait()
        return ContextPrefetchFetch((_memory(),) if mine else (), None, None, "ok")


class _Verdict:
    """A semantic classifier that answers when the test lets it."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.release = asyncio.Event()

    async def __call__(self, _text: str) -> bool:
        await self.release.wait()
        return self.answer


def _runtime(name: str) -> DuplexRuntime:
    runtime = DuplexRuntime.create(session_id=name)
    bind_owner_policy(
        runtime,
        policy_version="test-policy",
        owner_evidence=False,
        tools=False,
        voice_profile=False,
    )
    decision = bind_owner_speaker(runtime)
    runtime._speaker_decision = replace(
        decision, reason_code=DEVICE_BOUND_SUBJECT_REASON, model_version="device-binding-v1"
    )
    return runtime


def _pipeline(
    runtime: DuplexRuntime, model: _Model, planner: _Planner | None = None, *, speculative: bool = True
) -> ReplyPipeline:
    agent = ReplyPipeline(
        instructions=SYSTEM_PROMPT,
        runtime=runtime,
        response_planner_client=planner or _Planner(),  # type: ignore[arg-type]
        speculative_reply=speculative,
    )
    agent.language_model = model
    return agent


async def _let_tasks_run() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def _reply(agent: ReplyPipeline, text: str) -> list[Any]:
    """Commit ``text`` the way the media session does and collect the reply items."""

    fence = await agent.prepare_committed_turn(text)
    request = LanguageModelRequest(user_text=text, cancellation=CancellationContext.capture(fence))
    return [item async for item in agent.stream(request)]


async def _earlier_turn(runtime: DuplexRuntime, agent: ReplyPipeline) -> None:
    """One finished exchange, so the predicted request carries history and a heard reply."""

    await _reply(agent, EARLIER)
    runtime.orchestrator.context.commit_assistant_heard(EARLIER_REPLY, speaker_scope="owner")


async def _announce(agent: ReplyPipeline, text: str) -> None:
    """The ASR final names the sentence, then the commit begins: the two calls the media session makes."""

    agent.warm_committed_turn(text)
    await _let_tasks_run()
    agent.start_committed_turn_plan(text)
    await _let_tasks_run()


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[str]:
    prefix = "speculative reply session_id="
    return [
        record.getMessage().split("outcome=", 1)[1].split(" ", 1)[0]
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]


def _spoken(items: list[Any]) -> str:
    return "".join(item for item in items if isinstance(item, str))


@pytest.mark.asyncio
async def test_an_unchanged_turn_plays_the_reply_the_old_path_would_have_asked_for(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.speculative_reply")
    replies: dict[bool, list[Any]] = {}
    models: dict[bool, _Model] = {}
    for speculative in (False, True):
        runtime = _runtime("speculative-reply-unchanged")
        model = _Model()
        agent = _pipeline(runtime, model, speculative=speculative)
        await _earlier_turn(runtime, agent)
        await _announce(agent, QUESTION)
        replies[speculative] = await _reply(agent, QUESTION)
        models[speculative] = model
        await runtime.close()

    old, new = models[False], models[True]
    assert len(old.requests) == len(new.requests) == 2, "one request per turn, never a second one"
    assert new.requests[1] == old.requests[1], "the speculative request is the old request, byte for byte"
    assert DRAWING in str(new.requests[1]) and EARLIER_REPLY in str(new.requests[1])
    assert replies[True] == replies[False] == ["第1次的回答。"]
    assert _outcomes(caplog) == ["used"]
    assert QUESTION not in caplog.text, "the sentence is never logged"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_after", [0, 1])
async def test_a_model_failure_reaches_the_reply_exactly_as_it_would_without_the_head_start(
    fail_after: int,
) -> None:
    replies: dict[bool, list[Any]] = {}
    for speculative in (False, True):
        runtime = _runtime("speculative-reply-failure")
        model = _Model()
        model.fail_after = fail_after
        agent = _pipeline(runtime, model, speculative=speculative)
        await _announce(agent, QUESTION)
        replies[speculative] = await _reply(agent, QUESTION)
        assert len(model.requests) == 1
        await runtime.close()

    assert replies[True] == replies[False] and replies[True], "the same fallback words, said the same way"


@pytest.mark.asyncio
async def test_the_request_goes_out_and_answers_while_the_commit_waits_for_the_close_verdict() -> None:
    runtime = _runtime("speculative-reply-overlap")
    verdict = _Verdict(answer=False)
    runtime.set_conversation_close_semantic_resolver(verdict)
    model = _Model()
    agent = _pipeline(runtime, model)

    await _announce(agent, QUESTION)
    reply = asyncio.create_task(_reply(agent, QUESTION))
    await _let_tasks_run()

    assert not reply.done(), "the commit still waits for the close verdict"
    assert len(model.requests) == 1 and model.yielded == _answer(0), "asked and answered meanwhile"
    verdict.release.set()
    assert await reply == ["第0次的回答。"]
    assert len(model.requests) == 1
    await runtime.close()


async def _change_text(runtime: DuplexRuntime, agent: ReplyPipeline) -> str:
    return OTHER


async def _change_speaker(runtime: DuplexRuntime, agent: ReplyPipeline) -> str:
    runtime._speaker_decision = replace(runtime._speaker_decision, model_version="device-binding-v2")
    return QUESTION


async def _change_fence(runtime: DuplexRuntime, agent: ReplyPipeline) -> str:
    await runtime.orchestrator.bump_tool_epoch_on_condition_change()
    return QUESTION


async def _change_policy(runtime: DuplexRuntime, agent: ReplyPipeline) -> str:
    runtime._mode_policy = replace(runtime.mode_policy, policy_version="test-policy-2")
    return QUESTION


async def _change_context(runtime: DuplexRuntime, agent: ReplyPipeline) -> str:
    runtime.orchestrator.context.business_summary = "今天下午要去奶奶家"
    return QUESTION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        (_change_text, "other_text"),
        (_change_speaker, "other_speaker"),
        (_change_fence, "other_fence"),
        (_change_policy, "other_policy"),
        (_change_context, "other_request"),
    ],
)
async def test_a_turn_that_differs_in_any_input_sends_its_own_request(
    caplog: pytest.LogCaptureFixture, change: Any, outcome: str
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.speculative_reply")
    runtime = _runtime(f"speculative-reply-{outcome}")
    model = _Model()
    agent = _pipeline(runtime, model)
    await _earlier_turn(runtime, agent)
    caplog.clear()
    await _announce(agent, QUESTION)
    assert len(model.requests) == 2, "the speculative request is out"

    committed = await change(runtime, agent)
    items = await _reply(agent, committed)

    assert len(model.requests) == 3, "the committed turn asked for itself"
    assert _spoken(items) == "第2次的回答。"
    assert 1 in model.closed, "the speculative stream is closed"
    assert _outcomes(caplog) == [outcome]
    await runtime.close()


@pytest.mark.asyncio
async def test_a_memory_still_on_its_way_at_the_commit_sends_no_speculative_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.speculative_reply")
    runtime = _runtime("speculative-reply-late")
    planner = _Planner()
    planner.hold = asyncio.Event()
    model = _Model()
    agent = _pipeline(runtime, model, planner)

    await _announce(agent, QUESTION)
    assert model.requests == [], "the request waits for the memory it may carry"
    items = await _reply(agent, QUESTION)
    planner.hold.set()
    await _let_tasks_run()

    assert len(model.requests) == 1 and DRAWING not in str(model.requests[0])
    assert items == ["第0次的回答。"]
    assert _outcomes(caplog) == ["late"]
    await runtime.close()


@pytest.mark.asyncio
async def test_a_crisis_sentence_sends_no_speculative_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.speculative_reply")
    runtime = _runtime("speculative-reply-crisis")
    model = _Model()
    agent = _pipeline(runtime, model)

    await _announce(agent, "我不想活了")
    items = await _reply(agent, "我不想活了")

    assert model.requests == []
    assert [type(item) for item in items] == [FixedSafetyScript]
    assert _outcomes(caplog) == ["crisis"], "one line, from the commit start"
    await runtime.close()


@pytest.mark.asyncio
async def test_a_farewell_the_commit_rejects_never_plays_and_its_stream_is_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.speculative_reply")
    runtime = _runtime("speculative-reply-close-rejected")
    verdict = _Verdict(answer=True)
    runtime.set_conversation_close_semantic_resolver(verdict)
    model = _Model()
    model.release.clear()
    agent = _pipeline(runtime, model)
    farewell = "那我先去写作业啦"

    runtime.start_conversation_close_verdict(farewell)
    await _announce(agent, farewell)
    assert len(model.requests) == 1, "the verdict was not in when the request went out"
    verdict.release.set()
    assert await runtime.resolve_conversation_close_needed(farewell) is True
    agent.discard_speculative_reply("conversation_end_explicit")
    model.release.set()
    await _let_tasks_run()

    assert model.closed == [0] and model.yielded == []
    assert _outcomes(caplog) == ["close"]
    await runtime.close()


@pytest.mark.asyncio
async def test_a_close_verdict_that_lands_during_the_commit_keeps_the_speculative_text_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.speculative_reply")
    runtime = _runtime("speculative-reply-close-prepared")
    verdict = _Verdict(answer=True)
    runtime.set_conversation_close_semantic_resolver(verdict)
    model = _Model()
    agent = _pipeline(runtime, model)

    runtime.start_conversation_close_verdict(QUESTION)
    await _announce(agent, QUESTION)
    verdict.release.set()
    items = await _reply(agent, QUESTION)

    assert len(model.requests) == 2 and 0 in model.closed
    assert _spoken(items) == "第1次的回答。"
    assert _outcomes(caplog) == ["close"]
    await runtime.close()


@pytest.mark.asyncio
async def test_a_live_lookup_turn_never_plays_the_speculative_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.speculative_reply")
    runtime = _runtime("speculative-reply-lookup")
    verdict = _Verdict(answer=True)
    runtime.set_live_lookup_semantic_resolver(verdict)
    model = _Model()
    agent = _pipeline(runtime, model)
    question = "恐龙为什么会灭绝呀"

    runtime.live_lookup_needed(question, start_verdict=True)
    await _announce(agent, question)
    assert len(model.requests) == 1, "the verdict was not in when the request went out"
    verdict.release.set()
    items = await _reply(agent, question)

    assert "第0次" not in _spoken(items) and model.yielded[:1] != []
    assert 0 in model.closed
    assert _outcomes(caplog) == ["lookup"]
    await runtime.close()


@pytest.mark.asyncio
async def test_a_cancelled_speculation_leaks_nothing_into_any_later_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.speculative_reply")
    runtime = _runtime("speculative-reply-no-leak")
    model = _Model()
    agent = _pipeline(runtime, model)

    await _announce(agent, QUESTION)
    assert model.yielded == _answer(0), "the speculative answer is buffered in full"
    first = await _reply(agent, OTHER)
    runtime.orchestrator.context.commit_assistant_heard(_spoken(first), speaker_scope="owner")
    second = await _reply(agent, QUESTION)

    assert first == ["第1次的回答。"] and second == ["第2次的回答。"]
    assert "第0次" not in _spoken(first + second)
    assert model.closed[0] == 0
    assert _outcomes(caplog) == ["other_text"]
    await runtime.close()


@pytest.mark.asyncio
async def test_an_adopted_stream_stopped_early_closes_the_model_stream() -> None:
    runtime = _runtime("speculative-reply-adopted-stop")
    model = _Model()
    agent = _pipeline(runtime, model)
    model.release.clear()

    await _announce(agent, QUESTION)
    fence = await agent.prepare_committed_turn(QUESTION)
    request = LanguageModelRequest(user_text=QUESTION, cancellation=CancellationContext.capture(fence))
    consumer = asyncio.create_task(anext(aiter(agent.stream(request))))
    await _let_tasks_run()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await _let_tasks_run()

    assert model.closed == [0] and model.yielded == []
    await runtime.close()


@pytest.mark.asyncio
async def test_without_the_switch_no_request_goes_out_before_the_commit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.speculative_reply")
    runtime = _runtime("speculative-reply-off")
    model = _Model()
    agent = _pipeline(runtime, model, speculative=False)

    await _announce(agent, QUESTION)
    assert model.requests == []
    assert await _reply(agent, QUESTION) == ["第0次的回答。"]
    assert _outcomes(caplog) == []
    await runtime.close()


@pytest.mark.asyncio
async def test_a_clock_question_answered_locally_sends_no_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.speculative_reply")
    runtime = _runtime("speculative-reply-local-answer")
    model = _Model()
    agent = _pipeline(runtime, model)

    await _announce(agent, "现在几点了")
    await _reply(agent, "现在几点了")

    assert model.requests == []
    assert _outcomes(caplog) == ["plan"]
    await runtime.close()
