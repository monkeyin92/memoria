from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from services.control_api.app import main
from services.control_api.app.multi_subject_runtime import (
    PostgresMultiSubjectRuntimeControl,
)
from services.control_api.app.routes import readiness as readiness_routes
from services.control_api.tests.test_tutor_config import _production_settings
from services.session_runtime.service import PostgresSessionRuntimeService


class _ProductionSettingsProxy:
    def __init__(self, settings: object) -> None:
        self._settings = settings

    def validate_production(self) -> None:
        return None

    def __getattr__(self, name: str) -> object:
        return getattr(self._settings, name)


class _FakeEvolutionStore:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.initialized = False
        self.closed = False

    def initialize(self) -> None:
        self.initialized = True

    def close(self) -> None:
        self.closed = True


class _NoopWorker:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _ReadinessStore:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def readiness(self) -> object:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class _FakeMemoryWiring:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True
        self._events.append("memory_started")

    async def close(self) -> None:
        self.closed = True
        self._events.append("memory_closed")


def _production_test_settings(tmp_path: Path) -> _ProductionSettingsProxy:
    settings = _production_settings().model_copy(
        update={
            "memoria_db_path": str(tmp_path / "memoria.sqlite3"),
            "evolution_db_path": str(tmp_path / "evolution.sqlite3"),
            "speaker_database_path": str(tmp_path / "speakers.sqlite3"),
            "identity_db_path": str(tmp_path / "identity.sqlite3"),
            "voice_sample_store_path": str(tmp_path / "voice-samples"),
            "archive_object_store_path": str(tmp_path / "archive-objects"),
            "archive_database_url": SecretStr(""),
            "guardian_database_url": SecretStr(""),
            "identity_database_url": SecretStr(""),
            "speaker_database_url": SecretStr(""),
            "evolution_database_url": SecretStr("postgresql://memoria_evolution:test@db/memoria"),
            "voice_object_bucket": "",
            "archive_object_bucket": "",
            "offline_mock": True,
        }
    )
    return _ProductionSettingsProxy(settings)


def _patch_production_lifespan_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[object, object]:
    settings = _production_test_settings(tmp_path)
    monkeypatch.setattr(main, "ControlSettings", lambda: settings)
    monkeypatch.setattr(main, "PostgresEvolutionStore", _FakeEvolutionStore)
    monkeypatch.setattr(main, "EvolutionSleepWorker", _NoopWorker)
    monkeypatch.setattr(main, "MemoryCompilerWorker", _NoopWorker)
    monkeypatch.setattr(main, "CorpusRetentionWorker", _NoopWorker)
    monkeypatch.setattr(main, "AccountDeletionWorker", _NoopWorker)

    session_initialized: list[object] = []
    session_closed: list[object] = []
    consent_initialized: list[object] = []
    consent_closed: list[object] = []
    memory_wirings: list[_FakeMemoryWiring] = []
    memory_install_kwargs: list[dict[str, object]] = []
    events: list[str] = []

    async def fake_session_initialize(store: object) -> None:
        session_initialized.append(store)
        events.append("session_initialized")

    async def fake_session_close(store: object) -> None:
        session_closed.append(store)
        events.append("session_closed")

    async def fake_consent_initialize(store: object) -> None:
        consent_initialized.append(store)

    async def fake_consent_close(store: object) -> None:
        consent_closed.append(store)

    def fake_install_memory_production(app: object, *_args: object, **kwargs: object) -> object:
        wiring = _FakeMemoryWiring(events)
        memory_wirings.append(wiring)
        memory_install_kwargs.append(kwargs)
        app.state.memory_wiring = wiring
        return wiring

    monkeypatch.setattr(
        main.PostgresSessionRuntimeStore,
        "initialize",
        fake_session_initialize,
    )
    monkeypatch.setattr(
        main.PostgresSessionRuntimeStore,
        "close",
        fake_session_close,
    )
    monkeypatch.setattr(
        main.PostgresBindingConsentStore,
        "initialize",
        fake_consent_initialize,
    )
    monkeypatch.setattr(
        main.PostgresBindingConsentStore,
        "close",
        fake_consent_close,
    )
    monkeypatch.setattr(
        main,
        "install_memory_production",
        fake_install_memory_production,
    )
    return main.create_app(), SimpleNamespace(
        session_initialized=session_initialized,
        session_closed=session_closed,
        consent_initialized=consent_initialized,
        consent_closed=consent_closed,
        memory_wirings=memory_wirings,
        memory_install_kwargs=memory_install_kwargs,
        events=events,
    )


