from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from services.agent.src.voice_core.device_security import provision_device
from services.control_api.app.main import create_app
from services.control_api.app.session_directory import SessionRoute


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)


async def _anonymous(client: AsyncClient) -> tuple[str, dict[str, str]]:
    anonymous = await client.post("/v1/auth/anonymous")
    assert anonymous.status_code == 200
    body = anonymous.json()
    return str(body["user_id"]), {"Authorization": f"Bearer {body['access_token']}"}


async def _streamcore_session(
    client: AsyncClient, app: FastAPI, user_id: str, headers: dict[str, str]
) -> str:
    """A voice session on a StreamCore route (the one stop-dispatch runtime).

    Control no longer issues StreamCore media itself; the route is leased
    directly so the retained Media Edge stop dispatch stays covered.
    """

    created = await client.post("/v1/sessions", headers=headers, json={})
    assert created.status_code == 200, created.text
    session_id = str(created.json()["session_id"])
    settings = app.state.settings
    await app.state.session_directory.claim(
        session_id,
        media_edge_id=settings.media_edge_id,
        voice_core_id=settings.voice_core_id,
        device_id="h5",
        account_id=user_id,
        stream_epoch=1,
        generation=0,
        media_runtime="streamcore",
        owner_instance_id=settings.media_edge_id,
    )
    return session_id


@pytest.mark.asyncio
async def test_stop_refuses_a_session_without_a_streamcore_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, headers = await _anonymous(client)
        created = await client.post("/v1/sessions", headers=headers, json={})
        session_id = created.json()["session_id"]
        stop = await client.post(
            f"/v1/media/sessions/{session_id}/stop",
            headers={**headers, "Idempotency-Key": "media-stop-1"},
        )
        response_stop = await client.post(
            f"/v1/sessions/{session_id}/stop-response",
            headers=headers,
            json={"reason": "user_button"},
        )
    assert created.status_code == 200
    # No LiveKit room exists to message and a direct device stops in-band.
    assert stop.status_code == response_stop.status_code == 409
    assert stop.json()["detail"] == {"code": "stop_response_runtime_unsupported"}


@pytest.mark.asyncio
async def test_retired_media_session_routes_are_gone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, headers = await _anonymous(client)
        session_id = (
            await client.post("/v1/sessions", headers=headers, json={})
        ).json()["session_id"]
        h5 = await client.post("/v1/media/sessions", headers=headers, json={})
        legacy_device = await client.post(
            "/v1/devices/doll-1/media-session",
            headers=headers,
            json={"client_type": "device"},
        )
        retired = [
            await client.post(f"/v1/sessions/{session_id}/{path}", headers=headers, json={})
            for path in (
                "mini-program/gateway-ticket",
                "rtc-recovered",
                "media-reconnect",
                "media-fallback",
            )
        ]
    assert h5.status_code in {404, 405}
    assert legacy_device.status_code in {404, 405}
    assert [response.status_code for response in retired] == [404, 404, 404, 404]


@pytest.mark.asyncio
async def test_device_challenge_endpoint_bounds_pending_bootstrap_nonces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    provisioned = provision_device("doll-rate-limit")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.post("/v1/auth/anonymous")
        headers = {"Authorization": f"Bearer {anonymous.json()['access_token']}"}
        registered = await client.post(
            "/v1/devices/doll-rate-limit/identity",
            headers=headers,
            json={"public_key": provisioned.identity.public_key_b64},
        )
        assert registered.status_code == 200
        for _ in range(3):
            challenge = await client.post("/v1/devices/doll-rate-limit/challenge")
            assert challenge.status_code == 200
        limited = await client.post("/v1/devices/doll-rate-limit/challenge")
    assert limited.status_code == 429


@pytest.mark.asyncio
async def test_streamcore_heartbeat_rotates_token_and_offline_stop_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("STREAMCORE_TOKEN_SECRET", "streamcore-token-secret-long-enough")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous(client)
        session_id = await _streamcore_session(client, app, user_id, headers)
        heartbeat = await client.post(
            f"/v1/sessions/{session_id}/media-heartbeat",
            headers=headers,
            json={"stream_epoch": 1},
        )
        stop = await client.post(
            f"/v1/sessions/{session_id}/stop-response",
            headers={**headers, "Idempotency-Key": "streamcore-stop-1"},
            json={"reason": "user_button"},
        )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["media_runtime"] == "streamcore"
    assert heartbeat.json()["stream_epoch"] == 1
    assert heartbeat.json()["token"]
    assert heartbeat.json()["token_expires_at"]
    assert stop.status_code == 200
    assert stop.json()["media_runtime"] == "streamcore"
    assert stop.json()["generation_id"] == 1
    assert stop.json()["media_stop_dispatch"] == "pending"


