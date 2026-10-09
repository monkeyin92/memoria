"""A committed question is still answered when an edge with no words of its own cuts in (N-8).

Field 2026-10-09 10:52 CST (the pre-fix baseline, t010 "讲一个很短的笑话吧"): the recognizer finalized 3
characters of the question and left the rest as interim text; the question was committed on the 3 characters
and its reply prepared.  1.1 s later a device VAD edge on room noise arrived (``rms=0.0006``, 2.6 s after the
child stopped talking) and, 19 ms after it, the pending turn already carried 5 characters of interim text
(``partial_chars=5``).  Those words cannot belong to the edge: they were the unfinished tail of the question
itself, already there before the edge.  The prepared reply was "not held" for them, superseded before its first
frame, and the interim text never became a final: the turn ended empty and nothing answered the question.

These drive the real registry/runtime over the public stream with a reply whose first audio is gated, and
script the recognizer the way the field log shows it: an interim result for the tail of the question, then an
edge with no words of its own.  The control case keeps what must not change: interim words that belong to the
edge (the child talking on) still take the floor.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core import media_session_turns
from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.media_session import MediaReplyChunk
from services.agent.src.voice_core.speech_timeline import ASRResult
from services.agent.tests.unit.test_media_session_unheard_reply_hold import (
    QUESTION,
    _device_identity,
    _GatedReplyProvider,
    _Scene,
)


class _InterimProvider(_GatedReplyProvider):
    """Also returns scripted interim (not final) ASR results on chosen frames."""

    def __init__(self) -> None:
        super().__init__()
        self.interims: dict[int, tuple[str, int]] = {}
        self.edge_finals: dict[int, tuple[str, int]] = {}
        self.asked: list[str] = []

    def generate_reply(
        self,
        identity: SessionIdentity,
        user_text: str,
        fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        self.asked.append(user_text)
        return super().generate_reply(identity, user_text, fence)

    async def ingest_audio(self, identity: SessionIdentity, frame: AudioFrame) -> Sequence[ASRResult]:
        interim = self.interims.pop(frame.sequence, None)
        final = self.edge_finals.pop(frame.sequence, None)
        scripted = interim or final
        if scripted is None:
            return await super().ingest_audio(identity, frame)
        self.audio_calls.append(frame.sequence)
        text, start_sample = scripted
        # The final is the next revision of the same sentence, as the recognizer sends it.
        return (
            ASRResult(
                task_epoch=1,
                sentence_id=f"edge-{start_sample}",
                revision=1 if interim else 2,
                capture_start_sample=start_sample,
                capture_end_sample=frame.capture_end_sample,
                text=text,
                is_final=final is not None,
                confidence=0.9,
                stream_epoch=frame.identity.stream_epoch,
            ),
        )


class _InterimScene(_Scene):
    def __init__(self, identity: SessionIdentity, provider: _InterimProvider) -> None:
        super().__init__(identity, provider)
        self.interim_provider = provider

    async def interim_frames(self, count: int, text: str) -> None:
        """``count`` frames; the ASR answers the last one with an interim result, never a final."""

        start_sample = self.sample
        self.interim_provider.interims[self._audio_sequence + count - 1] = (text, start_sample)
        await self.audio_frames(count)

    async def final_frames_covering(self, count: int, text: str, *, since: int) -> None:
        """``count`` frames; the ASR ends them with a final that covers the whole edge, from ``since``."""

        self.interim_provider.edge_finals[self._audio_sequence + count - 1] = (text, since)
        await self.audio_frames(count)

    async def wait_for_first_frame(self, *, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.first_frame_sent() and loop.time() < deadline:
            await asyncio.sleep(0.02)
        return self.first_frame_sent()


async def _run(script: Any, *, name: str) -> None:
    provider = _InterimProvider()
    scene = _InterimScene(_device_identity(name), provider)
    await scene.start()
    try:
        await script(scene)
    finally:
        provider.gate.set()
        await scene.close()


@pytest.fixture(autouse=True)
def _fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_BASE_S", 0.3)
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_VAD_EXTENSION_S", 0.2)
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_MAX_S", 0.6)


@pytest.mark.asyncio
async def test_interim_text_from_before_the_edge_does_not_cost_the_prepared_reply() -> None:
    async def script(scene: _InterimScene) -> None:
        await scene.committed_question_with_a_prepared_reply()

        # The tail of the question reaches the pending turn as interim text after the commit and
        # never gets a final; then an edge with no words of its own arrives.
        await scene.interim_frames(5, "笑话吧")
        await scene.vad_start(scene.sample)
        scene.gated.gate.set()
        await scene.audio_frames(3)

        # The edge brings nothing: the hold cap runs out and the question is answered, once.
        answered = await scene.wait_for_first_frame(timeout=3.0)
        assert answered, (
            "the committed question was never answered after an edge that had no words of its own: "
            f"terminals={scene.terminals()} asked={scene.interim_provider.asked}"
        )
        assert scene.interim_provider.asked == [QUESTION]
        assert "preempted" not in scene.terminals()

    await _run(script, name="n8-stale-interim")


@pytest.mark.asyncio
async def test_interim_words_that_become_a_final_still_take_the_floor_and_get_their_own_answer() -> None:
    async def script(scene: _InterimScene) -> None:
        await scene.committed_question_with_a_prepared_reply()

        edge = scene.sample
        await scene.vad_start(edge)
        await scene.interim_frames(10, "我还想问")
        scene.gated.gate.set()
        await scene.wait_for(lambda: bool(scene.terminals()) or scene.first_frame_sent(), timeout=3.0)
        assert not scene.first_frame_sent(), "the old reply must not talk over the new words"

        # The words were real: the ASR finalizes them, and that new question is answered once.
        await scene.final_frames_covering(5, "我还想问一件事", since=edge)
        await scene.vad_end(scene.sample)

        assert await scene.wait_for_first_frame(timeout=3.0), "the new question was not answered"
        assert scene.terminals()[0] == "preempted"
        # The first reply was never spoken; the new words, and only they, got an answer.
        assert scene.interim_provider.asked == [QUESTION, "我还想问一件事"]

    await _run(script, name="n8-words-become-final")
