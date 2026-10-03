"""Shared fakes, fixtures and helpers for the media-session test modules.

Split out of the former ``test_media_session.py`` (TODOLIST P2-08 / batch 5).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from types import SimpleNamespace
from typing import Any

import grpc
import pytest
import pytest_asyncio
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.state_machine import (
    ConversationState,
    InteractionPhase,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaSessionResources,
    MediaTextSpan,
    MediaVoiceCoreRegistry,
    MediaVoiceProvider,
)
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputState,
)
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import set_floor
from services.speaker.domain import SpeakerDecision, permissions_for_speaker


class FakeMediaProvider(MediaVoiceProvider):
    def __init__(self) -> None:
        self.audio_calls: list[int] = []
        self.cancelled: list[GenerationFence] = []
        self.pause_asr_calls: list[int] = []
        self.closed = False

    async def ingest_audio(
        self,
        _identity: SessionIdentity,
        frame: AudioFrame,
    ) -> Sequence[ASRResult]:
        self.audio_calls.append(frame.sequence)
        if frame.sequence != 0:
            return ()
        return (
            ASRResult(
                task_epoch=1,
                sentence_id="fake-sentence",
                revision=1,
                capture_start_sample=0,
                capture_end_sample=frame.frame_samples,
                text="你好",
                is_final=True,
                confidence=0.99,
                stream_epoch=frame.identity.stream_epoch,
            ),
        )

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
                text="你好。",
                first=True,
                final=True,
            )

        return chunks()

    def cancel_generation(self, fence: GenerationFence) -> bool:
        self.cancelled.append(fence)
        return True

    async def pause_asr_for_playback(self, identity: SessionIdentity) -> None:
        self.pause_asr_calls.append(identity.stream_epoch)

    async def close(self, _identity: SessionIdentity) -> None:
        self.closed = True


def _verified_owner_decision() -> SpeakerDecision:
    return SpeakerDecision(
        classification="owner",
        score=0.95,
        quality_score=0.95,
        reason_code="owner_match",
        model_version="speaker-test-v1",
        template_version=1,
        profile_id="owner-profile",
        permissions=permissions_for_speaker("owner"),
    )


def _bind_verified_owner_classifier(runtime: DuplexRuntime) -> None:
    """Wire the production speaker-authority seam to a verified owner.

    Media Voice resolves authority for the current utterance, so a test that
    expects an overlapping user turn to take the floor must prove the speaker
    is the owner instead of leaving authority unresolved.
    """

    async def classify_owner(_pcm: bytes, sample_rate: int) -> SpeakerDecision:
        assert sample_rate == 16_000
        return _verified_owner_decision()

    runtime.set_speaker_classifier(classify_owner, sample_rate=16_000)


class _CapturingMediaBridge(MediaBridgeGrpcServer):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, dict[str, object]]] = []

    async def emit_event(
        self,
        _session_id: str,
        event_type: str,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> bool:
        self.events.append((event_type, payload))
        return True


def open_bridge_connection(
    bridge: MediaBridgeGrpcServer,
    identity: SessionIdentity,
    *,
    traceparent: str = "",
) -> Any:
    """Open one transport connection exactly as the gRPC hello handler does."""

    return bridge._open_connection(identity, traceparent=traceparent)


async def _seed_pending_media_turn(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    *,
    text: str,
    endpoint_sample: int = 600,
    retire_sample: int = 640,
) -> Any:
    context = await registry.open_session(identity)
    assert await registry.accept_asr_result(
        identity.session_id,
        ASRResult(
            task_epoch=1,
            sentence_id=f"seed-{identity.session_id}",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=endpoint_sample,
            text=text,
            is_final=True,
            stream_epoch=identity.stream_epoch,
        ),
    )
    context.pending.turn_endpoint_sample = endpoint_sample
    context.pending.turn_retire_sample = retire_sample
    return context


class MultiFinalProvider(FakeMediaProvider):
    async def ingest_audio(
        self,
        _identity: SessionIdentity,
        frame: AudioFrame,
    ) -> Sequence[ASRResult]:
        self.audio_calls.append(frame.sequence)
        return (
            ASRResult(
                task_epoch=1,
                sentence_id=f"sentence-{frame.sequence}",
                revision=1,
                capture_start_sample=frame.capture_start_sample,
                capture_end_sample=frame.capture_end_sample,
                text=("第一句", "第二句")[frame.sequence],
                is_final=True,
                confidence=0.99,
                stream_epoch=frame.identity.stream_epoch,
            ),
        )


class TimedInterruptProvider(FakeMediaProvider):
    async def interrupted_timed_text_spans(
        self,
        _fence: GenerationFence,
    ) -> tuple[MediaTextSpan, ...]:
        return (MediaTextSpan("你好。", 0, 2),)


async def _requests(queue: asyncio.Queue[media_pb2.MediaToCore | None]):
    while True:
        message = await queue.get()
        if message is None:
            return
        yield message


_EVENT_BACKLOG: dict[int, list[Any]] = {}


async def _next_event(call, kind: str):
    backlog = _EVENT_BACKLOG.setdefault(id(call), [])
    for _ in range(64):
        for index, queued in enumerate(backlog):
            if queued.WhichOneof("event") == kind:
                return backlog.pop(index)
        event = await asyncio.wait_for(call.read(), timeout=1)
        if event is grpc.aio.EOF:
            raise AssertionError(f"bridge ended before {kind}")
        if event.WhichOneof("event") == kind:
            return event
        backlog.append(event)
    raise AssertionError(f"bridge did not emit {kind}")


def _queued_event(connection: Any, kind: str) -> Any:
    while not connection.outgoing.empty():
        event = connection.outgoing.get_nowait()
        if event is not None and event.WhichOneof("event") == kind:
            return event
    raise AssertionError(f"bridge queue did not contain {kind}")


def _owner_silence_identity(session_id: str) -> SessionIdentity:
    return SessionIdentity(
        session_id,
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
    )


class _LateOwnedDelegationProvider(FakeMediaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.output_kinds: list[int] = []
        self.release = asyncio.Event()
        self.ack_started = asyncio.Event()
        self.ack_completed = asyncio.Event()
        self.deep_started = asyncio.Event()

    async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
        await self.release.wait()
        return "南京今天多云，气温二十二度。"

    @staticmethod
    def accept_output_intent(intent: Any) -> Any:
        return intent

    def generate_output(
        self,
        _identity: SessionIdentity,
        intent: Any,
        _fence: GenerationFence,
        *,
        work_id: str,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        _ = work_id
        self.output_kinds.append(int(intent.kind))
        if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
            self.ack_started.set()
        elif intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT:
            self.deep_started.set()

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=b"\x02\x00\x03\x00",
                source_start_sample=source_start_sample,
                text=str(intent.tts_source),
                first=True,
                final=True,
            )
            if intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
                self.ack_completed.set()

        return chunks()


class _CapturingGenerationBridge(MediaBridgeGrpcServer):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[object] = []
        self.generation_starts: list[GenerationFence] = []

    async def emit_pcm(self, _session_id: str, _frame: object) -> bool:
        self.frames.append(_frame)
        return True

    async def emit_generation(self, *_args: object, **_kwargs: object) -> bool:
        action = _kwargs.get("action")
        fence = _args[1] if len(_args) > 1 else _kwargs.get("fence")
        if action == media_pb2.GENERATION_ACTION_START and isinstance(fence, GenerationFence):
            self.generation_starts.append(fence)
        return True


class _AckCapturingProvider(FakeMediaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self.started = asyncio.Event()
        self.completed = asyncio.Event()

    def generate_output(
        self,
        _identity: SessionIdentity,
        intent: Any,
        _fence: GenerationFence,
        *,
        work_id: str,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        _ = work_id
        self.texts.append(str(intent.tts_source))
        self.started.set()

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=b"\x02\x00\x03\x00",
                source_start_sample=source_start_sample,
                text=str(intent.tts_source),
                first=True,
                final=True,
            )
            self.completed.set()

        return chunks()


async def _finish_output_owner_playback(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    bridge: _CapturingGenerationBridge,
    session: Any,
) -> None:
    context = registry.session_state(identity.session_id)
    owner = context.output.output_owner
    assert owner is not None
    ack_frame = bridge.frames[-1]
    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=owner.fence.generation_id,
            received_sequence=ack_frame.sequence,
            rendered_sample_end=(ack_frame.source_start_sample + ack_frame.frame_samples),
            client_monotonic_ms=1,
            turn_id=owner.fence.turn_id,
            tool_epoch=owner.fence.tool_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )


async def _finish_device_wake_ack_if_any(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    provider: _LateOwnedDelegationProvider,
    bridge: _CapturingGenerationBridge,
    session: Any,
) -> None:
    if identity.client_type != "device":
        return
    try:
        await asyncio.wait_for(provider.ack_started.wait(), timeout=1)
    except TimeoutError:
        return
    await asyncio.wait_for(provider.ack_completed.wait(), timeout=1)
    context = registry.session_state(identity.session_id)
    for _ in range(40):
        if context.output.output_owner is not None or bridge.frames:
            break
        await asyncio.sleep(0)
    if context.output.output_owner is not None:
        await _finish_output_owner_playback(registry, identity, bridge, session)
    elif bridge.frames:
        ack_frame = bridge.frames[-1]
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=ack_frame.generation_id,
                received_sequence=ack_frame.sequence,
                rendered_sample_end=(ack_frame.source_start_sample + ack_frame.frame_samples),
                client_monotonic_ms=1,
                turn_id=ack_frame.turn_id,
                tool_epoch=ack_frame.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
    provider.ack_started.clear()
    provider.ack_completed.clear()


async def _ack_owned_filler_then_wait(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    provider: _LateOwnedDelegationProvider,
    bridge: _CapturingGenerationBridge,
) -> tuple[Any, GenerationFence]:
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    await _finish_device_wake_ack_if_any(registry, identity, provider, bridge, session)
    committed = await context.runtime.on_turn_committed("今天南京天气怎么样")
    await asyncio.wait_for(provider.ack_started.wait(), timeout=1)
    ack_owner = context.output.output_owner
    assert ack_owner is not None
    ack_fence = ack_owner.fence
    claim = context.output.delegation_output_claims[committed]
    assert ack_fence.turn_id == committed.turn_id
    assert ack_fence.generation_id == committed.generation_id
    assert claim.state is DelegationOutputState.OWNED
    await asyncio.wait_for(provider.ack_completed.wait(), timeout=1)
    ack_frame = bridge.frames[-1]
    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=ack_fence.generation_id,
            received_sequence=ack_frame.sequence,
            rendered_sample_end=(ack_frame.source_start_sample + ack_frame.frame_samples),
            client_monotonic_ms=1,
            turn_id=ack_fence.turn_id,
            tool_epoch=ack_fence.tool_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )
    assert claim.state is DelegationOutputState.OWNED
    assert context.runtime.orchestrator.state is ConversationState.TOOL_WAITING
    assert context.runtime.interaction_phase is InteractionPhase.TOOL_WAITING
    assert context.runtime.fence.matches(ack_fence)
    assert not provider.deep_started.is_set()
    return context, ack_fence


def _connect_vad_segment(
    identity: SessionIdentity,
    *,
    sample: int = 0,
    rms: float | None = None,
    segment_id: str = "connect-vad",
) -> SpeechSegment:
    return SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        provider_task_epoch=0,
        segment_id=segment_id,
        revision=1,
        kind=SegmentKind.VAD,
        capture_start_sample=sample,
        capture_end_sample=sample + 1,
        near_end_rms=rms,
    )


def _device_identity(session_id: str) -> SessionIdentity:
    return SessionIdentity(
        session_id,
        account_id="account",
        device_id="device",
        client_type="device",
        subject_id="owner",
        binding_id="binding",
        binding_version=1,
        runtime_profile_version=1,
    )


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    """Poll until predicate() is truthy or the deadline passes."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.005)


