"""A reply that stops progressing must not leave the device speaking forever.

Field 2026-10-20 (``20261020`` runs 2 and 4, ``claude-rca-stuck-speaking.md``):
the server cancelled an audible generation without sending it a
``generation.cancelled``.  The firmware only leaves SPEAKING on a generation
terminal, and the server's silence timer only runs in LISTENING, so the device
stayed in SPEAKING for 4-13 minutes while ``media DTLN stats`` kept growing:
the open microphone fed DTLN and FunASR the whole time.

These drive the real registry/runtime over the public stream.  ``N`` is
injected as 0.2 s instead of the production 15 s; the watchdog compares the
same monotonic clock either way.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.media_session import MediaReplyChunk
from services.agent.src.voice_core.media_session_speaking_watchdog import (
    SPEAKING_STALL_TIMEOUT_S,
)
from services.agent.tests.unit.test_media_session_golden import _REPLY_PCM
from services.agent.tests.unit.test_media_session_playback_stop import (
    _device_identity,
    _LongStoryProvider,
    _StoryHarness,
)

_STALL_S = 0.2
_WATCHDOG_LOGGER = "services.agent.src.voice_core.media_session_speaking_watchdog"


def _dev_identity(session_id: str) -> SessionIdentity:
    return _device_identity(session_id)


class _SteadyProvider(_LongStoryProvider):
    """A long reply whose provider keeps producing PCM at the paced frame rate."""

    def __init__(self, chunks: int = 60) -> None:
        super().__init__()
        self.chunks = chunks

    def generate_reply(
        self,
        _identity: SessionIdentity,
        _user_text: str,
        _fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        self.reply_calls += 1
        total = self.chunks

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            for index in range(total):
                yield MediaReplyChunk(
                    pcm_s16le=_REPLY_PCM,
                    source_start_sample=index * 160,
                    text="从前有一座山。" if index == 0 else "",
                    first=index == 0,
                    final=index == total - 1,
                )

        return chunks()


def _with_stall(harness: _StoryHarness, timeout_s: float = _STALL_S) -> _StoryHarness:
    harness.registry.speaking_stall_timeout_s = timeout_s
    return harness


async def _start_and_lose_the_reply(harness: _StoryHarness) -> GenerationFence:
    """Reproduce the field failure: an audible generation dies with no terminal.

    The first phrase reached the speaker, so the device is in SPEAKING.  The
    output task is cancelled, the owner lease is released, and nothing sends
    ``generation.cancelled``: exactly what run2/run4 logged as
    ``status=aborted reason=output_task_cancelled`` followed by
    ``status=completed reason=delegation_output`` with no terminal event.
    """

    story = await harness.start_story()
    context = harness.context
    task = context.output.reply_task
    assert task is not None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert context.output.reply_task is None
    assert context.output.output_owner is None
    assert context.runtime.assistant_speaking
    assert context.output.audio_sent_for(story), "the generation's first frame must be on the wire"
    return story


def _cancelled_generations(harness: _StoryHarness) -> list[Any]:
    return [
        message.generation
        for message in harness.outputs
        if message.WhichOneof("event") == "generation"
        and message.generation.action == media_pb2.GENERATION_ACTION_CANCEL
    ]


def _close_states(harness: _StoryHarness) -> list[Any]:
    return [
        message.state
        for message in harness.outputs
        if message.WhichOneof("event") == "state"
        and message.state.state == media_pb2.CONVERSATION_STATE_CLOSED
    ]


async def _wait_for_standby(harness: _StoryHarness, context: Any) -> None:
    async def stopped() -> bool:
        return context.standby_requested and bool(_close_states(harness))

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3.0
    while not await stopped():
        if loop.time() >= deadline:
            raise AssertionError("watchdog did not close the stalled session")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_cancelled_audible_reply_stops_uplink_asr_and_stands_by(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _LongStoryProvider()
    harness = _with_stall(_StoryHarness(_dev_identity("stall-cancelled"), provider))
    await harness.start()
    try:
        caplog.set_level(logging.WARNING, logger=_WATCHDOG_LOGGER)
        story = await _start_and_lose_the_reply(harness)
        context = harness.context
        session = harness.bridge.bridge.get(harness.identity.session_id)
        assert session is not None
        audio_before = len(provider.audio_calls)

        await _wait_for_standby(harness, context)

        # The device is told how the generation it is speaking ends, then that
        # the conversation is closed (the standby that closes its microphone).
        [cancel] = _cancelled_generations(harness)
        assert (cancel.turn_id, cancel.generation_id) == (
            story.turn_id,
            story.generation_id,
        )
        assert cancel.reason == "speaking_stalled"
        assert _close_states(harness)[-1].reason == "speaking_stalled"
        assert context.standby_reason == "speaking_stalled"
        assert context.ingress.input_revoked
        assert harness.registry.session_state(harness.identity.session_id) is None

        # No uplink frame reaches the provider after the watchdog fired, even
        # if the transport hands one to the registry's callback.
        assert not session.accepts_uplink()
        await harness.registry.on_audio_frame(
            session,
            AudioFrame(
                identity=harness.identity,
                sequence=harness._audio_sequence,
                capture_start_sample=harness.sample,
                frame_samples=320,
                payload=b"\x00\x10" * 320,
            ),
        )
        assert len(provider.audio_calls) == audio_before

        # The warning carries session, reason and seconds only: no transcript.
        warnings = [
            record
            for record in caplog.records
            if record.name == _WATCHDOG_LOGGER and record.levelno == logging.WARNING
        ]
        assert warnings, "the stalled close must be observable"
        message = warnings[-1].getMessage()
        assert harness.identity.session_id in message
        assert "reason=stalled_speaking" in message
        assert "idle_s=" in message
        assert "timeout_s=" in message
        assert "从前有一座山" not in message
    finally:
        provider.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_a_downlink_stall_after_the_first_frame_stands_by() -> None:
    """The provider already finished; the downlink simply stops moving."""

    provider = _LongStoryProvider()
    harness = _with_stall(_StoryHarness(_dev_identity("stall-downlink"), provider))
    await harness.start()
    try:
        story = await harness.start_story()
        # The provider keeps the stream open and sends nothing: the server
        # still believes it is speaking, the device hears the first phrase.
        await asyncio.sleep(0)
        await _wait_for_standby(harness, harness.context)

        [cancel] = _cancelled_generations(harness)
        assert cancel.generation_id == story.generation_id
        assert cancel.reason == "speaking_stalled"
        assert _close_states(harness)[-1].reason == "speaking_stalled"
    finally:
        provider.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_a_long_reply_with_continuous_downlink_frames_is_not_misjudged() -> None:
    """Frames keep arriving well inside N (20 ms pacing vs a 200 ms window)."""

    provider = _SteadyProvider(chunks=90)
    harness = _with_stall(_StoryHarness(_dev_identity("stall-normal-long"), provider))
    await harness.start()
    try:
        started = asyncio.get_running_loop().time()
        await harness.utterance("给我讲个故事", frames=25)
        await harness.wait_for(
            lambda: harness.count(
                "generation", lambda g: g.action == media_pb2.GENERATION_ACTION_COMPLETE
            )
            >= 1,
            timeout=5.0,
        )
        # 90 frames at 20 ms pacing: the reply spans many injected windows.
        assert asyncio.get_running_loop().time() - started > _STALL_S * 3
        context = harness.context
        assert not context.standby_requested
        assert not context.closed
        assert _cancelled_generations(harness) == []
        assert _close_states(harness) == []
        assert harness.count("audio") == provider.chunks
    finally:
        provider.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_a_stop_that_already_sent_the_terminal_does_not_trip_the_watchdog() -> None:
    """「停」 ends the generation itself; the watchdog must stay out of it."""

    provider = _LongStoryProvider()
    harness = _with_stall(_StoryHarness(_dev_identity("stall-after-stop"), provider))
    await harness.start()
    try:
        story = await harness.start_story()
        await harness.audio_frames(15, final_text="停")
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        # Give the (injected, 200 ms) watchdog several windows to misfire.
        await asyncio.sleep(0.6)
        assert not harness.context.standby_requested
        assert _close_states(harness) == []
        assert harness.runtime.fence.generation_id == story.generation_id + 1
    finally:
        provider.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_the_production_window_is_the_documented_15_seconds() -> None:
    assert SPEAKING_STALL_TIMEOUT_S == 15.0


@pytest.mark.asyncio
async def test_compliance_stuck_speaking_bounds_uplink_asr_and_archive() -> None:
    """Compliance 2026-10-20: once stuck, within N seconds the server stops
    taking uplink audio, ASR forwards drop to zero, and nothing said during
    the stuck window reaches the spool or the archive."""

    provider = _LongStoryProvider()
    harness = _with_stall(_StoryHarness(_dev_identity("stall-compliance"), provider))
    archived: list[dict[str, Any]] = []
    owner_turns: list[dict[str, Any]] = []

    async def record_evidence(event: dict[str, Any]) -> None:
        archived.append(event)

    async def record_owner_turn(event: dict[str, Any], pcm: bytes, sample_rate: int) -> None:
        owner_turns.append(event)

    # The same two hooks media_agent_factory._bind_archive wires to
    # ArchiveSink.publish / publish_owner_turn (encrypt + spool write).
    harness.runtime.set_evidence_publisher(record_evidence)
    harness.runtime.set_owner_turn_publisher(record_owner_turn)
    await harness.start()
    try:
        await _start_and_lose_the_reply(harness)
        context = harness.context
        session = harness.bridge.bridge.get(harness.identity.session_id)
        assert session is not None
        loop = asyncio.get_running_loop()
        stuck_at = loop.time()

        # 1) Uplink stops within the bounded window: N plus one poll interval
        #    plus scheduling slack, never unbounded.
        await _wait_for_standby(harness, context)
        stopped_after = loop.time() - stuck_at
        assert stopped_after <= _STALL_S + harness.registry._speaking_stall_poll_s + 0.5
        assert context.ingress.input_revoked
        assert not session.accepts_uplink()

        # 2) After the deadline ASR forwards are zero, even though the device
        #    keeps streaming its open microphone and ASR would return a final.
        forwarded_before = len(provider.audio_calls)
        archived_before = len(archived)
        stuck_text = "卡住期间孩子说的话"
        await harness.audio_frames(25, final_text=stuck_text)
        await asyncio.sleep(_STALL_S)
        assert len(provider.audio_calls) - forwarded_before == 0

        # 3) Nothing from the stuck window is persisted: no evidence event
        #    (spool/archive) and no owner-turn audio, let alone its transcript.
        assert len(archived) == archived_before
        assert owner_turns == []
        assert all(stuck_text not in str(event) for event in archived)
    finally:
        provider.release.set()
        await harness.close()
