from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from services.archive.object_store import EncryptedS3ObjectStore
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
async def test_local_readiness_checks_core_services_without_active_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)

    status, body = await _ready(create_app())

    assert status == 200
    assert body["status"] == "ready"
    assert list((tmp_path / "archive-objects").rglob("*")) == []
    assert list((tmp_path / "voice-samples").rglob("*")) == []
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


class _UnavailableObjectStore:
    async def probe(self) -> None:
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


class _RecordingS3:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def head_bucket(self, **kwargs: object) -> None:
        self.calls.append(("head_bucket", str(kwargs["Bucket"])))
        if self.fail:
            raise RuntimeError("bucket missing")

    def put_object(self, **kwargs: object) -> None:
        self.calls.append(("put_object", str(kwargs.get("Bucket", ""))))

    def delete_object(self, **kwargs: object) -> None:
        self.calls.append(("delete_object", str(kwargs.get("Bucket", ""))))

    def delete_objects(self, **kwargs: object) -> None:
        self.calls.append(("delete_objects", str(kwargs.get("Bucket", ""))))

    def get_object(self, **kwargs: object) -> None:
        self.calls.append(("get_object", str(kwargs.get("Bucket", ""))))

    def list_object_versions(self, **kwargs: object) -> None:
        self.calls.append(("list_object_versions", str(kwargs.get("Bucket", ""))))


def _s3_store(client: _RecordingS3, bucket: str) -> EncryptedS3ObjectStore:
    return EncryptedS3ObjectStore(
        client=client,
        bucket=bucket,
        key=Fernet.generate_key().decode("ascii"),
        key_version="v1",
    )


@pytest.mark.asyncio
async def test_readiness_heads_real_buckets_and_does_not_write_or_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    app = create_app()
    archive = _RecordingS3()
    voice = _RecordingS3()
    app.state.archive_object_store = _s3_store(archive, "memoria-archive")
    app.state.voice_object_store = _s3_store(voice, "memoria-voice")

    status, body = await _ready(app)

    assert status == 200
    assert body["checks"]["core"]["archive_object_store"] == "ready"
    assert body["checks"]["core"]["voice_object_store"] == "ready"
    assert archive.calls == [("head_bucket", "memoria-archive")]
    assert voice.calls == [("head_bucket", "memoria-voice")]

    missing = _RecordingS3(fail=True)
    app.state.archive_object_store = _s3_store(missing, "memoria-archive")
    status, body = await _ready(app)

    assert status == 503
    assert body["checks"]["core"]["archive_object_store"] == "unavailable"
    assert missing.calls == [("head_bucket", "memoria-archive")]
    assert voice.calls == [
        ("head_bucket", "memoria-voice"),
        ("head_bucket", "memoria-voice"),
    ]


