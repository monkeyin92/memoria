"""Session runtime Postgres: session lifecycle, subject routes, close semantics and device trust."""
# ruff: noqa: F811 - imported pytest fixtures are shadowed by test injection

import asyncio
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    RuntimeProfileSignedV2,
    RuntimeProfileV2,
    SessionEvent,
)
from services.session_runtime.postgres_store import (
    SESSION_RUNTIME_SCHEMA_SQL,
    PostgresSessionRuntimeStore,
    SessionRuntimeConflict,
    SessionRuntimeContext,
)
from services.session_runtime.profile_service import sign_runtime_profile_payload
from services.session_runtime.tests.postgres_store_test_support import (
    _DEVICE_ONBOARDING_SCHEMA_SQL,
    _SIGNING_KEY,
    _profile_event,
    _seed_binding,
    _seed_delegated_binding,
    _seed_verified_device,
    _signed_unknown_profile,
    postgres_runtime,  # noqa: F401 - pytest discovers imported fixtures by name
)


def _next_confirmed_profile(
    previous: RuntimeProfileSignedV2,
    *,
    profile_id: str,
    issued_at: datetime,
) -> RuntimeProfileSignedV2:
    payload = previous.model_dump(mode="json", exclude={"signature"})
    payload.update(
        {
            "runtime_profile_id": profile_id,
            "active_subject_id": previous.actor_id,
            "subject_revision": 1,
            "subject_category": "adult",
            "age_band": "adult",
            "speaker_state": "confirmed",
            "speaker_confidence": 1.0,
            "service_mode": "adult_companion",
            "session_epoch": previous.session_epoch + 1,
            "issued_at": issued_at.isoformat(),
            "expires_at": (issued_at + timedelta(minutes=5)).isoformat(),
        }
    )
    canonical = RuntimeProfileV2.model_validate(payload).model_dump(mode="json")
    return RuntimeProfileSignedV2.model_validate(
        {
            **canonical,
            "signature": sign_runtime_profile_payload(
                canonical,
                signing_key=_SIGNING_KEY,
            ),
        }
    )


@pytest.mark.asyncio
async def test_postgres_concurrent_double_switch_has_one_cas_winner(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    initial = _signed_unknown_profile(
        session_id="session-cas",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-cas-1",
        issued_at=now,
    )
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=SessionRuntimeContext.from_profile(
                initial,
                profile_revision=1,
            ),
            profile=initial,
            event=_profile_event(initial),
            request_hash=hashlib.sha256(b"session-cas-request").hexdigest(),
            idempotency_key="start-session-cas",
        )

    candidates = tuple(
        _next_confirmed_profile(
            initial,
            profile_id=f"rp-session-cas-{suffix}",
            issued_at=now + timedelta(seconds=1),
        )
        for suffix in ("switch-a", "switch-b")
    )

    async def attempt(profile: RuntimeProfileSignedV2) -> RuntimeProfileSignedV2:
        async with store.action_transaction(
            actor_id="actor-a",
            device_id="device-a",
            subject_id=profile.active_subject_id,
        ) as connection:
            return await store.rotate_profile(
                connection,
                expected_runtime_profile_id=initial.runtime_profile_id,
                expected_profile_revision=1,
                expected_session_epoch=1,
                profile=profile,
                event=_profile_event(
                    profile,
                    event_type="subject_switched",
                    event_sequence=2,
                    generation_id=1,
                    turn_id=1,
                    tool_epoch=1,
                ),
            )

    results = await asyncio.gather(
        *(attempt(profile) for profile in candidates),
        return_exceptions=True,
    )
    winners = [item for item in results if isinstance(item, RuntimeProfileSignedV2)]
    conflicts = [item for item in results if isinstance(item, SessionRuntimeConflict)]

    async with store.read_transaction(actor_id="actor-a") as connection:
        current = await store.current_profile(connection, session_id="session-cas")

    assert len(winners) == 1, repr(results)
    assert len(conflicts) == 1, repr(results)
    assert current == winners[0]
    assert current is not None
    assert current.session_epoch == 2


@pytest.mark.asyncio
async def test_real_create_session_route_persists_signed_v2_receipts_and_outbox(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from httpx import ASGITransport, AsyncClient
    from services.control_api.app.main import create_app
    from services.session_runtime.service import (
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "control.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_AUTH_SECRET",
        "test-auth-material-that-is-long-enough",
    )
    monkeypatch.setenv(
        "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET",
        _SIGNING_KEY.decode(),
    )
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "session-pg-route-test")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    app = create_app()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        identity = await client.post("/v1/auth/anonymous")
        assert identity.status_code == 200
        actor_id = str(identity.json()["user_id"])
        admin = await asyncpg.connect(bootstrap_dsn)
        try:
            await _seed_binding(
                admin,
                actor_id=actor_id,
                device_id="device-route",
                binding_id="binding-route",
            )
        finally:
            await admin.close()
        response = await client.post(
            "/v1/sessions",
            headers={
                "Authorization": f"Bearer {identity.json()['access_token']}",
                "Idempotency-Key": "route-session-start-1",
            },
            json={
                "client": {
                    "platform": "web",
                    "timezone": "Asia/Shanghai",
                    "device_id": "device-route",
                    "binding_version": 1,
                }
            },
        )

    assert response.status_code == 200, response.text
    wire = response.json()["runtime_profile"]
    signed = RuntimeProfileSignedV2.model_validate(wire)
    assert signed.signature_schema == "runtime-profile-v2"
    assert signed.actor_id == actor_id
    assert signed.active_subject_id is None
    assert signed.service_mode.value == "unknown_safe"
    assert signed.session_epoch == 1
    assert signed.policy_receipt_ids

    async with store.read_transaction(actor_id=actor_id) as connection:
        current = await store.current_profile(
            connection,
            session_id=signed.session_id,
        )
    assert current == signed

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        receipt_count = await admin.fetchval(
            "SELECT count(*) FROM policy_receipts_v2 WHERE session_id = $1",
            signed.session_id,
        )
        outbox_count = await admin.fetchval(
            "SELECT count(*) FROM session_runtime_outbox WHERE session_id = $1",
            signed.session_id,
        )
    finally:
        await admin.close()
    assert receipt_count >= len(signed.policy_receipt_ids)
    assert outbox_count == 1


