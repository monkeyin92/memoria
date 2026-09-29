from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from services.agent.src.heartbeat import (
    AgentHeartbeat,
    AgentHeartbeatConfig,
    build_agent_heartbeat,
    heartbeat_endpoint,
    is_agent_heartbeat_healthy,
    main,
)

_ENDPOINT = "http://control-api:8000/internal/readiness/agent-heartbeat"


def _heartbeat(
    state_path: Path,
    *,
    release_tag: str = "release-heartbeat-test",
    interval_s: float = 10.0,
    clock: object = None,
) -> AgentHeartbeat:
    return AgentHeartbeat(
        AgentHeartbeatConfig(
            endpoint=_ENDPOINT,
            internal_token="agent-heartbeat-token",
            release_tag=release_tag,
            state_path=state_path,
            interval_s=interval_s,
        ),
        boot_id=UUID("8f819a3b-ec8f-4319-94ab-7cace979145f"),
        clock=clock,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_heartbeat_reports_the_bridge_contract_without_livekit_fields(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "recorded"})

    heartbeat = _heartbeat(
        tmp_path / "heartbeat.json",
        clock=lambda: datetime(2026, 7, 22, 10, 11, 12, tzinfo=UTC),
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await heartbeat.report(client)
        heartbeat.mark_worker_ready()
        await heartbeat.report(client)
        heartbeat.mark_worker_stopped()
        await heartbeat.report(client)

    assert [request.method for request in requests] == ["POST", "POST", "POST"]
    assert requests[0].headers["X-Memoria-Internal-Token"] == "agent-heartbeat-token"
    payloads = [json.loads(request.content) for request in requests]
    assert payloads[1] == {
        "release_tag": "release-heartbeat-test",
        "boot_id": "8f819a3b-ec8f-4319-94ab-7cace979145f",
        "worker_ready": True,
        "last_loop_at": "2026-07-22T10:11:12+00:00",
    }
    assert [payload["worker_ready"] for payload in payloads] == [False, True, False]


@pytest.mark.asyncio
async def test_accepted_ready_heartbeat_writes_non_secret_local_health_state(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "heartbeat.json"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "recorded"})

    heartbeat = _heartbeat(
        state_path,
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

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "recorded"})

    heartbeat = _heartbeat(state_path)

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

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    heartbeat = _heartbeat(state_path)
    heartbeat.mark_worker_ready()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await heartbeat.report(client)

    assert state_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_release_tag_rejection_logs_status_and_reported_tag(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stack tag split must be distinguishable from Control API being down."""

    caplog.set_level(logging.WARNING, logger="services.agent.src.heartbeat")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "agent release tag does not match config"})

    heartbeat = _heartbeat(
        tmp_path / "heartbeat.json",
        release_tag="20260831-2215-miniprogram-bind-view-control-api",
        interval_s=0.01,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(heartbeat.run_with_client(client))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    rejected = [
        record.message
        for record in caplog.records
        if record.message.startswith("agent heartbeat rejected")
    ]
    assert rejected, f"no rejection logged; got {[r.message for r in caplog.records]}"
    assert "status=409" in rejected[0]
    assert "release_tag=20260831-2215-miniprogram-bind-view-control-api" in rejected[0]
    assert "agent release tag does not match config" in rejected[0]


@pytest.mark.asyncio
async def test_transport_failure_still_logs_generic_reason(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="services.agent.src.heartbeat")

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("control api unreachable")

    heartbeat = _heartbeat(tmp_path / "heartbeat.json", interval_s=0.01)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(heartbeat.run_with_client(client))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert any(
        record.message == "agent heartbeat failed: ConnectError" for record in caplog.records
    )


def test_heartbeat_endpoint_targets_the_archive_host_readiness_route() -> None:
    assert (
        heartbeat_endpoint("https://control-api:8443/v1/archive/session-events?x=1")
        == "https://control-api:8443/internal/readiness/agent-heartbeat"
    )


def test_build_agent_heartbeat_is_production_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-under-test")
    tokens: list[str] = []

    def internal_token(capability: str) -> str:
        tokens.append(capability)
        return "x" * 32

    production = SimpleNamespace(
        environment="production",
        archive_session_events_url="http://control-api:8000/v1/archive/session-events",
        internal_token=internal_token,
    )

    assert build_agent_heartbeat(SimpleNamespace(environment="development")) is None
    assert isinstance(build_agent_heartbeat(production), AgentHeartbeat)
    assert tokens == ["agent_heartbeat"]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (None, False),
        ({"release_tag": "expected", "accepted_at": "2026-07-22T10:10:41+00:00"}, False),
        ({"release_tag": "expected", "accepted_at": "2026-07-22T10:10:42+00:00"}, True),
        ({"release_tag": "expected", "accepted_at": "2026-07-22T10:11:13+00:00"}, False),
        ({"release_tag": "wrong", "accepted_at": "2026-07-22T10:11:12+00:00"}, False),
        ({"release_tag": "expected", "accepted_at": "2026-07-22T10:11:12"}, False),
    ],
)
def test_health_requires_fresh_matching_accepted_state(
    tmp_path: Path,
    state: dict[str, str] | None,
    expected: bool,
) -> None:
    state_path = tmp_path / "heartbeat.json"
    if state is not None:
        state_path.write_text(json.dumps(state), encoding="utf-8")

    assert (
        is_agent_heartbeat_healthy(
            state_path=state_path,
            release_tag="expected",
            now=datetime(2026, 7, 22, 10, 11, 12, tzinfo=UTC),
        )
        is expected
    )


def test_health_probe_addr_requires_a_listening_port(tmp_path: Path) -> None:
    state_path = tmp_path / "heartbeat.json"
    now = datetime(2026, 7, 22, 10, 11, 12, tzinfo=UTC)
    state_path.write_text(
        json.dumps({"release_tag": "expected", "accepted_at": now.isoformat()}),
        encoding="utf-8",
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert is_agent_heartbeat_healthy(
            state_path=state_path,
            release_tag="expected",
            probe_addr=f"127.0.0.1:{port}",
            now=now,
        )
    assert not is_agent_heartbeat_healthy(
        state_path=state_path,
        release_tag="expected",
        probe_addr=f"127.0.0.1:{port}",
        now=now,
        timeout_s=0.2,
    )
    assert not is_agent_heartbeat_healthy(
        state_path=state_path,
        release_tag="expected",
        probe_addr="not-an-address",
        now=now,
    )


def test_check_health_cli_exit_code(tmp_path: Path) -> None:
    state_path = tmp_path / "heartbeat.json"
    state_path.write_text(
        json.dumps(
            {"release_tag": "expected", "accepted_at": datetime.now(UTC).isoformat()}
        ),
        encoding="utf-8",
    )

    args = ["--check-health", "--state-path", str(state_path)]
    assert main([*args, "--release-tag", "expected"]) == 0
    assert main([*args, "--release-tag", "other"]) == 1
    with pytest.raises(SystemExit):
        main(["--state-path", str(state_path)])
