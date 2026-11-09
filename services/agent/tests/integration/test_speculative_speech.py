"""The first phrase of a speculative reply is synthesized before the commit, and only its own turn hears it.

2026-11-01 device soaks: on an adopted turn the first phrase reached Doubao after the commit and its first PCM
came 0.3-0.45 s later, while the speculative reply often had that phrase in hand before the commit was done.
These run the real Doubao client against a local binary-protocol server that answers every task request with
audio only that text gets.  They pin that an adopted, unchanged turn plays the session opened ahead with exactly
the items the old path produces, that any difference opens a fresh session as before, and that the audio of a
discarded session reaches no reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import replace
from typing import Any, cast

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.providers.doubao_tts import DOUBAO_TTS_MODEL, DoubaoTTS, DoubaoTTSConfig
from services.agent.src.providers.doubao_voice_catalog import catalog_by_id
from services.agent.src.providers.fixed_safety_audio import (
    FixedSafetyTTS,
    shipped_approved_catalog,
)
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import ContextPrefetchFetch, ResponsePlanFetch
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.agent.tests.integration.mock_servers import MockDoubaoServer, task_pcm
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import bind_owner_speaker
from services.speaker.domain import DEVICE_BOUND_SUBJECT_REASON

_VOICES = catalog_by_id()
SYSTEM_PROMPT = "【不可变安全底线】不要输出 Markdown。【服务模式】孩子：一到两句短话。"
QUESTION = "你还记得我今天画的什么吗"
OTHER = "我们明天去公园好不好"
FIRST = "第0次的回答。"
PLAN = {"rate": 1.0, "instruction": "", "pitch": 0, "reference_contexts": ()}


def _answer(index: int) -> list[str]:
    return [f"第{index}次", "的回答。", "我们一起想想吧。"]


class _Model:
    """Reply model double: the ``n``-th request is answered with ``_answer(n)`` at once."""

    def __init__(self) -> None:
        self.requests = 0

    @contextlib.asynccontextmanager
    async def chat(self, *, chat_ctx: Any, tools: list[Any]) -> AsyncIterator[Any]:
        index = self.requests
        self.requests += 1

        async def tokens() -> AsyncGenerator[str, None]:
            for token in _answer(index):
                yield token

        stream = tokens()
        try:
            yield stream
        finally:
            await stream.aclose()


class _Planner:
    """A device fence's plan verdict (the local safe plan answers) and no memory."""

    async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
        return ResponsePlanFetch(None, "fence_mismatch")

    async def prefetch_context(self, **_kwargs: object) -> ContextPrefetchFetch:
        return ContextPrefetchFetch((), None, None, "ok")


def _config(server: MockDoubaoServer, **overrides: object) -> DoubaoTTSConfig:
    values: dict[str, object] = {
        "api_key": "test",
        "ws_url": server.ws_url,
        "speaker": _VOICES["warm_companion"].speaker_id,
        "pool_size": 2,
    }
    values.update(overrides)
    return DoubaoTTSConfig(**values)  # type: ignore[arg-type]


@contextlib.asynccontextmanager
async def _doubao(
    **overrides: object,
) -> AsyncIterator[tuple[MockDoubaoServer, DoubaoTTS, FixedSafetyTTS]]:
    server = MockDoubaoServer(scenario="stream_tasks")
    server.start()
    inner = DoubaoTTS(_config(server, **overrides))
    try:
        await inner.pool.warm()
        yield server, inner, FixedSafetyTTS(inner, shipped_approved_catalog())
    finally:
        await inner.aclose()
        server.stop()


async def _until(condition: Any) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


async def _drain(stream: Any) -> tuple[bytes, list[str]]:
    pcm = b"".join([event.frame.data async for event in stream])
    return pcm, [str(word) for word in stream.timed_transcript()]


