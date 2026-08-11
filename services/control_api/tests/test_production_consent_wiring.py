from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.device_binding_token import mint_device_binding_token
from services.control_api.app.main import create_app
from services.identity.authority import (
    RejectingConsentSnapshotResolver,
    RejectingTransferEvidenceVerifier,
)
from services.identity.service import IdentityService


def _default_app(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_IDENTITY_DB_PATH",
        str(tmp_path / "identity.sqlite3"),
    )
    monkeypatch.setenv(
        "MEMORIA_AUTH_SECRET",
        "production-consent-auth-secret-long-enough-0123456789",
    )
    monkeypatch.setenv(
        "MEMORIA_TRANSFER_EVIDENCE_SECRET",
        "production-consent-transfer-secret-32-bytes",
    )
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    return create_app()


@pytest.mark.asyncio
async def test_binding_fails_with_stable_503_when_consent_authority_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _default_app(monkeypatch, tmp_path)
    app.state.identity_service = IdentityService(
        app.state.identity_store,
        transfer_verifier=RejectingTransferEvidenceVerifier(),
        consent_resolver=RejectingConsentSnapshotResolver(),
    )
    now = datetime.now(UTC)
    token = mint_device_binding_token(
        device_id="device-consent-unavailable",
        secret=app.state.settings.device_binding_token_key(),
        now=now,
        ttl=timedelta(minutes=5),
        nonce="consent-unavailable",
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"username": "consent-owner", "password": "safe-password"},
        )
        assert registered.status_code == 201
        identity = registered.json()
        response = await client.post(
            "/v1/device-bindings",
            headers={"Authorization": f"Bearer {identity['access_token']}"},
            json={
                "device_claim_token": token,
                "declared_mode": "self_use",
                "account_owner_person_id": identity["user_id"],
                "primary_subject": {
                    "person_id": identity["user_id"],
                    "relationship": "self",
                },
                "persona_selection": "starlight",
                "service_preferences": {
                    "memory_level": "personal",
                    "interview_frequency": "low",
                },
                "consent_offer_ids": ["offer_self_memory_retention_v1"],
            },
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "consent_authority_unavailable"}
    }


@pytest.mark.asyncio
async def test_development_binding_uses_persistent_authority_bound_to_real_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _default_app(monkeypatch, tmp_path)
    now = datetime.now(UTC)
    token = mint_device_binding_token(
        device_id="device-consent-persisted",
        secret=app.state.settings.device_binding_token_key(),
        now=now,
        ttl=timedelta(minutes=5),
        nonce="consent-persisted",
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"username": "consent-persisted", "password": "safe-password"},
        )
        assert registered.status_code == 201
        identity = registered.json()
        response = await client.post(
            "/v1/device-bindings",
            headers={"Authorization": f"Bearer {identity['access_token']}"},
            json={
                "device_claim_token": token,
                "declared_mode": "self_use",
                "account_owner_person_id": identity["user_id"],
                "primary_subject": {
                    "person_id": identity["user_id"],
                    "relationship": "self",
                },
                "persona_selection": "starlight",
                "service_preferences": {
                    "memory_level": "personal",
                    "interview_frequency": "low",
                },
                "consent_offer_ids": ["offer_self_memory_retention_v1"],
            },
        )

    assert response.status_code == 201
    manifest = response.json()
    snapshot = await app.state.binding_consent_authority.get(
        manifest["consent_snapshot_id"]
    )
    assert snapshot.binding_id == manifest["binding_id"]
    assert snapshot.binding_version == manifest["binding_version"]
    assert snapshot.device_id == manifest["device_id"]
    assert snapshot.account_owner_person_id == manifest["account_owner_id"]
    assert snapshot.consent_offer_ids == ("offer_self_memory_retention_v1",)
    assert not hasattr(snapshot, "grants")
    assert not hasattr(snapshot, "capabilities")


@pytest.mark.asyncio
async def test_binding_request_cannot_supply_a_consent_snapshot_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    app = _default_app(monkeypatch, tmp_path)
    now = datetime.now(UTC)
    token = mint_device_binding_token(
        device_id="device-consent-forged",
        secret=app.state.settings.device_binding_token_key(),
        now=now,
        ttl=timedelta(minutes=5),
        nonce="consent-forged",
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"username": "consent-forged", "password": "safe-password"},
        )
        identity = registered.json()
        response = await client.post(
            "/v1/device-bindings",
            headers={"Authorization": f"Bearer {identity['access_token']}"},
            json={
                "device_claim_token": token,
                "declared_mode": "self_use",
                "account_owner_person_id": identity["user_id"],
                "primary_subject": {
                    "person_id": identity["user_id"],
                    "relationship": "self",
                },
                "persona_selection": "starlight",
                "service_preferences": {"memory_level": "off"},
                "consent_offer_ids": [],
                "consent_snapshot_id": "client-forged-snapshot",
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"
