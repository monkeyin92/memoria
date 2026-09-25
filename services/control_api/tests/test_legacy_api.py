from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from services.control_api.app.main import create_app
from services.digital_self.compiler import canonical_manifest_bytes
from services.digital_self.domain import (
    CognitiveClaimManifestEntry,
    DecisionCaseManifestEntry,
    DigitalSelfManifest,
    DigitalSelfSourceSummary,
    DigitalSelfVersion,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    RelationshipProfileManifestEntry,
    VersionNotFoundError,
)
from services.legacy.domain import LegacyManifestItemRef, RegisteredGranteeSnapshot
from services.self_model.domain import RelationshipProfile, SelfModelNotFoundError
from services.speaker.domain import SpeakerProfileSummary
from services.voice_profile.domain import VoiceResolution

PASSWORD = "safe-password"


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "memoria.sqlite3"
    monkeypatch.setenv("MEMORIA_DB_PATH", str(path))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-test-s9")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setenv(
        "MEMORIA_RESPONSE_PLAN_TOKEN",
        "legacy-response-plan-token-that-is-long-enough",
    )
    return path


async def _register_verified_adult(
    client: AsyncClient,
    app: FastAPI,
    username: str,
) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/register",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 201
    registered = cast(dict[str, str], response.json())
    app.state.memory_store.update_subject_profile(
        user_id=registered["user_id"],
        subject_category="adult",
        birth_year_band="adult",
        age_evidence_status="verified",
        now=datetime.now(UTC).isoformat(),
    )
    login = await client.post(
        "/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert login.status_code == 200
    logged_in = cast(dict[str, str], login.json())
    assert logged_in["user_id"] == registered["user_id"]
    return logged_in


def _headers(account: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {account['access_token']}"}


def _relationship(owner_account_id: str) -> RelationshipProfile:
    now = datetime.now(UTC)
    return RelationshipProfile(
        profile_id="relationship-profile-1",
        account_id=owner_account_id,
        version_number=3,
        person_id="person-grantee",
        relationship_id="relationship-family",
        salutation="小梅",
        tone="warm",
        advice_style="listen-first",
        sharing_scope="family",
        boundaries=("不讨论未授权的医疗信息",),
        status="approved",
        unresolved_conflict=False,
        sources=(),
        owner_reviewed_at=now,
        step_up_verified=True,
        created_at=now,
    )


def _version(owner_account_id: str, relationship: RelationshipProfile) -> DigitalSelfVersion:
    relationship_entry = RelationshipProfileManifestEntry(
        profile_id=relationship.profile_id,
        version_number=relationship.version_number,
        person_id=relationship.person_id,
        relationship_id=relationship.relationship_id,
        salutation=relationship.salutation,
        tone=relationship.tone,
        advice_style=relationship.advice_style,
        sharing_scope=relationship.sharing_scope,
        boundaries=relationship.boundaries,
        support_source_event_ids=(),
        counterexample_source_event_ids=(),
    )
    memory_entry = MemoryClaimManifestEntry(
        claim_id="memory-family-story",
        category="life_story",
        subject_key="owner",
        predicate="remembers",
        value="一家人每年春天去看海。",
        confidence=0.95,
        sensitive_domain="family",
        extractor_version="test-v1",
        source_event_id="source-family-story",
        valid_at="2026-07-23T08:00:00+00:00",
    )
    manifest = DigitalSelfManifest(
        schema_version="digital-self-manifest-v3",
        compiler_version="compiler-v3",
        policy_version="policy-v3",
        parent_version_id=None,
        rollback_target_version_id=None,
        entries=(memory_entry, relationship_entry),
        source_summary=DigitalSelfSourceSummary(
            memory_claim_count=1,
            persona_trait_count=0,
            persona_version_id=None,
            source_summary_sha256="source-summary-s9",
            relationship_profile_count=1,
        ),
    )
    return DigitalSelfVersion(
        version_id="digital-self-frozen-7",
        account_id=owner_account_id,
        version_number=7,
        status="frozen",
        manifest=manifest,
        manifest_sha256=hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest(),
        created_at=datetime.now(UTC),
    )


class _Versions:
    def __init__(self, version: DigitalSelfVersion) -> None:
        self.version = version

    async def get(self, *, account_id: str, version_id: str) -> DigitalSelfVersion:
        if account_id != self.version.account_id or version_id != self.version.version_id:
            raise VersionNotFoundError(version_id)
        return self.version


class _SelfModels:
    def __init__(self, relationship: RelationshipProfile) -> None:
        self.relationship = relationship

    async def get_relationship_profile(
        self,
        *,
        account_id: str,
        profile_id: str,
        version_number: int | None = None,
    ) -> RelationshipProfile:
        relationship = self.relationship
        if (
            account_id != relationship.account_id
            or profile_id != relationship.profile_id
            or version_number not in {None, relationship.version_number}
        ):
            raise SelfModelNotFoundError(profile_id)
        return relationship


class _ActiveSpeakers:
    async def profiles(self, account_id: str) -> tuple[SpeakerProfileSummary, ...]:
        return (
            SpeakerProfileSummary(
                profile_id=f"speaker-{account_id}",
                identity_id=f"owner:{account_id}",
                template_version=1,
                model_version="test-v1",
                sample_count=3,
                status="active",
                evaluation_ref="test-evaluation",
                evaluation_sample_count=200,
                far=0.01,
                frr=0.01,
                eer=0.01,
                unknown_rejection=0.99,
                created_at="2026-07-23T08:00:00+00:00",
                activated_at="2026-07-23T08:00:00+00:00",
                revoked_at=None,
            ),
        )


class _VoiceMustNotResolve:
    def __init__(self) -> None:
        self.account_ids: list[str] = []

    async def resolve(self, *, account_id: str) -> VoiceResolution:
        self.account_ids.append(account_id)
        raise AssertionError("voice_allowed=false must not resolve an owner personal voice")


@dataclass(slots=True)
class _LegacyContext:
    app: FastAPI
    owner: dict[str, str]
    grantee: dict[str, str]
    version: DigitalSelfVersion
    relationship: RelationshipProfile
    versions: _Versions
    self_models: _SelfModels
    voice_manager: _VoiceMustNotResolve


async def _prepare(app: FastAPI, client: AsyncClient) -> _LegacyContext:
    owner = await _register_verified_adult(client, app, "legacy-owner")
    grantee = await _register_verified_adult(client, app, "legacy-grantee")
    relationship = _relationship(owner["user_id"])
    version = _version(owner["user_id"], relationship)
    versions = _Versions(version)
    self_models = _SelfModels(relationship)
    voice_manager = _VoiceMustNotResolve()
    app.state.digital_self_registry = versions
    app.state.self_model_registry = self_models
    app.state.speaker_authority = _ActiveSpeakers()
    app.state.voice_profile_manager = voice_manager
    return _LegacyContext(
        app=app,
        owner=owner,
        grantee=grantee,
        version=version,
        relationship=relationship,
        versions=versions,
        self_models=self_models,
        voice_manager=voice_manager,
    )


def _issue_body(
    context: _LegacyContext,
    *,
    idempotency_key: str = "issue-grant-1",
    password: str = PASSWORD,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "grantee_username": context.grantee["username"],
        "version_id": context.version.version_id,
        "relationship_profile_id": context.relationship.profile_id,
        "allowed_items": [
            {"kind": "memory_claim", "item_id": "memory-family-story"},
            {
                "kind": "relationship_profile",
                "item_id": context.relationship.profile_id,
            },
        ],
        "voice_allowed": False,
        "expires_at": (expires_at or datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "password": password,
        "idempotency_key": idempotency_key,
    }


async def _issue(client: AsyncClient, context: _LegacyContext, **overrides: Any) -> Response:
    body = _issue_body(context)
    body.update(overrides)
    return await client.post(
        "/v1/legacy/grants",
        headers=_headers(context.owner),
        json=body,
    )


async def _activate(
    client: AsyncClient,
    context: _LegacyContext,
    grant: dict[str, Any],
    *,
    idempotency_key: str = "activate-grant-1",
) -> Response:
    return await client.post(
        f"/v1/legacy/grants/{grant['grant_id']}/activate",
        headers=_headers(context.owner),
        json={
            "expected_grant_snapshot_sha256": grant["grant_snapshot_sha256"],
            "password": PASSWORD,
            "idempotency_key": idempotency_key,
        },
    )


@pytest.mark.asyncio
async def test_issue_requires_step_up_and_exact_frozen_approved_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await _prepare(app, client)

        wrong_password = await _issue(client, context, password="wrong-password")

        context.versions.version = replace(context.version, status="approved")
        not_frozen = await _issue(client, context, idempotency_key="issue-not-frozen")
        context.versions.version = context.version

        context.self_models.relationship = replace(context.relationship, status="candidate")
        not_approved = await _issue(client, context, idempotency_key="issue-not-approved")
        context.self_models.relationship = replace(
            context.relationship,
            salutation="不匹配的称呼",
        )
        relationship_not_exact = await _issue(
            client,
            context,
            idempotency_key="issue-relationship-not-exact",
        )
        context.self_models.relationship = context.relationship

        body = _issue_body(context, idempotency_key="issue-unknown-item")
        body["allowed_items"] = [{"kind": "memory_claim", "item_id": "outside-manifest"}]
        item_not_exact = await _issue(client, context, **body)
        issued = await _issue(client, context)

    assert wrong_password.status_code == 403
    assert wrong_password.json()["detail"] == {"code": "step_up_failed"}
    for response in (not_frozen, not_approved, relationship_not_exact, item_not_exact):
        assert response.status_code == 422
        assert response.json()["detail"] == {"code": "legacy_contract_invalid"}
    assert issued.status_code == 201
    assert issued.json()["status"] == "pending"
    assert issued.json()["voice_allowed"] is False


@pytest.mark.asyncio
async def test_issue_rejects_private_manifest_items_of_every_supported_kind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await _prepare(app, client)
        relationship_entry = next(
            entry
            for entry in context.version.manifest.entries
            if isinstance(entry, RelationshipProfileManifestEntry)
        )
        memory_entry = next(
            entry
            for entry in context.version.manifest.entries
            if isinstance(entry, MemoryClaimManifestEntry)
        )
        private_items = (
            (
                replace(memory_entry, sensitive_domain="private"),
                {"kind": "memory_claim", "item_id": memory_entry.claim_id},
            ),
            (
                PersonaTraitManifestEntry(
                    trait_id="private-persona",
                    persona_version_id="persona-version-1",
                    category="verbal_tic",
                    description="private style",
                    context="private",
                    counterexample="",
                    confidence=0.8,
                    source_event_ids=("private-persona-source",),
                ),
                {"kind": "persona_trait", "item_id": "private-persona"},
            ),
            (
                CognitiveClaimManifestEntry(
                    claim_id="private-cognitive",
                    claim_type="belief",
                    statement="private belief",
                    context="private",
                    confidence=0.9,
                    sharing_scope="private",
                    support_source_event_ids=("private-cognitive-source",),
                    counterexample_source_event_ids=(),
                ),
                {"kind": "cognitive_claim", "item_id": "private-cognitive"},
            ),
            (
                DecisionCaseManifestEntry(
                    case_id="private-decision",
                    kind="real",
                    context="private decision",
                    options=("a", "b"),
                    constraints=(),
                    chosen_option="a",
                    rejected_options=("b",),
                    outcome="private",
                    reflection="private",
                    still_endorsed=True,
                    sharing_scope="private",
                    support_source_event_ids=("private-decision-source",),
                    counterexample_source_event_ids=(),
                ),
                {"kind": "decision_case", "item_id": "private-decision"},
            ),
        )
        responses: list[Response] = []
        for entry, item in private_items:
            manifest = replace(
                context.version.manifest,
                entries=(entry, relationship_entry),  # type: ignore[arg-type]
            )
            context.versions.version = replace(
                context.version,
                manifest=manifest,
                manifest_sha256=hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest(),
            )
            responses.append(
                await _issue(
                    client,
                    context,
                    allowed_items=[item],
                    idempotency_key=f"private-{item['kind']}",
                )
            )

        private_relationship = replace(context.relationship, sharing_scope="private")
        private_relationship_entry = replace(relationship_entry, sharing_scope="private")
        private_manifest = replace(
            context.version.manifest,
            entries=(private_relationship_entry,),
        )
        context.versions.version = replace(
            context.version,
            manifest=private_manifest,
            manifest_sha256=hashlib.sha256(
                canonical_manifest_bytes(private_manifest)
            ).hexdigest(),
        )
        context.self_models.relationship = private_relationship
        responses.append(
            await _issue(
                client,
                context,
                allowed_items=[
                    {
                        "kind": "relationship_profile",
                        "item_id": private_relationship.profile_id,
                    }
                ],
                idempotency_key="private-relationship",
            )
        )

    assert all(response.status_code == 422 for response in responses)
    assert all(
        response.json()["detail"] == {"code": "legacy_contract_invalid"}
        for response in responses
    )


@pytest.mark.asyncio
async def test_lists_owner_and_grantee_envelopes_with_account_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await _prepare(app, client)
        issued = await _issue(client, context)
        assert issued.status_code == 201

        owner_list = await client.get(
            "/v1/legacy/grants",
            headers=_headers(context.owner),
            params={"role": "owner"},
        )
        grantee_list = await client.get(
            "/v1/legacy/grants",
            headers=_headers(context.grantee),
            params={"role": "grantee"},
        )
        wrong_role = await client.get(
            "/v1/legacy/grants",
            headers=_headers(context.owner),
            params={"role": "grantee"},
        )

    assert owner_list.status_code == grantee_list.status_code == 200
    assert owner_list.json()["role"] == "owner"
    assert grantee_list.json()["role"] == "grantee"
    assert wrong_role.json() == {"role": "grantee", "items": []}
    owner_item = owner_list.json()["items"][0]
    grantee_item = grantee_list.json()["items"][0]
    assert owner_item == grantee_item
    assert owner_item["owner_username"] == context.owner["username"]
    assert owner_item["grantee_username"] == context.grantee["username"]
    assert owner_item["grant_id"] == issued.json()["grant_id"]


@pytest.mark.asyncio
async def test_pending_owner_preview_and_activated_grantee_session_freeze_server_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await _prepare(app, client)
        issue_response = await _issue(client, context)
        assert issue_response.status_code == 201
        grant = issue_response.json()

        owner_preview = await client.post(
            "/v1/sessions",
            headers=_headers(context.owner),
            json={"interaction_mode": "legacy", "legacy_grant_id": grant["grant_id"]},
        )
        owner_session_id = owner_preview.json()["session_id"]
        owner_plan = await client.post(
            "/v1/interaction/response-plan",
            headers={
                "X-Memoria-Internal-Token": (
                    "legacy-response-plan-token-that-is-long-enough"
                )
            },
            json={
                "session_id": owner_session_id,
                "query": "我们每年春天会去哪里看海？",
                "fence": {
                    "session_id": owner_session_id,
                    "turn_id": 1,
                    "generation_id": 1,
                    "tool_epoch": 0,
                },
                "speaker_decision": {
                    "classification": "owner",
                    "reason_code": "trusted",
                    "model_version": "test-v1",
                    "profile_id": f"speaker-{context.owner['user_id']}",
                    "template_version": 1,
                },
            },
        )
        pending_grantee = await client.post(
            "/v1/sessions",
            headers=_headers(context.grantee),
            json={"interaction_mode": "legacy", "legacy_grant_id": grant["grant_id"]},
        )
        activated_response = await _activate(client, context, grant)
        assert activated_response.status_code == 200
        activated = activated_response.json()
        grantee_session = await client.post(
            "/v1/sessions",
            headers=_headers(context.grantee),
            json={"interaction_mode": "legacy", "legacy_grant_id": grant["grant_id"]},
        )

    assert owner_preview.status_code == 200
    assert owner_plan.status_code == 200
    owner_interaction = owner_preview.json()["interaction"]
    assert owner_interaction["actor_account_id"] == context.owner["user_id"]
    assert owner_interaction["resource_owner_account_id"] == context.owner["user_id"]
    assert owner_interaction["legacy_actor_role"] == "owner_preview"
    assert owner_interaction["legacy_shell_id"] is None
    assert pending_grantee.status_code == 409
    assert pending_grantee.json()["detail"] == {"code": "legacy_grant_unavailable"}

    assert grantee_session.status_code == 200
    interaction = grantee_session.json()["interaction"]
    assert interaction["actor_account_id"] == context.grantee["user_id"]
    assert interaction["resource_owner_account_id"] == context.owner["user_id"]
    assert interaction["legacy_actor_role"] == "grantee"
    assert interaction["legacy_grantee_account_id"] == context.grantee["user_id"]
    assert interaction["legacy_shell_id"]
    assert interaction["legacy_grant_id"] == grant["grant_id"]
    assert interaction["legacy_grant_snapshot_sha256"] == activated["grant_snapshot_sha256"]
    assert interaction["legacy_scope_sha256"] == grant["scope_sha256"]
    assert interaction["relationship_profile_id"] == context.relationship.profile_id
    assert interaction["relationship_profile_version"] == context.relationship.version_number
    assert interaction["legacy_voice_allowed"] is False
    assert interaction["voice_profile_id"] is None
    assert interaction["voice_provider"] is None
    assert interaction["fallback_voice_profile_id"] == "warm_companion"
    assert interaction["fallback_voice_provider"] == "volcengine_doubao"
    assert interaction["fallback_voice_model"] == "seed-tts-2.0"
    assert context.voice_manager.account_ids == []
    persisted = app.state.memory_store.get_voice_session_by_id(
        session_id=grantee_session.json()["session_id"]
    )
    assert persisted is not None
    assert persisted["user_id"] == context.grantee["user_id"]
    assert persisted["resource_owner_account_id"] == context.owner["user_id"]
    assert persisted["legacy_shell_id"] == interaction["legacy_shell_id"]
    assert persisted["legacy_scope_sha256"] == grant["scope_sha256"]
    owner_runtime_audits = [
        event
        for event in await app.state.legacy_registry.list_audit_events(
            actor_account_id=context.owner["user_id"],
            grant_id=grant["grant_id"],
        )
        if event.actor_account_id == context.owner["user_id"]
        and event.action in {"read_source", "plan_answer", "refuse"}
    ]
    assert owner_runtime_audits
    assert all(event.shell_id is None for event in owner_runtime_audits)


@pytest.mark.asyncio
async def test_legacy_response_plan_uses_exact_scope_and_revocation_stops_next_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await _prepare(app, client)
        issued = (await _issue(client, context)).json()
        activated = (
            await _activate(client, context, issued)
        ).json()
        created = await client.post(
            "/v1/sessions",
            headers=_headers(context.grantee),
            json={
                "interaction_mode": "legacy",
                "legacy_grant_id": activated["grant_id"],
            },
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        plan_body = {
            "session_id": session_id,
            "query": "我们每年春天会去哪里看海？",
            "fence": {
                "session_id": session_id,
                "turn_id": 1,
                "generation_id": 1,
                "tool_epoch": 0,
            },
            "speaker_decision": {
                "classification": "owner",
                "reason_code": "trusted",
                "model_version": "test-v1",
                "profile_id": f"speaker-{context.grantee['user_id']}",
                "template_version": 1,
            },
        }
        planned = await client.post(
            "/v1/interaction/response-plan",
            headers={
                "X-Memoria-Internal-Token": (
                    "legacy-response-plan-token-that-is-long-enough"
                )
            },
            json=plan_body,
        )
        planned_duplicate = await client.post(
            "/v1/interaction/response-plan",
            headers={
                "X-Memoria-Internal-Token": (
                    "legacy-response-plan-token-that-is-long-enough"
                )
            },
            json=plan_body,
        )
        refusal_body = {
            **plan_body,
            "query": "授权资料中没有的事情是什么？",
            "fence": {
                **plan_body["fence"],
                "turn_id": 2,
                "generation_id": 2,
            },
        }
        refusal = await client.post(
            "/v1/interaction/response-plan",
            headers={
                "X-Memoria-Internal-Token": (
                    "legacy-response-plan-token-that-is-long-enough"
                )
            },
            json=refusal_body,
        )
        revoked = await client.post(
            f"/v1/legacy/grants/{activated['grant_id']}/revoke",
            headers=_headers(context.owner),
            json={
                "expected_grant_snapshot_sha256": activated[
                    "grant_snapshot_sha256"
                ],
                "password": PASSWORD,
                "idempotency_key": "revoke-after-plan",
            },
        )
        plan_body["fence"] = {
            **plan_body["fence"],
            "turn_id": 3,
            "generation_id": 3,
        }
        denied = await client.post(
            "/v1/interaction/response-plan",
            headers={
                "X-Memoria-Internal-Token": (
                    "legacy-response-plan-token-that-is-long-enough"
                )
            },
            json=plan_body,
        )

    assert planned.status_code == 200
    assert planned_duplicate.json() == planned.json()
    assert refusal.status_code == 200
    assert refusal.json()["provenance"]["disclosures"] == ["digital_identity", "unknown"]
    payload = planned.json()
    assert payload["epistemic_status"] == "fact"
    assert [(item["kind"], item["item_id"]) for item in payload["grounded_items"]] == [
        ("memory_claim", "memory-family-story")
    ]
    provenance = payload["provenance"]
    assert provenance["actor_account_id"] == context.grantee["user_id"]
    assert provenance["resource_owner_account_id"] == context.owner["user_id"]
    assert provenance["legacy_actor_role"] == "grantee"
    assert provenance["legacy_grant_id"] == activated["grant_id"]
    assert provenance["legacy_scope_sha256"] == activated["scope_sha256"]
    assert "digital_identity" in provenance["disclosures"]
    assert revoked.status_code == 200
    assert denied.status_code == 409
    assert denied.json()["detail"] == {"code": "response_plan_unavailable"}
    audits = await app.state.legacy_registry.list_audit_events(
        actor_account_id=context.owner["user_id"],
        grant_id=activated["grant_id"],
    )
    runtime = [
        event
        for event in audits
        if event.action in {"read_source", "plan_answer", "refuse"}
    ]
    assert [(event.action, event.reason) for event in runtime if event.action == "plan_answer"] == [
        ("plan_answer", "answer_planned")
    ]
    assert {
        (event.target_kind, event.target_id)
        for event in runtime
        if event.action == "read_source"
    } == {
        ("memory_claim", "memory-family-story"),
        ("relationship_profile", context.relationship.profile_id),
    }
    assert [(event.decision, event.reason) for event in runtime if event.action == "refuse"] == [
        ("denied", "unknown_refusal")
    ]
    assert all(event.session_id == session_id and event.tool_epoch == 0 for event in runtime)
    assert all(not hasattr(event, "payload") for event in runtime)

@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resource_owner_account_id", "forged-owner"),
        ("digital_self_version_id", "forged-version"),
        ("legacy_scope_sha256", "f" * 64),
        ("legacy_actor_role", "owner_preview"),
        ("relationship_profile_version", 99),
    ],
)
@pytest.mark.asyncio
async def test_legacy_session_rejects_client_owned_authority_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await _prepare(app, client)
        issued = await _issue(client, context)
        body: dict[str, object] = {
            "interaction_mode": "legacy",
            "legacy_grant_id": issued.json()["grant_id"],
            field: value,
        }
        response = await client.post(
            "/v1/sessions",
            headers=_headers(context.owner),
            json=body,
        )

    assert response.status_code == 422
    assert app.state.memory_store.list_voice_sessions(user_id=context.owner["user_id"]) == ()


@pytest.mark.asyncio
async def test_revoked_and_expired_grants_are_rejected_immediately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await _prepare(app, client)
        issued_response = await _issue(client, context)
        grant = issued_response.json()
        activated_response = await _activate(client, context, grant)
        activated = activated_response.json()
        active_session = await client.post(
            "/v1/sessions",
            headers=_headers(context.grantee),
            json={"interaction_mode": "legacy", "legacy_grant_id": grant["grant_id"]},
        )
        revoked_shell_id = active_session.json()["interaction"]["legacy_shell_id"]
        revoked_preferences_body = {
            "expected_revision": 1,
            "preferred_response_length": "brief",
            "question_frequency": "rare",
            "idempotency_key": "revoked-preferences",
        }
        assert (
            await client.patch(
                f"/v1/legacy/shells/{revoked_shell_id}/preferences",
                headers=_headers(context.grantee),
                json=revoked_preferences_body,
            )
        ).status_code == 200
        revoked_response = await client.post(
            f"/v1/legacy/grants/{grant['grant_id']}/revoke",
            headers=_headers(context.owner),
            json={
                "expected_grant_snapshot_sha256": activated["grant_snapshot_sha256"],
                "password": PASSWORD,
                "idempotency_key": "revoke-grant-1",
            },
        )
        assert revoked_response.status_code == 200
        revoked_session = await client.post(
            "/v1/sessions",
            headers=_headers(context.grantee),
            json={"interaction_mode": "legacy", "legacy_grant_id": grant["grant_id"]},
        )
        revoked_preferences = await client.patch(
            f"/v1/legacy/shells/{revoked_shell_id}/preferences",
            headers=_headers(context.grantee),
            json=revoked_preferences_body,
        )
        revoked_shell_gets = [
            await client.get(
                f"/v1/legacy/shells/{revoked_shell_id}/preferences",
                headers=_headers(actor),
            )
            for actor in (context.owner, context.grantee)
        ]
        revoked_list = await client.get(
            "/v1/legacy/grants",
            headers=_headers(context.grantee),
            params={"role": "grantee"},
        )

        current = datetime.now(UTC)
        expired = await app.state.legacy_registry.issue(
            owner_account_id=context.owner["user_id"],
            grantee=RegisteredGranteeSnapshot(
                account_id=context.grantee["user_id"],
                registered_at=current - timedelta(days=2),
            ),
            version=context.version,
            relationship_profile=context.relationship,
            allowed_items=(LegacyManifestItemRef("memory_claim", "memory-family-story"),),
            visibility="family",
            voice_allowed=False,
            expires_at=current - timedelta(seconds=1),
            idempotency_key="issue-already-expired",
            now=current - timedelta(minutes=2),
        )
        expired = await app.state.legacy_registry.activate(
            actor_account_id=context.owner["user_id"],
            grant_id=expired.grant_id,
            expected_grant_snapshot_sha256=expired.grant_snapshot_sha256,
            idempotency_key="activate-before-expiry",
            now=current - timedelta(minutes=1, seconds=30),
        )
        expired_access = await app.state.legacy_registry.resolve_access(
            actor_account_id=context.grantee["user_id"],
            grant_id=expired.grant_id,
            purpose="grantee_session",
            now=current - timedelta(minutes=1),
        )
        expired_preferences_body = {
            "expected_revision": 1,
            "preferred_response_length": "brief",
            "question_frequency": "rare",
            "idempotency_key": "expired-preferences",
        }
        await app.state.legacy_registry.update_shell_preferences(
            actor_account_id=context.grantee["user_id"],
            shell_id=expired_access.shell_id or "",
            preferences=(
                ("preferred_response_length", "brief"),
                ("question_frequency", "rare"),
            ),
            expected_shell_revision=1,
            idempotency_key="expired-preferences",
            now=current - timedelta(seconds=30),
        )
        expired_session = await client.post(
            "/v1/sessions",
            headers=_headers(context.owner),
            json={"interaction_mode": "legacy", "legacy_grant_id": expired.grant_id},
        )
        expired_preferences = await client.patch(
            f"/v1/legacy/shells/{expired_access.shell_id}/preferences",
            headers=_headers(context.grantee),
            json=expired_preferences_body,
        )
        expired_shell_gets = [
            await client.get(
                f"/v1/legacy/shells/{expired_access.shell_id}/preferences",
                headers=_headers(actor),
            )
            for actor in (context.owner, context.grantee)
        ]
        expired_list = await client.get(
            "/v1/legacy/grants",
            headers=_headers(context.grantee),
            params={"role": "grantee"},
        )

    assert revoked_session.status_code == expired_session.status_code == 409
    assert revoked_session.json()["detail"] == {"code": "legacy_grant_unavailable"}
    assert expired_session.json()["detail"] == {"code": "legacy_grant_unavailable"}
    assert revoked_preferences.status_code == expired_preferences.status_code == 403
    assert revoked_preferences.json()["detail"] == {"code": "legacy_access_denied"}
    assert expired_preferences.json()["detail"] == {"code": "legacy_access_denied"}
    assert all(response.status_code == 403 for response in revoked_shell_gets)
    assert all(response.status_code == 403 for response in expired_shell_gets)
    assert revoked_list.json()["items"][0]["shell"] is None
    assert all(item["shell"] is None for item in expired_list.json()["items"])


@pytest.mark.parametrize("deleting_role", ["owner", "grantee"])
@pytest.mark.asyncio
async def test_account_deletion_blocks_deleting_actor_and_counterparty_legacy_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    deleting_role: str,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await _prepare(app, client)
        issued = (await _issue(client, context)).json()
        activated = await _activate(client, context, issued)
        assert activated.status_code == 200
        actor = context.owner if deleting_role == "owner" else context.grantee
        counterparty = context.grantee if deleting_role == "owner" else context.owner
        app.state.memory_store.begin_account_deletion(
            user_id=actor["user_id"],
            started_at=datetime.now(UTC).isoformat(),
        )
        deleting_actor_response = await client.post(
            "/v1/sessions",
            headers=_headers(actor),
            json={"interaction_mode": "legacy", "legacy_grant_id": issued["grant_id"]},
        )
        counterparty_response = await client.post(
            "/v1/sessions",
            headers=_headers(counterparty),
            json={"interaction_mode": "legacy", "legacy_grant_id": issued["grant_id"]},
        )

    assert deleting_actor_response.status_code == 401
    assert deleting_actor_response.json()["detail"] == "access session is unavailable"
    assert counterparty_response.status_code == 409
    assert counterparty_response.json()["detail"] == {"code": "legacy_grant_unavailable"}


@pytest.mark.asyncio
async def test_only_grantee_updates_shell_preferences_with_strict_revision_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context = await _prepare(app, client)
        issued = (await _issue(client, context)).json()
        activated = await _activate(client, context, issued)
        assert activated.status_code == 200
        session = await client.post(
            "/v1/sessions",
            headers=_headers(context.grantee),
            json={"interaction_mode": "legacy", "legacy_grant_id": issued["grant_id"]},
        )
        shell_id = session.json()["interaction"]["legacy_shell_id"]
        first_body = {
            "expected_revision": 1,
            "preferred_response_length": "brief",
            "question_frequency": "rare",
            "idempotency_key": "shell-preferences-1",
        }
        owner_update = await client.patch(
            f"/v1/legacy/shells/{shell_id}/preferences",
            headers=_headers(context.owner),
            json=first_body,
        )
        first = await client.patch(
            f"/v1/legacy/shells/{shell_id}/preferences",
            headers=_headers(context.grantee),
            json=first_body,
        )
        replay = await client.patch(
            f"/v1/legacy/shells/{shell_id}/preferences",
            headers=_headers(context.grantee),
            json=first_body,
        )
        reused_key = await client.patch(
            f"/v1/legacy/shells/{shell_id}/preferences",
            headers=_headers(context.grantee),
            json={**first_body, "preferred_response_length": "detailed"},
        )
        stale_revision = await client.patch(
            f"/v1/legacy/shells/{shell_id}/preferences",
            headers=_headers(context.grantee),
            json={**first_body, "idempotency_key": "shell-preferences-stale"},
        )
        current = await client.get(
            f"/v1/legacy/shells/{shell_id}/preferences",
            headers=_headers(context.grantee),
        )
        listed = await client.get(
            "/v1/legacy/grants",
            headers=_headers(context.grantee),
            params={"role": "grantee"},
        )

    assert owner_update.status_code == 404
    assert owner_update.json()["detail"] == {"code": "legacy_resource_not_found"}
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["revision"] == 2
    assert first.json()["preferences"] == {
        "preferred_response_length": "brief",
        "question_frequency": "rare",
    }
    assert reused_key.status_code == 409
    assert reused_key.json()["detail"] == {"code": "legacy_idempotency_conflict"}
    assert stale_revision.status_code == 409
    assert stale_revision.json()["detail"] == {"code": "legacy_snapshot_conflict"}
    assert current.json() == first.json()
    assert listed.json()["items"][0]["shell"] == first.json()
