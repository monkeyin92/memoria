from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from services.consent.bound_subject import (
    MINOR_SESSION_CAPABILITIES,
    BoundSubjectConsentService,
    BoundSubjectGrant,
)
from services.consent.postgres_store import PostgresConsentStore
from services.control_api.app import device_session_close
from services.control_api.app.database import MemoryStore
from services.control_api.app.guardian_revocation import guardian_revocation_hook
from services.control_api.app.multi_subject_runtime import PostgresMultiSubjectRuntimeControl
from services.control_api.app.session_termination import (
    AccountSessionTerminator,
    RealtimeConnectionRegistry,
)
from services.governance.subject_deletion import SubjectDeletionLedger
from services.governance.subject_ports import SubjectScope
from services.governance.tests.test_subject_deletion import _service
from services.guardian.consent import ConsentRevocation
from services.identity.tests.test_postgres_revoke_classify import setup
from services.session_runtime.commands import (
    StartPersistentSessionCommand,
    SwitchPersistentSubjectCommand,
)
from services.session_runtime.issue_authority import ProfileConsentWithdrawn
from services.session_runtime.postgres_store import PostgresSessionRuntimeStore
from services.session_runtime.profile_service import verify_runtime_profile_payload
from services.session_runtime.service import build_postgres_session_runtime_service
from services.session_runtime.tests.postgres_store_test_support import _seed_verified_device

pytestmark = pytest.mark.asyncio
KEY = b"fixture-profile-key-long-enough-for-hmac"


@pytest_asyncio.fixture
async def authority(postgres_database, tmp_path, monkeypatch):
    database = postgres_database
    identity_store, identity, child = await setup(database, tmp_path)
    consent_store = PostgresConsentStore(database.role_dsn("memoria_consent"))
    await consent_store.initialize()
    consent = BoundSubjectConsentService(
        store=consent_store, identity=identity, provision=consent_store.authorize_bound_subject
    )
    runtime_store = PostgresSessionRuntimeStore(
        dsn=database.role_dsn("memoria_session_api"),
        action_dsn=database.role_dsn("memoria_action_executor"),
        projector_dsn=database.role_dsn("memoria_session_projector"),
    )
    await runtime_store.initialize()
    runtime = build_postgres_session_runtime_service(store=runtime_store, signing_key=KEY)
    control = PostgresMultiSubjectRuntimeControl(identity=identity, sessions=runtime)
    store = MemoryStore(dsn=database.role_dsn("memoria_control"), initialize_schema=False)
    admin = await asyncpg.connect(database.owner_dsn())
    requests = []
    edge_status = [200]
    original = httpx.AsyncClient

    def edge(request):
        requests.append(json.loads(request.content))
        return httpx.Response(edge_status[0], json={"delivered": True})

    monkeypatch.setattr(
        device_session_close.httpx, "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(edge), **kwargs),
    )
    close = partial(
        device_session_close.close_subject_media_session, store=store,
        runtime_service=runtime, directory=None,
        settings=SimpleNamespace(media_edge_internal_control_url="http://edge",
                                 media_edge_internal_control_token="x" * 32),
    )
    terminator = AccountSessionTerminator(
        store=store, connections=RealtimeConnectionRegistry(), close_subject_session=close
    )
    try:
        yield SimpleNamespace(**locals())
    finally:
        await admin.close()
        store.close()
        await consent_store.close()
        await identity_store.close()


async def grant(f, *, owner="parent", device="device-1", source="voice"):
    manifest = await f.identity.get_active_manifest(device, actor_person_id=owner)
    now = datetime.now(UTC)
    if not await f.admin.fetchval("SELECT 1 FROM device_fleet_devices WHERE device_id=$1", device):
        await f.admin.execute(
            "INSERT INTO device_fleet_devices(device_id,family_space_id,binding_id,binding_version,"
            "lifecycle_status,capability_manifest_hash,capabilities,firmware_version,firmware_security_version,"
            "firmware_sha256,bootloader_version,anti_rollback_floor_version,anti_rollback_floor_security_version,created_at,updated_at) "
            "VALUES($1,$2,$3,1,'bound',$4,'[]','test',1,$4,'boot','test',1,$5,$5)",
            device, f"family-{manifest.binding_id}", manifest.binding_id, "a" * 64, now,
        )
        await _seed_verified_device(f.admin, device_id=device, binding_id=manifest.binding_id, now=now)
    await f.consent.grant(BoundSubjectGrant(
        actor_person_id=owner, subject_person_id=f.child, binding_id=manifest.binding_id,
        kind="guardian", capabilities=MINOR_SESSION_CAPABILITIES, source_key=source,
    ))
    return manifest


