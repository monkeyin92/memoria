"""The model sees the same spoken bounds that its reply will be fitted to."""

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from services.agent.src import llm_types as llm
from services.agent.src.agent import plan_matches_mode_policy
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.tests.unit.production_wiring_support import _policy, _profile, _runtime
from services.agent.tests.unit.runtime_state_helpers import ScriptedChatModel, bind_owner_speaker
from services.agent.tests.unit.test_interaction_mode_agent import _plan
from services.common.companions import designed_voice_speaker_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("query,mode,chars,sentences", [
    ("请详细介绍太阳系的八颗行星，每颗三句话。", "student_minor", 300, 10),
    ("天空为什么是蓝色的？", "student_minor", 110, 4),
    ("请详细介绍太阳系的八颗行星，每颗三句话。", "adult_companion", 560, 12),
    ("给我讲一个海底探险的长故事，里面要有小海龟和发光的鱼。", "student_minor", 300, 10),
])
async def test_generation_request_exposes_its_actual_spoken_bounds(
    query: str, mode: str, chars: int, sentences: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(monkeypatch)
    runtime.set_mode_policy(_policy(_profile(
        epoch=1, subject="person_child" if mode == "student_minor" else "person_parent", mode=mode,
    )))
    bind_owner_speaker(runtime)
    await runtime.on_turn_committed(query)
    agent = ReplyPipeline(instructions="test", runtime=runtime, tts_model="seed-tts-2.0")
    template = _plan(runtime)
    plan = replace(
        template, provenance=replace(template.provenance, mode_policy_version=runtime.mode_policy.policy_version),
    )
    assert plan_matches_mode_policy(plan, runtime.mode_policy, tts_model="seed-tts-2.0")
    agent.cache_response_plan(plan)
    assert agent.response_plan_authorized(plan)
    assert plan.voice_target.profile_id is not None
    assert runtime.bind_generation_voice(
        runtime.fence, profile_id=plan.voice_target.profile_id,
        resource_id=plan.voice_target.model, voice_kind="designed",
        speaker_sha256=designed_voice_speaker_sha256(plan.voice_target.profile_id) or "",
    )
    requests: list[str] = []
    user_questions: list[str] = []

    async def model(ctx: Any, *_args: Any) -> AsyncIterator[str]:
        requests.append("\n".join(m.text_content or "" for m in ctx.items if m.role == "system"))
        user_questions.extend(m.text_content or "" for m in ctx.items if m.role == "user")
        for _ in range(14):
            yield "一个简短要点。"

    agent.language_model = ScriptedChatModel(model)
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content=query)
    output = [item async for item in agent.stream_reply(chat_ctx)]
    assert len(requests) == 1
    assert query in user_questions
    assert f"最多{chars}字、{sentences}句" in requests[0]
    assert "先覆盖每个要点" in requests[0]
    assert "不承诺未讲出的内容" in requests[0]
    assert len(output) == sentences
    assert sum(ch.isalnum() for part in output for ch in part) <= chars