@pytest.mark.asyncio
async def test_online_readiness_reports_unavailable_control_database_as_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    monkeypatch.setenv("OFFLINE_MOCK", "false")
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
        DASHSCOPE_API_KEY="dashscope-key",
        MEMORIA_AUTH_SECRET="control-auth-material-that-is-long-enough",
        MEMORIA_RELEASE_TAG="release-readiness-test",
        LLM_PROVIDER="bailian_deepseek",
        OFFLINE_MOCK=False,
    )
    body = {
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
async def test_smoke_mark_rejects_engines_the_control_api_is_not_configured_with(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A smoke that exercised another engine than this Control API runs is not evidence."""

    _configure_local(monkeypatch, tmp_path)
    app = create_app()
    app.state.settings = ControlSettings(
        _env_file=None,
        ENVIRONMENT="production",
        PUBLIC_BASE_URL="https://voice.example.com",
        ALLOWED_ORIGINS="https://voice.example.com",
        DASHSCOPE_API_KEY="dashscope-key",
        MEMORIA_AUTH_SECRET="control-auth-material-that-is-long-enough",
        MEMORIA_RELEASE_TAG="release-readiness-test",
        LLM_PROVIDER="bailian_deepseek",
        ASR_PROVIDER="volc",
        OFFLINE_MOCK=False,
    )
    body = {
        "funasr": True,
        "llm": True,
        "llm_provider": "bailian_deepseek",
        "release_tag": "release-readiness-test",
        "tts": {"provider": "doubao", "audio": True, "word_timestamps": True},
        "engines": "voice_engines asr=funasr tts=doubao",
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

    assert response.status_code == 409
    assert "asr=funasr" in response.json()["detail"]
    assert "asr=volc" in response.json()["detail"]


@pytest.mark.asyncio
async def test_heartbeat_running_another_engine_closes_production_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Agent and Control read separate env files, so their engines can split.

    The bridge heartbeat is otherwise fresh and the smokes passed, but the
    engine it built is not the one this Control API configured. Reporting that
    is the only way the split shows up before a child hears the wrong voice.
    """

    _configure_local(monkeypatch, tmp_path)
    app = create_app()
    app.state.settings = ControlSettings(
        _env_file=None,
        ENVIRONMENT="production",
        MEMORIA_RELEASE_TAG="release-heartbeat-test",
        MEMORIA_AGENT_HEARTBEAT_TOKEN=SecretStr("agent-heartbeat-token"),
        ASR_PROVIDER="funasr",
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
                "last_loop_at": datetime.now(UTC).isoformat(),
                "chat_llm_providers": ["deepseek"],
                "engines": "voice_engines asr=volc tts=doubao",
            },
        )
        ready = await client.get("/health/ready")

    assert heartbeat.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["checks"]["agent"]["status"] == "engine_mismatch"
    assert ready.json()["checks"]["agent"]["engines"] == "voice_engines asr=volc tts=doubao"


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
        retired_field = await client.post(
            "/internal/readiness/agent-heartbeat",
            headers={"X-Memoria-Internal-Token": "agent-heartbeat-token"},
            json={
                "release_tag": "release-heartbeat-test",
                "boot_id": "8f819a3b-ec8f-4319-94ab-7cace979145f",
                "worker_ready": True,
                "livekit_ready": True,
                "last_loop_at": datetime.now(UTC).isoformat(),
                "chat_llm_providers": ["deepseek"],
            },
        )
        heartbeat = await client.post(
            "/internal/readiness/agent-heartbeat",
            headers={"X-Memoria-Internal-Token": "agent-heartbeat-token"},
            json={
                "release_tag": "release-heartbeat-test",
                "boot_id": "8f819a3b-ec8f-4319-94ab-7cace979145f",
                "worker_ready": True,
                "last_loop_at": datetime.now(UTC).isoformat(),
                "chat_llm_providers": ["deepseek"],
            },
        )
        ready = await client.get("/health/ready")

    assert missing.status_code == 503
    assert missing.json()["checks"]["agent"] == {"status": "missing"}
    # The media bridge heartbeat has no LiveKit field; extra keys are refused.
    assert retired_field.status_code == 422
    assert heartbeat.status_code == 200
    assert heartbeat.json()["status"] == "recorded"
    assert ready.status_code == 200
    assert ready.json()["checks"]["agent"] == {
        "status": "ready",
        "release_tag": "release-heartbeat-test",
        "boot_id": "8f819a3b-ec8f-4319-94ab-7cace979145f",
        "worker_ready": True,
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
                "last_loop_at": datetime.fromtimestamp(
                    datetime.now(UTC).timestamp() - 46,
                    tz=UTC,
                ).isoformat(),
                "chat_llm_providers": ["deepseek"],
            },
        )
        stale = await client.get("/health/ready")

    assert heartbeat.status_code == 200
    assert stale.status_code == 503
    assert stale.json()["checks"]["agent"]["status"] == "stale"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_llm_providers", "status_code"),
    [(None, 422), ([], 422), (["openai"], 422), (["qwen", "deepseek"], 200)],
    ids=["missing", "empty", "unfiled-family", "both-filed"],
)
async def test_an_agent_heartbeat_names_the_filed_chat_models_it_builds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    chat_llm_providers: list[str] | None,
    status_code: int,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    monkeypatch.setenv("MEMORIA_AGENT_HEARTBEAT_TOKEN", "agent-heartbeat-token")
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-heartbeat-test")
    body: dict[str, object] = {
        "release_tag": "release-heartbeat-test",
        "boot_id": "8f819a3b-ec8f-4319-94ab-7cace979145f",
        "worker_ready": True,
        "last_loop_at": datetime.now(UTC).isoformat(),
    }
    if chat_llm_providers is not None:
        body["chat_llm_providers"] = chat_llm_providers

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.post(
            "/internal/readiness/agent-heartbeat",
            headers={"X-Memoria-Internal-Token": "agent-heartbeat-token"},
            json=body,
        )

    assert response.status_code == status_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age_h", "status_code", "smokes", "warnings"),
    [
        (1, 200, "passed", []),
        # One 12 h refresh missed (+1 h grace): still ready, but reported.
        (14, 200, "overdue", ["smoke_refresh_overdue"]),
        (25, 503, "expired", None),
    ],
)
async def test_readiness_reports_an_overdue_smoke_refresh_before_it_expires(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    age_h: int,
    status_code: int,
    smokes: str,
    warnings: list[str] | None,
) -> None:
    _configure_local(monkeypatch, tmp_path)
    app = create_app()
    app.state.settings = ControlSettings(
        _env_file=None,
        ENVIRONMENT="production",
        MEMORIA_RELEASE_TAG="release-refresh-test",
        LLM_PROVIDER="bailian_deepseek",
        OFFLINE_MOCK=False,
    )

    async def ready_core(*_: object) -> dict[str, str]:
        return {"control_database": "ready"}

    monkeypatch.setattr(readiness_routes, "_valid_configuration", lambda _: True)
    monkeypatch.setattr(readiness_routes, "_missing_config", lambda _: [])
    monkeypatch.setattr(readiness_routes, "_core_checks", ready_core)
    monkeypatch.setattr(readiness_routes, "_agent_state", lambda *_: {"status": "ready"})
    marked_at = datetime.fromtimestamp(datetime.now(UTC).timestamp() - age_h * 3600, tz=UTC)
    app.state.memory_store.mark_readiness(
        release_tag="release-refresh-test",
        llm_provider="bailian_deepseek",
        marked_at=marked_at.isoformat().replace("+00:00", "Z"),
    )

    code, body = await _ready(app)

    assert code == status_code
    assert body["smokes"] == smokes
    if warnings is not None:
        assert body["status"] == "ready"
        assert body["warnings"] == warnings