async def stream(f, sid, *, owner="parent", device="device-1", confirmed=True):
    profile = await f.runtime.start(StartPersistentSessionCommand(
        session_id=sid, actor_id=owner, device_id=device, expected_binding_version=1,
        idempotency_key=f"start-{sid}", now=datetime.now(UTC),
        requested_capabilities=MINOR_SESSION_CAPABILITIES,
        device_bound_subject_id=f.child if confirmed else None,
    ))
    f.store.add_voice_session(
        session_id=sid, user_id=owner, room_name=sid, voice_backend="cascade",
        created_at=datetime.now(UTC).isoformat(), interaction_mode="companion",
        mode_policy_version="v1", digital_self_version_id=None,
    )
    from services.control_api.tests.test_subject_session_close_pipeline import seed
    seed(f.store, sid, f.child)
    with f.store.connection() as connection:
        connection.execute("UPDATE device_media_sessions SET device_id=? WHERE session_id=?", (device, sid))
    return profile


async def test_voice_regrant_rotates_control_without_reusing_closed_voice(authority):
    f = authority
    manifest = await grant(f)
    first = await f.control.ensure_profile(device_id="device-1", session_id=None, actor_id="parent", now=datetime.now(UTC))
    await stream(f, "voice")
    assert "chat" in first.capabilities
    await f.consent.revoke(actor_person_id="parent", subject_person_id=f.child,
        binding_id=manifest.binding_id, capabilities=MINOR_SESSION_CAPABILITIES, reason="voice_revoked")
    downgraded = await f.control.ensure_profile(device_id="device-1", session_id=None, actor_id="parent", now=datetime.now(UTC))
    assert not set(MINOR_SESSION_CAPABILITIES) & set(downgraded.capabilities)
    assert downgraded.active_subject_id == f.child
    assert downgraded.session_epoch > first.session_epoch
    assert await f.runtime.session_state(actor_id="parent", session_id=first.session_id) == "active"
    assert verify_runtime_profile_payload(downgraded.model_dump(mode="json"), signing_key=KEY)
    with pytest.raises(ProfileConsentWithdrawn):
        await f.control.ensure_profile(device_id="device-1", session_id="voice", actor_id="parent", now=datetime.now(UTC))
    assert await f.runtime.session_state(actor_id="parent", session_id="voice") == "closed"
    await grant(f, source="voice-regrant")
    restored = await f.control.refresh_profile(device_id="device-1", actor_id="parent", now=datetime.now(UTC))
    assert restored.session_id == first.session_id
    assert restored.session_epoch > downgraded.session_epoch
    assert set(first.capabilities) <= set(restored.capabilities)
    assert verify_runtime_profile_payload(restored.model_dump(mode="json"), signing_key=KEY)
    assert await f.runtime.session_state(actor_id="parent", session_id="voice") == "closed"


async def test_revocation_closes_only_grantors_binding_with_two_valid_owners(authority):
    f = authority
    await f.identity.register_person(person_id="other", display_name="Other", timezone="Asia/Shanghai",
        subject_category="adult", age_band="adult", age_evidence_status="verified", age_evidence_id="fixture")
    await f.identity.attest_binding_relationship(source_person_id="other", target_person_id=f.child,
        relation_type="guardian_of", actor_person_id="other")
    await f.identity.create_binding(device_id="device-2", declared_mode="parent_for_child",
        account_owner_person_id="other", primary_subject_ids=(f.child,),
        roles=(("other", "guardian"), ("other", "device_admin")),
        service_profile_version="parent_for_child-v1", policy_bundle_version="multi-subject-v1",
        consent_offer_ids=("offer_minor_voice_session_v1",), persona_assignment_id="starlight:v1",
        actor_person_id="other", now=datetime.now(UTC))
    second = await f.identity.get_active_manifest("device-2", actor_person_id="other")
    await f.admin.execute("INSERT INTO identity_device_binding_roles(binding_id,person_id,role,status,permissions_json,granted_at) VALUES($1,'parent','member','active','[]',now())", second.binding_id)
    visible = await f.identity.list_active_manifests_for_person(f.child, actor_person_id="parent")
    assert {manifest.device_id for manifest in visible} == {"device-1", "device-2"}
    await grant(f)
    await grant(f, owner="other", device="device-2", source="other-voice")
    await stream(f, "a")
    await stream(f, "b", owner="other", device="device-2")
    hook = guardian_revocation_hook(f.terminator, identity=f.identity, bound_consent=f.consent)
    await hook(ConsentRevocation(f.child, "parent", "minor_voice_session", "bound_subject"))
    assert f.requests == [{"device_id": "device-1", "session_id": "a"}]
    assert await f.runtime.session_state(actor_id="parent", session_id="a") == "closed"
    assert f.store.get_device_media_session(session_id="a")["closed_at"] is not None
    assert await f.runtime.session_state(actor_id="other", session_id="b") == "active"
    assert f.store.get_device_media_session(session_id="b")["closed_at"] is None
    current, _ = await f.runtime.current(actor_id="other", session_id="b", now=datetime.now(UTC))
    assert "chat" in current.capabilities
    assert await f.terminator.terminate_subject(f.child) == 1