@pytest.mark.asyncio
async def test_postgres_start_atomically_degrades_incomplete_subject_facts(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.session_runtime.commands import StartPersistentSessionCommand
    from services.session_runtime.service import build_postgres_session_runtime_service
    from services.session_runtime.subject_resolver import SubjectCandidate

    store, bootstrap_dsn = postgres_runtime
    actor_id = "actor-start-facts-unverified"
    device_id = "device-start-facts-unverified"
    binding_id = "binding-start-facts-unverified"
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_binding(
            admin,
            actor_id=actor_id,
            device_id=device_id,
            binding_id=binding_id,
        )
        await admin.execute(
            """
            UPDATE identity_persons
            SET subject_category = 'unknown', age_band = 'unknown',
                age_evidence_status = 'unverified', updated_at = $2
            WHERE person_id = $1
            """,
            actor_id,
            datetime.now(UTC),
        )
    finally:
        await admin.close()

    observed_action_subjects: list[str | None] = []
    set_action_subject = store.set_action_subject

    async def capture_action_subject(
        connection: asyncpg.Connection,
        subject_id: str | None,
    ) -> None:
        observed_action_subjects.append(subject_id)
        await set_action_subject(connection, subject_id)

    monkeypatch.setattr(store, "set_action_subject", capture_action_subject)
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    now = datetime.now(UTC)
    profile = await service.start(
        StartPersistentSessionCommand(
            session_id="session-start-facts-unverified",
            actor_id=actor_id,
            device_id=device_id,
            expected_binding_version=1,
            idempotency_key="start-facts-unverified",
            now=now,
            requested_capabilities=("chat",),
            candidates=(SubjectCandidate(subject_id=actor_id, confidence=0.99),),
        )
    )

    assert observed_action_subjects == [None]
    assert profile.active_subject_id is None
    assert profile.subject_revision == 0
    assert profile.subject_category.value == "unknown"
    assert profile.age_band.value == "unknown"
    assert profile.speaker_state.value == "unconfirmed"
    assert profile.speaker_confidence is None
    assert profile.service_mode.value == "unknown_safe"

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        event_row = await admin.fetchrow(
            "SELECT event_type, payload_json FROM session_runtime_events "
            "WHERE session_id = $1 ORDER BY event_sequence DESC LIMIT 1",
            profile.session_id,
        )
    finally:
        await admin.close()
    assert event_row is not None
    assert event_row["event_type"] == "subject_resolved"
    event_payload = event_row["payload_json"]
    if isinstance(event_payload, str):
        event_payload = json.loads(event_payload)
    assert event_payload["payload"]["reason_code"] == "subject_facts_unverified"


@pytest.mark.asyncio
async def test_postgres_switch_atomically_degrades_incomplete_subject_facts(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.session_runtime.commands import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.service import build_postgres_session_runtime_service

    store, bootstrap_dsn = postgres_runtime
    actor_id = "actor-switch-facts-unverified"
    device_id = "device-switch-facts-unverified"
    binding_id = "binding-switch-facts-unverified"
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_binding(
            admin,
            actor_id=actor_id,
            device_id=device_id,
            binding_id=binding_id,
        )
        await admin.execute(
            """
            UPDATE identity_persons
            SET subject_category = 'unknown', age_band = 'unknown',
                age_evidence_status = 'unverified', updated_at = $2
            WHERE person_id = $1
            """,
            actor_id,
            datetime.now(UTC),
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    now = datetime.now(UTC)
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-switch-facts-unverified",
            actor_id=actor_id,
            device_id=device_id,
            expected_binding_version=1,
            idempotency_key="start-before-facts-switch",
            now=now,
            requested_capabilities=("chat",),
        )
    )

    observed_action_subjects: list[str | None] = []
    set_action_subject = store.set_action_subject

    async def capture_action_subject(
        connection: asyncpg.Connection,
        subject_id: str | None,
    ) -> None:
        observed_action_subjects.append(subject_id)
        await set_action_subject(connection, subject_id)

    monkeypatch.setattr(store, "set_action_subject", capture_action_subject)
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id=actor_id,
            subject_id=actor_id,
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat",),
        )
    )

    assert observed_action_subjects == [None]
    assert profile.active_subject_id is None
    assert profile.subject_revision == 0
    assert profile.subject_category.value == "unknown"
    assert profile.age_band.value == "unknown"
    assert profile.speaker_state.value == "unconfirmed"
    assert profile.speaker_confidence is None
    assert profile.service_mode.value == "unknown_safe"
    assert profile.session_epoch == initial.session_epoch + 1

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        event_row = await admin.fetchrow(
            "SELECT event_type, payload_json FROM session_runtime_events "
            "WHERE session_id = $1 ORDER BY event_sequence DESC LIMIT 1",
            profile.session_id,
        )
    finally:
        await admin.close()
    assert event_row is not None
    assert event_row["event_type"] == "epoch_bumped"
    event_payload = event_row["payload_json"]
    if isinstance(event_payload, str):
        event_payload = json.loads(event_payload)
    assert event_payload["payload"]["reason_code"] == "subject_facts_unverified"


