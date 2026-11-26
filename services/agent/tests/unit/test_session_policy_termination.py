from __future__ import annotations

import asyncio

import httpx
import pytest
from services.agent.src.mode_policy_client import (
    ModePolicyClient,
    ModePolicyClientConfig,
    RuntimeSessionTerminated,
)
from services.agent.tests.unit.test_production_prompt_profile_gating import (
    _policy,
    _profile,
    _runtime,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,detail,terminal",
    [
        (410, {"code": "session_terminated", "reason": "consent_withdrawn"}, True),
        (410, {"code": "unknown"}, False),
        (403, {"code": "session_terminated"}, False),
        (401, {"code": "invalid_token"}, False),
        (503, {"code": "session_runtime_authority_unavailable"}, False),
    ],
)
async def test_only_explicit_terminal_contract_ends_agent_session(
    monkeypatch, status, detail, terminal
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"detail": detail})
        )
    ) as http:
        client = ModePolicyClient(
            ModePolicyClientConfig(
                endpoint="http://control/session-policy", internal_token="fixture"
            ),
            client=http,
        )
        runtime = _runtime(monkeypatch)
        runtime.set_mode_policy(_policy(_profile(epoch=1)))

        async def refresh():
            return (await client.fetch(session_id=runtime.session_id)).runtime_profile

        runtime.set_runtime_profile_refresher(refresh)
        try:
            if terminal:
                with pytest.raises(RuntimeSessionTerminated):
                    await asyncio.wait_for(
                        runtime._spawn(
                            runtime.on_turn_committed("continue talking"), name="test-turn"
                        ),
                        timeout=2,
                    )
                assert runtime._closed
                assert runtime.fence.turn_id == 0

                async def transient_after_terminal():
                    return None

                runtime.set_runtime_profile_refresher(transient_after_terminal)
                with pytest.raises(RuntimeSessionTerminated):
                    await runtime.on_turn_committed("another turn")
                assert runtime.fence.turn_id == 0
            else:
                assert await runtime.refresh_runtime_profile() is None
                assert not runtime._closed
                assert runtime.orchestrator.runtime_profiles.current is None
                assert runtime.mode_policy.allows_conversation()
        finally:
            await runtime.close()


@pytest.mark.asyncio
async def test_network_failure_remains_unknown_safe(monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("authority unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        client = ModePolicyClient(
            ModePolicyClientConfig(
                endpoint="http://control/session-policy", internal_token="fixture"
            ),
            client=http,
        )
        runtime = _runtime(monkeypatch)
        runtime.set_mode_policy(_policy(_profile(epoch=1)))

        async def refresh():
            return (await client.fetch(session_id=runtime.session_id)).runtime_profile

        runtime.set_runtime_profile_refresher(refresh)
        try:
            assert await runtime.refresh_runtime_profile() is None
            assert not runtime._closed
            assert runtime.mode_policy.allows_conversation()
        finally:
            await runtime.close()