@pytest.mark.asyncio
async def test_streamcore_stop_dispatches_cancel_to_current_media_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    dispatched: list[dict[str, object]] = []

    async def dispatch(
        *, session_id: str, route: object, event: dict[str, object]
    ) -> dict[str, object]:
        dispatched.append({"session_id": session_id, "route": route, "event": event})
        return {"stream_epoch": 1, "turn_id": 4, "generation_id": 7, "tool_epoch": 2}

    app.state.media_stop_dispatcher = dispatch
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous(client)
        session_id = await _streamcore_session(client, app, user_id, headers)
        stop = await client.post(
            f"/v1/sessions/{session_id}/stop-response",
            headers={**headers, "Idempotency-Key": "dispatch-stop-1"},
            json={"reason": "user_button"},
        )
        repeat = await client.post(
            f"/v1/sessions/{session_id}/stop-response",
            headers={**headers, "Idempotency-Key": "dispatch-stop-1"},
            json={"reason": "user_button"},
        )

    assert stop.status_code == repeat.status_code == 200
    assert stop.json() == repeat.json()
    assert len(dispatched) == 1
    assert dispatched[0]["session_id"] == session_id
    assert stop.json()["stream_epoch"] == 1
    assert stop.json()["turn_id"] == 4
    assert stop.json()["generation_id"] == 7
    assert stop.json()["tool_epoch"] == 2
    assert dispatched[0]["event"] == {
        "type": "stop_response",
        "session_id": session_id,
        "reason": "user_button",
        "action": "atomic_cancel",
        "create_user_turn": False,
        "media_runtime": "streamcore",
        "stream_epoch": 1,
        "idempotency_key": "dispatch-stop-1",
    }


@pytest.mark.asyncio
async def test_streamcore_stop_forwards_expected_fence_to_media_edge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    dispatched: list[dict[str, object]] = []

    async def dispatch(
        *, session_id: str, route: object, event: dict[str, object]
    ) -> dict[str, object]:
        dispatched.append({"session_id": session_id, "route": route, "event": event})
        return {"stream_epoch": 1, "turn_id": 4, "generation_id": 7, "tool_epoch": 2}

    app.state.media_stop_dispatcher = dispatch
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous(client)
        session_id = await _streamcore_session(client, app, user_id, headers)
        stop = await client.post(
            f"/v1/sessions/{session_id}/stop-response",
            headers={**headers, "Idempotency-Key": "fenced-stop-1"},
            json={
                "reason": "user_button",
                "stream_epoch": 1,
                "turn_id": 4,
                "generation_id": 7,
                "tool_epoch": 2,
            },
        )

    assert stop.status_code == 200
    assert dispatched[0]["event"]["turn_id"] == 4
    assert dispatched[0]["event"]["generation_id"] == 7
    assert dispatched[0]["event"]["tool_epoch"] == 2
    assert dispatched[0]["event"]["stream_epoch"] == 1


@pytest.mark.asyncio
async def test_streamcore_stop_rejects_stale_or_partial_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()

    async def dispatch(
        *, session_id: str, route: object, event: dict[str, object]
    ) -> dict[str, object]:
        return {"stream_epoch": 1, "turn_id": 4, "generation_id": 7, "tool_epoch": 2}

    app.state.media_stop_dispatcher = dispatch
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous(client)
        session_id = await _streamcore_session(client, app, user_id, headers)
        stale = await client.post(
            f"/v1/sessions/{session_id}/stop-response",
            headers={**headers, "Idempotency-Key": "stale-stop-1"},
            json={
                "reason": "user_button",
                "stream_epoch": 99,
                "turn_id": 4,
                "generation_id": 7,
                "tool_epoch": 2,
            },
        )
        partial = await client.post(
            f"/v1/sessions/{session_id}/stop-response",
            headers={**headers, "Idempotency-Key": "partial-stop-1"},
            json={"reason": "user_button", "stream_epoch": 1, "generation_id": 7},
        )

    assert stale.status_code == 409
    assert partial.status_code == 400


@pytest.mark.asyncio
async def test_streamcore_stop_retry_reuses_generation_after_edge_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    attempts: list[tuple[int, str]] = []

    async def dispatch(
        *, session_id: str, route: SessionRoute, event: dict[str, object]
    ) -> dict[str, object]:
        _ = session_id
        attempts.append((route.generation, str(event["idempotency_key"])))
        if len(attempts) == 1:
            raise RuntimeError("edge temporarily unavailable")
        return {"stream_epoch": 1, "turn_id": 2, "generation_id": 3, "tool_epoch": 0}

    app.state.media_stop_dispatcher = dispatch
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous(client)
        session_id = await _streamcore_session(client, app, user_id, headers)
        first = await client.post(
            f"/v1/sessions/{session_id}/stop-response",
            headers={**headers, "Idempotency-Key": "retry-stop-1"},
            json={"reason": "user_button"},
        )
        second = await client.post(
            f"/v1/sessions/{session_id}/stop-response",
            headers={**headers, "Idempotency-Key": "retry-stop-1"},
            json={"reason": "user_button"},
        )

    assert first.status_code == 502
    assert second.status_code == 200
    assert second.json()["generation_id"] == 3
    assert attempts == [(0, "retry-stop-1"), (0, "retry-stop-1")]


@pytest.mark.asyncio
async def test_media_slo_report_is_token_gated_and_evaluated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MEDIA_SLO_REPORT_TOKEN", "slo-report-token-long-enough")
    app = create_app()
    metrics = {
        "first_audio_p95_ms": 700,
        "interrupt_stop_p95_ms": 100,
        "session_failure_rate": 0.01,
        "stale_generation_total": 0,
        "stale_asr_final_total": 0,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        forged = await client.post(
            "/v1/internal/media-runtime/slo",
            headers={"X-Media-SLO-Token": "wrong-token"},
            json={"source": "test-agent", "metrics": metrics},
        )
        report = await client.post(
            "/v1/internal/media-runtime/slo",
            headers={"X-Media-SLO-Token": "slo-report-token-long-enough"},
            json={"source": "test-agent", "metrics": metrics},
        )
    assert forged.status_code == 401
    assert report.status_code == 200
    assert report.json()["rollback_required"] is False
