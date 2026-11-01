"""Weather follow-ups through the device path: Volcengine ASR results, the
media timeline, the turn commit and the delegated lookup.

Field 2026-10-31 (soak run 235238): 「那明天呢？」 after 「杭州」 committed with
``text_len=10`` and was answered by the plain LLM; the weather thread never saw
the five characters the child said.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import struct
from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.providers.funasr_protocol import sentence_to_asr_result
from services.agent.src.providers.volc_asr import VolcASRConfig, VolcASRSession
from services.agent.src.providers.volc_protocol import FULL_SERVER_RESPONSE
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaVoiceCoreRegistry,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _CapturingGenerationBridge,
    _finish_output_owner_playback,
    _wait_until,
)
from services.agent.tests.unit.test_volc_asr import FakeVolcConnection
from services.common.realtime_information import ASK_WEATHER_CITY_REPLY

_TURN_SAMPLES = 16_000
_TURN_GAP = 48_000


class _ScriptedVolc(FakeVolcConnection):
    """Streams the partials and the definite utterance a real task sends."""

    def __init__(self, partials: tuple[str, ...], final: str) -> None:
        super().__init__(result_text=final)
        self._partials = partials
        self._scripted = False

    async def send(self, data: bytes | str) -> None:
        await super().send(data)
        if self._scripted or len(self.audio_frames) != 1:
            return
        self._scripted = True
        for index, text in enumerate(self._partials):
            self._push(text, end_ms=300 + 100 * index, definite=False, last=False)
        self._push(self.result_text, end_ms=900, definite=True, last=False)

    def _push_result(self, text: str, *, last: bool, sequence: int = 1) -> None:
        self._push(text, end_ms=900, definite=bool(text), last=last, sequence=sequence)

    def _push(
        self,
        text: str,
        *,
        end_ms: int,
        definite: bool,
        last: bool,
        sequence: int = 1,
    ) -> None:
        utterances = (
            [{"text": text, "start_time": 0, "end_time": end_ms, "definite": definite}]
            if text
            else []
        )
        payload = gzip.compress(
            json.dumps(
                {"result": {"text": text, "utterances": utterances}},
                ensure_ascii=False,
            ).encode()
        )
        flags = 0b0011 if last else 0b0001
        header = bytes([0x11, (FULL_SERVER_RESPONSE << 4) | flags, 0x11, 0x00])
        self._incoming.put_nowait(
            header + struct.pack(">i", sequence) + struct.pack(">I", len(payload)) + payload
        )


class _Search:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def resolve(self, *, query: str) -> str:
        self.queries.append(query)
        return f"查到了：{query}"


async def _never_live(_text: str) -> bool:
    return False


class _PipelineDelegation(FakeMediaProvider):
    """Production's delegation seam: the media task asks the reply pipeline."""

    def __init__(self, search: _Search, *, prepare_tail_s: float = 0.0) -> None:
        super().__init__()
        self.search = search
        self.pipeline: ReplyPipeline | None = None
        self.spoken: list[str] = []
        self.delivered: list[str] = []
        self.prepare_tail_s = prepare_tail_s

    def runtime(self, session_id: str) -> DuplexRuntime:
        runtime = DuplexRuntime.create(session_id=session_id, barge_in_enabled=False)
        # Production's classifier sees only the current text and says no to 「那明天呢」.
        runtime.set_live_lookup_semantic_resolver(_never_live)
        committed = runtime.on_turn_committed
        tail_s = self.prepare_tail_s

        async def _prepare(*args: Any, **kwargs: Any) -> GenerationFence:
            fence = await committed(*args, **kwargs)
            # Production's prepare keeps working (plan, context freeze) after the delegation started,
            # then marks the turn's context ready.
            await asyncio.sleep(tail_s)
            assert self.pipeline is not None
            self.pipeline._mark_context_ready(fence)
            return fence

        runtime.on_turn_committed = _prepare  # type: ignore[method-assign]
        self.pipeline = ReplyPipeline(
            instructions="test",
            runtime=runtime,
            realtime_search_resolver=self.search,
        )
        return runtime

    async def start_delegation(self, text: str, fence: GenerationFence) -> str | None:
        assert self.pipeline is not None
        return await self.pipeline.resolve_media_delegation(text, fence)

    @staticmethod
    def accept_output_intent(intent: Any) -> Any:
        return intent

    def generate_output(
        self,
        _identity: SessionIdentity,
        intent: Any,
        _fence: GenerationFence,
        *,
        work_id: str,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        _ = work_id
        self.spoken.append(str(intent.tts_source))

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            if self.prepare_tail_s > 0:
                # Synthesis takes a moment before the first audio, as Doubao's does.
                await asyncio.sleep(self.prepare_tail_s * 2)
            self.delivered.append(str(intent.tts_source))
            yield MediaReplyChunk(
                pcm_s16le=b"\x02\x00\x03\x00",
                source_start_sample=source_start_sample,
                text=str(intent.tts_source),
                first=True,
                final=True,
            )

        return chunks()


class _Device:
    def __init__(
        self,
        name: str,
        turns: list[tuple[tuple[str, ...], str]],
        *,
        prepare_tail_s: float = 0.0,
    ) -> None:
        self.search = _Search()
        self.provider = _PipelineDelegation(self.search, prepare_tail_s=prepare_tail_s)
        self.bridge = _CapturingGenerationBridge()
        self.registry = MediaVoiceCoreRegistry(
            bridge=self.bridge,
            provider_factory=lambda _identity: self.provider,
            runtime_factory=self.provider.runtime,
        )
        self.registry.install()
        self.identity = SessionIdentity(name)
        self.session = self.bridge.bridge.open(self.identity)
        connections = [_ScriptedVolc(partials, final) for partials, final in turns]

        async def connector(*_args: Any, **_kwargs: Any) -> FakeVolcConnection:
            return connections.pop(0)

        self.asr = VolcASRSession(
            VolcASRConfig(
                app_id="app",
                access_token="token",
                resource_id="resource",
                connector=connector,
            )
        )
        self.turn = 0
        self.revisions: dict[tuple[int, str], int] = {}

    async def open(self) -> None:
        self.context = await self.registry.open_session(self.identity)

    async def _asr_results(self, start: int) -> None:
        await self.asr.send_pcm(b"\x10\x00" * _TURN_SAMPLES, capture_start_sample=start)
        task_epoch = self.asr.task_epoch
        origin = self.asr.task_sample_origin
        while True:
            event = await asyncio.wait_for(self.asr.events.get(), timeout=1.0)
            sentence = event.sentence
            if sentence is None:
                continue
            key = (task_epoch, str(sentence.sentence_id))
            self.revisions[key] = self.revisions.get(key, 0) + 1
            await self.registry.accept_asr_result(
                self.identity.session_id,
                sentence_to_asr_result(
                    sentence,
                    task_epoch=task_epoch,
                    revision=self.revisions[key],
                    stream_epoch=self.identity.stream_epoch,
                    sample_offset=origin,
                ),
            )
            if sentence.sentence_end:
                break
        await self.asr.rotate_task(require_consumed=False)

    async def say(self) -> None:
        start = self.turn * _TURN_GAP
        end = start + _TURN_SAMPLES
        self.turn += 1
        delivered_before = len(self.provider.delivered)
        await self._asr_results(start)
        pending = self.context.turn_endpoint_task
        if pending is not None:
            await pending
        if len(self._user_turns()) < self.turn:
            self.context.pending.turn_endpoint_sample = end
            self.context.pending.turn_retire_sample = end
            # The device's endpoint commit, which also retires the previous reply.
            reason = await self.registry._commit_pending_turn(self.context)
            assert reason in {None, "heard_output_in_flight"}, reason
        try:
            await _wait_until(
                lambda: len(self.provider.delivered) > delivered_before
                and self.context.output.output_owner is not None,
                timeout=1.0,
            )
        except AssertionError:
            # Not a lookup: the conversation reply is outside this fixture.
            return
        await asyncio.sleep(0.01)
        await _finish_output_owner_playback(
            self.registry, self.identity, self.bridge, self.session
        )

    def _user_turns(self) -> list[str]:
        return [
            turn.content
            for turn in self.context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]

    async def close(self) -> None:
        await self.asr.aclose()
        await self.registry.finalize_session(self.identity.session_id)


def _names(query: str, *words: str) -> bool:
    return all(word in query for word in words)


@pytest.mark.asyncio
async def test_device_city_answer_then_follow_up_searches_the_remembered_city() -> None:
    device = _Device(
        "device-weather-followup",
        [
            (("今天", "今天天气", "今天天气怎么样"), "今天天气怎么样？"),
            (("杭州",), "杭州。"),
            (("那明天", "那明天呢"), "那明天呢？"),
        ],
    )
    await device.open()
    try:
        await device.say()
        assert device.provider.spoken[-1] == ASK_WEATHER_CITY_REPLY
        assert device.search.queries == []

        await device.say()
        assert len(device.search.queries) == 1
        assert _names(device.search.queries[0], "杭州", "今天", "天气")

        await device.say()
        assert device._user_turns()[-1] == "那明天呢？"
        assert len(device.search.queries) == 2
        assert _names(device.search.queries[1], "杭州", "明天", "天气")
        assert device.provider.spoken[-1] == f"查到了：{device.search.queries[1]}"
    finally:
        await device.close()


@pytest.mark.asyncio
async def test_device_named_city_carries_to_day_follow_up_and_a_cityless_question() -> None:
    device = _Device(
        "device-weather-named-city",
        [
            (("上海", "上海今天天气", "上海今天天气怎么样"), "上海今天天气怎么样？"),
            (("那后天", "那后天呢"), "那后天呢？"),
            (("你知道", "你知道今天天气", "你知道今天天气怎么样吗"), "你知道今天天气怎么样吗？"),
        ],
    )
    await device.open()
    try:
        await device.say()
        assert len(device.search.queries) == 1
        assert device._user_turns() == ["上海今天天气怎么样？"]
        assert _names(device.search.queries[0], "上海")

        await device.say()
        assert device._user_turns()[-1] == "那后天呢？"
        assert len(device.search.queries) == 2
        assert _names(device.search.queries[1], "上海", "后天", "天气")

        await device.say()
        assert len(device.search.queries) == 3
        assert _names(device.search.queries[2], "上海", "今天", "天气")
        assert "你知道" not in device.search.queries[2]
    finally:
        await device.close()


@pytest.mark.asyncio
async def test_device_day_follow_up_before_any_city_is_answered() -> None:
    """Device soaks 2026-11-01 (run 172746 t056, run 190839 t011, run 201623 t006/t016/t023): 「那明天呢？」
    right after the city question, with no city given, got no reply at all.  Asking for the city needs no
    lookup, so the delegated answer started before the commit finished, and the commit then cancelled it
    as the previous reply."""

    device = _Device(
        "device-weather-followup-no-city",
        [
            (("今天", "今天天气", "今天天气怎么样"), "今天天气怎么样？"),
            (("那明天", "那明天呢"), "那明天呢？"),
        ],
        prepare_tail_s=0.05,
    )
    await device.open()
    try:
        await device.say()
        assert device.provider.spoken[-1] == ASK_WEATHER_CITY_REPLY

        delivered_before = len(device.provider.delivered)
        await device.say()
        assert device._user_turns()[-1] == "那明天呢？"
        assert len(device.provider.delivered) > delivered_before, "the follow-up got no reply"
        assert device.provider.delivered[-1] == ASK_WEATHER_CITY_REPLY
    finally:
        await device.close()


@pytest.mark.asyncio
async def test_the_city_question_waits_for_its_turn_context_to_be_frozen() -> None:
    """Device soak 2026-11-01 run 205917: the city question was ready before ``prepare_turn`` froze the
    turn's context, carried the old context version, and was dropped as superseded at its first audio."""

    provider = _PipelineDelegation(_Search())
    runtime = provider.runtime("city-question-waits-for-context")
    pipeline = provider.pipeline
    assert pipeline is not None
    first = await runtime.on_turn_committed("今天天气怎么样？", input_modality="audio")
    assert await pipeline.resolve_media_delegation("今天天气怎么样？", first) == ASK_WEATHER_CITY_REPLY
    fence = await runtime.on_turn_committed("那明天呢？", input_modality="audio")
    pipeline._context_ready_by_fence.pop(fence, None)

    answer = asyncio.create_task(pipeline.resolve_media_delegation("那明天呢？", fence))
    await asyncio.sleep(0.05)
    assert not answer.done(), "the answer waits for the turn's context"
    pipeline._mark_context_ready(fence)

    assert await asyncio.wait_for(answer, 1.0) == ASK_WEATHER_CITY_REPLY
    await runtime.close()
