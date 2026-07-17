from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import jwt
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from services.control_api.app.config import ControlSettings
from services.control_api.app.main import create_app
from services.control_api.app.routes import session as session_routes
from services.control_api.app.security import mint_participant_token


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_session_routes() -> None:
    session_routes.reset_session_state()


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    offline: bool,
) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-test-a")
    monkeypatch.setenv("READINESS_GATE_TTL_S", "86400")
    monkeypatch.setenv("OFFLINE_MOCK", "true" if offline else "false")
    if offline:
        monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
        monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
        monkeypatch.setenv("LIVEKIT_API_KEY", "test-key")
        monkeypatch.setenv("LIVEKIT_API_SECRET", "test-livekit-material-long-enough")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")


async def _anonymous_identity(client: AsyncClient) -> tuple[str, dict[str, str]]:
    response = await client.post("/v1/auth/anonymous")
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"].startswith("anon-")
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    return str(body["user_id"]), headers


@pytest.mark.asyncio
async def test_health_live() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_anonymous_token_uses_server_generated_subject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=True)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        current = await client.get("/v1/auth/me", headers=headers)
    assert current.status_code == 200
    assert current.json() == {"user_id": user_id}


@pytest.mark.asyncio
async def test_create_session_and_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=True)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        response = await client.post(
            "/v1/sessions",
            headers=headers,
            json={
                "user_id": user_id,
                "locale": "zh-CN",
                "client": {"platform": "web", "timezone": "Asia/Shanghai"},
            },
        )
        assert response.status_code == 200
        data = response.json()
        claims = jwt.decode(
            data["participant_token"],
            options={"verify_signature": False},
            algorithms=["HS256"],
        )
        assert claims["roomConfig"]["agents"] == [{"agentName": "duplex-zh-agent"}]
        assert data["room_name"].startswith("voice-")
        assert data["expires_in"] == 300
        assert data["agent_name"]
        assert data["voice_backend"] == "cascade"

        stop = await client.post(
            f"/v1/sessions/{data['session_id']}/stop-response",
            headers=headers,
            json={"reason": "user_button"},
        )
    assert stop.status_code == 200
    assert stop.json()["action"] == "atomic_cancel"
    assert stop.json()["create_user_turn"] is False


@pytest.mark.asyncio
async def test_create_omni_session_requires_only_dashscope_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=False)
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "")
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        response = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"user_id": user_id, "voice_backend": "qwen_omni"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data == {
        "session_id": data["session_id"],
        "voice_backend": "qwen_omni",
        "sdp_exchange_path": f"/v1/sessions/{data['session_id']}/omni/sdp",
        "config": {
            "model": "qwen3.5-omni-flash-realtime",
            "voice": "Tina",
            "turn_detection": {
                "type": "semantic_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 500,
                "silence_duration_ms": 800,
            },
        },
    }


