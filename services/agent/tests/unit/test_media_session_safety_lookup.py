"""A lookup cue must not take ownership from an approved safety response plan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from services.agent.src import llm_types as llm
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import ResponsePlanFetch
from services.agent.src.voice_core.media_session import MediaReplyChunk
from services.agent.tests.unit.agent_wiring_support import _plan_for_fence
from services.agent.tests.unit.test_media_session_golden import (
    _h5_identity,
    _Harness,
    _ScriptedProvider,
)
from services.common.fixed_safety_scripts import CRISIS_SELF_ESCALATION_SCRIPT, FixedSafetyScript


@pytest.mark.asyncio
@pytest.mark.parametrize("safety", [True, False], ids=["semantic-crisis", "ordinary-lookup"])
async def test_lookup_ownership_respects_prepared_safety_plan(safety: bool) -> None:
    query = "我已经准备行动了，先看看明天杭州天气怎么样" if safety else "明天杭州天气怎么样"
    weather = "杭州明天晴，二十度。"
    searches: list[str] = []
    spoken: list[str] = []

    class Planner:
        echoes_session_epoch = True

        async def fetch(self, **kwargs: Any) -> ResponsePlanFetch:
            plan = _plan_for_fence(kwargs["fence"], instructions="test", speaker_class="owner")
            if safety:
                plan = replace(plan, direct_text=CRISIS_SELF_ESCALATION_SCRIPT.display)
            return ResponsePlanFetch(plan, "ok")

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            searches.append(query)
            return weather

    class Provider(_ScriptedProvider):
        pipeline: ReplyPipeline

        async def prepare_committed_turn(self, _identity: Any, text: str) -> Any:
            return await self.pipeline.prepare_committed_turn(text)

        def fixed_safety_planned(self, fence: Any) -> bool:
            return self.pipeline.fixed_safety_planned(fence)

        async def start_delegation(self, text: str, fence: Any) -> Any:
            return await self.pipeline.resolve_media_delegation(text, fence)

        @staticmethod
        def accept_output_intent(intent: Any) -> Any:
            return intent

        async def generate_reply(
            self, _identity: Any, user_text: str, _fence: Any
        ) -> AsyncIterator[MediaReplyChunk]:
            chat = llm.ChatContext.empty()
            chat.add_message(role="user", content=user_text)
            async for item in self.pipeline.stream_reply(chat):
                text = item.display if isinstance(item, FixedSafetyScript) else str(item)
                spoken.append(text)
                yield MediaReplyChunk(
                    pcm_s16le=b"\x00\x00" * 480,
                    source_start_sample=0,
                    text=text,
                    first=True,
                    final=True,
                )

        async def generate_output(
            self,
            _identity: Any,
            intent: Any,
            _fence: Any,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            spoken.append(intent.tts_source)
            yield MediaReplyChunk(
                pcm_s16le=b"\x00\x00" * 480,
                source_start_sample=source_start_sample,
                text=intent.tts_source,
                first=True,
                final=True,
            )

    provider = Provider()
    harness = _Harness(_h5_identity(f"safety-lookup-{safety}"), provider, verified_owner=True)
    provider.pipeline = ReplyPipeline(
        instructions="test",
        runtime=harness.runtime,
        response_planner_client=Planner(),
        realtime_search_resolver=Resolver(),
    )
    await harness.start()
    try:
        await harness.utterance(query)
        await harness.wait_for(lambda: bool(spoken))
        await harness.settle()
        assert provider.pipeline.fixed_safety_planned(harness.runtime.fence) is safety
        assert harness.count("audio") > 0
        assert (
            harness.count(
                "generation",
                lambda event: (
                    event.reason == "user_turn_committed" and event.protected_playback is safety
                ),
            )
            == 1
        )
        if safety:
            assert spoken == [CRISIS_SELF_ESCALATION_SCRIPT.display]
            assert searches == []
        else:
            assert spoken == [weather]
            assert len(searches) == 1
    finally:
        await harness.close()