@pytest.mark.asyncio
async def test_parent_for_child_primary_snapshot_never_defaults_to_account_owner(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id="guardian-owner",
            subject_id="child-primary",
            device_id="device-parent-child",
            binding_id="binding-parent-child",
            declared_mode="parent_for_child",
            subject_category="minor",
            age_band="under_14",
        )
    finally:
        await admin.close()
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )

    snapshot = await service.binding_snapshot(
        actor_id="guardian-owner",
        device_id="device-parent-child",
        expected_binding_version=1,
        now=datetime.now(UTC),
    )

    assert snapshot.primary_subject_ids == ("child-primary",)
    assert set(snapshot.member_subject_ids) == {"guardian-owner", "child-primary"}


@pytest.mark.asyncio
async def test_postgres_device_admin_switch_keeps_actor_separate_from_parent_subject(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.commands import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.service import (
        PersistentSessionDenied,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id="device-admin-child",
            subject_id="parent-subject",
            device_id="device-parent-care",
            binding_id="binding-parent-care",
            declared_mode="child_for_parent",
            subject_category="adult",
            age_band="adult",
        )
    finally:
        await admin.close()
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    now = datetime.now(UTC)
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-parent-care",
            actor_id="device-admin-child",
            device_id="device-parent-care",
            expected_binding_version=1,
            idempotency_key="start-parent-care",
            now=now,
            requested_capabilities=("chat", "english_practice"),
        )
    )
    switched = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="device-admin-child",
            subject_id="parent-subject",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat", "tutor", "english_practice"),
        )
    )

    assert switched.actor_id == "device-admin-child"
    assert switched.active_subject_id == "parent-subject"
    assert switched.session_epoch == initial.session_epoch + 1
    assert "chat" in {item.value for item in switched.capabilities}
    with pytest.raises(PersistentSessionDenied, match="stale"):
        await service.decide(
            runtime_profile_id=initial.runtime_profile_id,
            capability="chat",
            actor_id="device-admin-child",
            data_classification="ephemeral",
            safety_state="normal",
            now=now + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_postgres_minor_without_consent_gets_only_audited_denies(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.commands import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.service import build_postgres_session_runtime_service

    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id="guardian-no-consent",
            subject_id="minor-no-consent",
            device_id="device-minor-no-consent",
            binding_id="binding-minor-no-consent",
            declared_mode="parent_for_child",
            subject_category="minor",
            age_band="under_14",
        )
    finally:
        await admin.close()
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    now = datetime.now(UTC)
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-minor-no-consent",
            actor_id="guardian-no-consent",
            device_id="device-minor-no-consent",
            expected_binding_version=1,
            idempotency_key="start-minor-no-consent",
            now=now,
            requested_capabilities=("chat", "tutor", "english_practice"),
        )
    )
    switched = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="guardian-no-consent",
            subject_id="minor-no-consent",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat", "tutor", "english_practice"),
        )
    )

    assert switched.active_subject_id == "minor-no-consent"
    assert switched.capabilities == ()
    assert switched.policy_receipt_ids == ()
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        effects = await admin.fetch(
            """
            SELECT effect, reason_code FROM policy_receipts_v2
            WHERE runtime_profile_id = $1 ORDER BY capability
            """,
            switched.runtime_profile_id,
        )
    finally:
        await admin.close()
    assert len(effects) == 3
    assert {str(item["effect"]) for item in effects} == {"deny"}