@pytest.mark.asyncio
async def test_production_lifespan_wires_authoritative_runtime_without_state_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, lifecycle = _patch_production_lifespan_dependencies(monkeypatch, tmp_path)

    assert app.state.session_runtime_store is None
    assert app.state.session_runtime_service is None
    assert app.state.multi_subject_runtime is None
    assert app.state.policy_receipt_writer is None
    assert app.state.binding_consent_store is None

    async with main.lifespan(app):
        store = app.state.session_runtime_store
        consent_store = app.state.binding_consent_store
        service = app.state.session_runtime_service
        runtime = app.state.multi_subject_runtime

        assert type(store) is main.PostgresSessionRuntimeStore
        assert type(consent_store) is main.PostgresBindingConsentStore
        assert isinstance(service, PostgresSessionRuntimeService)
        assert isinstance(runtime, PostgresMultiSubjectRuntimeControl)
        assert runtime.sessions is service
        assert service._store is store
        assert app.state.policy_receipt_writer is None
        assert lifecycle.session_initialized == [store]
        assert lifecycle.consent_initialized == [consent_store]
        assert len(lifecycle.memory_wirings) == 1
        assert app.state.memory_wiring is lifecycle.memory_wirings[0]
        assert lifecycle.memory_wirings[0].started is True
        installed = lifecycle.memory_install_kwargs[0]
        assert installed["sensitive_write"] is not None
        assert installed["context_builder"] is not None
        assert isinstance(
            installed["grant_resolver"],
            main.IdentityRelationshipGrantResolver,
        )
        assert isinstance(
            installed["shared_action_executor"],
            main.PostgresFamilySharedActionExecutor,
        )
        assert installed["receipt_verifier"] is None
        assert installed["family_membership_verifier"] is None
        assert installed["consent_verifier"] is None
        assert lifecycle.events.index("session_initialized") < lifecycle.events.index(
            "memory_started"
        )
        assert store._dsn == ("postgresql://memoria_session_api:test@db/memoria")
        assert store._action_dsn == ("postgresql://memoria_action_executor:test@db/memoria")

    assert lifecycle.session_closed == [store]
    assert lifecycle.consent_closed == [consent_store]
    assert lifecycle.memory_wirings[0].closed is True
    assert lifecycle.events.index("memory_closed") < lifecycle.events.index("session_closed")


@pytest.mark.asyncio
async def test_production_lifespan_closes_initialized_store_when_startup_body_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, lifecycle = _patch_production_lifespan_dependencies(monkeypatch, tmp_path)

    def fail_after_runtime_install(_app: object) -> None:
        raise RuntimeError("startup failed after Session Runtime installation")

    monkeypatch.setattr(main, "_install_tutor_authority", fail_after_runtime_install)

    with pytest.raises(RuntimeError, match="startup failed"):
        async with main.lifespan(app):
            raise AssertionError("lifespan body must not be reached")

    assert len(lifecycle.session_initialized) == 1
    assert lifecycle.session_closed == lifecycle.session_initialized
    assert len(lifecycle.consent_initialized) == 1
    assert lifecycle.consent_closed == lifecycle.consent_initialized
    assert len(lifecycle.memory_wirings) == 1
    assert lifecycle.memory_wirings[0].started is True
    assert lifecycle.memory_wirings[0].closed is True
    assert lifecycle.events.index("memory_closed") < lifecycle.events.index("session_closed")


def test_production_create_app_eager_state_has_no_memory_runtime_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _production_test_settings(tmp_path)
    monkeypatch.setattr(main, "ControlSettings", lambda: settings)

    app = main.create_app()

    assert app.state.session_runtime_store is None
    assert app.state.session_runtime_service is None
    assert app.state.multi_subject_runtime is None
    assert app.state.policy_receipt_writer is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "error", "expected"),
    [
        ({"session_runtime_schema": "ready"}, None, "ready"),
        (None, RuntimeError("schema/RLS unavailable"), "unavailable"),
    ],
)
async def test_readiness_calls_store_probe_and_maps_failure_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result: object,
    error: Exception | None,
    expected: str,
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
    app = main.create_app()
    store = _ReadinessStore(result=result, error=error)
    app.state.session_runtime_store = store

    checks = await readiness_routes._core_checks(
        SimpleNamespace(app=app),
        app.state.settings,
    )

    assert checks["session_runtime"] == expected
    assert store.calls == 1