class _DelegationProbeProvider(FakeMediaProvider):
    """Records conversation replies, deep/ACK outputs and delegation starts."""

    def __init__(self) -> None:
        super().__init__()
        self.reply_calls = 0
        self.output_kinds: list[int] = []
        self.delegations: list[tuple[str, GenerationFence]] = []

    @staticmethod
    def accept_output_intent(intent: Any) -> Any:
        return intent

    def generate_reply(
        self,
        identity: SessionIdentity,
        user_text: str,
        fence: GenerationFence,
    ) -> AsyncIterator[MediaReplyChunk]:
        self.reply_calls += 1
        return super().generate_reply(identity, user_text, fence)

    def generate_output(
        self,
        _identity: SessionIdentity,
        intent: Any,
        _fence: GenerationFence,
        *,
        work_id: str,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        _ = work_id
        self.output_kinds.append(int(intent.kind))

        async def chunks() -> AsyncIterator[MediaReplyChunk]:
            yield MediaReplyChunk(
                pcm_s16le=b"\x02\x00\x03\x00",
                source_start_sample=source_start_sample,
                first=True,
                final=True,
            )

        return chunks()


class _FenceCheckingBridge(MediaBridgeGrpcServer):
    def __init__(self) -> None:
        super().__init__()
        self.accepted_pcm: list[object] = []
        self.rejected_pcm: list[object] = []
        self.frames = self.accepted_pcm
        self.start_reasons: list[str] = []
        self.generation_starts: list[GenerationFence] = []

    async def emit_generation(
        self, session_id: str, fence: object, *, action: int, reason: str = "", **_kwargs: object
    ) -> bool:
        if action == media_pb2.GENERATION_ACTION_START:
            session = self.bridge.get(session_id)
            if session is not None and isinstance(fence, GenerationFence):
                session.generation.advance(fence)
                session.reset_downlink_generation(fence)
                session.generation_active = True
                self.generation_starts.append(fence)
            self.start_reasons.append(reason)
        return True

    async def emit_pcm(self, session_id: str, frame: object) -> bool:
        session = self.bridge.get(session_id)
        if session is None or not session.accept_downlink(frame):  # type: ignore[arg-type]
            self.rejected_pcm.append(frame)
            return False
        self.accepted_pcm.append(frame)
        return True


async def _qa_commit_question_then_finish_ack_playback(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    bridge: _CapturingGenerationBridge,
    provider: _LateOwnedDelegationProvider,
    session: Any,
    question: str,
    *,
    start_sample: int = 0,
    end_sample: int = 600,
    retire_sample: int = 640,
) -> tuple[Any, GenerationFence]:
    """Commit one media turn through the real gate, then play its ACK out.

    Lands the session inside the gate's blind window: the live-lookup
    acknowledgement has finished playing, so ``_reply_in_flight`` is false,
    yet the delegated answer is still OWNED and undelivered.
    """

    context = await _seed_pending_media_turn(
        registry,
        identity,
        text=question,
        endpoint_sample=end_sample,
        retire_sample=retire_sample,
    )
    fence, reason = await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
        start_sample=start_sample,
        end_sample=end_sample,
        retire_sample=retire_sample,
    )
    assert fence is not None, reason
    await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
    await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
    ack_owner = context.output.output_owner
    assert ack_owner is not None
    ack_fence = ack_owner.fence
    ack_frame = bridge.frames[-1]
    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=ack_fence.generation_id,
            received_sequence=ack_frame.sequence,
            rendered_sample_end=(ack_frame.source_start_sample + ack_frame.frame_samples),
            client_monotonic_ms=1,
            turn_id=ack_fence.turn_id,
            tool_epoch=ack_fence.tool_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )
    return context, fence


