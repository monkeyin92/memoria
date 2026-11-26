"""A real registry publication must preserve the issued identity epoch."""

import json

import pytest
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.tests.unit.media_session_support import (
    FakeMediaProvider,
    open_bridge_connection,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy


@pytest.mark.asyncio
@pytest.mark.parametrize("with_profile", [False, True])
async def test_registry_preserves_signed_epoch_in_client_envelope(with_profile):
    bridge = MediaBridgeGrpcServer()
    registry = MediaVoiceCoreRegistry(bridge=bridge, provider_factory=lambda _: FakeMediaProvider())
    registry.install()
    identity = SessionIdentity("profile-envelope", stream_epoch=18)
    connection = open_bridge_connection(bridge, identity)
    context = await registry.open_session(identity)
    try:
        if with_profile:
            bind_owner_policy(context.runtime)
        connection = bridge._connections[identity.session_id]
        while not connection.outgoing.empty():
            connection.outgoing.get_nowait()
        task = context.runtime.publish_assistant_state("user_speaking")
        assert task is not None
        await task
        events = []
        while not connection.outgoing.empty():
            events.append(connection.outgoing.get_nowait())
        state = next(event.client for event in events if event.HasField("client"))
        payload = json.loads(state.json_payload)
        assert payload["session_epoch"] == (1 if with_profile else 0)
        assert (
            payload["payload"]["session_epoch"]
            == payload["session_epoch"]
            == context.runtime.fence.session_epoch
        )
    finally:
        await registry.finalize_session(identity.session_id)
