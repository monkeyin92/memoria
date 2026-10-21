"""A fixed safety script plays to its end (field 2026-10-21 H01).

crisis-self-v1 was cut after 5.4 s, before 12356: a 3-character final read as
a spoken stop superseded it. While a fixed safety script is audible no stop
word, farewell or next question may stop or supersede it; the turn waits for
the script's end. These scenarios drive the real registry/runtime with a
script reply that stays audible until the test releases it.
"""

from __future__ import annotations

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
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
async def test_device_stop_word_does_not_cut_a_fixed_safety_script(
    script: FixedSafetyScript,
) -> None:
    async def scenario(harness: _StoryHarness) -> None:
        playing = await harness.start_story()
        assert harness.runtime.floor.fixed_safety_audible
        await harness.audio_frames(15, final_text="停")
        await harness.assert_story_still_playing(playing)
        await harness.audio_frames(15, final_text="别说了")
        await harness.assert_story_still_playing(playing)

        await _finish_script(harness)
        await harness.settle()
        assert harness.cancel_effects() == []
        assert [
            d["terminal_event"] for d in harness.deliveries_for(playing) if d["terminal_event"]
        ] != ["preempted"]
        assert harness.story.reply_calls == 1
        assert not harness.closed()

    await _run_story(
        _device_identity(f"safety-stop-{script.template_id}"),
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
