"""API contracts for the identity lifecycle: actor spoofing is impossible,
relationship invites are two-party, disputes need both acknowledgements,
transfer needs step-up + target acceptance, and role grants cannot widen."""

from __future__ import annotations

import asyncio
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
from services.identity.domain import BindingVersionConflictError


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


def _guardian_permissions(payload: dict, person_id: str) -> tuple[str, ...]:
    """The guardian role's permission set inside a manifest payload."""
    return tuple(
        sorted(
            permission
            for role in payload["roles"]
            if role["person_id"] == person_id and role["role"] == "guardian"
            for permission in role["permissions"]
        )
    )


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


async def _register_verified_owner(client, app, owner: dict) -> None:
    await app.state.identity_service.register_person(
        person_id=owner["user_id"],
        display_name="家长",
        timezone="Asia/Shanghai",
        subject_category="adult",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="fixture-adult-evidence",
        now=datetime.now(UTC),
    )


async def _bind_parent_child(
    client, app, owner: dict, device_id: str, nonce: str, child_name: str, age_band: str
) -> str:
    now = datetime.now(UTC)
    token = mint_device_binding_token(
        device_id=device_id,
        secret=app.state.settings.device_binding_token_key(),
        now=now,
        ttl=timedelta(minutes=5),
        nonce=nonce,
    )
    created = await client.post(
        "/v1/device-bindings",
        headers=_headers(owner),
        json={
            "device_claim_token": token,
            "declared_mode": "parent_for_child",
            "account_owner_person_id": owner["user_id"],
            "primary_subject": {
                "person_id": "new",
                "relationship": "guardian_of",
                "subject_draft": {"display_name": child_name, "age_band": age_band},
            },
            "persona_selection": "starlight",
            "consent_offer_ids": ["offer_minor_voice_session_v1"],
        },
    )
    assert created.status_code == 201, created.text
    subjects = created.json()["primary_subject_ids"]
    assert len(subjects) == 1 and subjects[0] != owner["user_id"]
    return str(subjects[0])


@pytest.mark.asyncio
async def test_binding_member_add_second_child_then_switch_to_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _env(monkeypatch, tmp_path, "binding-members")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "binding-members-owner")
        await _register_verified_owner(client, app, owner)
        child1 = await _bind_parent_child(
            client,
            app,
            owner,
            "device-members",
            "members-1",
            "老大",
            "under_14",
        )
        added = await client.post(
            "/v1/devices/device-members/binding/members",
            headers=_headers(owner),
            json={
                "person_id": "new",
                "subject_draft": {"display_name": "老二", "age_band": "14_17"},
            },
        )
        assert added.status_code == 201, added.text
        body = added.json()
        assert body["binding_version"] == 2
        assert body["status"] == "active"
        assert len(body["primary_subject_ids"]) == 2
        assert child1 in body["primary_subject_ids"]
        child2 = next(p for p in body["primary_subject_ids"] if p != child1)
        assert child2 != owner["user_id"]
        assert owner["user_id"] in body["guardian_ids"]
        assert body["account_owner_id"] == owner["user_id"]
        child2_person = await app.state.identity_service.get_person(
            child2, actor_person_id=owner["user_id"]
        )
        assert child2_person.subject_category == "minor"
        assert child2_person.age_band == "14_17"
        declarations = [
            relationship
            for relationship in await app.state.identity_service.list_relationships(
                person_id=child2
            )
            if relationship.relation_type == "guardian_of"
            and relationship.source_person_id == owner["user_id"]
        ]
        assert len(declarations) == 1
        assert declarations[0].confirmed_by_source_at is not None
        assert declarations[0].confirmed_by_target_at is None
        assert (
            declarations[0].established_evidence_id
            == "guardian_declaration_v1:device_binding"
        )
        resolution = await client.post(
            "/v1/sessions/resolve-subject",
            headers=_headers(owner),
            json={"device_id": "device-members", "environment": {}},
        )
        assert resolution.status_code == 200, resolution.text
        candidate_ids = {
            candidate["person_id"]
            for candidate in resolution.json()["candidate_subjects"]
        }
        assert child1 in candidate_ids
        assert child2 in candidate_ids
        assert resolution.json()["allowed_confirmation_methods"] == ["app_confirm"]
        profile = await client.get(
            "/v1/devices/device-members/runtime-profile", headers=_headers(owner)
        )
        assert profile.status_code == 200, profile.text
        switch = await client.post(
            "/v1/sessions/" + profile.json()["session_id"] + "/active-subject",
            headers=_headers(owner),
            json={"person_id": child2, "confirmation_method": "app_confirm"},
        )
        assert switch.status_code == 200, switch.text
        assert switch.json()["active_subject_id"] == child2


