"""Control API policy chain: signed runtime profile propagation into both policy seams."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT
from services.control_api.app.main import create_app
from services.control_api.tests.interaction_api_test_support import _MemoryCatalog
from services.control_api.tests.policy_chain_test_support import (
    _CRISIS_QUERY,
    _MEMORY_QUERY,
    _activate_link_and_grant_retention,
    _configure,
    _declare_guardianship,
    _response_plan,
    _session_policy,
    _start_session_and_switch_to_child,
    _SubjectScopedMemoryCatalog,
    pytestmark,  # noqa: F401 - pytest discovers the module-wide marker
)
from services.guardian.postgres_store import PostgresGuardianStore
from services.guardian.tests.test_guardian_postgres_store import (
    _ensure_roles,
    _role_dsn,
)
from services.session_runtime.commands import SwitchPersistentSubjectCommand
from services.session_runtime.postgres_store import (
    PostgresSessionRuntimeStore,
)
from services.session_runtime.profile_service import verify_runtime_profile_payload
from services.session_runtime.service import (
    PersistentSessionDenied,
    build_postgres_session_runtime_service,
)
from services.session_runtime.tests.postgres_store_test_support import (
    _SIGNING_KEY,
    postgres_runtime,  # noqa: F401 - pytest discovers imported fixtures by name
    postgres_runtime_with_consent,  # noqa: F401 - pytest discovers imported fixtures by name
)


@pytest.mark.asyncio
async def test_real_runtime_child_profile_reaches_both_policy_seams(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    assert signing_key == _SIGNING_KEY
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
    )
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chain = await _start_session_and_switch_to_child(
            client,
            app,
            bootstrap_dsn=bootstrap_dsn,
            device_id="device-real-runtime-chain",
            binding_id="binding-real-runtime-chain",
            child_person_id="minor-real-runtime-chain",
        )
        await _declare_guardianship(
            app,
            guardian_id=chain.account_id,
            child_id=chain.child_person_id,
            device_id="device-real-runtime-chain",
            now=datetime.now(UTC),
        )

        # A private-context question for the child reads nothing: the account's
        # memory belongs to the account, and the child has no retention consent.
        memory_turn = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_MEMORY_QUERY,
            turn_id=1,
            generation_id=1,
        )
        assert memory_turn.status_code == 200, memory_turn.text
        assert memory_turn.json()["grounded_items"] == []
        assert catalog.queries == []

        # The fixed crisis text is still delivered verbatim for this subject.
        crisis_turn = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_CRISIS_QUERY,
            turn_id=2,
            generation_id=2,
        )
        assert crisis_turn.status_code == 200, crisis_turn.text
        assert crisis_turn.json()["direct_text"] == CRISIS_SELF_SCRIPT.display

        # /session-policy must read the SAME signed profile the authority issued
        # for the child — not an account fallback, not a fixture.
        policy_response = await _session_policy(client, session_id=chain.session_id)
        assert policy_response.status_code == 200, policy_response.text
        policy = policy_response.json()
        signed = policy["runtime_profile"]
        assert signed["runtime_profile_id"] == chain.child_profile.runtime_profile_id
        assert signed["session_epoch"] == 2 == chain.child_profile.session_epoch
        assert signed["active_subject_id"] == chain.child_person_id
        assert signed["subject_category"] == "minor"
        assert signed["age_band"] == "under_14"
        assert signed["service_mode"] == "student_minor"
        assert signed["speaker_state"] == "confirmed"
        assert verify_runtime_profile_payload(signed, signing_key=signing_key) is True
        assert policy["interaction_mode"] == "companion"
        assert policy["runtime_profile_version"] == 0  # never observed by the device yet
        # The account owner's own name must not follow another subject.
        assert "owner_display_name" not in policy
        # One-sided declaration only: no consent, no verified link, so the child
        # keeps the ephemeral-only ceiling and no session capability.
        assert policy["memory_retention"] == "ephemeral_only"
        assert policy["history_eligible"] is False
        assert policy["owner_projection_eligible"] is False
        assert policy["capabilities"] == {
            "conversation": False,
            "private_memory": False,
            "persona": False,
            "persona_low_sensitivity": False,
            "tools": False,
            "history": False,
            "learning": False,
            "voice_profile": False,
        }
        assert chain.child_profile.capabilities == ()
        assert {str(item) for item in signed["capabilities"]} == set()

        # The subject switch superseded the previous epoch's profile.
        with pytest.raises(PersistentSessionDenied, match="stale"):
            await chain.runtime_service.decide(
                runtime_profile_id=chain.initial_profile.runtime_profile_id,
                capability="chat",
                actor_id=chain.account_id,
                data_classification="ephemeral",
                safety_state="normal",
                now=datetime.now(UTC),
            )

        # The crisis notification is bound to the child AND to the declared
        # guardian, without manufacturing an active guardian link.
        notifications = await app.state.guardian_store.guardian_notifications(
            guardian_user_id=chain.account_id
        )
        assert [(item.minor_user_id, item.status) for item in notifications] == [
            (chain.child_person_id, "pending")
        ]
        assert (
            await app.state.guardian_store.active_link(
                guardian_user_id=chain.account_id,
                minor_user_id=chain.child_person_id,
            )
            is None
        )
        events = await app.state.life_archive.evidence_window(
            account_id=chain.child_person_id,
            occurred_after=datetime(1970, 1, 1, tzinfo=UTC),
            occurred_before=datetime.now(UTC),
            event_types=("guardian.crisis_event",),
        )
        assert len(events) == 1
        payload = dict(events[0].payload)
        assert payload["declared_guardian_count"] == 1
        assert payload["contains_transcript"] is False
        assert _CRISIS_QUERY not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_same_session_reads_account_memory_only_for_the_account_subject(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Subject-scoped memory, proven inside one real session.

    The only difference between the two turns is the subject the signed Runtime
    Profile names: the child turn reads nothing, the account's own turn reads
    exactly once.  That is what makes the first turn evidence rather than an
    always-empty path.
    """

    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
    )
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chain = await _start_session_and_switch_to_child(
            client,
            app,
            bootstrap_dsn=bootstrap_dsn,
            device_id="device-real-runtime-memory",
            binding_id="binding-real-runtime-memory",
            child_person_id="minor-real-runtime-memory",
        )

        child_turn = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_MEMORY_QUERY,
            turn_id=1,
            generation_id=1,
        )
        assert child_turn.status_code == 200, child_turn.text
        assert child_turn.json()["grounded_items"] == []
        assert catalog.queries == []

        account_profile = await chain.runtime_service.switch_subject(
            SwitchPersistentSubjectCommand(
                session_id=chain.session_id,
                actor_id=chain.account_id,
                subject_id=chain.account_id,
                now=datetime.now(UTC),
                requested_capabilities=("chat",),
            )
        )
        assert account_profile.active_subject_id == chain.account_id
        assert account_profile.session_epoch == 3

        owner_policy_response = await _session_policy(client, session_id=chain.session_id)
        assert owner_policy_response.status_code == 200, owner_policy_response.text
        owner_policy = owner_policy_response.json()
        assert owner_policy["runtime_profile"]["active_subject_id"] == chain.account_id
        assert (
            verify_runtime_profile_payload(owner_policy["runtime_profile"], signing_key=signing_key)
            is True
        )
        # The account's own turn may keep its own display name and is not
        # subject to the minor retention ceiling.
        assert owner_policy["owner_display_name"] == "朋友"
        assert owner_policy["capabilities"]["conversation"] is True
        assert "memory_retention" not in owner_policy

        owner_turn = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_MEMORY_QUERY,
            turn_id=3,
            generation_id=3,
        )
        assert owner_turn.status_code == 200, owner_turn.text
        items = owner_turn.json()["grounded_items"]
        assert len(catalog.queries) == 1
        assert items
        assert "杭州" in json.dumps(items, ensure_ascii=False)


