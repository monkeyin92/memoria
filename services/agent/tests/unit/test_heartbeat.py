from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from services.agent.src import heartbeat as heartbeat_module
from services.agent.src.heartbeat import (
    AgentHeartbeat,
    AgentHeartbeatConfig,
    is_agent_heartbeat_healthy,
)


@pytest.mark.asyncio
async def test_agent_heartbeat_reports_boot_and_worker_readiness() -> None:
    requests: list[httpx.Request] = []
    registered = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="OK")
        requests.append(request)
        return httpx.Response(200, json={"status": "recorded"})

    heartbeat = AgentHeartbeat(
        AgentHeartbeatConfig(
            endpoint="http://control-api:8000/internal/readiness/agent-heartbeat",
            internal_token="agent-heartbeat-token",
            release_tag="release-heartbeat-test",
        ),
        registration_probe=lambda: registered,
        boot_id=UUID("8f819a3b-ec8f-4319-94ab-7cace979145f"),
        clock=lambda: datetime(2026, 7, 22, 10, 11, 12, tzinfo=UTC),
    )
    heartbeat.mark_worker_ready()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await heartbeat.report(client)
        registered = True
        await heartbeat.report(client)

    first = json.loads(requests[0].content)
    second = json.loads(requests[1].content)
    assert requests[0].headers["X-Memoria-Internal-Token"] == "agent-heartbeat-token"
    assert first == {
        "release_tag": "release-heartbeat-test",
        "boot_id": "8f819a3b-ec8f-4319-94ab-7cace979145f",
        "worker_ready": True,
        "livekit_ready": False,
        "last_loop_at": "2026-07-22T10:11:12+00:00",
    }
    assert second["livekit_ready"] is True


@pytest.mark.asyncio
async def test_agent_heartbeat_reports_livekit_not_ready_when_worker_health_fails() -> None:
    payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "127.0.0.1":
            return httpx.Response(503, text="failed to connect to livekit")
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "recorded"})

    heartbeat = AgentHeartbeat(
        AgentHeartbeatConfig(
            endpoint="http://control-api:8000/internal/readiness/agent-heartbeat",
            internal_token="agent-heartbeat-token",
            release_tag="release-heartbeat-test",
        ),
        registration_probe=lambda: True,
    )
    heartbeat.mark_worker_ready()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await heartbeat.report(client)

    assert payloads[0]["worker_ready"] is True
    assert payloads[0]["livekit_ready"] is False


@pytest.mark.asyncio
async def test_accepted_heartbeat_writes_non_secret_local_health_state(tmp_path: Path) -> None:
    state_path = tmp_path / "heartbeat.json"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200)
        return httpx.Response(200, json={"status": "recorded"})

    heartbeat = AgentHeartbeat(
        AgentHeartbeatConfig(
            endpoint="http://control-api:8000/internal/readiness/agent-heartbeat",
            internal_token="agent-heartbeat-token",
            release_tag="release-heartbeat-test",
            state_path=state_path,
        ),
        registration_probe=lambda: True,
        clock=lambda: datetime(2026, 7, 22, 10, 11, 12, tzinfo=UTC),
    )
    heartbeat.mark_worker_ready()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await heartbeat.report(client)

    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "release_tag": "release-heartbeat-test",
        "accepted_at": "2026-07-22T10:11:12+00:00",
    }