@pytest.mark.asyncio
async def test_real_pg_control_routes_share_start_current_resolve_and_switch_authority(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from httpx import ASGITransport, AsyncClient
    from services.control_api.app.main import create_app
    from services.control_api.app.multi_subject_runtime import (
        PostgresMultiSubjectRuntimeControl,
    )
    from services.identity.domain import (
        BindingManifest,
        IdentityNotFoundError,
        ManifestRole,
        PersonSubject,
    )
    from services.session_runtime.service import (
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "control-pg.sqlite3"))
    monkeypatch.setenv(
        "MEMORIA_AUTH_SECRET",
        "test-auth-material-that-is-long-enough",
    )
    monkeypatch.setenv(
        "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET",
        _SIGNING_KEY.decode(),
    )
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "session-pg-control-route-test")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    app = create_app()
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    app.state.session_runtime_service = service

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        identity_response = await client.post("/v1/auth/anonymous")
        stranger_response = await client.post("/v1/auth/anonymous")
        actor_id = str(identity_response.json()["user_id"])
        parent_id = "parent-route-subject"
        now = datetime.now(UTC)
        manifest = BindingManifest(
            binding_id="binding-control-route",
            device_id="device-control-route",
            declared_mode="child_for_parent",
            binding_version=1,
            status="active",
            reason="create",
            supersedes_binding_id=None,
            family_space_id="family-control-route",
            account_owner_id=actor_id,
            device_admin_ids=(actor_id,),
            primary_subject_ids=(parent_id,),
            guardian_ids=(),
            delegate_ids=(),
            emergency_contact_ids=(),
            member_ids=(parent_id,),
            roles=(
                ManifestRole(actor_id, "account_owner", ("binding.manage",)),
                ManifestRole(actor_id, "device_admin", ("binding.manage",)),
                ManifestRole(parent_id, "primary_subject", ("content.read",)),
                ManifestRole(parent_id, "member", ("family.shared.read",)),
            ),
            service_profile_version="delegated-v1",
            policy_bundle_version="multi-subject-v2",
            consent_snapshot_id=None,
            persona_assignment_id="starlight:v1",
            valid_from=now,
            valid_until=None,
            created_at=now,
        )
        people = {
            actor_id: PersonSubject(
                actor_id,
                "设备管理员",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                created_at=now,
                updated_at=now,
            ),
            parent_id: PersonSubject(
                parent_id,
                "父母",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                created_at=now,
                updated_at=now,
            ),
        }

        class IdentityView:
            #: Rows the assignment authority reports for the binding; the test
            #: keeps this in step with ``identity_persona_assignments``.
            persona_overrides: tuple[object, ...] = ()

            # Production Identity runs with FORCE RLS: a read without an
            # identity actor sees no rows, so the view refuses it too.
            async def get_active_manifest(
                self,
                device_id: str,
                _now: datetime | None = None,
                actor_person_id: str | None = None,
            ) -> BindingManifest | None:
                if actor_person_id is None:
                    return None
                return manifest if device_id == manifest.device_id else None

            async def get_person(
                self, person_id: str, actor_person_id: str | None = None
            ) -> PersonSubject:
                if actor_person_id is None:
                    raise IdentityNotFoundError(f"person {person_id} is not visible")
                return people[person_id]

            async def list_persona_assignments(
                self,
                *,
                binding_id: str,
                actor_person_id: str | None = None,
            ) -> tuple[object, ...]:
                return tuple(
                    item
                    for item in self.persona_overrides
                    if item.binding_id == binding_id  # type: ignore[attr-defined]
                )

        view = IdentityView()
        app.state.multi_subject_runtime = PostgresMultiSubjectRuntimeControl(
            identity=view,  # type: ignore[arg-type]
            sessions=service,
        )
        admin = await asyncpg.connect(bootstrap_dsn)
        try:
            await _seed_delegated_binding(
                admin,
                actor_id=actor_id,
                subject_id=parent_id,
                device_id=manifest.device_id,
                binding_id=manifest.binding_id,
                declared_mode="child_for_parent",
                subject_category="adult",
                age_band="adult",
            )
            # The only subject on this binding carries a persona of their own;
            # the binding default stays starlight:v1.
            await admin.execute(
                """
                INSERT INTO identity_persona_assignments (
                    binding_id, subject_id, assignment_id, persona_id,
                    persona_version, created_at, updated_at
                ) VALUES ($1, $2, 'taoxi:v1', 'taoxi', 1, $3, $3)
                """,
                manifest.binding_id,
                parent_id,
                now,
            )
        finally:
            await admin.close()
        view.persona_overrides = (
            SimpleNamespace(
                binding_id=manifest.binding_id,
                subject_id=parent_id,
                assignment_id="taoxi:v1",
            ),
        )

        headers = {
            "Authorization": f"Bearer {identity_response.json()['access_token']}",
            "Idempotency-Key": "control-route-start",
        }
        started = await client.post(
            "/v1/sessions",
            headers=headers,
            json={
                "client": {
                    "platform": "web",
                    "timezone": "Asia/Shanghai",
                    "device_id": manifest.device_id,
                    "binding_version": 1,
                }
            },
        )
        assert started.status_code == 200, started.text
        initial = RuntimeProfileSignedV2.model_validate(
            started.json()["runtime_profile"]
        )
        current = await client.get(
            f"/v1/devices/{manifest.device_id}/runtime-profile",
            headers=headers,
            params={"session_id": initial.session_id},
        )
        assert current.status_code == 200, current.text
        assert current.json()["runtime_profile_id"] == initial.runtime_profile_id
        # No active subject yet: the binding default applies.
        assert initial.persona_assignment_id == "starlight:v1"

        resolution = await client.post(
            "/v1/sessions/resolve-subject",
            headers=headers,
            json={
                "device_id": manifest.device_id,
                "session_id": initial.session_id,
                "client_claimed_person_id": parent_id,
                "environment": {},
            },
        )
        assert resolution.status_code == 200, resolution.text
        assert resolution.json()["resolution"] == "confirmation_required"
        switched = await client.post(
            f"/v1/sessions/{initial.session_id}/active-subject",
            headers=headers,
            json={
                "person_id": parent_id,
                "confirmation_method": "app_confirm",
            },
        )
        assert switched.status_code == 200, switched.text
        switched_profile = RuntimeProfileSignedV2.model_validate(switched.json())
        assert switched_profile.actor_id == actor_id
        assert switched_profile.active_subject_id == parent_id
        assert switched_profile.session_epoch == initial.session_epoch + 1
        # The subject carries their own persona; switching to them switches it.
        assert switched_profile.persona_assignment_id == "taoxi:v1"
        assert switched_profile.persona.persona_id == "taoxi"

        stale = await client.post(
            "/v1/policy/decisions",
            headers=headers,
            json={
                "runtime_profile_id": initial.runtime_profile_id,
                "capability": "chat",
            },
        )
        assert stale.status_code == 403
        stranger = await client.get(
            f"/v1/devices/{manifest.device_id}/runtime-profile",
            headers={
                "Authorization": (
                    f"Bearer {stranger_response.json()['access_token']}"
                )
            },
            params={"session_id": initial.session_id},
        )
        assert stranger.status_code == 403

        # P1-03: a persona write reaches the next profile read, not the next
        # profile TTL.  Dropping the override re-resolves to the binding
        # default while the session keeps its confirmed subject and epoch.
        admin = await asyncpg.connect(bootstrap_dsn)
        try:
            await admin.execute(
                """
                DELETE FROM identity_persona_assignments
                WHERE binding_id = $1 AND subject_id = $2
                """,
                manifest.binding_id,
                parent_id,
            )
        finally:
            await admin.close()
        view.persona_overrides = ()
        reissued = await client.get(
            f"/v1/devices/{manifest.device_id}/runtime-profile",
            headers=headers,
            params={"session_id": initial.session_id},
        )
        assert reissued.status_code == 200, reissued.text
        reissued_profile = RuntimeProfileSignedV2.model_validate(reissued.json())
        assert reissued_profile.persona_assignment_id == "starlight:v1"
        assert reissued_profile.persona.persona_id == "starlight"
        assert reissued_profile.active_subject_id == parent_id
        assert reissued_profile.session_epoch == switched_profile.session_epoch + 1


