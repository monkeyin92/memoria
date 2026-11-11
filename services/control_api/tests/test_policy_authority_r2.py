"""Real PostgreSQL consent lifetime at both public policy entry points."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app
from services.control_api.app.multi_subject_runtime import PostgresMultiSubjectRuntimeControl
from services.control_api.tests.policy_chain_test_support import (
    _anonymous_account,
    _configure,
    _response_plan,
    _session_policy,
    pytestmark,  # noqa: F401
)
from services.session_runtime.commands import SwitchPersistentSubjectCommand
from services.session_runtime.profile_service import verify_runtime_profile_payload
from services.session_runtime.service import build_postgres_session_runtime_service
from services.session_runtime.tests.postgres_store_test_support import (
    _SIGNING_KEY,
    _seed_delegated_binding,
    _seed_self_consent,
    _seed_verified_device,
    postgres_runtime,  # noqa: F401
    postgres_runtime_with_consent,  # noqa: F401
)
from services.session_runtime.tests.test_postgres_action_authority import _revoke_self_consent


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["valid", "expired", "revoked"])
async def test_signed_private_capability_tracks_live_consent(
    postgres_runtime_with_consent,  # noqa: F811
    monkeypatch,
    tmp_path,
    state,  # noqa: F811
):
    store, dsn = postgres_runtime_with_consent
    _configure(monkeypatch, tmp_path)
    app = create_app()
    service = build_postgres_session_runtime_service(store=store, signing_key=_SIGNING_KEY)
    app.state.session_runtime_service = service
    app.state.multi_subject_runtime = PostgresMultiSubjectRuntimeControl(
        identity=app.state.identity_service,
        sessions=service,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        actor, headers = await _anonymous_account(client)
        admin = await asyncpg.connect(dsn)
        try:
            await _seed_delegated_binding(
                admin,
                actor_id=actor,
                subject_id="other",
                device_id="r2-device",
                binding_id="r2-binding",
                declared_mode="self_use",
                subject_category="adult",
                age_band="adult",
            )
            now = datetime.now(UTC)
            await _seed_verified_device(
                admin, device_id="r2-device", binding_id="r2-binding", now=now
            )
            evidence, snapshot = await _seed_self_consent(
                admin,
                actor_id=actor,
                device_id="r2-device",
                binding_id="r2-binding",
                capability="memory_recall_private",
                purpose="memory_recall",
                now=now,
                evidence_valid_until=now + timedelta(seconds=3) if state == "expired" else None,
            )
        finally:
            await admin.close()
        opened = await client.post(
            "/v1/sessions",
            headers={**headers, "Idempotency-Key": "r2-open"},
            json={
                "client": {
                    "platform": "web",
                    "timezone": "Asia/Shanghai",
                    "device_id": "r2-device",
                    "binding_version": 1,
                },
            },
        )
        assert opened.status_code == 200, opened.text
        session_id = opened.json()["session_id"]
        profile = await service.switch_subject(
            SwitchPersistentSubjectCommand(
                session_id=session_id,
                actor_id=actor,
                subject_id=actor,
                now=datetime.now(UTC),
                requested_capabilities=("chat", "memory_recall_private"),
            )
        )
        assert "memory_recall_private" in {x.value for x in profile.capabilities}
        before = await _session_policy(client, session_id=session_id)
        assert before.status_code == 200, before.text
        assert verify_runtime_profile_payload(
            before.json()["runtime_profile"], signing_key=_SIGNING_KEY
        )
        if state == "expired":
            await asyncio.sleep(
                max(0, (evidence.valid_until - datetime.now(UTC)).total_seconds()) + 0.02
            )
        if state == "revoked":
            admin = await asyncpg.connect(dsn)
            try:
                await _revoke_self_consent(
                    admin, evidence=evidence, snapshot=snapshot, now=datetime.now(UTC)
                )
            finally:
                await admin.close()
        decision = await client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": profile.runtime_profile_id,
                "capability": "memory_recall_private",
                "data_classification": "private",
                "safety_state": "normal",
            },
        )
        if state == "valid":
            assert decision.status_code == 200, decision.text
            assert decision.json()["effect"] in ("allow", "allow_with_obligations")
        else:
            assert decision.status_code == 403, decision.text
            assert decision.json()["detail"]["code"] == "policy_actor_mismatch"
        policy = await _session_policy(client, session_id=session_id)
        if state == "valid":
            assert policy.status_code == 200, policy.text
            assert policy.json()["capabilities"]["private_memory"] is True
            from services.policy.action_authorizer import ActionAuthorizationError

            async def unavailable(*args, **kwargs):
                raise ActionAuthorizationError("synthetic consent discovery unavailable")

            original_discovery = service._consent.discover_current
            monkeypatch.setattr(service._consent, "discover_current", unavailable)
            unavailable_policy = await _session_policy(client, session_id=session_id)
            assert unavailable_policy.status_code == 503, unavailable_policy.text
            unavailable_decision = await client.post(
                "/v1/policy/decisions",
                headers=headers,
                json={
                    "runtime_profile_id": profile.runtime_profile_id,
                    "capability": "memory_recall_private",
                    "data_classification": "private",
                },
            )
            assert unavailable_decision.status_code == 503, unavailable_decision.text
            unavailable_plan = await _response_plan(
                client,
                session_id=session_id,
                query="我们以前聊过什么？",
                turn_id=3,
                generation_id=3,
            )
            assert unavailable_plan.status_code == 200, unavailable_plan.text
            assert unavailable_plan.json()["grounded_items"] == []
            monkeypatch.setattr(service._consent, "discover_current", original_discovery)
            admin = await asyncpg.connect(dsn)
            try:
                await admin.execute(
                    "UPDATE identity_device_bindings SET status='superseded' WHERE binding_id='r2-binding'"
                )
                await admin.execute("""INSERT INTO identity_device_bindings (
                    binding_id, device_id, declared_mode, family_space_id, account_owner_person_id,
                    binding_version, status, reason, valid_from, supersedes_binding_id,
                    service_profile_version, policy_bundle_version, persona_assignment_id, created_at
                ) SELECT 'r2-rebound', device_id, declared_mode, family_space_id, 'other', 2,
                    'active', 'supersede', now(), binding_id, service_profile_version,
                    policy_bundle_version, persona_assignment_id, now()
                    FROM identity_device_bindings WHERE binding_id='r2-binding'""")
            finally:
                await admin.close()
            rebound_policy = await _session_policy(client, session_id=session_id)
            assert rebound_policy.status_code == 503, rebound_policy.text
            rebound_decision = await client.post(
                "/v1/policy/decisions",
                headers=headers,
                json={
                    "runtime_profile_id": profile.runtime_profile_id,
                    "capability": "memory_recall_private",
                    "data_classification": "private",
                },
            )
            assert rebound_decision.status_code == 403, rebound_decision.text
            rebound_plan = await _response_plan(
                client,
                session_id=session_id,
                query="我们以前聊过什么？",
                turn_id=4,
                generation_id=4,
            )
            assert rebound_plan.status_code == 200, rebound_plan.text
            assert rebound_plan.json()["grounded_items"] == []
        else:
            assert policy.status_code == 503, policy.text
            assert policy.json()["detail"]["code"] == "session_runtime_authority_unavailable"
            plan = await _response_plan(
                client,
                session_id=session_id,
                query="我们以前聊过什么？",
                turn_id=2,
                generation_id=2,
            )
            assert plan.status_code == 200, plan.text
            assert plan.json()["grounded_items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("age", ["under_14", "14_17", "adult", "unknown_safe"])
@pytest.mark.parametrize("state", ["valid", "expired", "revoked"])
async def test_age_retention_dual_entry_matrix(
    postgres_runtime,  # noqa: F811
    monkeypatch,
    tmp_path,
    age,
    state,  # noqa: F811
):
    from services.control_api.tests.policy_r2_support import guardian_store
    from services.guardian.consent import GuardianConsentService

    store, dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    service = build_postgres_session_runtime_service(store=store, signing_key=_SIGNING_KEY)
    app.state.session_runtime_service = service
    app.state.multi_subject_runtime = PostgresMultiSubjectRuntimeControl(
        identity=app.state.identity_service, sessions=service
    )
    guardian = await guardian_store(dsn)
    app.state.guardian_store = guardian
    consent = GuardianConsentService(guardian, app.state.life_archive)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            actor, headers = await _anonymous_account(client)
            minor = age in ("under_14", "14_17")
            admin = await asyncpg.connect(dsn)
            try:
                await _seed_delegated_binding(
                    admin,
                    actor_id=actor,
                    subject_id="r2-subject",
                    device_id="r2-matrix",
                    binding_id="r2-matrix-binding",
                    declared_mode="parent_for_child" if minor else "self_use",
                    subject_category="minor" if minor else "adult",
                    age_band=age if minor else "adult",
                )
            finally:
                await admin.close()
            opened = await client.post(
                "/v1/sessions",
                headers={**headers, "Idempotency-Key": "matrix-open"},
                json={
                    "client": {
                        "platform": "web",
                        "timezone": "Asia/Shanghai",
                        "device_id": "r2-matrix",
                        "binding_version": 1,
                    }
                },
            )
            assert opened.status_code == 200, opened.text
            sid = opened.json()["session_id"]
            profile = opened.json()["runtime_profile"]
            if age != "unknown_safe":
                signed = await service.switch_subject(
                    SwitchPersistentSubjectCommand(
                        session_id=sid,
                        actor_id=actor,
                        subject_id="r2-subject" if minor else actor,
                        now=datetime.now(UTC),
                        requested_capabilities=("chat", "tutor"),
                    )
                )
                profile = signed.model_dump(mode="json")
            now = datetime.now(UTC)
            if state == "expired":
                # Retention grants have no expiry in the shipped product contract.
                with pytest.raises(ValueError, match="only corpus recording and subject custody"):
                    await consent.grant_for_person(
                        subject_person_id="r2-subject",
                        grantor_person_id=actor,
                        consent_kind="memory_retention",
                        policy_version="minor-retention-v1",
                        evidence_event_id="r2-invalid-expiry",
                        now=now,
                        expires_at=now + timedelta(seconds=1),
                    )
                assert (
                    await guardian.active_consent(
                        minor_user_id="r2-subject", consent_kind="memory_retention"
                    )
                    is None
                )
            else:
                grant = await consent.grant_for_person(
                    subject_person_id="r2-subject",
                    grantor_person_id=actor,
                    consent_kind="memory_retention",
                    policy_version="minor-retention-v1",
                    evidence_event_id="r2-grant",
                    now=now,
                )
                assert (
                    await guardian.active_consent(
                        minor_user_id="r2-subject", consent_kind="memory_retention"
                    )
                    is not None
                )
            if state == "revoked":
                await consent.revoke_for_person(
                    consent_id=grant.consent_id,
                    grantor_person_id=actor,
                    subject_person_id="r2-subject",
                    evidence_event_id="r2-revoke",
                )
            policy = await _session_policy(client, session_id=sid)
            assert policy.status_code == 200, policy.text
            body = policy.json()
            assert verify_runtime_profile_payload(body["runtime_profile"], signing_key=_SIGNING_KEY)
            allowed = age == "adult" or (minor and state == "valid")
            assert (body.get("memory_retention") != "ephemeral_only") is allowed
            assert "memory_recall_private" not in body["runtime_profile"]["capabilities"]
            decision = await client.post(
                "/v1/policy/decisions",
                headers=headers,
                json={
                    "runtime_profile_id": profile["runtime_profile_id"],
                    "capability": "memory_recall_private",
                    "data_classification": "private",
                },
            )
            assert decision.status_code == 200, decision.text
            assert decision.json()["effect"] == "deny"
            plan = await _response_plan(
                client,
                session_id=sid,
                query="我这道题又做错了，能陪我继续练习吗？",
                turn_id=1,
                generation_id=1,
            )
            assert plan.status_code == 200, plan.text
            assert plan.json()["grounded_items"] == []
            rotations = await asyncio.gather(
                *(
                    service.switch_subject(
                        SwitchPersistentSubjectCommand(
                            session_id=sid,
                            actor_id=actor,
                            subject_id=actor,
                            now=datetime.now(UTC),
                            requested_capabilities=("chat",),
                        )
                    )
                    for _ in range(2)
                ),
                return_exceptions=True,
            )
            from services.session_runtime.postgres_store import SessionRuntimeConflict

            winners = [item for item in rotations if not isinstance(item, BaseException)]
            assert winners
            assert all(
                not isinstance(item, BaseException) or isinstance(item, SessionRuntimeConflict)
                for item in rotations
            )
            current, context = await service.current(
                actor_id=actor, session_id=sid, now=datetime.now(UTC)
            )
            assert current.active_subject_id == actor
            assert current.session_epoch == max(item.session_epoch for item in winners)
            stale = await client.post(
                "/v1/policy/decisions",
                headers=headers,
                json={
                    "runtime_profile_id": profile["runtime_profile_id"],
                    "capability": "chat",
                    "data_classification": "ephemeral",
                },
            )
            assert stale.status_code == 403, stale.text
            admin = await asyncpg.connect(dsn)
            try:
                mutations = {
                    "under_14": "device_id='r2-wrong-device'",
                    "14_17": "binding_version=binding_version+1",
                    "adult": "active_subject_id='r2-wrong-person'",
                    "unknown_safe": "session_epoch=session_epoch+1",
                }
                await admin.execute(
                    f"UPDATE session_runtime_contexts SET {mutations[age]} WHERE session_id=$1",
                    sid,
                )
            finally:
                await admin.close()
            mismatched = await _session_policy(client, session_id=sid)
            assert mismatched.status_code == 503, mismatched.text
            mismatched_decision = await client.post(
                "/v1/policy/decisions",
                headers=headers,
                json={
                    "runtime_profile_id": current.runtime_profile_id,
                    "capability": "chat",
                    "data_classification": "ephemeral",
                },
            )
            assert mismatched_decision.status_code == 503, mismatched_decision.text
    finally:
        await guardian.close()
