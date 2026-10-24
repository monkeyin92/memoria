from __future__ import annotations

import pytest
from services.agent.src.contracts.ids import CancellationContext
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.handlers import LanguageModelRequest
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import bind_owner_speaker

_SENTENCE = "我现在还查不到天气，可以请爸爸妈妈帮你看看窗外或者手机哦。"


class _RaisingChatModel:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, **_kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("chat model must not invent weather")


class _RaisingResolver:
    def __init__(self) -> None:
        self.weather = None
        self.qwen = None
        self.calls = 0

    async def resolve(self, *, query: str) -> str:
        self.calls += 1
        raise AssertionError(f"realtime search must not run for {query}")


@pytest.mark.asyncio
async def test_both_weather_turns_speak_the_fallback_without_calling_a_model() -> None:
    runtime = DuplexRuntime.create(session_id="weather-fallback")
    bind_owner_policy(
        runtime,
        policy_version="test-policy",
        private_context=True,
        owner_evidence=True,
        tools=True,
        voice_profile=False,
    )
    bind_owner_speaker(runtime)
    model = _RaisingChatModel()
    resolver = _RaisingResolver()
    pipeline = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        language_model=model,
        realtime_search_resolver=resolver,
    )

    spoken: list[str] = []
    for query in ("今天天气怎么样", "杭州"):
        fence = await pipeline.prepare_committed_turn(query)
        parts = [
            part
            async for part in pipeline.stream(
                LanguageModelRequest(query, CancellationContext.capture(fence))
            )
        ]
        spoken.append("".join(part if isinstance(part, str) else str(part) for part in parts))
        assert runtime.pending_realtime_request is None

    assert spoken == [_SENTENCE, _SENTENCE]
    assert model.calls == 0
    assert resolver.calls == 0
    await runtime.close()
