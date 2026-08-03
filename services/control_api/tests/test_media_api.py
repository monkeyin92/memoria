from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from services.agent.src.voice_core.device_security import provision_device, sign_challenge
from services.control_api.app.main import create_app
from services.control_api.app.session_directory import SessionRoute


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)


@pytest.mark.asyncio
async def test_media_session_projection_claims_directory_and_exposes_livekit_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.post("/v1/auth/anonymous")
        assert anonymous.status_code == 200
        headers = {"Authorization": f"Bearer {anonymous.json()['access_token']}"}
        response = await client.post(
            "/v1/media/sessions",
            headers=headers,
            json={"client_type": "h5", "capabilities": {"data_channel": True}},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["media_runtime"] == "livekit"
        assert body["fallback_runtime"] == "livekit"
        assert body["streamcore"] is None
        assert body["fallback"] == {"media_runtime": "livekit"}
        assert body["stream_epoch"] == 1
        route = await app.state.session_directory.lookup(body["session_id"])
        assert route is not None
        assert route.stream_epoch == 1
        assert route.device_id == "h5"
        stop = await client.post(
            f"/v1/media/sessions/{body['session_id']}/stop",
            headers={**headers, "Idempotency-Key": "media-stop-1"},
        )
        repeat = await client.post(
            f"/v1/media/sessions/{body['session_id']}/stop",
            headers={**headers, "Idempotency-Key": "media-stop-1"},
        )
    assert stop.status_code == repeat.status_code == 200
    assert stop.json() == repeat.json()


@pytest.mark.asyncio
async def test_device_media_session_keeps_livekit_until_linux_aec_adapter_is_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.post("/v1/auth/anonymous")
        headers = {"Authorization": f"Bearer {anonymous.json()['access_token']}"}
        response = await client.post(
            "/v1/devices/doll-1/media-session",
            headers=headers,
            json={"client_type": "device"},
        )
    assert response.status_code == 200
    assert response.json()["media_runtime"] == "livekit"
    assert response.json()["device_id"] == "doll-1"


@pytest.mark.asyncio
async def test_device_signed_challenge_binds_media_session_to_registered_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    provisioned = provision_device("doll-signed")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.post("/v1/auth/anonymous")
        headers = {"Authorization": f"Bearer {anonymous.json()['access_token']}"}
        registered = await client.post(
            "/v1/devices/doll-signed/identity",
            headers=headers,
            json={"public_key": provisioned.identity.public_key_b64},
        )
        assert registered.status_code == 200
        challenge_response = await client.post("/v1/devices/doll-signed/challenge")
        assert challenge_response.status_code == 200
        challenge = challenge_response.json()
        signed = sign_challenge(
            provisioned,
            nonce=challenge["nonce"],
            issued_at_ms=challenge["issued_at_ms"],
        )
        device_session = await client.post(
            "/v1/devices/doll-signed/media-session",
            json={
                "client_type": "device",
                "device_proof": {
                    "nonce": signed.nonce,
                    "issued_at_ms": signed.issued_at_ms,
                    "signature": signed.signature_b64,
                },
            },
        )
        replay = await client.post(
            "/v1/devices/doll-signed/media-session",
            json={
                "client_type": "device",
                "device_proof": {
                    "nonce": signed.nonce,
                    "issued_at_ms": signed.issued_at_ms,
                    "signature": signed.signature_b64,
                },
            },
        )
    assert device_session.status_code == 200
    assert device_session.json()["device_id"] == "doll-signed"
    assert replay.status_code == 401


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
async def test_streamcore_reconnect_gets_authoritative_epoch_and_rotated_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MEDIA_RUNTIME_DEFAULT", "streamcore")
    monkeypatch.setenv("STREAMCORE_EXPERIMENT_PERCENT", "100")
    monkeypatch.setenv("STREAMCORE_WHIP_URL", "http://localhost:7000/whip")
    monkeypatch.setenv("STREAMCORE_TOKEN_SECRET", "streamcore-token-secret-long-enough")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.post("/v1/auth/anonymous")
        headers = {"Authorization": f"Bearer {anonymous.json()['access_token']}"}
        created = await client.post("/v1/media/sessions", headers=headers, json={})
        assert created.status_code == 200
        assert created.json()["media_runtime"] == "streamcore"
        reconnect = await client.post(
            f"/v1/sessions/{created.json()['session_id']}/media-reconnect",
            headers=headers,
        )
        heartbeat = await client.post(
            f"/v1/sessions/{created.json()['session_id']}/media-heartbeat",
            headers=headers,
            json={"stream_epoch": 2},
        )
        stop = await client.post(
            f"/v1/sessions/{created.json()['session_id']}/stop-response",
            headers={**headers, "Idempotency-Key": "streamcore-stop-1"},
            json={"reason": "user_button"},
        )
    assert reconnect.status_code == 200
    body = reconnect.json()
    assert body["stream_epoch"] == 2
    assert body["streamcore"]["stream_epoch"] == 2
    assert body["streamcore"]["token"] != created.json()["streamcore"]["token"]
    assert heartbeat.status_code == 200
    assert heartbeat.json()["stream_epoch"] == 2
    assert stop.status_code == 200
    assert stop.json()["media_runtime"] == "streamcore"
    assert stop.json()["generation_id"] == 1


@pytest.mark.asyncio
async def test_streamcore_stop_dispatches_cancel_to_current_media_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MEDIA_RUNTIME_DEFAULT", "streamcore")
    monkeypatch.setenv("STREAMCORE_EXPERIMENT_PERCENT", "100")
    monkeypatch.setenv("STREAMCORE_WHIP_URL", "http://localhost:7000/whip")
    monkeypatch.setenv("STREAMCORE_TOKEN_SECRET", "streamcore-token-secret-long-enough")
    app = create_app()
    dispatched: list[dict[str, object]] = []

    async def dispatch(
        *, session_id: str, route: object, event: dict[str, object]
    ) -> dict[str, object]:
        dispatched.append({"session_id": session_id, "route": route, "event": event})
        return {"stream_epoch": 1, "turn_id": 4, "generation_id": 7, "tool_epoch": 2}

    app.state.media_stop_dispatcher = dispatch
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.post("/v1/auth/anonymous")
        headers = {"Authorization": f"Bearer {anonymous.json()['access_token']}"}
        created = await client.post("/v1/media/sessions", headers=headers, json={})
        session_id = created.json()["session_id"]
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
    monkeypatch.setenv("MEDIA_RUNTIME_DEFAULT", "streamcore")
    monkeypatch.setenv("STREAMCORE_EXPERIMENT_PERCENT", "100")
    monkeypatch.setenv("STREAMCORE_WHIP_URL", "http://localhost:7000/whip")
    monkeypatch.setenv("STREAMCORE_TOKEN_SECRET", "streamcore-token-secret-long-enough")
    app = create_app()
    dispatched: list[dict[str, object]] = []

    async def dispatch(
        *, session_id: str, route: object, event: dict[str, object]
    ) -> dict[str, object]:
        dispatched.append({"session_id": session_id, "route": route, "event": event})
        return {"stream_epoch": 1, "turn_id": 4, "generation_id": 7, "tool_epoch": 2}

    app.state.media_stop_dispatcher = dispatch
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.post("/v1/auth/anonymous")
        headers = {"Authorization": f"Bearer {anonymous.json()['access_token']}"}
        created = await client.post("/v1/media/sessions", headers=headers, json={})
        session_id = created.json()["session_id"]
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
    monkeypatch.setenv("MEDIA_RUNTIME_DEFAULT", "streamcore")
    monkeypatch.setenv("STREAMCORE_EXPERIMENT_PERCENT", "100")
    monkeypatch.setenv("STREAMCORE_WHIP_URL", "http://localhost:7000/whip")
    monkeypatch.setenv("STREAMCORE_TOKEN_SECRET", "streamcore-token-secret-long-enough")
    app = create_app()

    async def dispatch(
        *, session_id: str, route: object, event: dict[str, object]
    ) -> dict[str, object]:
        return {"stream_epoch": 1, "turn_id": 4, "generation_id": 7, "tool_epoch": 2}

    app.state.media_stop_dispatcher = dispatch
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.post("/v1/auth/anonymous")
        headers = {"Authorization": f"Bearer {anonymous.json()['access_token']}"}
        created = await client.post("/v1/media/sessions", headers=headers, json={})
        session_id = created.json()["session_id"]
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
    monkeypatch.setenv("MEDIA_RUNTIME_DEFAULT", "streamcore")
    monkeypatch.setenv("STREAMCORE_EXPERIMENT_PERCENT", "100")
    monkeypatch.setenv("STREAMCORE_WHIP_URL", "http://localhost:7000/whip")
    monkeypatch.setenv("STREAMCORE_TOKEN_SECRET", "streamcore-token-secret-long-enough")
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
        anonymous = await client.post("/v1/auth/anonymous")
        headers = {"Authorization": f"Bearer {anonymous.json()['access_token']}"}
        created = await client.post("/v1/media/sessions", headers=headers, json={})
        session_id = created.json()["session_id"]
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
async def test_streamcore_fallback_transitions_route_before_livekit_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MEDIA_RUNTIME_DEFAULT", "streamcore")
    monkeypatch.setenv("STREAMCORE_EXPERIMENT_PERCENT", "100")
    monkeypatch.setenv("STREAMCORE_WHIP_URL", "http://localhost:7000/whip")
    monkeypatch.setenv("STREAMCORE_TOKEN_SECRET", "streamcore-token-secret-long-enough")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.post("/v1/auth/anonymous")
        headers = {"Authorization": f"Bearer {anonymous.json()['access_token']}"}
        created = await client.post("/v1/media/sessions", headers=headers, json={})
        session_id = created.json()["session_id"]
        fallback = await client.post(
            f"/v1/sessions/{session_id}/media-fallback",
            headers=headers,
            json={"stream_epoch": 1},
        )
        stop = await client.post(
            f"/v1/sessions/{session_id}/stop-response",
            headers={**headers, "Idempotency-Key": "fallback-stop-1"},
            json={"reason": "user_button"},
        )
    assert fallback.status_code == 200
    assert fallback.json()["media_runtime"] == "livekit"
    assert stop.status_code == 200
    assert "media_runtime" not in stop.json()


@pytest.mark.asyncio
async def test_streamcore_rollout_requires_a_fresh_slo_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("MEDIA_RUNTIME_DEFAULT", "streamcore")
    monkeypatch.setenv("STREAMCORE_EXPERIMENT_PERCENT", "100")
    monkeypatch.setenv("STREAMCORE_WHIP_URL", "http://localhost:7000/whip")
    monkeypatch.setenv("STREAMCORE_TOKEN_SECRET", "streamcore-token-secret-long-enough")
    monkeypatch.setenv("STREAMCORE_SLO_GATE_ENABLED", "true")
    monkeypatch.setenv("MEDIA_SLO_REPORT_TOKEN", "slo-report-token-long-enough")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.post("/v1/auth/anonymous")
        headers = {"Authorization": f"Bearer {anonymous.json()['access_token']}"}
        before = await client.post("/v1/media/sessions", headers=headers, json={})
        report = await client.post(
            "/v1/internal/media-runtime/slo",
            headers={"X-Media-SLO-Token": "slo-report-token-long-enough"},
            json={
                "source": "test-agent",
                "metrics": {
                    "first_audio_p95_ms": 700,
                    "interrupt_stop_p95_ms": 100,
                    "session_failure_rate": 0.01,
                    "stale_generation_total": 0,
                    "stale_asr_final_total": 0,
                },
            },
        )
        after = await client.post("/v1/media/sessions", headers=headers, json={})
    assert before.status_code == after.status_code == 200
    assert before.json()["media_runtime"] == "livekit"
    assert report.status_code == 200
    assert report.json()["rollback_required"] is False
    assert after.json()["media_runtime"] == "streamcore"
