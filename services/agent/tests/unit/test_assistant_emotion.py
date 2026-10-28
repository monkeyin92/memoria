from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from services.agent.src import llm_types as llm
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.assistant_emotion import compose_tts_instruction
from services.agent.src.orchestration.phrase_segmenter import PhraseSegmenter
from services.agent.src.orchestration.prosody import (
    SpeechPlan,
    mascot_expression_for_reply,
    speech_plan_for_turn,
)
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.runtime_emotion import DuplexRuntimeEmotionMixin
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import (
    ScriptedChatModel,
    bind_owner_speaker,
    commit_media_turn,
)


def _fence(name: str = "emotion-tag") -> GenerationFence:
    return GenerationFence(name, 1, 1, 0)


def _plan(text: str = "今天星期三。") -> SpeechPlan:
    return speech_plan_for_turn(label="neutral", provider_label="neutral", text=text)


def test_emotion_tag_is_stripped_across_streaming_tokens() -> None:
    seg = PhraseSegmenter(_fence())
    parts: list[str] = []
    for token in ("[emo", "tion:ha", "ppy]你好呀。"):
        parts.extend(s.text for s in seg.push_token(token))
    parts.extend(s.text for s in seg.flush(end_of_stream=True))

    assert seg.assistant_emotion == "happy"
    assert "".join(parts) == "你好呀。"
    assert "[emotion" not in "".join(parts)


def test_invalid_emotion_tag_is_stripped_but_not_applied() -> None:
    seg = PhraseSegmenter(_fence())
    parts = [s.text for s in seg.push_token("[emotion:excited]你好。")]
    parts.extend(s.text for s in seg.flush(end_of_stream=True))

    assert seg.assistant_emotion is None
    assert "".join(parts) == "你好。"


def test_incomplete_tag_is_dropped_at_flush() -> None:
    seg = PhraseSegmenter(_fence())
    assert seg.push_token("[emotion:hap") == []
    assert seg.flush(end_of_stream=True) == []
    assert seg.assistant_emotion is None


def test_emotion_tag_resets_with_segmenter() -> None:
    seg = PhraseSegmenter(_fence())
    seg.push_token("[emotion:caring]你好。")
    assert seg.assistant_emotion == "caring"

    seg.reset(_fence("next-turn"))
    assert seg.assistant_emotion is None
    seg.push_token("再见。")
    assert seg.assistant_emotion is None


def test_plain_text_is_unchanged() -> None:
    seg = PhraseSegmenter(_fence())
    parts = [s.text for s in seg.push_token("今天星期三。")]
    parts.extend(s.text for s in seg.flush(end_of_stream=True))

    assert seg.assistant_emotion is None
    assert "".join(parts) == "今天星期三。"


@pytest.mark.parametrize(
    ("emotion", "expected"),
    [("happy", "happy"), ("caring", "caring"), ("curious", "curious")],
)
def test_declared_emotion_drives_mascot_expression(emotion: str, expected: str) -> None:
    plan = replace(_plan(), assistant_emotion=emotion)

    assert mascot_expression_for_reply(plan=plan, text="今天星期三。") == expected


def test_supportive_delivery_wins_over_declared_emotion() -> None:
    plan = speech_plan_for_turn(label="sad", provider_label="sad", text="我很难过。")
    plan = replace(plan, assistant_emotion="happy")

    assert mascot_expression_for_reply(plan=plan, text="我很难过。") == "caring"


def test_neutral_declared_emotion_keeps_text_heuristic() -> None:
    plan = replace(_plan(), assistant_emotion="neutral")

    assert mascot_expression_for_reply(plan=plan, text="太好了！") == "happy"


def test_compose_tts_instruction_appends_declared_emotion() -> None:
    assert compose_tts_instruction("", "happy") == "回复情绪轻快，带一点笑意"
    assert compose_tts_instruction("自然口语。", "caring") == "自然口语；回复情绪温柔关切"
    assert compose_tts_instruction("自然口语。", "neutral") == "自然口语"
    assert len(compose_tts_instruction("长" * 300, "happy")) <= 240


class _FakeRecords:
    def __init__(self, plan: SpeechPlan) -> None:
        self._plan = plan
        self.bound: SpeechPlan | None = None

    def speech_plan_for(self, fence: GenerationFence, default: SpeechPlan) -> SpeechPlan:
        return self.bound or self._plan

    def bind_speech_plan(self, fence: GenerationFence, plan: SpeechPlan) -> None:
        self.bound = plan


class _FakeRuntime(DuplexRuntimeEmotionMixin):
    def __init__(self, plan: SpeechPlan, records: _FakeRecords) -> None:
        self.speech_plan = plan
        self._generation_records = records  # type: ignore[assignment]
        self.tts_calls = 0

    def apply_speech_plan_to_tts(self, fence: GenerationFence) -> None:
        self.tts_calls += 1


def test_apply_assistant_emotion_rebinds_plan_and_tts() -> None:
    plan = _plan()
    records = _FakeRecords(plan)
    runtime = _FakeRuntime(plan, records)
    fence = _fence()

    runtime.apply_assistant_emotion(fence, "caring")

    assert records.bound is not None
    assert records.bound.assistant_emotion == "caring"
    assert "回复情绪温柔关切" in records.bound.tts_instruction
    assert runtime.tts_calls == 1

    runtime.apply_assistant_emotion(fence, "caring")
    assert runtime.tts_calls == 1


def test_supportive_plan_keeps_its_safety_instruction() -> None:
    plan = speech_plan_for_turn(label="sad", provider_label="sad", text="我很难过。")
    records = _FakeRecords(plan)
    runtime = _FakeRuntime(plan, records)

    runtime.apply_assistant_emotion(_fence(), "happy")

    assert records.bound is not None
    assert records.bound.assistant_emotion == "happy"
    assert records.bound.tts_instruction == plan.tts_instruction


@pytest.mark.asyncio
async def test_stream_reply_strips_tag_and_binds_plan() -> None:
    async def fake_llm(_ctx: Any, _tools: list[Any]) -> AsyncIterator[str]:
        yield "[emotion:happy]"
        yield "你好呀。"

    runtime = DuplexRuntime.create(session_id="emotion-e2e")
    await runtime.orchestrator.ready()
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
    pipeline = ReplyPipeline(instructions="test", runtime=runtime)
    pipeline.language_model = ScriptedChatModel(fake_llm)
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="你好")
    fence = await commit_media_turn(pipeline, "你好")

    spoken = [item async for item in pipeline.stream_reply(chat_ctx)]

    assert spoken == ["你好呀。"]
    plan = runtime.speech_plan_for_fence(fence)
    assert plan.assistant_emotion == "happy"
    assert "回复情绪轻快" in plan.tts_instruction
    await runtime.close()
