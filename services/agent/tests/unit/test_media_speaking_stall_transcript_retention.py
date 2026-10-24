"""What the ASR heard before a speaking-stall close leaves the session.

#345 closes a device session 15 s after downlink PCM stops while the runtime
still believes the assistant is speaking.  The close path is allowed to end
the session; it is not allowed to rewrite what happened before it.

The contract pinned here, at the archive/transcript boundary the session owns:

* A round that committed before the stall keeps its ``speech.utterance_finalized``
  evidence after the close, and an undelivered event waits on the encrypted
  spool instead of vanishing with the process.
* Evidence is metadata-only (#282): no payload carries the spoken text, so
  "retained" means the event survives, never the words.
* Speech the ASR accepts inside the stall window is not persisted.  #345
  requires that a stuck conversation stop feeding the archive, so the last
  uncommitted final is dropped with the pending turn even though the ASR
  supervisor already heard it.

Together these answer "does the 15 s window lose conversation text": there is
no text store to lose it from, and the window's speech is deliberately outside
the evidence record.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from services.agent.src.archive_sink import ArchiveSink, ArchiveSinkConfig
from services.agent.src.voice_core.aigc_notice import AigcNoticeConfig
from services.agent.tests.unit.media_session_support import _bind_verified_owner_classifier
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.test_media_session_playback_stop import (
    _device_identity,
    _LongStoryProvider,
    _StoryHarness,
)
from services.agent.tests.unit.test_media_speaking_watchdog import _with_stall

_STALL_S = 1.0
_HEARD_INSIDE_THE_WINDOW = "卡住前说的话"
_TRANSCRIPT_KEYS = ("text", "transcript", "user_text", "assistant_text")


class _DeviceSession(_StoryHarness):
    """Device story session with the archive/owner-turn sinks wired as in production."""

    def __init__(self, session_id: str, provider: _LongStoryProvider) -> None:
        super().__init__(_device_identity(session_id), provider)
        # These cases pin the stall/archive contract, not notice timing.
        self.registry.aigc_notice_config = AigcNoticeConfig(enabled=False)
        bind_owner_policy(self.runtime)
        _bind_verified_owner_classifier(self.runtime)
        self.evidence: list[dict[str, Any]] = []
        self.owner_turns: list[dict[str, Any]] = []

    def bind_recording_sinks(self) -> None:
        async def publish(event: dict[str, Any]) -> None:
            self.evidence.append(event)

        async def publish_owner_turn(event: dict[str, Any], pcm: bytes, sample_rate: int) -> None:
            self.owner_turns.append(event)

        # The two hooks media_agent_factory._bind_archive wires.
        self.runtime.set_evidence_publisher(publish)
        self.runtime.set_owner_turn_publisher(publish_owner_turn)

    def utterance_events(self) -> list[dict[str, Any]]:
        return [
            event for event in self.evidence if event["event_type"] == "speech.utterance_finalized"
        ]


async def _lose_the_reply(harness: _StoryHarness) -> None:
    """The field #345 state: audible generation gone, device still SPEAKING."""

    story = await harness.start_story()
    context = harness.context
    task = context.output.reply_task
    assert task is not None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert context.runtime.assistant_speaking
    assert context.output.audio_sent_for(story)


