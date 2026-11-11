"""Device-bound tutoring consent is independent of private retention consent."""

import os
from datetime import UTC, datetime

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
)
from services.control_api.tests.policy_r2_support import guardian_store
from services.guardian.consent import GuardianConsentService
from services.session_runtime.commands import StartPersistentSessionCommand
from services.session_runtime.service import build_postgres_session_runtime_service
from services.session_runtime.tests.postgres_store_test_support import (
    _SIGNING_KEY,
    _seed_delegated_binding,
    _seed_self_consent,
    _seed_verified_device,
    postgres_runtime,  # noqa: F401
    postgres_runtime_with_consent,  # noqa: F401
)

pytestmark = pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"), reason="requires isolated local PG"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("age", ["under_14", "14_17"])
@pytest.mark.parametrize("retention", [True, False])
async def test_authorized_device_tutoring_survives_retention_refusal(
    postgres_runtime_with_consent,  # noqa: F811
    monkeypatch,
    tmp_path,
    age,
    retention,
):
    store, dsn = postgres_runtime_with_consent
    _configure(monkeypatch, tmp_path)
    app = create_app()
    service = build_postgres_session_runtime_service(store=store, signing_key=_SIGNING_KEY)
    app.state.session_runtime_service = service
    app.state.multi_subject_runtime = PostgresMultiSubjectRuntimeControl(
        identity=app.state.identity_service, sessions=service
    )
    guardian = await guardian_store(dsn)
    app.state.guardian_store = guardian
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            actor, headers = await _anonymous_account(client)
            admin = await asyncpg.connect(dsn)
            try:
                now = datetime.now(UTC)
                await _seed_delegated_binding(
                    admin,
                    actor_id=actor,
                    subject_id="tutor-child",
                    device_id="tutor-device",
                    binding_id="tutor-binding",
                    declared_mode="parent_for_child",
                    subject_category="minor",
                    age_band=age,
                )
                await _seed_verified_device(
                    admin, device_id="tutor-device", binding_id="tutor-binding", now=now
                )
                await admin.execute(
                    """INSERT INTO identity_relationships (
                    relationship_id, source_person_id, target_person_id, relation_type,
                    status, valid_from, established_evidence_id, confirmed_by_source_at,
                    confirmed_by_target_at, requires_confirmation, can_delegate, created_at, updated_at
                ) VALUES ('tutor-guardian', $1, 'tutor-child', 'guardian_of', 'active', $2,
                    'r2-confirmed-guardian', $2, $2, true, false, $2, $2)""",
                    actor,
                    now,
                )
                await _seed_self_consent(
                    admin,
                    actor_id="tutor-child",
                    device_id="tutor-device",
                    binding_id="tutor-binding",
                    capability="tutor",
                    purpose="user_request",
                    now=now,
                )
            finally:
                await admin.close()
            if retention:
                await GuardianConsentService(guardian, app.state.life_archive).grant_for_person(
                    subject_person_id="tutor-child",
                    grantor_person_id=actor,
                    consent_kind="memory_retention",
                    policy_version="minor-retention-v1",
                    evidence_event_id="tutor-retention",
                )
            opened = await client.post(
                "/v1/sessions",
                headers={**headers, "Idempotency-Key": "tutor-start"},
                json={
                    "client": {
                        "platform": "web",
                        "timezone": "Asia/Shanghai",
                        "device_id": "tutor-device",
                        "binding_version": 1,
                    }
                },
            )
            assert opened.status_code == 200, opened.text
            sid = opened.json()["session_id"]
            signed = await service.start(
                StartPersistentSessionCommand(
                    session_id="device-tutor-real",
                    actor_id=actor,
                    device_id="tutor-device",
                    expected_binding_version=1,
                    idempotency_key="device-tutor-start",
                    now=datetime.now(UTC),
                    requested_capabilities=("tutor",),
                    device_bound_subject_id="tutor-child",
                )
            )
            admin = await asyncpg.connect(dsn)
            try:
                reasons = await admin.fetch(
                    "SELECT capability, effect, reason_code FROM policy_receipts_v2 WHERE runtime_profile_id=$1",
                    signed.runtime_profile_id,
                )
            finally:
                await admin.close()
            assert "tutor" in signed.capabilities, reasons
            assert "memory_recall_private" not in signed.capabilities
            policy = await _session_policy(client, session_id=sid)
            assert policy.status_code == 200, policy.text
            assert policy.json()["runtime_profile"]["service_mode"] == "unknown_safe"
            assert policy.json()["memory_retention"] == "ephemeral_only"
            decision = await client.post(
                "/v1/policy/decisions",
                headers=headers,
                json={
                    "runtime_profile_id": signed.runtime_profile_id,
                    "capability": "tutor",
                    "data_classification": "ephemeral",
                },
            )
            assert decision.status_code == 200, decision.text
            assert decision.json()["effect"] == "allow_with_obligations"
            private = await client.post(
                "/v1/policy/decisions",
                headers=headers,
                json={
                    "runtime_profile_id": signed.runtime_profile_id,
                    "capability": "memory_recall_private",
                    "data_classification": "private",
                },
            )
            assert private.status_code == 200, private.text
            assert private.json()["effect"] == "deny"
            assert private.json()["reason_code"] == "capability_not_in_runtime_profile"
            plan = await _response_plan(
                client,
                session_id=sid,
                query="今天的分数题还是不会，可以先讲第一步吗？",
                turn_id=1,
                generation_id=1,
            )
            assert plan.status_code == 200, plan.text
            assert plan.json()["grounded_items"] == []
    finally:
        await guardian.close()
