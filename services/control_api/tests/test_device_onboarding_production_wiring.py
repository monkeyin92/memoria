from __future__ import annotations

import base64

import pytest
from services.control_api.app import main
from services.control_api.app.config import ControlSettings


class _FakePostgresStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.initialized = False
        self.closed = False

    def initialize(self) -> None:
        self.initialized = True

    def close(self) -> None:
        self.closed = True


def _settings(**overrides: str) -> ControlSettings:
    values = {
        "ENVIRONMENT": "production",
        "DEVICE_MEDIA_GATEWAY_URL": "wss://voice.example.com/memoria-device-media",
        "MEMORIA_DEVICE_ONBOARDING_DATABASE_URL": (
            "postgresql://memoria_device_onboarding_api:secret@postgres/memoria"
        ),
        "MEMORIA_DEVICE_ACTIVATION_SIGNING_SEED_B64": base64.b64encode(
            bytes(range(32))
        ).decode("ascii"),
    }
    values.update(overrides)
    return ControlSettings(_env_file=None, **values)


def test_production_onboarding_uses_postgres_and_explicit_activation_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "PostgresBootstrapStore", _FakePostgresStore)

    service = main._device_onboarding_service(_settings())

    assert service is not None
    assert isinstance(service.store, _FakePostgresStore)
    assert service.store.initialized is True
    assert service.offline_mock is False
    assert service.activation_public_key_b64 == (
        "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg"
    )
    service.close()
    assert service.store.closed is True


def test_production_without_device_gateway_keeps_onboarding_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = False

    class _ForbiddenStore(_FakePostgresStore):
        def __init__(self, dsn: str) -> None:
            nonlocal connected
            connected = True
            super().__init__(dsn)

    monkeypatch.setattr(main, "PostgresBootstrapStore", _ForbiddenStore)

    assert main._device_onboarding_service(
        _settings(DEVICE_MEDIA_GATEWAY_URL="")
    ) is None
    assert connected is False


def test_production_onboarding_rejects_invalid_activation_seed_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = False

    class _ForbiddenStore(_FakePostgresStore):
        def __init__(self, dsn: str) -> None:
            nonlocal connected
            connected = True
            super().__init__(dsn)

    monkeypatch.setattr(main, "PostgresBootstrapStore", _ForbiddenStore)

    with pytest.raises(RuntimeError, match="signing seed"):
        main._device_onboarding_service(
            _settings(MEMORIA_DEVICE_ACTIVATION_SIGNING_SEED_B64="not-base64")
        )

    assert connected is False


def test_production_onboarding_closes_store_when_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_FakePostgresStore] = []

    class _FailingStore(_FakePostgresStore):
        def __init__(self, dsn: str) -> None:
            super().__init__(dsn)
            created.append(self)

        def initialize(self) -> None:
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(main, "PostgresBootstrapStore", _FailingStore)

    with pytest.raises(RuntimeError, match="database unavailable"):
        main._device_onboarding_service(_settings())

    assert len(created) == 1
    assert created[0].closed is True