async def _wait_until_closed(harness: _StoryHarness) -> Any:
    """Wait out the stall close and return the context the close retired."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    context = harness.context
    assert context is not None
    while harness.registry.session_state(harness.identity.session_id) is not None:
        if loop.time() >= deadline:
            raise AssertionError("the stall close did not finish")
        await asyncio.sleep(0.01)
    await harness.settle()
    return context


@pytest.mark.asyncio
async def test_completed_rounds_before_the_stall_keep_their_evidence() -> None:
    """Every finished round is archived before the close and survives it."""

    harness = _with_stall(
        _DeviceSession("stall-retention-rounds", _LongStoryProvider(story_call=3)),
        _STALL_S,
    )
    harness.bind_recording_sinks()
    await harness.start()
    try:
        for index, text in enumerate(("第一轮问题", "第二轮问题"), start=1):
            await harness.utterance(text, frames=10)
            await harness.wait_for(lambda index=index: harness.story.reply_calls == index)
        await _lose_the_reply(harness)
        archived_before_close = len(harness.utterance_events())

        context = await _wait_until_closed(harness)

        assert context.standby_reason == "speaking_stalled"
        # Two answered rounds plus the story request that stalled.
        assert len(harness.utterance_events()) == archived_before_close == 3
        assert [event["turn_id"] for event in harness.utterance_events()] == [1, 2, 3]
    finally:
        harness.story.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_archived_evidence_carries_no_transcript_text() -> None:
    """The archive contract after #282: metadata only, for every subject."""

    harness = _with_stall(_DeviceSession("stall-retention-text", _LongStoryProvider()), _STALL_S)
    harness.bind_recording_sinks()
    await harness.start()
    try:
        await _lose_the_reply(harness)
        await _wait_until_closed(harness)

        assert harness.evidence, "the session must archive at least its turn"
        for event in harness.evidence:
            assert not set(_TRANSCRIPT_KEYS) & set(event["payload"]), event["event_type"]
    finally:
        harness.story.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_speech_accepted_inside_the_stall_window_never_reaches_evidence() -> None:
    """Compliance #345: speech in the stuck window stays out of the archive.

    The device negotiated ``interrupt_assist``: firmware suppresses playback
    vad.start, so the utterance arrives as audio plus one ASR final and never
    pins an endpoint.  The ASR supervisor accepts it (it is in the provisional
    transcript), but no commit follows, so the close drops it with
    ``PendingTurn.reset`` and no evidence event exists for it.  That is the
    required outcome: persisting it would put stuck-window speech into the
    spool and the archive, which the watchdog exists to prevent.
    """

    harness = _with_stall(
        _DeviceSession("stall-retention-window", _LongStoryProvider()),
        _STALL_S,
    )
    harness.bind_recording_sinks()
    await harness.start()
    try:
        await _lose_the_reply(harness)
        before = len(harness.utterance_events())

        # Playback-window speech: audio and one final, no VAD edge.
        await harness.audio_frames(10, final_text=_HEARD_INSIDE_THE_WINDOW)
        context = harness.context
        assert context.projection.provisional is not None
        assert context.projection.provisional.text == _HEARD_INSIDE_THE_WINDOW
        assert context.pending.turn_endpoint_sample is None
        # The words do reach the device as a live subtitle; nothing persists them.
        assert (
            harness.count("transcript", lambda event: event.text == _HEARD_INSIDE_THE_WINDOW) >= 1
        )

        await _wait_until_closed(harness)

        assert len(harness.utterance_events()) == before
        assert all(
            _HEARD_INSIDE_THE_WINDOW not in json.dumps(event, ensure_ascii=False)
            for event in harness.evidence
        )
        assert harness.owner_turns == []
    finally:
        harness.story.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_undelivered_evidence_waits_on_the_encrypted_spool(tmp_path: Path) -> None:
    """A Control outage leaves the session's events on disk, still text-free."""

    key = Fernet.generate_key().decode()
    spool = tmp_path / "archive-events.spool"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session-events"):
            return httpx.Response(503, json={"detail": "unavailable"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = ArchiveSink(
        ArchiveSinkConfig(
            endpoint="http://archive.test/v1/archive/session-events",
            internal_token="token",
            spool_path=spool,
            spool_key=key,
        ),
        client=client,
    )
    harness = _with_stall(_DeviceSession("stall-retention-spool", _LongStoryProvider()), _STALL_S)

    async def publish(event: dict[str, Any]) -> None:
        await sink.publish(event)

    async def publish_owner_turn(event: dict[str, Any], pcm: bytes, sample_rate: int) -> None:
        await sink.publish_owner_turn(event, pcm=pcm, sample_rate=sample_rate)

    harness.runtime.set_evidence_publisher(publish)
    harness.runtime.set_owner_turn_publisher(publish_owner_turn)
    await harness.start()
    try:
        await _lose_the_reply(harness)
        await _wait_until_closed(harness)
        await sink.close()

        rows = [
            json.loads(Fernet(key).decrypt(line))
            for line in spool.read_bytes().splitlines()
            if line
        ]
        bodies = [row["body"] for row in rows]
        assert [body["event_type"] for body in bodies].count("speech.utterance_finalized") == 1
        for body in bodies:
            assert not set(_TRANSCRIPT_KEYS) & set(body["payload"]), body["event_type"]
    finally:
        harness.story.release.set()
        await harness.close()
        await sink.close()
        await client.aclose()
