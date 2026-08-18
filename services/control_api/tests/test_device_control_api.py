from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from services.control_api.app.account_gate import AccountOperationGate
from services.control_api.app.database import MemoryStore
from services.control_api.app.device_control import (
    AcousticCapabilityAuthority,
    RuntimeProfileLedger,
    stable_profile_fingerprint,
)
from services.control_api.app.routes import device_control
from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
)
from services.control_api.tests.test_device_onboarding_api import (
    _activate_device,
)
from services.device_fleet.tests.test_bootstrap_vertical_slice import (
    _fixture,
)


class _Settings:
    device_runtime_profile_ttl_s = 3600

    def __init__(self) -> None:
        self.runtime_profile_signing_secret = SecretStr("device-profile-signing-secret-material")

    def runtime_profile_signing_key(self) -> bytes:
        return self.runtime_profile_signing_secret.get_secret_value().encode("utf-8")


def _profile_app(
    tmp_path: Path,
    *,
    category: str | None,
) -> tuple[FastAPI, dict[str, object]]:
    service, store, device_key, payload = _fixture()
    manifest = _activate_device(service, store, device_key, payload)
    app = FastAPI()
    app.include_router(device_control.router)
    app.state.device_onboarding_service = service
    memory = MemoryStore(str(tmp_path / "memoria.sqlite3"))
    memory.initialize()
    if category is not None:
        memory.update_subject_profile(
            user_id="person_a",
            subject_category=category,  # type: ignore[arg-type]
            birth_year_band="adult" if category == "adult" else "14_17",
            age_evidence_status="verified" if category == "adult" else "unverified",
            now=datetime.now(UTC).isoformat(),
        )
    app.state.memory_store = memory
    app.state.account_operations = AccountOperationGate()
    app.state.settings = _Settings()

    async def invalidate(**_payload: object) -> dict[str, object]:
        return {"delivered": False}

    app.state.device_runtime_invalidator = invalidate

    async def runtime_status(**payload: object) -> dict[str, object]:
        return {
            "device_id": payload["device_id"],
            "connected": False,
        }

    app.state.device_runtime_status_reader = runtime_status

    def authenticated_user() -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id="person_a",
            session_id="test-session",
            jti="test-jti",
        )

    app.dependency_overrides[require_authenticated_user] = authenticated_user
    return app, manifest


@pytest.mark.asyncio
async def test_device_settings_matrix_and_learning_mode_versioning(
    tmp_path: Path,
) -> None:
    app, _manifest = _profile_app(tmp_path, category="adult")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        initial = await client.get("/v1/devices/dev_test_01/settings")
        assert initial.status_code == 200, initial.text
        body = initial.json()
        for key in (
            "settings_version",
            "volume_limit",
            "screen_brightness",
            "night_mode",
            "do_not_disturb",
            "learning_mode",
            "audio_mode",
            "wake_mode",
            "allowed_barge_in",
        ):
            assert key in body
        assert body["learning_mode"] == "off"
        assert body["runtime_profile_version"] == 0

        patched = await client.patch(
            "/v1/devices/dev_test_01/settings",
            json={"changes": {"learning_mode": "tutor_english"}, "reason": "guardian_choice"},
        )
        assert patched.status_code == 200, patched.text
        updated = patched.json()
        assert updated["learning_mode"] == "tutor_english"
        assert updated["settings_version"] == 1
        assert updated["runtime_profile_version"] == 1
        assert updated["runtime_apply_status"] == "next_session"

        replayed = await client.patch(
            "/v1/devices/dev_test_01/settings",
            json={"changes": {"learning_mode": "tutor_english"}},
        )
        assert replayed.status_code == 200
        assert replayed.json()["runtime_profile_version"] == 1

        forbidden_audio = await client.patch(
            "/v1/devices/dev_test_01/settings",
            json={"changes": {"audio_mode": "full_duplex_verified"}},
        )
        assert forbidden_audio.status_code == 422
        assert forbidden_audio.json()["detail"]["code"] == "audio_mode_requires_aec_evidence"

        interrupt_assist = await client.patch(
            "/v1/devices/dev_test_01/settings",
            json={"changes": {"audio_mode": "interrupt_assist"}},
        )
        assert interrupt_assist.status_code == 200, interrupt_assist.text
        assert interrupt_assist.json()["audio_mode"] == "interrupt_assist"

        contradictory_none = await client.patch(
            "/v1/devices/dev_test_01/settings",
            json={"changes": {"allowed_barge_in": ["none", "button"]}},
        )
        assert contradictory_none.status_code == 422
        assert contradictory_none.json()["detail"] == (
            "allowed_barge_in none must be the only value"
        )

        AcousticCapabilityAuthority(app.state.memory_store).register(
            device_id="dev_test_01",
            board_profile="memoria-atk-dnesp32s3-v1",
            firmware_version_range="0.2.x",
            acoustic_profile_version=1,
            simultaneous_capture_playback=True,
            aec_reference_type="software_post_gain_pre_i2s",
            aec_verified=True,
            max_barge_in_level="low",
            tested_volume_range="40-90",
            tested_distance_m=2.0,
            test_report_uri="s3://memoria-acoustic/atk-v1",
            approved_by="qa",
            now=datetime.now(UTC),
        )
        allowed_audio = await client.patch(
            "/v1/devices/dev_test_01/settings",
            json={"changes": {"audio_mode": "full_duplex_verified"}},
        )
        assert allowed_audio.status_code == 200, allowed_audio.text
        assert allowed_audio.json()["audio_mode"] == "full_duplex_verified"
        assert allowed_audio.json()["runtime_profile_version"] == 3


