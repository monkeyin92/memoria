"""An approved crisis line must reach the child even when a wordless turn interrupts.

Production C1 (bridge ``bf245ed1``, session ``a6b8d6ac``, 2026-10-21 03:51:53Z):

- 03:51:53.2「我不想活了」committed; the server emitted ``crisis-self-v1``.
- 03:51:53.441 a device VAD start on echo opened a user turn with no words.
- 03:51:53.4 the first Doubao attempt failed (``failed to synthesize speech
  text_len=100``); the retry produced its first packet at 03:51:55.377.
- 03:51:55.379 ``prepared reply held for a user turn with no words``.
- 03:51:55.555 ``prepared reply dropped cause=textless_turn_over`` ->
  ``status=aborted reason=superseded emitted_audio=False``.

The scenarios below replay that order: the reply is ready behind a wordless turn
that produced no ASR text.  Such a turn no longer holds the reply (soak
2026-10-29), so the script speaks at once; words on the turn still take the floor.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
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


async def _speak_after_the_wordless_edge(scene: _Scene) -> None:
    """C1 order: the edge opens while the reply waits, and the reply is ready with no words."""

    await scene.prepare_reply()
    await scene.vad_start(scene.sample)
    scene.gated.gate.set()
    await scene.wait_for(scene.first_frame_sent, timeout=2.0)
    # The empty edge ends after the script started; it must not take it back.
    await scene.audio_frames(3, final_text="")
    await scene.vad_end(scene.sample)


def _scene(name: str, *, text: str = DISPLAY, fixed_safety: bool = True) -> _Scene:
    return _Scene(
        _device_identity(name),
        _FixedScriptProvider(text=text, fixed_safety=fixed_safety),
    )


@pytest.mark.asyncio
async def test_a_wordless_turn_does_not_drop_the_prepared_crisis_script(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C1: the script is ready behind the noise edge; the child must hear it at once."""

    scene = _scene("c1-crisis-script")

    async def body(current: _Scene) -> None:
        await _speak_after_the_wordless_edge(current)
        await current.settle()

        assert "preempted" not in current.terminals()

    with caplog.at_level(logging.INFO, logger="services.agent"):
        await _run(scene, body)

    messages = [record.getMessage() for record in caplog.records]
    joined = " ".join(messages)
    # The wordless turn existed, and the script went out without waiting on it.
    assert "prepared reply released past a user turn with no words" in joined
    assert "prepared reply held for a user turn with no words" not in joined
    assert "evidence-less floor hold capped" not in joined
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
