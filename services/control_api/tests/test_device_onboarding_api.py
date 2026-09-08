from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from packages.contracts.generated.python.multi_subject_contracts import (
    RuntimeProfileSignedV2,
    RuntimeProfileV2,
)
from pydantic import SecretStr
from services.common.miniprogram_gateway_ticket import verify_device_gateway_ticket
from services.control_api.app.account_gate import AccountOperationGate
from services.control_api.app.database import MemoryStore
from services.control_api.app.device_control import (
    RuntimeProfileLedger,
    stable_profile_fingerprint,
)
from services.control_api.app.routes import device_onboarding, interaction, media
from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
)
from services.device_fleet.bootstrap_domain import (
    b64url_encode,
    canonical_json_bytes,
    sha256_hex,
)
from services.device_fleet.bootstrap_service import DeviceOnboardingService
from services.device_fleet.tests.test_bootstrap_vertical_slice import (
    _fixture,
    _online,
)
from services.session_runtime.postgres_store import (
    SessionRuntimeConflict,
    SessionRuntimeContext,
)
from services.session_runtime.profile_service import sign_runtime_profile_payload
from services.session_runtime.service import (
    PersistentSessionDenied,
    PersistentSessionNotFound,
    PersistentSessionUnavailable,
    StartPersistentSessionCommand,
)
from services.session_runtime.subject_resolver import SubjectCandidate


def _app(service: DeviceOnboardingService | None, actor: str = "person_a") -> FastAPI:
    app = FastAPI()
    app.include_router(device_onboarding.router)
    app.include_router(media.device_router)
    if service is not None:
        app.state.device_onboarding_service = service
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedUser(
        user_id=actor, session_id="test-session", jti="test-jti"
    )
    return app


def _activate_device(
    service: DeviceOnboardingService,
    store: object,
    device_key: Ed25519PrivateKey,
    payload: object,
) -> dict[str, object]:
    _online(service, store, device_key, payload)  # type: ignore[arg-type]
    session = store.find_session_by_client(  # type: ignore[attr-defined]
        actor_id="person_a",
        client_onboarding_id="client_a",
    )
    assert session is not None
    claim = service.reserve_claim(
        actor_id="person_a",
        onboarding_session_id=session.onboarding_session_id,
        device_id=session.device_id,
        idempotency_key="api-media-claim",
        expected_state_version=session.state_version,
    )
    result = service.create_binding(
        actor_id="person_a",
        claim_id=str(claim["claim_id"]),
        onboarding_session_id=session.onboarding_session_id,
        initialization={
            "declared_mode": "self_use",
            "account_owner_person_id": "person_a",
            "primary_subject": {"person_id": "person_a", "relationship": "self"},
            "persona_selection": "companion_x",
            "service_preferences": {},
            "consent_offer_ids": [],
        },
        idempotency_key="api-media-binding",
    )
    manifest = result["manifest"]
    assert isinstance(manifest, dict)
    unsigned: dict[str, object] = {
        "device_id": "dev_test_01",
        "certificate_id": "cert_test_01",
        "binding_id": manifest["binding_id"],
        "binding_version": manifest["binding_version"],
        "activation_version": manifest["activation_version"],
        "config_hash": manifest["config_hash"],
        "firmware_version": "0.1.0",
        "monotonic_counter": 2,
        "applied_at": "2026-08-11T08:00:00Z",
    }
    service.accept_activation_ack(
        device_id="dev_test_01",
        ack={
            **unsigned,
            "signature": b64url_encode(device_key.sign(canonical_json_bytes(unsigned))),
        },
    )
    return manifest


def _client_payload() -> dict[str, object]:
    return {
        "platform": "wechat-miniprogram",
        "app_version": "0.1.0",
        "base_library_version": "3.0.0",
    }


_DIRECT_PROFILE_SIGNING_KEY = b"device-media-test-runtime-profile-signing-key"
_POLICY_TOKEN = "interaction-policy-token-that-is-long-enough"


def _signed_runtime_profile(
    *,
    session_id: str,
    actor_id: str,
    device_id: str,
    binding_id: str,
    binding_version: int,
    subject_id: str | None,
    session_epoch: int = 1,
    service_mode: str = "adult_companion",
    capabilities: tuple[str, ...] = ("chat", "memory_recall_private"),
    **overrides: object,
) -> RuntimeProfileSignedV2:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "signature_schema": "runtime-profile-v2",
        "runtime_profile_id": f"rp-{session_id}",
        "device_id": device_id,
        "session_id": session_id,
        "actor_id": actor_id,
        "binding_id": binding_id,
        "binding_version": binding_version,
        "active_subject_id": subject_id,
        "subject_revision": 1,
        "subject_category": "adult",
        "age_band": "adult",
        "speaker_state": "confirmed",
        "speaker_confidence": 0.99,
        "service_mode": service_mode,
        "persona_assignment_id": "starlight:v1",
        "persona": {
            "persona_id": "starlight",
            "version": 1,
            "relationship_stage": "new",
        },
        "policy_bundle_version": "runtime-profile-test-v2",
        "capabilities": list(capabilities),
        "obligations": [],
        "policy_receipt_ids": [],
        "session_epoch": session_epoch,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        **overrides,
    }
    unsigned = RuntimeProfileV2.model_validate(payload)
    wire = unsigned.model_dump(mode="json")
    return RuntimeProfileSignedV2.model_validate(
        {
            **wire,
            "signature": sign_runtime_profile_payload(
                wire, signing_key=_DIRECT_PROFILE_SIGNING_KEY
            ),
        }
    )


