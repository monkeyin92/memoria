"""Unbinding in the Mini Program also frees the device in the fleet.

It used to revoke only the Identity binding: the board kept its activation
manifest and a fresh scan of its QR was refused as already bound.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from services.control_api.tests.child_layer_test_support import (
    FailOnSubjectDelete,
    record_layer_erasures,
)
from services.control_api.tests.test_device_display_profile import (
    CERTIFICATE_ID,
    DEVICE_ID,
    PATH,
    _bound_device,
    _signed_headers,
)
from services.control_api.tests.test_device_onboarding_binding_integration import (
    _app,
    _online_claim,
)
from services.control_api.tests.test_guardian_api import _mark_verified_adult
from services.device_fleet.bootstrap_domain import (
    BootstrapQRPayload,
    DeviceLifecycle,
    QRSignatureError,
    b64url_encode,
    encode_bootstrap_qr,
)
from services.guardian.child_memory import (
    KIND_CHILD_ARCHIVE,
    KIND_DIALOGUE_SUMMARY,
    ArchiveFact,
    DialogueSummary,
)
from services.memory_scope.child_layers import ChildLayerStore
from services.memory_scope.tests import test_postgres_store
from services.memory_scope.tests.test_child_layers_postgres import (
    _close_layers,
    _layers,
)

MANIFEST_PATH = f"/v1/devices/{DEVICE_ID}/activation-manifest"

pg_env = test_postgres_store.pg_env


@pytest.mark.asyncio
async def test_unbind_releases_the_fleet_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    key = Ed25519PrivateKey.generate()
    service = app.state.device_onboarding_service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _bound_device(client, app, key)
        before = service.store.get_device(DEVICE_ID)
        assert before.lifecycle_status is DeviceLifecycle.BOUND
        manifest = await client.get(
            MANIFEST_PATH, headers=_signed_headers(key, path=MANIFEST_PATH)
        )
        assert manifest.status_code == 200

        unbound = await client.post(
            f"/v1/devices/{DEVICE_ID}/binding/unbind",
            headers={"Authorization": f"Bearer {owner['access_token']}"},
            json={"reason": "unbind", "purge_subject_data": False},
        )
        assert unbound.status_code == 200, unbound.text
        assert unbound.json()["device_released"] is True

        after = service.store.get_device(DEVICE_ID)
        assert after.lifecycle_status is DeviceLifecycle.PROVISIONED
        assert (after.actor_id, after.binding_id) == (None, None)
        assert after.activation_version == before.activation_version
        # The board now learns it is released and brings its QR back.
        manifest = await client.get(
            MANIFEST_PATH, headers=_signed_headers(key, path=MANIFEST_PATH)
        )
        display = await client.get(PATH, headers=_signed_headers(key))
    assert manifest.status_code == 409
    assert display.status_code == 409
    assert after.certificate_id == CERTIFICATE_ID


async def _layer_counts(
    store: ChildLayerStore, *, subject_id: str, owner_id: str
) -> tuple[int, int]:
    summaries = await store.list(
        subject_id=subject_id,
        kind=KIND_DIALOGUE_SUMMARY,
        viewer_subject_id=owner_id,
    )
    archives = await store.list(
        subject_id=subject_id,
        kind=KIND_CHILD_ARCHIVE,
        viewer_subject_id=owner_id,
    )
    return len(summaries), len(archives)


def _reprovision_payload() -> BootstrapQRPayload:
    return BootstrapQRPayload(
        typ="memoria-device-bootstrap",
        ver=1,
        device_id=DEVICE_ID,
        bootstrap_nonce=b64url_encode(b"reprovision-nonc"),
        ble_name="MEMORIA-TEST",
        ble_service_uuid="12345678-1234-5678-1234-567812345678",
        certificate_id=CERTIFICATE_ID,
        provisioning_protocol="memoria-provisioning/1",
        firmware_version="0.1.0",
        pop=b64url_encode(b"0123456789abcdef-pop"),
    )


async def _bound_child_device(
    client: AsyncClient, app: Any, key: Ed25519PrivateKey
) -> tuple[dict[str, Any], str]:
    service = app.state.device_onboarding_service
    registered = await client.post(
        "/v1/auth/register",
        json={"username": "fleet-child-owner", "password": "safe-password"},
    )
    assert registered.status_code == 201, registered.text
    owner = registered.json()
    _mark_verified_adult(app, owner["user_id"])
    await app.state.identity_service.register_person(
        person_id=owner["user_id"],
        display_name="家长",
        timezone="Asia/Shanghai",
        subject_category="adult",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="fixture-fleet-child-owner",
        now=datetime.now(UTC),
    )
    # Marking the account adult rotates its session authority; log in again
    # exactly like a real phone-verified owner before binding.
    login = await client.post(
        "/v1/auth/login",
        json={"username": "fleet-child-owner", "password": "safe-password"},
    )
    assert login.status_code == 200, login.text
    owner["access_token"] = login.json()["access_token"]
    session_id, claim_id = _online_claim(
        service, actor_id=owner["user_id"], device_key=key
    )
    bound = await client.post(
        "/v1/device-bindings",
        headers={
            "Authorization": f"Bearer {owner['access_token']}",
            "Idempotency-Key": "fleet-child-binding-01",
        },
        json={
            "claim_id": claim_id,
            "onboarding_session_id": session_id,
            "declared_mode": "parent_for_child",
            "account_owner_person_id": owner["user_id"],
            "primary_subject": {
                "person_id": "new",
                "relationship": "guardian_of",
                "subject_draft": {"display_name": "小乐", "age_band": "under_14"},
            },
            "persona_selection": "starlight",
            "service_preferences": {"memory_level": "personal"},
            "consent_offer_ids": [
                "offer_minor_voice_session_v1",
                "offer_minor_memory_retention_v1",
            ],
        },
    )
    assert bound.status_code == 201, bound.text
    return owner, str(bound.json()["primary_subject_ids"][0])


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
)
async def test_release_manifest_and_new_identity_do_not_touch_child_layers(
    pg_env: tuple[str, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    app = _app(monkeypatch, tmp_path, offline_mock=True)
    key = Ed25519PrivateKey.generate()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            owner, child_id = await _bound_child_device(client, app, key)
            owner_id = str(owner["user_id"])
            await store.write_summary(
                DialogueSummary(
                    subject_id=child_id,
                    text="孩子聊了小狗和画画",
                    source_event_id="evt-fleet-keep",
                    session_id="sess-fleet",
                ),
                resource_owner_id=owner_id,
                actor_id=owner_id,
                consent_snapshot_id="consent-fleet-summary",
            )
            await store.write_archive_fact(
                ArchiveFact(subject_id=child_id, field="pet_name", text="宠物叫豆豆"),
                resource_owner_id=owner_id,
                actor_id=owner_id,
                consent_snapshot_id="consent-fleet-archive",
            )
            before = await _layer_counts(
                store, subject_id=child_id, owner_id=owner_id
            )
            layer_calls = record_layer_erasures(monkeypatch, store)
            deletion = FailOnSubjectDelete()
            app.state.child_layer_store = store
            app.state.subject_deletion = deletion

            unbound = await client.post(
                f"/v1/devices/{DEVICE_ID}/binding/unbind",
                headers={"Authorization": f"Bearer {owner['access_token']}"},
                json={"reason": "unbind", "purge_subject_data": False},
            )
            assert unbound.status_code == 200, unbound.text
            assert unbound.json()["device_released"] is True

            # Both device reads an unbound board makes on its way to a local
            # erase: the activation manifest and the idle display poll.
            manifest = await client.get(
                MANIFEST_PATH, headers=_signed_headers(key, path=MANIFEST_PATH)
            )
            display = await client.get(PATH, headers=_signed_headers(key))
            assert manifest.status_code == 409
            assert display.status_code == 409

            service = app.state.device_onboarding_service
            # Re-bootstrap / re-provision creates a new onboarding session.
            session = service.introspect(
                actor_id=owner_id,
                qr_payload=encode_bootstrap_qr(_reprovision_payload(), key),
                client_onboarding_id="reprovision-client",
                client={
                    "platform": "wechat-miniprogram",
                    "app_version": "0.1.0",
                    "base_library_version": "3.0.0",
                },
            )
            assert session["device"]["device_id"] == DEVICE_ID
            # A factory reset that regenerates the device identity key cannot
            # activate the old certificate, and fails closed without deleting.
            with pytest.raises(QRSignatureError):
                service.introspect(
                    actor_id=owner_id,
                    qr_payload=encode_bootstrap_qr(
                        _reprovision_payload(), Ed25519PrivateKey.generate()
                    ),
                    client_onboarding_id="new-identity-client",
                    client={
                        "platform": "wechat-miniprogram",
                        "app_version": "0.1.0",
                        "base_library_version": "3.0.0",
                    },
                )
            # Releasing again is a no-op for the fleet, never an erasure.
            assert (
                service.release_device_binding(
                    device_id=DEVICE_ID,
                    binding_id=str(unbound.json()["binding_id"]),
                )
                is False
            )
            assert layer_calls == []
            assert deletion.calls == []
            assert (
                await _layer_counts(store, subject_id=child_id, owner_id=owner_id)
                == before
            )
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)