@pytest.mark.asyncio
async def test_device_settings_dispatches_runtime_invalidation_at_the_right_boundary(
    tmp_path: Path,
) -> None:
    app, _manifest = _profile_app(tmp_path, category="adult")
    calls: list[dict[str, object]] = []

    async def invalidate(**payload: object) -> dict[str, object]:
        calls.append(payload)
        return {"delivered": True}

    app.state.device_runtime_invalidator = invalidate
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        ordinary = await client.patch(
            "/v1/devices/dev_test_01/settings",
            json={"changes": {"volume_limit": 61}},
        )
        safety = await client.patch(
            "/v1/devices/dev_test_01/settings",
            json={"changes": {"allowed_barge_in": ["button"]}},
        )
    assert ordinary.status_code == 200, ordinary.text
    assert ordinary.json()["runtime_apply_status"] == "dispatched"
    assert safety.status_code == 200, safety.text
    assert calls == [
        {
            "device_id": "dev_test_01",
            "profile_version": 1,
            "apply_at": "next_safe_point",
        },
        {
            "device_id": "dev_test_01",
            "profile_version": 2,
            "apply_at": "immediate_fail_closed",
        },
    ]


@pytest.mark.asyncio
async def test_device_settings_reports_applied_only_from_matching_live_device_ack(
    tmp_path: Path,
) -> None:
    app, _manifest = _profile_app(tmp_path, category="adult")

    async def runtime_status(**payload: object) -> dict[str, object]:
        return {
            "device_id": payload["device_id"],
            "connected": True,
            "applied_profile_version": 1,
            "applied_settings_version": 1,
        }

    app.state.device_runtime_status_reader = runtime_status
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        patched = await client.patch(
            "/v1/devices/dev_test_01/settings",
            json={"changes": {"volume_limit": 61}},
        )
        assert patched.status_code == 200, patched.text
        current = await client.get("/v1/devices/dev_test_01/settings")
    assert current.status_code == 200, current.text
    assert current.json()["runtime_apply_status"] == "applied"


@pytest.mark.asyncio
async def test_device_runtime_profile_is_signed_and_versioned(tmp_path: Path) -> None:
    app, manifest = _profile_app(tmp_path, category="adult")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        patched = await client.patch(
            "/v1/devices/dev_test_01/settings",
            json={"changes": {"learning_mode": "tutor_homework"}},
        )
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["runtime_profile_version"] == 1
        assert body["binding_version"] == manifest["binding_version"]

        changes = await client.get(
            "/v1/devices/dev_test_01/runtime-profile/changes",
            params={"after_version": 0},
        )
        assert changes.status_code == 200
        assert changes.json()["changed"] is True
        changes = await client.get(
            "/v1/devices/dev_test_01/runtime-profile/changes",
            params={"after_version": body["runtime_profile_version"]},
        )
        assert changes.json()["changed"] is False

        ack = await client.post(
            "/v1/devices/dev_test_01/runtime-profile/ack",
            json={
                "profile_version": body["runtime_profile_version"],
                "accepted": True,
            },
        )
        assert ack.status_code == 200, ack.text
        assert ack.json()["replayed"] is False
        second = await client.patch(
            "/v1/devices/dev_test_01/settings",
            json={"changes": {"learning_mode": "off"}},
        )
        assert second.status_code == 200
        assert second.json()["runtime_profile_version"] == 2
        latest_ack = await client.post(
            "/v1/devices/dev_test_01/runtime-profile/ack",
            json={"profile_version": 2, "accepted": True},
        )
        assert latest_ack.status_code == 200
        regression = await client.post(
            "/v1/devices/dev_test_01/runtime-profile/ack",
            json={"profile_version": 1, "accepted": True},
        )
        assert regression.status_code == 409