async def _speak(
    tts: FixedSafetyTTS, fence: GenerationFence, phrases: list[str]
) -> tuple[bytes, list[str]]:
    """What the media adapter does with a reply: take the head start or open a stream, then send every phrase."""

    tts.bind_fence(fence)
    stream = tts.take_presynthesis(fence, phrases[0])
    rest = phrases[1:] if stream is not None else phrases
    if stream is None:
        stream = tts.stream()
    for phrase in rest:
        stream.push_text(phrase)
    stream.end_input()
    try:
        return await _drain(stream)
    finally:
        await stream.aclose()


def _traces(tts: DoubaoTTS) -> list[str]:
    """The audio stages the runtime hears from ``tts``, in order."""

    traces: list[str] = []
    tts.set_trace_callback(lambda name, _status, _detail: traces.append(name))
    return traces


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[str]:
    prefix = "speculative speech session_id="
    return [
        record.getMessage().split("outcome=", 1)[1].split(" ", 1)[0]
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]


@pytest.mark.asyncio
async def test_an_adopted_head_start_is_heard_exactly_as_a_fresh_session_would_be(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.providers.doubao_presynthesis")
    fence = GenerationFence("head-start", 2, 2, 0)
    phrases = [FIRST, "我们一起想想吧。"]
    heard: dict[bool, tuple[bytes, list[str]]] = {}
    for head_start in (False, True):
        async with _doubao() as (server, inner, tts):
            traces = _traces(inner)
            if head_start:
                pending = tts.presynthesize(fence, FIRST, **PLAN)
                assert pending is not None
                await _until(lambda: server.chunk_count == 1)
                assert traces == [], "the session's audio stages wait for the turn that takes it"
                pending.adopt()
            inner.apply_speech_plan(emotion="neutral", fence=fence, **PLAN)
            heard[head_start] = await _speak(tts, fence, phrases)
            assert server.sessions == 1 and server.task_requests == [phrases]
            assert traces.count("doubao_first_pcm") == 1

    assert heard[True] == heard[False]
    assert heard[True][0] == task_pcm(phrases[0]) + task_pcm(phrases[1])
    assert _outcomes(caplog) == ["used"]
    assert FIRST not in caplog.text, "the reply is never logged"


def _other_text(inner: DoubaoTTS, fence: GenerationFence) -> list[str]:
    inner.apply_speech_plan(emotion="neutral", fence=fence, **PLAN)
    return ["第0次的另一种回答。"]


def _other_voice(inner: DoubaoTTS, fence: GenerationFence) -> list[str]:
    calm = _VOICES["calm_guide"]
    inner.apply_voice_profile(
        model=DOUBAO_TTS_MODEL, voice=calm.speaker_id, profile_id=calm.profile_id
    )
    inner.apply_speech_plan(emotion="neutral", fence=fence, **PLAN)
    return [FIRST]


def _other_rate(inner: DoubaoTTS, fence: GenerationFence) -> list[str]:
    inner.apply_speech_plan(emotion="neutral", fence=fence, **{**PLAN, "rate": 0.9})
    return [FIRST]


def _other_volume(inner: DoubaoTTS, fence: GenerationFence) -> list[str]:
    inner.apply_speech_plan(emotion="neutral", fence=fence, **{**PLAN, "pitch": 32})
    return [FIRST]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        (_other_text, "other_text"),
        (_other_voice, "other_config"),
        (_other_rate, "other_config"),
        (_other_volume, "other_config"),
    ],
)
async def test_any_difference_cancels_the_head_start_and_the_turn_opens_its_own_session(
    caplog: pytest.LogCaptureFixture, change: Any, outcome: str
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.providers.doubao_presynthesis")
    fence = GenerationFence("head-start-differs", 2, 2, 0)
    async with _doubao() as (server, inner, tts):
        pending = tts.presynthesize(fence, FIRST, **PLAN)
        assert pending is not None
        await _until(lambda: server.chunk_count == 1)
        pending.adopt()
        phrases = change(inner, fence)

        heard = await _speak(tts, fence, phrases)

        assert heard[0] == task_pcm(phrases[0]), "only the turn's own session is heard"
        assert server.sessions == 2 and server.task_requests == [[FIRST], phrases]
        assert _outcomes(caplog) == [outcome]


@pytest.mark.asyncio
async def test_a_head_start_for_a_reply_that_was_not_adopted_is_never_heard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.providers.doubao_presynthesis")
    fence = GenerationFence("head-start-not-adopted", 2, 2, 0)
    async with _doubao() as (server, inner, tts):
        assert tts.presynthesize(fence, FIRST, **PLAN) is not None
        await _until(lambda: server.chunk_count == 1)
        inner.apply_speech_plan(emotion="neutral", fence=fence, **PLAN)

        heard = await _speak(tts, fence, [FIRST])

        assert heard[0] == task_pcm(FIRST) and server.sessions == 2
        assert _outcomes(caplog) == ["not_adopted"]


@pytest.mark.asyncio
async def test_a_head_start_for_another_generation_is_never_heard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.providers.doubao_presynthesis")
    fence = GenerationFence("head-start-fence", 2, 2, 0)
    async with _doubao() as (server, inner, tts):
        pending = tts.presynthesize(fence, FIRST, **PLAN)
        assert pending is not None
        pending.adopt()
        inner.apply_speech_plan(emotion="neutral", fence=fence, **PLAN)
        later = replace(fence, tool_epoch=1)
        inner.apply_speech_plan(emotion="neutral", fence=later, **PLAN)

        assert tts.take_presynthesis(later, FIRST) is None, "another generation leaves it alone"
        inner.bind_fence(later)
        assert tts.take_presynthesis(fence, FIRST) is None, (
            "the provider would open the other one now"
        )
        assert _outcomes(caplog) == ["other_fence"]


@pytest.mark.asyncio
async def test_style_context_is_part_of_what_must_match() -> None:
    fence = GenerationFence("head-start-style", 2, 2, 0)
    styled = {
        **PLAN,
        "instruction": "整体开心、轻快。",
        "reference_contexts": ("用户：我画了小狗",),
    }
    async with _doubao(style_control_enabled=True) as (server, inner, tts):
        pending = tts.presynthesize(fence, FIRST, **styled)
        assert pending is not None
        await _until(lambda: server.chunk_count == 1)
        pending.adopt()
        inner.apply_speech_plan(
            emotion="neutral", fence=fence, **{**styled, "instruction": "整体温柔、关切。"}
        )

        await _speak(tts, fence, [FIRST])

        assert server.sessions == 2
        contexts = [params["context_texts"] for params in server.start_session_params]
        assert "温柔" in contexts[1][0] and "开心" not in contexts[1][0]


@pytest.mark.asyncio
async def test_approved_safety_copy_is_never_sent_to_doubao_ahead_of_its_turn() -> None:
    template = shipped_approved_catalog().templates[0]
    fence = GenerationFence("head-start-safety", 2, 2, 0)
    async with _doubao() as (server, _inner, tts):
        first_sentence = template.display.split("。", 1)[0] + "。"

        assert tts.presynthesize(fence, first_sentence, **PLAN) is None
        await asyncio.sleep(0.05)
        assert server.sessions == 0


@pytest.mark.asyncio
async def test_a_discarded_head_start_closes_its_session_and_no_later_reply_hears_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.providers.doubao_presynthesis")
    fence = GenerationFence("head-start-discarded", 2, 2, 0)
    async with _doubao() as (server, inner, tts):
        pending = tts.presynthesize(fence, FIRST, **PLAN)
        assert pending is not None
        await _until(lambda: server.chunk_count == 1)
        pending.discard("other_request")
        await _until(lambda: len(server.canceled_sessions) == 1)

        later = replace(fence, turn_id=3, generation_id=3)
        inner.apply_speech_plan(emotion="neutral", fence=later, **PLAN)
        heard = await _speak(tts, later, ["第1次的回答。"])

        assert heard[0] == task_pcm("第1次的回答。")
        assert server.task_requests == [[FIRST], ["第1次的回答。"]]
        assert _outcomes(caplog) == ["other_request"]


def _runtime(name: str, tts: Any) -> DuplexRuntime:
    runtime = DuplexRuntime.create(session_id=name, tts=tts)
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


async def _let_tasks_run() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def _turn(
    agent: ReplyPipeline, adapter: ExistingVoiceProviderAdapter, text: str
) -> list[Any]:
    """Commit ``text`` the way the media session does and collect what the device would receive."""

    fence = await agent.prepare_committed_turn(text)
    identity = SessionIdentity(fence.session_id, stream_epoch=1)
    return [chunk async for chunk in adapter.generate_reply(identity, text, fence)]


async def _announce(agent: ReplyPipeline, text: str) -> None:
    agent.warm_committed_turn(text)
    await _let_tasks_run()
    agent.start_committed_turn_plan(text)
    await _let_tasks_run()


def _pipeline(
    runtime: DuplexRuntime, *, speech: bool
) -> tuple[ReplyPipeline, ExistingVoiceProviderAdapter]:
    agent = ReplyPipeline(
        instructions=SYSTEM_PROMPT,
        runtime=runtime,
        response_planner_client=cast(Any, _Planner()),
        speculative_speech=speech,
    )
    agent.language_model = _Model()
    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: None),
        language_model=cast(Any, agent),
        speech_synthesis=cast(Any, runtime.tts),
    )
    return agent, adapter


