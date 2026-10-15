"""Playback endings: a terminal phrase stands by, a crisis script stops only after the pin."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from services.agent.src.voice_core.media_session_standby import MediaSessionStandbyMixin
from services.agent.src.voice_core.playback_lifecycle import (
    crisis_playback_stop_blocked,
    generation_control_reason,
    mark_played_crisis_script_interrupted,
)


def test_an_unconfirmed_crisis_script_does_not_arm_stop() -> None:
    context = SimpleNamespace(
        standby_after_playback=False,
        runtime=SimpleNamespace(playback_stop_suppressed=True),
    )

    assert (
        generation_control_reason(context, "assistant_reply", terminal=False)
        == "safety_pin_unconfirmed"
    )


def test_standby_after_a_terminal_phrase_does_not_resume_the_silence_budget() -> None:
    calls: list[object] = []
    session = MediaSessionStandbyMixin()
    session._pause_owner_silence_timer = lambda _context: calls.append("pause")  # type: ignore[method-assign]
    session._arm_owner_silence_timer = lambda _context, *, reset: calls.append(("arm", reset))  # type: ignore[method-assign]
    context = SimpleNamespace(standby_after_playback=True)

    session._sync_owner_silence_phase(context, "listening")

    assert calls == ["pause"]
    calls.clear()
    context.standby_after_playback = False
    session._sync_owner_silence_phase(context, "listening")
    assert calls == [("arm", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("template_id", ["crisis-self-v1", "victim-support-v1"])
async def test_interrupting_a_confirmed_crisis_script_reports_only_the_event_id(
    template_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    reported: list[str] = []

    async def report(event_id: str) -> None:
        reported.append(event_id)

    runtime = SimpleNamespace(
        safety_reply_playing=True,
        guardian_pin_confirmed=True,
        guardian_crisis_template_id=template_id,
        guardian_crisis_event_id="evt-crisis-1",
        playback_stop_suppressed=False,
        report_crisis_script_interrupted=report,
    )
    runtime.clear_safety_reply_playing = lambda: setattr(runtime, "safety_reply_playing", False)
    context = SimpleNamespace(
        runtime=runtime,
        identity=SimpleNamespace(session_id="ses-crisis"),
    )

    assert crisis_playback_stop_blocked(context) is False
    await mark_played_crisis_script_interrupted(context)

    assert reported == ["evt-crisis-1"]
    assert runtime.safety_reply_playing is False
    assert f"reply_template_id={template_id}" in caplog.text
    assert "evt-crisis-1" in caplog.text
    assert "停" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("template_id", ["crisis-self-v1", "victim-support-v1"])
async def test_an_unconfirmed_crisis_script_is_not_marked_interrupted(template_id: str) -> None:
    reported: list[str] = []

    async def report(event_id: str) -> None:
        reported.append(event_id)

    runtime = SimpleNamespace(
        safety_reply_playing=True,
        guardian_pin_confirmed=False,
        guardian_crisis_template_id=template_id,
        guardian_crisis_event_id=None,
        report_crisis_script_interrupted=report,
        clear_safety_reply_playing=lambda: None,
    )
    context = SimpleNamespace(
        runtime=runtime,
        identity=SimpleNamespace(session_id="ses-crisis"),
    )

    assert crisis_playback_stop_blocked(context) is True
    await mark_played_crisis_script_interrupted(context)

    assert reported == []
    assert runtime.safety_reply_playing is True