@pytest.mark.asyncio
async def test_binding_member_add_rejects_stranger_duplicate_mode_and_adult(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _env(monkeypatch, tmp_path, "binding-members-guard")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "members-guard-owner")
        stranger = await _register(client, "members-guard-stranger")
        await _register_verified_owner(client, app, owner)
        child1 = await _bind_parent_child(
            client,
            app,
            owner,
            "device-members-guard",
            "members-guard-1",
            "老大",
            "under_14",
        )
        device = "/v1/devices/device-members-guard/binding/members"
        stranger_add = await client.post(
            device,
            headers=_headers(stranger),
            json={
                "person_id": "new",
                "subject_draft": {"display_name": "老二", "age_band": "under_14"},
            },
        )
        assert stranger_add.status_code == 403, stranger_add.text
        assert stranger_add.json()["detail"]["code"] == "binding_forbidden"
        duplicate = await client.post(
            device, headers=_headers(owner), json={"person_id": child1}
        )
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()["detail"]["code"] == "member_already_present"
        unknown_person = await client.post(
            device, headers=_headers(owner), json={"person_id": "person-missing"}
        )
        assert unknown_person.status_code == 404, unknown_person.text
        unknown_device = await client.post(
            "/v1/devices/device-missing/binding/members",
            headers=_headers(owner),
            json={
                "person_id": "new",
                "subject_draft": {"display_name": "老二", "age_band": "under_14"},
            },
        )
        assert unknown_device.status_code == 404, unknown_device.text
        missing_draft = await client.post(
            device, headers=_headers(owner), json={"person_id": "new"}
        )
        assert missing_draft.status_code == 422, missing_draft.text
        await _bind_self(client, app, owner=stranger, device_id="device-self-guard")
        self_add = await client.post(
            "/v1/devices/device-self-guard/binding/members",
            headers=_headers(stranger),
            json={
                "person_id": "new",
                "subject_draft": {"display_name": "老二", "age_band": "under_14"},
            },
        )
        assert self_add.status_code == 422, self_add.text
        assert self_add.json()["detail"]["code"] == "member_not_supported_for_mode"
        await app.state.identity_service.register_person(
            person_id="members-guard-adult",
            display_name="成年人",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="verified",
            age_evidence_id="fixture-adult-evidence",
            now=datetime.now(UTC),
        )
        adult_add = await client.post(
            device, headers=_headers(owner), json={"person_id": "members-guard-adult"}
        )
        assert adult_add.status_code == 422, adult_add.text
        assert adult_add.json()["detail"]["code"] == "member_age_band_rejected"


