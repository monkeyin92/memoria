from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.control_api.app.main import create_app
from services.digital_self.preview import (
    FIDELITY_CATEGORIES,
    FidelityTrialSpec,
)
from services.speaker.domain import SpeakerProfileSummary


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "memoria.sqlite3"
    monkeypatch.setenv("MEMORIA_DB_PATH", str(path))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    return path


async def _register(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": "safe-password"},
    )
    assert response.status_code == 201
    return response.json()


async def _approved_version(
    app: object,
    client: AsyncClient,
    path: Path,
    owner: dict[str, str],
) -> dict[str, object]:
    occurred_at = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
    event_id = "preview-owner-source"
    await LifeArchive.sqlite(path).record(
        EvidenceEvent(
            event_id=event_id,
            account_id=owner["user_id"],
            event_type="speech.utterance_finalized",
            occurred_at=occurred_at,
            speaker_class="owner",
            source="preview-api-test",
            payload={
                "text": "我喜欢在雨天散步。",
                "interaction_mode": "companion",
                "prompt_kind": "spontaneous",
                "owner_projection_eligible": True,
            },
        )
    )
    app.state.digital_self_registry.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO memory_claims (
                claim_id, account_id, category, subject_key, predicate, value,
                confidence, status, sensitive_domain, extractor_version,
                source_event_id, valid_at, created_at
            ) VALUES (?, ?, 'daily_life', 'owner', 'preference',
                      '我喜欢在雨天散步。', 0.9, 'confirmed', 'personal',
                      'test-v1', ?, ?, ?)
            """,
            (
                "00000000-0000-0000-0000-000000000071",
                owner["user_id"],
                event_id,
                occurred_at.isoformat(),
                occurred_at.isoformat(),
            ),
        )
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    built = (await client.post("/v1/digital-self/versions", headers=headers)).json()
    await client.post(
        f"/v1/digital-self/versions/{built['version_id']}/testing",
        headers=headers,
        json={"expected_manifest_sha256": built["manifest_sha256"]},
    )
    specs = tuple(
        FidelityTrialSpec(
            category=category,
            prompt=f"{category} prompt",
            generic_answer="generic",
            digital_self_answer="digital",
            available=True,
            coverage_gap=None,
            epistemic_status=(
                "unknown"
                if category in {"unknown", "privacy"}
                else "inference"
                if category == "decision"
                else "fact"
            ),
            has_source=category not in {"unknown", "privacy"},
            unsupported_fact=False,
            decision_inference_disclosed=True,
            privacy_refused=True,
            identity_disclosed=True,
        )
        for category in FIDELITY_CATEGORIES
    )
    evaluation = await app.state.self_preview_registry.start_evaluation(
        account_id=owner["user_id"],
        version_id=built["version_id"],
        manifest_sha256=built["manifest_sha256"],
        trial_specs=specs,
        idempotency_key="preview-api-evaluation",
        now=occurred_at,
    )
    for trial in evaluation.trials:
        await app.state.self_preview_registry.submit_trial_choice(
            account_id=owner["user_id"],
            evaluation_id=evaluation.evaluation_id,
            trial_id=trial.trial_id,
            preferred_slot="a" if trial.slot_a == "digital" else "b",
            rationale=None,
            now=occurred_at,
        )
    await app.state.self_preview_registry.complete_evaluation(
        account_id=owner["user_id"],
        evaluation_id=evaluation.evaluation_id,
        verdict="approve",
        rationale=None,
        now=occurred_at,
    )
    approved = await client.post(
        f"/v1/digital-self/versions/{built['version_id']}/approve",
        headers=headers,
        json={
            "password": "safe-password",
            "expected_manifest_sha256": built["manifest_sha256"],
        },
    )
    assert approved.status_code == 200
    return approved.json()


@pytest.mark.asyncio
async def test_owner_preview_grant_freezes_self_preview_session_and_is_one_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _configure(monkeypatch, tmp_path)
    app = create_app()

    class _ActiveSpeaker:
        async def profiles(self, account_id: str) -> tuple[SpeakerProfileSummary, ...]:
            return (
                SpeakerProfileSummary(
                    profile_id="speaker-active",
                    identity_id=f"owner:{account_id}",
                    template_version=1,
                    model_version="test-v1",
                    sample_count=3,
                    status="active",
                    evaluation_ref="test-evaluation",
                    evaluation_sample_count=200,
                    far=0.01,
                    frr=0.01,
                    eer=0.01,
                    unknown_rejection=0.99,
                    created_at="2026-07-23T08:00:00+00:00",
                    activated_at="2026-07-23T08:00:00+00:00",
                    revoked_at=None,
                ),
            )

    app.state.speaker_authority = _ActiveSpeaker()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _register(client, "preview-owner")
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        version = await _approved_version(app, client, path, owner)
        issued = await client.post(
            "/v1/digital-self/preview-grants",
            headers=headers,
            json={
                "version_id": version["version_id"],
                "manifest_sha256": version["manifest_sha256"],
                "perspective": "child",
                "password": "safe-password",
                "idempotency_key": "preview-grant-1",
            },
        )
        grant = issued.json()
        created = await client.post(
            "/v1/sessions",
            headers=headers,
            json={
                "interaction_mode": "self_preview",
                "preview_grant_id": grant["grant_id"],
            },
        )
        replay = await client.post(
            "/v1/sessions",
            headers=headers,
            json={
                "interaction_mode": "self_preview",
                "preview_grant_id": grant["grant_id"],
            },
        )
        second_grant = await client.post(
            "/v1/digital-self/preview-grants",
            headers=headers,
            json={
                "version_id": version["version_id"],
                "manifest_sha256": version["manifest_sha256"],
                "perspective": "owner",
                "password": "safe-password",
                "idempotency_key": "preview-grant-2",
            },
        )
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                DELETE FROM digital_self_fidelity_evaluations
                WHERE account_id = ? AND version_id = ? AND manifest_sha256 = ?
                """,
                (
                    owner["user_id"],
                    version["version_id"],
                    version["manifest_sha256"],
                ),
            )
        legacy_direct_grant = await client.post(
            "/v1/digital-self/preview-grants",
            headers=headers,
            json={
                "version_id": version["version_id"],
                "manifest_sha256": version["manifest_sha256"],
                "perspective": "owner",
                "password": "safe-password",
                "idempotency_key": "preview-grant-without-fidelity",
            },
        )
        stale_grant_session = await client.post(
            "/v1/sessions",
            headers=headers,
            json={
                "interaction_mode": "self_preview",
                "preview_grant_id": second_grant.json()["grant_id"],
            },
        )

    assert issued.status_code == 201
    assert created.status_code == 200
    interaction = created.json()["interaction"]
    assert interaction["interaction_mode"] == "self_preview"
    assert interaction["digital_self_version_id"] == version["version_id"]
    assert interaction["manifest_sha256"] == version["manifest_sha256"]
    assert interaction["preview_grant_id"] == grant["grant_id"]
    assert interaction["perspective"] == "child"
    assert interaction["simulated_output"] is True
    assert interaction["history_eligible"] is False
    assert interaction["owner_projection_eligible"] is False
    assert interaction["companion_style_id"] is None
    assert interaction["fallback_voice_profile_id"] == "warm_companion"
    assert interaction["fallback_voice_provider"] == "volcengine_doubao"
    assert interaction["fallback_voice_model"] == "seed-tts-2.0"
    assert interaction["fallback_voice_resource_id"] == "seed-tts-2.0"
    assert replay.status_code == 409
    assert second_grant.status_code == 201
    assert legacy_direct_grant.status_code == 409
    assert legacy_direct_grant.json()["detail"]["code"] == "fidelity_approval_required"
    assert stale_grant_session.status_code == 409
    assert stale_grant_session.json()["detail"]["code"] == "preview_version_unavailable"
