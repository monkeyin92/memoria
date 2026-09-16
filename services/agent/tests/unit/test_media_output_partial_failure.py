"""Failure terminal state after partial audio already reached the device.

A provider that dies or stalls after some PCM was accepted must end the audible
generation promptly and in the same authority chain: one successor generation,
a device cancel/flush, no playback completion, and no resurrection by late
results. The alternative — waiting for the whole segment to drain before the
failure becomes visible — is what makes a stalled reply look like a long answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from livekit.agents import APIConnectionError
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.src.voice_core.media_session_types import MediaReplyChunk
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent
from services.agent.tests.unit.test_media_session import FakeMediaProvider

_CANCEL_GENERATION = media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
_GENERATION_ACTION_COMPLETE = media_pb2.GENERATION_ACTION_COMPLETE


class _PartialThenFail(FakeMediaProvider):
    """One delivered frame, then a hard provider fault.

    The fault is exactly the one the provider budget raises once a generation
    exceeds its hard deadline mid-utterance, so the terminal state is pinned
    against the production failure and not only against a generic crash.
    """

    def __init__(self, failure: BaseException | None = None) -> None:
        super().__init__()
        self._failure = failure or APIConnectionError("total-timeout")

    def generate_reply(
        self,
        _identity: SessionIdentity,
        _user_text: str,
        _fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=b"\x02\x00\x03\x00",
                source_start_sample=0,
                text="前半句",
                first=True,
                final=False,
            )
            raise self._failure

        return chunks()


class _PartialThenStall(FakeMediaProvider):
    """One delivered frame, then a provider that stops making progress."""

    def generate_reply(
        self,
        _identity: SessionIdentity,
        _user_text: str,
        _fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=b"\x02\x00\x03\x00",
                source_start_sample=0,
                text="前半句",
                first=True,
                final=False,
            )
            await asyncio.Event().wait()

        return chunks()


class _CapturingBridge(MediaBridgeGrpcServer):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[Any] = []
        self.effects: list[tuple[int, GenerationFence, str, dict[str, object]]] = []
        self.generations: list[tuple[int, GenerationFence]] = []
        self.states: list[str] = []

    async def emit_pcm(self, _session_id: str, frame: Any) -> bool:
        self.frames.append(frame)
        return True

    async def emit_realtime_effect(
        self,
        _session_id: str,
        effect_kind: int,
        fence: GenerationFence,
        *,
        source_event_id: str,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> bool:
        self.effects.append((effect_kind, fence, source_event_id, payload))
        return True

    async def emit_generation(
        self,
        _session_id: str,
        fence: GenerationFence,
        *,
        action: int,
        **_kwargs: object,
    ) -> bool:
        self.generations.append((action, fence))
        return True

    async def emit_event(
        self,
        _session_id: str,
        event_type: str,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> bool:
        if event_type == "assistant_state":
            self.states.append(str(payload.get("state", "")))
        return True


async def _drive_partial_failure(
    provider: FakeMediaProvider,
    *,
    session_id: str,
    generation_timeout_s: float | None = None,
) -> tuple[_CapturingBridge, Any, GenerationFence, object, float]:
    bridge = _CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    if generation_timeout_s is not None:
        registry.output_generation_timeout_s = generation_timeout_s
    identity = SessionIdentity(session_id)
    context = await registry._get_or_create(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.playback.start(fence)
    started = asyncio.get_running_loop().time()
    outcome: object
    try:
        outcome = await registry.generate_reply(identity.session_id, "你好", fence)
    except Exception as exc:  # the caller must see the provider failure
        outcome = f"{type(exc).__name__}: {exc}"
    elapsed = asyncio.get_running_loop().time() - started
    return bridge, registry, fence, outcome, elapsed


def _assert_terminal_after_partial_audio(
    *,
    bridge: _CapturingBridge,
    context: Any,
    fence: GenerationFence,
    elapsed: float,
    bounded_s: float,
) -> None:
    # Partial audio really reached the device before the failure.
    assert bridge.frames, "the first frame must have been delivered"
    assert len(bridge.frames) == 1

    # One successor generation carries the device flush; nothing in the old
    # generation is treated as a completed reply.
    cancels = [
        item for item in bridge.effects if item[0] == _CANCEL_GENERATION
    ]
    assert len(cancels) == 1
    _kind, cancelled, source_event_id, payload = cancels[0]
    assert cancelled.generation_id > fence.generation_id
    assert cancelled.session_id == fence.session_id
    assert source_event_id in {"output_provider_failed", "output_timeout"}
    # The cancel effect is the device-side "stop speaking" signal: it carries
    # the successor fence the device flushes against.
    assert payload.get("reason") in {"provider_failed", "output_timeout"}
    assert not [
        item for item in bridge.generations if item[0] == _GENERATION_ACTION_COMPLETE
    ]

    # The delivery ledger must record a failure terminal, never playback.
    delivery = context.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.terminal_event is ReplyDeliveryEvent.ERROR
    assert delivery.first_frame_sent is True
    assert context.provider_complete is False

    # The authority returns the floor; the device leaves speaking on the cancel
    # effect above rather than on a fresh assistant_state projection.
    assert context.runtime.interaction_phase.value == "listening"
    assert context.runtime.assistant_speaking is False
    assert "speaking" in bridge.states

    # Failure is visible immediately, not after the segment drains.
    assert elapsed < bounded_s


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [APIConnectionError("total-timeout"), RuntimeError("injected provider fault")],
)
async def test_provider_fault_after_partial_audio_fails_closed_and_promptly(
    failure: BaseException,
) -> None:
    bridge, registry, fence, outcome, elapsed = await _drive_partial_failure(
        _PartialThenFail(failure),
        session_id="partial-audio-provider-fault",
    )
    try:
        assert isinstance(outcome, str) and outcome.startswith(type(failure).__name__)
        _assert_terminal_after_partial_audio(
            bridge=bridge,
            context=registry._sessions[fence.session_id],
            fence=fence,
            elapsed=elapsed,
            bounded_s=1.0,
        )
    finally:
        await registry._finalize_session(fence.session_id)


@pytest.mark.asyncio
async def test_provider_stall_after_partial_audio_fails_closed_within_the_stall_budget() -> None:
    bridge, registry, fence, outcome, elapsed = await _drive_partial_failure(
        _PartialThenStall(),
        session_id="partial-audio-provider-stall",
        generation_timeout_s=0.2,
    )
    try:
        assert outcome is False
        _assert_terminal_after_partial_audio(
            bridge=bridge,
            context=registry._sessions[fence.session_id],
            fence=fence,
            elapsed=elapsed,
            bounded_s=1.0,
        )
    finally:
        await registry._finalize_session(fence.session_id)


@pytest.mark.asyncio
async def test_late_results_cannot_resurrect_a_failed_partial_generation() -> None:
    """Ack/ENDED for the failed generation must stay fenced out."""

    bridge, registry, fence, _outcome, _elapsed = await _drive_partial_failure(
        _PartialThenFail(),
        session_id="partial-audio-late-results",
    )
    context = registry._sessions[fence.session_id]
    try:
        session = bridge.bridge.open(context.identity)
        authority_fence = context.runtime.fence
        delivery = context.reply_delivery.get(fence)
        assert delivery is not None
        terminal_before = delivery.terminal_event
        cancels_before = len(bridge.effects)
        generations_before = len(bridge.generations)

        # A stale terminal ack for the failed generation arrives late, claiming
        # the whole range was rendered and played.
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=context.identity,
                generation_id=fence.generation_id,
                received_sequence=3,
                rendered_sample_end=48_000,
                client_monotonic_ms=1_000,
                approximate=False,
                turn_id=fence.turn_id,
                tool_epoch=fence.tool_epoch,
                session_epoch=fence.session_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )

        assert context.runtime.fence == authority_fence
        assert context.reply_delivery.get(fence) is not None
        assert context.reply_delivery.get(fence).terminal_event is terminal_before
        assert len(bridge.effects) == cancels_before
        assert len(bridge.generations) == generations_before
        assert context.runtime.interaction_phase.value == "listening"
        assert context.runtime.assistant_speaking is False
    finally:
        await registry._finalize_session(fence.session_id)