@pytest.mark.parametrize("residue", ["failed", "missing", "actor_mismatch", "member_actor_mismatch"])
async def test_unrelated_residue_does_not_block_target_stop(authority, residue):
    f = authority
    await grant(f)
    await stream(f, "residue", confirmed=False)
    if residue == "failed":
        await f.runtime.fail_session(actor_id="parent", session_id="residue", reason_code="abandoned", now=datetime.now(UTC))
    elif residue == "missing":
        with f.store.connection() as connection:
            connection.execute("UPDATE device_media_sessions SET session_id='missing' WHERE session_id='residue'")
        f.store.delete_voice_sessions(user_id="parent")
    else:
        if residue == "member_actor_mismatch":
            await f.identity.register_person(person_id="stranger", display_name="Member", timezone="Asia/Shanghai",
                subject_category="adult", age_band="adult", age_evidence_status="verified", age_evidence_id="fixture")
            manifest = await f.identity.get_active_manifest("device-1", actor_person_id="parent")
            await f.admin.execute("INSERT INTO identity_device_binding_roles(binding_id,person_id,role,status,permissions_json,granted_at) VALUES($1,'stranger','member','active','[]',now())", manifest.binding_id)
        f.store.register_account(user_id="stranger", username="stranger", username_normalized="stranger", password_hash="fixture", now=datetime.now(UTC).isoformat())
        with f.store.connection() as connection:
            connection.execute("UPDATE voice_sessions SET user_id='stranger' WHERE session_id='residue'")
    await stream(f, "target")
    assert await f.terminator.terminate_subject(f.child) == 1
    assert f.requests == [{"device_id": "device-1", "session_id": "target"}]
    assert f.store.get_device_media_session(session_id="target")["closed_at"] is not None


async def test_target_edge_failure_keeps_saga_checkpoint_retryable(authority, tmp_path):
    f = authority
    await grant(f)
    await stream(f, "target")
    async def terminate(scope):
        return await f.terminator.terminate_subject(scope.subject_id)
    ledger = SubjectDeletionLedger.beside(f.store, tmp_path / "unused")
    ledger.initialize()
    service, parts = _service(tmp_path, ledger=ledger)
    service._terminate_sessions = terminate
    f.edge_status[0] = 503
    with pytest.raises(HTTPException) as exc:
        await service.delete_subject(SubjectScope("parent", f.child))
    assert exc.value.status_code == 503
    assert f.store.get_device_media_session(session_id="target")["closed_at"] is None
    with parts["ledger"]._connect() as connection:
        assert connection.execute("SELECT step FROM subject_deletions").fetchone()[0] == "started"
    assert await f.runtime.session_state(actor_id="parent", session_id="target") == "closed"
    f.edge_status[0] = 200
    assert await service.retry_pending_deletions() == 1
    assert len(f.requests) == 2
    assert f.store.get_device_media_session(session_id="target")["closed_at"] is not None


async def test_target_failed_authority_is_not_skipped(authority):
    f = authority
    await grant(f)
    await stream(f, "failed-target")
    await f.runtime.fail_session(actor_id="parent", session_id="failed-target", reason_code="abandoned", now=datetime.now(UTC))
    with pytest.raises(HTTPException) as exc:
        await f.terminator.terminate_subject(f.child)
    assert exc.value.status_code == 409
    assert f.requests == []
    assert f.store.get_device_media_session(session_id="failed-target")["closed_at"] is None


