"""Live PG consent withdrawal through ASGI lifecycle and read boundaries."""

# ruff: noqa: F811
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app
from services.control_api.app.multi_subject_runtime import PostgresMultiSubjectRuntimeControl
from services.control_api.app.security import mint_memoria_access_token
from services.control_api.tests.policy_chain_test_support import (  # noqa: F401
    _configure,
    _session_policy,
    pytestmark,
)
from services.control_api.tests.test_device_control_api import _profile_app
from services.identity.postgres_store import PostgresIdentityStore
from services.identity.service import IdentityService
from services.session_runtime.commands import (
    StartPersistentSessionCommand,
    SwitchPersistentSubjectCommand,
)
from services.session_runtime.errors import PersistentSessionDenied, PersistentSessionNotFound
from services.session_runtime.profile_service import verify_runtime_profile_payload
from services.session_runtime.service import build_postgres_session_runtime_service
from services.session_runtime.tests.postgres_store_test_support import (
    _SIGNING_KEY,
    _dsn_with,
    _seed_binding,
    _seed_self_consent,
    _seed_verified_device,
    postgres_runtime,  # noqa: F401
    postgres_runtime_with_consent,  # noqa: F401
)
from services.session_runtime.tests.test_postgres_action_authority import _revoke_self_consent


@pytest_asyncio.fixture
async def lifecycle(postgres_runtime_with_consent, monkeypatch, tmp_path):
    store, dsn = postgres_runtime_with_consent
    _configure(monkeypatch, tmp_path)
    app = create_app()
    activation, _ = _profile_app(tmp_path, category="adult")
    app.state.device_onboarding_service = activation.state.device_onboarding_service
    app.state.memory_store.update_subject_profile(
        user_id="person_a",
        subject_category="adult",
        birth_year_band="adult",
        age_evidence_status="verified",
        now=datetime.now(UTC).isoformat(),
    )
    admin = await asyncpg.connect(dsn)
    try:
        await _seed_binding(
            admin, actor_id="person_a", device_id="dev_test_01", binding_id="r6-binding"
        )
        await _seed_verified_device(
            admin, device_id="dev_test_01", binding_id="r6-binding", now=datetime.now(UTC)
        )
        evidence, snapshot = await _seed_self_consent(
            admin,
            actor_id="person_a",
            device_id="dev_test_01",
            binding_id="r6-binding",
            capability="memory_recall_private",
            purpose="memory_recall",
            now=datetime.now(UTC),
        )
        await admin.execute(
            "ALTER ROLE memoria_identity_registration LOGIN PASSWORD 'r6-identity-test'"
        )
        await admin.execute("ALTER ROLE memoria_identity LOGIN PASSWORD 'r6-local-identity'")
    finally:
        await admin.close()
    identity_store = PostgresIdentityStore(
        _dsn_with(
            dsn,
            database=dsn.rsplit("/", 1)[1],
            user="memoria_identity",
            password="r6-local-identity",
        ),
        registration_dsn=_dsn_with(
            dsn,
            database=dsn.rsplit("/", 1)[1],
            user="memoria_identity_registration",
            password="r6-identity-test",
        ),
    )
    await identity_store.initialize(expected_role="memoria_identity")
    app.state.identity_service = IdentityService(identity_store)
    service = build_postgres_session_runtime_service(store=store, signing_key=_SIGNING_KEY)
    control = PostgresMultiSubjectRuntimeControl(
        identity=app.state.identity_service, sessions=service
    )
    app.state.session_runtime_service = service
    app.state.multi_subject_runtime = control
    now = datetime.now(UTC)
    app.state.memory_store.create_auth_session(
        session_id="r6-auth",
        user_id="person_a",
        refresh_hash="r6-hash",
        expires_at=(now + timedelta(hours=1)).isoformat(),
        now=now.isoformat(),
    )
    token, _ = mint_memoria_access_token(
        app.state.settings, user_id="person_a", session_id="r6-auth"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        try:
            yield app, service, control, client, dsn, evidence, snapshot
        finally:
            await identity_store.close()


async def issue(env, *, control_session=False):
    app, service, control, client, dsn, evidence, snapshot = env
    if control_session:
        response = await client.get("/v1/devices/dev_test_01/runtime-profile")
        assert response.status_code == 200, response.text
        payload = response.json()
        profile, _ = await service.current(
            actor_id="person_a", session_id=payload["session_id"], now=datetime.now(UTC)
        )
    else:
        profile = await service.start(
            StartPersistentSessionCommand(
                session_id="r6-conversation",
                actor_id="person_a",
                device_id="dev_test_01",
                expected_binding_version=1,
                idempotency_key="r6-start",
                now=datetime.now(UTC),
                confirm_sole_bound_subject=True,
                requested_capabilities=("chat", "tutor", "memory_recall_private"),
            )
        )
    app.state.memory_store.add_voice_session(
        session_id=profile.session_id,
        user_id="person_a",
        room_name="r6-room",
        voice_backend="cascade",
        created_at=datetime.now(UTC).isoformat(),
        interaction_mode="companion",
        mode_policy_version="companion-v1",
        digital_self_version_id=None,
    )
    assert "memory_recall_private" in profile.capabilities
    assert verify_runtime_profile_payload(profile.model_dump(mode="json"), signing_key=_SIGNING_KEY)
    return profile


async def revoke(env):
    admin = await asyncpg.connect(env[4])
    try:
        await _revoke_self_consent(admin, evidence=env[5], snapshot=env[6], now=datetime.now(UTC))
    finally:
        await admin.close()


async def persisted(env, sid):
    admin = await asyncpg.connect(env[4])
    try:
        row = await admin.fetchrow(
            "SELECT c.*, p.payload_json FROM session_runtime_contexts c JOIN session_runtime_profiles p ON p.runtime_profile_id=c.current_runtime_profile_id WHERE c.session_id=$1",
            sid,
        )
        import json

        return dict(row), json.loads(row["payload_json"])
    finally:
        await admin.close()


@pytest.mark.parametrize("action", ["get", "refresh", "resolve", "switch", "unknown"])
async def test_withdrawal_allows_safe_rotation(lifecycle, action):
    app, service, control, client, *_ = lifecycle
    old = await issue(lifecycle, control_session=action in {"get", "refresh", "resolve"})
    if action == "switch":
        admin = await asyncpg.connect(lifecycle[4])
        try:
            await admin.execute(
                "INSERT INTO identity_device_binding_roles (binding_id, person_id, role, status, permissions_json, granted_at) VALUES ('r6-binding', 'actor-b', 'member', 'active', '[]', now())"
            )
            await admin.execute("""INSERT INTO identity_relationships (
                relationship_id, source_person_id, target_person_id, relation_type,
                status, valid_from, established_evidence_id, confirmed_by_source_at,
                confirmed_by_target_at, requires_confirmation, can_delegate, created_at, updated_at
            ) VALUES ('r6-delegate', 'person_a', 'actor-b', 'delegate_for', 'active', now(),
                'r6-confirmed-delegate', now(), now(), true, true, now(), now())""")
        finally:
            await admin.close()
    await revoke(lifecycle)
    with pytest.raises(PersistentSessionDenied):
        await service.current(actor_id="person_a", session_id=old.session_id, now=datetime.now(UTC))
    denied = await _session_policy(client, session_id=old.session_id)
    assert denied.status_code == 503, denied.text
    if action == "get":
        response = await client.get("/v1/devices/dev_test_01/runtime-profile")
        assert response.status_code == 200, response.text
        payload = response.json()
    elif action == "refresh":
        profile = await control.refresh_profile(
            device_id="dev_test_01", actor_id="person_a", now=datetime.now(UTC)
        )
        response = await client.get("/v1/devices/dev_test_01/runtime-profile")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["runtime_profile_id"] == profile.runtime_profile_id
    elif action == "resolve":
        response = await client.post(
            "/v1/sessions/resolve-subject", json={"device_id": "dev_test_01"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["resolution"] == "confirmed"
        row, payload = await persisted(lifecycle, old.session_id)
        assert response.json()["runtime_profile_id"] == payload["runtime_profile_id"]
    elif action == "switch":
        response = await client.post(
            f"/v1/sessions/{old.session_id}/active-subject", json={"person_id": "actor-b"}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
    else:
        profile = await service.switch_subject(
            SwitchPersistentSubjectCommand(
                session_id=old.session_id,
                actor_id="person_a",
                subject_id=None,
                now=datetime.now(UTC),
                requested_capabilities=("chat", "tutor", "memory_recall_private"),
            )
        )
        payload = profile.model_dump(mode="json")
        assert payload["active_subject_id"] is None
    assert verify_runtime_profile_payload(payload, signing_key=_SIGNING_KEY)
    assert "memory_recall_private" not in payload["capabilities"]
    assert "chat" in payload["capabilities"]
    if action == "switch":
        assert payload["active_subject_id"] == "actor-b"
        assert "tutor" not in payload["capabilities"]
    row, stored = await persisted(lifecycle, old.session_id)
    assert stored == payload
    assert row["state"] == "active"
    assert row["session_epoch"] == payload["session_epoch"] > old.session_epoch
    assert row["generation_id"] > 0 and row["tool_epoch"] > 0
    stale = await client.post(
        "/v1/policy/decisions",
        json={"runtime_profile_id": old.runtime_profile_id, "capability": "chat"},
    )
    assert stale.status_code == 403, stale.text
    private = await client.post(
        "/v1/policy/decisions",
        json={
            "runtime_profile_id": payload["runtime_profile_id"],
            "capability": "memory_recall_private",
            "data_classification": "private",
        },
    )
    assert private.status_code == 200, private.text
    assert private.json()["effect"] == "deny"
    assert private.json()["reason_code"] == "capability_not_in_runtime_profile"


@pytest.mark.parametrize(
    "capability,classification,allowed",
    [
        ("chat", "ephemeral", True),
        ("tutor", "ephemeral", True),
        ("chat", "private", False),
        ("memory_recall_private", "private", False),
    ],
)
async def test_live_decision_after_withdrawal(lifecycle, capability, classification, allowed):
    app, service, control, client, *_ = lifecycle
    profile = await issue(lifecycle)
    await revoke(lifecycle)
    response = await client.post(
        "/v1/policy/decisions",
        json={
            "runtime_profile_id": profile.runtime_profile_id,
            "capability": capability,
            "data_classification": classification,
        },
    )
    if allowed:
        assert response.status_code == 200, response.text
        assert response.json()["effect"] in {"allow", "allow_with_obligations"}
    else:
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "policy_actor_mismatch"
    row, stored = await persisted(lifecycle, profile.session_id)
    assert stored == profile.model_dump(mode="json")
    assert row["session_epoch"] == profile.session_epoch
    with pytest.raises(PersistentSessionDenied):
        await service.authorize_action(
            actor_id="person_a",
            session_id=profile.session_id,
            runtime_profile_id=profile.runtime_profile_id,
            capability="memory_recall_private",
            session_epoch=profile.session_epoch,
            generation_id=1,
            turn_id=1,
            tool_epoch=0,
            data_classification="private",
            safety_state="normal",
            now=datetime.now(UTC),
        )


@pytest.mark.parametrize("terminal", ["failed", "closed"])
async def test_withdrawal_terminal_cleanup(lifecycle, terminal):
    app, service, control, client, *_ = lifecycle
    old = await issue(lifecycle)
    await revoke(lifecycle)
    if terminal == "failed":
        await service.fail_session(
            actor_id="person_a",
            session_id=old.session_id,
            reason_code="startup_rejected",
            now=datetime.now(UTC),
        )
    else:
        result = await service.close_session(
            actor_id="person_a",
            session_id=old.session_id,
            reason_code="requested",
            now=datetime.now(UTC),
        )
        assert result.applied
    row, stored = await persisted(lifecycle, old.session_id)
    assert row["state"] == terminal
    assert row["session_epoch"] == old.session_epoch + 1
    assert row["active_subject_id"] is None
    with pytest.raises(PersistentSessionNotFound):
        await service.current(actor_id="person_a", session_id=old.session_id, now=datetime.now(UTC))
    rejected = await client.get(
        "/v1/devices/dev_test_01/runtime-profile", params={"session_id": old.session_id}
    )
    assert rejected.status_code == 403, rejected.text
    assert (await persisted(lifecycle, old.session_id))[0]["state"] == terminal


async def test_session_state_read_fails_closed_after_withdrawal(lifecycle):
    app, service, control, client, *_ = lifecycle
    profile = await issue(lifecycle)

    async def status(**_):
        return {
            "device_id": "dev_test_01",
            "connected": True,
            "session_id": profile.session_id,
            "stream_epoch": 18,
            "assistant_state": {
                "session_id": profile.session_id,
                "stream_epoch": 18,
                "sequence": 1,
                "phase": "speaking",
                "observed_at": datetime.now(UTC).isoformat(),
                "fence": {
                    "session_epoch": profile.session_epoch,
                    "generation_id": 0,
                    "turn_id": 0,
                    "tool_epoch": 0,
                },
            },
        }

    app.state.device_runtime_status_reader = status
    before = await client.get("/v1/devices/dev_test_01/session-state")
    assert before.status_code == 200, before.text
    assert before.json()["status"] == "online"
    await revoke(lifecycle)
    after = await client.get("/v1/devices/dev_test_01/session-state")
    assert after.status_code == 200, after.text
    assert after.json()["status"] == "unknown"
    assert after.json()["phase"] is None
    assert "snapshot" not in after.json()
    assert (await persisted(lifecycle, profile.session_id))[1] == profile.model_dump(mode="json")


@pytest.mark.parametrize(
    "fault", ["actor", "device", "revoked", "rebound", "projection", "closed", "failed"]
)
async def test_control_downgrade_never_revives_invalid_authority(lifecycle, fault):
    app, service, control, client, *_ = lifecycle
    old = await issue(lifecycle, control_session=True)
    await revoke(lifecycle)
    admin = await asyncpg.connect(lifecycle[4])
    requested_device = "dev_test_01"
    try:
        if fault == "actor":
            now = datetime.now(UTC)
            app.state.memory_store.create_auth_session(
                session_id="r6-other",
                user_id="actor-b",
                refresh_hash="other",
                expires_at=(now + timedelta(hours=1)).isoformat(),
                now=now.isoformat(),
            )
            token, _ = mint_memoria_access_token(
                app.state.settings, user_id="actor-b", session_id="r6-other"
            )
            client.headers["Authorization"] = f"Bearer {token}"
        elif fault == "device":
            await admin.execute("""INSERT INTO identity_device_bindings (
                binding_id, device_id, declared_mode, account_owner_person_id, binding_version,
                status, reason, valid_from, service_profile_version, policy_bundle_version, persona_assignment_id, created_at)
                SELECT 'r6-second', 'r6-second-device', declared_mode, account_owner_person_id,
                binding_version, status, reason, valid_from, service_profile_version,
                policy_bundle_version, persona_assignment_id, created_at FROM identity_device_bindings WHERE binding_id='r6-binding'""")
            await admin.execute(
                """INSERT INTO identity_device_binding_roles SELECT 'r6-second', person_id, role, status, permissions_json, granted_at, ended_at FROM identity_device_binding_roles WHERE binding_id='r6-binding'"""
            )
            requested_device = "r6-second-device"
        elif fault in {"revoked", "rebound"}:
            await admin.execute(
                "UPDATE identity_device_bindings SET status=$1 WHERE binding_id='r6-binding'",
                "revoked" if fault == "revoked" else "superseded",
            )
            if fault == "rebound":
                await admin.execute("""INSERT INTO identity_device_bindings (
                    binding_id, device_id, declared_mode, account_owner_person_id, binding_version,
                    status, reason, valid_from, supersedes_binding_id, service_profile_version,
                    policy_bundle_version, persona_assignment_id, created_at)
                    SELECT 'r6-rebound', device_id, declared_mode, account_owner_person_id, 2,
                    'active', 'supersede', now(), binding_id, service_profile_version,
                    policy_bundle_version, persona_assignment_id, now() FROM identity_device_bindings WHERE binding_id='r6-binding'""")
                await admin.execute(
                    """INSERT INTO identity_device_binding_roles SELECT 'r6-rebound', person_id, role, status, permissions_json, granted_at, ended_at FROM identity_device_binding_roles WHERE binding_id='r6-binding'"""
                )
        elif fault == "projection":
            await admin.execute(
                "UPDATE session_runtime_contexts SET binding_version=2 WHERE session_id=$1",
                old.session_id,
            )
        else:
            await admin.execute(
                "UPDATE session_runtime_contexts SET state=$1 WHERE session_id=$2",
                fault,
                old.session_id,
            )
    finally:
        await admin.close()
    before, payload = await persisted(lifecycle, old.session_id)
    response = await client.get(
        f"/v1/devices/{requested_device}/runtime-profile", params={"session_id": old.session_id}
    )
    assert response.status_code in {403, 404, 503}, response.text
    after, stored = await persisted(lifecycle, old.session_id)
    assert after == before and stored == payload
    response = await client.post(
        f"/v1/sessions/{old.session_id}/active-subject",
        json={"person_id": "actor-b" if fault == "actor" else "person_a"},
    )
    if fault != "device":
        assert response.status_code in {403, 404, 503}, response.text
        assert (await persisted(lifecycle, old.session_id))[0] == before


async def test_expired_conversation_cannot_rotate_or_decide(lifecycle):
    app, service, control, client, *_ = lifecycle
    old = await issue(lifecycle)
    await revoke(lifecycle)
    with pytest.raises(PersistentSessionDenied):
        await service.switch_subject(
            SwitchPersistentSubjectCommand(
                session_id=old.session_id,
                actor_id="person_a",
                subject_id="person_a",
                now=old.expires_at,
                requested_capabilities=("chat",),
            )
        )
    with pytest.raises(PersistentSessionDenied):
        await service.decide(
            runtime_profile_id=old.runtime_profile_id,
            actor_id="person_a",
            capability="chat",
            data_classification="ephemeral",
            safety_state="normal",
            now=old.expires_at,
        )
    assert (await persisted(lifecycle, old.session_id))[0]["session_epoch"] == old.session_epoch


async def test_chat_does_not_restore_unsigned_tutor_or_private_capability(lifecycle):
    app, service, control, client, *_ = lifecycle
    old = await issue(lifecycle)
    limited = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=old.session_id,
            actor_id="person_a",
            subject_id="person_a",
            now=datetime.now(UTC),
            requested_capabilities=("chat", "memory_recall_private"),
        )
    )
    await revoke(lifecycle)
    for cap, effect in (("chat", "allow"), ("tutor", "deny")):
        response = await client.post(
            "/v1/policy/decisions",
            json={
                "runtime_profile_id": limited.runtime_profile_id,
                "capability": cap,
                "data_classification": "ephemeral",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["effect"] == effect
        if cap == "tutor":
            assert response.json()["reason_code"] == "capability_not_in_runtime_profile"
    assert (await persisted(lifecycle, limited.session_id))[1] == limited.model_dump(mode="json")
    with pytest.raises(PersistentSessionDenied):
        await service.current(
            actor_id="person_a", session_id=limited.session_id, now=datetime.now(UTC)
        )