async def _qa_commit_repeat_question(
    registry: MediaVoiceCoreRegistry,
    context: Any,
    identity: SessionIdentity,
    *,
    text: str,
    start_sample: int,
    end_sample: int,
    retire_sample: int,
) -> tuple[GenerationFence | None, str | None]:
    """Re-transcribe ``text`` over the contiguous range and commit it."""

    segment = SpeechSegment(
        session_id=identity.session_id,
        stream_epoch=identity.stream_epoch,
        provider_task_epoch=2,
        segment_id=f"qa-repeat-{start_sample}",
        revision=1,
        kind=SegmentKind.ASR_FINAL,
        capture_start_sample=start_sample,
        capture_end_sample=end_sample,
        text=text,
        final=True,
    )
    assert context.runtime.ingest_media_speech_segment(segment)
    await registry._apply_projection_segment(context, segment)
    context.pending.turn_start_sample = start_sample
    context.pending.turn_end_sample = end_sample
    context.pending.turn_endpoint_sample = end_sample
    context.pending.turn_retire_sample = retire_sample
    return await registry.commit_user_turn(
        identity.session_id,
        stream_epoch=identity.stream_epoch,
        start_sample=start_sample,
        end_sample=end_sample,
        retire_sample=retire_sample,
    )


async def _qa_open_empty_vad_tail(
    registry: MediaVoiceCoreRegistry, context: Any, session: Any, identity: SessionIdentity
) -> int:
    registry._clear_pending_turn_state(context)
    for name, start, final in (("start", 640, False), ("end", 1280, True)):
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=0,
                segment_id=f"empty-lookup-{name}",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=start,
                capture_end_sample=start + 1,
                final=final,
                voiced_end_sample=1200 if final else None,
            ),
        )
    task = context.turn_endpoint_task
    assert task is not None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert context.pending.turn_endpoint_sample == 1200
    assert not context.runtime.output_floor_allows_assistant
    return 1200