async def test_missing_runtime_authority_does_not_report_stop_success(authority):
    f = authority
    await grant(f)
    await stream(f, "target")
    close = partial(device_session_close.close_subject_media_session, store=f.store,
        runtime_service=None, directory=None, settings=SimpleNamespace())
    terminator = AccountSessionTerminator(store=f.store, connections=RealtimeConnectionRegistry(), close_subject_session=close)
    with pytest.raises(HTTPException) as exc:
        await terminator.terminate_subject(f.child)
    assert exc.value.status_code == 503
    assert exc.value.detail == {"code": "session_runtime_authority_unavailable"}
    assert f.requests == []
    assert f.store.get_device_media_session(session_id="target")["closed_at"] is None


async def _rewrite_current_profile(f, sid, payload):
    async with f.admin.transaction():
        await f.admin.execute("SET LOCAL session_replication_role = replica")
        await f.admin.execute(
            "UPDATE session_runtime_profiles SET payload_json=$2::jsonb WHERE runtime_profile_id="
            "(SELECT current_runtime_profile_id FROM session_runtime_contexts WHERE session_id=$1)",
            sid, json.dumps(payload),
        )


async def _current_payload(f, sid):
    return json.loads(await f.admin.fetchval(
        "SELECT p.payload_json::text FROM session_runtime_profiles p JOIN session_runtime_contexts c "
        "ON p.runtime_profile_id=c.current_runtime_profile_id WHERE c.session_id=$1", sid))


def _misattribute_projection(f, sid, projection):
    if projection == "stranger":
        f.store.register_account(user_id="stranger", username="stranger", username_normalized="stranger",
            password_hash="fixture", now=datetime.now(UTC).isoformat())
    with f.store.connection() as connection:
        if projection == "stranger":
            connection.execute("UPDATE voice_sessions SET user_id='stranger' WHERE session_id=?", (sid,))
        else:
            connection.execute("DELETE FROM voice_sessions WHERE session_id=?", (sid,))


@pytest.mark.parametrize("projection", ["stranger", "deleted"])
async def test_target_closes_with_signed_actor_whatever_the_projection_says(authority, projection):
    f = authority
    await grant(f)
    await stream(f, "target")
    _misattribute_projection(f, "target", projection)
    assert await f.terminator.terminate_subject(f.child) == 1
    assert f.requests == [{"device_id": "device-1", "session_id": "target"}]
    assert await f.runtime.session_state(actor_id="parent", session_id="target") == "closed"
    assert f.store.get_device_media_session(session_id="target")["closed_at"] is not None


async def test_erasure_saga_waits_for_signed_target_behind_wrong_projection(authority, tmp_path):
    f = authority
    await grant(f)
    await stream(f, "target")
    _misattribute_projection(f, "target", "stranger")
    signed = await _current_payload(f, "target")
    await _rewrite_current_profile(f, "target", {**signed, "active_subject_id": "someone-else"})

    async def terminate(scope):
        return await f.terminator.terminate_subject(scope.subject_id)
    ledger = SubjectDeletionLedger.beside(f.store, tmp_path / "unused")
    ledger.initialize()
    service, parts = _service(tmp_path, ledger=ledger)
    service._terminate_sessions = terminate
    with pytest.raises(HTTPException) as exc:
        await service.delete_subject(SubjectScope("parent", f.child))
    assert exc.value.status_code == 403
    assert f.requests == []
    with parts["ledger"]._connect() as connection:
        assert connection.execute("SELECT step FROM subject_deletions").fetchone()[0] == "started"
    assert await f.runtime.session_state(actor_id="parent", session_id="target") == "active"
    await _rewrite_current_profile(f, "target", signed)
    assert await service.retry_pending_deletions() == 1
    assert f.requests == [{"device_id": "device-1", "session_id": "target"}]
    assert await f.runtime.session_state(actor_id="parent", session_id="target") == "closed"
    assert f.store.get_device_media_session(session_id="target")["closed_at"] is not None


