from __future__ import annotations

import asyncio
from typing import Any

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.fixed_speech import FixedSpeechPlayer


class _Handle:
    def __init__(self) -> None:
        self.done = asyncio.Event()
        self.interrupted = False

    async def wait_for_playout(self) -> None:
        await self.done.wait()

    def interrupt(self, *, force: bool = False) -> None:
        assert force is True
        self.interrupted = True
        self.done.set()


class _Session:
    def __init__(self, handle: _Handle, *, fail: bool = False) -> None:
        self.handle = handle
        self.fail = fail
        self.calls: list[tuple[str, dict[str, object]]] = []

    def say(self, text: str, **kwargs: object) -> _Handle:
        self.calls.append((text, kwargs))
        if self.fail:
            raise RuntimeError("say failed")
        return self.handle


class _TTS:
    def __init__(self) -> None:
        self.bound: list[object] = []
        self.plans: list[dict[str, object]] = []

    def bind_fence(self, fence: object) -> None:
        self.bound.append(fence)

    def apply_speech_plan(self, **kwargs: object) -> None:
        self.plans.append(kwargs)


@pytest.mark.asyncio
async def test_fixed_speech_publishes_fenced_speaking_before_session_say() -> None:
    runtime = DuplexRuntime.create(session_id="fixed-speech", barge_in_enabled=False)
    await runtime.orchestrator.ready()
    state_release = asyncio.Event()
    state_started = asyncio.Event()
    policy_release = asyncio.Event()
    policy_started = asyncio.Event()
    published: list[dict[str, Any]] = []

    async def publish(event: dict[str, Any]) -> None:
        if event["type"] == "assistant_state" and event["state"] == "speaking":
            state_started.set()
            await state_release.wait()
        if event["type"] == "input_policy" and event["reason"] == "assistant_speaking":
            policy_started.set()
            await policy_release.wait()
        published.append(event)

    runtime.set_event_publisher(publish)
    handle = _Handle()
    session = _Session(handle)
    tts = _TTS()
    player = FixedSpeechPlayer(session, runtime, tts, settle_delay_s=0)
    fence = runtime.fence

    task = asyncio.create_task(player.say("欢迎回来", emotion="happy", rate=1.05))
    await asyncio.gather(state_started.wait(), policy_started.wait())
    await asyncio.sleep(0)
    assert session.calls == []
    assert runtime._was_speaking is True

    state_release.set()
    await asyncio.sleep(0)
    assert session.calls == []
    policy_release.set()
    while not session.calls:
        await asyncio.sleep(0)
    assert tts.bound == [fence]
    assert tts.plans == [
        {
            "emotion": "happy",
            "rate": 1.05,
            "instruction": "",
            "fence": fence,
        }
    ]
    handle.done.set()
    await task

    states = [event for event in published if event["type"] == "assistant_state"]
    assert [event["state"] for event in states] == ["speaking", "listening"]
    assert all(event["generation_id"] == fence.generation_id for event in states)
    assert runtime._was_speaking is False


@pytest.mark.asyncio
async def test_fixed_speech_failure_restores_listening_and_stops_claiming_floor() -> None:
    runtime = DuplexRuntime.create(session_id="fixed-speech-failure")
    await runtime.orchestrator.ready()
    published: list[dict[str, Any]] = []

    async def publish(event: dict[str, Any]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    player = FixedSpeechPlayer(
        _Session(_Handle(), fail=True),
        runtime,
        _TTS(),
        settle_delay_s=0,
    )

    with pytest.raises(RuntimeError, match="say failed"):
        await player.say("无法播放")

    assert [
        event["state"]
        for event in published
        if event["type"] == "assistant_state"
    ] == ["speaking", "listening"]
    assert runtime._was_speaking is False


@pytest.mark.asyncio
async def test_fixed_speech_publication_failure_still_releases_current_floor() -> None:
    runtime = DuplexRuntime.create(
        session_id="fixed-speech-publish-failure",
        barge_in_enabled=False,
    )
    await runtime.orchestrator.ready()
    published: list[dict[str, Any]] = []

    async def publish(event: dict[str, Any]) -> None:
        if event["type"] == "input_policy" and event["reason"] == "assistant_speaking":
            raise RuntimeError("policy publish failed")
        published.append(event)

    runtime.set_event_publisher(publish)
    player = FixedSpeechPlayer(
        _Session(_Handle()),
        runtime,
        _TTS(),
        settle_delay_s=0,
    )

    with pytest.raises(RuntimeError, match="policy publish failed"):
        await player.say("无法进入播放")

    assert [
        event["state"]
        for event in published
        if event["type"] == "assistant_state"
    ] == ["speaking", "listening"]
    assert runtime._was_speaking is False
    assert runtime._pending_assistant_text == ""


@pytest.mark.asyncio
async def test_fixed_speech_cancellation_stops_handle_and_restores_listening() -> None:
    runtime = DuplexRuntime.create(session_id="fixed-speech-cancel")
    await runtime.orchestrator.ready()
    published: list[dict[str, Any]] = []

    async def publish(event: dict[str, Any]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    handle = _Handle()
    session = _Session(handle)
    player = FixedSpeechPlayer(session, runtime, _TTS(), settle_delay_s=0)
    task = asyncio.create_task(player.say("会被取消的欢迎语"))
    while not session.calls:
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert handle.interrupted is True
    assert runtime._was_speaking is False
    assert [
        event["state"]
        for event in published
        if event["type"] == "assistant_state"
    ] == ["speaking", "listening"]


@pytest.mark.asyncio
async def test_stale_fixed_speech_cleanup_cannot_release_a_new_generation() -> None:
    runtime = DuplexRuntime.create(session_id="fixed-speech-stale")
    await runtime.orchestrator.ready()
    published: list[dict[str, Any]] = []

    async def publish(event: dict[str, Any]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    handle = _Handle()
    player = FixedSpeechPlayer(
        _Session(handle),
        runtime,
        _TTS(),
        settle_delay_s=0,
    )
    task = asyncio.create_task(player.say("旧代次欢迎语"))
    while not runtime._was_speaking:
        await asyncio.sleep(0)

    old_fence = runtime.fence
    await runtime.on_real_interrupt(
        cause="new_device_generation",
        create_user_turn=False,
        force_generation_bump=True,
    )
    assert not runtime.fence.matches(old_fence)
    runtime._was_speaking = True
    handle.done.set()
    await task

    stale_listening = [
        event
        for event in published
        if event["type"] == "assistant_state"
        and event["state"] == "listening"
        and event["generation_id"] == old_fence.generation_id
    ]
    assert stale_listening == []
    assert runtime._was_speaking is True


@pytest.mark.asyncio
async def test_fixed_enrollment_prompt_restores_enrollment_state() -> None:
    runtime = DuplexRuntime.create(session_id="fixed-speech-enroll")
    await runtime.orchestrator.ready()
    published: list[dict[str, Any]] = []

    async def publish(event: dict[str, Any]) -> None:
        published.append(event)

    runtime.set_event_publisher(publish)
    handle = _Handle()
    handle.done.set()
    player = FixedSpeechPlayer(_Session(handle), runtime, _TTS(), settle_delay_s=0)

    await player.say("请登记声音", restore_state="speaker_enroll")

    assert [
        event["state"]
        for event in published
        if event["type"] == "assistant_state"
    ] == ["speaking", "speaker_enroll"]
