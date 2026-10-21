"""An approved crisis line must reach the child even when a wordless turn interrupts.

Production C1 (bridge ``bf245ed1``, session ``a6b8d6ac``, 2026-10-21 03:51:53Z):

- 03:51:53.2「我不想活了」committed; the server emitted ``crisis-self-v1``.
- 03:51:53.441 a device VAD start on echo opened a user turn with no words.
- 03:51:53.4 the first Doubao attempt failed (``failed to synthesize speech
  text_len=100``); the retry produced its first packet at 03:51:55.377.
- 03:51:55.379 ``prepared reply held for a user turn with no words``.
- 03:51:55.555 ``prepared reply dropped cause=textless_turn_over`` ->
  ``status=aborted reason=superseded emitted_audio=False``.

The child heard nothing.  The scenarios below replay that order inside the media
session: the reply is held before its first PCM, a VAD edge opens a wordless turn
that endpoint-commits as ``empty_media_turn`` while the reply waits, and the
script is only released afterwards.  A fixed safety script must still be spoken;
an ordinary reply keeps its previous behaviour, and real words still win.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core import media_session_output_stream, media_session_turns
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import MediaReplyChunk
from services.agent.tests.unit.test_media_session_golden import (
    _REPLY_PCM,
    _Harness,
    _ScriptedProvider,
)
from services.agent.tests.unit.test_media_session_unheard_reply_hold import (
    QUESTION,
    _device_identity,
)
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT

DISPLAY = CRISIS_SELF_SCRIPT.display
ORDINARY = "从前有一座山。"
# The wordless turn is retired by the (shortened) evidence-less cap before the reply
# is released: C1 released the script 1.9 s after the edge, past the 1 s cap.
HOLD_RETIRE_S = 1.2
# The pre-C1 hold cap retired an empty turn about a second after it opened; C1
# released the script 1.9 s after the edge, so the release lands after the cap.


class _FixedScriptProvider(_ScriptedProvider):
    """The committed reply is held (no PCM yet) and may be an approved safety script."""

    def __init__(self, *, text: str, fixed_safety: bool) -> None:
        super().__init__()
        self.text = text
        self.fixed_safety = fixed_safety
        self.gate = asyncio.Event()
        self.reply_calls = 0
        self.safety_fences: set[GenerationFence] = set()

    def generate_reply(
        self,
        _identity: SessionIdentity,
        _user_text: str,
        fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        self.reply_calls += 1
        if self.fixed_safety:
            # The provider marks the fence when the phrase stream yields the approved script.
            self.safety_fences.add(fence)

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            await self.gate.wait()
            yield MediaReplyChunk(
                pcm_s16le=_REPLY_PCM,
                source_start_sample=0,
                text=self.text,
                assistant_text_delta=self.text,
                first=True,
                final=True,
            )

        return chunks()

    def is_fixed_safety_reply(self, fence: GenerationFence) -> bool:
        return fence in self.safety_fences


class _Scene(_Harness):
    def __init__(self, identity: SessionIdentity, provider: _FixedScriptProvider) -> None:
        super().__init__(identity, provider)
        self.gated = provider

    def runtime_fence(self) -> GenerationFence:
        return self.runtime.fence

    def context(self) -> Any:
        return self.registry.session_state(self.identity.session_id)

    def first_frame_sent(self) -> bool:
        return self.count("audio") > 0

    def terminals(self) -> list[str]:
        return [d["terminal_event"] for d in self.deliveries if d["terminal_event"]]

    def wordless_turn_is_gone(self) -> bool:
        state = self.registry.session_state(self.identity.session_id)
        return (
            state.pending.turn_start_sample is None
            and state.runtime.output_floor_allows_assistant
        )

    async def prepare_reply(self) -> GenerationFence:
        await self.utterance(QUESTION, frames=25)
        await self.wait_for(lambda: self.gated.reply_calls == 1)
        assert not self.first_frame_sent()
        return self.runtime.fence


async def _run(scene: _Scene, body: Any) -> None:
    await scene.start()
    try:
        await body(scene)
    finally:
        scene.gated.gate.set()
        await scene.close()


async def _answer_after_the_wordless_turn(scene: _Scene) -> None:
    """C1 order: the edge opens and commits empty while the reply waits, then the reply is ready."""

    await scene.prepare_reply()
    await scene.vad_start(scene.sample)
    await scene.audio_frames(3, final_text="")
    await scene.vad_end(scene.sample)
    # C1: the cap retired the wordless turn first; the script's first packet came
    # 1.9 s after the edge, behind a reply the child had never heard.
    await asyncio.sleep(HOLD_RETIRE_S)
    scene.gated.gate.set()


def _scene(name: str, *, text: str = DISPLAY, fixed_safety: bool = True) -> _Scene:
    return _Scene(
        _device_identity(name),
        _FixedScriptProvider(text=text, fixed_safety=fixed_safety),
    )


@pytest.fixture
def short_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-C1 evidence-less cap: an empty turn is retired about a second after it opens."""

    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_BASE_S", 0.5)
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_VAD_EXTENSION_S", 0.3)
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_MAX_S", 1.0)


