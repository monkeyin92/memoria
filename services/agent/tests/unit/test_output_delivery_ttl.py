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
