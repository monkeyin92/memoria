"""A cancelled crisis-self script reports only its event id, after the stop returns."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from services.agent.src.voice_core.crisis_script_interrupt import (
    CrisisScriptInterruptReporter,
    crisis_script_interrupt_endpoint,
    crisis_self_interrupt_event_id,
    publish_crisis_self_interrupt,
)
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
)
from services.guardian.crisis import minor_crisis_event_id

_UTTERANCE = "我不会做题。我不想活了"
_EVENT = minor_crisis_event_id(
    minor_user_id="minor-1",
    session_id="session-1",
    turn_id=4,
    generation_id=2,
    tool_epoch=1,
)


def _event_id(*, announced_text: str, crisis_reply_heard: bool = False, minor_user_id: str = "minor-1") -> str | None:
    return crisis_self_interrupt_event_id(
        announced_text=announced_text,
        crisis_reply_heard=crisis_reply_heard,
        minor_user_id=minor_user_id,
        session_id="session-1",
        turn_id=4,
        generation_id=2,
        tool_epoch=1,
    )


def test_only_an_in_progress_crisis_self_or_victim_script_produces_an_event_id() -> None:
    assert _event_id(announced_text=CRISIS_SELF_SCRIPT.display) == _EVENT
    assert _event_id(announced_text=CRISIS_SELF_SCRIPT.spoken) == _EVENT
    assert _event_id(announced_text="你说的话很重要") == _EVENT
    assert _event_id(announced_text=SUPPORT_VICTIM_SCRIPT.display) == _EVENT
    assert _event_id(announced_text=SUPPORT_VICTIM_SCRIPT.spoken) == _EVENT
    assert _event_id(announced_text="你说的事情很重要") == _EVENT
    assert _event_id(announced_text=CRISIS_SELF_SCRIPT.display, crisis_reply_heard=True) is None
    assert _event_id(announced_text=SUPPORT_VICTIM_SCRIPT.display, crisis_reply_heard=True) is None
    assert _event_id(announced_text=SUPPORT_RELAY_SCRIPT.display) is None
    assert _event_id(announced_text="你说的") is None
    assert _event_id(announced_text=_UTTERANCE) is None
    assert _event_id(announced_text="停") is None
    assert _event_id(announced_text=CRISIS_SELF_SCRIPT.display, minor_user_id="") is None


def test_publish_sends_the_event_id_and_drops_the_utterance_and_keyword() -> None:
    seen: list[str] = []
    publish_crisis_self_interrupt(
        seen.append,
        announced_text=CRISIS_SELF_SCRIPT.display,
        crisis_reply_heard=False,
        minor_user_id="minor-1",
        session_id="session-1",
        turn_id=4,
        generation_id=2,
        tool_epoch=1,
    )
    assert seen == [_EVENT]
    assert _UTTERANCE not in seen[0]
    assert "停" not in seen[0]
    assert "自杀" not in seen[0]

    publish_crisis_self_interrupt(
        seen.append,
        announced_text=SUPPORT_VICTIM_SCRIPT.display,
        crisis_reply_heard=False,
        minor_user_id="minor-1",
        session_id="session-1",
        turn_id=4,
        generation_id=2,
        tool_epoch=1,
    )
    assert seen == [_EVENT, _EVENT]

    publish_crisis_self_interrupt(
        seen.append,
        announced_text=SUPPORT_RELAY_SCRIPT.display,
        crisis_reply_heard=False,
        minor_user_id="minor-1",
        session_id="session-1",
        turn_id=4,
        generation_id=2,
        tool_epoch=1,
    )
    assert seen == [_EVENT, _EVENT]


def test_interrupt_endpoint_uses_the_response_plan_origin() -> None:
    assert crisis_script_interrupt_endpoint(
        "http://control-api:8000/v1/interaction/response-plan?x=1"
    ) == "http://control-api:8000/v1/guardian/crisis-script-interrupted"


@pytest.mark.asyncio
async def test_submit_posts_only_the_event_id_and_returns_before_the_response() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"crisis_event_id": _EVENT}
        assert request.headers["X-Memoria-Internal-Token"] == "t" * 32
        started.set()
        await release.wait()
        return httpx.Response(200)

    reporter = CrisisScriptInterruptReporter(
        endpoint="http://control-api:8000/v1/guardian/crisis-script-interrupted",
        token="t" * 32,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    reporter.submit(_EVENT)
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await asyncio.gather(*list(reporter._tasks))
    await reporter.aclose()
