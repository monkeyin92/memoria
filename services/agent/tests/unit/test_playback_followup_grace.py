"""The post-playback follow-up grace can be shortened for the latency experiment (N-14 tier 3).

After a reply the child's next sentence has no device VAD edge, so its ASR final is endpointed with a
fixed 1.2 s grace.  Shortening it saves that long on every follow-up question; the risk is a child's
mid-sentence pause being cut into two turns.  Whether to shorten it for good is a robot experiment
(docs/runbooks/followup-grace-experiment.md); until then the default is unchanged and an unreadable
setting must never shorten it.
"""

from __future__ import annotations

import time

import pytest
from services.agent.src.voice_core import media_session_playback_stop as playback_stop
from services.agent.src.voice_core.media_session_playback_stop import (
    PLAYBACK_FOLLOWUP_GRACE_ENV,
    playback_followup_grace_s,
)
from services.agent.tests.unit.media_session_support import _user_turn_texts
from services.agent.tests.unit.test_media_session_playback_stop import (
    _device_identity,
    _run_story,
    _StoryHarness,
)


def test_without_the_setting_the_grace_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PLAYBACK_FOLLOWUP_GRACE_ENV, raising=False)
    assert playback_followup_grace_s() == 1.2
    monkeypatch.setenv(PLAYBACK_FOLLOWUP_GRACE_ENV, "  ")
    assert playback_followup_grace_s() == 1.2


@pytest.mark.parametrize("raw", ["0.3", "0.6", "0.9", "1.2", " 0.75 "])
def test_a_shorter_grace_in_range_is_used(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(PLAYBACK_FOLLOWUP_GRACE_ENV, raw)
    assert playback_followup_grace_s() == float(raw)


@pytest.mark.parametrize("raw", ["abc", "0.2", "0", "-1", "1.5", "3", "nan", "inf", "1,2"])
def test_anything_else_keeps_the_default_instead_of_shortening_it_blindly(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(PLAYBACK_FOLLOWUP_GRACE_ENV, raw)
    assert playback_followup_grace_s() == 1.2


def test_the_default_is_the_ceiling_the_turn_budget_was_sized_for() -> None:
    assert playback_stop._PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S == 1.2


async def _seconds_to_answer_a_followup(session_id: str, *parts: str) -> float:
    """The reply has played out; the child's next sentence arrives as ASR finals without a VAD edge.

    The time is counted from the arrival of the last final to the model being asked.
    """

    measured: dict[str, float] = {}

    async def script(harness: _StoryHarness) -> None:
        await harness.utterance("你好", frames=25)
        await harness.wait_for(lambda: harness.count("audio") >= 1)
        await harness.audio_frames(60)
        await harness.playback_ended()
        await harness.audio_frames(60)  # past the 0.8 s echo margin
        for part in parts:
            started = time.monotonic()
            await harness.audio_frames(25, final_text=part)
        await harness.wait_for(lambda: harness.story.reply_calls == 2, timeout=5.0)
        measured["seconds"] = time.monotonic() - started
        assert _user_turn_texts(harness.context) == ["你好", " ".join(parts)]

    await _run_story(_device_identity(session_id), script, story_call=0)
    return measured["seconds"]


@pytest.mark.asyncio
async def test_a_shorter_grace_answers_the_followup_sooner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PLAYBACK_FOLLOWUP_GRACE_ENV, raising=False)
    question = "那我明天出门要带伞吗"
    default = await _seconds_to_answer_a_followup("followup-grace-default", question)
    monkeypatch.setenv(PLAYBACK_FOLLOWUP_GRACE_ENV, "0.3")
    shortened = await _seconds_to_answer_a_followup("followup-grace-short", question)

    assert default >= 1.1, "the default still waits the full 1.2 s grace"
    assert shortened <= default - 0.6, (default, shortened)


@pytest.mark.asyncio
async def test_the_moved_endpoint_of_a_continuing_sentence_uses_the_shortened_grace_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second final that follows the first within the grace moves the endpoint and restarts the wait."""

    parts = ("那我明天", "出门要带伞吗")
    monkeypatch.delenv(PLAYBACK_FOLLOWUP_GRACE_ENV, raising=False)
    default = await _seconds_to_answer_a_followup("followup-advance-default", *parts)
    monkeypatch.setenv(PLAYBACK_FOLLOWUP_GRACE_ENV, "0.3")
    shortened = await _seconds_to_answer_a_followup("followup-advance-short", *parts)

    assert default >= 1.1
    assert shortened <= default - 0.6, (default, shortened)
