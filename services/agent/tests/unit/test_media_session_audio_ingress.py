"""Media session: PCM pacing and audio-ingress failure handling."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaSessionResources,
    MediaVoiceCoreRegistry,
)
from services.agent.src.voice_core.media_session_output_stream import _next_pcm_send_slot
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    open_bridge_connection,
)


def test_pcm_output_pacer_spaces_frames_and_does_not_burst_after_a_stall() -> None:
    delay, next_send_at = _next_pcm_send_slot(
        now=10.0,
        next_send_at=10.0,
        frame_samples=480,
    )
    assert delay == 0
    assert next_send_at == pytest.approx(10.02)

    delay, next_send_at = _next_pcm_send_slot(
        now=10.005,
        next_send_at=next_send_at,
        frame_samples=480,
    )
    assert delay == pytest.approx(0.015)
    assert next_send_at == pytest.approx(10.04)

    delay, next_send_at = _next_pcm_send_slot(
        now=11.0,
        next_send_at=next_send_at,
        frame_samples=480,
    )
    assert delay == 0
    assert next_send_at == pytest.approx(11.02)


@pytest.mark.asyncio
async def test_registry_rejects_late_audio_after_terminal_cleanup() -> None:
    identity = SessionIdentity(
        "terminal-registry-input",
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
    )
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    connection = open_bridge_connection(bridge, identity)
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    await registry.open_session(identity)
    await registry.finalize_session(identity.session_id)

    assert bridge.bridge.is_terminal(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
    )
    await registry.on_audio_frame(
        connection.session,
        AudioFrame(
            identity=identity,
            sequence=0,
            capture_start_sample=0,
            frame_samples=2,
            payload=b"\x00\x00\x01\x00",
        ),
    )
    assert registry.context(identity.session_id) is None
    with pytest.raises(ValueError, match="media session is terminal"):
        await registry.open_session(identity)


@pytest.mark.asyncio
async def test_old_epoch_provider_callback_cannot_update_speaker() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(FakeMediaProvider):
        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            started.set()
            await release.wait()
            return (
                ASRResult(
                    task_epoch=1,
                    sentence_id="old-epoch",
                    revision=1,
                    capture_start_sample=frame.capture_start_sample,
                    capture_end_sample=frame.capture_end_sample,
                    text="旧连接结果",
                    is_final=True,
                    stream_epoch=frame.identity.stream_epoch,
                ),
            )

    identity = SessionIdentity("old-epoch-callback", stream_epoch=1)
    provider = BlockingProvider()
    bridge = MediaBridgeGrpcServer()
    runtime = DuplexRuntime.create(session_id=identity.session_id)
    speaker_pcm: list[bytes] = []
    runtime.feed_speaker_pcm = speaker_pcm.append  # type: ignore[method-assign]
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
    )
    registry.install()
    session = bridge.bridge.open(identity)

    callback = asyncio.create_task(
        registry.on_audio_frame(
            session,
            AudioFrame(identity, 0, 0, 320, b"\x00\x00" * 320),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await registry.open_session(SessionIdentity(identity.session_id, stream_epoch=2))
    release.set()
    await asyncio.wait_for(callback, timeout=1)

    assert speaker_pcm == []
    await runtime.close()
    await provider.close(identity)


@pytest.mark.asyncio
async def test_slow_new_session_does_not_block_existing_session_audio() -> None:
    bridge = MediaBridgeGrpcServer()
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    resources: dict[str, MediaSessionResources] = {}

    async def build_session(identity: SessionIdentity) -> MediaSessionResources:
        if identity.session_id == "slow-new-session":
            slow_started.set()
            await release_slow.wait()
        resources[identity.session_id] = MediaSessionResources(
            DuplexRuntime.create(session_id=identity.session_id),
            FakeMediaProvider(),
        )
        return resources[identity.session_id]

    registry = MediaVoiceCoreRegistry(bridge=bridge, session_factory=build_session)
    registry.install()
    existing_identity = SessionIdentity("existing-session")
    existing_session = bridge.bridge.open(existing_identity)
    await registry.open_session(existing_identity)

    slow_task = asyncio.create_task(registry.open_session(SessionIdentity("slow-new-session")))
    await asyncio.wait_for(slow_started.wait(), timeout=1)
    audio_task = asyncio.create_task(
        registry.on_audio_frame(
            existing_session,
            AudioFrame(
                identity=existing_identity,
                sequence=0,
                capture_start_sample=0,
                frame_samples=2,
                payload=b"\x00\x00\x01\x00",
            ),
        )
    )
    completed, _ = await asyncio.wait({audio_task}, timeout=0.05)
    release_slow.set()
    await slow_task
    if audio_task not in completed:
        audio_task.cancel()
        await asyncio.gather(audio_task, return_exceptions=True)
    for resource in resources.values():
        await resource.runtime.close()
        await resource.provider.close(existing_identity)

    assert audio_task in completed
    assert resources[existing_identity.session_id].provider.audio_calls == [0]


@pytest.mark.asyncio
async def test_audio_ingress_overflow_drops_oldest_and_marks_discontinuity() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.reset_samples: list[int] = []

        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            if frame.sequence == 0:
                started.set()
                await release.wait()
            return ()

        async def reset_after_discontinuity(
            self,
            _identity: SessionIdentity,
            *,
            capture_start_sample: int,
        ) -> None:
            self.reset_samples.append(capture_start_sample)

    bridge = MediaBridgeGrpcServer()
    provider = SlowProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        audio_ingress_max_frames=2,
    )
    identity = SessionIdentity("bounded-audio-pump")
    session = bridge.bridge.open(identity)

    for sequence in range(4):
        await asyncio.wait_for(
            registry.on_audio_frame(
                session,
                AudioFrame(
                    identity=identity,
                    sequence=sequence,
                    capture_start_sample=sequence * 2,
                    frame_samples=2,
                    payload=b"\x00\x00\x01\x00",
                ),
            ),
            timeout=0.05,
        )
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    for _ in range(20):
        if provider.audio_calls == [0, 2, 3]:
            break
        await asyncio.sleep(0)

    # Overflow drops only the oldest buffered frame (sequence 1) so the newest
    # speech still reaches the provider; both gap boundaries reset the task.
    assert provider.audio_calls == [0, 2, 3]
    assert provider.reset_samples == [4, 6]
    assert registry.metrics.get("media_pcm_overflow_total") >= 1
    assert registry.metrics.get("media_discontinuity_total") >= 1
    session.close()
    await registry.on_session_closed(session)


@pytest.mark.asyncio
async def test_audio_ingress_provider_failure_drains_backlog_and_recovers_next_frame() -> None:
    class RecoveringProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.recoveries = 0
            self.reset_samples: list[int] = []

        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            if frame.sequence == 0:
                self.first_started.set()
                await self.release_first.wait()
                raise RuntimeError("provider send failed")
            return ()

        async def recover_after_failure(self, _identity: SessionIdentity) -> None:
            self.recoveries += 1

        async def reset_after_discontinuity(
            self,
            _identity: SessionIdentity,
            *,
            capture_start_sample: int,
        ) -> None:
            self.reset_samples.append(capture_start_sample)

    provider = RecoveringProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        audio_ingress_max_frames=4,
    )
    identity = SessionIdentity("provider-send-recovery", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)

    await registry.on_audio_frame(session, AudioFrame(identity, 0, 0, 2, b"\x00\x00" * 2))
    await asyncio.wait_for(provider.first_started.wait(), timeout=1)
    await registry.on_audio_frame(session, AudioFrame(identity, 1, 2, 2, b"\x01\x00" * 2))
    await registry.on_audio_frame(session, AudioFrame(identity, 2, 4, 2, b"\x02\x00" * 2))
    provider.release_first.set()
    for _ in range(50):
        if context.ingress.provider_failed:
            break
        await asyncio.sleep(0)

    assert provider.audio_calls == [0]
    assert context.ingress.queue.empty()
    assert context.ingress.provider_failed is True

    await registry.on_audio_frame(session, AudioFrame(identity, 3, 6, 2, b"\x03\x00" * 2))
    for _ in range(50):
        if provider.audio_calls == [0, 3]:
            break
        await asyncio.sleep(0)

    assert provider.recoveries == 1
    assert provider.reset_samples == [6]
    assert provider.audio_calls == [0, 3]
    assert context.ingress.provider_failed is False
    await context.runtime.close()


@pytest.mark.asyncio
async def test_audio_ingress_lazy_task_start_failure_drains_backlog_and_resets_at_recovery_frame() -> (
    None
):
    class LazyStartFailureProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.failure_started = asyncio.Event()
            self.release_failure = asyncio.Event()
            self.recoveries = 0
            self.reset_samples: list[int] = []

        async def ingest_audio(
            self,
            _identity: SessionIdentity,
            frame: AudioFrame,
        ) -> Sequence[ASRResult]:
            self.audio_calls.append(frame.sequence)
            if frame.sequence == 1:
                self.failure_started.set()
                await self.release_failure.wait()
                raise RuntimeError("lazy task start failed")
            return ()

        async def recover_after_failure(self, _identity: SessionIdentity) -> None:
            self.recoveries += 1

        async def reset_after_discontinuity(
            self,
            _identity: SessionIdentity,
            *,
            capture_start_sample: int,
        ) -> None:
            self.reset_samples.append(capture_start_sample)

    provider = LazyStartFailureProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        audio_ingress_max_frames=4,
    )
    identity = SessionIdentity("lazy-task-start-recovery", stream_epoch=1)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)

    await registry.on_audio_frame(session, AudioFrame(identity, 0, 0, 2, b"\x00\x00" * 2))
    await registry.on_audio_frame(session, AudioFrame(identity, 1, 2, 2, b"\x01\x00" * 2))
    await asyncio.wait_for(provider.failure_started.wait(), timeout=1)
    await registry.on_audio_frame(session, AudioFrame(identity, 2, 4, 2, b"\x02\x00" * 2))
    provider.release_failure.set()

    for _ in range(50):
        if context.ingress.provider_failed:
            break
        await asyncio.sleep(0)

    assert provider.audio_calls == [0, 1]
    assert context.ingress.queue.empty()
    assert context.ingress.provider_failed is True

    await registry.on_audio_frame(session, AudioFrame(identity, 3, 6, 2, b"\x03\x00" * 2))
    for _ in range(50):
        if provider.audio_calls == [0, 1, 3]:
            break
        await asyncio.sleep(0)

    assert provider.recoveries == 1
    assert provider.reset_samples == [6]
    assert provider.audio_calls == [0, 1, 3]
    assert context.ingress.provider_failed is False
    await context.runtime.close()
