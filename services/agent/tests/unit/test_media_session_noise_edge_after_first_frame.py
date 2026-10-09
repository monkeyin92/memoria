"""A reply whose first frame is out, but not yet heard, survives a VAD edge that holds no words (N-8).

Field 2026-10-08 18:16 (the diagnostic round on build 24, the Mac speaker at volume 30): the owner asked a
question, it was committed, and the reply's first frame left the bridge.  21 ms later a 20 ms device VAD edge
on room noise (``Device VAD start ... rms=0.0009``, no words) reached the bridge; the next provider chunk found
the floor taken and cancelled the reply 82 ms after its first frame, before the device had rendered a sample
(``output_frames=0``).  The wait of ``test_media_session_unheard_reply_hold`` covers only the time before the
first PCM, but a frame is "sent" a network trip before it is heard.  Round 18 p03 (first frame, cut after
0.10 s) had the same shape.

These drive the real registry/runtime over the public stream with a reply whose later chunks are gated, so the
edge lands between the first frame and the next chunk.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core import media_session_output_stream
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import MediaReplyChunk
from services.agent.src.voice_core.media_session_output_stream import _TextlessSpare
from services.agent.tests.unit.test_media_session_golden import _REPLY_PCM
from services.agent.tests.unit.test_media_session_unheard_reply_hold import (
    QUESTION,
    _context,
    _device_identity,
    _GatedReplyProvider,
    _Predicate,
    _Scene,
)

_PCM_SAMPLES = len(_REPLY_PCM) // 2


class _StreamedReplyProvider(_GatedReplyProvider):
    """The first PCM leaves at once; each later chunk waits for its gate, so an edge can land in between."""

    def __init__(self, *, later_chunks: int = 1) -> None:
        super().__init__()
        self.later_gates = [asyncio.Event() for _ in range(later_chunks)]

    def generate_reply(
        self,
        _identity: SessionIdentity,
        _user_text: str,
        _fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        self.reply_calls += 1
        later_gates = self.later_gates

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=_REPLY_PCM,
                source_start_sample=0,
                text="从前有一座山。",
                first=True,
                final=False,
            )
            for index, gate in enumerate(later_gates):
                await gate.wait()
                yield MediaReplyChunk(
                    pcm_s16le=_REPLY_PCM,
                    source_start_sample=_PCM_SAMPLES * (index + 1),
                    text="山里住着一只小熊。",
                    first=False,
                    final=index == len(later_gates) - 1,
                )

        return chunks()


class _StreamScene(_Scene):
    def __init__(self, identity: SessionIdentity, provider: _StreamedReplyProvider) -> None:
        super().__init__(identity, provider)
        self.streamed = provider

    def open_gate(self, index: int = 0) -> None:
        self.streamed.later_gates[index].set()

    def audio_of(self, fence: GenerationFence) -> int:
        """Frames the bridge sent for one generation (a replacement reply has its own)."""

        return self.count("audio", lambda frame: frame.generation_id == fence.generation_id)

    async def question_with_the_first_frame_out(self) -> GenerationFence:
        """The question is committed and the first PCM of its reply is on the wire, unplayed."""

        await self.utterance(QUESTION, frames=25)
        await self.wait_for(self.first_frame_sent, timeout=3.0)
        assert self.count("audio") == 1
        assert self.terminals() == []
        return self.runtime.fence


async def _run(script: Any, *, name: str, later_chunks: int = 1) -> None:
    provider = _StreamedReplyProvider(later_chunks=later_chunks)
    scene = _StreamScene(_device_identity(name), provider)
    await scene.start()
    try:
        await script(scene)
    finally:
        for gate in provider.later_gates:
            gate.set()
        await scene.close()


@pytest.mark.asyncio
async def test_a_noise_edge_after_the_first_frame_does_not_cancel_a_reply_nobody_has_heard() -> None:
    async def script(scene: _StreamScene) -> None:
        fence = await scene.question_with_the_first_frame_out()
        await scene.room_noise(frames=3)  # a VAD start on noise: no words follow

        scene.open_gate()  # the reply's next chunk reaches the stream while the edge is open
        await scene.wait_for(lambda: scene.audio_of(fence) == 2, timeout=3.0)

        assert scene.runtime.fence.matches(fence)
        assert scene.terminals() == []
        assert scene.streamed.reply_calls == 1

    await _run(script, name="n8-after-first-frame")


@pytest.mark.asyncio
async def test_words_on_that_edge_still_take_the_floor_from_a_reply_nobody_has_heard() -> None:
    async def script(scene: _StreamScene) -> None:
        fence = await scene.question_with_the_first_frame_out()
        await scene.room_noise()

        # The edge turns out to be the owner talking on: the ASR delivers words for it.
        await scene.audio_frames(10, final_text="我还想问一件事")
        scene.open_gate()
        await scene.wait_for(lambda: bool(scene.terminals()), timeout=3.0)

        assert scene.audio_of(fence) == 1  # the old reply never got its second chunk out

    await _run(script, name="n8-after-first-frame-words")


@pytest.mark.asyncio
async def test_a_reply_the_device_is_already_playing_yields_at_once_to_a_voice_edge() -> None:
    async def script(scene: _StreamScene) -> None:
        fence = await scene.question_with_the_first_frame_out()
        await scene.playback_progress()
        await scene.room_noise()

        scene.open_gate()
        await scene.wait_for(lambda: bool(scene.terminals()), timeout=3.0)

        assert scene.audio_of(fence) == 1  # an audible reply is a barge-in target, as before

    await _run(script, name="n8-audible-barge-in")


@pytest.mark.asyncio
async def test_a_turn_spared_while_unheard_stays_spared_once_the_reply_is_audible() -> None:
    async def script(scene: _StreamScene) -> None:
        fence = await scene.question_with_the_first_frame_out()
        await scene.room_noise()

        scene.open_gate(0)
        await scene.wait_for(lambda: scene.audio_of(fence) == 2, timeout=3.0)  # spared at this chunk
        await scene.playback_progress()  # the device plays it while the edge is still open
        assert not scene.runtime.output_floor_allows_assistant

        scene.open_gate(1)
        await scene.wait_for(lambda: scene.audio_of(fence) == 3, timeout=3.0)

        assert scene.terminals() == []

    await _run(script, name="n8-stays-spared", later_chunks=2)


@pytest.mark.asyncio
async def test_the_floor_comes_back_when_the_edge_turns_out_empty_and_the_reply_finishes() -> None:
    async def script(scene: _StreamScene) -> None:
        # The edge's turn never gets an ASR result, so the ordinary endpoint tail timeout (6 s) ends it.
        scene.registry.turn_endpoint_absolute_timeout_s = 0.4
        fence = await scene.question_with_the_first_frame_out()
        await scene.room_noise(frames=3)
        scene.open_gate()
        await scene.wait_for(lambda: scene.audio_of(fence) == 2, timeout=3.0)

        await scene.vad_end(scene.sample)
        await scene.wait_for(lambda: scene.runtime.output_floor_allows_assistant, timeout=5.0)
        await scene.playback_progress()
        await scene.playback_ended()

        assert scene.runtime.fence.matches(fence)
        assert "preempted" not in scene.terminals()
        assert scene.streamed.reply_calls == 1

    await _run(script, name="n8-floor-returns")


@pytest.mark.asyncio
async def test_the_next_question_after_a_spared_reply_gets_its_own_reply() -> None:
    async def script(scene: _StreamScene) -> None:
        # No stale barge-in candidate may survive the spared edge and swallow the child's next question.
        scene.registry.turn_endpoint_absolute_timeout_s = 0.4
        fence = await scene.question_with_the_first_frame_out()
        await scene.room_noise(frames=3)
        scene.open_gate()
        await scene.wait_for(lambda: scene.audio_of(fence) == 2, timeout=3.0)
        await scene.vad_end(scene.sample)
        await scene.wait_for(lambda: scene.runtime.output_floor_allows_assistant, timeout=5.0)
        await scene.playback_progress()
        await scene.playback_ended()
        calls = scene.streamed.reply_calls

        await scene.utterance("换一个故事吧", frames=25)
        await scene.wait_for(lambda: scene.streamed.reply_calls == calls + 1, timeout=5.0)
        await scene.wait_for(lambda: scene.runtime.fence.generation_id > fence.generation_id, timeout=3.0)
        await scene.wait_for(lambda: scene.audio_of(scene.runtime.fence) >= 1, timeout=3.0)

        assert "preempted" not in scene.terminals()

    await _run(script, name="n8-next-question")


@pytest.mark.asyncio
async def test_a_spared_reply_says_so_without_the_childs_words(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def script(scene: _StreamScene) -> None:
        fence = await scene.question_with_the_first_frame_out()
        await scene.room_noise()
        scene.open_gate()
        await scene.wait_for(lambda: scene.audio_of(fence) == 2, timeout=3.0)

    with caplog.at_level(logging.INFO, logger=media_session_output_stream.logger.name):
        await _run(script, name="n8-spared-log")

    [line] = [
        message
        for message in (record.getMessage() for record in caplog.records)
        if "reply spared from a user turn" in message
    ]
    assert "rendered=0" in line
    assert "turn_start=" in line
    assert QUESTION not in line  # flags and numbers only


# -- the decision itself -------------------------------------------------------------------------


def _reply_and_turn(
    *,
    floor_open: bool = False,
    partial: str | None = None,
    rendered: int = 0,
    barge_in: bool = True,
    selected: bool = True,
    owner_is_lease: bool = True,
    turn_start: int = 640,
) -> tuple[Any, Any]:
    """A streaming reply (the lease) and the user turn that holds the floor against it."""

    fence = GenerationFence(
        session_id="n8-spare", turn_id=1, generation_id=1, tool_epoch=0, session_epoch=0
    )
    lease = SimpleNamespace(
        fence=fence, intent=SimpleNamespace(intent_id="intent-1"), task=asyncio.current_task()
    )
    context = _context(floor_open=floor_open, turn_start=turn_start, partial=partial)
    context.closed = False
    context.standby_requested = False
    context.runtime.barge_in_enabled = barge_in
    context.runtime.fence = fence
    context.runtime.orchestrator = SimpleNamespace(
        delegation=SimpleNamespace(
            output_intent_is_selected=lambda *_args, **_kwargs: selected,
            current_context_version=lambda _session_id: 1,
        )
    )
    context.output = SimpleNamespace(
        output_owner=lease if owner_is_lease else None,
        playback=SimpleNamespace(rendered_sample_end=lambda _fence: rendered),
    )
    return context, lease


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "spared"),
    [
        ({}, True),  # a turn with no words against a reply nobody has heard
        ({"barge_in": False}, False),  # half-duplex has its own floor rules
        ({"floor_open": True}, False),  # nothing holds the floor
        ({"partial": "我还想问"}, False),  # the turn has words: the user really holds the floor
        ({"rendered": 160}, False),  # the child hears the reply: an edge is a barge-in
        ({"selected": False}, False),  # the reply would not be the output even with an open floor
        ({"owner_is_lease": False}, False),  # another owner took over
    ],
    ids=["unheard", "half-duplex", "floor-open", "words", "audible", "not-selected", "owner-lost"],
)
async def test_only_an_unheard_reply_is_spared_from_a_turn_with_no_words(
    case: dict[str, Any], spared: bool
) -> None:
    context, lease = _reply_and_turn(**case)
    spare = _TextlessSpare()

    assert _Predicate()._textless_turn_spares_reply(context, lease, spare) is spared
    assert (spare.turn_start_sample == 640) is spared


@pytest.mark.asyncio
async def test_a_turn_spared_once_stays_spared_after_the_reply_becomes_audible() -> None:
    context, lease = _reply_and_turn(rendered=160)

    spare = _TextlessSpare(turn_start_sample=640)  # spared while the reply was still unheard
    assert _Predicate()._textless_turn_spares_reply(context, lease, spare) is True

    other_turn = _TextlessSpare(turn_start_sample=100)  # a different turn, now against an audible reply
    assert _Predicate()._textless_turn_spares_reply(context, lease, other_turn) is False


@pytest.mark.asyncio
async def test_a_new_turn_against_a_still_unheard_reply_is_judged_afresh() -> None:
    context, lease = _reply_and_turn()
    spare = _TextlessSpare(turn_start_sample=100)

    assert _Predicate()._textless_turn_spares_reply(context, lease, spare) is True
    assert spare.turn_start_sample == 640


@pytest.mark.asyncio
async def test_the_stream_wait_spares_only_when_it_is_given_the_spare_state() -> None:
    context, lease = _reply_and_turn()

    assert await _Predicate()._wait_for_unheard_output_floor(context, lease, emitted_audio=True) is False
    assert (
        await _Predicate()._wait_for_unheard_output_floor(
            context, lease, emitted_audio=True, spare=_TextlessSpare()
        )
        is True
    )


@pytest.mark.asyncio
async def test_the_spare_decision_logs_numbers_and_never_the_childs_words(
    caplog: pytest.LogCaptureFixture,
) -> None:
    context, lease = _reply_and_turn(partial="   ")
    with caplog.at_level(logging.INFO, logger=media_session_output_stream.logger.name):
        assert _Predicate()._textless_turn_spares_reply(context, lease, _TextlessSpare()) is True

    [line] = [r.getMessage() for r in caplog.records if "reply spared from a user turn" in r.getMessage()]
    assert "turn_start=640" in line and "rendered=0" in line
