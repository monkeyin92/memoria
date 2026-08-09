from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "minor-route-auth-secret-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "minor-route-archive-token")
    monkeypatch.setenv("OFFLINE_MOCK", "true")


@pytest.mark.asyncio
async def test_every_minor_forbidden_route_family_uses_the_subject_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "minor-route-user", "password": "safe-password"},
            )
        ).json()
        app.state.memory_store.update_subject_profile(
            user_id=identity["user_id"],
            subject_category="minor",
            birth_year_band="14_to_17",
            now=datetime.now(UTC).isoformat(),
        )
        refreshed = (
            await client.post(
                "/v1/auth/login",
                json={"username": "minor-route-user", "password": "safe-password"},
            )
        ).json()
        auth = {"Authorization": f"Bearer {refreshed['access_token']}"}
        checks = [
            (
                await client.post(
                    "/v1/voices/consent",
                    headers=auth,
                    json={"accepted": True, "policy_version": "voice-clone-v1"},
                ),
                "voice_clone",
            ),
            (await client.get("/v1/digital-self/versions", headers=auth), "digital_self"),
            (
                await client.get("/v1/digital-self/preview-capability", headers=auth),
                "self_preview",
            ),
            (
                await client.get("/v1/legacy/grants", params={"role": "owner"}, headers=auth),
                "legacy_grant",
            ),
            (
                await client.get("/v1/legacy/grants", params={"role": "grantee"}, headers=auth),
                "legacy_receive",
            ),
            (await client.get("/v1/speakers", headers=auth), "speaker_enrollment"),
            (await client.get("/v1/archive/raw-voice-consent", headers=auth), "raw_voice_archive"),
            (
                await client.post(
                    "/v1/sessions",
                    headers=auth,
                    json={
                        "interaction_mode": "self_preview",
                        "preview_grant_id": "blocked-preview-grant",
                    },
                ),
                "self_preview",
            ),
            (
                await client.post(
                    "/v1/sessions",
                    headers=auth,
                    json={
                        "interaction_mode": "legacy",
                        "legacy_grant_id": "blocked-legacy-grant",
                    },
                ),
                "legacy_receive",
            ),
        ]
        voice_session = await client.post("/v1/sessions", headers=auth, json={})
        tutor_session = await client.post(
            "/v1/sessions",
            headers=auth,
            json={"session_focus": "tutor_english"},
        )

    for response, capability in checks:
        assert response.status_code == 403
        assert response.json()["detail"] == {
            "code": "minor_forbidden",
            "capability": capability,
        }
    for response in (voice_session, tutor_session):
        assert response.status_code == 403
        assert response.json()["detail"] == {
            "code": "guardian_consent_required",
            "capability": "minor_voice_session",
        }