@pytest.mark.asyncio
async def test_accepted_not_ready_heartbeat_does_not_refresh_local_health_state(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "heartbeat.json"
    original = '{"release_tag":"old","accepted_at":"2026-07-22T10:00:00+00:00"}'
    state_path.write_text(original, encoding="utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(503)
        return httpx.Response(200, json={"status": "recorded"})

    heartbeat = AgentHeartbeat(
        AgentHeartbeatConfig(
            endpoint="http://control-api:8000/internal/readiness/agent-heartbeat",
            internal_token="agent-heartbeat-token",
            release_tag="release-heartbeat-test",
            state_path=state_path,
        ),
        registration_probe=lambda: True,
    )
    heartbeat.mark_worker_ready()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await heartbeat.report(client)

    assert state_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (401, {"detail": "invalid agent heartbeat authentication"}),
        (200, {"status": "ignored"}),
    ],
)
async def test_failed_or_rejected_heartbeat_does_not_refresh_local_health_state(
    tmp_path: Path,
    status_code: int,
    body: dict[str, str],
) -> None:
    state_path = tmp_path / "heartbeat.json"
    original = '{"release_tag":"old","accepted_at":"2026-07-22T10:00:00+00:00"}'
    state_path.write_text(original, encoding="utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200)
        return httpx.Response(status_code, json=body)

    heartbeat = AgentHeartbeat(
        AgentHeartbeatConfig(
            endpoint="http://control-api:8000/internal/readiness/agent-heartbeat",
            internal_token="agent-heartbeat-token",
            release_tag="release-heartbeat-test",
            state_path=state_path,
        ),
        registration_probe=lambda: True,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await heartbeat.report(client)

    assert state_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_heartbeat_rechecks_registration_and_recovers_after_reconnect(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "heartbeat.json"
    payloads: list[dict[str, object]] = []
    registered = True
    now = datetime(2026, 7, 22, 10, 11, 12, tzinfo=UTC)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200)
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "recorded"})

    heartbeat = AgentHeartbeat(
        AgentHeartbeatConfig(
            endpoint="http://control-api:8000/internal/readiness/agent-heartbeat",
            internal_token="agent-heartbeat-token",
            release_tag="release-heartbeat-test",
            state_path=state_path,
        ),
        registration_probe=lambda: registered,
        clock=lambda: now,
    )
    heartbeat.mark_worker_ready()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await heartbeat.report(client)
        accepted_state = state_path.read_text(encoding="utf-8")

        registered = False
        now = datetime(2026, 7, 22, 10, 11, 22, tzinfo=UTC)
        await heartbeat.report(client)
        assert state_path.read_text(encoding="utf-8") == accepted_state

        registered = True
        now = datetime(2026, 7, 22, 10, 11, 32, tzinfo=UTC)
        await heartbeat.report(client)

    assert [payload["livekit_ready"] for payload in payloads] == [True, False, True]
    assert json.loads(state_path.read_text(encoding="utf-8"))["accepted_at"] == (
        "2026-07-22T10:11:32+00:00"
    )


@pytest.mark.parametrize(
    ("state", "sdk_status", "expected"),
    [
        (None, 200, False),
        ({"release_tag": "expected", "accepted_at": "2026-07-22T10:10:41+00:00"}, 200, False),
        ({"release_tag": "expected", "accepted_at": "2026-07-22T10:10:42+00:00"}, 200, True),
        ({"release_tag": "expected", "accepted_at": "2026-07-22T10:11:13+00:00"}, 200, False),
        ({"release_tag": "wrong", "accepted_at": "2026-07-22T10:11:12+00:00"}, 200, False),
        ({"release_tag": "expected", "accepted_at": "2026-07-22T10:11:12+00:00"}, 503, False),
        ({"release_tag": "expected", "accepted_at": "2026-07-22T10:11:12+00:00"}, 204, True),
    ],
)
def test_agent_heartbeat_health_requires_fresh_matching_state_and_sdk_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, str] | None,
    sdk_status: int,
    expected: bool,
) -> None:
    state_path = tmp_path / "heartbeat.json"
    if state is not None:
        state_path.write_text(json.dumps(state), encoding="utf-8")

    class Response:
        status = sdk_status

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(heartbeat_module.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    assert (
        is_agent_heartbeat_healthy(
            state_path=state_path,
            release_tag="expected",
            now=datetime(2026, 7, 22, 10, 11, 12, tzinfo=UTC),
        )
        is expected
    )
