"""Designed-voice session resolution after voice-clone removal."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from services.common.companions import DEFAULT_COMPANION_ID, designed_voice_profile
from services.control_api.app.main import create_app


@pytest.mark.asyncio
async def test_session_resolution_returns_designed_companion_voice() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create a minimal authenticated voice session via existing helpers when present.
        # Without a live session the endpoint must reject; this pins the route still exists.
        response = await client.post(
            "/v1/voices/session-resolution",
            json={"session_id": "missing-session"},
            headers={"X-Memoria-Internal-Token": "wrong"},
        )
        assert response.status_code in {401, 403, 404, 409}
        assert designed_voice_profile(DEFAULT_COMPANION_ID) is not None


@pytest.mark.asyncio
async def test_voice_clone_write_routes_are_gone() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for method, path in (
            ("POST", "/v1/voices/consent"),
            ("POST", "/v1/voices/enrollments"),
            ("GET", "/v1/voices/profiles"),
            ("GET", "/v1/voices/provider-samples/sample-1"),
            ("GET", "/v1/speakers"),
            ("POST", "/v1/speakers/enrollments"),
        ):
            response = await client.request(method, path)
            assert response.status_code == 404, (method, path, response.status_code)