class _DirectSessionAuthority:
    """In-process stand-in for the Postgres Session runtime authority."""

    def __init__(
        self,
        *,
        binding_id: str,
        binding_version: int,
        subject_id: str | None,
        profile_overrides: dict[str, object] | None = None,
        start_error: Exception | None = None,
    ) -> None:
        self._binding_id = binding_id
        self._binding_version = binding_version
        self._subject_id = subject_id
        self._profile_overrides = profile_overrides or {}
        self._start_error = start_error
        self.started: list[StartPersistentSessionCommand] = []
        self.failed: list[tuple[str, str]] = []
        self.profile: RuntimeProfileSignedV2 | None = None
        self.context: SessionRuntimeContext | None = None

    async def start(
        self,
        command: StartPersistentSessionCommand,
        *,
        before_commit: Callable[[RuntimeProfileSignedV2], Awaitable[None]] | None = None,
    ) -> RuntimeProfileSignedV2:
        self.started.append(command)
        if self._start_error is not None:
            raise self._start_error
        kwargs: dict[str, object] = {
            "binding_id": self._binding_id,
            "binding_version": self._binding_version,
            "subject_id": self._subject_id,
        }
        kwargs.update(self._profile_overrides)
        profile = _signed_runtime_profile(
            session_id=command.session_id,
            actor_id=command.actor_id,
            device_id=command.device_id,
            **kwargs,
        )
        self.profile = profile
        self.context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
        if before_commit is not None:
            await before_commit(profile)
        return profile

    async def current(
        self,
        *,
        actor_id: str,
        session_id: str,
        now: datetime,
    ) -> tuple[RuntimeProfileSignedV2, SessionRuntimeContext]:
        del actor_id, now
        if self.profile is None or self.profile.session_id != session_id:
            raise PersistentSessionNotFound(session_id)
        assert self.context is not None
        return self.profile, self.context

    async def fail_session(
        self,
        *,
        actor_id: str,
        session_id: str,
        reason_code: str,
        now: datetime,
    ) -> None:
        del actor_id, now
        self.failed.append((session_id, reason_code))


def _direct_media_settings() -> tuple[SimpleNamespace, Ed25519PrivateKey]:
    signing_key = Ed25519PrivateKey.generate()
    pem = signing_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return (
        SimpleNamespace(
            device_media_runtime="direct_voice_core",
            device_media_direct_rollout_mode="allowlist",
            device_media_direct_canary_device_ids="dev_test_01",
            device_direct_media_wss_url="wss://edge.example/v1/device/media",
            device_runtime_profile_ttl_s=3600,
            streamcore_token_private_key_pem=SecretStr(pem),
            streamcore_token_key_id="media-2026-08",
            streamcore_token_ttl_s=120,
            jwt_issuer="memoria-control-api",
            device_gateway_ticket_ttl_s=300,
            livekit_agent_name="duplex-zh-agent",
            internal_token=lambda capability: _POLICY_TOKEN,
        ),
        signing_key,
    )


def _direct_memory(tmp_path: Path) -> MemoryStore:
    memory = MemoryStore(str(tmp_path / "memoria.sqlite3"))
    memory.initialize()
    memory.update_subject_profile(
        user_id="person_a",
        subject_category="adult",
        birth_year_band="adult",
        age_evidence_status="verified",
        now=datetime.now(UTC).isoformat(),
    )
    return memory


def test_device_media_stream_epoch_reservation_is_monotonic_and_migration_safe(
    tmp_path: Path,
) -> None:
    memory = _direct_memory(tmp_path)
    assert memory.next_device_media_stream_epoch(device_id="dev_epoch") == 1
    assert memory.next_device_media_stream_epoch(device_id="dev_epoch") == 2
    memory.create_device_media_session(
        session_id="session-historical",
        device_id="dev_historical",
        binding_id="binding-1",
        binding_version=1,
        subject_id="person_a",
        active_subject_id="person_a",
        client_id="client-1",
        runtime="direct_voice_core",
        protocol_version=2,
        stream_epoch=7,
        firmware_version="0.2.0",
        board_profile="memoria-atk-dnesp32s3-v1",
        runtime_profile_version=1,
        settings_version=0,
        audio_mode_requested="half_duplex_safe",
        ticket_jti="ticket-historical",
        created_at="2026-08-13T00:00:00Z",
        expires_at="2026-08-13T00:02:00Z",
    )
    assert memory.next_device_media_stream_epoch(device_id="dev_historical") == 8