@pytest.mark.asyncio
async def _accept_media_asr_final(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    *,
    sentence_id: str,
    start_sample: int,
    end_sample: int,
    text: str,
    revision: int = 1,
) -> None:
    decision = await _accept_media_asr_decision(
        registry,
        identity,
        sentence_id=sentence_id,
        start_sample=start_sample,
        end_sample=end_sample,
        text=text,
        revision=revision,
    )
    assert decision.accepted is not None, decision.reason


async def _accept_media_asr_decision(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    *,
    sentence_id: str,
    start_sample: int,
    end_sample: int,
    text: str,
    revision: int = 1,
    is_final: bool = True,
) -> Any:
    """Feed one provider result through the production acceptance seam."""

    return await registry._accept_asr_result_decision(
        identity.session_id,
        ASRResult(
            task_epoch=3,
            sentence_id=sentence_id,
            revision=revision,
            capture_start_sample=start_sample,
            capture_end_sample=end_sample,
            text=text,
            is_final=is_final,
        ),
    )


# A device session must observe these five finals while the previous reply owns
# output.  Field evidence 2026-09-15 epoch 1955: the device reported no VAD edge
# after sample 113600, the replies played over samples ~163840-434240, and the
# real question arrived at 484480-508800, i.e. 8.4 s after the last of them.
_PLAYBACK_WINDOW_FINALS = (
    ("playback-2", 158_560, 163_040, "AAA", 1),
    ("playback-3", 185_280, 204_480, "BBBB", 2),
    ("playback-6", 258_880, 264_640, "CCCCCCCCCCC", 3),
    ("playback-9", 317_280, 332_640, "DDDD", 4),
    ("playback-10", 338_880, 350_080, "EEEEEEE", 5),
)


