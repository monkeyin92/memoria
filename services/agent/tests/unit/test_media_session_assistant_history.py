"""The next LLM request sees what the device actually played (field 2026-09-30).

ESP32 child session on the direct media path, 2026-09-30 00:10 CST: six turns,
every reply played to the end and ACKed (``actual_heard``,
``playback_completed``, ``media_playback_ack``), yet each reply introduced
itself again and re-answered every earlier question.  The LLM saw only the
user turns: the incremental TTS path mapped reply text into the playback
ledger solely from the provider's subtitle timing, so a reply whose timing was
unusable left the ledger with audio but no text, and the completed playback
committed an empty heard text.

These scenarios drive the real registry/runtime over the public gRPC stream
with the production reply chain (``ReplyPipeline`` behind
``ExistingVoiceProviderAdapter``); only ASR, the chat model, the planner and
the TTS wire are scripted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src import llm_types as llm
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.media_agent_factory import _SessionLanguageModel
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import ResponsePlanFetch
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import MediaReplyChunk
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import ScriptedChatModel
from services.agent.tests.unit.test_agent_production_wiring import _plan_for_fence
from services.agent.tests.unit.test_media_session_golden import (
    _device_identity,
    _Harness,
    _is_action,
    _ScriptedProvider,
)

# One pushed phrase renders as two 20 ms frames of 24 kHz downlink PCM.
_PHRASE_SAMPLES = 960
_GREETING = ("我是阿旭。", "很高兴认识你。")
_SKY = "天空是蓝色的，因为空气散射蓝光。"


class _SubtitledSpeech:
    """One live Doubao-style TTS stream per generation.

    ``timing="exact"`` exposes provider subtitle timing phrase by phrase;
    ``"degraded"`` reports a final alignment too far from the PCM to trust
    (Doubao then drops the timeline); ``"absent"`` has no timeline accessor.
    """

    def __init__(self, timing: str) -> None:
        self.timing = timing
        self.phrases: list[str] = []
        self.queue: asyncio.Queue[Any | None] = asyncio.Queue()
        if timing != "absent":
            self.timed_transcript = self._timed_transcript
            self.timed_transcript_alignment = self._alignment

    def push_text(self, text: str) -> None:
        self.phrases.append(text)
        self.queue.put_nowait(
            SimpleNamespace(
                frame=SimpleNamespace(data=b"\x01\x00" * _PHRASE_SAMPLES, sample_rate=24_000)
            )
        )

    def end_input(self) -> None:
        self.queue.put_nowait(None)

    def __aiter__(self) -> AsyncIterator[Any]:
        async def events() -> AsyncIterator[Any]:
            while (item := await self.queue.get()) is not None:
                yield item

        return events()

    async def aclose(self) -> None:
        return None

    def _alignment(self) -> str:
        return "ok" if self.timing == "exact" else "degraded"

    def _timed_transcript(self) -> tuple[SimpleNamespace, ...]:
        if self.timing != "exact":
            return ()
        step = _PHRASE_SAMPLES / 24_000
        return tuple(
            SimpleNamespace(text=phrase, start_time=index * step, end_time=(index + 1) * step)
            for index, phrase in enumerate(self.phrases)
        )


class _SubtitledTTS:
    def __init__(self, timing: str) -> None:
        self.timing = timing

    def stream(self) -> _SubtitledSpeech:
        return _SubtitledSpeech(self.timing)


class _ProductionReplyProvider(_ScriptedProvider):
    """Scripted ASR finals; turn preparation and replies use the production chain."""

    def __init__(self) -> None:
        super().__init__()
        self.adapter: ExistingVoiceProviderAdapter | None = None

    def _reply_adapter(self) -> ExistingVoiceProviderAdapter:
        assert self.adapter is not None
        return self.adapter

    async def prepare_committed_turn(self, identity: SessionIdentity, text: str) -> GenerationFence:
        return await self._reply_adapter().prepare_committed_turn(identity, text)

    def generate_reply(
        self,
        identity: SessionIdentity,
        user_text: str,
        fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        self.reply_calls += 1
        return self._reply_adapter().generate_reply(identity, user_text, fence)

    def cancel_generation(self, fence: GenerationFence) -> bool:
        self.cancelled.append(fence)
        return self._reply_adapter().cancel_generation(fence)

    async def interrupted_timed_text_spans(self, fence: GenerationFence) -> tuple[Any, ...]:
        return await self._reply_adapter().interrupted_timed_text_spans(fence)


class _Conversation:
    """A device session whose LLM requests are recorded as (role, text) lists."""

    def __init__(self, session_id: str, *, timing: str, replies: tuple[str, ...]) -> None:
        self.provider = _ProductionReplyProvider()
        self.harness = _Harness(_device_identity(session_id), self.provider)
        runtime = self.harness.runtime
        # No voiceprint: the device-bound subject is the owner (2026-09-24).
        bind_owner_policy(runtime)
        self.requests: list[list[tuple[str, str]]] = []
        self._replies = iter(replies)

        def respond(chat_ctx: Any, _tools: Any) -> AsyncIterator[Any]:
            self.requests.append(
                [
                    (str(message.role), message.text_content or "")
                    for message in chat_ctx.items
                    if message.role != "system"
                ]
            )
            reply = next(self._replies)

            async def chunks() -> AsyncIterator[Any]:
                yield llm.ChatChunk(id="reply", delta=llm.ChoiceDelta(content=reply))

            return chunks()

        class Planner:
            async def fetch(self, **kwargs: Any) -> ResponsePlanFetch:
                return ResponsePlanFetch(
                    _plan_for_fence(kwargs["fence"], instructions="陪孩子聊天，回答问题。"),
                    "ok",
                )

        pipeline = ReplyPipeline(
            instructions="你是阿旭。",
            runtime=runtime,
            response_planner_client=Planner(),  # type: ignore[arg-type]
            language_model=ScriptedChatModel(respond),
        )
        self.provider.adapter = ExistingVoiceProviderAdapter(
            asr_session_factory=lambda: None,  # type: ignore[arg-type,return-value]
            language_model=_SessionLanguageModel(pipeline, ()),  # type: ignore[arg-type]
            speech_synthesis=_SubtitledTTS(timing),  # type: ignore[arg-type]
        )

    async def ask(self, text: str) -> None:
        """Speak one committed turn and wait until its whole reply was sent."""

        harness = self.harness
        completed = harness.count("generation", _is_action(media_pb2.GENERATION_ACTION_COMPLETE))
        requests = len(self.requests)
        await harness.utterance(text, frames=25)
        await harness.wait_for(
            lambda: len(self.requests) > requests
            and harness.count("generation", _is_action(media_pb2.GENERATION_ACTION_COMPLETE))
            > completed
        )

    async def playback(
        self,
        *,
        rendered_sample_end: int | None = None,
        ended: bool = True,
        approximate: bool = False,
    ) -> None:
        """Report device playback of the current reply (default: all of it)."""

        harness = self.harness
        frame = harness.last("audio")
        end = frame.source_start_sample + frame.frame_samples
        rendered = end if rendered_sample_end is None else rendered_sample_end
        sequence = frame.sequence if rendered >= end else rendered // frame.frame_samples - 1
        await harness.send(
            media_pb2.MediaToCore(
                playback=media_pb2.PlaybackProgress(
                    identity=harness.wire_identity,
                    generation_id=frame.generation_id,
                    received_sequence=sequence,
                    rendered_sample_end=rendered,
                    client_monotonic_ms=1,
                    approximate=approximate,
                    turn_id=frame.turn_id,
                    tool_epoch=frame.tool_epoch,
                    session_epoch=frame.session_epoch,
                    event_type=(
                        media_pb2.PLAYBACK_EVENT_TYPE_ENDED
                        if ended
                        else media_pb2.PLAYBACK_EVENT_TYPE_PROGRESS
                    ),
                )
            )
        )

    async def keyword_stop(self) -> None:
        """The device keyword spotter hears 「停一下」 while the reply plays."""

        harness = self.harness
        await harness.send(
            media_pb2.MediaToCore(
                keyword=media_pb2.KeywordEvent(
                    identity=harness.wire_identity,
                    keyword="停一下",
                    confidence=0.95,
                    start_sample=harness.sample,
                    end_sample=harness.sample + 320,
                    hard_stop=True,
                )
            )
        )
        # The device already flushed locally, so Core sends no playback.flush
        # effect; keyword.hit is the last thing the stop emits.
        await harness.wait_for(
            lambda: harness.count(
                "client",
                lambda client: json.loads(client.json_payload).get("type") == "keyword.hit",
            )
            >= 1
        )
        assert not harness.count(
            "realtime_effect",
            lambda effect: effect.effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
        )
        # The stop word's own uplink audio; the next question starts after it.
        await harness.audio_frames(1)


async def _run(conversation: _Conversation, script: Any) -> None:
    await conversation.harness.start()
    try:
        await script(conversation)
    finally:
        await conversation.harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["exact", "degraded", "absent"])
async def test_fully_played_reply_is_history_for_the_next_llm_request(timing: str) -> None:
    conversation = _Conversation(
        f"assistant-history-complete-{timing}",
        timing=timing,
        replies=("".join(_GREETING), _SKY),
    )

    async def script(session: _Conversation) -> None:
        await session.ask("你好")
        await session.playback()
        await session.ask("为什么天空是蓝色的呀")

    await _run(conversation, script)

    assert conversation.requests == [
        [("user", "你好")],
        [
            ("user", "你好"),
            ("assistant", "".join(_GREETING)),
            ("user", "为什么天空是蓝色的呀"),
        ],
    ]


@pytest.mark.asyncio
async def test_approximate_playback_receipt_does_not_become_heard_history() -> None:
    # A hardware receipt that only bounds queued/I2S audio proves playback
    # ended, not what was heard: untimed reply text stays out of history.
    conversation = _Conversation(
        "assistant-history-approximate",
        timing="degraded",
        replies=("".join(_GREETING), _SKY),
    )

    async def script(session: _Conversation) -> None:
        await session.ask("你好")
        await session.playback(approximate=True)
        await session.ask("为什么天空是蓝色的呀")

    await _run(conversation, script)

    assert conversation.requests[-1] == [("user", "你好"), ("user", "为什么天空是蓝色的呀")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timing", "heard"),
    [("exact", _GREETING[0]), ("degraded", None), ("absent", None)],
)
async def test_stopped_reply_commits_only_its_heard_prefix(timing: str, heard: str | None) -> None:
    conversation = _Conversation(
        f"assistant-history-stopped-{timing}",
        timing=timing,
        replies=("".join(_GREETING), _SKY),
    )

    async def script(session: _Conversation) -> None:
        await session.ask("你好")
        # The device rendered the first phrase, then the child said 「停一下」.
        await session.playback(rendered_sample_end=_PHRASE_SAMPLES, ended=False)
        await session.keyword_stop()
        await session.ask("为什么天空是蓝色的呀")

    await _run(conversation, script)

    prior = [("assistant", heard)] if heard is not None else []
    assert conversation.requests[-1] == [
        ("user", "你好"),
        *prior,
        ("user", "为什么天空是蓝色的呀"),
    ]
