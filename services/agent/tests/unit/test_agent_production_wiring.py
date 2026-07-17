from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from livekit.agents import FlushSentinel, StopResponse, llm
from livekit.agents.types import TimedString
from services.agent.src import agent as agent_mod
from services.agent.src.agent import DuplexVoiceAgent
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.state_machine import ConversationState


async def _text_source(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


@pytest.mark.asyncio
async def test_agent_llm_node_uses_heard_history_and_phrase_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("当前问题")
    runtime.orchestrator.context.commit_assistant_heard("实际听到的旧回复")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="旧问题")
    chat_ctx.add_message(role="assistant", content="未听到的完整旧回复")
    chat_ctx.add_message(role="user", content="当前问题")
    captured: dict[str, Any] = {}

    async def _stream() -> AsyncIterator[Any]:
        yield llm.ChatChunk(
            id="content",
            delta=llm.ChoiceDelta(content="可以，我先帮你看一下。"),
        )
        yield llm.ChatChunk(
            id="usage",
            usage=llm.CompletionUsage(
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
            ),
        )

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        tools: list[Any],
        settings: Any,
    ) -> AsyncIterator[Any]:
        captured.update(ctx=safe_ctx, tools=tools, settings=settings)
        return _stream()

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))
    output = [item async for item in agent.llm_node(chat_ctx, [], None)]

    assert [message.text_content for message in captured["ctx"].messages()] == [
        "旧问题",
        "实际听到的旧回复",
        "当前问题",
    ]
    assert "可以，我先帮你看一下。" in output
    assert not any(isinstance(item, FlushSentinel) for item in output)
    assert runtime.orchestrator.active_llm_task is None


@pytest.mark.asyncio
async def test_agent_adds_only_the_current_turn_delivery_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("帮我安排一个十五分钟的英语口语训练")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="user", content="帮我安排一个十五分钟的英语口语训练")
    captured: dict[str, Any] = {}

    async def fake_llm_node(
        _agent: Any,
        safe_ctx: Any,
        _tools: list[Any],
        _settings: Any,
    ) -> AsyncIterator[Any]:
        captured["ctx"] = safe_ctx
        return _text_source("嗯，好，我先理一下。")

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    assert [item async for item in agent.llm_node(chat_ctx, [], None)]
    messages = captured["ctx"].messages()
    assert messages[-1].role == "system"
    assert "短衔接" in messages[-1].text_content
    assert [message.text_content for message in chat_ctx.messages()] == [
        "帮我安排一个十五分钟的英语口语训练"
    ]


@pytest.mark.asyncio
async def test_agent_llm_node_drops_token_after_fence_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("问题")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        yield llm.ChatChunk(id="empty")
        yield "这段旧回答不应出现。"

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))
    stream = agent.llm_node(llm.ChatContext.empty(), [], None)
    first = await anext(stream)
    assert isinstance(first, llm.ChatChunk)
    await runtime.orchestrator.bump_tool_epoch_on_condition_change()
    assert [item async for item in stream] == []


@pytest.mark.asyncio
async def test_non_preemptive_turn_commits_fence_before_first_llm_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.orchestrator.ready()
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    class Message:
        def text_content(self) -> str:
            return "你好"

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        yield "你好，有什么可以帮你的吗？"

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    await agent.on_user_turn_completed(llm.ChatContext.empty(), Message())
    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

    assert runtime.fence.turn_id == 1
    assert runtime.fence.generation_id == 1
    assert "你好，有什么可以帮你的吗？" in output
    assert runtime.orchestrator.fence_gate is not None
    assert runtime.orchestrator.fence_gate.dropped_count == 0