@pytest.mark.asyncio
async def test_omni_owner_can_exchange_sdp_without_receiving_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=False)
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "llm-test-workspace")
    exchanged: dict[str, object] = {}

    async def fake_exchange(settings: object, offer_sdp: bytes) -> bytes:
        exchanged["settings"] = settings
        exchanged["offer_sdp"] = offer_sdp
        return b"v=0\r\no=qwen-answer\r\n"

    monkeypatch.setattr(session_routes, "_exchange_omni_sdp", fake_exchange, raising=False)
    app = create_app()
    offer = b"v=0\r\no=browser-offer\r\n"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        created = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": user_id, "voice_backend": "qwen_omni"},
            )
        ).json()
        response = await client.post(
            created["sdp_exchange_path"],
            headers={**headers, "Content-Type": "application/sdp"},
            content=offer,
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/sdp")
    assert response.headers["cache-control"] == "no-store"
    assert response.content == b"v=0\r\no=qwen-answer\r\n"
    assert exchanged["offer_sdp"] == offer
    assert "test-dashscope-key" not in response.text


@pytest.mark.asyncio
async def test_omni_owner_can_publish_only_allowlisted_numeric_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure(monkeypatch, tmp_path, offline=False)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        created = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": user_id, "voice_backend": "qwen_omni"},
            )
        ).json()
        caplog.set_level("INFO", logger="uvicorn.error")
        accepted = await client.post(
            f"/v1/sessions/{created['session_id']}/telemetry",
            headers=headers,
            json={
                "name": "webrtc_inbound_audio",
                "elapsed_ms": 5000,
                "turn_id": 1,
                "generation_id": 1,
                "metrics": {
                    "jitter": 0.004,
                    "packets_lost": 2,
                    "packets_received": 100,
                    "bytes_received": 12000,
                    "concealed_samples": 480,
                    "silent_concealed_samples": 240,
                    "total_samples_received": 48000,
                    "concealment_events": 1,
                    "concealment_ratio": 0.01,
                    "non_silent_concealment_ratio": 0.005,
                    "jitter_buffer_delay": 0.12,
                    "jitter_buffer_emitted_count": 4800,
                    "average_jitter_buffer_delay_ms": 0.025,
                    "encoded_audio_bitrate_kbps": 96.0,
                },
            },
        )
        rejected = await client.post(
            f"/v1/sessions/{created['session_id']}/telemetry",
            headers=headers,
            json={
                "name": "webrtc_inbound_audio",
                "elapsed_ms": 5001,
                "turn_id": 1,
                "generation_id": 1,
                "metrics": {"transcript": "不得上传文本"},
            },
        )

    assert accepted.status_code == 204
    assert rejected.status_code == 422
    assert "不得上传文本" not in caplog.text


@pytest.mark.asyncio
async def test_omni_creation_fails_closed_without_server_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=True)
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "llm-test-workspace")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        missing_key = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"user_id": user_id, "voice_backend": "qwen_omni"},
        )
        invalid_backend = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"user_id": user_id, "voice_backend": "unknown"},
        )
    assert missing_key.status_code == 503
    assert invalid_backend.status_code == 422


@pytest.mark.asyncio
async def test_omni_sdp_endpoint_rejects_wrong_owner_mode_media_and_size(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=False)
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "llm-test-workspace")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first_user, first_headers = await _anonymous_identity(client)
        _, second_headers = await _anonymous_identity(client)
        omni = (
            await client.post(
                "/v1/sessions",
                headers=first_headers,
                json={"user_id": first_user, "voice_backend": "qwen_omni"},
            )
        ).json()
        cascade = (await client.post("/v1/sessions", headers=first_headers, json={})).json()
        wrong_owner = await client.post(
            omni["sdp_exchange_path"],
            headers={**second_headers, "Content-Type": "application/sdp"},
            content=b"v=0\r\n",
        )
        wrong_mode = await client.post(
            f"/v1/sessions/{cascade['session_id']}/omni/sdp",
            headers={**first_headers, "Content-Type": "application/sdp"},
            content=b"v=0\r\n",
        )
        wrong_media = await client.post(
            omni["sdp_exchange_path"],
            headers={**first_headers, "Content-Type": "application/json"},
            content=b"v=0\r\n",
        )
        invalid_sdp = await client.post(
            omni["sdp_exchange_path"],
            headers={**first_headers, "Content-Type": "application/sdp"},
            content=b"not-sdp",
        )
        too_large = await client.post(
            omni["sdp_exchange_path"],
            headers={**first_headers, "Content-Type": "application/sdp"},
            content=b"v=0\r\n" + b"a" * (64 * 1024),
        )
    assert wrong_owner.status_code == 404
    assert wrong_mode.status_code == 409
    assert wrong_media.status_code == 415
    assert invalid_sdp.status_code == 422
    assert too_large.status_code == 413


