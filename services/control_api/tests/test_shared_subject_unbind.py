from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app
from services.control_api.tests.test_bound_subject_binding import _bind, _owner, _RecordingConsent
from services.control_api.tests.test_guardian_api import _configure
from services.identity.tests.test_postgres_revoke_classify import bind


@pytest.mark.asyncio
async def test_unbind_retains_shared_child_data_and_consent(
    monkeypatch, tmp_path, postgres_database
):
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        recorder = _RecordingConsent()
        app.state.bound_subject_consent = recorder
        owner, headers = await _owner(client, app, "shared-unbind")
        created = await _bind(
            client,
            app,
            owner_id=owner,
            headers=headers,
            device_id="device-1",
            declared_mode="parent_for_child",
            relationship="guardian_of",
            age_band="under_14",
            offers=["offer_minor_voice_session_v1"],
        )
        assert created.status_code == 201, created.text
        child = created.json()["primary_subject_ids"][0]
        await bind(app.state.identity_service, child, owner=owner, now=datetime.now(UTC))
        from services.governance.tests.test_subject_release_postgres import seed_layers

        layers = await seed_layers(postgres_database, child)
        app.state.child_layer_store = layers
        try:
            result = await client.post(
                "/v1/devices/device-1/binding/unbind", headers=headers, json={"reason": "unbind"}
            )
            assert result.status_code == 200, result.text
            assert (
                len(
                    await layers.list(
                        subject_id=child, kind="dialogue_summary", viewer_subject_id="parent"
                    )
                )
                == 1
            ), "still-served child summary was erased"
            assert result.json()["subject_deletion"] == "retained_still_served"
            assert recorder.revokes == []
            assert not app.state.subject_deletion.is_subject_deleting(
                account_id=owner, subject_id=child
            )
            assert (
                await app.state.identity_service.get_person(child, actor_person_id=owner)
            ).status == "active"
        finally:
            await layers.close()