@pytest.mark.asyncio
async def test_one_physical_speech_epoch_cannot_commit_more_than_once() -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    await runtime.orchestrator.ready()
    published: list[dict[str, object]] = []

    async def publish(event: dict[str, object]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    class Message:
        def __init__(self, text: str, *, anchored: bool) -> None:
            self._text = text
            self.metrics: dict[str, object] = (
                {"started_speaking_at": 1.0, "stopped_speaking_at": 2.0}
                if anchored
                else {}
            )

        def text_content(self) -> str:
            return self._text

    runtime.on_user_voice_started()
    await agent.on_user_turn_completed(
        llm.ChatContext.empty(),
        Message("第一句话", anchored=True),
    )

    runtime.on_user_voice_started()
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(
            llm.ChatContext.empty(),
            Message("迟到片段", anchored=False),
        )

    assert runtime.fence.turn_id == 1
    assert [turn.content for turn in runtime.orchestrator.context.turns] == ["第一句话"]

    runtime.on_user_voice_started()
    await agent.on_user_turn_completed(
        llm.ChatContext.empty(),
        Message("真正的新讲话", anchored=True),
    )
    await asyncio.sleep(0)
    assert runtime.fence.turn_id == 2
    assert [
        event["text"]
        for event in published
        if event.get("type") == "transcript_delta"
        and event.get("speaker") == "user"
        and event.get("final") is True
    ] == ["第一句话", "真正的新讲话"]


@pytest.mark.asyncio
async def test_post_playback_backchannel_is_ignored_as_echo_tail() -> None:
    runtime = DuplexRuntime.create()
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("介绍一下")
    runtime.update_pending_assistant_text("你好。")
    await runtime.on_playback_started()
    await runtime.on_assistant_reply_completed("你好。")
    fence = runtime.fence
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    class Message:
        def text_content(self) -> str:
            return "是。"

    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext.empty(), Message())

    assert runtime.fence.matches(fence)
    assert runtime.orchestrator.metrics.get(
        "guarded_user_input_total", {"reason": "backchannel"}
    ) == 1


@pytest.mark.asyncio
async def test_post_playback_english_assistant_echo_is_ignored() -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("教我英语")
    runtime.update_pending_assistant_text("It's a good idea.")
    await runtime.on_playback_started()
    await runtime.on_assistant_reply_completed("It's a good idea.")
    fence = runtime.fence
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    class Message:
        def text_content(self) -> str:
            return "it's"

    runtime.on_user_voice_started()
    runtime.observe_user_transcript("it's", final=True)
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext.empty(), Message())

    assert runtime.fence.matches(fence)
    assert runtime.orchestrator.metrics.get(
        "guarded_user_input_total", {"reason": "assistant_echo"}
    ) == 1


@pytest.mark.asyncio
async def test_production_echo_trace_is_quarantined_while_assistant_is_speaking() -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    await runtime.orchestrator.ready()
    await runtime.on_turn_committed("你充当我的英语培训师")
    runtime.update_pending_assistant_text(
        "好的，我们先说一句简单的英语，比如 Good morning, how are you?"
    )
    await runtime.on_playback_started()
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)
    fence = runtime.fence

    class Message:
        def __init__(self, text: str) -> None:
            self._text = text

        def text_content(self) -> str:
            return self._text

    for text in ("그.", "佢。", "啊！", "Good morning."):
        runtime.on_user_voice_started(now_ns=1_000_000_000)
        runtime.observe_user_transcript(text, final=True, now_ns=1_300_000_000)
        with pytest.raises(StopResponse):
            await agent.on_user_turn_completed(llm.ChatContext.empty(), Message(text))

    assert runtime.fence.matches(fence)
    assert [turn.content for turn in runtime.orchestrator.context.turns] == [
        "你充当我的英语培训师"
    ]
    assert runtime.orchestrator.metrics.get(
        "guarded_user_input_total", {"reason": "non_target_language"}
    ) == 2
    assert runtime.orchestrator.metrics.get(
        "guarded_user_input_total", {"reason": "backchannel"}
    ) == 1
    assert runtime.orchestrator.metrics.get(
        "guarded_user_input_total", {"reason": "assistant_echo"}
    ) == 1


@pytest.mark.asyncio
async def test_explicit_language_switch_allows_requested_script() -> None:
    runtime = DuplexRuntime.create(input_guard_enabled=True)
    await runtime.orchestrator.ready()
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    class Message:
        def __init__(self, text: str) -> None:
            self._text = text

        def text_content(self) -> str:
            return self._text

    await agent.on_user_turn_completed(llm.ChatContext.empty(), Message("请教我韩语"))
    runtime.update_pending_assistant_text("可以，我们开始。")
    await runtime.on_playback_started()
    runtime.on_user_voice_started(now_ns=1_000_000_000)
    decision = runtime.observe_user_transcript("그.", final=True, now_ns=1_300_000_000)

    assert decision == "accept"
    await runtime.on_real_interrupt(cause="accepted_test_input")
    await agent.on_user_turn_completed(llm.ChatContext.empty(), Message("그."))
    assert runtime.orchestrator.context.turns[-1].content == "그."


