"""P0-04: the signed Runtime Profile must reach both policy seams through the
REAL persistent Session Runtime.

``test_student_safety_loop.py`` injects a signed RuntimeProfile with
``_attach_signed_runtime_profile``, so it proves the decision path but not the
runtime chain; ``test_accountless_child_profile_reaches_the_device_through_the
_real_api`` closes the HTTP half (binding -> session -> resolve-subject ->
app_confirm switch -> device runtime-profile) and still stops before the two
Agent-facing seams, because those resolve the profile from
``PostgresSessionRuntimeService``, which is installed only for production.

This module wires that authority to a real PostgreSQL instance and drives
``/v1/interaction/session-policy`` and ``/v1/interaction/response-plan`` with the
profile the authority itself issued after an authoritative subject switch.  The
authority under test (binding, subject facts, subject switch, signed profile,
session epoch) is PostgreSQL.  The login-account plane, the guardian outbox and
the one-sided guardian declaration live in the app's local stores, exactly as in
the other Control tests, and the declaration is written through the same
Identity API the production write path uses.

Boundary: nothing here is a device receipt.  Device admission, the speaker/voice
gate and the on-device crisis script still need the hardware window.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from packages.contracts.generated.python.multi_subject_contracts import (
    RuntimeProfileSignedV2,
)
from services.common.companion_response_safety import CRISIS_SUPPORT_REPLY
from services.control_api.app.main import create_app
from services.control_api.app.subject_verification import ensure_account_person
from services.control_api.tests.test_interaction_api import _MemoryCatalog
from services.guardian.domain import ConsentRecord
from services.guardian.postgres_store import PostgresGuardianStore
from services.guardian.tests.test_guardian_postgres_store import (
    _ensure_roles,
    _role_dsn,
)
from services.session_runtime.postgres_store import (
    PostgresSessionRuntimeStore,
    SessionRuntimeConflict,
)
from services.session_runtime.profile_service import verify_runtime_profile_payload
from services.session_runtime.service import (
    PersistentSessionDenied,
    PersistentSessionNotFound,
    PersistentSessionUnavailable,
    PostgresSessionRuntimeService,
    SwitchPersistentSubjectCommand,
    build_postgres_session_runtime_service,
)
from services.session_runtime.tests.test_postgres_store import (
    _SIGNING_KEY,
    _seed_delegated_binding,
    postgres_runtime,  # noqa: F401 - pytest discovers imported fixtures by name
    postgres_runtime_with_consent,  # noqa: F401 - pytest discovers imported fixtures by name
)

_POLICY_TOKEN = "interaction-policy-token-that-is-long-enough"
_RESPONSE_PLAN_TOKEN = "response-plan-token-that-is-long-enough"
_CRISIS_QUERY = "我不会做题。我不想活了"
_MEMORY_QUERY = "我们以前聊过什么？"
# The same evidence id the production guardian declaration writes.
_DECLARATION_EVIDENCE = "guardian_declaration_v1:device_binding"

pytestmark = pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the real persistent Session Runtime chain",
)


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "persistent-runtime-auth-secret-long-enough")
    monkeypatch.setenv(
        "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET",
        _SIGNING_KEY.decode(),
    )
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "persistent-runtime-chain-test")
    monkeypatch.setenv("READINESS_GATE_TTL_S", "86400")
    monkeypatch.setenv("MEMORIA_INTERACTION_POLICY_TOKEN", _POLICY_TOKEN)
    monkeypatch.setenv("MEMORIA_RESPONSE_PLAN_TOKEN", _RESPONSE_PLAN_TOKEN)
    monkeypatch.setenv("OFFLINE_MOCK", "true")


@dataclass(frozen=True, slots=True)
class _Chain:
    """One device session whose active subject is an account-less minor."""

    account_id: str
    session_id: str
    child_person_id: str
    initial_profile: RuntimeProfileSignedV2
    child_profile: RuntimeProfileSignedV2
    runtime_service: PostgresSessionRuntimeService


async def _anonymous_account(client: AsyncClient) -> tuple[str, dict[str, str]]:
    response = await client.post("/v1/auth/anonymous")
    assert response.status_code == 200, response.text
    body = response.json()
    return str(body["user_id"]), {"Authorization": f"Bearer {body['access_token']}"}


async def _declare_guardianship(
    app: Any,
    *,
    guardian_id: str,
    child_id: str,
    device_id: str,
    now: datetime,
) -> None:
    """Write the one-sided declaration AND its binding through the app stores.

    The subject has no account, so the ``guardian_of`` relationship stays
    pending: the guardian confirms their own side and the target side is never
    confirmed.  This is the production write shape (``POST /v1/device-bindings``
    with ``parent_for_child`` + ``subject_draft``), not an activated link, and
    it is what makes the declaration binding-scoped: P0-04 only accepts a
    declared guardian whose declaration carries the device-binding evidence id
    and who owns an ACTIVE binding naming the subject.

    The Session authority's own binding lives in PostgreSQL
    (``_seed_delegated_binding``); this helper writes the app-side counterpart
    in the same sqlite Identity store the notification path reads, with the
    same device_id, so both halves describe one device.
    """

    identity = app.state.identity_service
    await ensure_account_person(
        app.state.memory_store,
        identity,
        user_id=guardian_id,
        now=now,
    )
    await identity.register_person(
        person_id=child_id,
        display_name="独立小明",
        timezone="Asia/Shanghai",
        subject_category="minor",
        age_band="under_14",
        age_evidence_status="unverified",
        now=now,
    )
    proposed = await identity.propose_relationship(
        source_person_id=guardian_id,
        target_person_id=child_id,
        relation_type="guardian_of",
        established_evidence_id=_DECLARATION_EVIDENCE,
        actor_person_id=guardian_id,
        now=now,
    )
    await identity.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=guardian_id,
        now=now,
    )
    await identity.create_binding(
        device_id=device_id,
        declared_mode="parent_for_child",
        account_owner_person_id=guardian_id,
        primary_subject_ids=(child_id,),
        roles=((guardian_id, "guardian"), (guardian_id, "device_admin")),
        consent_offer_ids=("offer_minor_voice_session_v1",),
        service_preferences={"memory_level": "ephemeral"},
        persona_assignment_id="starlight:v1",
        service_profile_version="parent_for_child-v1",
        policy_bundle_version="multi-subject-v1",
        actor_person_id=guardian_id,
        now=now,
    )
    # The declaration must now resolve back to this guardian: a declaration
    # without its binding is exactly the third-party self-declaration P0-04
    # rejects, and it would silently drop the notification asserted below.
    assert await identity.declared_guardians(subject_person_id=child_id) == (
        guardian_id,
    )


async def _start_session_and_switch_to_child(
    client: AsyncClient,
    app: Any,
    *,
    bootstrap_dsn: str,
    device_id: str,
    binding_id: str,
    child_person_id: str,
) -> _Chain:
    """Bind a device in the authority, start a session, switch to the child."""

    account_id, headers = await _anonymous_account(client)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id=account_id,
            subject_id=child_person_id,
            device_id=device_id,
            binding_id=binding_id,
            declared_mode="parent_for_child",
            subject_category="minor",
            age_band="under_14",
        )
    finally:
        await admin.close()

    created = await client.post(
        "/v1/sessions",
        headers={**headers, "Idempotency-Key": f"{binding_id}-session-1"},
        json={
            "client": {
                "platform": "web",
                "timezone": "Asia/Shanghai",
                "device_id": device_id,
                "binding_version": 1,
            }
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    session_id = str(body["session_id"])
    initial_profile = RuntimeProfileSignedV2.model_validate(body["runtime_profile"])
    assert initial_profile.active_subject_id is None
    assert initial_profile.service_mode.value == "unknown_safe"
    assert initial_profile.session_epoch == 1

    runtime_service = app.state.session_runtime_service
    assert isinstance(runtime_service, PostgresSessionRuntimeService)
    child_profile = await runtime_service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=session_id,
            actor_id=account_id,
            subject_id=child_person_id,
            now=datetime.now(UTC),
            requested_capabilities=("chat", "tutor", "english_practice"),
        )
    )
    assert child_profile.active_subject_id == child_person_id
    assert child_profile.session_epoch == initial_profile.session_epoch + 1
    return _Chain(
        account_id=account_id,
        session_id=session_id,
        child_person_id=child_person_id,
        initial_profile=initial_profile,
        child_profile=child_profile,
        runtime_service=runtime_service,
    )


async def _activate_link_and_grant_retention(
    app: Any,
    *,
    guardian_id: str,
    child_id: str,
    now: datetime,
) -> None:
    """Fixture state: an activated guardian link with a retention consent.

    The link is written directly in the local guardian store because an
    account-less subject cannot drive the HTTP confirmation flow; the recorded
    ``verified_via`` is ``manual_review``, never a fabricated WeChat
    verification.  What matters for the assertions below is only that the
    minor's own person id now has a retention consent.
    """

    store = app.state.guardian_store
    binding_code = "persistent-runtime-retention-binding-code"
    digest = hashlib.sha256(binding_code.encode("utf-8")).hexdigest()
    link = await store.create_link(
        guardian_user_id=guardian_id,
        minor_user_id=child_id,
        relation="parent",
        verified_via="manual_review",
        binding_code_hash=digest,
        binding_expires_at=now + timedelta(minutes=15),
        now=now,
    )
    await store.confirm_link(
        link_id=link.link_id,
        minor_user_id=child_id,
        binding_code_hash=digest,
        now=now,
    )
    await store.grant_consent(
        ConsentRecord(
            consent_id="consent-retention-real-runtime",
            link_id=link.link_id,
            consent_kind="memory_retention",
            policy_version="minor-retention-v1",
            granted_at=now,
            evidence_event_id="evidence-retention-real-runtime",
        ),
        actor_user_id=guardian_id,
    )


async def _response_plan(
    client: AsyncClient,
    *,
    session_id: str,
    query: str,
    turn_id: int,
    generation_id: int,
) -> Any:
    return await client.post(
        "/v1/interaction/response-plan",
        headers={"X-Memoria-Internal-Token": _RESPONSE_PLAN_TOKEN},
        json={
            "session_id": session_id,
            "query": query,
            "utterance_intent": "chat",
            "fence": {
                "session_id": session_id,
                "turn_id": turn_id,
                "generation_id": generation_id,
                "tool_epoch": 0,
            },
            "speaker_decision": {
                "classification": "owner",
                "reason_code": "trusted",
                "model_version": "campplus-test-v1",
                "profile_id": "speaker-profile-1",
                "template_version": 1,
            },
        },
    )


async def _session_policy(client: AsyncClient, *, session_id: str) -> Any:
    return await client.post(
        "/v1/interaction/session-policy",
        headers={"X-Memoria-Internal-Token": _POLICY_TOKEN},
        json={"session_id": session_id},
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
        assert crisis_turn.json()["direct_text"] == CRISIS_SUPPORT_REPLY

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
    catalog = _MemoryCatalog()
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
        assert catalog.queries == []

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
        assert len(catalog.queries) == 1
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
    catalog = _MemoryCatalog()
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
            # The account-keyed legacy catalog must never answer for another
            # subject (subject-keyed memory migration is still pending), so
            # the open retention gate must not leak account memory here.
            assert memory_open.status_code == 200, memory_open.text
            assert memory_open.json()["grounded_items"] == []
            assert catalog.queries == []

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


def _action_body(
    session_id: str,
    *,
    runtime_profile_id: str,
    session_epoch: int,
    generation_id: int,
    turn_id: int,
    tool_epoch: int,
    capability: str = "memory_capture",
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "runtime_profile_id": runtime_profile_id,
        "capability": capability,
        "session_epoch": session_epoch,
        "generation_id": generation_id,
        "turn_id": turn_id,
        "tool_epoch": tool_epoch,
        "data_classification": "private",
        "safety_state": "normal",
    }


@pytest.mark.asyncio
async def test_real_runtime_authority_unavailable_fails_closed_on_both_seams(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04 matrix: a real, installed authority that cannot answer must close.

    Both seams run against the PostgreSQL authority until it cannot answer —
    first because it is gone, then because its PostgreSQL is unreachable.  A
    dead database is not a fabricated profile: this is the production
    fail-closed shape for ``authority unavailable``.
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
            device_id="device-real-runtime-unavailable",
            binding_id="binding-real-runtime-unavailable",
            child_person_id="minor-real-runtime-unavailable",
        )

        # 1. The authority is not installed at all (production never wires the
        #    persistent Runtime outside deployment): the device-facing policy
        #    seam fails closed with the unavailable code, while /response-plan
        #    runs the documented offline deployment profile — the login
        #    account is the subject by construction, so the turn reads the
        #    account's own (empty) memory and never a child subject's state.
        app.state._state.pop("session_runtime_service", None)  # noqa: SLF001 - Starlette state dict, mirrors test_interaction_api
        policy_missing = await _session_policy(client, session_id=chain.session_id)
        assert policy_missing.status_code == 503, policy_missing.text
        assert (
            policy_missing.json()["detail"]["code"]
            == "session_runtime_authority_unavailable"
        )
        plan_missing = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_MEMORY_QUERY,
            turn_id=1,
            generation_id=1,
        )
        assert plan_missing.status_code == 200, plan_missing.text
        assert plan_missing.json()["grounded_items"] == []

        # 2. The authority is installed but its PostgreSQL is unreachable: the
        #    policy seam fails closed and /response-plan stays conservative —
        #    no memory read, no account fallback for the child subject, and
        #    the fixed crisis reply is still delivered verbatim.
        dead_store = PostgresSessionRuntimeStore(
            dsn="postgresql://memoria_session_api:x@127.0.0.1:9/postgres",
            action_dsn="postgresql://memoria_action_executor:x@127.0.0.1:9/postgres",
            connect_timeout_seconds=0.5,
            command_timeout_seconds=1.0,
        )
        app.state.session_runtime_service = build_postgres_session_runtime_service(
            store=dead_store,
            signing_key=signing_key,
        )
        policy_dead = await _session_policy(client, session_id=chain.session_id)
        assert policy_dead.status_code == 503, policy_dead.text
        assert (
            policy_dead.json()["detail"]["code"]
            == "session_runtime_authority_unavailable"
        )
        queries_before_dead = len(catalog.queries)
        crisis_dead = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_CRISIS_QUERY,
            turn_id=2,
            generation_id=2,
        )
        assert crisis_dead.status_code == 200, crisis_dead.text
        assert crisis_dead.json()["direct_text"] == CRISIS_SUPPORT_REPLY
        memory_dead = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_MEMORY_QUERY,
            turn_id=3,
            generation_id=3,
        )
        assert memory_dead.status_code == 200, memory_dead.text
        assert memory_dead.json()["grounded_items"] == []
        # No failure path leaked a memory read for the child subject.
        assert len(catalog.queries) == queries_before_dead


@pytest.mark.asyncio
async def test_real_runtime_profile_expiry_fails_closed_but_keeps_session_closable(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04 matrix: the profile TTL issued by the real authority expires.

    The expiry is produced by the authority itself (``profile_ttl``), not by
    editing the profile: both seams must fail closed once the TTL passes, and
    the expired profile must still not strand the Session authority (close
    remains possible).
    """

    store, bootstrap_dsn = postgres_runtime
    _configure(monkeypatch, tmp_path)
    app = create_app()
    signing_key = app.state.settings.runtime_profile_signing_key()
    app.state.session_runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=signing_key,
        profile_ttl=timedelta(seconds=2),
    )
    catalog = _MemoryCatalog()
    app.state.memory_catalog = catalog

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chain = await _start_session_and_switch_to_child(
            client,
            app,
            bootstrap_dsn=bootstrap_dsn,
            device_id="device-real-runtime-expiry",
            binding_id="binding-real-runtime-expiry",
            child_person_id="minor-real-runtime-expiry",
        )
        assert chain.child_profile.expires_at > chain.child_profile.issued_at

        policy_live = await _session_policy(client, session_id=chain.session_id)
        assert policy_live.status_code == 200, policy_live.text

        await asyncio.sleep(2.3)
        with pytest.raises(PersistentSessionDenied, match="expired"):
            await chain.runtime_service.current(
                actor_id=chain.account_id,
                session_id=chain.session_id,
                now=datetime.now(UTC),
            )
        policy_expired = await _session_policy(client, session_id=chain.session_id)
        assert policy_expired.status_code == 503, policy_expired.text
        assert (
            policy_expired.json()["detail"]["code"]
            == "session_runtime_authority_unavailable"
        )
        plan_expired = await _response_plan(
            client,
            session_id=chain.session_id,
            query=_MEMORY_QUERY,
            turn_id=1,
            generation_id=1,
        )
        # The response-plan seam keeps serving the conservative fixed reply:
        # the expired profile is never used as the subject, no memory is read
        # and no minor is guessed.
        assert plan_expired.status_code == 200, plan_expired.text
        assert plan_expired.json()["grounded_items"] == []
        assert catalog.queries == []

        # Expiry denies conversation work but must not strand the authority.
        closed = await chain.runtime_service.close_session(
            actor_id=chain.account_id,
            session_id=chain.session_id,
            reason_code="device_close",
            now=datetime.now(UTC),
        )
        assert closed.applied is True