@pytest.mark.parametrize(
    "field_name",
    [
        "MEMORIA_SESSION_RUNTIME_DATABASE_URL",
        "MEMORIA_ACTION_EXECUTOR_DATABASE_URL",
        "MEMORIA_SESSION_RUNTIME_PROJECTOR_DATABASE_URL",
        "MEMORIA_SESSION_RUNTIME_WORKER_DATABASE_URL",
        "MEMORIA_SESSION_RUNTIME_MAINTENANCE_DATABASE_URL",
    ],
)
def test_production_requires_all_session_runtime_dsns(field_name: str) -> None:
    settings = _production_settings(**{field_name: ""})

    with pytest.raises(ValueError, match=field_name):
        settings.validate_production()


def test_production_requires_exactly_one_session_runtime_schema_management_mode() -> None:
    externally_managed = _production_settings(
        MEMORIA_SESSION_RUNTIME_BOOTSTRAP_DATABASE_URL="",
        MEMORIA_SESSION_RUNTIME_SCHEMA_MANAGED_EXTERNALLY="true",
    )
    externally_managed.validate_production()

    unmanaged = _production_settings(
        MEMORIA_SESSION_RUNTIME_BOOTSTRAP_DATABASE_URL="",
        MEMORIA_SESSION_RUNTIME_SCHEMA_MANAGED_EXTERNALLY="false",
    )
    with pytest.raises(
        ValueError,
        match=(
            "MEMORIA_SESSION_RUNTIME_BOOTSTRAP_DATABASE_URL.*"
            "MEMORIA_SESSION_RUNTIME_SCHEMA_MANAGED_EXTERNALLY"
        ),
    ):
        unmanaged.validate_production()

    ambiguous = _production_settings(
        MEMORIA_SESSION_RUNTIME_SCHEMA_MANAGED_EXTERNALLY="true",
    )
    with pytest.raises(ValueError, match="exactly one Session Runtime schema management"):
        ambiguous.validate_production()


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        (
            "MEMORIA_SESSION_RUNTIME_DATABASE_URL",
            "postgresql://wrong_role:test@db/memoria",
        ),
        (
            "MEMORIA_ACTION_EXECUTOR_DATABASE_URL",
            "postgresql://memoria_session_api:test@db/memoria",
        ),
        (
            "MEMORIA_SESSION_RUNTIME_PROJECTOR_DATABASE_URL",
            "postgresql://memoria_session_worker:test@db/memoria",
        ),
        (
            "MEMORIA_SESSION_RUNTIME_WORKER_DATABASE_URL",
            "postgresql://memoria_session_projector:test@db/memoria",
        ),
        (
            "MEMORIA_SESSION_RUNTIME_MAINTENANCE_DATABASE_URL",
            "postgresql://memoria_session_api:test@db/memoria",
        ),
    ],
)
def test_production_rejects_session_runtime_role_mismatch(
    field_name: str,
    value: str,
) -> None:
    settings = _production_settings(**{field_name: value})

    with pytest.raises(ValueError, match="independent"):
        settings.validate_production()


def test_production_rejects_session_runtime_role_reuse_and_bootstrap_reuse() -> None:
    reused_runtime = _production_settings(
        MEMORIA_SESSION_RUNTIME_PROJECTOR_DATABASE_URL=(
            "postgresql://memoria_session_worker:test@db/memoria"
        )
    )
    with pytest.raises(ValueError, match="independent"):
        reused_runtime.validate_production()

    reused_bootstrap = _production_settings(
        MEMORIA_SESSION_RUNTIME_BOOTSTRAP_DATABASE_URL=(
            "postgresql://memoria_session_api:test@db/memoria"
        )
    )
    with pytest.raises(ValueError, match="independent Session Runtime bootstrap"):
        reused_bootstrap.validate_production()


def test_production_routes_only_resolve_runtime_from_app_state() -> None:
    route_source = (Path(main.__file__).parent / "routes" / "multi_subject.py").read_text(
        encoding="utf-8"
    )
    assert "request.app.state.multi_subject_runtime" in route_source
    assert "MultiSubjectRuntimeControl(" not in route_source
    assert "InMemoryPolicyReceiptWriter(" not in route_source