@pytest.mark.asyncio
async def test_omni_sdp_exchange_is_limited_per_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=False)
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "llm-test-workspace")

    async def fake_exchange(settings: object, offer_sdp: bytes) -> bytes:
        _ = settings, offer_sdp
        return b"v=0\r\no=qwen-answer\r\n"

    monkeypatch.setattr(session_routes, "_exchange_omni_sdp", fake_exchange)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        created = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": user_id, "voice_backend": "qwen_omni"},
            )
        ).json()
        first_responses = [
            await client.post(
                created["sdp_exchange_path"],
                headers={**headers, "Content-Type": "application/sdp"},
                content=b"v=0\r\no=browser-offer\r\n",
            )
            for _ in range(2)
        ]
    restarted_app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=restarted_app), base_url="http://test"
    ) as client:
        after_restart = await client.post(
            created["sdp_exchange_path"],
            headers={**headers, "Content-Type": "application/sdp"},
            content=b"v=0\r\no=browser-offer\r\n",
        )
    assert [response.status_code for response in first_responses] == [200, 200]
    assert after_restart.status_code == 429


@pytest.mark.asyncio
async def test_omni_upstream_uses_fixed_url_without_redirects_or_client_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=False)
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "")
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client"] = kwargs

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> object:
            captured.update(url=url, request=kwargs)
            return session_routes.httpx.Response(200, content=b"v=0\r\no=qwen-answer\r\n")

    monkeypatch.setattr(session_routes.httpx, "AsyncClient", FakeAsyncClient)
    answer = await session_routes._exchange_omni_sdp(
        ControlSettings(),
        b"v=0\r\no=browser-offer\r\n",
    )

    assert answer.startswith(b"v=0")
    assert captured["url"] == (
        "https://llm-qp8mf178biax7m6c.cn-beijing.maas.aliyuncs.com"
        "/api/v1/webrtc/realtime?model=qwen3.5-omni-flash-realtime"
    )
    assert captured["client"] == {
        "timeout": 10.0,
        "follow_redirects": False,
        "trust_env": False,
    }
    request = cast(dict[str, Any], captured["request"])
    assert request["headers"] == {
        "Authorization": "Bearer test-dashscope-key",
        "Content-Type": "application/sdp",
    }
    assert request["content"] == b"v=0\r\no=browser-offer\r\n"


@pytest.mark.asyncio
async def test_omni_upstream_error_body_is_not_exposed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=False)
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "llm-test-workspace")

    class FailedAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            _ = kwargs

        async def __aenter__(self) -> FailedAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> object:
            _ = url, kwargs
            return session_routes.httpx.Response(
                400,
                content=b"provider-internal-secret-detail",
            )

    monkeypatch.setattr(session_routes.httpx, "AsyncClient", FailedAsyncClient)
    with pytest.raises(HTTPException) as caught:
        await session_routes._exchange_omni_sdp(
            ControlSettings(),
            b"v=0\r\no=browser-offer\r\n",
        )

    assert caught.value.status_code == 502
    assert caught.value.detail == "Qwen3.5-Omni 建连失败"
    assert "provider-internal-secret-detail" not in str(caught.value.detail)


@pytest.mark.asyncio
async def test_session_routes_require_bearer_and_enforce_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=True)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.post("/v1/sessions", json={})
        first_user, first_headers = await _anonymous_identity(client)
        second_user, second_headers = await _anonymous_identity(client)
        mismatch = await client.post(
            "/v1/sessions",
            headers=first_headers,
            json={"user_id": second_user},
        )
        created = await client.post("/v1/sessions", headers=first_headers, json={})
        cross_user = await client.post(
            f"/v1/sessions/{created.json()['session_id']}/stop-response",
            headers=second_headers,
            json={},
        )
    assert missing.status_code == 401
    assert mismatch.status_code == 403
    assert created.status_code == 200
    assert cross_user.status_code == 404
    assert first_user != second_user


