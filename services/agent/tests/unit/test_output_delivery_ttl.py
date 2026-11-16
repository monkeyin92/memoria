"""Queue expiry must not revoke a delivering owner."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.agent.src.voice_core.media_session_output_owner import MediaOutputOwnerMixin as Owner
from services.agent.src.voice_core.media_session_types import OutputWork
from services.agent.tests.unit.test_delegation_coordinator import _coordinator_with_result, _request


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', [119999, 120000, 240000])
@pytest.mark.parametrize('started', [False, True])
@pytest.mark.parametrize('gate', ['none', 'floor', 'turn', 'generation', 'context', 'closed', 'standby', 'disconnect', 'cancelled', 'winner'])
async def test_delivery_ttl(monkeypatch, boundary, started, gate):
    clock = [1000]
    monkeypatch.setattr('time.time', lambda: clock[0] / 1000)
    coordinator = await _coordinator_with_result('A long answer')
    request = _request()
    handle = await coordinator.delegate(request)
    _ = [event async for event in coordinator.events(handle)]
    intent = coordinator.output_intent(handle, current_fence=request.fence,
        current_task_epoch=request.task_epoch, current_context_version=5,
        relevant=True, now_ms=1000)
    fence = replace(request.fence, generation_id=request.fence.generation_id + 1)
    runtime = SimpleNamespace(fence=fence, output_floor_allows_assistant=True,
        orchestrator=SimpleNamespace(delegation=coordinator, context_version_for_fence=lambda f: 5))
    context = SimpleNamespace(runtime=runtime, closed=False, standby_requested=False,
        output=SimpleNamespace(output_owner=None, provider_complete=False, output_work={}))
    work = Owner._rebind_output_work(context, OutputWork(intent, request.fence), fence, context_version=5)
    assert work.intent.expires_at_ms == 121000
    args = dict(current_fence=fence, current_context_version=5, floor_allows_output=True)
    coordinator.admit_output_intent(work.intent, **args)
    lease = Owner._acquire_output_owner(context, fence, work.intent)
    assert lease is not None
    if started:
        coordinator.start_output_intent_delivery(work.intent)
    clock[0] += boundary
    assert Owner._output_owner_is_current(context, lease) is (started or boundary < 120000)
    if started and gate != 'none':
        coordinator.queue_while_floor_blocked = True
        runtime.output_floor_allows_assistant = gate != 'floor'
        if gate in ('turn', 'generation'):
            runtime.fence = replace(fence, **{gate + '_id': getattr(fence, gate + '_id') + 1})
        if gate == 'winner':
            competitor = coordinator.conversation_reply(fence=fence, context_version=5, expires_at_ms=clock[0] + 1000, now_ms=clock[0])
            coordinator.admit_output_intent(competitor, **args)
        if gate == 'context':
            coordinator.activate_context_version(fence.session_id, 6)
        if gate == 'disconnect':
            coordinator.reset_output_intent_state(fence.session_id)
        if gate == 'cancelled':
            Owner._release_output_owner(context, fence, reason='cancelled')
        context.closed = gate == 'closed'
        context.standby_requested = gate == 'standby'
        assert not Owner._output_owner_is_current(context, lease)
    Owner._release_output_owner(context, fence, reason='completed')
    assert context.output.output_owner is None
    if started and gate == 'winner':
        assert coordinator.current_output_intent(fence.session_id, **args).intent_id == competitor.intent_id
        coordinator.complete_output_intent(competitor, **args)
    assert coordinator.current_output_intent(fence.session_id, **args) is None
    assert not coordinator._delivering_by_session


@pytest.mark.asyncio
async def test_deep_result_stream_completes_after_queue_ttl(monkeypatch):
    import asyncio
    import time

    from services.agent.src.orchestration.state_machine import ConversationState
    from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
    from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
    from services.agent.src.voice_core.media_protocol import (
        PlaybackEventType,
        PlaybackProgress,
        SessionIdentity,
    )
    from services.agent.src.voice_core.media_session import MediaReplyChunk, MediaVoiceCoreRegistry
    from services.agent.tests.unit.media_session_support import FakeMediaProvider

    accepted = asyncio.Event()
    release = asyncio.Event()
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    class Provider(FakeMediaProvider):
        def generate_output(self, identity, intent, fence, **kwargs):
            async def chunks():
                yield MediaReplyChunk(
                    pcm_s16le=b"\x02\x00" * 480, source_start_sample=0, first=True
                )
                await release.wait()
                yield MediaReplyChunk(
                    pcm_s16le=b"\x03\x00" * 480, source_start_sample=480, final=True
                )

            return chunks()

    class Bridge(MediaBridgeGrpcServer):
        def __init__(self):
            super().__init__()
            self.frames = []

        async def emit_pcm(self, session_id, frame):
            self.frames.append(frame)
            accepted.set()
            return True

        async def emit_generation(self, *args, **kwargs):
            return True

    provider = Provider()
    bridge = Bridge()
    registry = MediaVoiceCoreRegistry(bridge=bridge, provider_factory=lambda _: provider)
    registry.install()
    identity = SessionIdentity("deep-result-ttl-stream")
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    try:
        fence = await context.runtime.on_turn_committed("故事")
        context.output.playback.start(fence)
        coordinator = context.runtime.orchestrator.delegation
        now = int(clock[0] * 1000)
        intent = media_pb2.OutputIntent(
            intent_id="deep-ttl",
            tts_source="故事",
            session_id=identity.session_id,
            turn_id=fence.turn_id,
            generation_id=fence.generation_id,
            tool_epoch=fence.tool_epoch,
            kind=media_pb2.OUTPUT_INTENT_KIND_DEEP_RESULT,
            priority=1,
            created_at_ms=now,
            expires_at_ms=now + 120000,
            context_version=coordinator.current_context_version(identity.session_id),
            floor_requirement=media_pb2.FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
        )
        coordinator.admit_output_intent(
            intent,
            current_fence=fence,
            current_context_version=intent.context_version,
            floor_allows_output=True,
        )
        task = asyncio.create_task(registry._run_output_work(context, OutputWork(intent, fence)))
        await asyncio.wait_for(accepted.wait(), 1)
        clock[0] += 121
        release.set()
        result = await asyncio.wait_for(task, 1)
        assert result.status.value == "completed"
        assert len(bridge.frames) == 2
        assert context.output.provider_complete
        assert provider.cancelled == []
        await registry.on_playback_progress(
            session,
            PlaybackProgress(
                identity=identity,
                generation_id=fence.generation_id,
                received_sequence=1,
                rendered_sample_end=960,
                client_monotonic_ms=121000,
                turn_id=fence.turn_id,
                tool_epoch=fence.tool_epoch,
                event_type=PlaybackEventType.ENDED,
            ),
        )
        assert context.output.output_owner is None
        assert context.runtime.orchestrator.state is ConversationState.LISTENING
        assert not coordinator._delivering_by_session
    finally:
        release.set()
        await registry.finalize_session(identity.session_id)