def _close_event(
    *,
    session_id: str,
    profile: RuntimeProfileSignedV2,
    context: SessionRuntimeContext,
    reason_code: str,
    now: datetime,
) -> SessionEvent:
    return SessionEvent.model_validate(
        {
            "event_id": f"event-close-{uuid.uuid4().hex[:12]}",
            "event_type": "session_closed",
            "session_id": session_id,
            "session_epoch": context.session_epoch + 1,
            "device_id": profile.device_id,
            "binding_id": profile.binding_id,
            "binding_version": profile.binding_version,
            "generation_id": context.generation_id + 1,
            "turn_id": context.turn_id + 1,
            "tool_epoch": context.tool_epoch + 1,
            "event_sequence": context.profile_revision + 1,
            "active_subject_id": None,
            "runtime_profile_id": profile.runtime_profile_id,
            "actor_id": profile.actor_id,
            "subject_revision": 0,
            "occurred_at": now.isoformat(),
            "payload": {
                "reason_code": reason_code,
                "invalidated_fences": [
                    "session",
                    "generation",
                    "tool",
                    "effect",
                    "tts",
                    "memory",
                    "ui",
                ],
            },
        }
    )


@pytest.mark.asyncio
async def test_postgres_close_session_applies_terminal_fence_and_outbox(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-close-a",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-close-a-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"close-a-request").hexdigest(),
            idempotency_key="start-session-close-a",
        )

    event = _close_event(
        session_id="session-close-a",
        profile=profile,
        context=context,
        reason_code="network",
        now=now + timedelta(seconds=1),
    )
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        status = await store.close_session(
            connection,
            expected=context,
            event=event,
        )
    assert status == "closed"

    async with store.read_transaction(actor_id="actor-a") as connection:
        stored = await store.context_any_state(
            connection,
            session_id="session-close-a",
        )
    assert stored is not None
    closed_context, state = stored
    assert state == "closed"
    assert closed_context.active_subject_id is None
    assert closed_context.session_epoch == 2
    assert closed_context.generation_id == 1
    assert closed_context.turn_id == 1
    assert closed_context.tool_epoch == 1

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        event_row = await admin.fetchrow(
            "SELECT event_type, event_sequence, session_epoch, payload_json "
            "FROM session_runtime_events WHERE session_id = $1 "
            "ORDER BY event_sequence DESC LIMIT 1",
            "session-close-a",
        )
        outbox_row = await admin.fetchrow(
            "SELECT topic, status FROM session_runtime_outbox WHERE session_id = $1 "
            "AND topic = 'agent.session.closed'",
            "session-close-a",
        )
        event_count = await admin.fetchval(
            "SELECT count(*) FROM session_runtime_events WHERE session_id = $1",
            "session-close-a",
        )
        outbox_count = await admin.fetchval(
            "SELECT count(*) FROM session_runtime_outbox WHERE session_id = $1",
            "session-close-a",
        )
    finally:
        await admin.close()
    assert event_row is not None
    assert event_row["event_type"] == "session_closed"
    assert event_row["event_sequence"] == 2
    assert event_row["session_epoch"] == 2
    event_payload = event_row["payload_json"]
    if isinstance(event_payload, str):
        event_payload = json.loads(event_payload)
    assert event_payload["payload"]["reason_code"] == "network"
    assert outbox_row is not None
    assert outbox_row["topic"] == "agent.session.closed"
    assert outbox_row["status"] == "pending"
    assert event_count == 2  # subject_resolved + session_closed
    assert outbox_count == 2

    # Idempotent replay with the observed terminal epoch returns
    # already_closed and writes no second event or outbox row.
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        replayed = await store.close_session(
            connection,
            expected=closed_context,
            event=_close_event(
                session_id="session-close-a",
                profile=profile,
                context=closed_context,
                reason_code="network",
                now=now + timedelta(seconds=2),
            ),
        )
    assert replayed == "already_closed"
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        event_count = await admin.fetchval(
            "SELECT count(*) FROM session_runtime_events WHERE session_id = $1",
            "session-close-a",
        )
        outbox_count = await admin.fetchval(
            "SELECT count(*) FROM session_runtime_outbox WHERE session_id = $1",
            "session-close-a",
        )
    finally:
        await admin.close()
    assert event_count == 2
    assert outbox_count == 2


@pytest.mark.asyncio
async def test_postgres_close_session_conflicts_after_failure_and_missing(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-close-b",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-close-b-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"close-b-request").hexdigest(),
            idempotency_key="start-session-close-b",
        )
        failed_payload = _profile_event(profile).model_dump(mode="json")
        failed_payload.update(
            {
                "event_id": f"event-fail-{uuid.uuid4().hex[:12]}",
                "event_type": "session_failed",
                "session_epoch": context.session_epoch + 1,
                "generation_id": context.generation_id + 1,
                "turn_id": context.turn_id + 1,
                "tool_epoch": context.tool_epoch + 1,
                "event_sequence": context.profile_revision + 1,
            }
        )
        await store.fail_session(
            connection,
            expected=context,
            event=SessionEvent.model_validate(failed_payload),
        )
    # A failed authority must never relabel to closed. Each rejected function
    # call uses its own transaction because PostgreSQL intentionally aborts a
    # transaction after a SECURITY DEFINER function raises.
    with pytest.raises(SessionRuntimeConflict):
        async with store.action_transaction(
            actor_id="actor-a",
            device_id="device-a",
        ) as connection:
            await store.close_session(
                connection,
                expected=context,
                event=_close_event(
                    session_id="session-close-b",
                    profile=profile,
                    context=context,
                    reason_code="network",
                    now=now + timedelta(seconds=1),
                ),
            )
    # A stale expected profile/epoch (post-fail row) also conflicts.
    stale = replace(context, session_epoch=2)
    with pytest.raises(SessionRuntimeConflict):
        async with store.action_transaction(
            actor_id="actor-a",
            device_id="device-a",
        ) as connection:
            await store.close_session(
                connection,
                expected=stale,
                event=_close_event(
                    session_id="session-close-b",
                    profile=profile,
                    context=stale,
                    reason_code="network",
                    now=now + timedelta(seconds=1),
                ),
            )
    missing = replace(context, session_id="session-close-missing")
    with pytest.raises(SessionRuntimeConflict):
        async with store.action_transaction(
            actor_id="actor-a",
            device_id="device-a",
        ) as connection:
            await store.close_session(
                connection,
                expected=missing,
                event=_close_event(
                    session_id="session-close-missing",
                    profile=profile,
                    context=missing,
                    reason_code="network",
                    now=now + timedelta(seconds=1),
                ),
            )


