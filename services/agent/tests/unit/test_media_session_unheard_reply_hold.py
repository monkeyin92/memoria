"""A committed reply that is not audible yet survives a VAD edge that holds no words (N-8).

Field 2026-10-01 (round 4) and 2026-10-02 (rounds 8-9, pink-noise trigger): the owner's question was
heard and committed, and the reply was ready, when the device VAD fired once more on room noise
(``Device VAD start ... rms=0.0002``).  The server took the floor for that edge, so the reply was
superseded before its first frame, and the question was never answered.  The edge produced no words,
so there was nothing to yield the floor to.  These drive the real registry/runtime over the public
stream with a reply whose first audio is gated, to model the 1-2 s between the commit and the
first PCM in which the noise lands.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.voice_core import media_session_output_stream, media_session_turns
from services.agent.src.voice_core.media_protocol import (
    DEVICE_POST_PLAYBACK_HOLDOFF_S,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import MediaReplyChunk
from services.agent.src.voice_core.media_session_output_stream import MediaOutputStreamMixin
from services.agent.src.voice_core.media_session_pending_turn import PendingTurn
from services.agent.src.voice_core.media_session_turns import MediaTurnEndpointMixin
from services.agent.src.voice_core.speech_timeline import ASRResult
from services.agent.tests.unit.test_media_session_golden import (
    _REPLY_PCM,
    _Harness,
    _ScriptedProvider,
)

QUESTION = "给我讲一个故事吧"


class _GatedReplyProvider(_ScriptedProvider):
    """The committed reply is prepared, but its first PCM waits for ``gate``."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.cancelled_fences: list[GenerationFence] = []

    def generate_reply(
        self,
        _identity: SessionIdentity,
        _user_text: str,
        _fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        self.reply_calls += 1

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            await self.gate.wait()
            yield MediaReplyChunk(
                pcm_s16le=_REPLY_PCM,
                source_start_sample=0,
                text="从前有一座山。",
                first=True,
                final=True,
            )

        return chunks()


def _device_identity(session_id: str) -> SessionIdentity:
    return SessionIdentity(
        session_id,
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
        audio_mode="interrupt_assist",
    )


class _Scene(_Harness):
    def __init__(self, identity: SessionIdentity, provider: _GatedReplyProvider) -> None:
        super().__init__(identity, provider)
        self.gated = provider
        self.runtime = DuplexRuntime.create(
            session_id=identity.session_id,
            input_guard_enabled=True,
            barge_in_enabled=True,
            capture_release_holdoff_s=DEVICE_POST_PLAYBACK_HOLDOFF_S,
        )

    @property
    def context(self) -> Any:
        return self.registry.session_state(self.identity.session_id)

    def first_frame_sent(self) -> bool:
        return self.count("audio") > 0

    def terminals(self) -> list[str]:
        return [d["terminal_event"] for d in self.deliveries if d["terminal_event"]]

    async def committed_question_with_a_prepared_reply(self) -> GenerationFence:
        """The question is committed; the reply is ready but not yet on the speaker."""

        await self.utterance(QUESTION, frames=25)
        await self.wait_for(lambda: self.gated.reply_calls == 1)
        assert not self.first_frame_sent()
        return self.runtime.fence

    async def room_noise(self, *, frames: int = 0) -> None:
        """A device VAD start on noise: no ASR text follows."""

        await self.vad_start(self.sample)
        if frames:
            await self.audio_frames(frames)
        assert not self.runtime.output_floor_allows_assistant


async def _run(script: Any, *, name: str) -> None:
    provider = _GatedReplyProvider()
    scene = _Scene(_device_identity(name), provider)
    await scene.start()
    try:
        await script(scene)
    finally:
        provider.gate.set()
        await scene.close()


@pytest.fixture(autouse=True)
def _fast_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_BASE_S", 0.3)
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_VAD_EXTENSION_S", 0.2)
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_MAX_S", 0.6)


@pytest.mark.asyncio
async def test_room_noise_before_the_first_frame_does_not_lose_the_prepared_reply() -> None:
    async def script(scene: _Scene) -> None:
        fence = await scene.committed_question_with_a_prepared_reply()
        await scene.room_noise(frames=3)

        scene.gated.gate.set()  # the reply's first PCM is ready while the noise edge is open
        await scene.wait_for(scene.first_frame_sent, timeout=3.0)

        assert scene.runtime.output_floor_allows_assistant
        assert scene.runtime.fence.matches(fence)
        assert scene.gated.reply_calls == 1
        assert "preempted" not in scene.terminals()
        delivery = scene.context.output.reply_delivery.get(fence)
        assert delivery is not None and delivery.first_frame_sent

    await _run(script, name="n8-room-noise")


@pytest.mark.asyncio
async def test_without_a_noise_edge_the_prepared_reply_plays_at_once() -> None:
    async def script(scene: _Scene) -> None:
        fence = await scene.committed_question_with_a_prepared_reply()
        assert scene.runtime.output_floor_allows_assistant

        scene.gated.gate.set()
        await scene.wait_for(scene.first_frame_sent, timeout=3.0)

        assert scene.runtime.fence.matches(fence)
        assert scene.terminals() == []
        assert scene.context.pending.evidence_less_hold_since is None

    await _run(script, name="n8-no-noise")


@pytest.mark.asyncio
async def test_the_reply_waits_for_the_floor_instead_of_talking_over_the_noise_edge() -> None:
    async def script(scene: _Scene) -> None:
        await scene.committed_question_with_a_prepared_reply()
        await scene.room_noise()
        scene.gated.gate.set()

        await asyncio.sleep(0.1)  # well inside the hold: the floor is still the edge's
        await scene.settle()
        assert not scene.first_frame_sent()
        owner = scene.context.output.output_owner
        assert owner is not None and not scene.terminals()

        await scene.wait_for(scene.first_frame_sent, timeout=3.0)

    await _run(script, name="n8-waits")


@pytest.mark.asyncio
async def test_words_heard_on_the_new_speech_still_take_the_floor_from_the_prepared_reply() -> None:
    async def script(scene: _Scene) -> None:
        await scene.committed_question_with_a_prepared_reply()
        await scene.room_noise()
        scene.gated.gate.set()
        await asyncio.sleep(0.05)

        # The edge turns out to be the owner talking on: the ASR delivers words for it.
        await scene.audio_frames(10, final_text="我还想问一件事")
        await scene.wait_for(lambda: bool(scene.terminals()), timeout=3.0)

        assert not scene.first_frame_sent()
        assert scene.terminals() == ["preempted"]

    await _run(script, name="n8-words")


@pytest.mark.asyncio
async def test_words_that_arrive_before_the_reply_is_ready_supersede_it_as_before() -> None:
    async def script(scene: _Scene) -> None:
        await scene.committed_question_with_a_prepared_reply()
        await scene.vad_start(scene.sample)
        await scene.audio_frames(10, final_text="我还想问一件事")

        scene.gated.gate.set()
        await scene.wait_for(lambda: bool(scene.terminals()), timeout=3.0)

        assert not scene.first_frame_sent()
        assert scene.terminals() == ["preempted"]

    await _run(script, name="n8-words-first")


@pytest.mark.asyncio
async def test_a_floor_that_never_comes_back_does_not_hold_the_reply_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never_expires(self: Any, session_id: str, stream_epoch: int) -> None:
        return None

    # Neither the hold cap nor the tail timeout may reopen the floor in this test.
    monkeypatch.setattr(
        media_session_turns.MediaTurnEndpointMixin,
        "_expire_evidence_less_floor_hold",
        never_expires,
    )
    monkeypatch.setattr(media_session_output_stream, "_UNHEARD_OUTPUT_TEXTLESS_HOLD_WAIT_S", 0.4)

    async def script(scene: _Scene) -> None:
        await scene.committed_question_with_a_prepared_reply()
        await scene.room_noise()
        scene.gated.gate.set()

        await scene.wait_for(lambda: bool(scene.terminals()), timeout=3.0)
        assert not scene.first_frame_sent()
        assert scene.terminals() == ["preempted"]

    await _run(script, name="n8-bounded")


class _Predicate(MediaOutputStreamMixin):
    _pending_turn_has_text_evidence = staticmethod(
        MediaTurnEndpointMixin._pending_turn_has_text_evidence
    )

    def _output_owner_is_current(self, context: Any, lease: Any) -> bool:
        return False  # the user's VAD edge already took the floor


def _context(*, floor_open: bool, turn_start: int | None, partial: str | None = None) -> Any:
    pending = PendingTurn(turn_start_sample=turn_start)
    if partial is not None:
        pending.pending_partial = ASRResult(
            task_epoch=1,
            sentence_id="s",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=1_600,
            text=partial,
            is_final=False,
        )
    return SimpleNamespace(
        identity=SimpleNamespace(session_id="n8-predicate"),
        pending=pending,
        runtime=SimpleNamespace(output_floor_allows_assistant=floor_open, barge_in_enabled=True),
        projection=SimpleNamespace(provisional=None),
    )


@pytest.mark.parametrize(
    ("floor_open", "turn_start", "partial", "held"),
    [
        (False, 640, None, True),  # a VAD edge holds the floor and nothing was heard
        (True, 640, None, False),  # the floor is open: nothing to wait for
        (False, None, None, False),  # the floor is closed for another reason than a user turn
        (False, 640, "我还想问", False),  # the turn has words: the user really holds the floor
        (False, 640, "   ", True),  # whitespace is not a word
    ],
    ids=["noise-edge", "floor-open", "no-user-turn", "words", "blank-text"],
)
def test_only_a_user_turn_with_no_words_is_waited_out(
    floor_open: bool, turn_start: int | None, partial: str | None, held: bool
) -> None:
    context = _context(floor_open=floor_open, turn_start=turn_start, partial=partial)
    assert _Predicate()._floor_held_by_textless_turn(context) is held


@pytest.mark.asyncio
async def test_a_reply_that_is_not_held_says_which_input_decided_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Round 18 lost three prepared replies without a ``held`` line: say why the hold did not apply."""

    context = _context(floor_open=False, turn_start=640, partial="嗯")
    with caplog.at_level(logging.INFO, logger=media_session_output_stream.logger.name):
        waited = await _Predicate()._wait_for_unheard_output_floor(
            context, SimpleNamespace(fence="fence-1"), emitted_audio=False
        )

    assert waited is False
    [line] = [m for m in (r.getMessage() for r in caplog.records) if "prepared reply not held" in m]
    for expected in ("floor_open=False", "turn_started=True", "partial_chars=1", "provisional_chars=0"):
        assert expected in line
    assert "嗯" not in line  # lengths only, never the child's words