_ABANDONED_WINDOW_END = 350_080


_RETAINED_WINDOW_START = 484_480


class _PreparingDeviceProvider(FakeMediaProvider):
    """Records the text the production prepare seam hands to the provider."""

    def __init__(self, runtime: DuplexRuntime) -> None:
        super().__init__()
        self._runtime = runtime
        self.prepared: list[str] = []
        self.warmed: list[str] = []

    def warm_committed_turn(self, _identity: SessionIdentity, text: str) -> None:
        self.warmed.append(text)

    async def prepare_committed_turn(
        self,
        _identity: SessionIdentity,
        text: str,
    ) -> GenerationFence:
        self.prepared.append(text)
        return await self._runtime.on_turn_committed(text, input_modality="audio")


async def _open_device_overlap_session(
    session_id: str,
    *,
    during_playback: bool = True,
) -> SimpleNamespace:
    """A real device session whose previous reply overlapped the uplink.

    The identity is a ``client_type="device"`` one so the device-only early
    commit chain runs; the provider is fake, the network never is.  Close
    routing is the single semantic seam this scenario needs, so it is stubbed
    deterministically: it must not replace the production commit path.
    """

    runtime = DuplexRuntime.create(session_id=session_id)
    provider = _PreparingDeviceProvider(runtime)
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        session_factory=lambda _identity: MediaSessionResources(runtime, provider),
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = _device_identity(session_id)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    bind_owner_policy(context.runtime)
    _bind_verified_owner_classifier(context.runtime)
    # Drain the session-start profile rotation before any turn: otherwise the
    # first commit carries the epoch-drain barrier and the fake TTS pool cannot
    # finish its discard step inside the test.
    await context.runtime.settle_bootstrap_identity_epoch()

    close_semantic = SimpleNamespace(
        verdict_text=None,
        gate=asyncio.Event(),
        late_gate=asyncio.Event(),
        on_resolve=None,
        entered=asyncio.Event(),
        swallow=False,
        calls=0,
    )
    close_semantic.gate.set()
    # Later generations resolve normally unless a test parks them explicitly.
    close_semantic.late_gate.set()

    async def _close_verdict(text: str) -> bool:
        # Deterministic: the rule fast path still runs first, so only text it
        # misses reaches this seam.  A test can hold the verdict open, and can
        # make the step swallow cancellation like a production seam can.
        close_semantic.entered.set()
        # Later generations get their own gate so a test can finish one held
        # verdict while the next same-text one stays parked.
        gate = close_semantic.gate if close_semantic.calls == 0 else close_semantic.late_gate
        close_semantic.calls += 1
        while not gate.is_set():
            try:
                await gate.wait()
            except asyncio.CancelledError:
                if not close_semantic.swallow:
                    raise
        hook = close_semantic.on_resolve
        if hook is not None:
            hook()
        verdict_text = close_semantic.verdict_text
        return verdict_text is not None and verdict_text in text

    context.runtime.set_conversation_close_semantic_resolver(_close_verdict)
    if during_playback:
        # The previous reply still owns output while the uplink keeps streaming:
        # its barge-in voice start would rotate the epoch, so that start is left
        # to ``_start_retained_utterance`` after the reply is over.  Ownership is
        # installed directly so the test stays on the ASR/turn seam instead of
        # simulating a full generator lifecycle.
        set_floor(context.runtime, assistant_speaking=True)
    return SimpleNamespace(
        registry=registry,
        identity=identity,
        session=session,
        context=context,
        provider=provider,
        close_semantic=close_semantic,
    )