@pytest.mark.asyncio
async def test_service_close_session_replays_idempotently_and_rejects_unknown(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.commands import SessionCloseResult
    from services.session_runtime.service import (
        PersistentSessionNotFound,
        build_postgres_session_runtime_service,
    )

    store, _bootstrap_dsn = postgres_runtime
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-close-c",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-close-c-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"close-c-request").hexdigest(),
            idempotency_key="start-session-close-c",
        )

    first = await service.close_session(
        actor_id="actor-a",
        session_id="session-close-c",
        reason_code="superseded",
        now=now + timedelta(seconds=1),
    )
    assert first == SessionCloseResult(applied=True, already_closed=False)

    replayed = await service.close_session(
        actor_id="actor-a",
        session_id="session-close-c",
        reason_code="superseded",
        now=now + timedelta(seconds=2),
    )
    assert replayed == SessionCloseResult(applied=False, already_closed=True)

    with pytest.raises(PersistentSessionNotFound):
        await service.close_session(
            actor_id="actor-a",
            session_id="session-close-unknown",
            reason_code="network",
            now=now,
        )


@pytest.mark.asyncio
async def test_service_start_profile_ttl_is_per_session_and_bounded(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.commands import StartPersistentSessionCommand
    from services.session_runtime.service import build_postgres_session_runtime_service

    store, _bootstrap_dsn = postgres_runtime
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-ttl-a",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-ttl-a-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"ttl-a-request").hexdigest(),
            idempotency_key="start-session-ttl-a",
        )

    direct = await service.close_session(
        actor_id="actor-a",
        session_id="session-ttl-a",
        reason_code="device_close",
        now=now + timedelta(seconds=1),
    )
    assert direct.applied is True

    # Command-level bounds: the direct path may set a positive bounded TTL,
    # everything else keeps the 5-minute service default.
    with pytest.raises(ValueError, match="positive"):
        StartPersistentSessionCommand(
            session_id="s1",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="k1",
            now=now,
            profile_ttl=timedelta(0),
        )
    with pytest.raises(ValueError, match="one-day bound"):
        StartPersistentSessionCommand(
            session_id="s1",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="k1",
            now=now,
            profile_ttl=timedelta(days=2),
        )
    command = StartPersistentSessionCommand(
        session_id="s1",
        actor_id="actor-a",
        device_id="device-a",
        expected_binding_version=1,
        idempotency_key="k1",
        now=now,
        profile_ttl=timedelta(seconds=3600),
    )
    assert command.profile_ttl == timedelta(seconds=3600)
    default = StartPersistentSessionCommand(
        session_id="s2",
        actor_id="actor-a",
        device_id="device-a",
        expected_binding_version=1,
        idempotency_key="k2",
        now=now,
    )
    assert default.profile_ttl is None


@pytest.mark.asyncio
async def test_service_close_session_allows_expired_signed_profile(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    """Expiry denies use but must not strand an active Session authority."""

    from services.session_runtime.service import build_postgres_session_runtime_service

    store, _bootstrap_dsn = postgres_runtime
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    issued_at = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-expired-close-a",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-expired-close-a-1",
        issued_at=issued_at,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"expired-close-request").hexdigest(),
            idempotency_key="start-session-expired-close-a",
        )

    result = await service.close_session(
        actor_id="actor-a",
        session_id="session-expired-close-a",
        reason_code="network",
        now=issued_at + timedelta(minutes=10),
    )
    assert result.applied is True


@pytest.mark.asyncio
async def test_postgres_close_session_survives_binding_revocation(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    """Revocation must not strand the already-issued terminal authority."""

    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    profile = _signed_unknown_profile(
        session_id="session-revoked-close-a",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-session-revoked-close-a-1",
        issued_at=now,
    )
    context = SessionRuntimeContext.from_profile(profile, profile_revision=1)
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=context,
            profile=profile,
            event=_profile_event(profile),
            request_hash=hashlib.sha256(b"revoked-close-request").hexdigest(),
            idempotency_key="start-session-revoked-close-a",
        )

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "UPDATE identity_device_bindings SET status = 'revoked', "
            "valid_until = $1 WHERE binding_id = 'binding-a'",
            now + timedelta(milliseconds=1),
        )
    finally:
        await admin.close()

    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        status = await store.close_session(
            connection,
            expected=context,
            event=_close_event(
                session_id="session-revoked-close-a",
                profile=profile,
                context=context,
                reason_code="device_close",
                now=now + timedelta(seconds=1),
            ),
        )
    assert status == "closed"


