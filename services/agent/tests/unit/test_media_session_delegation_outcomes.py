"""Media session: QA outcomes, terminal output and delegation cancellation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.state_machine import (
    ConversationState,
)
from services.agent.src.prompts import (
    LIVE_LOOKUP_FILLER,
)
from services.agent.src.voice_core import media_session_projection
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
    _CapturingGenerationBridge,
    _DelegationProbeProvider,
    _device_identity,
    _finish_output_owner_playback,
    _LateOwnedDelegationProvider,
    _qa_commit_question_then_finish_ack_playback,
    _qa_commit_repeat_question,
    _qa_open_empty_vad_tail,
    _seed_pending_media_turn,
    _wait_until,
)
from services.common.realtime_information import WEATHER_LOOKUP_UNAVAILABLE_REPLY


@pytest.fixture(autouse=True)
def short_lookup_filler_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """These fakes model a slow lookup; keep the filler decision near-immediate."""
    monkeypatch.setattr(media_session_projection, "_LIVE_LOOKUP_FILLER_GRACE_S", 0.02)


@pytest.mark.asyncio
@pytest.mark.parametrize("result_before_tail", [True, False])
async def test_qa_weather_result_survives_empty_vad_tail(result_before_tail: bool) -> None:
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=60,
        turn_endpoint_absolute_timeout_s=60,
    )
    registry.install()
    identity = SessionIdentity(f"qa-weather-empty-vad-{result_before_tail}")
    session = bridge.bridge.open(identity)
    try:
        context, fence = await _qa_commit_question_then_finish_ack_playback(
            registry, identity, bridge, provider, session, "今天南京天气怎么样"
        )
        claim = context.output.delegation_output_claims[fence]
        endpoint = await _qa_open_empty_vad_tail(registry, context, session, identity)
        assert context.runtime.fence.matches(fence)
        assert await registry.generate_reply(identity.session_id, "今天南京天气怎么样", fence)
        assert claim.normal_reply_observed
        speaker_class = context.runtime._speaker_class
        frame_count = len(bridge.frames)
        if result_before_tail:
            provider.release.set()
            await _wait_until(lambda: claim.state is not DelegationOutputState.OWNED)
            assert claim.state is DelegationOutputState.COMPLETED
            assert any(
                work.intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT
                for work in context.output.output_work.values()
            )
            assert not provider.deep_started.is_set()
            assert len(bridge.frames) == frame_count
            assert context.output.output_retry_task is None
        await registry._expire_endpoint_tail(
            identity.session_id, identity.stream_epoch, endpoint
        )
        assert context.runtime.output_floor_allows_assistant
        assert context.runtime._speaker_class == speaker_class
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        await _wait_until(lambda: len(bridge.frames) > frame_count)
        deep_fence = context.output.output_owner.fence
        await _finish_output_owner_playback(registry, identity, bridge, session)
        delivery = context.output.reply_delivery.get(deep_fence)
        assert delivery.actual_heard and delivery.playback_ended
        await _wait_until(lambda: context.output.output_dispatch_task is None)
        assert not context.output.output_work
        assert context.output.output_retry_task is None
        assert provider.output_kinds.count(media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT) == 1
        assert provider.output_kinds.count(
            media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT
        ) == 1
        assert context.pending.turn_start_sample is None
        assert context.runtime.fence.turn_id == fence.turn_id
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("text_evidence", [False, True], ids=["room-noise", "owner-partial"])
async def test_qa_evidence_less_vad_cannot_hold_weather_result_past_cap(
    monkeypatch: pytest.MonkeyPatch,
    text_evidence: bool,
) -> None:
    """Room-noise VAD bursts must not starve a ready tool answer.

    Run 2026-09-24 session b18fede9: the weather answer was ready 2 s after
    the filler, but three noise VAD bursts (FunASR and rescue both empty)
    kept one empty pending turn open for ~8 s; each vad.start reset the
    2.5 s tail.  The cap retires such an evidence-less turn and resumes the
    answer, while any text evidence leaves the user's turn in charge.
    """

    from services.agent.src.voice_core import media_session_turns

    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_BASE_S", 0.3)
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_VAD_EXTENSION_S", 0.2)
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_MAX_S", 0.6)
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        # Only the cap may release the floor inside this test's window.
        turn_endpoint_grace_s=60,
        turn_endpoint_absolute_timeout_s=60,
    )
    registry.install()
    identity = SessionIdentity(f"qa-evidence-less-hold-{text_evidence}")
    session = bridge.bridge.open(identity)

    async def vad(name: str, sample: int, *, final: bool) -> None:
        await registry.on_speech_segment(
            session,
            SpeechSegment(
                session_id=identity.session_id,
                stream_epoch=identity.stream_epoch,
                provider_task_epoch=0,
                segment_id=f"noise-{name}",
                revision=1,
                kind=SegmentKind.VAD,
                capture_start_sample=sample,
                capture_end_sample=sample + 1,
                final=final,
                voiced_end_sample=sample if final else None,
            ),
        )

    try:
        context, fence = await _qa_commit_question_then_finish_ack_playback(
            registry, identity, bridge, provider, session, "今天南京天气怎么样"
        )
        claim = context.output.delegation_output_claims[fence]
        registry._clear_pending_turn_state(context)
        await vad("start-1", 640, final=False)
        assert not context.runtime.output_floor_allows_assistant
        if text_evidence:
            context.pending.pending_partial = ASRResult(
                stream_epoch=identity.stream_epoch,
                task_epoch=1,
                sentence_id="owner-partial",
                revision=1,
                capture_start_sample=640,
                capture_end_sample=4_000,  # reaches 210 ms into the edge: the owner's own words
                text="我还想问",
                is_final=False,
            )
        assert await registry.generate_reply(identity.session_id, "今天南京天气怎么样", fence)
        frame_count = len(bridge.frames)
        provider.release.set()
        await _wait_until(lambda: claim.state is not DelegationOutputState.OWNED)
        assert context.pending.evidence_less_hold_since is not None
        # Noise keeps re-opening the same empty turn faster than any tail.
        for index, sample in enumerate((1_280, 1_920, 2_560, 3_200, 3_840), start=1):
            await asyncio.sleep(0.1)
            await vad(f"end-{index}", sample, final=True)
            await asyncio.sleep(0.05)
            await vad(f"start-{index + 1}", sample + 160, final=False)

        if text_evidence:
            await asyncio.sleep(0.5)
            assert not context.runtime.output_floor_allows_assistant
            assert context.pending.turn_start_sample is not None
            assert not provider.deep_started.is_set()
            assert len(bridge.frames) == frame_count
            assert context.pending.evidence_less_hold_since is None
        else:
            await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
            await _wait_until(lambda: len(bridge.frames) > frame_count)
            assert context.pending.evidence_less_hold_since is None
            assert provider.output_kinds.count(media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT) == 1
            assert context.runtime.fence.turn_id == fence.turn_id
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalidator",
    [
        "new_vad", "stop", "new_turn", "identity", "close", "standby",
        "expiry", "context", "partial", "stream", "enrollment",
    ],
)
async def test_qa_empty_tail_does_not_revive_invalid_weather_output(invalidator: str) -> None:
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge, provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=60, turn_endpoint_absolute_timeout_s=60,
    )
    registry.install()
    identity = SessionIdentity(f"qa-weather-no-revive-{invalidator}")
    session = bridge.bridge.open(identity)
    try:
        context, fence = await _qa_commit_question_then_finish_ack_playback(
            registry, identity, bridge, provider, session, "今天南京天气怎么样"
        )
        endpoint = await _qa_open_empty_vad_tail(registry, context, session, identity)
        provider.release.set()
        claim = context.output.delegation_output_claims[fence]
        await _wait_until(lambda: claim.state is DelegationOutputState.COMPLETED)
        coordinator = context.runtime.orchestrator.delegation
        if invalidator == "new_vad":
            await registry.on_speech_segment(
                session,
                SpeechSegment(
                    session_id=identity.session_id, stream_epoch=identity.stream_epoch,
                    provider_task_epoch=0, segment_id="newer-vad", revision=1,
                    kind=SegmentKind.VAD, capture_start_sample=1280, capture_end_sample=1281,
                ),
            )
            assert context.pending.turn_endpoint_sample is None
        elif invalidator == "stop":
            await context.runtime.accept_media_generation(fence.bump_generation(), cause="stop")
        elif invalidator == "new_turn":
            await context.runtime.on_turn_committed("给我讲个故事")
        elif invalidator == "identity":
            context.runtime.orchestrator.bump_session_epoch(fence.session_epoch + 1)
        elif invalidator == "close":
            await registry.finalize_session(identity.session_id)
        elif invalidator == "standby":
            context.standby_requested = True
        elif invalidator == "stream":
            context.stream_epoch += 1
        elif invalidator == "enrollment":
            context.runtime.begin_formal_speaker_enrollment()
        elif invalidator == "expiry":
            for intent in coordinator._shadow_output_by_session[identity.session_id].values():
                intent.expires_at_ms = int(time.time() * 1_000) - 1
        elif invalidator == "context":
            coordinator.activate_context_version(
                identity.session_id, coordinator.current_context_version(identity.session_id) + 1
            )
        elif invalidator == "partial":
            context.pending.pending_partial = ASRResult(
                task_epoch=1, sentence_id="real-speech", revision=1,
                capture_start_sample=640, capture_end_sample=4_000, text="等等",
                is_final=False, stream_epoch=identity.stream_epoch,
            )
        await registry._expire_endpoint_tail(identity.session_id, identity.stream_epoch, endpoint)
        assert not await registry._start_selected_output(context)
        assert not provider.deep_started.is_set()
        assert len(bridge.frames) == 1
        assert context.output.output_retry_task is None
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_lookup", [True, False])
async def test_qa_conversation_or_failed_lookup_queues_during_empty_vad(
    failed_lookup: bool,
) -> None:
    class FailingProvider(_LateOwnedDelegationProvider):
        async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
            await self.release.wait()
            raise RuntimeError("lookup failed")

    provider = FailingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge, provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=60, turn_endpoint_absolute_timeout_s=60,
    )
    registry.install()
    identity = SessionIdentity(f"qa-empty-vad-conversation-{failed_lookup}")
    session = bridge.bridge.open(identity)
    try:
        if failed_lookup:
            context, fence = await _qa_commit_question_then_finish_ack_playback(
                registry, identity, bridge, provider, session, "今天南京天气怎么样"
            )
        else:
            context = await registry.open_session(identity)
            fence = await context.runtime.on_turn_committed("给我讲个故事")
            context.output.playback.start(fence)
        endpoint = await _qa_open_empty_vad_tail(registry, context, session, identity)
        frame_count = len(bridge.frames)
        assert await registry.generate_reply(identity.session_id, "给我讲个故事", fence)
        if failed_lookup:
            provider.release.set()
        await _wait_until(lambda: any(
            work.intent.kind == media_pb2.OUTPUT_INTENT_KIND_CONVERSATION_REPLY
            for work in context.output.output_work.values()
        ))
        assert len(bridge.frames) == frame_count
        assert context.output.output_retry_task is None
        await registry._expire_endpoint_tail(identity.session_id, identity.stream_epoch, endpoint)
        await _wait_until(lambda: len(bridge.frames) > frame_count)
        assert len(bridge.frames) == frame_count + 1
        await _finish_output_owner_playback(registry, identity, bridge, session)
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("preempt_owner", [False, True])
async def test_queued_ack_and_deep_result_survive_internal_output_handoffs(
    preempt_owner: bool,
) -> None:
    class Bridge(_CapturingGenerationBridge):
        async def emit_realtime_effect(self, *_args: object, **_kwargs: object) -> bool:
            return True

    provider = FakeMediaProvider()
    bridge = Bridge()
    registry = MediaVoiceCoreRegistry(bridge=bridge, provider_factory=lambda _: provider)
    registry.install()
    identity = SessionIdentity(f"queued-ack-deep-handoff-{preempt_owner}")
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    try:
        fence = await context.runtime.on_turn_committed("你好")
        context.output.playback.start(fence)
        assert await registry.generate_reply(identity.session_id, "你好", fence)
        assert len(bridge.frames) == 1
        if not preempt_owner:
            await _finish_output_owner_playback(registry, identity, bridge, session)
            assert context.output.output_owner is None
        coordinator = context.runtime.orchestrator.delegation
        context.runtime.on_user_voice_started()
        now = int(time.time() * 1_000)
        works = []
        for name, kind, pcm in (
            ("answer", media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT, b"\x04\x00\x05\x00"),
            ("ack", media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT, b"\x06\x00\x07\x00"),
        ):
            intent = media_pb2.OutputIntent(
                intent_id=name, session_id=identity.session_id, turn_id=fence.turn_id,
                generation_id=fence.generation_id, tool_epoch=fence.tool_epoch,
                kind=kind, priority=100, created_at_ms=now, expires_at_ms=now + 20_000,
                context_version=coordinator.current_context_version(identity.session_id),
                floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
                pcm_s16le=pcm,
            )
            coordinator.admit_output_intent(
                intent, current_fence=fence,
                current_context_version=coordinator.current_context_version(identity.session_id),
                floor_allows_output=False, now_ms=now,
            )
            work = _OutputWork(intent, fence)
            works.append(work)
            assert await registry._enqueue_output_work(context, work)
        assert len(bridge.frames) == 1
        context.runtime.open_assistant_floor(cause="empty_input_retired")
        if preempt_owner:
            assert await registry._enqueue_output_work(context, works[-1])
        else:
            registry._schedule_output_retry(context)
        await _wait_until(lambda: len(bridge.frames) == 2)
        assert len(context.output.output_work) == 2
        assert all(work.fence.matches(context.runtime.fence) for work in context.output.output_work.values())
        assert not {"answer", "ack"}.intersection(context.output.output_work)
        assert context.output.output_owner.intent.kind == media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT
        await _finish_output_owner_playback(registry, identity, bridge, session)
        await _wait_until(lambda: len(bridge.frames) == 3)
        assert context.output.output_owner.intent.kind == media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT
        deep_fence = context.output.output_owner.fence
        await _finish_output_owner_playback(registry, identity, bridge, session)
        assert context.output.reply_delivery.get(deep_fence).playback_ended
        assert not context.output.output_work
        assert len(bridge.frames) == 3
        assert context.runtime.fence.turn_id == fence.turn_id
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_qa_same_question_after_delivered_answer_is_not_blocked() -> None:
    """QA item 2(a): once the answer is delivered, re-asking must be answered.

    The COMPLETED claim has resolved and playback is over, so the widened gate
    must not treat the finished delegation as still in flight.
    """

    question = "今天南京天气怎么样"
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("qa-same-question-after-delivered")
    session = bridge.bridge.open(identity)
    try:
        context, fence = await _qa_commit_question_then_finish_ack_playback(
            registry,
            identity,
            bridge,
            provider,
            session,
            question,
        )
        claim = context.output.delegation_output_claims[fence]
        provider.release.set()
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        await _wait_until(lambda: claim.state is DelegationOutputState.COMPLETED, timeout=2.0)
        deep_owner = context.output.output_owner
        assert deep_owner is not None
        deep_fence = deep_owner.fence
        await _wait_until(lambda: len(bridge.frames) >= 2, timeout=2.0)
        deep_frame = bridge.frames[-1]
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=deep_fence.generation_id,
                received_sequence=deep_frame.sequence,
                rendered_sample_end=(deep_frame.source_start_sample + deep_frame.frame_samples),
                client_monotonic_ms=2,
                turn_id=deep_fence.turn_id,
                tool_epoch=deep_fence.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
        await _wait_until(lambda: context.output.output_owner is None, timeout=2.0)
        assert not registry._reply_or_delegation_pending(context)
        repeat_fence, repeat_reason = await _qa_commit_repeat_question(
            registry,
            context,
            identity,
            text=question,
            start_sample=600,
            end_sample=1200,
            retire_sample=1240,
        )
        assert repeat_reason != "duplicate_media_turn"
        assert repeat_fence is not None
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_qa_different_question_in_delegation_window_is_not_blocked() -> None:
    """QA item 2(d): the gate is text-scoped; a new question must go through."""

    question = "今天南京天气怎么样"
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("qa-different-question")
    session = bridge.bridge.open(identity)
    try:
        context, fence = await _qa_commit_question_then_finish_ack_playback(
            registry,
            identity,
            bridge,
            provider,
            session,
            question,
        )
        assert context.output.delegation_output_claims[fence].state is DelegationOutputState.OWNED
        repeat_fence, repeat_reason = await _qa_commit_repeat_question(
            registry,
            context,
            identity,
            text="北京明天天气怎么样",
            start_sample=600,
            end_sample=1200,
            retire_sample=1240,
        )
        assert repeat_reason != "duplicate_media_turn"
        assert repeat_fence is not None
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_qa_different_question_delivers_after_superseding_old_delegation() -> None:
    """A real topic switch must cancel the old lookup and render the new one."""

    class TopicSwitchProvider(_LateOwnedDelegationProvider):
        def __init__(self) -> None:
            super().__init__()
            self.delegation_started: dict[str, asyncio.Event] = {}
            self.release_by_text: dict[str, asyncio.Event] = {}
            self.output_texts: list[str] = []

        async def start_delegation(self, text: str, _fence: GenerationFence) -> str:
            started = self.delegation_started.setdefault(text, asyncio.Event())
            release = self.release_by_text.setdefault(text, asyncio.Event())
            started.set()
            await release.wait()
            return f"答案：{text}。"

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
            self.output_texts.append(str(getattr(intent, "tts_source", "") or ""))
            return super().generate_output(
                _identity,
                intent,
                _fence,
                work_id=work_id,
                source_start_sample=source_start_sample,
            )

    first_question = "今天南京天气怎么样"
    second_question = "北京明天天气怎么样"
    provider = TopicSwitchProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("qa-topic-switch-delivers-new")
    session = bridge.bridge.open(identity)
    try:
        context, first_fence = await _qa_commit_question_then_finish_ack_playback(
            registry,
            identity,
            bridge,
            provider,
            session,
            first_question,
        )
        await asyncio.wait_for(
            provider.delegation_started[first_question].wait(),
            timeout=2,
        )
        first_claim = context.output.delegation_output_claims[first_fence]
        assert first_claim.state is DelegationOutputState.OWNED

        second_fence, reason = await _qa_commit_repeat_question(
            registry,
            context,
            identity,
            text=second_question,
            start_sample=600,
            end_sample=1200,
            retire_sample=1240,
        )
        assert second_fence is not None, reason
        assert second_fence.turn_id > first_fence.turn_id
        await _wait_until(
            lambda: second_fence in context.output.delegation_output_claims,
            timeout=2,
        )
        await _wait_until(
            lambda: second_question in provider.delegation_started,
            timeout=2,
        )
        assert set(provider.delegation_started) == {first_question, second_question}, provider.delegation_started

        provider.release_by_text[second_question].set()
        await _wait_until(
            lambda: any(
                text.startswith("答案：北京明天天气怎么样")
                for text in provider.output_texts
            ),
            timeout=2,
        )
        assert any(
            text.startswith("答案：北京明天天气怎么样")
            for text in provider.output_texts
        )
        assert not any(
            text.startswith("答案：今天南京天气怎么样")
            for text in provider.output_texts
        )
        assert first_claim.state is DelegationOutputState.RELEASED
        assert not any(
            result.reason == "output_intent_inactive"
            for result in context.output.output_results
        )
        await _wait_until(
            lambda: not context.runtime.orchestrator.task_manager.tasks,
            timeout=2,
        )
    finally:
        provider.release_by_text.setdefault(first_question, asyncio.Event()).set()
        provider.release_by_text.setdefault(second_question, asyncio.Event()).set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_qa_released_delegation_local_fallback_emits_audio() -> None:
    """QA item 2(c): the RELEASED local fallback is not a dead end."""

    class FailingProvider(_DelegationProbeProvider):
        async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
            raise RuntimeError("deep provider exploded")

    provider = FailingProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("qa-released-fallback-audio")
    bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        query = "今天南京天气怎么样"
        fence = await context.runtime.on_turn_committed(query)
        context.output.playback.start(fence)
        assert await registry.generate_reply(identity.session_id, query, fence)
        await _wait_until(lambda: provider.reply_calls == 1, timeout=2.0)
        claim = context.output.delegation_output_claims[fence]
        assert claim.state is DelegationOutputState.RELEASED
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        assert bridge.frames, "local fallback emitted no audio frame"
        assert context.output.output_owner is not None
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_slow_lookup_uses_a_single_cue() -> None:
    """慢查询只播一句稍长提示，不再追加同质第二句稍等。

    epoch 1900 用 THINKING_FILLER 盖静音；真机反馈两句同质稍等体验差，
    改为更长的 LIVE_LOOKUP_FILLER 单句，并关掉第二句 cue。
    """

    class CueProvider(_LateOwnedDelegationProvider):
        def __init__(self) -> None:
            super().__init__()
            self.output_texts: list[str] = []

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
            self.output_texts.append(str(getattr(intent, "tts_source", "") or ""))
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

    provider = CueProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("slow-lookup-single-cue")
    session = bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        await context.runtime.on_turn_committed("今天南京天气怎么样")
        await asyncio.wait_for(provider.ack_started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2.0)
        await _finish_output_owner_playback(registry, identity, bridge, session)
        # Slow lookup remains pending; the old second cue fired after ~2.5s.
        await asyncio.sleep(0.6)
        assert provider.output_texts == [LIVE_LOOKUP_FILLER]
        assert provider.output_kinds == [
            media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT,
        ]
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_stale_generation_media_delegation_produces_no_output() -> None:
    started = asyncio.Event()
    deep_gate = asyncio.Event()

    class StaleProvider(_DelegationProbeProvider):
        async def start_delegation(self, text: str, fence: GenerationFence) -> str:
            self.delegations.append((text, fence))
            started.set()
            await deep_gate.wait()
            return "南京今天多云。"

    provider = StaleProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("stale-delegation")
    context = await registry.open_session(identity)
    first_fence = await context.runtime.on_turn_committed("今天南京天气怎么样")
    claim = context.output.delegation_output_claims[first_fence]

    # Wait until the deep provider is actually working so the supersede
    # happens mid-flight rather than before the delegation started.
    await asyncio.wait_for(started.wait(), timeout=1)

    # Supersede the generation while the provider is still working; the old
    # delegation must never admit an ACK, a deep result or a local reply.
    await context.runtime.on_turn_committed("你好")
    await asyncio.sleep(0.05)
    assert provider.output_kinds == []
    assert provider.reply_calls == 0

    deep_gate.set()
    await _wait_until(lambda: claim.state is DelegationOutputState.RELEASED)
    await asyncio.sleep(0.02)
    assert provider.output_kinds == []
    assert provider.reply_calls == 0
    assert provider.delegations == [("今天南京天气怎么样", first_fence)]
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_concurrent_media_normal_replies_produce_only_one_local_output_after_delegation_failure() -> (
    None
):
    class FailingProvider(_DelegationProbeProvider):
        async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
            raise RuntimeError("deep provider exploded")

    provider = FailingProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("delegation-duplicate-guard")
    context = await registry.open_session(identity)
    query = "今天南京天气怎么样"
    fence = await context.runtime.on_turn_committed(query)
    context.output.playback.start(fence)

    first = asyncio.create_task(registry.generate_reply(identity.session_id, query, fence))
    second = asyncio.create_task(registry.generate_reply(identity.session_id, query, fence))
    assert await asyncio.wait_for(first, timeout=1)
    assert await asyncio.wait_for(second, timeout=1)
    await _wait_until(lambda: provider.reply_calls == 1)

    await asyncio.sleep(0.02)
    claim = context.output.delegation_output_claims[fence]
    assert claim.state is DelegationOutputState.RELEASED
    assert provider.reply_calls == 1
    assert provider.output_kinds == []
    await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_playback_ack_without_text_spans_still_completes_speaking() -> None:
    provider = FakeMediaProvider()
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        turn_endpoint_grace_s=0.01,
    )
    registry.install()
    identity = SessionIdentity("no-span-ack-session")
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    fence = GenerationFence(identity.session_id, 1, 1, 0)
    assert await context.runtime.accept_media_generation(fence, cause="test")
    await context.runtime.on_assistant_speaking("你好。")
    context.runtime.orchestrator.state_machine.state = ConversationState.SPEAKING
    context.output.playback.start(fence)
    # Audio is delivered and fully rendered, but the provider never supplied a
    # timed text span: the ledger has a received watermark without any span.
    assert context.output.playback.register_audio(fence, 0, 0, 2)
    context.output.provider_complete = True
    completed: list[tuple[GenerationFence, str]] = []
    original = context.runtime.on_media_playback_done

    async def spy(
        done_fence: GenerationFence,
        heard_text: str,
        *,
        tools_active: bool = False,
    ) -> bool:
        completed.append((done_fence, heard_text))
        return await original(done_fence, heard_text, tools_active=tools_active)

    context.runtime.on_media_playback_done = spy  # type: ignore[method-assign]
    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=0,
            rendered_sample_end=2,
            client_monotonic_ms=1,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
        ),
    )

    # The final ACK advances only the audio watermark and yields no new text
    # span; the registry must still finalize playback instead of staying
    # SPEAKING forever.
    assert completed == [(fence, "")]
    assert context.runtime.orchestrator.state is ConversationState.LISTENING


class _TerminalCapturingBridge(MediaBridgeGrpcServer):
    """Record every generation action and cancel effect for terminal assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[Any] = []
        self.generations: list[tuple[int, GenerationFence]] = []
        self.effects: list[tuple[int, GenerationFence, str, dict[str, object]]] = []

    async def emit_pcm(self, _session_id: str, frame: object) -> bool:
        self.frames.append(frame)
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