@pytest.mark.asyncio
async def test_binding_member_add_reports_conflict_after_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Losing every race fails closed with an explicit 409, not a partial write.

    Exhausting the bounded retries must surface as a conflict so the caller
    re-reads; no stale subject list (and no new binding version) is written.
    """
    app = _env(monkeypatch, tmp_path, "binding-members-busy")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "members-busy-owner")
        await _register_verified_owner(client, app, owner)
        child1 = await _bind_parent_child(
            client,
            app,
            owner,
            "device-members-busy",
            "members-busy-1",
            "老大",
            "under_14",
        )
        identity = app.state.identity_service
        attempts = 0

        async def always_conflicts(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise BindingVersionConflictError("simulated concurrent writer")

        monkeypatch.setattr(identity, "supersede_binding", always_conflicts)
        busy = await client.post(
            "/v1/devices/device-members-busy/binding/members",
            headers=_headers(owner),
            json={
                "person_id": "new",
                "subject_draft": {"display_name": "老二", "age_band": "under_14"},
            },
        )
        assert busy.status_code == 409, busy.text
        assert busy.json()["detail"]["code"] == "binding_conflict"
        assert attempts == 3
        listed = await client.get(
            "/v1/devices/device-members-busy/bindings", headers=_headers(owner)
        )
        assert listed.status_code == 200, listed.text
        active = next(
            binding
            for binding in listed.json()["bindings"]
            if binding["status"] == "active"
        )
        assert active["binding_version"] == 1
        assert set(active["primary_subject_ids"]) == {child1}


@pytest.mark.asyncio
async def test_binding_member_add_preserves_tightened_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A permission set narrowed on the previous version is carried onward.

    Adding a member must never restore the role defaults over a deliberate
    tightening (the empty set is the strongest form of it), and it must not
    drop a narrowed non-empty subset either.
    """
    app = _env(monkeypatch, tmp_path, "binding-members-perms")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "members-perms-owner")
        await _register_verified_owner(client, app, owner)
        child1 = await _bind_parent_child(
            client,
            app,
            owner,
            "device-members-perms",
            "members-perms-1",
            "老大",
            "under_14",
        )
        identity = app.state.identity_service
        device = "/v1/devices/device-members-perms/binding/members"
        narrowed_cases = (
            (frozenset(), ()),
            (frozenset({"device.status.view"}), ("device.status.view",)),
        )
        versions = []
        for narrowed, expected in narrowed_cases:
            now = datetime.now(UTC)
            manifest = await identity.get_active_manifest(
                "device-members-perms", now=now, actor_person_id=owner["user_id"]
            )
            assert manifest is not None
            tightened = await identity.supersede_binding(
                device_id="device-members-perms",
                declared_mode=manifest.declared_mode,
                primary_subject_ids=manifest.primary_subject_ids,
                roles=((owner["user_id"], "guardian"),),
                role_permissions={(owner["user_id"], "guardian"): narrowed},
                valid_until=manifest.valid_until,
                actor_person_id=owner["user_id"],
                now=now,
            )
            assert (
                _guardian_permissions(tightened.to_dict(), owner["user_id"])
                == expected
            )
            added = await client.post(
                device,
                headers=_headers(owner),
                json={
                    "person_id": "new",
                    "subject_draft": {
                        "display_name": "新成员",
                        "age_band": "under_14",
                    },
                },
            )
            assert added.status_code == 201, added.text
            body = added.json()
            versions.append(body["binding_version"])
            assert _guardian_permissions(body, owner["user_id"]) == expected
            assert owner["user_id"] in body["guardian_ids"]
        assert versions == [3, 5]
        listed = await client.get(
            "/v1/devices/device-members-perms/bindings", headers=_headers(owner)
        )
        assert listed.status_code == 200, listed.text
        active = next(
            binding
            for binding in listed.json()["bindings"]
            if binding["status"] == "active"
        )
        assert active["binding_version"] == 5
        assert _guardian_permissions(active, owner["user_id"]) == (
            "device.status.view",
        )
        assert child1 in active["primary_subject_ids"]
        assert len(active["primary_subject_ids"]) == 3