def test_runtime_profile_ledger_preserves_profile_and_settings_components(
    tmp_path: Path,
) -> None:
    store = MemoryStore(str(tmp_path / "ledger.sqlite3"))
    store.initialize()
    ledger = RuntimeProfileLedger(store)
    now = datetime.now(UTC)
    profile_fingerprint = stable_profile_fingerprint(
        {
            "binding_version": 3,
            "active_subject_id": "person_a",
            "subject_category": "adult",
            "service_mode": "adult_companion",
            "capabilities": ["chat"],
        }
    )
    first = ledger.observe(
        device_id="dev-ledger",
        runtime_profile_id="profile-1",
        content_fingerprint=profile_fingerprint,
        issued_at=now,
        expires_at=now.replace(year=now.year + 1),
        now=now,
    )
    settings_one = ledger.observe_config_change(
        device_id="dev-ledger",
        content_fingerprint="a" * 64,
        now=now,
    )
    profile_replay = ledger.observe(
        device_id="dev-ledger",
        runtime_profile_id="profile-2",
        content_fingerprint=profile_fingerprint,
        issued_at=now,
        expires_at=now.replace(year=now.year + 1),
        now=now,
    )
    settings_replay = ledger.observe_config_change(
        device_id="dev-ledger",
        content_fingerprint="a" * 64,
        now=now,
    )
    settings_two = ledger.observe_config_change(
        device_id="dev-ledger",
        content_fingerprint="b" * 64,
        now=now,
    )
    assert [
        first.profile_version,
        settings_one.profile_version,
        profile_replay.profile_version,
        settings_replay.profile_version,
        settings_two.profile_version,
    ] == [1, 2, 2, 2, 3]
    assert settings_two.runtime_profile_id == "profile-2"
    assert settings_two.profile_fingerprint == profile_fingerprint
    assert settings_two.settings_fingerprint == "b" * 64


@pytest.mark.asyncio
async def test_device_diagnostics_projection(tmp_path: Path) -> None:
    app, manifest = _profile_app(tmp_path, category="adult")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/devices/dev_test_01/diagnostics/latest")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["binding"]["binding_id"] == manifest["binding_id"]
        assert body["binding"]["binding_version"] == manifest["binding_version"]
        assert body["acoustic_capability"] is None
        assert body["allowed_audio_modes"] == ["half_duplex_safe", "interrupt_assist"]
        assert "learning_mode" in body["settings"]
        assert body["live_runtime"] == {
            "device_id": "dev_test_01",
            "connected": False,
        }