@pytest.mark.hermetic
def test_missing_config_no_longer_checks_asr_credentials() -> None:
    """Control-api does not run ASR, so it does not check ASR credentials."""
    funasr = ControlSettings(
        _env_file=None,
        ASR_PROVIDER="funasr",
        MEMORIA_RELEASE_TAG="release-readiness-test",
    )
    volc = ControlSettings(
        _env_file=None,
        ASR_PROVIDER="volc",
        MEMORIA_RELEASE_TAG="release-readiness-test",
    )
    untouched = ControlSettings(
        _env_file=None,
        MEMORIA_RELEASE_TAG="release-readiness-test",
    )

    assert "DASHSCOPE_API_KEY" not in readiness_routes._missing_config(funasr)
    assert "DASHSCOPE_API_KEY" not in readiness_routes._missing_config(volc)
    assert untouched.asr_provider == "funasr"
    assert "DASHSCOPE_API_KEY" not in readiness_routes._missing_config(untouched)


@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_health_ready_ignores_a_missing_dashscope_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The bridge owns the ASR credential check, so control no longer 503s for it."""
    _configure_local(monkeypatch, tmp_path)
    monkeypatch.setenv("OFFLINE_MOCK", "false")
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-readiness-test")
    monkeypatch.setenv("ASR_PROVIDER", "funasr")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    status, body = await _ready(create_app())

    assert status == 503
    assert body["missing"] == []
    assert body["smokes"] == "not_run"


def test_overdue_threshold_follows_the_refresh_timer() -> None:
    timer = Path(__file__).parents[3] / "infra" / "memoria-readiness-refresh.timer"
    interval = next(
        line.split("=", 1)[1].strip()
        for line in timer.read_text(encoding="utf-8").splitlines()
        if line.startswith("OnUnitActiveSec=")
    )
    assert interval.endswith("h")
    assert int(interval[:-1]) * 3600 == readiness_routes._REFRESH_INTERVAL_S