async def _feed_playback_window_finals(window: SimpleNamespace) -> None:
    for sentence_id, start_sample, end_sample, text, revision in _PLAYBACK_WINDOW_FINALS:
        await _accept_media_asr_final(
            window.registry,
            window.identity,
            sentence_id=sentence_id,
            start_sample=start_sample,
            end_sample=end_sample,
            text=text,
            revision=revision,
        )


async def _commit_pending_turn_from_device_endpoint(
    window: SimpleNamespace,
    *,
    voiced_end_sample: int,
) -> None:
    """Drive the device VAD end and wait for the production commit path."""

    await window.registry.on_speech_segment(
        window.session,
        SpeechSegment(
            session_id=window.identity.session_id,
            stream_epoch=window.identity.stream_epoch,
            provider_task_epoch=9,
            segment_id="device-vad-end",
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=voiced_end_sample,
            capture_end_sample=voiced_end_sample + 1,
            final=True,
            voiced_end_sample=voiced_end_sample,
        ),
    )
    await _wait_until(
        lambda: bool(window.provider.prepared) or window.context.standby_requested,
        timeout=2.0,
    )


def _user_turn_texts(context: Any) -> list[str]:
    return [
        turn.content
        for turn in context.runtime.orchestrator.context.turns
        if turn.role == "user" and turn.content
    ]


def _finish_previous_reply(context: Any) -> None:
    """The reply that owned output is over; later finals are no longer overlap."""

    set_floor(context.runtime, assistant_speaking=False)


def _start_retained_utterance(window: SimpleNamespace) -> None:
    """End the previous reply and open the new utterance with verified owner PCM."""

    _finish_previous_reply(window.context)
    window.context.runtime.on_user_voice_started()
    window.context.runtime.feed_speaker_pcm(b"\x00\x00" * 8_000)


@pytest_asyncio.fixture
async def device_media_session() -> AsyncIterator[Any]:
    """Open device media sessions and drain every task they start at teardown."""

    windows: list[SimpleNamespace] = []

    async def open_session(session_id: str, *, during_playback: bool = True) -> SimpleNamespace:
        window = await _open_device_overlap_session(
            session_id,
            during_playback=during_playback,
        )
        windows.append(window)
        return window

    try:
        yield open_session
    finally:
        for window in windows:
            context = window.context
            # Release a held verdict so a swallowing step can finish, then wait
            # for it: no task may outlive the test.
            window.close_semantic.gate.set()
            window.close_semantic.late_gate.set()
            semantic_task = context.pending.conversation_close_semantic_task
            if semantic_task is not None and not semantic_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(semantic_task), timeout=1.0)
                except (TimeoutError, asyncio.CancelledError):
                    semantic_task.cancel()
            handle = context.pending.turn_endpoint_timeout_handle
            if handle is not None:
                handle.cancel()
            task = context.turn_endpoint_task
            if task is not None and not task.done():
                task.cancel()
            await window.registry.finalize_session(window.identity.session_id)
        await asyncio.sleep(0)


