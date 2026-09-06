from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from services.control_api.app.config import ControlSettings
from services.control_api.app.main import create_app
from services.control_api.app.routes import readiness as readiness_routes


def _configure_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_SPEAKER_DB_PATH", str(tmp_path / "speakers.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_ARCHIVE_OBJECT_STORE_PATH",
        str(tmp_path / "archive-objects"),
    )
    monkeypatch.setenv(
        "MEMORIA_VOICE_SAMPLE_STORE_PATH",
        str(tmp_path / "voice-samples"),
    )


async def _ready(app: FastAPI) -> tuple[int, dict[str, object]]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")
    return response.status_code, response.json()


@pytest.mark.asyncio
async def test_speaker_model_probe_requires_ready_status_and_matching_model() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "model_version": "campplus-test-v1",
            },
        )

    settings = ControlSettings(
        MEMORIA_SPEAKER_EMBEDDING_URL=("http://speaker-model.test:8001/v1/embeddings/speaker"),
        MEMORIA_SPEAKER_EMBEDDING_TOKEN=SecretStr("speaker-model-token"),
        MEMORIA_SPEAKER_EMBEDDING_MODEL="campplus-test-v1",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await readiness_routes._probe_speaker_model(settings, client=client) == "ready"
    finally:
        await client.aclose()

    assert len(seen) == 1
    assert str(seen[0].url) == "http://speaker-model.test:8001/health/ready"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (503, {"detail": "not ready"}),
        (200, {"status": "ready", "model_version": "campplus-other-v1"}),
        (200, {"status": "loading", "model_version": "campplus-test-v1"}),
    ],
)
async def test_speaker_model_probe_fails_closed_for_down_or_drifted_model(
    status_code: int,
    payload: dict[str, object],
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    settings = ControlSettings(
        MEMORIA_SPEAKER_EMBEDDING_URL="http://speaker-model.test/v1/embeddings/speaker",
        MEMORIA_SPEAKER_EMBEDDING_TOKEN=SecretStr("speaker-model-token"),
        MEMORIA_SPEAKER_EMBEDDING_MODEL="campplus-test-v1",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await readiness_routes._probe_speaker_model(settings, client=client) == "unavailable"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_local_readiness_checks_core_services_without_active_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)

    status, body = await _ready(create_app())

    assert status == 200
    assert body["status"] == "ready"
    assert body["checks"]["core"] == {
        "control_database": "ready",
        "evolution_store": "ready",
        "memory_archive": "ready",
        "memory_catalog": "ready",
        "persona": "ready",
        "speaker_authority": "ready",
        "voice_profile": "ready",
        "archive_object_store": "ready",
        "voice_object_store": "ready",
        "session_runtime": "skipped",
        "memory_scope": "skipped",
        "speaker_model": "skipped",
    }


@pytest.mark.asyncio
async def test_readiness_fails_closed_when_a_runtime_component_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    app = create_app()
    app.state.persona_engine = None

    status, body = await _ready(app)

    assert status == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["core"]["persona"] == "unavailable"


@pytest.mark.asyncio
async def test_readiness_fails_closed_when_speaker_model_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "MEMORIA_SPEAKER_EMBEDDING_URL",
        "http://speaker-model.test/v1/embeddings/speaker",
    )
    monkeypatch.setenv("MEMORIA_SPEAKER_EMBEDDING_TOKEN", "speaker-model-token")
    app = create_app()

    async def unavailable(_settings: ControlSettings, **_kwargs: object) -> str:
        return "unavailable"

    monkeypatch.setattr(readiness_routes, "_probe_speaker_model", unavailable)
    status, body = await _ready(app)

    assert status == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["core"]["speaker_model"] == "unavailable"


class _UnavailableObjectStore:
    async def put(self, **_: object) -> object:
        raise RuntimeError("object service unavailable")


class _UnavailableControlDatabase:
    def get_readiness(self, **_: object) -> object:
        raise RuntimeError("database unavailable")


@pytest.mark.asyncio
async def test_readiness_fails_closed_when_object_storage_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    app = create_app()
    app.state.archive_object_store = _UnavailableObjectStore()

    status, body = await _ready(app)

    assert status == 503
    assert body["checks"]["core"]["archive_object_store"] == "unavailable"