@pytest.mark.asyncio
async def test_minor_can_read_device_state_but_not_manage(tmp_path: Path) -> None:
    app, _manifest = _profile_app(tmp_path, category="minor")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (await client.get("/v1/devices/dev_test_01/settings")).status_code == 200
        assert (await client.get("/v1/devices/dev_test_01/diagnostics/latest")).status_code == 200
        forbidden = await client.patch(
            "/v1/devices/dev_test_01/settings",
            json={"changes": {"learning_mode": "tutor_english"}},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"]["code"] == "minor_forbidden"


class _CloseSessionAuthority:
    """Minimal Postgres authority double for the device close pipeline."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.replay_already_closed = False

    async def close_session(
        self,
        *,
        actor_id: str,
        session_id: str,
        reason_code: str,
        now: datetime,
    ) -> SimpleNamespace:
        self.calls.append(
            {
                "actor_id": actor_id,
                "session_id": session_id,
                "reason_code": reason_code,
            }
        )
        return SimpleNamespace(already_closed=self.replay_already_closed)


_CLOSE_REPORT_TOKEN = "edge-device-close-report-token-32chars"


def _close_report_fixture(
    tmp_path: Path,
) -> tuple[FastAPI, MemoryStore, _CloseSessionAuthority]:
    """App wired with the close-report token and one authoritative session."""

    app, manifest = _profile_app(tmp_path, category="adult")
    app.include_router(device_control.internal_router)
    app.state.device_close_report_token = SecretStr(_CLOSE_REPORT_TOKEN)
    authority = _CloseSessionAuthority()
    app.state.session_runtime_service = authority
    store = app.state.memory_store
    now = datetime.now(UTC)
    created_at = now.isoformat().replace("+00:00", "Z")
    store.create_device_media_session(
        session_id="session-close-1",
        device_id="dev_test_01",
        binding_id=str(manifest["binding_id"]),
        binding_version=int(manifest["binding_version"]),
        subject_id="person_a",
        active_subject_id="person_a",
        client_id="esp-installation-1",
        runtime="direct_voice_core",
        protocol_version=2,
        stream_epoch=1,
        firmware_version="0.2.0",
        board_profile="memoria-atk-dnesp32s3-v1",
        runtime_profile_version=1,
        settings_version=0,
        audio_mode_requested="half_duplex_safe",
        ticket_jti="ticket-close-1",
        created_at=created_at,
        expires_at=(now + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
    )
    store.add_voice_session(
        session_id="session-close-1",
        user_id="person_a",
        resource_owner_account_id="person_a",
        room_name="room-close-1",
        voice_backend="cascade",
        created_at=created_at,
        interaction_mode="companion",
        mode_policy_version="1",
        digital_self_version_id=None,
    )
    return app, store, authority


def _close_report_payload(now: datetime) -> dict[str, object]:
    """The exact field shape media_edge.DeviceSessionCloseReport emits."""

    return {
        "session_id": "session-close-1",
        "device_id": "dev_test_01",
        "stream_epoch": 1,
        "account_id": "person_a",
        "reason": "device_close",
        "connected": True,
        "connected_at": (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
        "closed_at": now.isoformat().replace("+00:00", "Z"),
    }


@pytest.mark.asyncio
async def test_edge_close_report_accepts_real_payload_and_closes_projection(
    tmp_path: Path,
) -> None:
    app, store, authority = _close_report_fixture(tmp_path)
    headers = {"X-Memoria-Edge-Device-Close-Token": _CLOSE_REPORT_TOKEN}
    now = datetime.now(UTC)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json=_close_report_payload(now),
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["closed"] is True
    assert body["already_closed"] is False
    assert body["reason"] == "device_close"
    assert body["device_id"] == "dev_test_01"
    assert body["session_id"] == "session-close-1"
    assert authority.calls == [
        {
            "actor_id": "person_a",
            "session_id": "session-close-1",
            "reason_code": "device_close",
        }
    ]
    row = store.get_device_media_session(session_id="session-close-1")
    assert row is not None
    assert row["closed_at"] is not None
    assert row["close_reason"] == "device_close"


@pytest.mark.asyncio
async def test_edge_close_report_replays_idempotently(tmp_path: Path) -> None:
    app, store, authority = _close_report_fixture(tmp_path)
    headers = {"X-Memoria-Edge-Device-Close-Token": _CLOSE_REPORT_TOKEN}
    payload = _close_report_payload(datetime.now(UTC))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json=payload,
        )
        assert first.status_code == 200, first.text
        authority.replay_already_closed = True
        second = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json=payload,
        )
    assert second.status_code == 200, second.text
    assert second.json()["already_closed"] is True
    row = store.get_device_media_session(session_id="session-close-1")
    assert row is not None
    assert row["close_reason"] == "device_close"


@pytest.mark.asyncio
async def test_edge_network_report_preserves_session_for_reconnect(tmp_path: Path) -> None:
    app, store, authority = _close_report_fixture(tmp_path)
    payload = {
        **_close_report_payload(datetime.now(UTC)),
        "reason": "network",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/internal/device-close",
            headers={"X-Memoria-Edge-Device-Close-Token": _CLOSE_REPORT_TOKEN},
            json=payload,
        )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "device_id": "dev_test_01",
        "session_id": "session-close-1",
        "closed": False,
        "reconnectable": True,
        "stale": False,
        "reason": "network",
    }
    assert authority.calls == []
    row = store.get_device_media_session(session_id="session-close-1")
    assert row is not None
    assert row["closed_at"] is None
    assert row["last_disconnected_at"] == payload["closed_at"]
    assert row["last_disconnect_reason"] == "network"


@pytest.mark.asyncio
async def test_stale_transport_report_cannot_touch_reconnected_epoch(tmp_path: Path) -> None:
    app, store, authority = _close_report_fixture(tmp_path)
    store.rotate_device_media_session_transport(
        session_id="session-close-1",
        expected_stream_epoch=1,
        stream_epoch=2,
        client_id="esp-installation-1",
        firmware_version="0.2.1",
        board_profile="memoria-atk-dnesp32s3-v1",
        runtime_profile_version=1,
        settings_version=0,
        audio_mode_requested="half_duplex_safe",
        ticket_jti="ticket-close-2",
        expires_at=(datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        counter_updated_at=datetime.now(UTC).isoformat(),
    )
    payload = {
        **_close_report_payload(datetime.now(UTC)),
        "reason": "superseded",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/internal/device-close",
            headers={"X-Memoria-Edge-Device-Close-Token": _CLOSE_REPORT_TOKEN},
            json=payload,
        )
    assert response.status_code == 200, response.text
    assert response.json()["stale"] is True
    assert response.json()["reconnectable"] is True
    assert authority.calls == []
    row = store.get_device_media_session(session_id="session-close-1")
    assert row is not None
    assert row["stream_epoch"] == 2
    assert row["closed_at"] is None
    assert row["last_disconnected_at"] is None


@pytest.mark.asyncio
async def test_edge_close_report_rejects_invalid_token_before_state(
    tmp_path: Path,
) -> None:
    app, store, authority = _close_report_fixture(tmp_path)
    payload = _close_report_payload(datetime.now(UTC))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        missing = await client.post("/v1/internal/device-close", json=payload)
        wrong = await client.post(
            "/v1/internal/device-close",
            headers={"X-Memoria-Edge-Device-Close-Token": "definitely-the-wrong-token"},
            json=payload,
        )
    assert missing.status_code == 401
    assert missing.json()["detail"]["code"] == "edge_close_report_token_invalid"
    assert wrong.status_code == 401
    assert wrong.json()["detail"]["code"] == "edge_close_report_token_invalid"
    assert authority.calls == []
    row = store.get_device_media_session(session_id="session-close-1")
    assert row is not None
    assert row["closed_at"] is None


@pytest.mark.asyncio
async def test_edge_close_report_rejects_stale_or_foreign_payload_shapes(
    tmp_path: Path,
) -> None:
    app, _store, authority = _close_report_fixture(tmp_path)
    headers = {"X-Memoria-Edge-Device-Close-Token": _CLOSE_REPORT_TOKEN}
    payload = _close_report_payload(datetime.now(UTC))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        legacy_minimal = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json={
                "session_id": "session-close-1",
                "device_id": "dev_test_01",
                "reason": "device_close",
            },
        )
        extra_field = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json={**payload, "bogus_field": True},
        )
        typed_epoch = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json={**payload, "stream_epoch": "one"},
        )
    assert legacy_minimal.status_code == 422
    assert extra_field.status_code == 422
    assert typed_epoch.status_code == 422
    assert authority.calls == []


@pytest.mark.asyncio
async def test_edge_close_report_rejects_account_and_device_identity_mismatch(
    tmp_path: Path,
) -> None:
    app, store, authority = _close_report_fixture(tmp_path)
    headers = {"X-Memoria-Edge-Device-Close-Token": _CLOSE_REPORT_TOKEN}
    base = _close_report_payload(datetime.now(UTC))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        foreign_account = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json={**base, "account_id": "person_b"},
        )
        foreign_device = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json={**base, "device_id": "dev_other"},
        )
    assert foreign_account.status_code == 409
    assert foreign_account.json()["detail"]["code"] == "media_session_account_mismatch"
    assert foreign_device.status_code == 404
    assert foreign_device.json()["detail"]["code"] == "media_session_not_found"
    assert authority.calls == []
    row = store.get_device_media_session(session_id="session-close-1")
    assert row is not None
    assert row["closed_at"] is None


@pytest.mark.asyncio
async def test_edge_close_report_rejects_stream_epoch_mismatch(tmp_path: Path) -> None:
    app, store, authority = _close_report_fixture(tmp_path)
    headers = {"X-Memoria-Edge-Device-Close-Token": _CLOSE_REPORT_TOKEN}
    payload = _close_report_payload(datetime.now(UTC))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        stale = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json={**payload, "stream_epoch": 2},
        )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "media_session_epoch_mismatch"
    assert authority.calls == []
    row = store.get_device_media_session(session_id="session-close-1")
    assert row is not None
    assert row["closed_at"] is None


@pytest.mark.asyncio
async def test_edge_close_report_projection_update_keeps_epoch_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed projection epoch at the final write must fail closed."""

    app, store, authority = _close_report_fixture(tmp_path)
    original_close = store.close_device_media_session

    def raced_close(**kwargs: object) -> bool:
        # Model a newer projection winning between the route's authoritative
        # read and its terminal SQLite update. The storage CAS must prevent the
        # stale Edge report from mutating it.
        assert kwargs["expected_stream_epoch"] == 1
        return False

    monkeypatch.setattr(store, "close_device_media_session", raced_close)
    original_get = store.get_device_media_session
    reads = 0

    def raced_get(*, session_id: str) -> dict[str, object] | None:
        nonlocal reads
        value = original_get(session_id=session_id)
        reads += 1
        if value is not None and reads >= 3:
            return {**value, "stream_epoch": 2}
        return value

    monkeypatch.setattr(store, "get_device_media_session", raced_get)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/internal/device-close",
            headers={"X-Memoria-Edge-Device-Close-Token": _CLOSE_REPORT_TOKEN},
            json=_close_report_payload(datetime.now(UTC)),
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "media_session_epoch_mismatch"
    assert len(authority.calls) == 1
    # Keep a real method reference in scope so static analysis catches a
    # future incompatible storage signature even though this test replaces it.
    assert callable(original_close)


@pytest.mark.asyncio
async def test_edge_close_report_semantic_validation_fails_closed(
    tmp_path: Path,
) -> None:
    app, _store, authority = _close_report_fixture(tmp_path)
    headers = {"X-Memoria-Edge-Device-Close-Token": _CLOSE_REPORT_TOKEN}
    now = datetime.now(UTC)
    base = _close_report_payload(now)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        not_connected = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json={**base, "connected": False},
        )
        garbage_time = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json={**base, "closed_at": "yesterday-ish"},
        )
        reversed_time = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json={**base, "connected_at": base["closed_at"], "closed_at": base["connected_at"]},
        )
        future_time = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json={
                **base,
                "closed_at": (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            },
        )
    assert not_connected.status_code == 422
    assert not_connected.json()["detail"]["code"] == "session_close_report_not_connected"
    assert garbage_time.status_code == 422
    assert garbage_time.json()["detail"]["code"] == "session_close_report_time_invalid"
    assert reversed_time.status_code == 422
    assert reversed_time.json()["detail"]["code"] == "session_close_report_time_invalid"
    assert future_time.status_code == 422
    assert future_time.json()["detail"]["code"] == "session_close_report_time_invalid"
    assert authority.calls == []


