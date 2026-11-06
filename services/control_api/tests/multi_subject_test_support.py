"""Shared builders for the split multi-subject API tests.

The ``test_multi_subject_*`` modules split the API suite by behavior; each
imports its common app-environment, identity-registration and binding builders
from here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from services.control_api.app.device_binding_token import mint_device_binding_token
from services.control_api.app.main import create_app
from services.control_api.tests.identity_test_helpers import (
    install_test_identity_authority,
)


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path, name: str):
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / f"{name}.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_IDENTITY_DB_PATH", str(tmp_path / f"{name}-identity.sqlite3")
    )
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", f"{name}-auth-secret-long-enough-0123456789")
    monkeypatch.setenv(
        "MEMORIA_TRANSFER_EVIDENCE_SECRET",
        f"{name}-transfer-evidence-secret-32-bytes",
    )
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    app = create_app()
    install_test_identity_authority(
        app,
        secret=app.state.settings.transfer_evidence_key(),
    )
    return app


async def _register(client: AsyncClient, username: str) -> dict:
    response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": "safe-password"},
    )
    assert response.status_code == 201
    return response.json()


async def _bind_self(
    client: AsyncClient,
    app,
    *,
    owner: dict,
    device_id: str,
    nonce: str,
) -> None:
    now = datetime.now(UTC)
    token = mint_device_binding_token(
        device_id=device_id,
        secret=app.state.settings.device_binding_token_key(),
        now=now,
        ttl=timedelta(minutes=5),
        nonce=nonce,
    )
    response = await client.post(
        "/v1/device-bindings",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
        json={
            "device_claim_token": token,
            "declared_mode": "self_use",
            "account_owner_person_id": owner["user_id"],
            "primary_subject": {
                "person_id": owner["user_id"],
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


async def _establish_relationship(
    identity,
    *,
    relation_type: str,
    source: str,
    target: str,
    now: datetime,
) -> None:
    """Two-party confirmed relationship required by binding role checks."""
    proposed = await identity.propose_relationship(
        source_person_id=source,
        target_person_id=target,
        relation_type=relation_type,  # type: ignore[arg-type]
        established_evidence_id=f"evidence-{relation_type}",
        actor_person_id=source,
        now=now,
    )
    await identity.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=source,
        now=now,
    )
    await identity.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=target,
        now=now,
    )


async def _bind_family(
    client: AsyncClient,
    app,
    *,
    owner: dict,
    device_id: str,
    nonce: str,
) -> str:
    now = datetime.now(UTC)
    # Fixture authority: pre-register the owner as a verified adult with an
    # explicit evidence id BEFORE the API binding call lazy-registers the
    # account person, so the API path itself never claims verified adult.
    await app.state.identity_service.register_person(
        person_id=owner["user_id"],
        display_name="家长",
        timezone="Asia/Shanghai",
        subject_category="adult",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="fixture-adult-evidence",
        now=now,
    )
    token = mint_device_binding_token(
        device_id=device_id,
        secret=app.state.settings.device_binding_token_key(),
        now=now,
        ttl=timedelta(minutes=5),
        nonce=nonce,
    )
    response = await client.post(
        "/v1/device-bindings",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
        json={
            "device_claim_token": token,
            "declared_mode": "family_shared",
            "account_owner_person_id": owner["user_id"],
            "primary_subject": {
                "person_id": "new",
                "relationship": "family_member_of",
                "subject_draft": {
                    "display_name": "小朋友",
                    "age_band": "under_14",
                },
            },
            "persona_selection": "starlight",
            "service_preferences": {
                "memory_level": "family_shared",
                "shared_persona_enabled": True,
            },
            "consent_offer_ids": ["offer_family_space_v1"],
        },
    )
    assert response.status_code == 201
    child_id = response.json()["primary_subject_ids"][0]
    return child_id