@pytest.mark.asyncio
async def test_a_wordless_turn_does_not_drop_the_prepared_crisis_script(
    short_hold: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C1: the script is ready after the noise edge emptied a turn; the child must hear it."""

    scene = _scene("c1-crisis-script")

    async def body(current: _Scene) -> None:
        await _answer_after_the_wordless_turn(current)
        await current.wait_for(current.first_frame_sent, timeout=8.0)
        await current.settle()

        assert "preempted" not in current.terminals()

    with caplog.at_level(logging.INFO, logger="services.agent"):
        await _run(scene, body)

    messages = [record.getMessage() for record in caplog.records]
    joined = " ".join(messages)
    # The wordless turn existed and the reply waited behind it ...
    assert "prepared reply held for a user turn with no words" in joined
    # ... and the C1 drop is not what ended the wait.
    assert "cause=textless_turn_over" not in joined
    # Never the child's words or the script itself.
    for message in messages:
        assert QUESTION not in message
        assert DISPLAY not in message


@pytest.mark.asyncio
async def test_an_ordinary_reply_still_gives_way_to_a_user_turn_with_words() -> None:
    """Control: the fix is scoped to fixed safety scripts; an ordinary held reply still yields."""

    scene = _scene("c1-ordinary-textless", text=ORDINARY, fixed_safety=False)

    async def body(current: _Scene) -> None:
        fence = current.runtime_fence()
        await current.prepare_reply()
        # An ordinary reply is held by the wordless turn exactly as before, and a
        # turn with words on it still supersedes the held reply.
        await current.vad_start(current.sample)
        await current.audio_frames(10, final_text="我还想问一件事")
        current.gated.gate.set()
        await current.wait_for(lambda: bool(current.terminals()), timeout=8.0)

        assert current.terminals() == ["preempted"]
        assert not current.context().output.audio_sent_for(fence)
        assert not current.first_frame_sent()

    await _run(scene, body)


@pytest.mark.asyncio
async def test_words_on_the_new_turn_still_take_the_floor_from_the_script() -> None:
    """A turn with words is a real barge-in, even against a crisis line."""

    scene = _scene("c1-crisis-words")

    async def body(current: _Scene) -> None:
        await current.prepare_reply()
        await current.vad_start(current.sample)
        await current.audio_frames(10, final_text="我还想问一件事")
        current.gated.gate.set()
        await current.wait_for(lambda: bool(current.terminals()), timeout=8.0)

        assert not current.first_frame_sent()
        assert current.terminals() == ["preempted"]

    await _run(scene, body)


@pytest.mark.asyncio
async def test_the_script_release_is_logged_without_the_childs_words(
    short_hold: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The C1 wait still leaves a diagnosable trace when a safety script is released."""

    scene = _scene("c1-crisis-trace")

    async def body(current: _Scene) -> None:
        await _answer_after_the_wordless_turn(current)
        await current.wait_for(current.first_frame_sent, timeout=8.0)

    with caplog.at_level(logging.INFO, logger=media_session_output_stream.logger.name):
        await _run(scene, body)

    lines = [r.getMessage() for r in caplog.records]
    released = [line for line in lines if "prepared reply released" in line]
    assert len(released) == 1
    assert "session=c1-crisis-trace" in released[0]
    for line in lines:
        assert QUESTION not in line
        assert DISPLAY not in line