@pytest.mark.asyncio
async def test_reply_budget_stops_after_three_spoken_sentences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("介绍一下")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        for sentence in ("第一句。", "第二句。", "第三句。", "第四句。", "第五句。"):
            yield sentence

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

    assert output == ["第一句。", "第二句。", "第三句。"]


@pytest.mark.asyncio
async def test_reply_budget_truncates_an_oversized_first_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("详细介绍")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    async def fake_llm_node(*_args: Any) -> AsyncIterator[Any]:
        yield "这" * 140 + "。"

    monkeypatch.setattr(agent_mod.Agent.default, "llm_node", staticmethod(fake_llm_node))

    output = [item async for item in agent.llm_node(llm.ChatContext.empty(), [], None)]

    assert output
    assert sum(ch.isalnum() for text in output for ch in text) <= agent_mod.MAX_VOICE_REPLY_CHARS


@pytest.mark.asyncio
async def test_agent_tts_and_transcription_nodes_gate_and_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DuplexRuntime.create()
    await runtime.on_turn_committed("问题")
    agent = DuplexVoiceAgent(instructions="test", runtime=runtime)

    async def fake_tts_node(
        _agent: Any,
        text: AsyncIterator[str],
        _settings: Any,
    ) -> AsyncIterator[Any]:
        async def frames() -> AsyncIterator[Any]:
            async for _ in text:
                yield SimpleNamespace(data=b"")
                yield SimpleNamespace(data=b"\x01\x02")

        return frames()

    async def fake_transcription_node(
        _agent: Any,
        _text: AsyncIterator[Any],
        _settings: Any,
    ) -> AsyncIterator[Any]:
        yield TimedString("你", start_time=0.0, end_time=0.1)
        yield SimpleNamespace(text="坏", start_time=object(), end_time=object())
        yield "普通"

    monkeypatch.setattr(agent_mod.Agent.default, "tts_node", staticmethod(fake_tts_node))
    monkeypatch.setattr(
        agent_mod.Agent.default,
        "transcription_node",
        staticmethod(fake_transcription_node),
    )
    frames = [frame async for frame in agent.tts_node(_text_source("回答。"), None)]
    transcript = [delta async for delta in agent.transcription_node(_text_source("ignored"), None)]

    assert len(frames) == 2
    assert runtime.orchestrator.published_audio_generations == [runtime.fence.generation_id]
    assert runtime.heard_tracker.full_text == "回答。"
    assert [word.text for word in runtime.heard_tracker.words] == ["你"]
    assert len(transcript) == 3
    assert runtime.orchestrator.active_tts_task is None


def test_agent_helpers_prewarm_and_turn_handling_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert agent_mod._message_text("plain") == "plain"
    assert agent_mod._message_text(SimpleNamespace(content=["a", 1, "b"])) == "a\nb"
    assert agent_mod._message_text(SimpleNamespace(content=12)) == "12"
    assert "session.history" not in inspect.getsource(agent_mod.entrypoint)

    loaded: dict[str, Any] = {}

    def fake_load(**kwargs: Any) -> str:
        loaded.update(kwargs)
        return "vad"

    monkeypatch.setattr(agent_mod.silero.VAD, "load", fake_load)
    proc = SimpleNamespace(userdata={})
    agent_mod.prewarm(proc)
    assert proc.userdata["vad"] == "vad"
    assert loaded["min_silence_duration"] == 0.30

    monkeypatch.setenv("LIVEKIT_TURN_DETECTOR_VERSION", "v1-mini")
    options = agent_mod.build_turn_handling_options("livekit_cloud")
    assert options["interruption"]["mode"] == "adaptive"
    assert options["preemptive_generation"]["enabled"] is False
    assert (
        agent_mod.build_turn_handling_config("livekit_cloud")[
            "preemptive_generation"
        ]["enabled"]
        is False
    )
    monkeypatch.setenv("LIVEKIT_ADAPTIVE_INTERRUPTION", "false")
    options = agent_mod.build_turn_handling_options("livekit_cloud")
    assert options["interruption"]["mode"] == "vad"
    assert agent_mod.build_turn_handling_options("cn_self_hosted")["interruption"]["mode"] == "vad"

    def fail_options(_profile: str) -> Any:
        raise ValueError("bad api")

    monkeypatch.setattr(agent_mod, "build_turn_handling_options", fail_options)
    monkeypatch.setenv("ENVIRONMENT", "development")
    kwargs = agent_mod.build_session_kwargs(
        vad=None,
        stt="stt",
        llm="llm",
        tts="tts",
        profile="livekit_cloud",
        offline=False,
    )
    assert "turn_handling_config" in kwargs
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="bad api"):
        agent_mod.build_session_kwargs(
            vad=None,
            stt="stt",
            llm="llm",
            tts="tts",
            profile="livekit_cloud",
            offline=False,
        )