@pytest.mark.asyncio
async def test_session_control_survives_control_api_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=True)
    first_app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=first_app), base_url="http://test"
    ) as client:
        _, headers = await _anonymous_identity(client)
        created = (await client.post("/v1/sessions", headers=headers, json={})).json()

    second_app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=second_app), base_url="http://test"
    ) as client:
        stop = await client.post(
            f"/v1/sessions/{created['session_id']}/stop-response",
            headers=headers,
            json={},
        )
        recovered = await client.post(
            f"/v1/sessions/{created['session_id']}/rtc-recovered",
            headers=headers,
        )
    assert stop.status_code == 200
    assert recovered.status_code == 200
    assert recovered.json()["action"] == "advance_generation"


@pytest.mark.asyncio
async def test_stop_response_routes_reliable_server_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=False)
    routed: dict[str, object] = {}

    async def fake_send(
        settings: object,
        *,
        room_name: str,
        event: dict[str, object],
    ) -> None:
        _ = settings
        routed.update(room_name=room_name, event=event)

    monkeypatch.setattr(session_routes, "_send_room_control", fake_send)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, headers = await _anonymous_identity(client)
        created = (await client.post("/v1/sessions", headers=headers, json={})).json()
        response = await client.post(
            f"/v1/sessions/{created['session_id']}/stop-response",
            headers=headers,
            json={"reason": "user_button"},
        )

    assert response.status_code == 200
    assert routed["room_name"] == created["room_name"]
    assert routed["event"] == {
        "type": "stop_response",
        "session_id": created["session_id"],
        "reason": "user_button",
        "action": "atomic_cancel",
        "create_user_turn": False,
    }


@pytest.mark.asyncio
async def test_rtc_recovery_routes_generation_advance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=False)
    routed: dict[str, object] = {}

    async def fake_send(
        settings: object,
        *,
        room_name: str,
        event: dict[str, object],
    ) -> None:
        _ = settings
        routed.update(room_name=room_name, event=event)

    monkeypatch.setattr(session_routes, "_send_room_control", fake_send)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, headers = await _anonymous_identity(client)
        created = (await client.post("/v1/sessions", headers=headers, json={})).json()
        response = await client.post(
            f"/v1/sessions/{created['session_id']}/rtc-recovered",
            headers=headers,
        )

    assert response.status_code == 200
    assert routed == {
        "room_name": created["room_name"],
        "event": {
            "type": "rtc_recovered",
            "session_id": created["session_id"],
            "action": "advance_generation",
            "create_user_turn": False,
        },
    }


@pytest.mark.asyncio
async def test_readiness_requires_fresh_authenticated_smokes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=False)
    app = create_app()
    mark_body: dict[str, Any] = {
        "livekit": True,
        "funasr": True,
        "llm": True,
        "llm_provider": "qwen",
        "release_tag": "release-test-a",
        "cosyvoice": True,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        before = await client.get("/health/ready")
        rejected = await client.post(
            "/internal/readiness/smokes",
            headers={"Authorization": "Bearer wrong"},
            json=mark_body,
        )
        wrong_release = await client.post(
            "/internal/readiness/smokes",
            headers={"Authorization": "Bearer test-auth-material-that-is-long-enough"},
            json={**mark_body, "release_tag": "release-test-b"},
        )
        marked = await client.post(
            "/internal/readiness/smokes",
            headers={"Authorization": "Bearer test-auth-material-that-is-long-enough"},
            json=mark_body,
        )
        ready = await client.get("/health/ready")

    assert before.status_code == 503
    assert before.json()["smokes"] == "not_run"
    assert rejected.status_code == 401
    assert wrong_release.status_code == 409
    assert marked.status_code == 200
    assert ready.status_code == 200
    assert ready.json()["checks"]["llm"] == {"provider": "qwen", "passed": True}


@pytest.mark.asyncio
async def test_readiness_evidence_survives_restart_and_is_release_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=False)
    mark_body = {
        "livekit": True,
        "funasr": True,
        "llm": True,
        "llm_provider": "qwen",
        "release_tag": "release-test-a",
        "cosyvoice": True,
    }
    first_app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=first_app), base_url="http://test"
    ) as client:
        marked = await client.post(
            "/internal/readiness/smokes",
            headers={"Authorization": "Bearer test-auth-material-that-is-long-enough"},
            json=mark_body,
        )
    assert marked.status_code == 200

    same_release_app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=same_release_app), base_url="http://test"
    ) as client:
        same_release = await client.get("/health/ready")
    assert same_release.status_code == 200

    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-test-b")
    new_release_app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=new_release_app), base_url="http://test"
    ) as client:
        new_release = await client.get("/health/ready")
    assert new_release.status_code == 503
    assert new_release.json()["smokes"] == "not_run"


