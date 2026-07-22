from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.control_api.app.main import create_app
from services.digital_self.registry import DigitalSelfRegistry


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "memoria.sqlite3"
    monkeypatch.setenv("MEMORIA_DB_PATH", str(path))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    return path


async def _register(client: AsyncClient, username: str, password: str = "safe-password") -> dict[str, str]:
    response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": password},
    )
    assert response.status_code == 201
    return response.json()


async def _seed_confirmed_owner_memory(
    app: object,
    path: Path,
    *,
    account_id: str,
    value: str = "我喜欢在雨天散步。",
) -> str:
    event_id = f"digital-self-source-{account_id}"
    occurred_at = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
    await LifeArchive.sqlite(path).record(
        EvidenceEvent(
            event_id=event_id,
            account_id=account_id,
            event_type="speech.utterance_finalized",
            occurred_at=occurred_at,
            speaker_class="owner",
            source="digital-self-api-test",
            payload={
                "text": value,
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
            },
        )
    )
    registry = app.state.digital_self_registry
    assert isinstance(registry, DigitalSelfRegistry)
    registry.initialize()
    now = occurred_at.isoformat()
    claim_id = "00000000-0000-0000-0000-000000000001"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO memory_claims (
                claim_id, account_id, category, subject_key, predicate, value,
                confidence, status, sensitive_domain, extractor_version,
                source_event_id, valid_at, created_at
            ) VALUES (?, ?, 'daily_life', 'owner', 'preference', ?, 0.9,
                      'confirmed', 'personal', 'test-v1', ?, ?, ?)
            """,
            (claim_id, account_id, value, event_id, now, now),
        )
    return claim_id


def _headers(identity: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {identity['access_token']}"}


@pytest.mark.asyncio
async def test_digital_self_build_rejects_empty_source_and_lists_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "digital-self-empty")
        headers = _headers(owner)
        built = await client.post("/v1/digital-self/versions", headers=headers)
        listed = await client.get("/v1/digital-self/versions", headers=headers)

    assert built.status_code == 422
    assert built.json()["detail"] == {"code": "empty_source"}
    assert listed.json() == {"items": []}


@pytest.mark.asyncio
async def test_digital_self_build_list_get_and_lifecycle_step_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _configure(monkeypatch, tmp_path)
    app = create_app()
    password = " safe-password "
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "digital-self-owner", password=password)
        headers = _headers(owner)
        await _seed_confirmed_owner_memory(app, path, account_id=owner["user_id"])
        built = await client.post("/v1/digital-self/versions", headers=headers)
        version = built.json()
        version_id = version["version_id"]
        digest = version["manifest_sha256"]
        listed = await client.get("/v1/digital-self/versions", headers=headers)
        fetched = await client.get(f"/v1/digital-self/versions/{version_id}", headers=headers)
        wrong_digest = await client.post(
            f"/v1/digital-self/versions/{version_id}/testing",
            headers=headers,
            json={"expected_manifest_sha256": "0" * 64},
        )
        testing = await client.post(
            f"/v1/digital-self/versions/{version_id}/testing",
            headers=headers,
            json={"expected_manifest_sha256": digest},
        )
        wrong_password = await client.post(
            f"/v1/digital-self/versions/{version_id}/approve",
            headers=headers,
            json={"password": "wrong-password", "expected_manifest_sha256": digest},
        )
        approved = await client.post(
            f"/v1/digital-self/versions/{version_id}/approve",
            headers=headers,
            json={"password": password, "expected_manifest_sha256": digest},
        )
        frozen = await client.post(
            f"/v1/digital-self/versions/{version_id}/freeze",
            headers=headers,
            json={"password": password, "expected_manifest_sha256": digest},
        )
        revoked = await client.post(
            f"/v1/digital-self/versions/{version_id}/revoke",
            headers=headers,
            json={"password": password, "expected_manifest_sha256": digest},
        )
        rollback = await client.post(
            f"/v1/digital-self/versions/{version_id}/rollback",
            headers=headers,
            json={"password": password, "expected_manifest_sha256": digest},
        )

    assert built.status_code == 201
    assert version["status"] == "draft"
    assert version["source_summary"]["memory_claim_count"] == 1
    assert version["manifest"]["entries"][0]["value"] == "我喜欢在雨天散步。"
    assert listed.json()["items"] == [version]
    assert fetched.json() == version
    assert wrong_digest.json()["detail"] == {"code": "source_snapshot_conflict"}
    assert testing.json()["status"] == "testing"
    assert wrong_password.json()["detail"] == {"code": "step_up_failed"}
    assert approved.json()["status"] == "approved"
    assert frozen.json()["status"] == "frozen"
    assert revoked.json()["status"] == "revoked"
    assert rollback.json()["status"] == "draft"
    assert rollback.json()["manifest"]["rollback_target_version_id"] == version_id


@pytest.mark.asyncio
async def test_digital_self_cross_account_versions_are_not_found(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "digital-self-owner")
        other = await _register(client, "digital-self-other")
        await _seed_confirmed_owner_memory(app, path, account_id=owner["user_id"])
        version_id = (
            await client.post("/v1/digital-self/versions", headers=_headers(owner))
        ).json()["version_id"]
        response = await client.get(
            f"/v1/digital-self/versions/{version_id}",
            headers=_headers(other),
        )

    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "version_not_found"}


@pytest.mark.asyncio
async def test_digital_self_deleting_account_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "digital-self-deleting")
        await app.state.account_operations.block_account(owner["user_id"])
        response = await client.post("/v1/digital-self/versions", headers=_headers(owner))

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "account_deletion_in_progress"}


@pytest.mark.asyncio
async def test_digital_self_source_correction_creates_new_version_without_mutating_old_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "digital-self-history")
        headers = _headers(owner)
        claim_id = await _seed_confirmed_owner_memory(app, path, account_id=owner["user_id"])
        first = (await client.post("/v1/digital-self/versions", headers=headers)).json()
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE memory_claims SET value = '我更喜欢晴天散步。' WHERE claim_id = ?",
                (claim_id,),
            )
        second = (await client.post("/v1/digital-self/versions", headers=headers)).json()
        reloaded_first = await client.get(
            f"/v1/digital-self/versions/{first['version_id']}", headers=headers
        )

    assert second["version_number"] == 2
    assert second["parent_version_id"] == first["version_id"]
    assert second["manifest_sha256"] != first["manifest_sha256"]
    assert reloaded_first.json() == first