@pytest.mark.asyncio
async def test_read_transaction_holds_single_snapshot_across_concurrent_subject_rotation(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    """P0-04 read consistency: profile/context reads share one snapshot.

    Contract: every statement inside ``read_transaction`` observes the same
    committed snapshot, so a subject rotation that commits between the profile
    read and the context read must not produce a mixed pair (old profile id +
    new context pointer). A fresh transaction afterwards observes the rotation.
    Fails on read-committed reads; passes on REPEATABLE READ.
    """

    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    initial = _signed_unknown_profile(
        session_id="session-read-snapshot",
        actor_id="actor-a",
        device_id="device-a",
        binding_id="binding-a",
        profile_id="rp-read-snapshot-1",
        issued_at=now,
    )
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await store.persist_initial(
            connection,
            context=SessionRuntimeContext.from_profile(initial, profile_revision=1),
            profile=initial,
            event=_profile_event(initial),
            request_hash=hashlib.sha256(b"read-snapshot-request").hexdigest(),
            idempotency_key="start-session-read-snapshot",
        )
    switched = _next_confirmed_profile(
        initial,
        profile_id="rp-read-snapshot-2",
        issued_at=now + timedelta(seconds=1),
    )
    async with store.read_transaction(actor_id="actor-a") as reader:
        before = await store.current_profile(reader, session_id="session-read-snapshot")
        assert before is not None
        assert before.runtime_profile_id == initial.runtime_profile_id
        async with store.action_transaction(
            actor_id="actor-a",
            device_id="device-a",
            subject_id=switched.active_subject_id,
        ) as writer:
            await store.rotate_profile(
                writer,
                expected_runtime_profile_id=initial.runtime_profile_id,
                expected_profile_revision=1,
                expected_session_epoch=1,
                profile=switched,
                event=_profile_event(
                    switched,
                    event_type="subject_switched",
                    event_sequence=2,
                    generation_id=1,
                    turn_id=1,
                    tool_epoch=1,
                ),
            )
        during = await store.current_context(reader, session_id="session-read-snapshot")
        assert during is not None
        assert (during.current_runtime_profile_id, during.session_epoch) == (
            before.runtime_profile_id,
            before.session_epoch,
        )
    async with store.read_transaction(actor_id="actor-a") as fresh:
        after = await store.current_profile(fresh, session_id="session-read-snapshot")
    assert after is not None
    assert after.runtime_profile_id == switched.runtime_profile_id


async def _rebind_through_onboarding(
    bootstrap_dsn: str,
    *,
    lifecycle: str = "bound",
) -> None:
    """Rebind device-a to binding-a2 v2 the way the onboarding flow does.

    Identity supersedes binding-a with binding-a2; device_onboarding_devices
    records the new binding (or, with ``lifecycle='provisioned'``, a release);
    device_fleet_devices is left on binding-a v1, as in production.
    """
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        # Installed after the Session schema, as on a fresh volume: this
        # file's own grant is what lets the trust port read the row.
        await admin.execute(_DEVICE_ONBOARDING_SCHEMA_SQL)
        await admin.execute("RESET ROLE")
        await admin.execute(
            "UPDATE identity_device_bindings SET status = 'superseded', "
            "valid_until = $2 WHERE binding_id = $1",
            "binding-a",
            now,
        )
        await admin.execute(
            """
            INSERT INTO identity_device_bindings (
                binding_id, device_id, declared_mode, family_space_id,
                account_owner_person_id, binding_version, status, reason,
                valid_from, valid_until, supersedes_binding_id,
                service_profile_version, policy_bundle_version,
                consent_snapshot_id, persona_assignment_id, created_at
            ) VALUES ('binding-a2', 'device-a', 'self_use', NULL, 'actor-a', 2,
                      'active', 'supersede', $1, NULL, 'binding-a', 'self-v1',
                      'multi-subject-v2', NULL, 'mianmian:v1', $1)
            """,
            now,
        )
        await admin.executemany(
            """
            INSERT INTO identity_device_binding_roles (
                binding_id, person_id, role, status, permissions_json,
                granted_at, ended_at
            ) VALUES ('binding-a2', 'actor-a', $1, 'active', $2::jsonb, $3, NULL)
            """,
            (
                ("account_owner", '["binding.manage"]', now),
                ("primary_subject", '["content.read"]', now),
            ),
        )
        bound = lifecycle == "bound"
        await admin.execute(
            """
            INSERT INTO device_onboarding_devices (
                device_id, certificate_id, public_key_b64, product_model,
                hardware_revision, firmware_version, firmware_security_version,
                capability_manifest_hash, minimum_firmware_security_version,
                lifecycle_status, binding_id, binding_version, actor_id,
                activation_version, created_at, updated_at
            ) VALUES ('device-a', 'cert-device-a', $1, 'memoria-esp-vocat',
                      'r1', '2.4.2', 1, $2, 1, $3, $4, $5, $6, 3, $7, $7)
            """,
            "A" * 43,
            hashlib.sha256(b"manifest:device-a").hexdigest(),
            lifecycle,
            "binding-a2" if bound else None,
            2 if bound else None,
            "actor-a" if bound else None,
            now,
        )
    finally:
        await admin.close()


async def _device_trust(
    store: PostgresSessionRuntimeStore, *, binding_id: str, binding_version: int
) -> dict[str, object]:
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
    ) as connection:
        await connection.execute(
            "SELECT set_config('app.authenticated_binding', $1, true)", binding_id
        )
        raw = await connection.fetchval(
            "SELECT action_device_lock_trust($1, $2, $3, $4, $5)",
            "actor-a",
            "device-a",
            binding_id,
            binding_version,
            datetime.now(UTC),
        )
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _start_command(*, session_id: str, binding_version: int) -> object:
    from services.session_runtime.commands import StartPersistentSessionCommand

    return StartPersistentSessionCommand(
        session_id=session_id,
        actor_id="actor-a",
        device_id="device-a",
        expected_binding_version=binding_version,
        idempotency_key=f"start-{session_id}",
        now=datetime.now(UTC),
        requested_capabilities=("chat",),
    )


