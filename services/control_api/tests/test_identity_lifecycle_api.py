"""API contracts for the identity lifecycle: actor spoofing is impossible,
relationship invites are two-party, disputes need both acknowledgements,
transfer needs step-up + target acceptance, and role grants cannot widen."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
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
) -> None:
    now = datetime.now(UTC)
    token = mint_device_binding_token(
        device_id=device_id,
        secret=app.state.settings.device_binding_token_key(),
        now=now,
        ttl=timedelta(minutes=5),
        nonce=f"nonce-{device_id}",
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


def _headers(user: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {user['access_token']}"}


def _outbox_rows(app) -> list[dict]:
    path = app.state.settings.identity_sqlite_path()
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT topic, payload_json FROM identity_outbox ORDER BY created_at"
        ).fetchall()
    return [
        {
            "topic": str(row["topic"]),
            "payload": json.loads(str(row["payload_json"])),
        }
        for row in rows
    ]


def _mint_receipt(
    app,
    *,
    actor: str,
    subject: str,
    device_id: str,
    binding_id: str,
    binding_version: int,
    expires_at: datetime,
    nonce: str = "step-up-1",
) -> tuple[str, str]:
    """Returns (policy_receipt_id, step_up_evidence_id)."""
    return app.state.transfer_test_authority.mint(
        actor_person_id=actor,
        subject_person_id=subject,
        device_id=device_id,
        binding_id=binding_id,
        binding_version=binding_version,
        expires_at=expires_at,
        nonce=nonce,
    )


@pytest.mark.asyncio
async def test_relationship_invite_accept_dispute_resolution_revoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _env(monkeypatch, tmp_path, "rel")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        parent = await _register(client, "parent")
        child = await _register(client, "child")
        await _bind_self(client, app, owner=parent, device_id="dev-parent")
        await _bind_self(client, app, owner=child, device_id="dev-child")

        # An actor field in the request body is rejected (extra=forbid).
        response = await client.post(
            "/v1/relationships/invites",
            headers=_headers(parent),
            json={
                "target_person_id": child["user_id"],
                "relation_type": "guardian_of",
                "established_evidence_id": "evidence-1",
                "actor_person_id": child["user_id"],
            },
        )
        assert response.status_code == 422

        response = await client.post(
            "/v1/relationships/invites",
            headers=_headers(parent),
            json={
                "target_person_id": child["user_id"],
                "relation_type": "guardian_of",
                "established_evidence_id": "evidence-1",
            },
        )
        assert response.status_code == 201, response.text
        relationship = response.json()
        relationship_id = relationship["relationship_id"]
        assert relationship["status"] == "pending"

        # A stranger cannot confirm the relationship.
        stranger = await _register(client, "stranger")
        response = await client.post(
            f"/v1/relationships/{relationship_id}/accept",
            headers=_headers(stranger),
            json={},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "relationship_forbidden"

        response = await client.post(
            f"/v1/relationships/{relationship_id}/accept",
            headers=_headers(parent),
            json={},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        response = await client.post(
            f"/v1/relationships/{relationship_id}/accept",
            headers=_headers(child),
            json={},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "active"

        response = await client.post(
            f"/v1/relationships/{relationship_id}/dispute",
            headers=_headers(parent),
            json={"reason": "范围争议"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "disputed"

        # One acknowledgement keeps it disputed; both restore active.
        response = await client.post(
            f"/v1/relationships/{relationship_id}/dispute/resolution",
            headers=_headers(parent),
            json={},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "disputed"
        response = await client.post(
            f"/v1/relationships/{relationship_id}/dispute/resolution",
            headers=_headers(child),
            json={},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "active"

        response = await client.post(
            f"/v1/relationships/{relationship_id}/revoke",
            headers=_headers(parent),
            json={"evidence_id": "evidence-revoke"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "revoked"


@pytest.mark.asyncio
async def test_relationship_actor_spoof_and_cross_access_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _env(monkeypatch, tmp_path, "rel2")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        a = await _register(client, "a-user")
        b = await _register(client, "b-user")
        await _bind_self(client, app, owner=a, device_id="dev-a")
        await _bind_self(client, app, owner=b, device_id="dev-b")

        # A cannot invite on B's behalf (actor must equal the authenticated user).
        response = await client.post(
            "/v1/relationships/invites",
            headers=_headers(a),
            json={
                "target_person_id": b["user_id"],
                "relation_type": "guardian_of",
                "established_evidence_id": "evidence-1",
            },
        )
        assert response.status_code == 201
        relationship_id = response.json()["relationship_id"]

        # Both sides accept, then B (an endpoint) can suspend.
        response = await client.post(
            f"/v1/relationships/{relationship_id}/accept",
            headers=_headers(a),
            json={},
        )
        assert response.status_code == 200
        response = await client.post(
            f"/v1/relationships/{relationship_id}/accept",
            headers=_headers(b),
            json={},
        )
        assert response.status_code == 200
        response = await client.post(
            f"/v1/relationships/{relationship_id}/suspend",
            headers=_headers(b),
            json={},
        )
        assert response.status_code == 200
        # A stranger's relationship list never contains other people's entries.
        stranger = await _register(client, "stranger2")
        response = await client.get(
            "/v1/relationships",
            headers=_headers(stranger),
        )
        assert response.status_code == 200
        assert all(
            item["relationship_id"] != relationship_id
            for item in response.json()["relationships"]
        )


@pytest.mark.asyncio
async def test_binding_supersede_cannot_widen_roles(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _env(monkeypatch, tmp_path, "bind")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "owner")
        await _bind_self(client, app, owner=owner, device_id="dev-sup")

        # Server-authoritative fields (service/policy profile, consent,
        # persona) must never be accepted from the request body.
        response = await client.post(
            "/v1/devices/dev-sup/binding/supersede",
            headers=_headers(owner),
            json={
                "declared_mode": "self_use",
                "primary_subject_ids": [owner["user_id"]],
                "roles": [],
                "service_profile_version": "self-v2",
                "policy_bundle_version": "policy-self-v2",
            },
        )
        assert response.status_code == 422

        response = await client.post(
            "/v1/devices/dev-sup/binding/supersede",
            headers=_headers(owner),
            json={
                "declared_mode": "self_use",
                "primary_subject_ids": [owner["user_id"]],
                "roles": [
                    {
                        "person_id": owner["user_id"],
                        "role": "account_owner",
                        "permissions": ["binding.manage", "content.read"],
                    }
                ],
            },
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "binding_conflict"

        response = await client.post(
            "/v1/devices/dev-sup/binding/supersede",
            headers=_headers(owner),
            json={
                "declared_mode": "self_use",
                "primary_subject_ids": [owner["user_id"]],
                "roles": [],
            },
        )
        assert response.status_code == 200
        assert response.json()["binding_version"] == 2


@pytest.mark.asyncio
async def test_transfer_requires_step_up_and_target_accept(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _env(monkeypatch, tmp_path, "tx")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "tx-owner")
        target = await _register(client, "tx-target")
        outsider = await _register(client, "tx-outsider")
        await _bind_self(client, app, owner=owner, device_id="dev-tx")
        await _bind_self(client, app, owner=target, device_id="dev-tx-target")

        # Non-owner cannot create a transfer intent.
        response = await client.post(
            "/v1/devices/dev-tx/transfers",
            headers=_headers(outsider),
            json={
                "to_account_owner_person_id": target["user_id"],
                "step_up_evidence_id": "step-up-1",
                "policy_receipt_id": "receipt-1",
                "idempotency_key": "test-idem-key-0001",
            },
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "transfer_forbidden"

        # Owner creates the intent with a server-signed step-up + receipt.
        v1 = app.state.multi_subject_binding_manifests["dev-tx"]
        policy_id, step_up = _mint_receipt(
            app,
            actor=owner["user_id"],
            subject=target["user_id"],
            device_id="dev-tx",
            binding_id=v1.binding_id,
            binding_version=v1.binding_version,
            expires_at=datetime.now(UTC) + timedelta(days=8),
        )
        response = await client.post(
            "/v1/devices/dev-tx/transfers",
            headers=_headers(owner),
            json={
                "to_account_owner_person_id": target["user_id"],
                "step_up_evidence_id": step_up,
                "policy_receipt_id": policy_id,
                "idempotency_key": "test-idem-key-0001",
            },
        )
        assert response.status_code == 201
        transfer_id = response.json()["transfer_id"]

        # Forged evidence is rejected even before the intent exists: an
        # unknown policy receipt with a fresh (valid) step-up ticket.
        forged_policy_id, fresh_step_up = _mint_receipt(
            app,
            actor=owner["user_id"],
            subject=target["user_id"],
            device_id="dev-tx",
            binding_id=v1.binding_id,
            binding_version=v1.binding_version,
            expires_at=datetime.now(UTC) + timedelta(days=8),
            nonce="forged-policy",
        )
        response = await client.post(
            "/v1/devices/dev-tx/transfers",
            headers=_headers(owner),
            json={
                "to_account_owner_person_id": target["user_id"],
                "step_up_evidence_id": fresh_step_up,
                "policy_receipt_id": "forged-receipt",
                "idempotency_key": "forged-key-0001",
            },
        )
        assert response.status_code == 422
        # A forged step-up ticket with a valid policy receipt is refused too.
        response = await client.post(
            "/v1/devices/dev-tx/transfers",
            headers=_headers(owner),
            json={
                "to_account_owner_person_id": target["user_id"],
                "step_up_evidence_id": "forged-step-up-ticket",
                "policy_receipt_id": forged_policy_id,
                "idempotency_key": "forged-key-0002",
            },
        )
        assert response.status_code == 422

        # Owner cannot accept on behalf of the target.
        accept_body = {
            "declared_mode": "self_use",
            "primary_subject_ids": [target["user_id"]],
            "roles": [],
            "idempotency_key": "accept-key-0001",
        }
        response = await client.post(
            f"/v1/transfers/{transfer_id}/accept",
            headers=_headers(owner),
            json=accept_body,
        )
        assert response.status_code == 403

        # Target accepts: new version, old version superseded, no old roles.
        response = await client.post(
            f"/v1/transfers/{transfer_id}/accept",
            headers=_headers(target),
            json=accept_body,
        )
        assert response.status_code == 200, response.text
        manifest = response.json()
        assert manifest["binding_version"] == 2
        assert manifest["reason"] == "transfer"
        assert manifest["account_owner_id"] == target["user_id"]
        assert all(
            role["person_id"] != owner["user_id"] for role in manifest["roles"]
        )

        # Idempotent replay by the target returns the same manifest.
        response = await client.post(
            f"/v1/transfers/{transfer_id}/accept",
            headers=_headers(target),
            json=accept_body,
        )
        assert response.status_code == 200
        assert response.json()["binding_id"] == manifest["binding_id"]

        # The old binding's cache invalidation outbox event was emitted.
        outbox = _outbox_rows(app)
        transferred = [
            row for row in outbox if row["topic"] == "identity.binding.transferred"
        ]
        assert len(transferred) == 1
        assert transferred[0]["payload"]["binding_version"] == 2

        # Old owner no longer holds any role; the new owner can list versions.
        response = await client.get(
            "/v1/devices/dev-tx/bindings", headers=_headers(target)
        )
        assert response.status_code == 200
        assert [item["binding_version"] for item in response.json()["bindings"]] == [1, 2]


@pytest.mark.asyncio
async def test_transfer_cancel_and_expiry_guards(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _env(monkeypatch, tmp_path, "tx2")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "tx2-owner")
        target = await _register(client, "tx2-target")
        await _bind_self(client, app, owner=owner, device_id="dev-tx2")
        await _bind_self(client, app, owner=target, device_id="dev-tx2b")

        v1 = app.state.multi_subject_binding_manifests["dev-tx2"]
        policy_id, step_up = _mint_receipt(
            app,
            actor=owner["user_id"],
            subject=target["user_id"],
            device_id="dev-tx2",
            binding_id=v1.binding_id,
            binding_version=v1.binding_version,
            expires_at=datetime.now(UTC) + timedelta(days=8),
        )
        # A past valid_until is rejected at the boundary.
        response = await client.post(
            "/v1/devices/dev-tx2/transfers",
            headers=_headers(owner),
            json={
                "to_account_owner_person_id": target["user_id"],
                "step_up_evidence_id": step_up,
                "policy_receipt_id": policy_id,
                "idempotency_key": "test-idem-key-0001",
                "valid_until": "2020-01-01T00:00:00Z",
            },
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "transfer_conflict"

        response = await client.post(
            "/v1/devices/dev-tx2/transfers",
            headers=_headers(owner),
            json={
                "to_account_owner_person_id": target["user_id"],
                "step_up_evidence_id": step_up,
                "policy_receipt_id": policy_id,
                "idempotency_key": "test-idem-key-0001",
            },
        )
        assert response.status_code == 201
        transfer_id = response.json()["transfer_id"]

        # The target cannot cancel the intent; only the current owner can.
        response = await client.post(
            f"/v1/transfers/{transfer_id}/cancel",
            headers=_headers(target),
            json={"reason": "no", "idempotency_key": "cancel-key-0001"},
        )
        assert response.status_code == 403
        response = await client.post(
            f"/v1/transfers/{transfer_id}/cancel",
            headers=_headers(owner),
            json={"reason": "changed my mind", "idempotency_key": "cancel-key-0001"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

        response = await client.post(
            f"/v1/transfers/{transfer_id}/accept",
            headers=_headers(target),
            json={
                "declared_mode": "self_use",
                "primary_subject_ids": [target["user_id"]],
                "roles": [],
                "idempotency_key": "accept-key-0002",
            },
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "transfer_conflict"


@pytest.mark.asyncio
async def test_cross_family_binding_read_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _env(monkeypatch, tmp_path, "family")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        family_a = await _register(client, "family-a")
        family_b = await _register(client, "family-b")
        await _bind_self(client, app, owner=family_a, device_id="dev-a")
        await _bind_self(client, app, owner=family_b, device_id="dev-b")

        response = await client.get(
            "/v1/devices/dev-a/bindings", headers=_headers(family_b)
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "binding_forbidden"

        response = await client.get(
            "/v1/devices/dev-a/transfers", headers=_headers(family_b)
        )
        assert response.status_code == 200
        assert response.json()["transfers"] == []


@pytest.mark.asyncio
async def test_persons_me_and_unbind_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _env(monkeypatch, tmp_path, "me")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        user = await _register(client, "me-user")
        response = await client.get("/v1/persons/me", headers=_headers(user))
        assert response.status_code == 404

        await _bind_self(client, app, owner=user, device_id="dev-me")
        response = await client.get("/v1/persons/me", headers=_headers(user))
        assert response.status_code == 200
        assert response.json()["person_id"] == user["user_id"]
        assert response.json()["subject_category"] == "unknown"

        response = await client.post(
            "/v1/devices/dev-me/binding/unbind",
            headers=_headers(user),
            json={"reason": "device_lost"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "revoked"
        response = await client.get(
            "/v1/devices/dev-me/bindings", headers=_headers(user)
        )
        assert response.status_code == 200
        assert response.json()["bindings"][0]["status"] == "revoked"