@pytest.mark.asyncio
async def test_readiness_evidence_expires_after_24_hours(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=False)
    app = create_app()
    old_mark = datetime.now(UTC) - timedelta(seconds=86_401)
    app.state.memory_store.mark_readiness(
        release_tag="release-test-a",
        llm_provider="qwen",
        marked_at=old_mark.isoformat().replace("+00:00", "Z"),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        expired = await client.get("/health/ready")
    assert app.state.settings.readiness_gate_ttl_s == 86_400
    assert expired.status_code == 503
    assert expired.json()["smokes"] == "expired"


@pytest.mark.asyncio
async def test_ready_offline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=True)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_token_mint_fails_closed_without_credentials() -> None:
    settings = ControlSettings(
        OFFLINE_MOCK=False,
        LIVEKIT_API_KEY="",
        LIVEKIT_API_SECRET="",
    )
    with pytest.raises(RuntimeError, match="credentials are required"):
        mint_participant_token(
            settings,
            room_name="voice-session",
            identity="user-session",
        )


def test_production_config_requires_secure_livekit() -> None:
    settings = ControlSettings(
        ENVIRONMENT="production",
        PUBLIC_BASE_URL="https://voice.example.com",
        ALLOWED_ORIGINS="https://voice.example.com",
        LIVEKIT_URL="ws://livekit.example.com",
        LIVEKIT_API_KEY="key",
        LIVEKIT_API_SECRET="test-livekit-material-long-enough",
    )
    with pytest.raises(ValueError, match="secure LIVEKIT_URL"):
        settings.validate_production()


def test_production_config_requires_immutable_release_tag() -> None:
    settings = ControlSettings(
        ENVIRONMENT="production",
        PUBLIC_BASE_URL="https://voice.example.com",
        ALLOWED_ORIGINS="https://voice.example.com",
        LIVEKIT_URL="wss://livekit.example.com",
        LIVEKIT_API_KEY="key",
        LIVEKIT_API_SECRET="test-livekit-material-long-enough",
        MEMORIA_AUTH_SECRET="test-auth-material-that-is-long-enough",
        MEMORIA_RELEASE_TAG="latest",
    )
    with pytest.raises(ValueError, match="immutable MEMORIA_RELEASE_TAG"):
        settings.validate_production()


@pytest.mark.asyncio
async def test_production_disables_api_documentation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=True)
    monkeypatch.setenv("ENVIRONMENT", "production")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        docs = await client.get("/docs")
        redoc = await client.get("/redoc")
        openapi = await client.get("/openapi.json")
    assert (docs.status_code, redoc.status_code, openapi.status_code) == (404, 404, 404)


@pytest.mark.asyncio
async def test_development_keeps_api_documentation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, offline=True)
    monkeypatch.setenv("ENVIRONMENT", "development")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        docs = await client.get("/docs")
        redoc = await client.get("/redoc")
        openapi = await client.get("/openapi.json")
    assert (docs.status_code, redoc.status_code, openapi.status_code) == (200, 200, 200)