@pytest.mark.asyncio
async def test_rebind_through_onboarding_keeps_session_start_allowed(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import build_postgres_session_runtime_service

    store, bootstrap_dsn = postgres_runtime
    await _rebind_through_onboarding(bootstrap_dsn)

    trust = await _device_trust(store, binding_id="binding-a2", binding_version=2)
    assert trust["device_trust"] == "untrusted"
    assert trust["reason_code"] == "device_attestation_unavailable"
    # Additive: the level is untouched, the bound-link fact rides beside it.
    assert trust["device_bound"] is True

    service = build_postgres_session_runtime_service(store=store, signing_key=_SIGNING_KEY)
    profile = await service.start(
        _start_command(session_id="session-after-rebind", binding_version=2)  # type: ignore[arg-type]
    )
    assert profile.binding_id == "binding-a2"
    assert profile.binding_version == 2

    # The superseded binding is no longer the device's: onboarding says so,
    # whatever device_fleet_devices still records.
    stale = await _device_trust(store, binding_id="binding-a", binding_version=1)
    assert stale == {
        "available": True,
        "device_trust": "revoked",
        "reason_code": "device_binding_mismatch",
    }


@pytest.mark.asyncio
async def test_released_onboarding_device_cannot_start_a_session(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        PersistentSessionDenied,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime
    await _rebind_through_onboarding(bootstrap_dsn, lifecycle="provisioned")

    trust = await _device_trust(store, binding_id="binding-a2", binding_version=2)
    assert trust["device_trust"] == "revoked"
    assert trust["reason_code"] == "device_lifecycle_not_bound"

    service = build_postgres_session_runtime_service(store=store, signing_key=_SIGNING_KEY)
    with pytest.raises(PersistentSessionDenied, match="device_lifecycle_not_bound"):
        await service.start(
            _start_command(session_id="session-after-release", binding_version=2)  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_fleet_revocation_still_blocks_an_onboarding_bound_device(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    await _rebind_through_onboarding(bootstrap_dsn)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "UPDATE device_fleet_devices SET lifecycle_status = 'suspended', "
            "family_space_id = NULL, binding_id = NULL, binding_version = NULL "
            "WHERE device_id = 'device-a'"
        )
    finally:
        await admin.close()

    trust = await _device_trust(store, binding_id="binding-a2", binding_version=2)
    assert trust == {
        "available": True,
        "device_trust": "revoked",
        "reason_code": "device_fleet_lifecycle_blocked",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("certificate", "bound"),
    [("active", True), ("revoked", False), ("expired", False)],
)
async def test_the_activation_certificate_decides_the_bound_fact(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
    certificate: str,
    bound: bool,
) -> None:
    # The fleet registry's row for the very certificate onboarding issued: an
    # active one is production's shape (a certificate, no attestation); one
    # that was withdrawn or ran out is not a device to vouch for.
    store, bootstrap_dsn = postgres_runtime
    await _rebind_through_onboarding(bootstrap_dsn)
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            """
            INSERT INTO device_fleet_certificates (
                certificate_id, device_id, family_space_id, binding_id,
                binding_version, public_key_b64, key_algorithm, status,
                valid_from, valid_until, revoked_at, revocation_reason_code,
                created_at
            ) VALUES ('cert-device-a', 'device-a', NULL, NULL, NULL, $1, 'ed25519',
                      $2, $3, $4, $5, $6, $3)
            """,
            "A" * 44,
            "active" if certificate == "expired" else certificate,
            now - timedelta(hours=2),
            now - timedelta(hours=1) if certificate == "expired" else now + timedelta(hours=1),
            now if certificate == "revoked" else None,
            "test" if certificate == "revoked" else None,
        )
    finally:
        await admin.close()

    trust = await _device_trust(store, binding_id="binding-a2", binding_version=2)

    assert trust["device_trust"] == "untrusted"
    assert trust["device_bound"] is bound


@pytest.mark.asyncio
async def test_an_attested_fleet_device_is_verified_and_reports_no_bound_fact(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    # Verified needs no onboarding row; the bound fact is derived from one.
    store, bootstrap_dsn = postgres_runtime
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin, device_id="device-a", binding_id="binding-a", now=datetime.now(UTC)
        )
    finally:
        await admin.close()

    trust = await _device_trust(store, binding_id="binding-a", binding_version=1)

    assert trust["device_trust"] == "verified"
    assert trust["device_bound"] is False


@pytest.mark.asyncio
async def test_session_schema_grants_the_onboarding_trust_read_when_installed_later(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    store, bootstrap_dsn = postgres_runtime
    await _rebind_through_onboarding(bootstrap_dsn)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        # An existing volume where onboarding predates this grant: re-running
        # the Session schema must restore it on its own.
        await admin.execute(
            "DROP POLICY device_onboarding_action_bridge_devices ON device_onboarding_devices"
        )
        await admin.execute(
            "DROP POLICY device_onboarding_action_bridge_devices_lock "
            "ON device_onboarding_devices"
        )
        await admin.execute(
            "REVOKE ALL ON device_onboarding_devices FROM memoria_device_action_bridge_owner"
        )
        await admin.execute(SESSION_RUNTIME_SCHEMA_SQL)
        await admin.execute("RESET ROLE")
    finally:
        await admin.close()

    trust = await _device_trust(store, binding_id="binding-a2", binding_version=2)
    assert trust["device_trust"] == "untrusted"
    assert (await store.readiness())["action_executor_consent_discover_exec"] is not None