def _device_view(chunks: list[Any]) -> list[tuple[Any, ...]]:
    return [
        (
            chunk.pcm_s16le,
            chunk.source_start_sample,
            chunk.assistant_text_delta,
            chunk.first,
            chunk.final,
            chunk.text_spans,
        )
        for chunk in chunks
    ]


@pytest.mark.asyncio
async def test_an_unchanged_turn_sends_the_device_what_the_old_path_sends(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.providers.doubao_presynthesis")
    received: dict[bool, list[Any]] = {}
    sessions_before_commit: dict[bool, int] = {}
    for speech in (False, True):
        async with _doubao() as (server, _inner, tts):
            runtime = _runtime("speculative-speech-unchanged", tts)
            agent, adapter = _pipeline(runtime, speech=speech)
            await _announce(agent, QUESTION)
            await asyncio.sleep(0.1)
            sessions_before_commit[speech] = server.sessions
            received[speech] = await _turn(agent, adapter, QUESTION)
            assert server.sessions == 1
            await runtime.close()

    assert sessions_before_commit == {False: 0, True: 1}, (
        "the head start opened its session before the commit"
    )
    assert _device_view(received[True]) == _device_view(received[False])
    assert b"".join(chunk.pcm_s16le for chunk in received[True]).startswith(task_pcm(FIRST))
    assert _outcomes(caplog) == ["used"]


@pytest.mark.asyncio
async def test_a_reply_to_another_sentence_never_carries_the_discarded_head_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.src.providers.doubao_presynthesis")
    async with _doubao() as (server, _inner, tts):
        runtime = _runtime("speculative-speech-other-sentence", tts)
        agent, adapter = _pipeline(runtime, speech=True)
        await _announce(agent, QUESTION)
        await _until(lambda: server.chunk_count == 1)

        chunks = await _turn(agent, adapter, OTHER)

        pcm = b"".join(chunk.pcm_s16le for chunk in chunks).rstrip(b"\x00")
        assert pcm == task_pcm("第1次的回答。") + task_pcm("我们一起想想吧。")
        assert task_pcm(FIRST)[:2] not in pcm
        assert server.task_requests == [[FIRST], ["第1次的回答。", "我们一起想想吧。"]]
        assert _outcomes(caplog) == ["other_text"]
        await runtime.close()
