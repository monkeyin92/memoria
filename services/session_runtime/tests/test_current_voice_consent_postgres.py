from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from services.consent.bound_subject import (
    MEMORY_CAPABILITIES,
    MINOR_SESSION_CAPABILITIES,
    BoundSubjectConsentService,
    BoundSubjectGrant,
)
from services.consent.postgres_store import PostgresConsentStore
from services.identity.tests.test_postgres_revoke_classify import setup
from services.session_runtime.commands import StartPersistentSessionCommand
from services.session_runtime.errors import PersistentSessionDenied
from services.session_runtime.postgres_store import PostgresSessionRuntimeStore
from services.session_runtime.service import build_postgres_session_runtime_service
from services.session_runtime.tests.postgres_store_test_support import _seed_verified_device


@pytest.mark.asyncio
async def test_current_rejects_revoked_chat_consent_on_real_authorities(
    postgres_database, tmp_path
):
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
    )
    await runtime_store.initialize()
    runtime = build_postgres_session_runtime_service(
        store=runtime_store, signing_key=b"fixture-profile-key-long-enough-for-hmac"
    )
    now = datetime.now(UTC)
    manifest = await identity.get_active_manifest("device-1", actor_person_id="parent")
    admin = await asyncpg.connect(database.owner_dsn())
    try:
        await admin.execute(
            "INSERT INTO device_fleet_devices(device_id,family_space_id,binding_id,binding_version,lifecycle_status,capability_manifest_hash,capabilities,firmware_version,firmware_security_version,firmware_sha256,bootloader_version,anti_rollback_floor_version,anti_rollback_floor_security_version,created_at,updated_at) VALUES('device-1',$1,$2,1,'bound',$3,'[]','test',1,$3,'boot','test',1,$4,$4)",
            f"family-{manifest.binding_id}",
            manifest.binding_id,
            "a" * 64,
            now,
        )
        await _seed_verified_device(
            admin, device_id="device-1", binding_id=manifest.binding_id, now=now
        )
        await consent.grant(
            BoundSubjectGrant(
                actor_person_id="parent",
                subject_person_id=child,
                binding_id=manifest.binding_id,
                kind="guardian",
                capabilities=MINOR_SESSION_CAPABILITIES,
                source_key="fixture-voice",
            )
        )
        profile = await runtime.start(
            StartPersistentSessionCommand(
                session_id="voice-session",
                actor_id="parent",
                device_id="device-1",
                expected_binding_version=1,
                idempotency_key="start-voice-session",
                now=now,
                requested_capabilities=MINOR_SESSION_CAPABILITIES,
                device_bound_subject_id=child,
            )
        )
        assert "chat" in profile.capabilities
        assert (await runtime.current(actor_id="parent", session_id="voice-session", now=now))[
            0
        ] == profile
        from services.control_api.app.multi_subject_runtime import (
            PostgresMultiSubjectRuntimeControl,
        )

        control_runtime = PostgresMultiSubjectRuntimeControl(identity=identity, sessions=runtime)
        await consent.grant(
            BoundSubjectGrant(
                actor_person_id="parent",
                subject_person_id=child,
                binding_id=manifest.binding_id,
                kind="guardian",
                capabilities=MEMORY_CAPABILITIES,
                source_key="fixture-memory",
            )
        )
        control_id = control_runtime.control_session_id(
            "device-1", binding_id=manifest.binding_id, actor_id="parent"
        )
        memory_profile = await runtime.start(
            StartPersistentSessionCommand(
                session_id=control_id,
                actor_id="parent",
                device_id="device-1",
                expected_binding_version=1,
                idempotency_key="start-memory-session",
                now=datetime.now(UTC),
                requested_capabilities=(*MINOR_SESSION_CAPABILITIES, *MEMORY_CAPABILITIES),
                device_bound_subject_id=child,
            )
        )
        assert "memory_recall_private" in memory_profile.capabilities
        await consent.revoke(
            actor_person_id="parent",
            subject_person_id=child,
            binding_id=manifest.binding_id,
            capabilities=MEMORY_CAPABILITIES,
            reason="guardian_memory_retention_revoked",
        )
        downgraded = await control_runtime.ensure_profile(
            device_id="device-1", session_id=None, actor_id="parent", now=datetime.now(UTC)
        )
        assert "chat" in downgraded.capabilities
        assert "memory_recall_private" not in downgraded.capabilities
        assert (
            await runtime.session_state(actor_id="parent", session_id=downgraded.session_id)
            == "active"
        )
        from services.session_runtime.errors import PersistentSessionSubjectChanged

        with pytest.raises(PersistentSessionSubjectChanged):
            await runtime.close_session(
                actor_id="parent",
                session_id="voice-session",
                reason_code="wrong_subject",
                expected_subject_id="another-child",
                now=datetime.now(UTC),
            )
        assert (
            await runtime.session_state(actor_id="parent", session_id="voice-session") == "active"
        )
        unknown = await runtime.start(
            StartPersistentSessionCommand(
                session_id="unknown-session",
                actor_id="parent",
                device_id="device-1",
                expected_binding_version=1,
                idempotency_key="start-unknown-session",
                now=datetime.now(UTC),
                requested_capabilities=("chat",),
            )
        )
        assert unknown.active_subject_id is None
        with pytest.raises(PersistentSessionSubjectChanged):
            await runtime.close_session(
                actor_id="parent",
                session_id="unknown-session",
                reason_code="unconfirmed_subject",
                expected_subject_id=child,
                now=datetime.now(UTC),
            )
        assert (
            await runtime.session_state(actor_id="parent", session_id="unknown-session") == "active"
        )

        from services.control_api.app.guardian_revocation import guardian_revocation_hook
        from services.guardian.consent import ConsentRevocation

        class EdgeUnavailable:
            fail = True
            calls = 0

            async def terminate_subject(self, subject, **kwargs):
                assert subject == child
                self.calls += 1
                if self.fail:
                    raise RuntimeError("edge close unavailable")

        terminator = EdgeUnavailable()
        hook = guardian_revocation_hook(terminator, identity=identity, bound_consent=consent)
        event = ConsentRevocation(child, "parent", "minor_voice_session", "bound_subject")
        with pytest.raises(RuntimeError, match="edge close unavailable"):
            await hook(event)
        terminator.fail = False
        await hook(event)
        assert terminator.calls == 2
        with pytest.raises(PersistentSessionDenied) as denied:
            await runtime.current(
                actor_id="parent", session_id="voice-session", now=datetime.now(UTC)
            )
        assert "chat" in denied.value.withdrawn
        from services.control_api.app.database import MemoryStore
        from services.control_api.app.routes.interaction import router

        control = MemoryStore(dsn=database.role_dsn("memoria_control"), initialize_schema=False)
        control.register_account(
            user_id="parent",
            username="parent",
            username_normalized="parent",
            password_hash="fixture",
            now=now.isoformat(),
        )
        control.add_voice_session(
            session_id="voice-session",
            user_id="parent",
            resource_owner_account_id="parent",
            room_name="room",
            voice_backend="cascade",
            created_at=now.isoformat(),
            interaction_mode="companion",
            mode_policy_version="1",
            digital_self_version_id=None,
        )
        app = FastAPI()
        app.include_router(router)
        app.state.memory_store = control
        app.state.session_runtime_service = runtime
        app.state.settings = SimpleNamespace(internal_token=lambda kind: "fixture-policy-token")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://control"
        ) as client:
            result = await client.post(
                "/v1/interaction/session-policy",
                headers={"X-Memoria-Internal-Token": "fixture-policy-token"},
                json={"session_id": "voice-session"},
            )
            assert result.status_code == 410, result.text
            assert result.json()["detail"] == {
                "code": "session_terminated",
                "reason": "consent_withdrawn",
            }
            result = await client.post(
                "/v1/interaction/session-policy",
                headers={"X-Memoria-Internal-Token": "fixture-policy-token"},
                json={"session_id": "voice-session"},
            )
            assert result.status_code == 410, result.text
            assert result.json()["detail"]["reason"] == "closed"
        replay = await runtime.close_session(
            actor_id="parent",
            session_id="voice-session",
            reason_code="consent_withdrawn",
            expected_subject_id=child,
            now=datetime.now(UTC),
        )
        assert replay.already_closed
        with pytest.raises(PersistentSessionSubjectChanged):
            await runtime.close_session(
                actor_id="parent",
                session_id="voice-session",
                reason_code="wrong_subject",
                expected_subject_id="another-child",
                now=datetime.now(UTC),
            )
        control.close()
    finally:
        await admin.close()
        await consent_store.close()
        await identity_store.close()