async def _complete_previous_device_playback(
    window: SimpleNamespace,
    *,
    frame_samples: int = 3_200,
) -> None:
    """Drive the previous reply's playback to terminal through the real ledger.

    Production ordering inside: the previous turn commits and its reply owns
    output (the provisional for the follow-up is minted under the reply's
    fence, exactly as in the field), realtime echo of the reply lands while
    the reply still owns output, then the device reports the terminal ENDED
    progress event.  The runtime speaking seam is released exactly as the
    existing overlap tests release it after a real playback ends.
    """

    context = window.context
    fence = await context.runtime.on_turn_committed("明天上海天气怎么样")
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="echo-1",
        start_sample=160_000,
        end_sample=190_000,
        text="上海明天晴转多云",
    )
    assert context.pending.pending_turn_playback_overlap is True
    context.output.playback.start(fence)
    assert context.output.playback.register_audio(fence, 0, 0, frame_samples)
    context.output.provider_complete = True
    await window.registry.on_playback_progress(
        window.session,
        PlaybackProgress(
            identity=window.identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=frame_samples,
            client_monotonic_ms=5,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            session_epoch=fence.session_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )
    _finish_previous_reply(context)


async def _device_vad(
    window: SimpleNamespace,
    segment_id: str,
    sample: int,
    *,
    final: bool,
) -> None:
    await window.registry.on_speech_segment(
        window.session,
        SpeechSegment(
            session_id=window.identity.session_id,
            stream_epoch=window.identity.stream_epoch,
            provider_task_epoch=0,
            segment_id=segment_id,
            revision=1,
            kind=SegmentKind.VAD,
            capture_start_sample=sample,
            capture_end_sample=sample + 1,
            final=final,
            voiced_end_sample=sample if final else None,
            near_end_rms=0.05,
        ),
    )


async def _open_retained_window(
    open_session: Any,
    session_id: str,
    *,
    retained_text: str = "下午一起出发吗",
    retained_start: int = _RETAINED_WINDOW_START,
    retained_end: int = 508_800,
) -> SimpleNamespace:
    """A playback-overlap candidate window, then one plain retained utterance."""

    window = await open_session(session_id)
    await _feed_playback_window_finals(window)
    _start_retained_utterance(window)
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="plain-1",
        start_sample=retained_start,
        end_sample=retained_end,
        text=retained_text,
    )
    return window


async def _start_speaking_reply(
    registry: MediaVoiceCoreRegistry,
    session: object,
    identity: SessionIdentity,
) -> tuple[object, GenerationFence]:
    """Drive one VAD+ASR turn and a provider reply, returning its context."""

    context = await registry.open_session(identity)

    async def vad(segment_id: str, sample: int, *, final: bool) -> None:
        await registry.on_speech_segment(
            session,  # type: ignore[arg-type]
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=1,
                provider_task_epoch=0,
                segment_id=segment_id,
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=sample,
                capture_end_sample=sample + 1,
                final=final,
                voiced_end_sample=sample if final else None,
            ),
        )

    await vad("start", 0, final=False)
    final = ASRResult(1, "turn-final", 1, 0, 320, "你好", True, stream_epoch=1)
    assert await registry.accept_asr_result(identity.session_id, final)
    registry._observe_final_asr_result(context, final)
    await vad("end", 320, final=True)
    await asyncio.sleep(0.03)
    fence = context.runtime.fence
    if context.output.reply_task is not None:
        assert await asyncio.wait_for(context.output.reply_task, timeout=1)
    assert context.runtime.orchestrator.state is ConversationState.SPEAKING
    # Keep the bridge-side authoritative gate in sync with the runtime fence
    # so a subsequent client stop/KWS cancel derives the expected fence.
    session.generation.advance(fence)  # type: ignore[attr-defined]
    session.reset_downlink_generation(fence)  # type: ignore[attr-defined]
    session.generation_active = True  # type: ignore[attr-defined]
    return context, fence
