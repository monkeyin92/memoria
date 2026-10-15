"""Defense in depth: fence_mismatch fallback still speaks fixed crisis/support replies."""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest
from services.agent.src import response_plan_sidecar
from services.agent.src.contracts.ids import CancellationContext
from services.agent.src.duplex_runtime import POST_PLAYBACK_ECHO_GUARD_MS, DuplexRuntime
from services.agent.src.orchestration.handlers import LanguageModelRequest
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import (
    ResponsePlan,
    ResponsePlanFetch,
    ResponseProvenance,
    ResponseVoiceTarget,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import (
    ScriptedChatModel,
    bind_owner_speaker,
    set_floor,
)
from services.common.companion_response_safety import NAMELESS_IDENTITY_REPLY
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    FixedSafetyScript,
)
from services.common.llm_failover import LLM_UNAVAILABLE_TEXT


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
@pytest.mark.parametrize(
    "follow_up",
    ["我找不到我的橡皮了", "嗯嗯好的大人陪着你", "你叫什么名字"],
)
async def test_after_fixed_safety_plan_failure_speaks_neutral_line(
    follow_up: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed plan after a fixed safety script must not leave the child in silence
    and must not open a free chat reply. The spoken line is the existing
    llm_unavailable phrase.
    """

    runtime = _runtime("post-safety-neutral")
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
        second = await pipeline.prepare_committed_turn(follow_up)
        spoken = [
            token
            async for token in pipeline.stream(
                LanguageModelRequest(
                    user_text=follow_up,
                    cancellation=CancellationContext.capture(second),
                )
            )
        ]
    heard = "".join(token for token in spoken if isinstance(token, str))
    assert heard == LLM_UNAVAILABLE_TEXT
    assert "模型不该开口。" not in heard
    assert NAMELESS_IDENTITY_REPLY not in heard
    assert any(
        "post_fixed_safety_neutral_reply" in record.getMessage()
        and "request_or_payload_invalid" in record.getMessage()
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


def _companion_chat_plan(fence: Any) -> ResponsePlan:
    return ResponsePlan(
        fence=fence,
        instructions="按当前控制计划自然回答。",
        direct_text=None,
        epistemic_status="not_applicable",
        epistemic_reason_codes=("test",),
        grounded_items=(),
        disclosures=(),
        voice_target=ResponseVoiceTarget(
            kind="companion",
            profile_id="warm_companion",
            model="seed-tts-2.0",
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
            speaker_reason_code="owner_match",
            speaker_profile_id="owner",
            speaker_model_version="test",
            speaker_template_version=1,
            source_refs=(),
            epistemic_status="not_applicable",
            epistemic_reason_codes=("test",),
            disclosures=(),
        ),
    )


class _ParkChatPlanner:
    echoes_session_epoch = True

    async def fetch(self, **kwargs: Any) -> ResponsePlanFetch:
        query = str(kwargs["query"])
        if query == "我们去公园玩吧":
            return ResponsePlanFetch(_companion_chat_plan(kwargs["fence"]), "ok")
        return ResponsePlanFetch(None, "request_or_payload_invalid")


@pytest.mark.asyncio
async def test_after_fixed_safety_a_real_plan_still_speaks_chat() -> None:
    runtime = _runtime("post-safety-real-plan")

    async def model(_safe_ctx: Any, _tools: list[Any]) -> Any:
        async def one() -> Any:
            yield "公园很好玩。"

        return one()

    pipeline = _pipeline(runtime, _ParkChatPlanner())
    pipeline.language_model = ScriptedChatModel(model)
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

    second = await pipeline.prepare_committed_turn("我们去公园玩吧")
    spoken = [
        token
        async for token in pipeline.stream(
            LanguageModelRequest(
                user_text="我们去公园玩吧",
                cancellation=CancellationContext.capture(second),
            )
        )
    ]
    assert "".join(spoken) == "公园很好玩。"
    await runtime.close()


@pytest.mark.asyncio
async def test_fixed_safety_script_echo_stays_rejected_after_the_guard_window() -> None:
    runtime = _runtime("post-safety-echo")
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
    aged = time.monotonic_ns() - (POST_PLAYBACK_ECHO_GUARD_MS + 1_000) * 1_000_000
    set_floor(
        runtime,
        played_assistant_text=CRISIS_SELF_SCRIPT.display,
        last_playback_completed_ns=aged,
        assistant_speaking=False,
    )
    echo = "你说的话很重要"
    accepted, reason = runtime.accept_user_turn(
        echo,
        input_modality="audio",
        speech_anchored=True,
        canonical_speech_epoch=runtime._speaker_epoch,
    )
    assert accepted is False
    assert reason == "assistant_echo"

    set_floor(runtime, played_assistant_text="今天天气不错呀", last_playback_completed_ns=aged)
    accepted, reason = runtime.accept_user_turn(
        echo,
        input_modality="audio",
        speech_anchored=True,
        canonical_speech_epoch=runtime._speaker_epoch,
    )
    assert accepted is False
    assert reason == "assistant_echo"

    accepted, reason = runtime.accept_user_turn(
        "我们去公园玩吧",
        input_modality="audio",
        speech_anchored=True,
        canonical_speech_epoch=runtime._speaker_epoch,
    )
    assert accepted is True
    assert reason is None
    await runtime.close()
