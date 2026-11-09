"""Media session: playback completion failures and TTS-unavailable standby."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.state_machine import (
    ConversationState,
)
from services.agent.src.providers.tts_failover import TTSUnavailableError
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.playback_ledger import PlaybackSpan
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _device_identity,
)
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT


@pytest.mark.asyncio
async def test_approximate_device_progress_completes_without_actual_heard() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    metrics = MetricsRegistry()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
        metrics=metrics,
    )
    registry.install()
    identity = SessionIdentity(
        "device-approximate-playback",
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="subject",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
    )
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    assert await context.runtime.accept_media_generation(fence, cause="test")
    await context.runtime.on_assistant_speaking("你好")
    context.runtime.orchestrator.state_machine.state = ConversationState.SPEAKING
    context.output.playback.start(fence)
    assert context.output.playback.register_audio(fence, 0, 0, 320)
    assert context.output.playback.add_span(
        PlaybackSpan(
            fence=fence,
            text_start=0,
            text_end=2,
            audio_start_sample=0,
            audio_end_sample=320,
            text="你好",
            sequence=0,
        )
    )
    context.output.provider_complete = True

    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=320,
            client_monotonic_ms=1,
            approximate=True,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )

    assert context.output.playback.actual_heard_text(fence) == ""
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    assert all(turn.content != "你好" for turn in context.runtime.orchestrator.context.turns)
    delivery = context.output.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.terminal_event is ReplyDeliveryEvent.PLAYBACK_ENDED
    assert delivery.actual_heard is False
    assert (
        metrics.get("voice_conversation_participation_proxy_ms_total", {"kind": "assistant"}) == 0
    )


@pytest.mark.asyncio
async def test_typed_device_playback_error_is_not_successful_completion() -> None:
    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.controls: list[tuple[int, GenerationFence, str]] = []

        async def emit_generation(
            self,
            _session_id: str,
            fence: GenerationFence,
            *,
            action: int,
            reason: str = "",
            **_kwargs: object,
        ) -> bool:
            self.controls.append((action, fence, reason))
            return True

    provider = FakeMediaProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity(
        "device-playback-error",
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="subject",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
    )
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    assert await context.runtime.accept_media_generation(fence, cause="test")
    await context.runtime.on_assistant_speaking("你好")
    context.runtime.orchestrator.state_machine.state = ConversationState.SPEAKING
    context.output.playback.start(fence)
    assert context.output.playback.register_audio(fence, 0, 0, 320)
    context.output.provider_complete = True
    context.output.assistant_text = CRISIS_SELF_SCRIPT.display
    published: list[str] = []
    registry.crisis_script_interrupt_publisher = published.append

    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=0,
            client_monotonic_ms=1,
            approximate=True,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            event_type=PlaybackEventType.ERROR,
        ),
    )

    delivery = context.output.reply_delivery.get(fence)
    assert delivery is not None
    assert delivery.terminal_event is ReplyDeliveryEvent.ERROR
    assert delivery.playback_ended is False
    assert context.runtime.orchestrator.state is ConversationState.LISTENING
    assert context.runtime.fence.generation_id == fence.generation_id + 1
    assert bridge.controls == [
        (
            media_pb2.GENERATION_ACTION_CANCEL,
            context.runtime.fence,
            "device_playback_error",
        )
    ]
    assert len(published) == 1


@pytest.mark.asyncio
async def test_tts_unavailable_requests_standby_with_play_line_when_no_audio_sent() -> None:
    """TTS failover exhaustion with no audio sent requests standby with play_line=True."""

    standby_calls: list[tuple[str, bool]] = []

    class FailingProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                raise TTSUnavailableError("all TTS engines failed")
                yield  # type: ignore[unreachable]

            return chunks()

    class StandbyCapturingRegistry(MediaVoiceCoreRegistry):
        async def _request_device_standby(
            self,
            context: Any,
            *,
            reason: str,
            expected_activity_revision: int | None = None,
            expected_endpoint: tuple[int, int] | None = None,
            play_line: bool = False,
        ) -> bool:
            standby_calls.append((reason, play_line))
            return True

    bridge = MediaBridgeGrpcServer()
    registry = StandbyCapturingRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FailingProvider(),
    )
    registry.install()
    identity = _device_identity("tts-unavailable-no-audio")
    bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.output.playback.start(fence)
    with pytest.raises(TTSUnavailableError, match="all TTS engines failed"):
        await registry.generate_reply(identity.session_id, "你好", fence)
    assert len(standby_calls) == 1
    assert standby_calls[0][0] == "tts_unavailable"
    assert standby_calls[0][1] is True
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_tts_unavailable_does_not_request_standby_when_audio_already_sent() -> None:
    """TTS failover exhaustion after audio was sent does not request standby."""

    standby_calls: list[tuple[str, bool]] = []

    class FailingProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                raise TTSUnavailableError("all TTS engines failed")
                yield  # type: ignore[unreachable]

            return chunks()

    class StandbyCapturingRegistry(MediaVoiceCoreRegistry):
        async def _request_device_standby(
            self,
            context: Any,
            *,
            reason: str,
            expected_activity_revision: int | None = None,
            expected_endpoint: tuple[int, int] | None = None,
            play_line: bool = False,
        ) -> bool:
            standby_calls.append((reason, play_line))
            return True

    bridge = MediaBridgeGrpcServer()
    registry = StandbyCapturingRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FailingProvider(),
    )
    registry.install()
    identity = _device_identity("tts-unavailable-with-audio")
    bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.output.playback.start(fence)
    context.output.reply_delivery.record(fence, ReplyDeliveryEvent.FIRST_FRAME_SENT)
    delivery = context.output.reply_delivery.get(fence)
    assert delivery is not None and delivery.first_frame_sent is True
    with pytest.raises(TTSUnavailableError, match="all TTS engines failed"):
        await registry.generate_reply(identity.session_id, "你好", fence)
    assert len(standby_calls) == 0
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_user_stop_does_not_request_tts_unavailable_standby() -> None:
    """A user stop does not trigger TTS unavailable standby."""

    standby_calls: list[tuple[str, bool]] = []

    class StandbyCapturingRegistry(MediaVoiceCoreRegistry):
        async def _request_device_standby(
            self,
            context: Any,
            *,
            reason: str,
            expected_activity_revision: int | None = None,
            expected_endpoint: tuple[int, int] | None = None,
            play_line: bool = False,
        ) -> bool:
            standby_calls.append((reason, play_line))
            return True

    bridge = MediaBridgeGrpcServer()
    registry = StandbyCapturingRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FakeMediaProvider(),
    )
    registry.install()
    identity = _device_identity("user-stop-no-tts-unavailable")
    bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    await context.runtime.on_turn_committed("你好")
    await registry._request_device_standby(context, reason="user_stop")
    tts_unavailable_calls = [
        (reason, play_line)
        for reason, play_line in standby_calls
        if reason == "tts_unavailable"
    ]
    assert len(tts_unavailable_calls) == 0
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_generic_provider_failure_does_not_request_tts_unavailable_standby() -> None:
    """A non-TTS provider failure class never triggers the fallback-line standby."""

    standby_calls: list[tuple[str, bool]] = []

    class FailingProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                raise RuntimeError("language model unavailable")
                yield  # type: ignore[unreachable]

            return chunks()

    class StandbyCapturingRegistry(MediaVoiceCoreRegistry):
        async def _request_device_standby(
            self,
            context: Any,
            *,
            reason: str,
            expected_activity_revision: int | None = None,
            expected_endpoint: tuple[int, int] | None = None,
            play_line: bool = False,
        ) -> bool:
            standby_calls.append((reason, play_line))
            return True

    bridge = MediaBridgeGrpcServer()
    registry = StandbyCapturingRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: FailingProvider(),
    )
    registry.install()
    identity = _device_identity("generic-failure-no-standby")
    bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.output.playback.start(fence)
    with pytest.raises(RuntimeError, match="language model unavailable"):
        await registry.generate_reply(identity.session_id, "你好", fence)
    assert standby_calls == []
    await registry.finalize_session(identity.session_id)
