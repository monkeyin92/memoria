"""A reply cut off after its first frame must not leave the device in SPEAKING (N-8, round 18 p03).

Field 2026-10-05 (round 18, ``n8-pilot`` p03): the first frame of a committed reply was on the speaker when
the device VAD fired for 20 ms of room noise.  The server took the floor for the edge, the next provider
chunk found the owner no longer current and aborted the audible reply as ``superseded``; the abort's
CANCEL_GENERATION effect became a ``playback.flush`` that made the firmware wait for the replacement
generation's audio, and nothing ever followed it (the edge was empty-handed, so was the runtime, which had
restored LISTENING).  The device stayed in SPEAKING for 30.76 s until the edge closed the session on its
``owner_silence_timeout``, and whatever the child said meanwhile never got in.  The spoken-stop path already
ends that replacement generation; the abort path now does too.  These drive the real registry/runtime over the
public stream.
"""

from __future__ import annotations

from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.media_protocol import DEVICE_POST_PLAYBACK_HOLDOFF_S
from services.agent.tests.unit.test_media_session_golden import _h5_identity
from services.agent.tests.unit.test_media_session_playback_stop import (
    _device_identity,
    _LongStoryProvider,
    _run_story,
    _StoryHarness,
)


def _replacement_events(harness: _StoryHarness, story: GenerationFence) -> list[Any]:
    return [
        message.generation
        for message in harness.outputs
        if message.WhichOneof("event") == "generation"
        and message.generation.generation_id == story.generation_id + 1
    ]


async def _noise_edge_over_the_audible_reply(harness: _StoryHarness) -> GenerationFence:
    """The first phrase is on the speaker; a VAD edge with no words takes the floor from it."""

    story = await harness.start_story()
    await harness.vad_start(harness.sample)
    assert not harness.runtime.output_floor_allows_assistant
    # The next provider chunk is what notices that the reply lost the floor.
    harness.story.release.set()
    await harness.wait_for(lambda: bool(harness.cancel_effects()))
    return story


@pytest.mark.asyncio
async def test_a_reply_superseded_after_its_first_frame_ends_the_replacement_generation() -> None:
    async def script(harness: _StoryHarness) -> None:
        story = await _noise_edge_over_the_audible_reply(harness)

        [effect] = harness.cancel_effects()
        assert effect.source_event_id == "output_superseded"
        assert (effect.turn_id, effect.generation_id) == (story.turn_id, story.generation_id + 1)
        # playback.flush made the firmware wait for the replacement generation's audio and no reply follows
        # it: it must end with a CANCEL (a COMPLETE tears the stream down, see the stop-path test) or the
        # device stays in SPEAKING until the edge's 30 s owner-silence close.
        [terminal] = _replacement_events(harness, story)
        assert terminal.action == media_pb2.GENERATION_ACTION_CANCEL
        assert terminal.turn_id == story.turn_id
        assert terminal.reason == "output_superseded"
        flush_at = next(i for i, m in enumerate(harness.outputs) if m.WhichOneof("event") == "realtime_effect")
        terminal_at = next(
            i
            for i, m in enumerate(harness.outputs)
            if m.WhichOneof("event") == "generation"
            and m.generation.generation_id == story.generation_id + 1
        )
        assert flush_at < terminal_at
        assert [d["terminal_event"] for d in harness.deliveries_for(story) if d["terminal_event"]] == [
            "preempted"
        ]
        assert not harness.runtime.assistant_speaking
        assert not harness.closed()

    await _run_story(_device_identity("superseded-audible"), script)


@pytest.mark.asyncio
async def test_the_next_question_after_the_cut_off_reply_gets_its_own_reply() -> None:
    async def script(harness: _StoryHarness) -> None:
        story = await _noise_edge_over_the_audible_reply(harness)
        calls = harness.story.reply_calls

        # The noise turn ends empty; the child asks again and is answered on a fresh generation.
        await harness.vad_end(harness.sample)
        await harness.utterance("换一个故事吧", frames=25)
        await harness.wait_for(lambda: harness.story.reply_calls == calls + 1)
        await harness.wait_for(lambda: harness.count("audio") >= 2)

        assert harness.runtime.fence.generation_id > story.generation_id + 1
        assert len(_replacement_events(harness, story)) == 1  # nothing else rode the dead replacement
        assert not harness.closed()
        assert not harness.context.standby_requested and not harness.context.closed

    await _run_story(_device_identity("superseded-then-next"), script)


@pytest.mark.asyncio
async def test_without_a_device_flush_there_is_no_replacement_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def script(harness: _StoryHarness) -> None:
        monkeypatch.setattr(
            type(harness.registry),
            "_device_playback_flush_required",
            staticmethod(lambda _context, _fence: False),
        )
        story = await harness.start_story()
        await harness.vad_start(harness.sample)
        harness.story.release.set()
        await harness.wait_for(lambda: harness.runtime.fence.generation_id > story.generation_id)
        await harness.settle()

        assert harness.cancel_effects() == []
        assert _replacement_events(harness, story) == []

    await _run_story(_device_identity("superseded-no-flush"), script)


@pytest.mark.asyncio
async def test_a_flush_that_never_reached_the_edge_leaves_no_replacement_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def script(harness: _StoryHarness) -> None:
        async def effect_rejected(*_args: Any, **_kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(harness.bridge, "emit_realtime_effect", effect_rejected)
        story = await harness.start_story()
        await harness.vad_start(harness.sample)
        harness.story.release.set()
        await harness.wait_for(lambda: harness.runtime.fence.generation_id > story.generation_id)
        await harness.settle()

        assert _replacement_events(harness, story) == []

    await _run_story(_device_identity("superseded-flush-rejected"), script)


@pytest.mark.asyncio
async def test_only_the_firmware_waits_for_a_replacement_generation() -> None:
    async def script(harness: _StoryHarness) -> None:
        story = await _noise_edge_over_the_audible_reply(harness)

        assert len(harness.cancel_effects()) == 1
        assert _replacement_events(harness, story) == []

    provider = _LongStoryProvider()
    harness = _StoryHarness(_h5_identity("superseded-h5"), provider)
    # Same barge-in runtime as the device's; only the client differs.
    harness.runtime = DuplexRuntime.create(
        session_id="superseded-h5",
        input_guard_enabled=True,
        barge_in_enabled=True,
        capture_release_holdoff_s=DEVICE_POST_PLAYBACK_HOLDOFF_S,
    )
    await harness.start()
    try:
        await script(harness)
    finally:
        provider.release.set()
        await harness.close()