def test_cascade_audio_output_uses_high_quality_opus_bitrate() -> None:
    options = agent_mod.build_cascade_audio_output_options()

    assert options.sample_rate == 24000
    assert options.num_channels == 1
    assert options.track_publish_options.audio_encoding.max_bitrate == 64000


def test_self_hosted_turn_handling_filters_short_echoes_and_reads_timing_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "ENDPOINTING_MIN_DELAY_S",
        "ENDPOINTING_MAX_DELAY_S",
        "ENDPOINTING_ALPHA",
        "INTERRUPTION_MIN_DURATION_S",
        "FALSE_INTERRUPTION_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)

    options = agent_mod.build_turn_handling_options("cn_self_hosted")
    assert options["endpointing"] == {
        "mode": "dynamic",
        "min_delay": 1.50,
        "max_delay": 2.20,
        "alpha": 0.85,
    }
    # Production emitted a second FINAL about 0.5 s after the first EOU. Keep
    # both fragments inside one physical speech epoch instead of generating twice.
    assert options["endpointing"]["min_delay"] >= 0.90 + 0.50
    assert options["interruption"]["min_duration"] == 0.45
    assert options["interruption"]["min_words"] == 0
    assert options["interruption"]["false_interruption_timeout"] == 1.70
    assert (
        options["interruption"]["false_interruption_timeout"]
        >= options["endpointing"]["min_delay"]
    )
    assert agent_mod.build_turn_handling_config("cn_self_hosted")["interruption"] == options[
        "interruption"
    ]

    cloud = agent_mod.build_turn_handling_config("livekit_cloud")
    assert cloud["endpointing"]["min_delay"] == 0.30
    assert cloud["interruption"]["min_duration"] == 0.25
    assert cloud["interruption"]["min_words"] == 0
    assert cloud["interruption"]["false_interruption_timeout"] == 1.20

    monkeypatch.setenv("ENDPOINTING_MIN_DELAY_S", "0.61")
    monkeypatch.setenv("ENDPOINTING_MAX_DELAY_S", "2.40")
    monkeypatch.setenv("ENDPOINTING_ALPHA", "0.75")
    monkeypatch.setenv("INTERRUPTION_MIN_DURATION_S", "0.52")
    monkeypatch.setenv("FALSE_INTERRUPTION_TIMEOUT_S", "0.93")
    overridden = agent_mod.build_turn_handling_options("cn_self_hosted")
    assert overridden["endpointing"] == {
        "mode": "dynamic",
        "min_delay": 0.61,
        "max_delay": 2.40,
        "alpha": 0.75,
    }
    assert overridden["interruption"]["min_duration"] == 0.52
    assert overridden["interruption"]["false_interruption_timeout"] == 0.93


class _Emitter:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}

    def on(self, name: str, handler: Any) -> None:
        self.handlers.setdefault(name, []).append(handler)

    def off(self, name: str, handler: Any) -> None:
        self.handlers[name].remove(handler)

    def emit(self, name: str, event: Any) -> None:
        for handler in tuple(self.handlers.get(name, ())):
            handler(event)


class _FakeParticipant:
    def __init__(self) -> None:
        self.published: list[tuple[dict[str, Any], bool, str]] = []

    async def publish_data(self, payload: str, *, reliable: bool, topic: str) -> None:
        self.published.append((json.loads(payload), reliable, topic))


