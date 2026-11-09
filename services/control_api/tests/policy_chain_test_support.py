"""Shared fixtures, constants and helpers for the split persistent runtime policy chain tests."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from httpx import AsyncClient
from packages.contracts.generated.python.multi_subject_contracts import (
    RuntimeProfileSignedV2,
)
from services.archive.memory_domain import (
    MemorySearchResult,
)
from services.control_api.app.subject_verification import ensure_account_person
from services.control_api.tests.interaction_api_test_support import _MemoryCatalog
from services.guardian.domain import ConsentRecord
from services.session_runtime.commands import SwitchPersistentSubjectCommand
from services.session_runtime.service import (
    PostgresSessionRuntimeService,
)
from services.session_runtime.tests.postgres_store_test_support import (
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


class _SubjectScopedMemoryCatalog(_MemoryCatalog):
    """The account's claims answer only a read keyed to the account itself.

    Companion reads are keyed to the active subject's own lineage (4fc9be9):
    another subject's read targets that subject and finds nothing of the
    account owner's, which is what the real catalog does.
    """

    async def context(self, query: object) -> MemorySearchResult:
        result = await super().context(query)
        if getattr(query, "subject_id", None) != getattr(query, "account_id", None):
            return MemorySearchResult(items=())
        return result


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
    age_band: str = "under_14",
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
        age_band=age_band,
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
            consent_id=str(uuid.uuid4()),
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


async def _context_prefetch(
    client: AsyncClient,
    *,
    session_id: str,
    query: str,
) -> Any:
    return await client.post(
        "/v1/interaction/context-prefetch",
        headers={"X-Memoria-Internal-Token": _RESPONSE_PLAN_TOKEN},
        json={
            "session_id": session_id,
            "query": query,
            "speaker_decision": {
                "classification": "owner",
                "reason_code": "trusted",
                "model_version": "campplus-test-v1",
                "profile_id": "speaker-profile-1",
                "template_version": 1,
            },
        },
    )