@pytest.mark.asyncio
async def test_binding_member_add_concurrent_appends_keep_every_member(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Two concurrent appends read version 1: neither member may be lost.

    Both requests are held until each has read the same ACTIVE version, which
    is exactly the interleaving that previously let the second writer overwrite
    the first member with its own stale subject list.
    """
    app = _env(monkeypatch, tmp_path, "binding-members-race")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "members-race-owner")
        await _register_verified_owner(client, app, owner)
        child1 = await _bind_parent_child(
            client,
            app,
            owner,
            "device-members-race",
            "members-race-1",
            "老大",
            "under_14",
        )
        identity = app.state.identity_service
        original = identity.get_active_manifest
        arrived = 0
        both_read = asyncio.Event()

        async def gated(*args, **kwargs):
            nonlocal arrived
            manifest = await original(*args, **kwargs)
            if arrived < 2:
                arrived += 1
                if arrived == 2:
                    both_read.set()
                await both_read.wait()
            return manifest

        monkeypatch.setattr(identity, "get_active_manifest", gated)
        device = "/v1/devices/device-members-race/binding/members"
        responses = await asyncio.gather(
            client.post(
                device,
                headers=_headers(owner),
                json={
                    "person_id": "new",
                    "subject_draft": {"display_name": "老二", "age_band": "under_14"},
                },
            ),
            client.post(
                device,
                headers=_headers(owner),
                json={
                    "person_id": "new",
                    "subject_draft": {"display_name": "老三", "age_band": "14_17"},
                },
            ),
        )
        assert [response.status_code for response in responses] == [201, 201], [
            response.text for response in responses
        ]
        added_ids = {
            person_id
            for response in responses
            for person_id in response.json()["primary_subject_ids"]
        } - {child1}
        assert len(added_ids) == 2
        assert sorted(
            response.json()["binding_version"] for response in responses
        ) == [2, 3]
        listed = await client.get(
            "/v1/devices/device-members-race/bindings", headers=_headers(owner)
        )
        assert listed.status_code == 200, listed.text
        active = next(
            binding
            for binding in listed.json()["bindings"]
            if binding["status"] == "active"
        )
        assert active["binding_version"] == 3
        assert set(active["primary_subject_ids"]) == {child1, *added_ids}
        assert {
            role["person_id"]
            for role in active["roles"]
            if role["role"] == "primary_subject"
        } == {child1, *added_ids}


@pytest.mark.asyncio
async def test_binding_member_add_keeps_family_space(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Appending to a family_shared binding must not drop the family space.

    The append used to rebuild the version without the family space id, so the
    family_shared mode constraint rejected it with 409.
    """
    app = _env(monkeypatch, tmp_path, "binding-members-family")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "members-family-owner")
        await _register_verified_owner(client, app, owner)
        now = datetime.now(UTC)
        token = mint_device_binding_token(
            device_id="device-members-family",
            secret=app.state.settings.device_binding_token_key(),
            now=now,
            ttl=timedelta(minutes=5),
            nonce="members-family-1",
        )
        created = await client.post(
            "/v1/device-bindings",
            headers=_headers(owner),
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
        assert created.status_code == 201, created.text
        family_space_id = created.json()["family_space_id"]
        assert family_space_id
        added = await client.post(
            "/v1/devices/device-members-family/binding/members",
            headers=_headers(owner),
            json={
                "person_id": "new",
                "subject_draft": {"display_name": "奶奶", "age_band": "adult"},
            },
        )
        assert added.status_code == 201, added.text
        assert added.json()["family_space_id"] == family_space_id
        listed = await client.get(
            "/v1/devices/device-members-family/bindings", headers=_headers(owner)
        )
        assert listed.status_code == 200, listed.text
        active = next(
            binding
            for binding in listed.json()["bindings"]
            if binding["status"] == "active"
        )
        assert active["binding_version"] == 2
        assert active["family_space_id"] == family_space_id
        assert len(active["primary_subject_ids"]) == 2


@pytest.mark.asyncio
async def test_age_evidence_declaration_owner_self_stranger(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _env(monkeypatch, tmp_path, "age-evidence")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        owner = await _register(client, "age-evidence-owner")
        stranger = await _register(client, "age-evidence-stranger")
        await _register_verified_owner(client, app, owner)
        child = await _bind_parent_child(
            client,
            app,
            owner,
            "device-age-evidence",
            "age-evidence-1",
            "老大",
            "under_14",
        )
        target = "/v1/persons/" + child + "/age-evidence"
        corrected = await client.patch(
            target, headers=_headers(owner), json={"age_band": "14_17"}
        )
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["age_band"] == "14_17"
        assert corrected.json()["subject_category"] == "minor"
        # A declaration against the owner's own verified adulthood fails closed
        # into disputed instead of downgrading them to a minor.  This runs
        # before the self-declaration below changes the owner's own band.
        disputed = await client.patch(
            "/v1/persons/" + owner["user_id"] + "/age-evidence",
            headers=_headers(owner),
            json={"age_band": "under_14"},
        )
        assert disputed.status_code == 200, disputed.text
        assert disputed.json()["age_evidence_status"] == "disputed"
        denied = await client.patch(
            target, headers=_headers(stranger), json={"age_band": "14_17"}
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"]["code"] == "guardian_binding_owner_required"
        adult_claim = await client.patch(
            target, headers=_headers(owner), json={"age_band": "adult"}
        )
        assert adult_claim.status_code == 422, adult_claim.text
        missing = await client.patch(
            "/v1/persons/person-missing/age-evidence",
            headers=_headers(owner),
            json={"age_band": "14_17"},
        )
        assert missing.status_code == 404, missing.text
        # After the disputed downgrade the owner can still self-declare unknown.
        me_patch = await client.patch(
            "/v1/persons/" + owner["user_id"] + "/age-evidence",
            headers=_headers(owner),
            json={"age_band": "unknown"},
        )
        assert me_patch.status_code == 200, me_patch.text
