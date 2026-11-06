"""Media session: output lifecycle, owner rechecks and playback overlap."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.interruption_guard import PlaybackInputDecision
from services.agent.src.orchestration.state_machine import (
    ConversationState,
    InteractionPhase,
)
from services.agent.src.prompts import (
    BRIDGE_PHRASES,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import (
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import (
    MediaReplyChunk,
    MediaVoiceCoreRegistry,
    _OutputWork,
)
from services.agent.src.voice_core.media_session_types import (
    DelegationOutputState,
)
from services.agent.src.voice_core.speech_timeline import (
    ASRResult,
    SegmentKind,
    SpeechSegment,
)
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    _accept_media_asr_decision,
    _accept_media_asr_final,
    _ack_owned_filler_then_wait,
    _AckCapturingProvider,
    _CapturingGenerationBridge,
    _commit_pending_turn_from_device_endpoint,
    _device_identity,
    _feed_playback_window_finals,
    _finish_output_owner_playback,
    _LateOwnedDelegationProvider,
    _open_retained_window,
    _start_retained_utterance,
    _wait_until,
)
from services.agent.tests.unit.runtime_state_helpers import set_floor


async def _speaking_session_with_effects(
    name: str,
    *,
    speak_first: bool,
) -> tuple[Any, Any, Any, GenerationFence, list[tuple[int, GenerationFence, str, dict[str, object]]], asyncio.Task[bool], asyncio.Event]:
    """A device session whose first output of ``fence`` puts one frame on the wire (or none)."""

    first_frame_sent = asyncio.Event()
    release = asyncio.Event()

    class AckProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                if speak_first:
                    yield MediaReplyChunk(
                        pcm_s16le=b"\x02\x00\x03\x00",
                        source_start_sample=0,
                        text="我查一下",
                        first=True,
                        final=False,
                    )
                await release.wait()

            return chunks()

    effects: list[tuple[int, GenerationFence, str, dict[str, object]]] = []

    class CapturingBridge(MediaBridgeGrpcServer):
        async def emit_pcm(self, _session_id: str, _frame: object) -> bool:
            first_frame_sent.set()
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
            effects.append((effect_kind, fence, source_event_id, payload))
            return True

    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(bridge=bridge, provider_factory=lambda _identity: AckProvider())
    registry.install()
    identity = _device_identity(name)
    bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("查一下")
    context.output.playback.start(fence)
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "查一下", fence))
    if speak_first:
        await asyncio.wait_for(first_frame_sent.wait(), timeout=1)
    else:
        await asyncio.sleep(0.05)
    return registry, identity, context, fence, effects, reply, release


@pytest.mark.asyncio
async def test_a_failed_follow_on_output_still_closes_a_fence_that_already_spoke() -> None:
    """The dispatch that fails sent nothing (its first frame was rejected as a sequence gap), but the
    acknowledgement before it did: without a terminal fence the device stays in SPEAKING."""

    registry, identity, context, fence, effects, reply, release = await _speaking_session_with_effects(
        "follow-on-failure-after-ack", speak_first=True
    )
    try:
        assert context.output.audio_sent_for(fence) is True
        await registry._abort_unheard_stream(
            context, fence, reason="transport_rejected", emitted_audio=False
        )
        assert context.runtime.orchestrator.state is ConversationState.LISTENING
        assert any(
            effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
            and cancelled.generation_id == fence.generation_id + 1
            for effect_kind, cancelled, _source, _payload in effects
        )
    finally:
        release.set()
        await asyncio.gather(reply, return_exceptions=True)
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_a_failed_output_on_a_fence_that_never_spoke_does_not_cancel_it() -> None:
    """The control: nothing reached the device, so there is no audible generation to close."""

    registry, identity, context, fence, effects, reply, release = await _speaking_session_with_effects(
        "follow-on-failure-silent", speak_first=False
    )
    try:
        assert context.output.audio_sent_for(fence) is False
        await registry._abort_unheard_stream(
            context, fence, reason="transport_rejected", emitted_audio=False
        )
        assert not any(
            effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
            for effect_kind, _cancelled, _source, _payload in effects
        )
    finally:
        release.set()
        await asyncio.gather(reply, return_exceptions=True)
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_owner_is_rechecked_after_speaking_transition_before_output() -> None:
    speaking_started = asyncio.Event()
    release_speaking = asyncio.Event()

    class CapturingBridge(MediaBridgeGrpcServer):
        def __init__(self) -> None:
            super().__init__()
            self.event_types: list[str] = []
            self.frames: list[object] = []

        async def emit_event(
            self,
            _session_id: str,
            event_type: str,
            _payload: dict[str, object],
            **_kwargs: object,
        ) -> bool:
            self.event_types.append(event_type)
            return True

        async def emit_pcm(self, _session_id: str, frame: object) -> bool:
            self.frames.append(frame)
            return True

    provider = FakeMediaProvider()
    bridge = CapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("owner-recheck-after-await")
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("帮我查一下")
    context.output.playback.start(fence)
    original = context.runtime.on_assistant_speaking

    async def blocked_speaking(text: str, **kwargs: object) -> bool:
        speaking_started.set()
        await release_speaking.wait()
        return await original(text, **kwargs)

    context.runtime.on_assistant_speaking = blocked_speaking  # type: ignore[method-assign]
    reply = asyncio.create_task(registry.generate_reply(identity.session_id, "帮我查一下", fence))
    await asyncio.wait_for(speaking_started.wait(), timeout=1)

    coordinator = context.runtime.orchestrator.delegation
    now_ms = int(time.time() * 1_000)
    acknowledgement = coordinator.bridge_acknowledgement(
        BRIDGE_PHRASES[0],
        fence=fence,
        context_version=coordinator.current_context_version(identity.session_id),
        expires_at_ms=now_ms + 2_000,
        now_ms=now_ms,
    )
    assert (
        coordinator.admit_output_intent(
            acknowledgement,
            current_fence=fence,
            current_context_version=coordinator.current_context_version(identity.session_id),
            floor_allows_output=True,
            now_ms=now_ms + 1,
        )
        == BRIDGE_PHRASES[0]
    )
    release_speaking.set()

    assert not await asyncio.wait_for(reply, timeout=1)
    await asyncio.sleep(0)
    assert "transcript_delta" not in bridge.event_types
    assert "assistant_expression" not in bridge.event_types
    assert bridge.frames == []
    assert provider.cancelled == [fence]
    assert context.output.output_owner is None
    assert context.runtime.orchestrator.state is ConversationState.THINKING
    assert context.runtime._voice_floor.pending_assistant_text == ""
    assert context.runtime.assistant_speaking is False
    assert context.runtime._assistant_expression_fence is None
    coordinator.complete_output_intent(
        acknowledgement,
        current_fence=fence,
        current_context_version=coordinator.current_context_version(identity.session_id),
        floor_allows_output=True,
        now_ms=now_ms + 2,
    )
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_progressing_output_generation_is_not_killed_by_wall_clock_timeout() -> None:
    """Long TTS is paced to realtime; the deadline is a stall watchdog."""

    class SlowProgressProvider(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                for index in range(4):
                    if index:
                        await asyncio.sleep(0.04)
                    yield MediaReplyChunk(
                        pcm_s16le=b"\x02\x00\x03\x00",
                        source_start_sample=index * 2,
                        text="今" if index == 0 else "",
                        first=index == 0,
                        final=index == 3,
                    )

            return chunks()

    bridge = MediaBridgeGrpcServer()
    provider = SlowProgressProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        output_generation_timeout_s=0.08,
    )
    registry.install()
    identity = SessionIdentity("progressing-output-generation")
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("今天天气怎么样")
    context.output.playback.start(fence)

    result = await asyncio.wait_for(
        registry.generate_reply(identity.session_id, "今天天气怎么样", fence),
        timeout=1,
    )

    assert result is True
    assert context.output.output_owner is None
    assert provider.cancelled == []
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_provider_timeout_error_is_not_reclassified_as_generation_deadline() -> None:
    class ProviderTimeout(FakeMediaProvider):
        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                raise TimeoutError("provider timeout")
                yield MediaReplyChunk(b"\x00\x00", 0)  # pragma: no cover

            return chunks()

    bridge = MediaBridgeGrpcServer()
    provider = ProviderTimeout()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        output_generation_timeout_s=1.0,
    )
    registry.install()
    identity = SessionIdentity("provider-timeout-error")
    context = await registry.open_session(identity)
    fence = await context.runtime.on_turn_committed("你好")
    context.output.playback.start(fence)

    with pytest.raises(TimeoutError, match="provider timeout"):
        await registry.generate_reply(identity.session_id, "你好", fence)

    assert context.output.output_owner is None
    assert context.output.playback.current_fence is fence
    assert context.runtime.orchestrator.state is ConversationState.THINKING
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_unheard_wake_pcm_restores_half_duplex_listen() -> None:
    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()

    def runtime_factory(session_id: str) -> DuplexRuntime:
        runtime = DuplexRuntime.create(session_id=session_id, barge_in_enabled=False)

        def drop_pcm(_cancellation: object, _pcm: bytes) -> bytes | None:
            return None

        runtime.gate_tts_audio = drop_pcm  # type: ignore[method-assign]
        return runtime

    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=runtime_factory,
    )
    registry.install()
    identity = _device_identity("device-unheard-wake")
    bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await _wait_until(
            lambda: (
                context.output.output_owner is None
                and context.runtime.assistant_speaking is False
                and context.runtime.orchestrator.state is ConversationState.LISTENING
                and bool(provider.texts)
            )
        )
        assert context.runtime.on_user_voice_started() is PlaybackInputDecision.ACCEPT
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_late_clock_fact_recovery_does_not_preempt_in_flight_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Late overlap-rejected clock-fact must not cancel an in-flight reply.

    Regression for stage-7 epoch 1363: weekday early-committed turn3 and
    started Doubao TTS, then a longer-range offline final was rejected as
    cross_sentence_overlap, recovery early-committed turn4, and cancel_reply
    killed turn3 (reply_task_exception). While a reply owns the session,
    recovery may still ingest timeline text but must not re-arm/commit.
    """

    provider = _AckCapturingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = _device_identity("device-late-clock-fact-inflight")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(provider.completed.wait(), timeout=1)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        provider.started.clear()
        provider.completed.clear()
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=1,
                segment_id="clock-start",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=0,
                capture_end_sample=1,
            ),
        )
        from services.agent.src.voice_core.asr_stream_supervisor import ASRDecisionReason

        first = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-final",
            revision=1,
            capture_start_sample=0,
            capture_end_sample=178_240,
            text="今天星期几",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, first)

        async def _reply_owns_session() -> bool:
            return bool(
                context.output.output_owner is not None
                and any(
                    turn.role == "user" and turn.content == "今天星期几"
                    for turn in context.runtime.orchestrator.context.turns
                )
            )

        for _ in range(40):
            if await _reply_owns_session():
                break
            await asyncio.sleep(0.05)
        assert await _reply_owns_session()
        assert MediaVoiceCoreRegistry._reply_in_flight(context)
        in_flight_fence = context.output.output_owner.fence
        in_flight_task = context.output.reply_task
        generation_count_before = len(bridge.generation_starts)
        user_turns_before = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns_before == ["今天星期几"]
        committed = context.asr.last_committed_sample
        assert committed >= 178_240

        # Blocking interval after the committed watermark (playback echo shape).
        # The reply's own line is what the uplink hears: announce it first.
        context.output.assistant_text = "你好我是茉莉今天想聊点什么呀"
        echo = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="playback-echo",
            revision=1,
            capture_start_sample=committed,
            capture_end_sample=committed + 62_080,
            text="你好我是茉莉今天想聊点什么呀",
            is_final=True,
            confidence=0.9,
        )
        assert await registry.accept_asr_result(identity.session_id, echo)

        # Longer overlapping rescue final cannot supersede a real provider
        # interval → cross_sentence_overlap → recovery must skip re-commit.
        late = ASRResult(
            stream_epoch=identity.stream_epoch,
            task_epoch=1,
            sentence_id="clock-late-offline",
            revision=1,
            capture_start_sample=committed,
            capture_end_sample=committed + 120_000,
            text="今天是星期几",
            is_final=True,
            confidence=0.95,
            rescue_synthesized=True,
        )
        with caplog.at_level(logging.WARNING):
            decision = await registry._accept_asr_result_decision(
                identity.session_id,
                late,
            )
        assert decision.accepted is None
        assert decision.reason is ASRDecisionReason.CROSS_SENTENCE_OVERLAP
        assert context.pending.turn_endpoint_sample is None
        assert context.output.output_owner is not None
        assert context.output.output_owner.fence == in_flight_fence
        if in_flight_task is not None:
            assert context.output.reply_task is in_flight_task
            assert not in_flight_task.cancelled()
        assert len(bridge.generation_starts) == generation_count_before
        user_turns_after = [
            turn.content
            for turn in context.runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        assert user_turns_after == ["今天星期几"]
        assert any(
            "clock-fact recovery commit skipped: reply in flight" in record.message
            for record in caplog.records
        )
        await asyncio.sleep(0.05)
        assert not any(
            "reply_task_exception" in record.message or "reply_task_cancelled" in record.message
            for record in caplog.records
        )
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_half_duplex_owned_result_respects_a_closed_output_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A late result must not clear fresh speech and reopen the floor."""

    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=False,
        ),
    )
    registry.install()
    identity = SessionIdentity("half-duplex-closed-floor")
    context = None
    try:
        context, fence = await _ack_owned_filler_then_wait(
            registry,
            identity,
            provider,
            bridge,
        )
        claim = context.output.delegation_output_claims[fence]
        set_floor(context.runtime, fresh_user_speech=True)
        context.runtime.set_interaction_phase(
            InteractionPhase.USER_SPEAKING,
            cause="test_fresh_user_speech",
        )

        provider.release.set()
        with caplog.at_level(logging.INFO):
            await _wait_until(lambda: claim.state is DelegationOutputState.COMPLETED, timeout=1)
        await asyncio.sleep(0)

        assert not provider.deep_started.is_set()
        assert context.runtime._voice_floor.fresh_user_speech is True
        assert any(
            work.intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT
            for work in context.output.output_work.values()
        )
        assert context.output.output_retry_task is None
        assert any(
            "media output deferred" in record.getMessage()
            for record in caplog.records
        )
    finally:
        provider.release.set()
        if context is not None:
            await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_inactive_output_intent_drops_deep_result_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """epoch 1912: 已完成的深查答案不得因输出意图非活动而零错误静默丢弃。

    The finished deep answer was released with ``reason=output_intent_inactive``
    and only an INFO line, so losing a completed answer left no trace.  The drop
    must now surface an explicit warning on the delegation seam.
    """

    question = "今天南京天气怎么样"
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("deep-result-dropped-observable")
    try:
        context = await registry.open_session(identity)
        delegation = context.runtime.orchestrator.delegation
        # Force the exact seam that dropped the epoch-1912 answer: the admitted
        # output intent is judged inactive even though the deep result finished.
        monkeypatch.setattr(
            type(delegation),
            "output_intent_is_active",
            lambda *_args, **_kwargs: False,
        )
        provider.release.set()
        with caplog.at_level(logging.WARNING):
            await context.runtime.on_turn_committed(question)
            await _wait_until(
                lambda: any(
                    claim.state is DelegationOutputState.RELEASED
                    for claim in context.output.delegation_output_claims.values()
                ),
                timeout=2.0,
            )
        assert any(
            "deep result dropped" in record.getMessage() for record in caplog.records
        )
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_blocked_output_work_is_bounded_with_its_admission_queue() -> None:
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(bridge=bridge, provider_factory=lambda _: FakeMediaProvider())
    identity = SessionIdentity("bounded-blocked-output")
    bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    try:
        fence = await context.runtime.on_turn_committed("你好")
        context.runtime.on_user_voice_started()
        coordinator = context.runtime.orchestrator.delegation
        now = int(time.time() * 1_000)
        for index in range(12):
            intent = coordinator.bridge_acknowledgement(
                BRIDGE_PHRASES[0], fence=fence,
                context_version=coordinator.current_context_version(identity.session_id),
                expires_at_ms=now + 20_000, now_ms=now,
            )
            intent.intent_id = f"bounded-{index:02d}"
            coordinator.admit_output_intent(
                intent, current_fence=fence,
                current_context_version=coordinator.current_context_version(identity.session_id),
                floor_allows_output=False, now_ms=now,
            )
            assert await registry._enqueue_output_work(context, _OutputWork(intent, fence))
            assert len(context.output.output_work) <= 4
            assert context.output.output_retry_task is None
        assert not bridge.frames
        await context.runtime.accept_media_generation(fence.bump_turn(), cause="new_turn")
        registry._schedule_output_retry(context)
        assert not context.output.output_work
        assert context.output.output_retry_task is None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_media_playback_overlap_split_is_blocked_by_forced_text(
    device_media_session: Any,
) -> None:
    """An authoritative forced text keeps the candidate; releasing it splits."""

    window = await device_media_session("split-forced-text")
    await _feed_playback_window_finals(window)
    _start_retained_utterance(window)
    context = window.context
    assert context.pending.pending_turn_playback_overlap is True

    context.pending.live_query_forced_text = "明天南京天气"
    forced = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="far-1",
        start_sample=400_000,
        end_sample=410_000,
        text="下午一起出发吗",
    )
    assert forced.accepted is not None
    assert context.pending.turn_start_sample == 158_560

    context.pending.live_query_forced_text = None
    split = await _accept_media_asr_decision(
        window.registry,
        window.identity,
        sentence_id="far-2",
        start_sample=470_000,
        end_sample=480_000,
        text="下午一起出发吗",
        revision=2,
    )
    assert split.accepted is not None
    assert context.pending.turn_start_sample == 470_000


@pytest.mark.asyncio
async def test_device_retained_window_drops_playback_overlap_after_the_split(
    device_media_session: Any,
) -> None:
    """The retained window is no longer overlap: its own long pause still merges."""

    window = await _open_retained_window(
        device_media_session,
        "device-retained-flag",
        retained_end=490_000,
    )
    context = window.context
    assert context.pending.pending_turn_playback_overlap is False
    assert context.pending.pending_turn_onset_floor == 484_480
    await _accept_media_asr_final(
        window.registry,
        window.identity,
        sentence_id="clause-2",
        start_sample=532_000,
        end_sample=540_000,
        text="还有明天呢",
        revision=2,
    )
    assert context.pending.turn_start_sample == 484_480

    await _commit_pending_turn_from_device_endpoint(window, voiced_end_sample=540_000)
    text = window.provider.prepared[-1]
    assert "下午一起出发吗" in text and "还有明天呢" in text
    assert "AAA" not in text