@pytest.mark.asyncio
async def test_real_runtime_cross_account_manager_cannot_switch_or_replay_stale_profile(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04 matrix: a different login account cannot manage the child session.

    The legal manager of the binding is the account that owns it.  Another
    anonymous account must not be able to switch the subject of a session it
    does not own, and the device-facing action seam must reject both the stale
    profile id of a superseded epoch and a stale session epoch.
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
            device_id="device-real-runtime-manager",
            binding_id="binding-real-runtime-manager",
            child_person_id="minor-real-runtime-manager",
        )
        account_b, headers_b = await _anonymous_account(client)
        assert account_b != chain.account_id

        # A foreign manager cannot switch this session's subject...
        with pytest.raises(
            (PersistentSessionDenied, PersistentSessionNotFound)
        ):
            await chain.runtime_service.switch_subject(
                SwitchPersistentSubjectCommand(
                    session_id=chain.session_id,
                    actor_id=account_b,
                    subject_id=chain.child_person_id,
                    now=datetime.now(UTC),
                    requested_capabilities=("chat",),
                )
            )
        # ...and cannot even read another session as its current authority.
        with pytest.raises((PersistentSessionDenied, PersistentSessionNotFound)):
            await chain.runtime_service.current(
                actor_id=account_b,
                session_id=chain.session_id,
                now=datetime.now(UTC),
            )
        # ...and the session epoch is unchanged: no partial switch happened.
        profile_after, context_after = await chain.runtime_service.current(
            actor_id=chain.account_id,
            session_id=chain.session_id,
            now=datetime.now(UTC),
        )
        assert profile_after.runtime_profile_id == chain.child_profile.runtime_profile_id
        assert context_after.session_epoch == chain.child_profile.session_epoch

        # Device-facing seam: the stale child profile of the current epoch is
        # the one a compromised Agent would replay.  A superseded profile id is
        # rejected as forged; a stale epoch is rejected as a stale fence.
        stale_profile = await client.post(
            "/v1/interaction/action-policy",
            headers={"X-Memoria-Internal-Token": _POLICY_TOKEN},
            json=_action_body(
                chain.session_id,
                runtime_profile_id="rp-from-a-superseded-epoch",
                session_epoch=context_after.session_epoch,
                generation_id=context_after.generation_id,
                turn_id=context_after.turn_id,
                tool_epoch=context_after.tool_epoch,
            ),
        )
        assert stale_profile.status_code == 403, stale_profile.text
        assert stale_profile.json()["detail"]["code"] == "action_profile_forged"

        stale_epoch = await client.post(
            "/v1/interaction/action-policy",
            headers={"X-Memoria-Internal-Token": _POLICY_TOKEN},
            json=_action_body(
                chain.session_id,
                runtime_profile_id=context_after.current_runtime_profile_id,
                session_epoch=1,
                generation_id=context_after.generation_id,
                turn_id=context_after.turn_id,
                tool_epoch=context_after.tool_epoch,
            ),
        )
        assert stale_epoch.status_code == 409, stale_epoch.text
        assert stale_epoch.json()["detail"]["code"] == "action_fence_stale"

        # A fresh-epoch positive action authorization is exercised on the real
        # consent-enabled PG authority by
        # ``session_runtime/tests/test_postgres_store.py::
        # test_authorize_action_reuses_same_transaction_consent_discovery``;
        # this module pins the fail-closed rejections at the HTTP seam.


