from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from services.control_api.app.assistant_snapshot import project_assistant_snapshot
from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.control_api.tests.test_device_control_api import _profile_app


def test_freshness_and_connection_fences():
    now = datetime.now(UTC)
    snapshot = {
        "session_id": "session",
        "stream_epoch": 2,
        "phase": "thinking_silent",
        "observed_at": now.isoformat(),
    }
    runtime = {
        "connected": True,
        "session_id": "session",
        "stream_epoch": 2,
        "assistant_state": snapshot,
    }
    assert project_assistant_snapshot(runtime, now=now)["phase"] == "thinking_silent"
    assert (
        project_assistant_snapshot(runtime, now=now + timedelta(seconds=16))["status"] == "unknown"
    )
    assert (
        project_assistant_snapshot({**runtime, "stream_epoch": 3}, now=now)["status"] == "unknown"
    )
    assert (
        project_assistant_snapshot({**runtime, "connected": False}, now=now)["status"] == "offline"
    )
    assert project_assistant_snapshot(None, now=now)["status"] == "unknown"
    assert (
        project_assistant_snapshot({**runtime, "assistant_state": None}, now=now)["status"]
        == "unknown"
    )


async def test_read_endpoint_protects_device_owner(tmp_path):
    app, _ = _profile_app(tmp_path, category="adult")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/devices/dev_test_01/session-state")
        assert response.status_code == 200
        assert response.json()["status"] == "offline"
        app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedUser(
            user_id="other", session_id="s", jti="j"
        )
        assert (await client.get("/v1/devices/dev_test_01/session-state")).status_code in (403, 404)
