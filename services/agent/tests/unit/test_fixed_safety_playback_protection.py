"""A fixed safety script plays to completion even when the child says 「停」.

Field 2026-10-21 H01 cut crisis-self-v1 after 5.4 s, before 12356. Product and
compliance decided on 2026-10-22 00:40 that the fixed safety script is not
interruptible by voice, exactly like a head pat or screen tap (#355 changes
the earlier 2026-10-21 stop rule). A stop must not cut the line, and it must
not raise the parent's unfinished-script reminder; that reminder is reserved
for power loss, network loss and playback failure.
"""

from __future__ import annotations

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.tests.unit.test_media_session_golden import _h5_identity
from services.agent.tests.unit.test_media_session_playback_stop import (
    _device_identity,
    _run_story,
    _StoryHarness,
)
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    FixedSafetyScript,
)

_SCRIPTS = pytest.mark.parametrize(
    "script",
    [CRISIS_SELF_SCRIPT, SUPPORT_VICTIM_SCRIPT, SUPPORT_RELAY_SCRIPT],
    ids=lambda script: script.template_id,
)


async def _finish_script(harness: _StoryHarness) -> None:
    harness.story.release.set()
    await harness.settle()
    await harness.playback_ended()
    await harness.wait_for(lambda: not harness.runtime.assistant_speaking)


@_SCRIPTS
@pytest.mark.asyncio
async def test_device_stop_word_does_not_cut_the_crisis_script(
    script: FixedSafetyScript,
) -> None:
    """「停」 is ignored while the fixed safety script is audible."""

    async def scenario(harness: _StoryHarness) -> None:
        published: list[str] = []
        harness.registry.crisis_script_interrupt_publisher = published.append
        playing = await harness.start_story()
        assert harness.runtime.floor.fixed_safety_audible
        await harness.audio_frames(15, final_text="停")
        await harness.assert_story_still_playing(playing)
        assert published == []
        assert not harness.closed()

        await _finish_script(harness)
        await harness.settle()
        assert harness.cancel_effects() == []
        assert published == []

    await _run_story(
        _device_identity(f"safety-stop-{script.template_id}"),
        scenario,
        opening=script.display,
    )


@pytest.mark.asyncio
async def test_device_hard_stop_keyword_does_not_cut_the_crisis_script() -> None:
    """The device already flushed locally; the Edge generation must stay playable."""

    async def scenario(harness: _StoryHarness) -> None:
        playing = await harness.start_story()
        await harness.send(
            media_pb2.MediaToCore(
                keyword=media_pb2.KeywordEvent(
                    identity=harness.wire_identity,
                    keyword="停",
                    confidence=0.95,
                    start_sample=harness.sample,
                    end_sample=harness.sample + 320,
                    hard_stop=True,
                )
            )
        )
        await harness.assert_story_still_playing(playing)
        bridge_session = harness.bridge.bridge.get(harness.identity.session_id)
        assert bridge_session is not None
        assert bridge_session.generation_active
        assert bridge_session.fence.matches(playing)

        frames_before_release = harness.count("audio")
        await _finish_script(harness)
        await harness.settle()
        assert harness.count("audio") > frames_before_release
        assert harness.cancel_effects() == []

    await _run_story(
        _device_identity("safety-hard-stop"),
        scenario,
        opening=CRISIS_SELF_SCRIPT.display,
    )


@_SCRIPTS
@pytest.mark.asyncio
async def test_device_non_stop_turns_do_not_cut_a_fixed_safety_script(
    script: FixedSafetyScript,
) -> None:
    """Wait/completion turns and new questions keep #346 protection."""

    async def scenario(harness: _StoryHarness) -> None:
        playing = await harness.start_story()
        assert harness.runtime.floor.fixed_safety_audible
        for text in ("等一下", "好了", "我们下午几点出发"):
            await harness.audio_frames(15, final_text=text)
            await harness.assert_story_still_playing(playing)
        await _finish_script(harness)
        await harness.settle()
        assert harness.cancel_effects() == []
        assert harness.story.reply_calls == 1
        assert not harness.closed()

    await _run_story(
        _device_identity(f"safety-nonstop-{script.template_id}"),
        scenario,
        opening=script.display,
    )


@pytest.mark.asyncio
async def test_device_farewell_waits_for_the_crisis_script_to_end() -> None:
    async def scenario(harness: _StoryHarness) -> None:
        playing = await harness.start_story()
        await harness.audio_frames(15, final_text="再见")
        await harness.assert_story_still_playing(playing)
        assert not harness.closed()

        await _finish_script(harness)
        assert harness.cancel_effects() == []

    await _run_story(
        _device_identity("safety-farewell"),
        scenario,
        opening=CRISIS_SELF_SCRIPT.display,
    )


@pytest.mark.asyncio
async def test_h5_spoken_stop_does_not_cut_the_crisis_script() -> None:
    async def scenario(harness: _StoryHarness) -> None:
        playing = await harness.start_story()
        await harness.utterance("停一下", frames=15)
        await harness.assert_story_still_playing(playing)

        await _finish_script(harness)
        await harness.settle()
        assert harness.cancel_effects() == []
        assert harness.story.reply_calls == 1

    await _run_story(
        _h5_identity("h5-safety-stop"),
        scenario,
        opening=CRISIS_SELF_SCRIPT.display,
    )


@pytest.mark.asyncio
async def test_device_stop_word_still_stops_an_ordinary_story() -> None:
    async def scenario(harness: _StoryHarness) -> None:
        playing = await harness.start_story()
        assert not harness.runtime.floor.fixed_safety_audible
        await harness.audio_frames(15, final_text="停")
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        harness.assert_story_stopped_session_open(playing)

    await _run_story(_device_identity("ordinary-story-stop"), scenario)


def _speaking_runtime(text: str) -> DuplexRuntime:
    runtime = DuplexRuntime.create(session_id="safety-floor")
    runtime._voice_floor.update(pending_assistant_text=text, assistant_speaking=True)
    return runtime


@_SCRIPTS
def test_fixed_safety_playback_blocks_barge_in_while_audible(script: FixedSafetyScript) -> None:
    runtime = _speaking_runtime(script.display)

    assert runtime.floor.fixed_safety_audible
    assert runtime.playback_overlap_input_blocked()


def test_first_phrase_of_the_crisis_script_is_already_protected() -> None:
    runtime = _speaking_runtime(CRISIS_SELF_SCRIPT.phrases[0].display)

    assert runtime.floor.fixed_safety_audible


def test_ordinary_reply_keeps_barge_in() -> None:
    runtime = _speaking_runtime("从前有一座山，山里有一座庙。")

    assert not runtime.floor.fixed_safety_audible
    assert not runtime.playback_overlap_input_blocked()


def test_protection_ends_when_the_script_is_no_longer_audible() -> None:
    runtime = _speaking_runtime(CRISIS_SELF_SCRIPT.display)
    runtime._voice_floor.update(pending_assistant_text="", assistant_speaking=False)

    assert not runtime.floor.fixed_safety_audible
    assert not runtime.playback_overlap_input_blocked()
