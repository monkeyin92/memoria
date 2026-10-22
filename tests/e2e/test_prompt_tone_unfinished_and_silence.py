"""Prompt-tone loss marks an unfinished crisis script. Silence past 8s closes.

Power loss and a network drop that does not reconnect are different transports
and the same parent reminder. The opening cue is already playback: the script
text is not on the wire yet. Silence under the 8s boundary stays listening and
is not uploaded.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from services.agent.src.orchestration.state_machine import InteractionPhase
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.media_session import MediaReplyChunk
from services.agent.tests.unit.test_media_session_golden import (
    _REPLY_PCM,
    _device_identity,
    _Harness,
    _ScriptedProvider,
)
from services.agent.tests.unit.test_media_session_playback_stop import (
    _LongStoryProvider,
    _StoryHarness,
)
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT
from services.guardian.crisis import minor_crisis_event_id


class _OpeningCueProvider(_LongStoryProvider):
    """Opening cue first, then the crisis script, held until the test releases it."""

    def generate_reply(
        self,
        _identity: object,
        _user_text: str,
        _fence: object,
    ) -> AsyncIterator[MediaReplyChunk]:
        self.reply_calls += 1
        release = self.release

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=_REPLY_PCM,
                source_start_sample=0,
                text="",
                first=True,
                final=False,
                aigc_marker=True,
            )
            await release.wait()
            yield MediaReplyChunk(
                pcm_s16le=_REPLY_PCM,
                source_start_sample=160,
                text=CRISIS_SELF_SCRIPT.display,
                final=True,
            )

        return chunks()


def _closed(harness: _Harness) -> bool:
    return (
        harness.count(
            "state",
            lambda state: state.state == media_pb2.CONVERSATION_STATE_CLOSED,
        )
        > 0
    )


async def _play_opening_cue(harness: _StoryHarness) -> list[str]:
    published: list[str] = []
    harness.registry.crisis_script_interrupt_publisher = published.append
    await harness.start_story()
    context = harness.context
    assert context.output.assistant_text == ""
    assert harness.runtime.assistant_speaking
    harness.runtime.note_fixed_safety_spoken(CRISIS_SELF_SCRIPT.display)
    return published


def _expected_event(harness: _StoryHarness) -> str:
    fence = harness.runtime.fence
    return minor_crisis_event_id(
        minor_user_id=harness.identity.subject_id,
        session_id=harness.identity.session_id,
        turn_id=fence.turn_id,
        generation_id=fence.generation_id,
        tool_epoch=fence.tool_epoch,
    )


async def _drop_stream(harness: _StoryHarness) -> None:
    await harness._requests.put(None)
    if harness._reader is not None:
        await asyncio.wait_for(harness._reader, timeout=2)


@pytest.mark.asyncio
async def test_power_loss_during_the_opening_cue_marks_the_script_unfinished() -> None:
    published: list[str] = []

    async def scenario(harness: _StoryHarness) -> None:
        nonlocal published
        published = await _play_opening_cue(harness)
        session = harness.bridge.bridge.get(harness.identity.session_id)
        assert session is not None
        session.close()
        await _drop_stream(harness)
        await harness.wait_for(lambda: len(published) == 1)

        assert published == [_expected_event(harness)]

    provider = _OpeningCueProvider()
    harness = _StoryHarness(_device_identity("power-during-cue"), provider)
    await harness.start()
    try:
        await scenario(harness)
    finally:
        provider.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_network_loss_during_the_opening_cue_marks_the_script_unfinished() -> None:
    published: list[str] = []

    async def scenario(harness: _StoryHarness) -> None:
        nonlocal published
        published = await _play_opening_cue(harness)
        harness.registry.reconnect_grace_s = 0.2
        session = harness.bridge.bridge.get(harness.identity.session_id)
        assert session is not None
        assert session.terminal_requested is False
        await _drop_stream(harness)
        await harness.wait_for(lambda: len(published) == 1, timeout=2)

        assert published == [_expected_event(harness)]

    provider = _OpeningCueProvider()
    harness = _StoryHarness(_device_identity("network-during-cue"), provider)
    await harness.start()
    try:
        await scenario(harness)
    finally:
        provider.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_silence_past_eight_seconds_closes_and_does_not_upload() -> None:
    provider = _ScriptedProvider()
    harness = _Harness(
        _device_identity("silence-eight-seconds"),
        provider,
        owner_silence_timeout_s=8.0,
    )
    await harness.start()
    try:
        await harness.utterance("你好呀", frames=8)
        await harness.wait_for(lambda: harness.count("audio") >= 1)
        await harness.playback_ended()
        await harness.wait_for(
            lambda: harness.runtime.interaction_phase is InteractionPhase.LISTENING
        )
        session = harness.registry.session_state(harness.identity.session_id)
        assert session is not None
        deadline = session.owner_silence_deadline
        assert deadline is not None
        loop = asyncio.get_running_loop()
        assert deadline - loop.time() >= 7.5
        ingested = len(provider.audio_calls)
        for _ in range(10):
            await harness._requests.put(
                media_pb2.MediaToCore(
                    audio=media_pb2.AudioFrame(
                        identity=harness.wire_identity,
                        sequence=harness._audio_sequence,
                        capture_start_sample=harness.sample,
                        frame_samples=320,
                        payload=b"\x00\x00" * 320,
                    )
                )
            )
            harness._audio_sequence += 1
            harness.sample += 320
        await harness.settle()
        assert len(provider.audio_calls) == ingested
        assert session.ingress.listening_silence_dropped_frames >= 10
        shifted = session.owner_silence_deadline
        assert shifted is not None
        assert abs(shifted - deadline) < 0.25
        deadline = shifted
        remaining = deadline - loop.time()
        assert remaining > 1.0
        await asyncio.sleep(remaining - 1.0)
        assert not _closed(harness)
        await harness.wait_for(lambda: _closed(harness), timeout=3.0)
        assert loop.time() >= deadline - 0.05
        closed = [
            message.state
            for message in harness.outputs
            if message.WhichOneof("event") == "state"
            and message.state.state == media_pb2.CONVERSATION_STATE_CLOSED
        ]
        assert [state.reason for state in closed] == ["owner_silence_timeout"]
    finally:
        await harness.close()