class _FakeRoom(_Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.name = "voice-public-session"
        self.local_participant = _FakeParticipant()


class _FakePool:
    def __init__(self) -> None:
        self.discarded: list[Any] = []

    async def warm(self) -> None:
        raise RuntimeError("warm unavailable")

    async def discard_active_connection(self, fence: Any) -> None:
        self.discarded.append(fence)


class _FakeTTS:
    def __init__(self) -> None:
        self.pool = _FakePool()
        self.bound: list[Any] = []

    def bind_fence(self, fence: Any) -> None:
        self.bound.append(fence)


class _FakeAudio(_Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.pause_count = 0
        self.resume_count = 0

    def pause(self) -> None:
        self.pause_count += 1

    def resume(self) -> None:
        self.resume_count += 1


class _RecordingInterruption(dict[str, Any]):
    def __init__(self) -> None:
        super().__init__(min_words=0)
        self.history: list[int] = []

    def __setitem__(self, key: str, value: Any) -> None:
        if key == "min_words":
            self.history.append(int(value))
        super().__setitem__(key, value)


class _FakeSession(_Emitter):
    last: _FakeSession | None = None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.kwargs = kwargs
        self.output = SimpleNamespace(audio=_FakeAudio())
        self.history = llm.ChatContext.empty()
        self.history.add_message(role="assistant", content="实际听到")
        self.started: tuple[Any, Any, Any] | None = None
        self.generated: list[str] = []
        self.interrupt_count = 0
        self.said: list[str] = []
        self.options = SimpleNamespace(interruption=_RecordingInterruption())
        _FakeSession.last = self

    async def start(self, *, room: Any, agent: Any, room_options: Any) -> None:
        self.started = (room, agent, room_options)

    def interrupt(self, *, force: bool = False) -> asyncio.Future[None]:
        self.interrupt_count += 1
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    def say(self, text: str, **_kwargs: Any) -> str:
        self.said.append(text)
        return text

    async def generate_reply(self, *, instructions: str) -> None:
        self.generated.append(instructions)


@pytest.mark.asyncio
async def test_entrypoint_routes_control_playback_and_ui_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent.src.providers import cosyvoice_tts, deepseek, funasr_stt

    fake_tts = _FakeTTS()
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "cn_self_hosted")
    monkeypatch.setenv("SPEAKER_VERIFY_ENABLED", "false")

    class FakeCosy:
        @classmethod
        def from_env(cls) -> _FakeTTS:
            return fake_tts

    class FakeFun:
        @classmethod
        def from_env(cls) -> str:
            return "stt"

    class FakeDeepConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeDeepClient:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(cosyvoice_tts, "CosyVoiceTTS", FakeCosy)
    monkeypatch.setattr(funasr_stt, "FunASRSTT", FakeFun)
    monkeypatch.setattr(deepseek, "DeepSeekConfig", FakeDeepConfig)
    monkeypatch.setattr(deepseek, "DeepSeekClient", FakeDeepClient)
    monkeypatch.setattr(agent_mod.openai, "LLM", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(agent_mod, "AgentSession", _FakeSession)
    monkeypatch.setattr(
        agent_mod,
        "build_session_kwargs",
        lambda **kwargs: {"turn_handling_config": {}, **kwargs},
    )

    room = _FakeRoom()
    shutdown_callbacks: list[Any] = []
    ctx = SimpleNamespace(
        room=room,
        proc=SimpleNamespace(userdata={"vad": "vad"}),
        connect=lambda: asyncio.sleep(0),
        add_shutdown_callback=shutdown_callbacks.append,
    )
    await agent_mod.entrypoint(ctx)
    session = _FakeSession.last
    assert session is not None and session.started is not None and session.generated
    assert any(event[0].get("state") == "ready" for event in room.local_participant.published)
    assert any(
        event[0].get("type") == "audio_trace"
        and event[0].get("name") == "audio_output_attached"
        for event in room.local_participant.published
    )
    await asyncio.sleep(0)
    runtime: DuplexRuntime = ctx.proc.userdata["duplex_runtime"]
    assert runtime.session_id == "public-session"

    session.emit("agent_state_changed", SimpleNamespace(new_state="thinking"))
    session.emit("user_input_transcribed", SimpleNamespace(transcript="你好", is_final=True))
    session.emit(
        "conversation_item_added",
        SimpleNamespace(item=SimpleNamespace(role="assistant", text_content="回复")),
    )
    session.emit("conversation_item_added", SimpleNamespace(item=SimpleNamespace(role="user")))
    assert session.options.interruption["min_words"] == 1000
    session.emit(
        "user_state_changed",
        SimpleNamespace(old_state="listening", new_state="speaking"),
    )
    assert session.options.interruption["min_words"] == 0
    await asyncio.sleep(0)

    await runtime.on_turn_committed("等待停止")
    room.emit("data_received", SimpleNamespace(topic="other", participant=None, data=b"{}"))
    room.emit(
        "data_received",
        SimpleNamespace(topic="voice-agent.control", participant=object(), data=b"{}"),
    )
    room.emit(
        "data_received",
        SimpleNamespace(topic="voice-agent.control", participant=None, data=b"not-json"),
    )
    room.emit(
        "data_received",
        SimpleNamespace(
            topic="voice-agent.control",
            participant=None,
            data=json.dumps({"type": "stop_response", "session_id": "wrong"}).encode(),
        ),
    )
    room.emit(
        "data_received",
        SimpleNamespace(
            topic="voice-agent.control",
            participant=None,
            data=json.dumps(
                {
                    "type": "stop_response",
                    "session_id": "public-session",
                    "reason": "user_button",
                }
            ).encode(),
        ),
    )
    await asyncio.sleep(0.02)
    assert session.interrupt_count == 1
    assert runtime.orchestrator.state is ConversationState.LISTENING

    before_recovery = runtime.fence
    room.emit(
        "data_received",
        SimpleNamespace(
            topic="voice-agent.control",
            participant=None,
            data=json.dumps(
                {"type": "rtc_recovered", "session_id": "public-session"}
            ).encode(),
        ),
    )
    await asyncio.sleep(0.02)
    assert session.interrupt_count == 2
    assert runtime.fence.generation_id == before_recovery.generation_id + 1

    await runtime.on_turn_committed("播放")
    runtime.update_pending_assistant_text("播放内容")
    assert session.output.audio is not None
    session.output.audio.emit("playback_started", SimpleNamespace())
    await asyncio.sleep(0.02)
    assert session.options.interruption["min_words"] == 1000
    session.emit(
        "user_state_changed",
        SimpleNamespace(old_state="listening", new_state="speaking"),
    )
    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript="그.", is_final=True),
    )
    await asyncio.sleep(0)
    assert session.output.audio.pause_count == 0
    assert session.output.audio.resume_count == 0
    assert any(
        event[0].get("type") == "assistant_audio"
        and event[0].get("action") == "duck"
        and event[0].get("gain") == 0.55
        for event in room.local_participant.published
    )
    assert any(
        event[0].get("type") == "assistant_audio"
        and event[0].get("action") == "restore"
        and event[0].get("gain") == 1.0
        for event in room.local_participant.published
    )
    assert 1000 in session.options.interruption.history
    assert session.options.interruption["min_words"] == 1000
    session.emit(
        "user_state_changed",
        SimpleNamespace(old_state="listening", new_state="speaking"),
    )
    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript="等等", is_final=True),
    )
    await asyncio.sleep(0)
    assert 0 in session.options.interruption.history
    assert session.options.interruption["min_words"] == 0
    session.output.audio.emit(
        "playback_finished",
        SimpleNamespace(
            playback_position=0.5,
            interrupted=False,
            synchronized_transcript="播放内容",
        ),
    )
    session.emit(
        "conversation_item_added",
        SimpleNamespace(
            item=SimpleNamespace(
                role="assistant",
                text_content="播放内容",
                interrupted=False,
            )
        ),
    )
    await asyncio.sleep(0.02)
    assert runtime.orchestrator.context.turns[-1].content == "播放内容"

    await runtime.on_turn_committed("会被语音中断")
    await session.interrupt(force=True)
    assert session.interrupt_count == 3
    assert runtime.orchestrator.state is ConversationState.USER_SPEAKING

    assert len(shutdown_callbacks) == 1
    await shutdown_callbacks[0]()
    assert room.handlers["data_received"] == []
