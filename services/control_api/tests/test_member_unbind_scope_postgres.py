from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import FastAPI
from services.control_api.app.routes.identity_lifecycle import UnbindRequest, unbind_device
from services.control_api.tests.test_bound_subject_binding import _RecordingConsent
from services.governance.tests.test_account_data_postgres_bound_child import close, fixture
from starlette.requests import Request


@pytest.mark.asyncio
async def test_member_unbind_immediately_erases_binding_owners_archive(postgres_database, tmp_path):
    f = await fixture(postgres_database, tmp_path)
    async def redact(subject_id, account_id):
        await f.identity.redact_bound_subject(person_id=subject_id, actor_person_id=account_id)
    f.subject._redact_identity = redact
    f.subject._complete_release = f.store.complete_subject_release
    admin = await asyncpg.connect(postgres_database.owner_dsn())
    try:
        await f.identity.register_person(person_id="manager", display_name="Manager", timezone="Asia/Shanghai",
            subject_category="adult", age_band="adult", age_evidence_status="verified", age_evidence_id="fixture")
        binding = await f.identity.get_active_manifest("device-1", actor_person_id="parent")
        await admin.execute("INSERT INTO identity_device_binding_roles(binding_id, person_id, role, status, permissions_json, granted_at) VALUES($1,'manager','device_admin','active','[\"binding.manage\"]',now())", binding.binding_id)
        await admin.execute("INSERT INTO archive_evidence_events(event_id,account_id,event_type,schema_version,occurred_at,subject_id,speaker_class,source,payload,content_sha256) VALUES('owner-child-event','parent','speech.utterance_finalized',1,now(),$1,'owner','asr','{}',repeat('a',64))", f.child)
        app = FastAPI()
        recorder = _RecordingConsent()
        app.state.bound_subject_consent = recorder
        app.state.identity_service = f.identity
        app.state.subject_deletion = f.subject
        request = Request({"type": "http", "app": app})
        result = await unbind_device("device-1", UnbindRequest(reason="unbind"), request,
                                    SimpleNamespace(user_id="manager"))
        assert result["subject_deletion"] == "completed"
        assert recorder.revokes[0]["actor_person_id"] == "parent"
        assert await admin.fetchval("SELECT count(*) FROM archive_evidence_events WHERE event_id='owner-child-event'") == 0
        assert f.ledger.completed() == (("parent", f.child, True),)
        assert await f.store.pending_subject_releases() == ()
    finally:
        await admin.close()
        await close(f)