async def _ack_frame(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    session: Any,
    bridge: _TerminalCapturingBridge,
    fence: GenerationFence,
) -> None:
    frame = next(
        item
        for item in reversed(bridge.frames)
        if item.turn_id == fence.turn_id and item.generation_id == fence.generation_id
    )
    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=frame.sequence,
            rendered_sample_end=frame.source_start_sample + frame.frame_samples,
            client_monotonic_ms=1,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("barge_in", [False, True])
async def test_commit_returning_after_filler_first_frame_keeps_same_turn_output(
    barge_in: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filler that starts while the turn is committing must survive the commit.

    The regression read the previous generation before ``commit_user_turn``
    yielded, then cancelled whatever task was current after it returned.  With
    barge-in enabled that killed the just-started filler without a terminal
    event, leaving the device in SPEAKING.
    """

    question = "今天南京天气怎么样"
    provider = _LateOwnedDelegationProvider()
    bridge = _TerminalCapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        runtime_factory=lambda session_id: DuplexRuntime.create(
            session_id=session_id,
            barge_in_enabled=barge_in,
        ),
        turn_endpoint_grace_s=60,
        turn_endpoint_absolute_timeout_s=60,
    )
    registry.install()
    identity = SessionIdentity(f"same-turn-filler-barge-in-{barge_in}")
    session = bridge.bridge.open(identity)
    context = None
    try:
        context = await _seed_pending_media_turn(
            registry,
            identity,
            text=question,
            endpoint_sample=600,
            retire_sample=640,
        )
        original_commit = registry.commit_user_turn

        async def commit_after_filler_first_frame(
            session_id: str,
            *,
            stream_epoch: int,
            start_sample: int,
            end_sample: int,
            retire_sample: int | None = None,
            provider_final_missing: bool = False,
        ) -> tuple[GenerationFence | None, str | None]:
            result = await original_commit(
                session_id,
                stream_epoch=stream_epoch,
                start_sample=start_sample,
                end_sample=end_sample,
                retire_sample=retire_sample,
                provider_final_missing=provider_final_missing,
            )
            fence = result[0]
            assert fence is not None
            # The delegation filler starts inside the commit.  Returning only
            # after its first frame reproduces the real 1.6 s commit window.
            await _wait_until(
                lambda: any(
                    frame.turn_id == fence.turn_id
                    and frame.generation_id == fence.generation_id
                    for frame in bridge.frames
                ),
                timeout=2,
            )
            return result

        monkeypatch.setattr(registry, "commit_user_turn", commit_after_filler_first_frame)
        reason = await registry._commit_pending_turn(context)  # noqa: SLF001
        assert reason == (None if barge_in else "heard_output_in_flight")
        owner = context.output.output_owner
        assert owner is not None
        filler_fence = owner.fence
        assert filler_fence.turn_id == context.runtime.fence.turn_id
        delivery = context.output.reply_delivery.get(filler_fence)
        assert delivery is not None and delivery.first_frame_sent
        assert not any(cancelled.matches(filler_fence) for cancelled in provider.cancelled)
        assert context.output.output_owner is owner

        await _ack_frame(registry, identity, session, bridge, filler_fence)
        provider.release.set()
        await _wait_until(
            lambda: any(
                frame.turn_id == filler_fence.turn_id
                and frame.generation_id > filler_fence.generation_id
                for frame in bridge.frames
            ),
            timeout=3,
        )
        deep_frame = next(
            frame
            for frame in bridge.frames
            if frame.turn_id == filler_fence.turn_id
            and frame.generation_id > filler_fence.generation_id
        )
        deep_fence = GenerationFence(
            identity.session_id,
            deep_frame.turn_id,
            deep_frame.generation_id,
            deep_frame.tool_epoch,
        )
        await _ack_frame(registry, identity, session, bridge, deep_fence)
        await _wait_until(
            lambda: context.runtime.orchestrator.state is ConversationState.LISTENING,
            timeout=3,
        )

        first_frame_generations = {(frame.turn_id, frame.generation_id) for frame in bridge.frames}
        terminal_generations = {
            (fence.turn_id, fence.generation_id)
            for action, fence in bridge.generations
            if action in {media_pb2.GENERATION_ACTION_COMPLETE, media_pb2.GENERATION_ACTION_CANCEL}
        }
        assert first_frame_generations <= terminal_generations
        assert not context.runtime.assistant_speaking
        assert context.runtime.orchestrator.state is ConversationState.LISTENING
    finally:
        provider.release.set()
        if context is not None:
            await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_cancelled_audible_output_gets_terminal_generation_and_listening() -> None:
    """Cancelling a generation that already sent its first frame must close it."""

    class BlockingProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        def generate_reply(
            self,
            _identity: SessionIdentity,
            _user_text: str,
            _fence: GenerationFence,
        ) -> AsyncIterator[MediaReplyChunk]:
            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                self.started.set()
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=0,
                    text="你好。",
                    first=True,
                    final=False,
                )
                await self.release.wait()

            return chunks()

    provider = BlockingProvider()
    bridge = _TerminalCapturingBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = _device_identity("cancelled-audible-output")
    bridge.bridge.open(identity)
    reply: asyncio.Task[bool] | None = None
    try:
        context = await registry.open_session(identity)
        fence = await context.runtime.on_turn_committed("你好")
        context.output.playback.start(fence)
        reply = asyncio.create_task(
            registry.generate_reply(identity.session_id, "你好", fence)
        )
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        await _wait_until(lambda: bool(bridge.frames), timeout=2)
        delivery = context.output.reply_delivery.get(fence)
        assert delivery is not None and delivery.first_frame_sent
        assert context.runtime.assistant_speaking

        await registry._cancel_reply_task(context, fence, reason="test_cancel")  # noqa: SLF001

        assert not context.runtime.assistant_speaking
        assert context.runtime.orchestrator.state is ConversationState.LISTENING
        assert context.runtime.fence.generation_id > fence.generation_id
        assert any(
            action == media_pb2.GENERATION_ACTION_CANCEL
            and cancelled.generation_id > fence.generation_id
            for action, cancelled in bridge.generations
        )
        assert any(
            effect_kind == media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION
            and cancelled.generation_id > fence.generation_id
            for effect_kind, cancelled, _source, _payload in bridge.effects
        )
        terminal = context.output.reply_delivery.get(fence)
        assert terminal is not None
        assert terminal.terminal_event is not None
    finally:
        provider.release.set()
        if reply is not None:
            await asyncio.gather(reply, return_exceptions=True)
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_fixed_weather_fallback_is_the_first_and_only_spoken_text() -> None:
    class FallbackProvider(FakeMediaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.output_intents: list[Any] = []

        async def start_delegation(self, _text: str, _fence: GenerationFence) -> str:
            return WEATHER_LOOKUP_UNAVAILABLE_REPLY

        def accept_output_intent(self, intent: Any) -> None:
            self.output_intents.append(intent)

        def generate_output(
            self,
            _identity: SessionIdentity,
            _intent: Any,
            _fence: GenerationFence,
            *,
            work_id: str,
            source_start_sample: int,
        ) -> AsyncIterator[MediaReplyChunk]:
            _ = work_id

            async def chunks() -> AsyncIterator[MediaReplyChunk]:
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00\x03\x00",
                    source_start_sample=source_start_sample,
                    first=True,
                    final=True,
                )

            return chunks()

    provider = FallbackProvider()
    registry = MediaVoiceCoreRegistry(
        bridge=MediaBridgeGrpcServer(),
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    context = await registry.open_session(SessionIdentity("weather-fallback-first"))
    fence = await context.runtime.on_turn_committed("今天南京天气怎么样")
    context.output.playback.start(fence)

    await _wait_until(lambda: provider.output_intents, timeout=2)

    assert str(provider.output_intents[0].tts_source) == WEATHER_LOOKUP_UNAVAILABLE_REPLY
    await registry.finalize_session(context.identity.session_id)


@pytest.mark.asyncio
async def test_delegation_answer_waits_for_floor_cap_without_a_pending_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Soak 20261030-150810 t010: ownership handoff + closed floor + no pending turn.

    The weather turn was committed, the reply dispatch handed audio to the
    delegation, and a later statement closed the floor while ``pending`` held
    no turn: the evidence-less cap required ``turn_start_sample`` and could
    never fire, so the ready answer stayed queued with zero audio.
    """

    from services.agent.src.voice_core import media_session_turns

    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_BASE_S", 0.3)
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_VAD_EXTENSION_S", 0.2)
    monkeypatch.setattr(media_session_turns, "_EVIDENCE_LESS_HOLD_MAX_S", 0.6)
    provider = _LateOwnedDelegationProvider()
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
    )
    registry.install()
    identity = SessionIdentity("delegation-floor-cap-no-pending-turn")
    bridge.bridge.open(identity)
    try:
        context = await registry.open_session(identity)
        query = "今天南京天气怎么样"
        fence = await context.runtime.on_turn_committed(query)
        await _wait_until(lambda: fence in context.output.delegation_output_claims)
        claim = context.output.delegation_output_claims[fence]
        await _wait_until(lambda: claim.state is DelegationOutputState.OWNED)
        assert await registry.generate_reply(identity.session_id, query, fence)
        context.runtime.on_user_voice_started()
        assert context.pending.turn_start_sample is None
        assert not context.runtime.output_floor_allows_assistant
        frame_count = len(bridge.frames)
        provider.release.set()
        await _wait_until(lambda: claim.state is DelegationOutputState.COMPLETED)
        await asyncio.wait_for(provider.deep_started.wait(), timeout=2)
        await _wait_until(lambda: len(bridge.frames) > frame_count, timeout=2)
        assert context.runtime.output_floor_allows_assistant
        assert context.pending.evidence_less_hold_since is None
        assert provider.output_kinds.count(media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT) == 1
        assert context.output.output_owner is not None
    finally:
        provider.release.set()
        await registry.finalize_session(identity.session_id)