@pytest.mark.asyncio
async def test_retention_consent_does_not_redirect_the_read_to_the_account_key(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A consented minor still must not be fed the account owner's memory.

    The retention ceiling and the memory scope are two different decisions.  A
    retention consent for the child lifts only the ceiling: the account-keyed
    legacy archive stores every first-person claim under the login account, so
    reading it "for the child" would hand the child the account owner's own
    memory.  This case is the one where that second gate is load-bearing —
    without it the read would run and return the account's row.
    """

    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
    )
    catalog = _SubjectScopedMemoryCatalog()
    app.state.memory_catalog = catalog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chain = await _start_session_and_switch_to_child(
            client,
            app,
            bootstrap_dsn=bootstrap_dsn,
            device_id="device-real-runtime-consent",
            binding_id="binding-real-runtime-consent",
            child_person_id="minor-real-runtime-consent",
        )
        await _activate_link_and_grant_retention(
            app,
            guardian_id=chain.account_id,
            child_id=chain.child_person_id,
            now=datetime.now(UTC),
        )

        # The consent is found for the child's OWN person id: the ceiling lifts.
        child_policy_response = await _session_policy(client, session_id=chain.session_id)
        assert child_policy_response.status_code == 200, child_policy_response.text
        child_policy = child_policy_response.json()
        assert child_policy["runtime_profile"]["active_subject_id"] == chain.child_person_id
        assert "memory_retention" not in child_policy
        # Consent is not relationship evidence: the runtime still issues no
        # session capability for this subject.
        assert child_policy["capabilities"]["conversation"] is False
        assert chain.child_profile.capabilities == ()

        # ... yet the child's turn still reads nothing from the account key.
        child_turn = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_MEMORY_QUERY,
            turn_id=1,
            generation_id=1,
        )
        assert child_turn.status_code == 200, child_turn.text
        assert child_turn.json()["grounded_items"] == []
        # The lifted ceiling allows a read, but only of the child's own
        # lineage: nothing is ever read under the account key for the child.
        assert [getattr(q, "subject_id", None) for q in catalog.queries] == [
            chain.child_person_id
        ]

        # The account's own turn, in the same session, does read exactly once.
        account_profile = await chain.runtime_service.switch_subject(
            SwitchPersistentSubjectCommand(
                session_id=chain.session_id,
                actor_id=chain.account_id,
                subject_id=chain.account_id,
                now=datetime.now(UTC),
                requested_capabilities=("chat",),
            )
        )
        assert account_profile.active_subject_id == chain.account_id
        owner_turn = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_MEMORY_QUERY,
            turn_id=2,
            generation_id=2,
        )
        assert owner_turn.status_code == 200, owner_turn.text
        assert len(catalog.queries) == 2
        assert getattr(catalog.queries[-1], "subject_id", None) == chain.account_id
        assert owner_turn.json()["grounded_items"]


@pytest.mark.asyncio
async def test_real_postgres_person_consent_lifts_and_closes_both_policy_seams(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04/P1-01: real-PG person consent drives both Agent-facing seams.

    The account-less subject's consent lives in the NOBYPASSRLS PostgreSQL
    guardian store (granted and revoked through GuardianConsentService) while
    the signed profile comes from the real persistent Session Runtime.  Both
    /v1/interaction/session-policy and /v1/interaction/response-plan must see
    the same grant and then close again.
    """

    from services.guardian.consent import GuardianConsentService

    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
    )
    catalog = _SubjectScopedMemoryCatalog()
    app.state.memory_catalog = catalog

    database = bootstrap_dsn.rstrip("/").rsplit("/", 1)[-1]
    admin = await asyncpg.connect(bootstrap_dsn)
    guardian: PostgresGuardianStore | None = None
    try:
        await _ensure_roles(admin, database=database)
    finally:
        await admin.close()
    try:
        guardian = PostgresGuardianStore(
            dsn=_role_dsn(
                bootstrap_dsn,
                database=database,
                role="memoria_guardian",
                password="api-role-password",
            ),
            bootstrap_dsn=bootstrap_dsn,
            maintenance_dsn=_role_dsn(
                bootstrap_dsn,
                database=database,
                role="memoria_guardian_maintenance",
                password="maintenance-role-password",
            ),
            worker_dsn=_role_dsn(
                bootstrap_dsn,
                database=database,
                role="memoria_guardian_worker",
                password="worker-role-password",
            ),
        )
        await guardian.initialize()
        app.state.guardian_store = guardian
        consent_service = GuardianConsentService(guardian, app.state.life_archive)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            chain = await _start_session_and_switch_to_child(
                client,
                app,
                bootstrap_dsn=bootstrap_dsn,
                device_id="device-pg-person-consent",
                binding_id="binding-pg-person-consent",
                child_person_id="minor-pg-person-consent",
            )

            # No consent yet: both seams stay conservative.
            policy_closed = await _session_policy(
                client, session_id=chain.session_id
            )
            assert policy_closed.status_code == 200, policy_closed.text
            assert policy_closed.json()["memory_retention"] == "ephemeral_only"
            memory_closed = await _response_plan(
                client,
                session_id=chain.session_id,
                query=_MEMORY_QUERY,
                turn_id=1,
                generation_id=1,
            )
            assert memory_closed.status_code == 200, memory_closed.text
            assert memory_closed.json()["grounded_items"] == []
            assert catalog.queries == []

            # Grant the person consent in the real PostgreSQL store.
            now = datetime.now(UTC)
            consent_id = str(uuid.uuid4())
            await consent_service.grant_for_person(
                subject_person_id=chain.child_person_id,
                grantor_person_id=chain.account_id,
                consent_kind="memory_retention",
                policy_version="minor-retention-v1",
                evidence_event_id="pg-person-consent-grant",
                consent_id=consent_id,
                now=now,
            )
            assert (
                await guardian.get_person_consent(
                    consent_id=consent_id,
                    actor_person_id=chain.account_id,
                    subject_person_id=chain.child_person_id,
                )
            ).consent_id == consent_id

            policy_open = await _session_policy(
                client, session_id=chain.session_id
            )
            assert policy_open.status_code == 200, policy_open.text
            assert "memory_retention" not in policy_open.json()
            memory_open = await _response_plan(
                client,
                session_id=chain.session_id,
                query=_MEMORY_QUERY,
                turn_id=2,
                generation_id=2,
            )
            # The open retention gate reads the child's own lineage only; the
            # account owner's memory never answers for another subject.
            assert memory_open.status_code == 200, memory_open.text
            assert memory_open.json()["grounded_items"] == []
            assert [getattr(q, "subject_id", None) for q in catalog.queries] == [
                chain.child_person_id
            ]

            # Revoke in the real PostgreSQL store: both seams close again.
            await consent_service.revoke_for_person(
                consent_id=consent_id,
                grantor_person_id=chain.account_id,
                subject_person_id=chain.child_person_id,
                evidence_event_id="pg-person-consent-revoke",
                now=now + timedelta(minutes=1),
            )
            assert (
                await guardian.active_consent(
                    minor_user_id=chain.child_person_id,
                    consent_kind="memory_retention",
                )
                is None
            )
            policy_closed_again = await _session_policy(
                client, session_id=chain.session_id
            )
            assert policy_closed_again.status_code == 200
            assert (
                policy_closed_again.json()["memory_retention"] == "ephemeral_only"
            )
            queries_before = len(catalog.queries)
            memory_closed_again = await _response_plan(
                client,
                session_id=chain.session_id,
                query=_MEMORY_QUERY,
                turn_id=3,
                generation_id=3,
            )
            assert memory_closed_again.status_code == 200
            assert memory_closed_again.json()["grounded_items"] == []
            assert len(catalog.queries) == queries_before
    finally:
        if guardian is not None:
            await guardian.close()