@pytest.mark.asyncio
async def test_online_readiness_reports_unavailable_control_database_as_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    monkeypatch.setenv("OFFLINE_MOCK", "false")
    monkeypatch.setenv("LIVEKIT_API_KEY", "test-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "test-livekit-secret")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    app = create_app()
    app.state.memory_store = _UnavailableControlDatabase()

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["smokes"] == "unavailable"
    assert response.json()["checks"]["core"]["control_database"] == "unavailable"


@pytest.mark.asyncio
async def test_readiness_fails_closed_when_evolution_store_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    app = create_app()

    class UnavailableEvolutionStore:
        def healthcheck(self) -> None:
            raise RuntimeError("evolution store unavailable")

    app.state.evolution_store = UnavailableEvolutionStore()
    status, body = await _ready(app)

    assert status == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["core"]["evolution_store"] == "unavailable"


class _FailingSessionRuntimeStore:
    async def readiness(self) -> object:
        raise RuntimeError("schema/RLS unavailable")


@pytest.mark.asyncio
async def test_health_ready_exposes_session_runtime_readiness_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    app = create_app()
    app.state.session_runtime_store = _FailingSessionRuntimeStore()

    monkeypatch.setattr(readiness_routes, "_valid_configuration", lambda _: True)
    monkeypatch.setattr(readiness_routes, "_missing_config", lambda _: [])
    monkeypatch.setattr(readiness_routes, "_smoke_state", lambda *_: "passed")

    status, body = await _ready(app)

    assert status == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["core"]["session_runtime"] == "unavailable"


@pytest.mark.asyncio
async def test_readiness_rejects_invalid_production_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    app = create_app()
    app.state.settings = ControlSettings(
        _env_file=None,
        ENVIRONMENT="production",
        OFFLINE_MOCK=True,
        LIVEKIT_API_KEY="",
        LIVEKIT_API_SECRET="",
    )

    status, body = await _ready(app)

    assert status == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["config"] is False


@pytest.mark.asyncio
async def test_smoke_mark_rejects_invalid_control_production_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    app = create_app()
    app.state.settings = ControlSettings(
        _env_file=None,
        ENVIRONMENT="production",
        PUBLIC_BASE_URL="https://voice.example.com",
        ALLOWED_ORIGINS="https://voice.example.com",
        LIVEKIT_URL="wss://livekit.example.com",
        LIVEKIT_API_KEY="key",
        LIVEKIT_API_SECRET="livekit-secret-material-that-is-long-enough",
        DASHSCOPE_API_KEY="dashscope-key",
        MEMORIA_AUTH_SECRET="control-auth-material-that-is-long-enough",
        MEMORIA_RELEASE_TAG="release-readiness-test",
        LLM_PROVIDER="bailian_deepseek",
        OFFLINE_MOCK=False,
    )
    body = {
        "livekit": True,
        "funasr": True,
        "llm": True,
        "llm_provider": "bailian_deepseek",
        "release_tag": "release-readiness-test",
        "tts": {
            "provider": "doubao",
            "audio": True,
            "word_timestamps": True,
        },
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/internal/readiness/smokes",
            headers={"Authorization": "Bearer control-auth-material-that-is-long-enough"},
            json=body,
        )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_fresh_agent_heartbeat_opens_production_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    app = create_app()
    app.state.settings = ControlSettings(
        _env_file=None,
        ENVIRONMENT="production",
        MEMORIA_RELEASE_TAG="release-heartbeat-test",
        MEMORIA_AGENT_HEARTBEAT_TOKEN=SecretStr("agent-heartbeat-token"),
        OFFLINE_MOCK=False,
    )

    async def ready_core(*_: object) -> dict[str, str]:
        return {"control_database": "ready"}

    monkeypatch.setattr(readiness_routes, "_valid_configuration", lambda _: True)
    monkeypatch.setattr(readiness_routes, "_missing_config", lambda _: [])
    monkeypatch.setattr(readiness_routes, "_smoke_state", lambda *_: "passed")
    monkeypatch.setattr(readiness_routes, "_core_checks", ready_core)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        missing = await client.get("/health/ready")
        heartbeat = await client.post(
            "/internal/readiness/agent-heartbeat",
            headers={"X-Memoria-Internal-Token": "agent-heartbeat-token"},
            json={
                "release_tag": "release-heartbeat-test",
                "boot_id": "8f819a3b-ec8f-4319-94ab-7cace979145f",
                "worker_ready": True,
                "livekit_ready": True,
                "last_loop_at": datetime.now(UTC).isoformat(),
            },
        )
        ready = await client.get("/health/ready")

    assert missing.status_code == 503
    assert missing.json()["checks"]["agent"] == {"status": "missing"}
    assert heartbeat.status_code == 200
    assert heartbeat.json()["status"] == "recorded"
    assert ready.status_code == 200
    assert ready.json()["checks"]["agent"] == {
        "status": "ready",
        "release_tag": "release-heartbeat-test",
        "boot_id": "8f819a3b-ec8f-4319-94ab-7cace979145f",
        "worker_ready": True,
        "livekit_ready": True,
        "last_loop_at": heartbeat.json()["last_loop_at"],
    }


@pytest.mark.asyncio
async def test_stale_agent_heartbeat_closes_production_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    app = create_app()
    app.state.settings = ControlSettings(
        _env_file=None,
        ENVIRONMENT="production",
        MEMORIA_RELEASE_TAG="release-heartbeat-test",
        MEMORIA_AGENT_HEARTBEAT_TOKEN=SecretStr("agent-heartbeat-token"),
        OFFLINE_MOCK=False,
    )

    async def ready_core(*_: object) -> dict[str, str]:
        return {"control_database": "ready"}

    monkeypatch.setattr(readiness_routes, "_valid_configuration", lambda _: True)
    monkeypatch.setattr(readiness_routes, "_missing_config", lambda _: [])
    monkeypatch.setattr(readiness_routes, "_smoke_state", lambda *_: "passed")
    monkeypatch.setattr(readiness_routes, "_core_checks", ready_core)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        heartbeat = await client.post(
            "/internal/readiness/agent-heartbeat",
            headers={"X-Memoria-Internal-Token": "agent-heartbeat-token"},
            json={
                "release_tag": "release-heartbeat-test",
                "boot_id": "8f819a3b-ec8f-4319-94ab-7cace979145f",
                "worker_ready": True,
                "livekit_ready": True,
                "last_loop_at": datetime.fromtimestamp(
                    datetime.now(UTC).timestamp() - 46,
                    tz=UTC,
                ).isoformat(),
            },
        )
        stale = await client.get("/health/ready")

    assert heartbeat.status_code == 200
    assert stale.status_code == 503
    assert stale.json()["checks"]["agent"]["status"] == "stale"