@pytest.mark.asyncio
async def test_real_runtime_concurrent_switches_serialize_and_supersede_old_epochs(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-04 matrix: concurrent subject switches never cross or double-issue.

    Contending switch commands on one real session must either serialize into
    strictly consecutive epochs or be denied by the contention gate; they must
    never both claim the same epoch.  After the storm, the authority's current
    profile is exactly the last accepted epoch, both HTTP seams read that same
    subject, and every superseded epoch is denied as stale.
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
            device_id="device-real-runtime-races",
            binding_id="binding-real-runtime-races",
            child_person_id="minor-real-runtime-races",
        )
        base_epoch = chain.child_profile.session_epoch

        async def _switch(subject_id: str) -> RuntimeProfileSignedV2:
            return await chain.runtime_service.switch_subject(
                SwitchPersistentSubjectCommand(
                    session_id=chain.session_id,
                    actor_id=chain.account_id,
                    subject_id=subject_id,
                    now=datetime.now(UTC),
                    requested_capabilities=("chat",),
                )
            )

        contention: tuple[type[BaseException], ...] = (
            PersistentSessionDenied,
            PersistentSessionUnavailable,
            SessionRuntimeConflict,
        )
        outcomes: list[RuntimeProfileSignedV2] = []
        for index in range(3):
            pair = (
                chain.child_person_id,
                chain.account_id,
            ) if index % 2 == 0 else (
                chain.account_id,
                chain.child_person_id,
            )
            results = await asyncio.gather(
                _switch(pair[0]),
                _switch(pair[1]),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, contention):
                    continue
                if isinstance(result, BaseException):
                    pytest.fail(f"unexpected switch failure: {result!r}")
                outcomes.append(result)
            assert outcomes, "a concurrent switch round must make progress"

        epochs = [item.session_epoch for item in outcomes]
        assert len(set(epochs)) == len(epochs), "each switch issues its own epoch"
        assert epochs == sorted(epochs), "epochs never move backwards"
        assert min(epochs) == base_epoch + 1, "the first new epoch is contiguous"

        final_profile, final_context = await chain.runtime_service.current(
            actor_id=chain.account_id,
            session_id=chain.session_id,
            now=datetime.now(UTC),
        )
        assert final_profile.session_epoch == max(epochs)
        assert final_context.session_epoch == final_profile.session_epoch
        assert final_profile.runtime_profile_id == final_context.current_runtime_profile_id

        policy_final = await _session_policy(client, session_id=chain.session_id)
        assert policy_final.status_code == 200, policy_final.text
        final_signed = policy_final.json()["runtime_profile"]
        assert final_signed["session_epoch"] == final_profile.session_epoch
        assert final_signed["runtime_profile_id"] == final_profile.runtime_profile_id
        assert final_signed["active_subject_id"] == final_profile.active_subject_id
        assert verify_runtime_profile_payload(final_signed, signing_key=signing_key) is True

        # Every superseded epoch is dead: the child profile issued before the
        # storm cannot authorize a decision any more.
        with pytest.raises(PersistentSessionDenied, match="stale"):
            await chain.runtime_service.decide(
                runtime_profile_id=chain.child_profile.runtime_profile_id,
                capability="chat",
                actor_id=chain.account_id,
                data_classification="ephemeral",
                safety_state="normal",
                now=datetime.now(UTC),
            )
