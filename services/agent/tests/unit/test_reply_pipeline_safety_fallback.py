"""Defense in depth: fence_mismatch fallback still speaks fixed crisis/support replies."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
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
from services.common.companion_response_safety import (
    AFTER_FIXED_SAFETY_SESSION_INSTRUCTION,
    NAMELESS_IDENTITY_REPLY,
)
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
        ("有个大哥哥打了我，还不让我说。", SUPPORT_VICTIM_SCRIPT),
        ("小明被他爸爸打了。", SUPPORT_RELAY_SCRIPT),
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
    ("query", "expected"),
    [
        ("我有点难过，不想活了。", CRISIS_SELF_SCRIPT),
        ("我想伤害我自己。", CRISIS_SELF_SCRIPT),
        ("我爸爸打我，好疼。", SUPPORT_VICTIM_SCRIPT),
        ("有个大哥哥打我，还不让我说。", SUPPORT_VICTIM_SCRIPT),
        ("我好朋友说她想伤害自己。", SUPPORT_RELAY_SCRIPT),
        ("我同学说他不想活了。", SUPPORT_RELAY_SCRIPT),
        ("他把我推倒了。", SUPPORT_VICTIM_SCRIPT),
    ],
)
async def test_invalid_planner_payload_still_speaks_the_routed_script(
    query: str, expected: FixedSafetyScript
) -> None:
    """Field 2026-10-21 D02/H03: 「我想伤害我自己」 fell back with no script."""

    runtime = _runtime("safety-invalid-payload")
    pipeline = _pipeline(runtime, _InvalidPayloadPlanner())  # type: ignore[arg-type]
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


def _heard(tokens: list[Any]) -> str:
    return "".join(token for token in tokens if isinstance(token, str))


def _system_text(chat_ctx: Any) -> str:
    return "\n".join(
        message.text_content for message in chat_ctx.items if message.role == "system"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "follow_up",
    ["我找不到我的橡皮了", "嗯嗯好的大人陪着你"],
)
async def test_ordinary_follow_up_after_crisis_is_a_brief_normal_reply(
    follow_up: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _runtime("post-safety-ordinary")
    pipeline = _pipeline(runtime, _InvalidPayloadPlanner())
    seen: list[Any] = []

    async def model(safe_ctx: Any, _tools: list[Any]) -> Any:
        seen.append(safe_ctx)

        async def one() -> Any:
            yield "橡皮可能在铅笔盒里。"

        return one()

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
    heard = _heard(spoken)
    assert heard == "橡皮可能在铅笔盒里。"
    assert CRISIS_SELF_SCRIPT.display not in heard
    assert "刚才的事别担心" not in heard
    assert seen
    system = _system_text(seen[0])
    assert AFTER_FIXED_SAFETY_SESSION_INSTRUCTION in system
    assert "一两句短话" in AFTER_FIXED_SAFETY_SESSION_INSTRUCTION
    assert follow_up not in "\n".join(record.getMessage() for record in caplog.records)
    await runtime.close()


_SOOTHING_FOLLOW_UP_PHRASES = (
    "刚才的事别担心",
    "别害怕",
    "不要怕",
    "没事的",
    "没关系",
    "别担心",
    "不用担心",
    "会好起来的",
    "一切都会好的",
    "我陪着你",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", _SOOTHING_FOLLOW_UP_PHRASES)
async def test_ordinary_follow_up_drops_a_repeated_script_and_a_dilution_line(
    phrase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _runtime("post-safety-dilution")
    pipeline = _pipeline(runtime, _InvalidPayloadPlanner())

    async def model(_safe_ctx: Any, _tools: list[Any]) -> Any:
        async def one() -> Any:
            yield CRISIS_SELF_SCRIPT.display
            yield f"{phrase}。橡皮可能在铅笔盒里。"

        return one()

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

    follow_up = "我找不到我的橡皮了"
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
    heard = _heard(spoken)
    assert heard == "橡皮可能在铅笔盒里。"
    assert CRISIS_SELF_SCRIPT.display not in heard
    assert phrase not in heard
    assert follow_up not in "\n".join(record.getMessage() for record in caplog.records)
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", _SOOTHING_FOLLOW_UP_PHRASES)
async def test_ordinary_chat_keeps_a_soothing_phrase(phrase: str, caplog: pytest.LogCaptureFixture) -> None:
    runtime = _runtime("ordinary-soothing")
    pipeline = _pipeline(runtime, _InvalidPayloadPlanner())
    reply = f"{phrase}。橡皮可能在铅笔盒里。"

    async def model(_safe_ctx: Any, _tools: list[Any]) -> Any:
        async def one() -> Any:
            yield reply

        return one()

    pipeline.language_model = ScriptedChatModel(model)
    query = "我找不到我的橡皮了"
    with caplog.at_level(logging.INFO):
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
    assert _heard(spoken) == reply
    assert query not in "\n".join(record.getMessage() for record in caplog.records)
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", _SOOTHING_FOLLOW_UP_PHRASES)
async def test_soothing_phrase_on_the_turn_after_the_follow_up_stays(
    phrase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _runtime("post-safety-later")
    pipeline = _pipeline(runtime, _InvalidPayloadPlanner())

    async def calm(_safe_ctx: Any, _tools: list[Any]) -> Any:
        async def one() -> Any:
            yield "橡皮可能在铅笔盒里。"

        return one()

    pipeline.language_model = ScriptedChatModel(calm)
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

    second = await pipeline.prepare_committed_turn("我找不到我的橡皮了")
    ordinary = [
        token
        async for token in pipeline.stream(
            LanguageModelRequest(
                user_text="我找不到我的橡皮了",
                cancellation=CancellationContext.capture(second),
            )
        )
    ]
    assert _heard(ordinary) == "橡皮可能在铅笔盒里。"

    reply = f"{phrase}。橡皮可能在铅笔盒里。"

    async def later(_safe_ctx: Any, _tools: list[Any]) -> Any:
        async def one() -> Any:
            yield CRISIS_SELF_SCRIPT.display
            yield reply

        return one()

    pipeline.language_model = ScriptedChatModel(later)
    follow_up = "橡皮是红色的"
    with caplog.at_level(logging.INFO):
        third = await pipeline.prepare_committed_turn(follow_up)
        spoken = [
            token
            async for token in pipeline.stream(
                LanguageModelRequest(
                    user_text=follow_up,
                    cancellation=CancellationContext.capture(third),
                )
            )
        ]
    heard = _heard(spoken)
    assert heard == reply
    assert CRISIS_SELF_SCRIPT.display not in heard
    assert follow_up not in "\n".join(record.getMessage() for record in caplog.records)
    await runtime.close()


@pytest.mark.asyncio
async def test_another_crisis_script_drops_soothing_on_the_following_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _runtime("post-safety-rearm")
    pipeline = _pipeline(runtime, _InvalidPayloadPlanner())

    async def calm(_safe_ctx: Any, _tools: list[Any]) -> Any:
        async def one() -> Any:
            yield "橡皮可能在铅笔盒里。"

        return one()

    pipeline.language_model = ScriptedChatModel(calm)
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
    second = await pipeline.prepare_committed_turn("我找不到我的橡皮了")
    ordinary = [
        token
        async for token in pipeline.stream(
            LanguageModelRequest(
                user_text="我找不到我的橡皮了",
                cancellation=CancellationContext.capture(second),
            )
        )
    ]
    assert _heard(ordinary) == "橡皮可能在铅笔盒里。"

    async def soothing(_safe_ctx: Any, _tools: list[Any]) -> Any:
        async def one() -> Any:
            yield "别害怕。橡皮可能在铅笔盒里。"

        return one()

    pipeline.language_model = ScriptedChatModel(soothing)
    crisis_again = "有个叔叔老是大我。"
    third = await pipeline.prepare_committed_turn(crisis_again)
    spoken_again = [
        token
        async for token in pipeline.stream(
            LanguageModelRequest(
                user_text=crisis_again,
                cancellation=CancellationContext.capture(third),
            )
        )
    ]
    assert spoken_again == [SUPPORT_VICTIM_SCRIPT]

    follow_up = "我的铅笔盒是蓝色的"
    with caplog.at_level(logging.INFO):
        fourth = await pipeline.prepare_committed_turn(follow_up)
        spoken = [
            token
            async for token in pipeline.stream(
                LanguageModelRequest(
                    user_text=follow_up,
                    cancellation=CancellationContext.capture(fourth),
                )
            )
        ]
    assert _heard(spoken) == "橡皮可能在铅笔盒里。"
    assert follow_up not in "\n".join(record.getMessage() for record in caplog.records)
    await runtime.close()


@pytest.mark.asyncio
async def test_ordinary_follow_up_stays_brief_and_a_story_request_stays_normal() -> None:
    runtime = _runtime("post-safety-brief")
    pipeline = _pipeline(runtime, _InvalidPayloadPlanner())
    long_reply = "第一句。第二句。第三句。第四句。"

    async def model(_safe_ctx: Any, _tools: list[Any]) -> Any:
        async def one() -> Any:
            yield long_reply

        return one()

    pipeline.language_model = ScriptedChatModel(model)
    first = await pipeline.prepare_committed_turn("我想伤害自己。")
    async for _ in pipeline.stream(
        LanguageModelRequest(
            user_text="我想伤害自己。",
            cancellation=CancellationContext.capture(first),
        )
    ):
        pass

    second = await pipeline.prepare_committed_turn("我找不到我的橡皮了")
    ordinary = [
        token
        async for token in pipeline.stream(
            LanguageModelRequest(
                user_text="我找不到我的橡皮了",
                cancellation=CancellationContext.capture(second),
            )
        )
    ]
    assert _heard(ordinary) == "第一句。第二句。"

    async def story(_safe_ctx: Any, _tools: list[Any]) -> Any:
        async def one() -> Any:
            yield long_reply

        return one()

    pipeline.language_model = ScriptedChatModel(story)
    third = await pipeline.prepare_committed_turn("给我讲一个故事吧")
    spoken_story = [
        token
        async for token in pipeline.stream(
            LanguageModelRequest(
                user_text="给我讲一个故事吧",
                cancellation=CancellationContext.capture(third),
            )
        )
    ]
    assert _heard(spoken_story) == long_reply
    await runtime.close()


@pytest.mark.asyncio
async def test_identity_follow_up_after_crisis_keeps_the_fixed_identity_line() -> None:
    runtime = _runtime("post-safety-identity")
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

    second = await pipeline.prepare_committed_turn("你叫什么名字")
    spoken = [
        token
        async for token in pipeline.stream(
            LanguageModelRequest(
                user_text="你叫什么名字",
                cancellation=CancellationContext.capture(second),
            )
        )
    ]
    heard = _heard(spoken)
    assert heard == NAMELESS_IDENTITY_REPLY
    assert CRISIS_SELF_SCRIPT.display not in heard
    assert "刚才的事别担心" not in heard
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


class _StaleClassifierPlanner:
    """A control-api still on the pre-#346 rules, as on prod 2026-10-21."""

    echoes_session_epoch = True

    def __init__(self, direct_text: str | None) -> None:
        self._direct_text = direct_text

    async def fetch(self, **kwargs: Any) -> ResponsePlanFetch:
        plan = replace(_companion_chat_plan(kwargs["fence"]), direct_text=self._direct_text)
        return ResponsePlanFetch(plan, "ok")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "planned", "expected"),
    [
        ("我有点难过，不想活了。", SUPPORT_RELAY_SCRIPT, CRISIS_SELF_SCRIPT),
        ("我有点难过不想活了", SUPPORT_RELAY_SCRIPT, CRISIS_SELF_SCRIPT),
        ("我有点难过,不想活了.", SUPPORT_RELAY_SCRIPT, CRISIS_SELF_SCRIPT),
        ("我爸爸打我，好疼。", None, SUPPORT_VICTIM_SCRIPT),
        ("我爸爸打我 好疼", None, SUPPORT_VICTIM_SCRIPT),
        ("有个大哥哥打我，还不让我说。", None, SUPPORT_VICTIM_SCRIPT),
        ("有个大哥哥打我。还不让我说。", None, SUPPORT_VICTIM_SCRIPT),
        ("我想伤害我自己。", None, CRISIS_SELF_SCRIPT),
        ("我想伤害我自己!", None, CRISIS_SELF_SCRIPT),
        ("我好朋友说她想伤害自己。", None, SUPPORT_RELAY_SCRIPT),
        ("我好朋友说,她想伤害自己", None, SUPPORT_RELAY_SCRIPT),
        ("我好朋友说她想伤害自己。", CRISIS_SELF_SCRIPT, CRISIS_SELF_SCRIPT),
    ],
)
async def test_planner_plan_never_speaks_less_protection_than_the_local_crisis_rules(
    query: str,
    planned: FixedSafetyScript | None,
    expected: FixedSafetyScript,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    runtime = _runtime("stale-planner-crisis")
    planner = _StaleClassifierPlanner(None if planned is None else planned.display)
    pipeline = _pipeline(runtime, planner)  # type: ignore[arg-type]
    fence = await pipeline.prepare_committed_turn(query)
    cached = pipeline.response_plans.get(fence)
    assert cached is not None
    assert cached.fixed_safety_script == expected
    assert cached.fixed_safety_script.template_id == expected.template_id
    assert runtime.fixed_safety_echo_text == expected.display
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
    raised = [r.getMessage() for r in caplog.records if "crisis_route_raised" in r.getMessage()]
    if planned == expected:
        assert raised == []
    else:
        assert len(raised) == 1
        assert f"local={expected.template_id}" in raised[0]
        assert query not in raised[0]
    await runtime.close()
