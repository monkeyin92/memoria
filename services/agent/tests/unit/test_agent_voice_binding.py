"""Agent production wiring: plan fetch, generation voice binding and runtime lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src import llm_types as llm
from services.agent.src.agent_voice_profile import bind_generation_tts_voice
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import (
    DuplexRuntime,
)
from services.agent.src.llm_types import StopResponse
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import (
    ResponsePlanFetch,
)
from services.agent.tests.unit.agent_wiring_support import (
    _plan_for_fence,
    _text_source,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import (
    ScriptedChatModel,
    commit_media_turn,
)
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


@pytest.mark.asyncio
async def test_agent_fetches_response_plan_once_per_committed_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    bind_owner_policy(runtime)

    class Message:
        def __init__(self, text: str) -> None:
            self._text = text

        def text_content(self) -> str:
            return self._text

    fetch_calls: list[dict[str, object]] = []

    async def classify(_pcm: bytes, _sample_rate: int) -> Any:
        return SpeakerDecision(
            classification="owner",
            score=0.98,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="campplus-test",
            template_version=1,
            profile_id="profile-owner-001",
            permissions=permissions_for_speaker("owner"),
        )

    class ResponsePlannerStub:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            fetch_calls.append(dict(kwargs))
            fence = kwargs["fence"]
            assert isinstance(fence, GenerationFence)
            return ResponsePlanFetch(
                plan=_plan_for_fence(
                    fence,
                    instructions="【控制计划】只使用当前已确认上下文。",
                ),
                reason="ok",
            )

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=ResponsePlannerStub(),  # type: ignore[arg-type]
    )
    agent.language_model = ScriptedChatModel(lambda *_args: _text_source("好。"))  # type: ignore[arg-type]

    await commit_media_turn(agent, Message("当前问题"))

    assert len(fetch_calls) == 1
    assert fetch_calls[0]["session_id"] == runtime.session_id
    assert fetch_calls[0]["query"] == "当前问题"
    assert fetch_calls[0]["fence"] == runtime.fence
    assert runtime.response_provenance_for(runtime.fence) is None
    assert [item async for item in agent.stream_reply(llm.ChatContext.empty())]
    assert runtime.response_provenance_for(runtime.fence) is not None


@pytest.mark.asyncio
async def test_companion_voice_turn_reaches_llm_when_guest_filter_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="companion-voice-turn")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=False, owner_evidence=False, tools=False, voice_profile=False, shadow_low_sensitivity_persona=False)
    runtime.set_reject_non_owner_voice(False)
    runtime.set_target_speaker_focus(True)
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="warm_companion",
        current_model="seed-tts-2.0",
        current_voice="zh_female_shaoergushi_uranus_bigtts",
        current_voice_kind="designed",
        bind_fence=lambda _fence: None,
    )

    class Message:
        def text_content(self) -> str:
            return "你好"

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="uncertain",
            score=0.42,
            quality_score=0.95,
            reason_code="shadow_guest_candidate",
            model_version="campplus-test",
            template_version=1,
            profile_id=None,
            permissions=permissions_for_speaker("uncertain"),
        )

    class ResponsePlannerStub:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            fence = kwargs["fence"]
            assert isinstance(fence, GenerationFence)
            return ResponsePlanFetch(
                plan=_plan_for_fence(
                    fence,
                    instructions="只回答当前问题。",
                    speaker_class="uncertain",
                ),
                reason="ok",
            )

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=ResponsePlannerStub(),  # type: ignore[arg-type]
        llm_provider="qwen",
        llm_model="qwen-plus",
        tts_provider="doubao",
        tts_model="seed-tts-2.0",
    )
    agent.language_model = ScriptedChatModel(lambda *_args: _text_source("你好，我在。"))

    await commit_media_turn(agent, Message())
    output = [item async for item in agent.stream_reply(llm.ChatContext.empty())]

    assert output
    assert runtime.generation_voice_for(runtime.fence) is not None
    assert runtime.response_provenance_for(runtime.fence) is not None


@pytest.mark.asyncio
async def test_agent_drops_response_plan_when_fence_changes_during_fetch() -> None:
    runtime = DuplexRuntime.create()
    bind_owner_policy(runtime)

    class Message:
        def text_content(self) -> str:
            return "会过期的问题"

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="owner",
            score=0.98,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="campplus-test",
            template_version=1,
            profile_id="profile-owner-001",
            permissions=permissions_for_speaker("owner"),
        )

    fetch_calls = 0

    class ResponsePlannerStub:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            nonlocal fetch_calls
            fetch_calls += 1
            requested_fence = kwargs["fence"]
            assert isinstance(requested_fence, GenerationFence)
            await runtime.orchestrator.bump_tool_epoch_on_condition_change()
            return ResponsePlanFetch(
                plan=_plan_for_fence(
                    requested_fence,
                    instructions="这份计划已经过期。",
                ),
                reason="ok",
            )

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=ResponsePlannerStub(),  # type: ignore[arg-type]
    )

    with pytest.raises(StopResponse):
        await commit_media_turn(agent, Message())

    # The request sent ahead of the commit saw the epoch move and was not used; the commit's own one is
    # dropped as well, and neither is asked again.
    assert fetch_calls == 2
    assert len(agent.response_plans) == 0


@pytest.mark.asyncio
async def test_planner_failure_fallback_is_current_turn_only_and_disables_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="fallback-current-only")
    bind_owner_policy(runtime, policy_version="test-policy", private_context=True, owner_evidence=True, tools=True, voice_profile=False, shadow_low_sensitivity_persona=False)

    class Message:
        def text_content(self) -> str:
            return "只回答现在这个问题"

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="owner",
            score=0.98,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="campplus-test",
            template_version=1,
            profile_id="profile-owner-001",
            permissions=permissions_for_speaker("owner"),
        )

    class ResponsePlannerStub:
        async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
            return ResponsePlanFetch(plan=None, reason="http_503")

    captured: dict[str, Any] = {}

    async def fake_llm_node(
        safe_ctx: Any,
        tools: list[Any],
    ) -> AsyncIterator[str]:
        captured.update(ctx=safe_ctx, tools=tools)
        yield "安全回答。"

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=ResponsePlannerStub(),  # type: ignore[arg-type]
    )
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="旧私人问题：保险号码是多少？")
    chat_ctx.add_message(role="assistant", content="旧私人回答：号码是 123456。")
    chat_ctx.add_message(role="user", content="只回答现在这个问题")
    agent.language_model = ScriptedChatModel(fake_llm_node)

    await commit_media_turn(agent, Message())
    assert [item async for item in agent.stream_reply(chat_ctx)] == [
        "安全回答。"
    ]

    conversation = [
        (message.role, message.text_content)
        for message in captured["ctx"].items
        if message.role != "system"
    ]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].items if message.role == "system"
    )
    assert conversation == [("user", "只回答现在这个问题")]
    assert "不得读取、引用或推断历史对话" in system_text
    assert "123456" not in system_text
    assert captured["tools"] == []
    provenance = runtime.response_provenance_for(runtime.fence)
    assert provenance is not None
    assert provenance["source_refs"] == []


@pytest.mark.asyncio
async def test_agent_uses_direct_response_text_without_calling_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnreadableContext:
        def copy(self) -> None:
            raise AssertionError("direct response must not assemble chat history")

    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("直接回答")
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="【控制计划】",
        direct_text="可以，直接说这一句。",
    ))
    called = False

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        nonlocal called
        called = True
        yield "不应触发"

    agent.language_model = ScriptedChatModel(fake_llm_node)

    output = [item async for item in agent.stream_reply(UnreadableContext())]

    assert called is False
    assert "".join(str(item) for item in output) == "可以，直接说这一句。"
    assert runtime.response_provenance_for(runtime.fence) is not None


@pytest.mark.asyncio
async def test_cascade_response_fails_closed_without_generation_voice_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("必须带声音来源")

    class TTSWithoutBoundVoice:
        def bind_fence(self, _fence: GenerationFence) -> None:
            pass

    runtime.tts = TTSWithoutBoundVoice()
    agent = ReplyPipeline(instructions="test", runtime=runtime)
    agent.response_plans.store(_plan_for_fence(
        runtime.fence,
        instructions="仅生成有完整审计来源的回答。",
    ))
    called = False

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        nonlocal called
        called = True
        yield "不应播放"

    agent.language_model = ScriptedChatModel(fake_llm_node)

    output = [item async for item in agent.stream_reply(llm.ChatContext.empty())]

    assert output == []
    assert called is False
    assert runtime.generation_voice_for(runtime.fence) is None
    assert runtime.response_provenance_for(runtime.fence) is None


@pytest.mark.asyncio
async def test_legacy_turn_stops_before_planning_when_generation_voice_cannot_bind() -> None:
    runtime = DuplexRuntime.create(session_id="legacy-bind-failure")
    runtime.set_mode_policy(
        ModePolicy(
            mode="legacy",
            policy_version="s9-v1",
            companion_style_id=None,
            style_version=None,
            references=(
                ("fallback_voice_profile_id", "bright_peer"),
                ("fallback_voice_provider", "volcengine_doubao"),
                ("fallback_voice_model", "seed-tts-2.0"),
                ("fallback_voice_resource_id", "seed-tts-2.0"),
                ("legacy_voice_allowed", False),
            ),
            capabilities=(("conversation", True),),
            companion_style=None,
        )
    )
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="wrong-designed-profile",
        current_model="seed-tts-2.0",
        current_voice="wrong-speaker",
        current_voice_kind="designed",
        bind_fence=lambda _fence: None,
    )

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="owner",
            score=0.98,
            quality_score=0.95,
            reason_code="owner_match",
            model_version="campplus-test",
            template_version=1,
            profile_id="profile-owner-001",
            permissions=permissions_for_speaker("owner"),
        )

    class PlannerStub:
        called = False

        async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
            self.called = True
            raise AssertionError("voice binding must fail before response planning")

    class Message:
        def text_content(self) -> str:
            return "请继续说。"

    planner = PlannerStub()
    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=planner,  # type: ignore[arg-type]
    )

    with pytest.raises(StopResponse):
        await commit_media_turn(agent, Message())

    assert planner.called is False
    assert runtime.generation_voice_for(runtime.fence) is None


@pytest.mark.asyncio
async def test_live_lookup_does_not_start_when_generation_voice_cannot_bind() -> None:
    runtime = DuplexRuntime.create(session_id="lookup-voice-bind")
    runtime.set_mode_policy(
        ModePolicy(
            mode="legacy",
            policy_version="s9-v1",
            companion_style_id=None,
            style_version=None,
            references=(
                ("fallback_voice_profile_id", "not-approved"),
                ("fallback_voice_provider", "volcengine_doubao"),
                ("fallback_voice_model", "seed-tts-2.0"),
                ("fallback_voice_resource_id", "seed-tts-2.0"),
                ("legacy_voice_allowed", False),
            ),
            capabilities=(("conversation", True),),
            companion_style=None,
        )
    )
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="wrong-designed-profile",
        current_model="seed-tts-2.0",
        current_voice="wrong-speaker",
        current_voice_kind="designed",
        bind_fence=lambda _fence: None,
    )
    started: list[str] = []

    async def starter(text: str, _fence: GenerationFence) -> None:
        started.append(text)

    agent = ReplyPipeline(instructions="test", runtime=runtime)
    runtime.set_delegation_starter(starter)
    with pytest.raises(StopResponse):
        await agent.prepare_turn(
            text="今天南京天气怎么样",
            speaker=SimpleNamespace(classification="owner"),
            input_modality="audio",
        )
    assert started == []
    assert runtime.generation_voice_for(runtime.fence) is None
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_close_is_terminal_and_leaves_no_delegation_task() -> None:
    """P2-04: close() must drain what the close path itself can schedule.

    The pending ``interaction-delegation-start`` task seen at test exit came
    from work scheduled while the runtime was shutting down. close() is now
    terminal (a late callback is refused instead of leaked) and it drains the
    background set once more after the orchestrator finished closing.
    """
    runtime = DuplexRuntime.create(session_id="close-terminal")
    bind_owner_policy(
        runtime,
        policy_version="test-policy",
        private_context=False,
        owner_evidence=False,
        tools=False,
        voice_profile=False,
        shadow_low_sensitivity_persona=False,
    )
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="warm_companion",
        current_model="seed-tts-2.0",
        current_voice="zh_female_shaoergushi_uranus_bigtts",
        current_voice_kind="designed",
        bind_fence=lambda _fence: None,
    )

    async def starter(_text: str, _fence: GenerationFence) -> None:
        await asyncio.sleep(30)

    class Planner:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            fence = kwargs["fence"]
            assert isinstance(fence, GenerationFence)
            return ResponsePlanFetch(
                _plan_for_fence(fence, instructions="先查天气再回答。"),
                "ok",
            )

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
    )
    runtime.set_delegation_starter(starter)
    await agent.prepare_turn(
        text="今天南京天气怎么样",
        speaker=SimpleNamespace(classification="owner"),
        input_modality="audio",
    )

    await runtime.close()
    await asyncio.sleep(0)

    leaked = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name() == "interaction-delegation-start"
        and not task.done()
    ]
    assert leaked == [], "close() 不得留下委托任务"

    # 关闭后再调度不会启动新工作，也不会留下未回收的任务。
    refused = runtime._spawn(_never_finishes(), name="after-close")
    await asyncio.sleep(0)
    assert refused.done()
    await runtime.close()


async def _never_finishes() -> None:
    await asyncio.sleep(30)


@pytest.mark.asyncio
async def test_live_lookup_starts_on_the_bound_generation() -> None:
    runtime = DuplexRuntime.create(session_id="lookup-voice-ok")
    bind_owner_policy(
        runtime,
        policy_version="test-policy",
        private_context=False,
        owner_evidence=False,
        tools=False,
        voice_profile=False,
        shadow_low_sensitivity_persona=False,
    )
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="warm_companion",
        current_model="seed-tts-2.0",
        current_voice="zh_female_shaoergushi_uranus_bigtts",
        current_voice_kind="designed",
        bind_fence=lambda _fence: None,
    )
    started: list[GenerationFence] = []

    async def starter(_text: str, fence: GenerationFence) -> None:
        started.append(fence)

    class Planner:
        async def fetch(self, **kwargs: object) -> ResponsePlanFetch:
            fence = kwargs["fence"]
            assert isinstance(fence, GenerationFence)
            return ResponsePlanFetch(
                _plan_for_fence(fence, instructions="先查天气再回答。"),
                "ok",
            )

    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        response_planner_client=Planner(),  # type: ignore[arg-type]
    )
    runtime.set_delegation_starter(starter)
    fence = await agent.prepare_turn(
        text="今天南京天气怎么样",
        speaker=SimpleNamespace(classification="owner"),
        input_modality="audio",
    )
    await asyncio.sleep(0)  # the commit spawned the starter
    assert started == [fence]
    assert runtime.generation_voice_for(fence) is not None
    await runtime.close()


def test_companion_realigns_wrong_designed_tts_before_binding() -> None:
    runtime = DuplexRuntime.create(session_id="companion-realign-tts")
    bind_owner_policy(
        runtime,
        policy_version="test-policy",
        private_context=False,
        owner_evidence=False,
        tools=False,
        voice_profile=False,
        shadow_low_sensitivity_persona=False,
    )

    class AligningTTS:
        current_voice_profile_id = "bright_peer"
        current_model = "seed-tts-2.0"
        current_voice = "zh_female_shaoergushi_uranus_bigtts"
        current_voice_kind = "designed"

        def bind_fence(self, _fence: GenerationFence) -> None:
            return None

        def apply_voice_profile(self, **kwargs: object) -> None:
            self.current_model = str(kwargs["model"])
            self.current_voice = str(kwargs["voice"])
            self.current_voice_profile_id = str(kwargs["profile_id"])
            self.current_voice_kind = str(kwargs["voice_kind"])

    tts = AligningTTS()
    runtime.tts = tts
    assert bind_generation_tts_voice(runtime, runtime.fence)
    assert tts.current_voice_profile_id == "warm_companion"
    snapshot = runtime.generation_voice_for(runtime.fence)
    assert snapshot is not None
    assert snapshot.profile_id == "warm_companion"


def test_companion_frozen_cosyvoice_clone_binds_without_voice_clone_use() -> None:
    import hashlib

    voice_id = "cosyvoice-v3.5-flash-clone-owner001"
    speaker_sha256 = hashlib.sha256(voice_id.encode()).hexdigest()
    runtime = DuplexRuntime.create(session_id="companion-cosyvoice-clone-bind")
    bind_owner_policy(
        runtime,
        policy_version="test-policy",
        private_context=False,
        owner_evidence=False,
        tools=False,
        voice_profile=False,
        shadow_low_sensitivity_persona=False,
        include_voice_clone=False,
    )
    runtime.set_mode_policy(
        replace(
            runtime.mode_policy,
            companion_style_id="taoxi",
            references=tuple(
                sorted(
                    {
                        "voice_profile_id": "voice-profile-personal",
                        "voice_profile_version": "2",
                        "voice_provider": "alibaba_model_studio",
                        "voice_model": "cosyvoice-v3.5-flash",
                        "voice_resource_id": "cosyvoice-v3.5-flash",
                        "voice_speaker_sha256": speaker_sha256,
                        "fallback_voice_profile_id": "bright_peer",
                        "fallback_voice_provider": "volcengine_doubao",
                        "fallback_voice_model": "seed-tts-2.0",
                        "fallback_voice_resource_id": "seed-tts-2.0",
                    }.items()
                )
            ),
        )
    )
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="voice-profile-personal",
        current_model="cosyvoice-v3.5-flash",
        current_voice=voice_id,
        current_voice_kind="personal",
        apply_voice_profile=lambda **_kwargs: None,
        use_baseline_voice=lambda: None,
    )
    assert bind_generation_tts_voice(runtime, runtime.fence)
    snapshot = runtime.generation_voice_for(runtime.fence)
    assert snapshot is not None
    assert snapshot.profile_id == "voice-profile-personal"
    assert snapshot.resource_id == "cosyvoice-v3.5-flash"
    assert snapshot.voice_kind == "personal"


@pytest.mark.asyncio
async def test_identity_rotation_still_binds_companion_generation_voice() -> None:
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        TEST_VERIFY_KEY,
        canonical_wire_payload,
    )

    runtime = DuplexRuntime.create(session_id="rotate-companion-voice")
    bind_owner_policy(
        runtime,
        policy_version="test-policy",
        private_context=True,
        owner_evidence=True,
        tools=False,
        voice_profile=False,
        shadow_low_sensitivity_persona=False,
    )
    runtime.orchestrator.runtime_profiles.verify_key = TEST_VERIFY_KEY
    switched = canonical_wire_payload(
        session_id="rotate-companion-voice",
        runtime_profile_id="rp_switched_voice",
        active_subject_id="person_parent",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        service_mode="adult_companion",
        session_epoch=2,
        capabilities=["chat", "memory_recall_private"],
        persona={
            "persona_id": "person_parent",
            "version": 4,
            "relationship_stage": "familiar",
        },
    )
    applied = runtime.apply_runtime_profile(switched)
    assert applied is not None
    assert runtime.mode_policy.companion_style_id == "starlight"
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="warm_companion",
        current_model="seed-tts-2.0",
        current_voice="zh_female_shaoergushi_uranus_bigtts",
        current_voice_kind="designed",
        bind_fence=lambda _fence: None,
    )
    assert bind_generation_tts_voice(runtime, runtime.fence)
    snapshot = runtime.generation_voice_for(runtime.fence)
    assert snapshot is not None
    assert snapshot.profile_id == "warm_companion"


def test_self_preview_selected_fallback_binds_exact_generation_voice_snapshot() -> None:
    runtime = DuplexRuntime.create(session_id="self-preview-selected-fallback")
    runtime.set_mode_policy(
        ModePolicy(
            mode="self_preview",
            policy_version="s8-v1",
            companion_style_id=None,
            style_version=None,
            references=tuple(
                sorted(
                    {
                        "fallback_voice_profile_id": "bright_peer",
                        "fallback_voice_provider": "volcengine_doubao",
                        "fallback_voice_model": "seed-tts-2.0",
                        "fallback_voice_resource_id": "seed-tts-2.0",
                    }.items()
                )
            ),
            capabilities=(),
            companion_style=None,
        )
    )
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="bright_peer",
        current_model="seed-tts-2.0",
        current_voice="zh_female_shaoergushi_uranus_bigtts",
        current_voice_kind="designed",
    )
    assert bind_generation_tts_voice(runtime, runtime.fence)
    snapshot = runtime.generation_voice_for(runtime.fence)
    assert snapshot is not None
    assert snapshot.profile_id == "bright_peer"
    assert snapshot.resource_id == "seed-tts-2.0"


def test_unknown_safe_audio_turn_binds_anonymous_approved_baseline_voice() -> None:
    runtime = DuplexRuntime.create(session_id="unknown-safe-baseline")
    runtime.set_mode_policy(ModePolicy.degraded_unknown_safe())
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="warm_companion",
        current_model="seed-tts-2.0",
        current_voice="zh_female_shaoergushi_uranus_bigtts",
        current_voice_kind="designed",
    )
    assert bind_generation_tts_voice(runtime, runtime.fence)
    snapshot = runtime.generation_voice_for(runtime.fence)
    assert snapshot is not None
    assert snapshot.profile_id is None
    assert snapshot.resource_id == "seed-tts-2.0"
    assert snapshot.voice_kind == "designed"


@pytest.mark.asyncio
async def test_unknown_safe_audio_turn_reaches_llm_with_current_public_turn_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="unknown-safe-public-turn")
    runtime.set_mode_policy(ModePolicy.degraded_unknown_safe())
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="warm_companion",
        current_model="seed-tts-2.0",
        current_voice="zh_female_shaoergushi_uranus_bigtts",
        current_voice_kind="designed",
        bind_fence=lambda _fence: None,
    )

    class Message:
        def text_content(self) -> str:
            return "请简单介绍一下你自己"

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="uncertain",
            score=0.0,
            quality_score=0.0,
            reason_code="authority_unavailable",
            model_version="unavailable",
            template_version=None,
            profile_id=None,
            permissions=permissions_for_speaker("uncertain"),
        )

    captured: dict[str, Any] = {}

    async def fake_llm_node(
        safe_ctx: Any,
        tools: list[Any],
    ) -> AsyncIterator[str]:
        captured.update(ctx=safe_ctx, tools=tools)
        yield "你好，我是一个人工智能机器人伙伴。"

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        llm_provider="qwen",
        llm_model="qwen-plus",
        tts_provider="doubao",
        tts_model="seed-tts-2.0",
    )
    agent.language_model = ScriptedChatModel(fake_llm_node)
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="旧私人问题：我的保险号码是什么？")
    chat_ctx.add_message(role="assistant", content="旧私人回答：号码是 123456。")
    chat_ctx.add_message(role="user", content="请简单介绍一下你自己")

    await commit_media_turn(agent, Message())
    output = [item async for item in agent.stream_reply(chat_ctx)]

    assert output == ["你好，我是一个人工智能机器人伙伴。"]
    conversation = [
        (message.role, message.text_content)
        for message in captured["ctx"].items
        if message.role != "system"
    ]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].items if message.role == "system"
    )
    assert conversation == [("user", "请简单介绍一下你自己")]
    assert "仅依据当前用户这一轮" in system_text
    assert "123456" not in system_text
    assert captured["tools"] == []
    voice = runtime.generation_voice_for(runtime.fence)
    assert voice is not None
    assert voice.profile_id is None
    provenance = runtime.response_provenance_for(runtime.fence)
    assert provenance is not None
    assert provenance["interaction_mode"] == "unknown_safe"
    assert provenance["actual_voice_profile_id"] is None
    assert provenance["source_refs"] == []


@pytest.mark.asyncio
async def test_unknown_safe_followup_keeps_this_session_public_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create(session_id="unknown-safe-nanjing-followup")
    runtime.set_mode_policy(ModePolicy.degraded_unknown_safe())
    runtime.tts = SimpleNamespace(
        current_voice_profile_id="warm_companion",
        current_model="seed-tts-2.0",
        current_voice="zh_female_shaoergushi_uranus_bigtts",
        current_voice_kind="designed",
        bind_fence=lambda _fence: None,
    )
    runtime.orchestrator.context.add_user("南京今天天气怎么样", speaker_scope="public")
    runtime.orchestrator.context.commit_assistant_heard(
        "南京今天多云，气温二十二度。",
        speaker_scope="public",
    )

    class Message:
        def text_content(self) -> str:
            return "今天适合去哪儿玩"

    async def classify(_pcm: bytes, _sample_rate: int) -> SpeakerDecision:
        return SpeakerDecision(
            classification="uncertain",
            score=0.0,
            quality_score=0.0,
            reason_code="authority_unavailable",
            model_version="unavailable",
            template_version=None,
            profile_id=None,
            permissions=permissions_for_speaker("uncertain"),
        )

    captured: dict[str, Any] = {}

    async def fake_llm_node(
        safe_ctx: Any,
        tools: list[Any],
    ) -> AsyncIterator[str]:
        captured["ctx"] = safe_ctx
        captured["tools"] = tools
        yield "你刚问了南京天气。如果还在南京，今天比较适合室内。你是在南京吗？"

    runtime.set_speaker_classifier(classify, sample_rate=16_000)
    runtime.on_user_voice_started()
    runtime.feed_speaker_pcm(b"\x01\x00" * 800)
    runtime.on_user_voice_stopped()
    agent = ReplyPipeline(
        instructions="test",
        runtime=runtime,
        llm_provider="qwen",
        llm_model="qwen-plus",
        tts_provider="doubao",
        tts_model="seed-tts-2.0",
    )
    agent.language_model = ScriptedChatModel(fake_llm_node)
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="旧私人问题：我的保险号码是什么？")
    chat_ctx.add_message(role="assistant", content="旧私人回答：号码是 123456。")
    chat_ctx.add_message(role="user", content="今天适合去哪儿玩")

    await commit_media_turn(agent, Message())
    output = [item async for item in agent.stream_reply(chat_ctx)]

    assert "".join(output) == "你刚问了南京天气。如果还在南京，今天比较适合室内。你是在南京吗？"
    conversation = [
        (message.role, message.text_content)
        for message in captured["ctx"].items
        if message.role != "system"
    ]
    system_text = "\n".join(
        message.text_content for message in captured["ctx"].items if message.role == "system"
    )
    assert conversation == [
        ("user", "南京今天天气怎么样"),
        ("assistant", "南京今天多云，气温二十二度。"),
        ("user", "今天适合去哪儿玩"),
    ]
    assert "本次会话内已经对用户公开说过的内容" in system_text
    assert "123456" not in system_text
    assert captured["tools"] == []
    await runtime.close()
