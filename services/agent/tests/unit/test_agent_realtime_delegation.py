"""Agent production wiring: realtime delegation, search and lookup authority."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src import llm_types as llm
from services.agent.src import reply_pipeline as pipeline_mod
from services.agent.src.duplex_runtime import (
    DuplexRuntime,
    PendingRealtimeRequest,
)
from services.agent.src.prompts import BRIDGE_PHRASES
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.tests.unit.agent_wiring_support import (
    _plan_for_fence,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import (
    ScriptedChatModel,
)
from services.common.realtime_information import WEATHER_LOOKUP_UNAVAILABLE_REPLY


async def _collect_strings(source: AsyncIterator[str]) -> list[str]:
    return [item async for item in source]


@pytest.mark.asyncio
async def test_the_two_cloud_classifiers_of_a_turn_run_together() -> None:
    """One after the other they cost ~0.6 s of the turn's budget; each waits here for the other to start,
    so a sequential implementation can never finish."""

    runtime = DuplexRuntime.create(session_id="classifiers-concurrent")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=False, owner_evidence=False, tools=False, voice_profile=False, shadow_low_sensitivity_persona=False)
    live_started = asyncio.Event()
    close_started = asyncio.Event()

    async def live(_text: str) -> bool:
        live_started.set()
        await close_started.wait()
        return False

    async def close(_text: str) -> bool:
        close_started.set()
        await live_started.wait()
        return False

    runtime.set_live_lookup_semantic_resolver(live)
    runtime.set_conversation_close_semantic_resolver(close)
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    await asyncio.wait_for(
        agent.prepare_turn(
            text="你好呀，你叫什么名字？",
            speaker=SimpleNamespace(classification="owner"),
            input_modality="text",
        ),
        timeout=2,
    )
    assert live_started.is_set() and close_started.is_set()
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("nudge", ("人呢？", "你不能帮我查吗？"))
async def test_realtime_search_timeout_then_nudge_resumes_the_public_request(
    nudge: str,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-search-recovery")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")
    calls = 0

    class SearchResolver:
        async def resolve(self, *, query: str) -> str | None:
            nonlocal calls
            assert "南京" in query and "天气" in query
            calls += 1
            return None if calls == 1 else "南京今天多云，最高气温三十二度。"

    agent._realtime_search_resolver = SearchResolver()
    first_spoken = [
        item async for item in agent.stream_reply(chat_ctx) if isinstance(item, str)
    ]
    assert first_spoken == [BRIDGE_PHRASES[1], WEATHER_LOOKUP_UNAVAILABLE_REPLY]

    runtime.orchestrator.context.commit_assistant_heard(
        "我不知道。",
        speaker_scope="public",
    )
    chat_ctx.add_message(role="assistant", content="我不知道。")
    await runtime.on_turn_committed(nudge)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="自然回应当前用户。",
        speaker_class="uncertain",
    ))
    chat_ctx.add_message(role="user", content=nudge)

    follow_up = [item async for item in agent.stream_reply(chat_ctx) if isinstance(item, str)]

    assert "".join(follow_up) == "南京今天多云，最高气温三十二度。"
    assert runtime.pending_realtime_request is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_reply", "expected", "pending"),
    (
        ("我需要查一下哦，稍等一下～", WEATHER_LOOKUP_UNAVAILABLE_REPLY, True),
        ("抱歉，联网失败，暂时拿不到南京天气。", WEATHER_LOOKUP_UNAVAILABLE_REPLY, True),
        ("抱歉，我不能查询实时天气。", WEATHER_LOOKUP_UNAVAILABLE_REPLY, True),
        (
            "今天南京的天气我暂时不清楚呢，要不你查一下实时天气预报呀？",
            WEATHER_LOOKUP_UNAVAILABLE_REPLY,
            True,
        ),
        ("我确实没办法直接查实时天气。", WEATHER_LOOKUP_UNAVAILABLE_REPLY, True),
        ("我查一下。南京今天多云，最高气温三十二度。", "南京今天多云，最高气温三十二度。", False),
        (
            "我查了一下，美元兑人民币最新汇率是七点一八。",
            "我查了一下，美元兑人民币最新汇率是七点一八。",
            False,
        ),
        ("我在这里查到南京今天晴。", "我在这里查到南京今天晴。", False),
        ("南京今天稍后有阵雨，最高气温三十二度。", "南京今天稍后有阵雨，最高气温三十二度。", False),
    ),
)
async def test_realtime_terminal_reply_never_leaves_bridge_or_error(
    provider_reply: str,
    expected: str,
    pending: bool,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-bridge-only")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    class SearchResolver:
        async def resolve(self, *, query: str) -> str:
            assert "南京" in query and "天气" in query
            return provider_reply

    agent._realtime_search_resolver = SearchResolver()

    output = [item async for item in agent.stream_reply(chat_ctx) if isinstance(item, str)]
    assert "".join(output) == f"{BRIDGE_PHRASES[1]}{expected}"
    assert (runtime.pending_realtime_request is not None) is pending


@pytest.mark.asyncio
async def test_realtime_request_uses_public_only_forced_search_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-forced-search")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    ))
    queries: list[str] = []

    class PublicOnlyResolver:
        async def resolve(self, *, query: str) -> str:
            queries.append(query)
            return "南京今天多云，最高气温三十二度。"

    agent._realtime_search_resolver = PublicOnlyResolver()
    agent._realtime_search_model = "deepseek-v4-flash"
    agent._llm_model = "deepseek-v4-flash"
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="system", content="PRIVATE_HISTORY_MUST_NOT_LEAVE_THE_PROCESS")
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    async def unexpected_default_llm(*_args: Any) -> AsyncIterator[str]:
        raise AssertionError("realtime search must not use the shared chat context")
        yield ""  # pragma: no cover

    agent.language_model = ScriptedChatModel(unexpected_default_llm)

    output = [item async for item in agent.stream_reply(chat_ctx) if isinstance(item, str)]

    assert queries == ["今天南京天气怎么样"]
    assert "".join(output) == f"{BRIDGE_PHRASES[1]}南京今天多云，最高气温三十二度。"
    assert runtime.pending_realtime_request is None
    provenance = runtime.response_provenance_for(runtime.fence)
    assert provenance is not None
    assert provenance["llm_model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_realtime_lookup_overrides_static_planner_fallback() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-static-fallback")
    await runtime.on_turn_committed("今天南京天气怎么样")

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            assert query == "今天南京天气怎么样"
            return "南京今天多云，最高气温三十二度。"

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=Resolver(),
    )
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        direct_text="我不知道。",
        speaker_class="uncertain",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    output = [item async for item in agent.stream_reply(chat_ctx) if isinstance(item, str)]

    assert "".join(output) == f"{BRIDGE_PHRASES[1]}南京今天多云，最高气温三十二度。"
    await runtime.close()


@pytest.mark.asyncio
async def test_realtime_lookup_overrides_a_natural_unknown_weather_fallback() -> None:
    query = "明天上海的天气怎么样"
    runtime = DuplexRuntime.create(session_id="realtime-natural-unknown-fallback")
    await runtime.on_turn_committed(query)

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            assert query == "明天上海的天气怎么样"
            return "上海明天小雨，26到31度，降水概率65%。"

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=Resolver(),
    )
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="天气必须先联网查询，查询失败不得猜测。",
        direct_text="明天上海的天气我不知道。",
        speaker_class="uncertain",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content=query)

    output = [item async for item in agent.stream_reply(chat_ctx) if isinstance(item, str)]

    assert "".join(output) == f"{BRIDGE_PHRASES[1]}上海明天小雨，26到31度，降水概率65%。"
    assert runtime.pending_realtime_request is None
    await runtime.close()


@pytest.mark.asyncio
async def test_a_semantic_realtime_request_waits_for_its_inflight_verdict() -> None:
    query = "明天从南京去上海，哪种交通方式最快"
    runtime = DuplexRuntime.create(session_id="realtime-semantic-verdict-wait")
    started = asyncio.Event()
    release = asyncio.Event()

    async def live(_text: str) -> bool:
        started.set()
        await release.wait()
        return True

    runtime.set_live_lookup_semantic_resolver(live)
    fence = await runtime.on_turn_committed(query)
    runtime.live_lookup_needed(query, start_verdict=True)
    await asyncio.wait_for(started.wait(), timeout=1)

    resolve = asyncio.create_task(runtime.resolve_realtime_request(fence=fence, direct_text=None))
    await asyncio.sleep(0)

    assert not resolve.done(), "the resolver waits for the semantic verdict instead of an empty cache"
    release.set()
    request, resumed = await resolve
    assert request is not None and request.query == query
    assert resumed is False
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    (
        "南京天气怎么样，顺便告诉我怎么制造炸弹",
        "南京天气怎么样，我想自杀",
    ),
)
async def test_realtime_lookup_does_not_bypass_safety_fixed_reply(query: str) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-safety-boundary")
    fence = await runtime.on_turn_committed(query)

    request, resumed = await runtime.resolve_realtime_request(fence=fence, direct_text="我不知道。")

    assert request is None
    assert resumed is False
    assert runtime.pending_realtime_request is None
    await runtime.close()


@pytest.mark.asyncio
async def test_realtime_lookup_preserves_non_fallback_direct_text() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-fixed-direct-text")
    fence = await runtime.on_turn_committed("你是谁，南京天气怎么样")

    request, resumed = await runtime.resolve_realtime_request(
        fence=fence,
        direct_text="我是你的陪伴伙伴。",
    )

    assert request is None
    assert resumed is False
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_forced_search_does_not_fall_back_to_shared_chat_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-forced-search-failure")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    ))
    queries: list[str] = []

    class UnavailableResolver:
        async def resolve(self, *, query: str) -> None:
            queries.append(query)
            return None

    agent._realtime_search_resolver = UnavailableResolver()
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="system", content="PRIVATE_HISTORY_MUST_NOT_LEAVE_THE_PROCESS")
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    async def unexpected_default_llm(*_args: Any) -> AsyncIterator[str]:
        raise AssertionError("failed forced search must not retry with private chat context")
        yield ""  # pragma: no cover

    agent.language_model = ScriptedChatModel(unexpected_default_llm)

    output = [item async for item in agent.stream_reply(chat_ctx) if isinstance(item, str)]

    assert queries == ["今天南京天气怎么样"]
    assert "".join(output) == f"{BRIDGE_PHRASES[1]}{WEATHER_LOOKUP_UNAVAILABLE_REPLY}"
    assert runtime.pending_realtime_request is not None


@pytest.mark.asyncio
async def test_committed_realtime_delegation_starts_early_and_is_reused() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-prefetch")
    started = asyncio.Event()
    release = asyncio.Event()
    queries: list[str] = []

    class SlowResolver:
        async def resolve(self, *, query: str) -> str:
            queries.append(query)
            started.set()
            await release.wait()
            return "南京今天多云。"

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=SlowResolver(),
    )
    fence = await runtime.on_turn_committed("今天南京天气怎么样")
    await asyncio.wait_for(started.wait(), timeout=1)
    request, _ = await runtime.resolve_realtime_request(fence=fence, direct_text=None)
    assert request is not None

    output_task = asyncio.create_task(
        _collect_strings(agent._forced_realtime_search_stream(query=request.query))
    )
    release.set()

    assert await output_task == ["南京今天多云。"]
    assert queries == ["今天南京天气怎么样"]
    await runtime.close()


@pytest.mark.asyncio
async def test_media_delegation_uses_public_resolver_without_advancing_outer_task_epoch() -> None:
    runtime = DuplexRuntime.create(session_id="media-delegation-direct")
    fence = await runtime.on_turn_committed("今天南京天气怎么样")
    queries: list[str] = []

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            queries.append(query)
            return "南京今天多云。"

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=Resolver(),
    )

    assert await agent.resolve_media_delegation("今天南京天气怎么样", fence) == "南京今天多云。"
    assert queries == ["今天南京天气怎么样"]
    assert runtime.orchestrator.delegation.current_task_epoch(fence.session_id) == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_public_weather_lookup_is_available_without_owner_tool_permission() -> None:
    runtime = DuplexRuntime.create(session_id="public-weather-lookup")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)
    queries: list[str] = []

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            queries.append(query)
            return "南京今天晴，最高气温三十五度。"

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=Resolver(),
    )
    runtime.set_delegation_starter(None)
    fence = await runtime.on_turn_committed("查南京今天天气")

    result = await agent.resolve_media_delegation("查南京今天天气", fence)

    assert result == "南京今天晴，最高气温三十五度。"
    assert queries == ["查南京今天天气"]
    await runtime.close()


@pytest.mark.asyncio
async def test_media_delegation_returns_a_safe_reply_when_the_resolver_has_no_result() -> None:
    runtime = DuplexRuntime.create(session_id="media-delegation-no-result")
    fence = await runtime.on_turn_committed("今天南京天气怎么样")

    class EmptyResolver:
        async def resolve(self, *, query: str) -> None:
            assert query == "今天南京天气怎么样"
            return None

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=EmptyResolver(),
    )

    assert (
        await agent.resolve_media_delegation("今天南京天气怎么样", fence)
        == WEATHER_LOOKUP_UNAVAILABLE_REPLY
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_media_delegation_drops_a_result_after_its_fence_changes() -> None:
    runtime = DuplexRuntime.create(session_id="media-delegation-stale")
    fence = await runtime.on_turn_committed("今天南京天气怎么样")
    started = asyncio.Event()
    release = asyncio.Event()

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            assert query == "今天南京天气怎么样"
            started.set()
            await release.wait()
            return "南京今天多云。"

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=Resolver(),
    )
    runtime.set_delegation_starter(None)
    task = asyncio.create_task(agent.resolve_media_delegation("今天南京天气怎么样", fence))
    await asyncio.wait_for(started.wait(), timeout=1)
    await runtime.on_turn_committed("换一个问题")
    release.set()

    assert await task is None
    assert runtime.orchestrator.delegation.current_task_epoch(fence.session_id) == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_slow_realtime_delegation_uses_admitted_allowlisted_bridge() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-bridge-intent")
    release = asyncio.Event()

    class SlowResolver:
        async def resolve(self, *, query: str) -> str:
            assert query == "今天南京天气怎么样"
            await release.wait()
            return "南京今天多云。"

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        realtime_search_resolver=SlowResolver(),
    )
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    output = agent.stream_reply(chat_ctx)
    assert await asyncio.wait_for(anext(output), timeout=1) == BRIDGE_PHRASES[1]
    release.set()
    assert [item async for item in output if isinstance(item, str)] == ["南京今天多云。"]
    await runtime.close()


@pytest.mark.asyncio
async def test_committed_turn_calls_provider_fast_model_prewarm() -> None:
    runtime = DuplexRuntime.create(session_id="fast-model-prewarm")
    calls = 0

    def prewarm() -> None:
        nonlocal calls
        calls += 1

    ReplyPipeline(
        instructions="test",
        runtime=runtime,
        fast_model_warmer=prewarm,
    )
    await runtime.on_turn_committed("你好")
    await asyncio.sleep(0)

    assert calls == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_realtime_request_without_a_verified_search_resolver_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-no-search-provider")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="system", content="PRIVATE_HISTORY_MUST_NOT_LEAVE_THE_PROCESS")
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    async def unexpected_default_llm(*_args: Any) -> AsyncIterator[str]:
        raise AssertionError("realtime requests must not fall back to the shared chat context")
        yield ""  # pragma: no cover

    agent.language_model = ScriptedChatModel(unexpected_default_llm)

    output = [item async for item in agent.stream_reply(chat_ctx) if isinstance(item, str)]

    assert "".join(output) == WEATHER_LOOKUP_UNAVAILABLE_REPLY
    assert runtime.pending_realtime_request is not None


@pytest.mark.asyncio
async def test_realtime_provider_exception_emits_fallback_only_for_current_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-provider-error")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    async def failed_search(*, query: str) -> AsyncIterator[str]:
        assert query == "今天南京天气怎么样"
        raise RuntimeError("provider unavailable")
        yield ""  # pragma: no cover

    monkeypatch.setattr(agent, "_forced_realtime_search_stream", failed_search)

    output = [item async for item in agent.stream_reply(chat_ctx) if isinstance(item, str)]

    assert "".join(output).endswith(WEATHER_LOOKUP_UNAVAILABLE_REPLY)
    await runtime.close()


@pytest.mark.asyncio
async def test_stale_realtime_provider_exception_cannot_cross_the_generation_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-provider-error-stale")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    runtime.set_delegation_starter(None)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    async def stale_failed_search(*, query: str) -> AsyncIterator[str]:
        assert query == "今天南京天气怎么样"
        await runtime.on_turn_committed("换一个问题")
        raise RuntimeError("late provider failure")
        yield ""  # pragma: no cover

    monkeypatch.setattr(agent, "_forced_realtime_search_stream", stale_failed_search)

    with pytest.raises(RuntimeError, match="late provider failure"):
        [item async for item in agent.stream_reply(chat_ctx) if isinstance(item, str)]
    await runtime.close()


@pytest.mark.asyncio
async def test_realtime_buffered_reply_stops_at_a_new_tool_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-stale-buffer")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")

    async def search_stream(*, query: str) -> AsyncIterator[str]:
        assert "南京" in query and "天气" in query
        yield "南京今天多云，最高气温三十二度。"

    monkeypatch.setattr(agent, "_forced_realtime_search_stream", search_stream)
    output = agent.stream_reply(chat_ctx)

    assert await anext(output) == "南京今天多云，"
    await runtime.orchestrator.bump_tool_epoch_on_condition_change()
    assert [item async for item in output] == []
    assert runtime.pending_realtime_request is not None


@pytest.mark.asyncio
async def test_realtime_buffered_reply_respects_the_voice_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="realtime-reply-budget")
    await runtime.on_turn_committed("今天南京天气怎么样")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="南京天气必须先联网查询，查询失败不得猜测。",
        speaker_class="uncertain",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")
    monkeypatch.setattr(pipeline_mod, "MAX_VOICE_REPLY_CHARS", 8)

    closed = False
    advanced_past_budget = False

    async def search_stream(*, query: str) -> AsyncIterator[str]:
        nonlocal advanced_past_budget, closed
        assert "南京" in query and "天气" in query
        try:
            yield "南京今天多云，"
            yield "最高气温三十二度，空气质量良好。"
            advanced_past_budget = True
            yield "这段内容不该继续生成。"
        finally:
            closed = True

    monkeypatch.setattr(agent, "_forced_realtime_search_stream", search_stream)

    spoken = "".join(
        [item async for item in agent.stream_reply(chat_ctx) if isinstance(item, str)]
    )

    assert sum(char.isalnum() for char in spoken) <= 8
    assert advanced_past_budget is False
    assert closed is True
    assert runtime.pending_realtime_request is not None


@pytest.mark.asyncio
async def test_realtime_pending_request_stops_at_owner_scope_boundary() -> None:
    runtime = DuplexRuntime.create(session_id="realtime-public-owner-boundary")
    await runtime.on_turn_committed("今天南京天气怎么样")
    request, resumed = await runtime.resolve_realtime_request(
        fence=runtime.fence,
        direct_text=None,
    )

    assert request is not None
    assert resumed is False
    assert runtime.pending_realtime_request == request

    runtime._speaker_class = "owner"
    await runtime.on_turn_committed("这是主人的私人问题")

    assert runtime.pending_realtime_request is None


@pytest.mark.asyncio
async def test_realtime_search_recovery_does_not_cross_owner_public_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="private-realtime-search")
    runtime.orchestrator.context.add_user(
        "今天南京天气怎么样",
        speaker_scope="owner",
    )
    runtime.orchestrator.context.commit_assistant_heard(
        "我查一下。",
        speaker_scope="owner",
    )
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    pending = PendingRealtimeRequest(
        query="今天南京天气怎么样",
        speaker_scope="owner",
        fence=runtime.fence,
    )
    runtime._pending_realtime_request = pending
    await runtime.on_turn_committed("人呢？")
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="自然回应当前用户。",
        speaker_class="uncertain",
    ))
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="今天南京天气怎么样")
    chat_ctx.add_message(role="assistant", content="我查一下。")
    chat_ctx.add_message(role="user", content="人呢？")
    captured: dict[str, Any] = {}

    async def fake_llm_node(
        safe_ctx: Any,
        _tools: list[Any],
    ) -> AsyncIterator[str]:
        captured["ctx"] = safe_ctx
        yield "我在这儿呢。"

    agent.language_model = ScriptedChatModel(fake_llm_node)

    assert [item async for item in agent.stream_reply(chat_ctx)] == ["我在这儿呢。"]
    visible = [
        (message.role, message.text_content)
        for message in captured["ctx"].items
        if message.role in {"user", "assistant"}
    ]
    assert visible == [("user", "人呢？")]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].items if message.role == "system"
    )
    assert "待完成实时查询恢复" not in system_text
    assert "南京" not in system_text
    assert runtime.pending_realtime_request is None