@pytest.mark.parametrize("projector", [None, "memoria_session_api"])
async def test_unreadable_global_authority_blocks_instead_of_reporting_absent(authority, projector):
    f = authority
    await grant(f)
    await stream(f, "target")
    _misattribute_projection(f, "target", "stranger")
    blind = PostgresSessionRuntimeStore(
        dsn=f.database.role_dsn("memoria_session_api"),
        action_dsn=f.database.role_dsn("memoria_action_executor"),
        projector_dsn=projector and f.database.role_dsn(projector),
    )
    close = partial(f.close.func, **{**f.close.keywords,
        "runtime_service": build_postgres_session_runtime_service(store=blind, signing_key=KEY)})
    terminator = AccountSessionTerminator(store=f.store, connections=RealtimeConnectionRegistry(), close_subject_session=close)
    with pytest.raises(HTTPException) as exc:
        await terminator.terminate_subject(f.child)
    assert exc.value.status_code == 503
    assert exc.value.detail == {"code": "session_runtime_authority_unavailable"}
    assert f.requests == []
    assert await f.runtime.session_state(actor_id="parent", session_id="target") == "active"


async def test_rls_hidden_session_of_another_subject_is_left_running(authority):
    f = authority
    await f.identity.register_person(person_id="other", display_name="Other", timezone="Asia/Shanghai",
        subject_category="adult", age_band="adult", age_evidence_status="verified", age_evidence_id="fixture")
    await f.identity.attest_binding_relationship(source_person_id="other", target_person_id=f.child,
        relation_type="guardian_of", actor_person_id="other")
    await f.identity.create_binding(device_id="device-2", declared_mode="parent_for_child",
        account_owner_person_id="other", primary_subject_ids=(f.child,),
        roles=(("other", "guardian"), ("other", "device_admin")),
        service_profile_version="parent_for_child-v1", policy_bundle_version="multi-subject-v1",
        consent_offer_ids=("offer_minor_voice_session_v1",), persona_assignment_id="starlight:v1",
        actor_person_id="other", now=datetime.now(UTC))
    await grant(f)
    await grant(f, owner="other", device="device-2", source="other-voice")
    await stream(f, "target")
    await stream(f, "hidden", owner="other", device="device-2", confirmed=False)
    with f.store.connection() as connection:
        connection.execute("UPDATE voice_sessions SET user_id='parent' WHERE session_id='hidden'")
    assert await f.runtime.session_state(actor_id="parent", session_id="hidden") is None
    assert await f.terminator.terminate_subject(f.child) == 1
    assert f.requests == [{"device_id": "device-1", "session_id": "target"}]
    assert await f.runtime.session_state(actor_id="other", session_id="hidden") == "active"
    assert f.store.get_device_media_session(session_id="hidden")["closed_at"] is None


async def test_subject_switched_after_global_read_loses_the_close_cas(authority, monkeypatch):
    f = authority
    await grant(f)
    await stream(f, "rotated")
    commit = f.runtime._commit_close

    async def switch_first(*args, **kwargs):
        await f.runtime.switch_subject(SwitchPersistentSubjectCommand(
            session_id="rotated", actor_id="parent", subject_id=None, now=datetime.now(UTC),
            requested_capabilities=("chat",)))
        return await commit(*args, **kwargs)
    monkeypatch.setattr(f.runtime, "_commit_close", switch_first)
    with pytest.raises(HTTPException) as exc:
        await f.terminator.terminate_subject(f.child)
    assert exc.value.status_code == 409
    monkeypatch.setattr(f.runtime, "_commit_close", commit)
    assert await f.terminator.terminate_subject(f.child) == 0
    assert f.requests == []
    assert await f.runtime.session_state(actor_id="parent", session_id="rotated") == "active"
    assert f.store.get_device_media_session(session_id="rotated")["closed_at"] is None


async def test_last_unbind_erasure_stops_the_session_its_actor_can_no_longer_see(authority, tmp_path):
    f = authority
    await grant(f)
    await stream(f, "target")
    await f.identity.revoke_binding(device_id="device-1", actor_person_id="parent", reason="unbind")
    assert await f.runtime.session_state(actor_id="parent", session_id="target") is None

    async def terminate(scope):
        return await f.terminator.terminate_subject(scope.subject_id)
    ledger = SubjectDeletionLedger.beside(f.store, tmp_path / "unused")
    ledger.initialize()
    service, parts = _service(tmp_path, ledger=ledger)
    service._terminate_sessions = terminate
    await service.delete_subject(SubjectScope("parent", f.child))
    assert f.requests == [{"device_id": "device-1", "session_id": "target"}]
    assert await f.admin.fetchval("SELECT state FROM session_runtime_contexts WHERE session_id='target'") == "closed"
    assert f.store.get_device_media_session(session_id="target")["closed_at"] is not None
    with parts["ledger"]._connect() as connection:
        assert connection.execute("SELECT step FROM subject_deletions").fetchone()[0] == "completed"