@pytest.mark.asyncio
async def test_edge_close_report_rejects_session_without_authoritative_voice_row(
    tmp_path: Path,
) -> None:
    app, store, authority = _close_report_fixture(tmp_path)
    assert store.delete_voice_session(session_id="session-close-1") is True
    headers = {"X-Memoria-Edge-Device-Close-Token": _CLOSE_REPORT_TOKEN}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/internal/device-close",
            headers=headers,
            json=_close_report_payload(datetime.now(UTC)),
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "media_session_actor_unresolved"
    assert authority.calls == []
    row = store.get_device_media_session(session_id="session-close-1")
    assert row is not None
    assert row["closed_at"] is None


@pytest.mark.asyncio
async def test_missing_subject_category_fails_closed(tmp_path: Path) -> None:
    app, _manifest = _profile_app(tmp_path, category=None)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for method, path, kwargs in (
            ("get", "/v1/devices/dev_test_01/settings", {}),
            (
                "patch",
                "/v1/devices/dev_test_01/settings",
                {"json": {"changes": {"learning_mode": "tutor_english"}}},
            ),
            (
                "post",
                "/v1/devices/dev_test_01/runtime-profile/ack",
                {"json": {"profile_version": 1, "accepted": True}},
            ),
            ("get", "/v1/devices/dev_test_01/diagnostics/latest", {}),
        ):
            response = await getattr(client, method)(path, **kwargs)
            assert response.status_code == 403, f"{method} {path}: {response.text}"
            assert response.json()["detail"]["code"] == "subject_category_unavailable"