def test_device_media_active_subject_migration_backfills_once_and_preserves_null(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-device-media.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE device_media_sessions (
                session_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
                subject_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                runtime TEXT NOT NULL CHECK (runtime IN ('livekit_compat', 'direct_voice_core')),
                protocol_version INTEGER NOT NULL CHECK (protocol_version IN (1, 2)),
                stream_epoch INTEGER NOT NULL CHECK (stream_epoch >= 1),
                firmware_version TEXT NOT NULL DEFAULT '',
                board_profile TEXT NOT NULL DEFAULT '',
                runtime_profile_version INTEGER NOT NULL DEFAULT 1
                    CHECK (runtime_profile_version >= 1),
                settings_version INTEGER NOT NULL DEFAULT 0
                    CHECK (settings_version >= 0),
                audio_mode_requested TEXT NOT NULL DEFAULT 'half_duplex_safe',
                audio_mode_effective TEXT NOT NULL DEFAULT '',
                aec_profile_version INTEGER,
                ticket_jti TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                connected_at TEXT,
                last_disconnected_at TEXT,
                last_disconnect_reason TEXT NOT NULL DEFAULT '',
                closed_at TEXT,
                close_reason TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            INSERT INTO device_media_sessions (
                session_id, device_id, binding_id, binding_version, subject_id,
                client_id, runtime, protocol_version, stream_epoch, ticket_jti,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-session",
                "legacy-device",
                "legacy-binding",
                1,
                "person_a",
                "legacy-client",
                "direct_voice_core",
                2,
                7,
                "legacy-ticket",
                "2026-08-16T00:00:00Z",
                "2026-08-16T00:02:00Z",
            ),
        )

    memory = MemoryStore(str(database_path))
    memory.initialize()
    legacy = memory.get_device_media_session(session_id="legacy-session")
    assert legacy is not None
    assert legacy["subject_id"] == "person_a"
    assert legacy["active_subject_id"] == "person_a"

    memory.create_device_media_session(
        session_id="unknown-safe-session",
        device_id="unknown-safe-device",
        binding_id="unknown-safe-binding",
        binding_version=1,
        subject_id="person_a",
        active_subject_id=None,
        client_id="unknown-safe-client",
        runtime="direct_voice_core",
        protocol_version=2,
        stream_epoch=1,
        firmware_version="0.2.0",
        board_profile="memoria-atk-dnesp32s3-v1",
        runtime_profile_version=1,
        settings_version=0,
        audio_mode_requested="half_duplex_safe",
        ticket_jti="unknown-safe-ticket",
        created_at="2026-08-17T00:00:00Z",
        expires_at="2026-08-17T00:02:00Z",
    )

    reopened = MemoryStore(str(database_path))
    reopened.initialize()
    unknown_safe = reopened.get_device_media_session(session_id="unknown-safe-session")
    assert unknown_safe is not None
    assert unknown_safe["subject_id"] == "person_a"
    assert unknown_safe["active_subject_id"] is None


def test_device_media_stream_epoch_reservation_fails_closed_at_uint32_limit(
    tmp_path: Path,
) -> None:
    memory = _direct_memory(tmp_path)
    maximum = (1 << 32) - 1
    with memory._connection() as connection:
        connection.execute(
            "INSERT INTO device_media_epoch_counters "
            "(device_id, last_stream_epoch, updated_at) VALUES (?, ?, ?)",
            ("dev_exhausted", maximum, datetime.now(UTC).isoformat()),
        )
    with pytest.raises(ValueError, match="stream_epoch exhausted"):
        memory.next_device_media_stream_epoch(device_id="dev_exhausted")


def _direct_app(
    service: DeviceOnboardingService,
    memory: MemoryStore,
    authority: _DirectSessionAuthority | None,
    settings_value: SimpleNamespace,
) -> FastAPI:
    app = _app(service)
    app.include_router(interaction.router)
    app.state.settings = settings_value
    app.state.memory_store = memory
    app.state.account_operations = AccountOperationGate()
    if authority is not None:
        app.state.session_runtime_service = authority
    return app


async def _post_direct_media_session(
    client: AsyncClient,
    service: DeviceOnboardingService,
    device_key: Ed25519PrivateKey,
    *,
    resume_session_id: str | None = None,
):
    challenge_response = await client.post(
        "/v1/devices/dev_test_01/media-challenge",
        headers={
            "X-Device-Certificate-ID": "cert_test_01",
            "X-Client-ID": "esp-installation-1",
        },
    )
    assert challenge_response.status_code == 200, challenge_response.text
    challenge = challenge_response.json()
    issued_at = datetime.fromisoformat(challenge["issued_at"].replace("Z", "+00:00"))
    signed_payload = service.media_challenge_signing_payload(
        challenge_id=challenge["challenge_id"],
        device_id="dev_test_01",
        certificate_id="cert_test_01",
        client_id="esp-installation-1",
        nonce=challenge["nonce"],
        issued_at=issued_at,
    )
    body: dict[str, object] = {
        "certificate_id": "cert_test_01",
        "challenge_id": challenge["challenge_id"],
        "nonce": challenge["nonce"],
        "signature": b64url_encode(device_key.sign(canonical_json_bytes(signed_payload))),
        "client_id": "esp-installation-1",
        "supported_protocol_versions": [2, 1],
    }
    if resume_session_id is not None:
        body["resume_session_id"] = resume_session_id
    return await client.post(
        "/v1/devices/dev_test_01/media-sessions",
        json=body,
    )


@pytest.mark.asyncio
async def test_missing_service_is_explicitly_unavailable() -> None:
    app = _app(None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/device-bootstrap/introspect",
            json={
                "qr_payload": "memoria-bootstrap:v1:x.y",
                "client_onboarding_id": "client_a",
                "client": _client_payload(),
            },
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "device_onboarding_unavailable"


@pytest.mark.asyncio
async def test_offline_registration_maps_public_key_contract_to_service() -> None:
    service, _store, _device_key, _payload = _fixture()
    app = _app(service)
    new_key = Ed25519PrivateKey.generate()
    public_key = b64url_encode(new_key.public_key().public_bytes_raw())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/device-fleet/offline-mock/devices",
            json={
                "device_id": "dev_http_registration",
                "certificate_id": "cert_http_registration",
                "public_key": public_key,
                "product_model": "memoria-atk-dnesp32s3-v1",
                "hardware_revision": "rev-a",
                "firmware_version": "0.1.0",
                "firmware_security_version": 1,
                "capability_manifest_hash": sha256_hex(b"http-registration"),
                "minimum_firmware_security_version": 1,
            },
        )
    assert response.status_code == 201, response.text
    assert response.json() == {
        "device_id": "dev_http_registration",
        "certificate_id": "cert_http_registration",
        "public_key": public_key,
        "lifecycle_status": "manufactured",
    }


@pytest.mark.asyncio
async def test_real_api_response_shapes_match_miniprogram_contracts() -> None:
    service, store, device_key, payload = _fixture()
    app = _app(service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        from services.device_fleet.bootstrap_domain import encode_bootstrap_qr

        introspected = await client.post(
            "/v1/device-bootstrap/introspect",
            json={
                "qr_payload": encode_bootstrap_qr(payload, device_key),
                "client_onboarding_id": "api-client-a",
                "client": _client_payload(),
            },
        )
        assert introspected.status_code == 200, introspected.text
        onboarding = introspected.json()
        assert set(onboarding) == {
            "onboarding_session_id",
            "state",
            "state_version",
            "activation_version",
            "expires_at",
            "device",
            "provisioning",
            "mobile_nonce",
        }
        assert set(onboarding["device"]) == {
            "device_id",
            "display_tail",
            "model",
            "firmware_version",
            "claim_status",
        }
        assert onboarding["device"]["claim_status"] in {
            "unclaimed",
            "reserved",
            "bound",
            "suspended",
            "revoked",
        }
        session_id = onboarding["onboarding_session_id"]
        session_response = await client.get(f"/v1/device-bootstrap/{session_id}")
        assert session_response.status_code == 200
        assert set(session_response.json()) == {
            "onboarding_session_id",
            "state",
            "state_version",
            "activation_version",
            "expires_at",
            "device",
            "provisioning",
        }

    # Complete the device proof through the service seam, then exercise the
    # public user claim endpoint with the state_version returned by the store.
    _online(
        service,
        store,
        device_key,
        replace(payload, bootstrap_nonce="ZmVkY2JhOTg3NjU0MzIxMA"),
        actor_id="person_a",
        client_id="api-client-b",
    )
    second = store.find_session_by_client(actor_id="person_a", client_onboarding_id="api-client-b")
    assert second is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        claimed = await client.post(
            "/v1/device-claims",
            json={
                "onboarding_session_id": second.onboarding_session_id,
                "device_id": second.device_id,
                "idempotency_key": "api-claim-01",
                "expected_state_version": second.state_version,
            },
        )
        assert claimed.status_code == 201, claimed.text
        claim = claimed.json()
        assert set(claim) == {
            "claim_id",
            "onboarding_session_id",
            "device_id",
            "status",
            "expires_at",
        }
        assert "binding_version" not in claim
        assert "state_version" not in claim
        resumed_claim = await client.get(f"/v1/device-bootstrap/{second.onboarding_session_id}")
        assert resumed_claim.status_code == 200
        assert resumed_claim.json()["claim_id"] == claim["claim_id"]

        # The development slice uses the explicit offline authority.  The
        # user-facing activation query is still exercised over HTTP.
        service.create_binding(
            actor_id="person_a",
            claim_id=claim["claim_id"],
            onboarding_session_id=second.onboarding_session_id,
            initialization={
                "declared_mode": "self_use",
                "account_owner_person_id": "person_a",
                "primary_subject": {"person_id": "person_a", "relationship": "self"},
                "persona_selection": "companion_x",
                "service_preferences": {},
                "consent_offer_ids": [],
            },
            idempotency_key="api-binding-01",
        )
        activation = await client.get("/v1/device-activations/dev_test_01")
        assert activation.status_code == 200, activation.text
        assert set(activation.json()) == {
            "activation_id",
            "device_id",
            "binding_id",
            "binding_version",
            "activation_version",
            "status",
            "config_hash",
            "acknowledged_at",
        }
        assert "issued_at" not in activation.json()
        assert "expires_at" not in activation.json()
        assert "downloaded_at" not in activation.json()
        assert "applied_at" not in activation.json()
        assert "ack_counter" not in activation.json()
        resumed_binding = await client.get(f"/v1/device-bootstrap/{second.onboarding_session_id}")
        assert resumed_binding.status_code == 200
        assert resumed_binding.json()["binding_id"] == activation.json()["binding_id"]
        assert resumed_binding.json()["activation_status"] == "manifest_ready"


@pytest.mark.asyncio
async def test_device_media_session_uses_fleet_proof_and_device_only_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, device_key, payload = _fixture()
    manifest = _activate_device(service, store, device_key, payload)
    app = _app(service)
    secret = "device-ticket-secret-material-that-is-long-enough"
    app.state.settings = SimpleNamespace(
        device_media_gateway_url="wss://media.example/v1/device/media",
        memoria_device_gateway_ticket_secret=SecretStr(secret),
        device_gateway_ticket_ttl_s=300,
        livekit_agent_name="duplex-zh-agent",
    )

    class Memory:
        @staticmethod
        def is_account_unavailable(*, user_id: str) -> bool:
            assert user_id == "person_a"
            return False

    class AccountOperations:
        @asynccontextmanager
        async def write(self, account_id: str):
            assert account_id == "person_a"
            yield

    app.state.memory_store = Memory()
    app.state.account_operations = AccountOperations()

    async def fake_create(
        body: object,
        request: object,
        user: AuthenticatedUser,
        *,
        device_id: str | None = None,
        binding_version: int | None = None,
    ) -> media.MediaSessionResponse:
        del body, request
        assert user.user_id == "person_a"
        assert device_id == "dev_test_01"
        assert binding_version is None
        return media.MediaSessionResponse(
            session_id="session-device-api",
            media_runtime="livekit",
            stream_epoch=1,
            fallback={"media_runtime": "livekit"},
            livekit={
                "url": "wss://livekit.example",
                "room_name": "voice-session-device-api",
                "participant_token": "must-not-leak",
            },
            device_id=device_id,
        )

    monkeypatch.setattr(media, "_create", fake_create)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        challenge_response = await client.post(
            "/v1/devices/dev_test_01/media-challenge",
            headers={
                "X-Device-Certificate-ID": "cert_test_01",
                "X-Client-ID": "esp-installation-1",
            },
        )
        assert challenge_response.status_code == 200, challenge_response.text
        challenge = challenge_response.json()
        issued_at = datetime.fromisoformat(challenge["issued_at"].replace("Z", "+00:00"))
        signed_payload = service.media_challenge_signing_payload(
            challenge_id=challenge["challenge_id"],
            device_id="dev_test_01",
            certificate_id="cert_test_01",
            client_id="esp-installation-1",
            nonce=challenge["nonce"],
            issued_at=issued_at,
        )
        session_response = await client.post(
            "/v1/devices/dev_test_01/media-sessions",
            json={
                "certificate_id": "cert_test_01",
                "challenge_id": challenge["challenge_id"],
                "nonce": challenge["nonce"],
                "signature": b64url_encode(device_key.sign(canonical_json_bytes(signed_payload))),
                "client_id": "esp-installation-1",
                "supported_protocol_versions": [2, 1],
            },
        )
        replay = await client.post(
            "/v1/devices/dev_test_01/media-sessions",
            json={
                "certificate_id": "cert_test_01",
                "challenge_id": challenge["challenge_id"],
                "nonce": challenge["nonce"],
                "signature": b64url_encode(device_key.sign(canonical_json_bytes(signed_payload))),
                "client_id": "esp-installation-1",
                "supported_protocol_versions": [2, 1],
            },
        )
    assert session_response.status_code == 200, session_response.text
    response = session_response.json()
    assert response["websocket_url"] == "wss://media.example/v1/device/media"
    assert response["runtime"] == "livekit_compat"
    assert response["protocol_version"] == 1
    assert response["interaction_authority"] == "python_authoritative"
    assert response["uplink"] == {
        "codec": "opus",
        "sample_rate": 16000,
        "channels": 1,
        "frame_ms": 20,
    }
    assert response["downlink"]["sample_rate"] == 24000
    assert "participant_token" not in session_response.text
    assert "room_name" not in session_response.text
    claims = verify_device_gateway_ticket(
        response["media_token"],
        secret=secret,
    )
    assert claims.device_id == "dev_test_01"
    assert claims.client_id == "esp-installation-1"
    assert claims.binding_id == manifest["binding_id"]
    assert claims.binding_version == manifest["binding_version"]
    assert claims.stream_epoch == 1
    assert replay.status_code == 409


@pytest.mark.asyncio
async def test_direct_device_media_session_never_touches_livekit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, store, device_key, payload = _fixture()
    manifest = _activate_device(service, store, device_key, payload)
    settings_value, signing_key = _direct_media_settings()
    memory = _direct_memory(tmp_path)
    authority = _DirectSessionAuthority(
        binding_id=str(manifest["binding_id"]),
        binding_version=int(manifest["binding_version"]),
        subject_id="person_a",
    )
    app = _direct_app(service, memory, authority, settings_value)

    async def fail_livekit_create(
        body: object,
        request: object,
        user: AuthenticatedUser,
        *,
        device_id: str | None = None,
        binding_version: int | None = None,
    ) -> media.MediaSessionResponse:
        del body, request, user, device_id, binding_version
        raise AssertionError("direct device media must not create a LiveKit session")

    monkeypatch.setattr(media, "_create", fail_livekit_create)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        challenge_response = await client.post(
            "/v1/devices/dev_test_01/media-challenge",
            headers={
                "X-Device-Certificate-ID": "cert_test_01",
                "X-Client-ID": "esp-installation-1",
            },
        )
        assert challenge_response.status_code == 200, challenge_response.text
        challenge = challenge_response.json()
        issued_at = datetime.fromisoformat(challenge["issued_at"].replace("Z", "+00:00"))
        signed_payload = service.media_challenge_signing_payload(
            challenge_id=challenge["challenge_id"],
            device_id="dev_test_01",
            certificate_id="cert_test_01",
            client_id="esp-installation-1",
            nonce=challenge["nonce"],
            issued_at=issued_at,
        )
        session_response = await client.post(
            "/v1/devices/dev_test_01/media-sessions",
            json={
                "certificate_id": "cert_test_01",
                "challenge_id": challenge["challenge_id"],
                "nonce": challenge["nonce"],
                "signature": b64url_encode(device_key.sign(canonical_json_bytes(signed_payload))),
                "client_id": "esp-installation-1",
                "supported_protocol_versions": [2, 1],
            },
        )
        replay = await client.post(
            "/v1/devices/dev_test_01/media-sessions",
            json={
                "certificate_id": "cert_test_01",
                "challenge_id": challenge["challenge_id"],
                "nonce": challenge["nonce"],
                "signature": b64url_encode(device_key.sign(canonical_json_bytes(signed_payload))),
                "client_id": "esp-installation-1",
                "supported_protocol_versions": [2, 1],
            },
        )
    assert session_response.status_code == 200, session_response.text
    response = session_response.json()
    assert response["websocket_url"] == "wss://edge.example/v1/device/media"
    assert response["runtime"] == "direct_voice_core"
    assert response["protocol_version"] == 2
    assert response["interaction_authority"] == "python_authoritative"
    assert response["downlink"] == {
        "codec": "opus",
        "sample_rate": 24000,
        "channels": 1,
        "frame_ms": 20,
    }
    assert response["expires_in"] == 120
    assert "participant_token" not in session_response.text
    assert "room_name" not in session_response.text
    assert "media_gateway" not in session_response.text
    claims = jwt.decode(
        response["media_token"],
        signing_key.public_key(),
        algorithms=["EdDSA"],
        audience="memoria-media-edge",
        issuer="memoria-control-api",
    )
    assert claims["typ"] == "memoria_device_media"
    assert claims["session_id"] == response["session_id"]
    assert claims["sub"] == "person_a"
    assert claims["device_id"] == "dev_test_01"
    assert claims["client_id"] == "esp-installation-1"
    assert claims["binding_id"] == manifest["binding_id"]
    assert claims["binding_version"] == manifest["binding_version"]
    assert claims["client_type"] == "device"
    assert claims["stream_epoch"] == 1
    assert claims["jti"] and claims["iat"] and claims["nbf"] and claims["exp"]
    assert authority.profile is not None
    assert claims["binding_id"] == authority.profile.binding_id
    assert claims["binding_version"] == authority.profile.binding_version
    assert claims["subject_id"] == authority.profile.active_subject_id
    assert claims["device_settings"] == {
        "settings_version": 0,
        "volume_limit": 30,
        "screen_brightness": 80,
        "night_mode": False,
        "do_not_disturb": False,
        "learning_mode": "off",
        "audio_mode": "half_duplex_safe",
        "wake_mode": "button_or_keyword",
        "allowed_barge_in": ["button", "keyword"],
        "wake_word_id": "mo_li",
        "wake_word_pinyin": "mo li",
        "wake_word_display": "茉莉",
    }
    ledger_entry = RuntimeProfileLedger(memory).current("dev_test_01")
    assert ledger_entry is not None
    assert ledger_entry.profile_version == claims["runtime_profile_version"]
    assert ledger_entry.content_fingerprint == stable_profile_fingerprint(
        authority.profile.model_dump(mode="json")
    )
    assert response["runtime_profile_version"] == claims["runtime_profile_version"]
    assert authority.started[0].candidates == (
        SubjectCandidate(subject_id="person_a", confidence=1.0),
    )
    voice_session = memory.get_voice_session_by_id(session_id=response["session_id"])
    assert voice_session is not None
    assert voice_session["room_name"] == f"voice-{response['session_id']}"
    assert voice_session["voice_backend"] == "cascade"
    assert voice_session["interaction_mode"] == "companion"
    assert voice_session["companion_style_id"] == "starlight"
    record = memory.get_device_media_session(session_id=response["session_id"])
    assert record is not None
    assert record["runtime"] == "direct_voice_core"
    assert record["protocol_version"] == 2
    assert record["stream_epoch"] == 1
    assert record["firmware_version"] == "0.1.0"
    assert record["board_profile"] == "memoria-devkit"
    assert record["runtime_profile_version"] == claims["runtime_profile_version"]
    assert record["subject_id"] == "person_a"
    assert record["active_subject_id"] == "person_a"
    assert record["settings_version"] == 0
    assert record["audio_mode_requested"] == "half_duplex_safe"
    assert record["ticket_jti"] == claims["jti"]
    assert replay.status_code == 409


@pytest.mark.asyncio
async def test_direct_runtime_requires_explicit_v2_advertisement_and_legacy_defaults_to_v1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, store, device_key, payload = _fixture()
    _activate_device(service, store, device_key, payload)
    settings_value, _ = _direct_media_settings()
    # Capability negotiation must choose the legacy endpoint before touching
    # the direct Session authority when an old firmware omits the new field.
    settings_value.device_media_gateway_url = "wss://legacy.example/v1/device/media"
    settings_value.memoria_device_gateway_ticket_secret = SecretStr(
        "legacy-device-gateway-ticket-secret-32chars"
    )
    memory = _direct_memory(tmp_path)
    app = _direct_app(service, memory, None, settings_value)

    async def fake_legacy_create(*args: object, **kwargs: object) -> media.MediaSessionResponse:
        del args, kwargs
        return media.MediaSessionResponse(
            session_id="legacy-session",
            media_runtime="livekit",
            stream_epoch=1,
            fallback={"media_runtime": "livekit"},
            livekit={
                "url": "wss://livekit.example",
                "room_name": "legacy-room",
                "participant_token": "server-only",
            },
            device_id="dev_test_01",
        )

    monkeypatch.setattr(media, "_create", fake_legacy_create)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        challenge_response = await client.post(
            "/v1/devices/dev_test_01/media-challenge",
            headers={
                "X-Device-Certificate-ID": "cert_test_01",
                "X-Client-ID": "esp-installation-1",
            },
        )
        challenge = challenge_response.json()
        issued_at = datetime.fromisoformat(challenge["issued_at"].replace("Z", "+00:00"))
        signed_payload = service.media_challenge_signing_payload(
            challenge_id=challenge["challenge_id"],
            device_id="dev_test_01",
            certificate_id="cert_test_01",
            client_id="esp-installation-1",
            nonce=challenge["nonce"],
            issued_at=issued_at,
        )
        response = await client.post(
            "/v1/devices/dev_test_01/media-sessions",
            json={
                "certificate_id": "cert_test_01",
                "challenge_id": challenge["challenge_id"],
                "nonce": challenge["nonce"],
                "signature": b64url_encode(
                    device_key.sign(canonical_json_bytes(signed_payload))
                ),
                "client_id": "esp-installation-1",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["runtime"] == "livekit_compat"
    assert response.json()["protocol_version"] == 1
    assert response.json()["websocket_url"] == "wss://legacy.example/v1/device/media"


@pytest.mark.asyncio
async def test_direct_canary_allowlist_keeps_non_matching_v2_device_on_legacy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, store, device_key, payload = _fixture()
    _activate_device(service, store, device_key, payload)
    settings_value, _ = _direct_media_settings()
    settings_value.device_media_direct_canary_device_ids = "dev_other"
    settings_value.device_media_gateway_url = "wss://legacy.example/v1/device/media"
    settings_value.memoria_device_gateway_ticket_secret = SecretStr(
        "legacy-device-gateway-ticket-secret-32chars"
    )
    memory = _direct_memory(tmp_path)
    app = _direct_app(service, memory, None, settings_value)

    async def fake_legacy_create(*args: object, **kwargs: object) -> media.MediaSessionResponse:
        del args, kwargs
        return media.MediaSessionResponse(
            session_id="legacy-canary-fence",
            media_runtime="livekit",
            stream_epoch=1,
            fallback={"media_runtime": "livekit"},
            livekit={
                "url": "wss://livekit.example",
                "room_name": "legacy-room",
                "participant_token": "server-only",
            },
            device_id="dev_test_01",
        )

    monkeypatch.setattr(media, "_create", fake_legacy_create)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await _post_direct_media_session(client, service, device_key)

    assert response.status_code == 200, response.text
    assert response.json()["runtime"] == "livekit_compat"
    assert response.json()["protocol_version"] == 1
    assert response.json()["websocket_url"] == "wss://legacy.example/v1/device/media"


@pytest.mark.asyncio
async def test_direct_reconnect_reuses_session_and_advances_only_transport_epoch(
    tmp_path: Path,
) -> None:
    service, store, device_key, payload = _fixture()
    manifest = _activate_device(service, store, device_key, payload)
    settings_value, signing_key = _direct_media_settings()
    memory = _direct_memory(tmp_path)
    authority = _DirectSessionAuthority(
        binding_id=str(manifest["binding_id"]),
        binding_version=int(manifest["binding_version"]),
        subject_id="person_a",
    )
    app = _direct_app(service, memory, authority, settings_value)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await _post_direct_media_session(client, service, device_key)
        assert first.status_code == 200, first.text
        first_body = first.json()
        resumed = await _post_direct_media_session(
            client,
            service,
            device_key,
            resume_session_id=str(first_body["session_id"]),
        )
    assert resumed.status_code == 200, resumed.text
    resumed_body = resumed.json()
    assert resumed_body["session_id"] == first_body["session_id"]
    assert resumed_body["stream_epoch"] == first_body["stream_epoch"] + 1
    assert resumed_body["downlink"] == {
        "codec": "opus",
        "sample_rate": 24000,
        "channels": 1,
        "frame_ms": 20,
    }
    assert len(authority.started) == 1
    resumed_claims = jwt.decode(
        str(resumed_body["media_token"]),
        signing_key.public_key(),
        algorithms=["EdDSA"],
        audience="memoria-media-edge",
        issuer="memoria-control-api",
    )
    assert resumed_claims["session_id"] == first_body["session_id"]
    assert resumed_claims["stream_epoch"] == resumed_body["stream_epoch"]
    record = memory.get_device_media_session(session_id=str(first_body["session_id"]))
    assert record is not None
    assert record["stream_epoch"] == resumed_body["stream_epoch"]
    assert record["ticket_jti"] == resumed_claims["jti"]


@pytest.mark.asyncio
async def test_direct_reconnect_refreshes_frozen_companion_from_current_profile(
    tmp_path: Path,
) -> None:
    service, store, device_key, payload = _fixture()
    manifest = _activate_device(service, store, device_key, payload)
    settings_value, _signing_key = _direct_media_settings()
    memory = _direct_memory(tmp_path)
    authority = _DirectSessionAuthority(
        binding_id=str(manifest["binding_id"]),
        binding_version=int(manifest["binding_version"]),
        subject_id="person_a",
    )
    app = _direct_app(service, memory, authority, settings_value)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await _post_direct_media_session(client, service, device_key)
        assert first.status_code == 200, first.text
        first_session = memory.get_voice_session_by_id(session_id=str(first.json()["session_id"]))
        assert first_session is not None
        assert first_session["companion_style_id"] == "starlight"
        memory.update_profile(
            user_id="person_a",
            values={"companion_id": "xuanmo"},
            now=datetime.now(UTC).isoformat(),
        )
        resumed = await _post_direct_media_session(
            client,
            service,
            device_key,
            resume_session_id=str(first.json()["session_id"]),
        )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["session_id"] == first.json()["session_id"]
    refreshed = memory.get_voice_session_by_id(session_id=str(first.json()["session_id"]))
    assert refreshed is not None
    assert refreshed["companion_style_id"] == "xuanmo"


@pytest.mark.asyncio
async def test_direct_unknown_safe_session_keeps_binding_owner_and_empty_runtime_subject(
    tmp_path: Path,
) -> None:
    service, store, device_key, payload = _fixture()
    manifest = _activate_device(service, store, device_key, payload)
    settings_value, signing_key = _direct_media_settings()
    memory = _direct_memory(tmp_path)
    authority = _DirectSessionAuthority(
        binding_id=str(manifest["binding_id"]),
        binding_version=int(manifest["binding_version"]),
        subject_id=None,
        profile_overrides={
            "subject_revision": 0,
            "subject_category": "unknown",
            "age_band": "unknown",
            "speaker_state": "unconfirmed",
            "speaker_confidence": None,
            "service_mode": "unknown_safe",
            "capabilities": ["chat"],
        },
    )
    app = _direct_app(service, memory, authority, settings_value)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await _post_direct_media_session(client, service, device_key)
        assert created.status_code == 200, created.text
        created_body = created.json()
        resumed = await _post_direct_media_session(
            client,
            service,
            device_key,
            resume_session_id=str(created_body["session_id"]),
        )

    assert resumed.status_code == 200, resumed.text
    resumed_body = resumed.json()
    assert created_body["subject_id"] is None
    assert resumed_body["subject_id"] is None
    assert resumed_body["stream_epoch"] == created_body["stream_epoch"] + 1
    for body in (created_body, resumed_body):
        claims = jwt.decode(
            str(body["media_token"]),
            signing_key.public_key(),
            algorithms=["EdDSA"],
            audience="memoria-media-edge",
            issuer="memoria-control-api",
        )
        assert claims["sub"] == "person_a"
        assert claims["subject_id"] == ""

    record = memory.get_device_media_session(session_id=str(created_body["session_id"]))
    assert record is not None
    assert record["subject_id"] == "person_a"
    assert record["active_subject_id"] is None
    assert memory.delete_device_media_sessions(subject_id="person_a") == 1


@pytest.mark.asyncio
async def test_direct_device_media_session_creates_authority_before_ticket_and_policy_readable(
    tmp_path: Path,
) -> None:
    service, store, device_key, payload = _fixture()
    manifest = _activate_device(service, store, device_key, payload)
    settings_value, signing_key = _direct_media_settings()
    memory = _direct_memory(tmp_path)
    authority = _DirectSessionAuthority(
        binding_id=str(manifest["binding_id"]),
        binding_version=int(manifest["binding_version"]),
        subject_id="person_a",
    )
    app = _direct_app(service, memory, authority, settings_value)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        session_response = await _post_direct_media_session(client, service, device_key)
        assert session_response.status_code == 200, session_response.text
        response = session_response.json()
        session_id = str(response["session_id"])
        # The authority start precedes ticket issuance and carries the
        # server-verified binding primary subject as the resolution candidate.
        assert len(authority.started) == 1
        command = authority.started[0]
        assert command.device_id == "dev_test_01"
        assert command.actor_id == "person_a"
        assert command.expected_binding_version == int(manifest["binding_version"])
        assert command.requested_capabilities == ("chat",)
        assert command.candidates == (SubjectCandidate(subject_id="person_a", confidence=1.0),)
        assert authority.profile is not None
        # Ticket claims must exactly equal the authoritative profile facts.
        claims = jwt.decode(
            str(response["media_token"]),
            signing_key.public_key(),
            algorithms=["EdDSA"],
            audience="memoria-media-edge",
            issuer="memoria-control-api",
        )
        assert claims["session_id"] == session_id
        assert claims["binding_id"] == authority.profile.binding_id
        assert claims["binding_version"] == authority.profile.binding_version
        assert claims["subject_id"] == authority.profile.active_subject_id
        assert claims["runtime_profile_version"] == response["runtime_profile_version"]
        ledger_entry = RuntimeProfileLedger(memory).current("dev_test_01")
        assert ledger_entry is not None
        assert ledger_entry.profile_version == claims["runtime_profile_version"]
        assert ledger_entry.content_fingerprint == stable_profile_fingerprint(
            authority.profile.model_dump(mode="json")
        )
        # The just-created session is immediately readable by the Agent's
        # /session-policy path with the same signed profile facts.
        policy_response = await client.post(
            "/v1/interaction/session-policy",
            headers={"X-Memoria-Internal-Token": _POLICY_TOKEN},
            json={"session_id": session_id},
        )
        assert policy_response.status_code == 200, policy_response.text
        policy = policy_response.json()
        assert policy["interaction_mode"] == "companion"
        assert policy["history_eligible"] is True
        runtime_profile = policy["runtime_profile"]
        assert runtime_profile["session_id"] == session_id
        assert runtime_profile["binding_id"] == claims["binding_id"]
        assert runtime_profile["binding_version"] == claims["binding_version"]
        assert runtime_profile["active_subject_id"] == claims["subject_id"]
        assert runtime_profile["session_epoch"] == authority.profile.session_epoch


@pytest.mark.asyncio
async def test_direct_device_media_session_requires_session_runtime_authority(
    tmp_path: Path,
) -> None:
    service, store, device_key, payload = _fixture()
    _activate_device(service, store, device_key, payload)
    settings_value, _ = _direct_media_settings()
    memory = _direct_memory(tmp_path)
    app = _direct_app(service, memory, None, settings_value)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        session_response = await _post_direct_media_session(client, service, device_key)
    assert session_response.status_code == 503, session_response.text
    assert session_response.json()["detail"]["code"] == "session_runtime_authority_unavailable"
    assert memory.get_voice_session_by_id(session_id="") is None
    assert memory.get_device_media_session(session_id="") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_error", "expected_status", "expected_code"),
    [
        (PersistentSessionDenied("denied"), 403, "session_runtime_authority_denied"),
        (SessionRuntimeConflict("conflict"), 409, "session_runtime_conflict"),
        (
            PersistentSessionUnavailable("unavailable"),
            503,
            "session_runtime_authority_unavailable",
        ),
    ],
)
async def test_direct_device_media_session_fails_closed_on_authority_errors(
    tmp_path: Path,
    start_error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    service, store, device_key, payload = _fixture()
    manifest = _activate_device(service, store, device_key, payload)
    settings_value, _ = _direct_media_settings()
    memory = _direct_memory(tmp_path)
    authority = _DirectSessionAuthority(
        binding_id=str(manifest["binding_id"]),
        binding_version=int(manifest["binding_version"]),
        subject_id="person_a",
        start_error=start_error,
    )
    app = _direct_app(service, memory, authority, settings_value)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        session_response = await _post_direct_media_session(client, service, device_key)
    assert session_response.status_code == expected_status, session_response.text
    assert session_response.json()["detail"]["code"] == expected_code
    # The before_commit callback never ran: no sqlite read model and no
    # ticket/device-media record were created for the failed authority start.
    assert authority.profile is None
    assert authority.failed == []
    assert memory.get_device_media_session(session_id="") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_overrides", "expected_fields"),
    [
        ({"binding_id": "binding-other"}, ["binding_id"]),
        ({"binding_version": 99}, ["binding_version"]),
    ],
)
async def test_direct_device_media_session_ticket_authority_mismatch_fails_closed(
    tmp_path: Path,
    profile_overrides: dict[str, object],
    expected_fields: list[str],
) -> None:
    service, store, device_key, payload = _fixture()
    manifest = _activate_device(service, store, device_key, payload)
    settings_value, _ = _direct_media_settings()
    memory = _direct_memory(tmp_path)
    authority = _DirectSessionAuthority(
        binding_id=str(manifest["binding_id"]),
        binding_version=int(manifest["binding_version"]),
        subject_id="person_a",
        profile_overrides=profile_overrides,
    )
    app = _direct_app(service, memory, authority, settings_value)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        session_response = await _post_direct_media_session(client, service, device_key)
    assert session_response.status_code == 503, session_response.text
    detail = session_response.json()["detail"]
    assert detail["code"] == "ticket_authority_mismatch"
    assert detail["fields"] == expected_fields
    # The authority session was invalidated and no credential was minted.
    assert len(authority.failed) == 1
    assert authority.failed[0][1] == "ticket_authority_mismatch"
    assert memory.get_device_media_session(session_id="") is None
    assert memory.get_voice_session_by_id(session_id="") is None


@pytest.mark.asyncio
async def test_direct_device_media_session_ledger_projection_version_is_stable(
    tmp_path: Path,
) -> None:
    service, store, device_key, payload = _fixture()
    manifest = _activate_device(service, store, device_key, payload)
    settings_value, signing_key = _direct_media_settings()
    memory = _direct_memory(tmp_path)
    authority = _DirectSessionAuthority(
        binding_id=str(manifest["binding_id"]),
        binding_version=int(manifest["binding_version"]),
        subject_id="person_a",
    )
    app = _direct_app(service, memory, authority, settings_value)
    versions: list[int] = []
    stream_epochs: list[int] = []
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for _ in range(2):
            session_response = await _post_direct_media_session(client, service, device_key)
            assert session_response.status_code == 200, session_response.text
            response = session_response.json()
            claims = jwt.decode(
                str(response["media_token"]),
                signing_key.public_key(),
                algorithms=["EdDSA"],
                audience="memoria-media-edge",
                issuer="memoria-control-api",
            )
            versions.append(int(claims["runtime_profile_version"]))
            stream_epochs.append(int(claims["stream_epoch"]))
    # A refresh with identical stable profile content must not advance the
    # device-visible projection version.
    assert versions == [1, 1]
    assert stream_epochs == [1, 2]
    ledger_entry = RuntimeProfileLedger(memory).current("dev_test_01")
    assert ledger_entry is not None
    assert ledger_entry.profile_version == 1
    assert len(authority.started) == 2


@pytest.mark.asyncio
async def test_direct_device_media_projection_failure_invalidates_authority_and_cleans_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, device_store, device_key, payload = _fixture()
    manifest = _activate_device(service, device_store, device_key, payload)
    settings_value, _ = _direct_media_settings()
    memory = _direct_memory(tmp_path)
    authority = _DirectSessionAuthority(
        binding_id=str(manifest["binding_id"]),
        binding_version=int(manifest["binding_version"]),
        subject_id="person_a",
    )
    original_add_voice_session = memory.add_voice_session

    def fail_after_partial_projection(**kwargs: object) -> None:
        original_add_voice_session(**kwargs)
        raise RuntimeError("simulated sqlite projection failure")

    monkeypatch.setattr(memory, "add_voice_session", fail_after_partial_projection)
    app = _direct_app(service, memory, authority, settings_value)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await _post_direct_media_session(client, service, device_key)

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "direct_media_projection_unavailable"
    assert authority.profile is not None
    session_id = authority.profile.session_id
    assert authority.failed == [(session_id, "direct_media_projection_unavailable")]
    assert memory.get_voice_session_by_id(session_id=session_id) is None
    assert memory.get_device_media_session(session_id=session_id) is None
